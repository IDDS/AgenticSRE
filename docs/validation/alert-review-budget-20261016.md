# 告警关系审核预算与部分结果验证（2026-10-16）

## 结论与交付范围

论文调研后的第一步已实现并在远程隔离候选环境验证：关系审核具有作业总预算；预算耗尽、批次失败或模型不可用时，仍发布可读取的事实结果；日志文本中的数值极值及其原文 ID 进入模型审核上下文。最终候选通过 **1,164 项回归测试**，另通过远程 Chromium 的实际前端函数与模拟 API 联动检查。

这一步解决审核资源上限和未完成结果不可读的问题。它没有实现跨扫描审核缓存、经过校准的候选路由、SOP 检索或图模型，也没有完成真实 LLM 下的等质量成本对照。本报告记录的是发布前验证快照，当时线上镜像未切换、代码未提交；用户随后授权发布，提交、生产配置及正式后端验收见[上线记录](alert-review-release-20261016.md)。

候选镜像：`agenticsre:alert-review-budget-20261016-r2`。

镜像 ID：`sha256:cefa5ebf8ae6df50143a5fd10bf5aa1329d74ad22b2e5291e9a644b1165dd3a1`。

全部构建、Python 测试、浏览器执行和模型桩对照均在 `snail` 完成。本机仅编辑源码、读取和归档证据；未安装依赖或运行构建测试。测试容器禁用网络，使用占位凭据，真实 LLM 调用次数为 0。

## 行为与使用方法

### 作业预算

`POST /api/alerts/scan-jobs` 和 `POST /api/alerts/compression-jobs` 支持可选 `review_budget`。未填写的字段采用下表默认值。预算约束适用于关系审核阶段，采集、候选检索、图构造和结果落盘不计入审核时限，因此 120 秒不是整个扫描请求的完成承诺。

| 字段 | 默认值 | 允许的最大值 | 计量含义 |
|---|---:|---:|---|
| `max_model_calls` | 8 | 100 | 调用前持久化预留的尝试次数 |
| `max_review_seconds` | 120 | 3,600 | 从首次进入审核阶段起的绝对时限，单位秒 |
| `max_input_bytes` | 524,288 | 8,388,608 | 所有调用的序列化 messages UTF-8 字节总额 |
| `max_batch_input_bytes` | 65,536 | 262,144 | 单次调用的同口径输入字节上限 |
| `max_output_tokens` | 32,768 | 262,144 | 调用前预留的输出 token 总额度 |

字段必须非负；除时限可为有限浮点数外，其余必须是整数，不接受布尔值、未知字段或越界数值。非法预算在入队前返回 HTTP 400。任一上限先触达即停止后续调用，默认 8 次不表示必定能够调用 8 次。

例如，实时扫描请求体可为：

```json
{
  "cluster_id": "configured-cluster-id",
  "namespace": "demo",
  "review_budget": {
    "max_model_calls": 2,
    "max_review_seconds": 60,
    "max_input_bytes": 131072,
    "max_batch_input_bytes": 65536,
    "max_output_tokens": 16384
  }
}
```

`cluster_id` 应替换为系统已有集群 ID。实时扫描是否启用模型取决于该集群配置；导入作业还需提交 `alerts` 并设置 `semantic: true`。

预算写入作业 SQLite，调用之前通过事务预留。失败或崩溃不会返还预留额度，重启和恢复也不重置计数或延长绝对 deadline。生产 `LLMClient` 的此次请求关闭 SDK 自动重试，并使用剩余时限约束请求超时。其他调用路径不传入这些新选项时保持原有客户端默认设置。

`model_calls` 是预留尝试次数，包含可能发生在实际网络发送前的崩溃，不能严格等同于供应商已计费请求。输入统计采用 UTF-8 字节，输出采用最大额度预留；两者都不是供应商实际 token 用量。接口的 `actual_input_tokens`、`actual_output_tokens` 保持 `null`，避免把估算值当成真实账单。低于 256 个剩余输出 token 时停止审核；未用完的输出预留暂不返还。

### 部分结果与恢复

审核未完成时作业返回 `partial`，提供 `scan_id`、`result_revision` 和 `result_url`，原始告警、事实组、证据包及现有诊断入口仍可访问。语义结果记录 `completed_pairs`、`failed_pairs`、`deferred_pairs` 和停止原因。**“审核完成”表示该候选已获得可解析的模型答复，不表示关系已被确认；未审核也不表示无关。**模型提出的关系仍接受原有引用和传播证据检查。

可以从以下接口读取覆盖详情：

```text
GET /api/alerts/compression-jobs/{job_id}
GET /api/alerts/compression/{scan_id}/summary
GET /api/alerts/compression/{scan_id}/page?section=review_pairs&offset=0&limit=50
GET /api/alerts/compression/{scan_id}/page?section=raw_alerts&offset=0&limit=50
POST /api/alerts/compression-jobs/{job_id}/resume
```

前端显示已审核、失败及待审数量，提供关系审核清单分页；预算用尽后隐藏重试按钮。恢复请求继续使用原预算，仅重试失败或未完成批次，成功 checkpoint 保留原批次边界和结果。预算已经耗尽时恢复返回 HTTP 409。没有提供给旧作业追加预算的接口；新扫描代表新观测，不应以不断创建同一快照的新作业绕过预算控制。

`can_resume` 当前反映额度和时限是否还有余量，不保证剩余候选能放入该额度。若某个候选的必需证据超过单批上限，该候选完整延期；若剩余总字节无法容纳下一批，也会停止。对这些情况，原预算下重复恢复未必推进，应查看待审原因。自适应剩余额度组批、单个大候选分块及更细的恢复可行性提示属于后续改进。

首次发布部分结果会记录生命周期；后续恢复沿用这次观测，不重复累计。恢复发布递增结果版本；分页或 dossier 的旧版本引用需刷新。若人工已经编辑结果，恢复和发布检查会拒绝覆盖。缺失预算账本的旧 checkpoint 按未知已耗额度保守处理，不重新授予完整预算。

### 关键事实进入审核上下文

以前审核提示主要使用每组首条告警和上下文。此次新增 `group_evidence`：组内计数、时间范围、首尾和代表样本、状态信息、数值最小/最大值以及来源 ID。日志数值从完整原文提取，因此即便展示片段被截到 500 字，位于其后的 `9,000 ms` 峰值仍可以结构化数值及来源 ID 进入提示。已有实时 Prometheus 指标上下文继续保留。

关系引用必须来自本次提示所带样本，并同时覆盖左右两组。数值样本是日志中的标量观测，不包装成连续指标序列。必需证据不通过静默裁剪来满足单批上限；无法容纳时保留原文并延期该候选。

## 验证结果

### 最终候选回归

| 检查 | 结果 | 证据 |
|---|---|---|
| 针对性 Python 回归 | 88 passed，5 warnings，12.67 秒 | `targeted-r2.log`、`targeted-r2.xml` |
| 全量 Python 回归 | 1,164 passed，5 warnings，228.60 秒 | `full-r2.log`、`full-r2.xml` |
| JavaScript 语法 | `node --check` 退出码 0 | `runner-r2.log`、`js-syntax-r2.log` |
| Chromium 组件流程 | 通过，页面异常 0 | `browser-results.json`、`browser-review.cjs` |
| 源码与镜像一致性 | 逐一比较本轮 9 个源文件 SHA256 | `metadata.json` |

测试容器使用 2 CPU、4 GiB 内存、`--network none`。5 条 warning 为既有 FastAPI/Starlette 弃用提示。较上一轮 1,129 项回归增加 35 项测试用例；新用例覆盖额度/时限、调用前落账、崩溃恢复、失败批次恢复、日志中间极值、旧账本迁移、人工编辑保护、API 参数校验和部分结果读取。早期 r1 的 1,154 项结果仅作过程记录，验收以最终 r2 为准。

浏览器加载实际 `web_app/static/js/app.js`，使用由候选后端生成的模拟 API fixture，运行实际轮询、渲染和分页函数。确认全部 56 个事实组具有“证据包诊断”入口，待审 35 对可分页，额度耗尽后无重试按钮，版本变化提示刷新。此检查是组件流程验证；没有启动线上后端，也没有点击触发真实诊断。后端接口另由 TestClient 回归验证，因此不将其表述为线上端到端诊断测试。

### 同一合成输入的旧版/候选版对照

使用同一组 56 条告警和预先固定的 55 对候选关系，分别运行旧版 `agenticsre:alert-multilingual-20261015-r2` 与最终候选。两者请求都指定 `max_model_calls: 1`；旧版未实现此字段。模型桩始终返回空关系数组。此实验只度量工作量与覆盖，不提供关系准确率真值。

| 指标 | 旧版 | 最终候选 |
|---|---:|---:|
| 模型桩调用 | 3 | 1 |
| 送入审核的候选对 | 55 | 20 |
| 明确延期的候选对 | 0 | 35 |
| 保留的原始告警 | 56 | 56 |
| 序列化提示 UTF-8 总字节 | 70,636 | 51,195 |
| 作业状态 | completed | partial |

调用减少伴随审核覆盖减少，不能据此声称等质量成本降低 67%。候选版为每对关系增加关键事实，单次提示更大；之后需要在相同诊断后端、等 token/调用预算、独立事故标签条件下，评估关键证据覆盖和最终诊断质量。原始记录为 `probe-baseline.json`、`probe-candidate.json`，复现实验脚本为 `budget-probe.py`。

## 复现与证据

远程工作目录：`/home/ubuntu/AgenticSRE-releases/alert-review-budget-20261016`。其 `run-remote.py r2` 使用已存在的离线基础镜像、远程测试依赖和测试 wrapper，依次构建候选、执行针对性和全量测试、检查 JavaScript 语法。不得在本机执行该脚本。

```sh
ssh snail 'cd /home/ubuntu/AgenticSRE-releases/alert-review-budget-20261016 && python3 run-remote.py r2'
```

浏览器用例需要该服务器既有 Playwright 路径及 `browser-fixture.json`，运行 `node browser-review.cjs`。`budget-probe.py` 在选定镜像的禁网容器中运行，分别传入 `baseline` 或 `candidate`，挂载本次目录为 `/qa`；具体镜像与输出参见上述对照。证据包还包含 Dockerfile、本轮源文件、源码归档、测试日志、JUnit XML、截图和文件哈希清单。

本地证据目录：[outputs/alert-review-budget-20261016](../../outputs/alert-review-budget-20261016/)。归档及最终生产状态核验见其中 `package-result.json`、`metadata.json`、`evidence-manifest.json`。截图使用测试页面的最小样式，仅证明对应交互和状态，不代表生产页面的视觉验收。

证据包已下载到本地：[evidence.tar.gz](../../outputs/alert-review-budget-20261016/evidence.tar.gz)，大小 1,509,210 字节，包含 43 个文件。远程与本地 SHA256 一致：`2a91d94ecce9dc9ed95da0553c9c7ba4c04c76857321d32adf2cac92db5ed758`。包内 `validation-report.md` 为归档前报告，不含本段对归档本身的哈希记录。九个本轮源文件的本地、远程及候选镜像哈希一致。

2026-10-16 09:44（北京时间）只读核验：`agenticsre` 与 `agenticsre-skill-runtime` 均为 `running healthy`，继续使用 `sha256:bb24c029edb678acf5ae97e6e10bbfc491c899f572f34b619753de8e6dc65d93`，未部署本次候选。新截图见[部分结果](../../outputs/alert-review-budget-20261016/partial-review.png)与[待审分页](../../outputs/alert-review-budget-20261016/review-pagination.png)。

当前可采用结论是：将有预算的审核、可读部分结果及关键事实引用作为后续回放的基线。默认预算尚未通过生产负载调优，真实 token、延迟尾部、误关联/漏关联及诊断准确率收益均待验证。跨扫描有效结果复用、按新增证据收益选择候选、独立故障配对回放仍是下一阶段任务。
