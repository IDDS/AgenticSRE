# 告警生命周期与真实故障闭环验证

## 本轮范围

补齐在线扫描之间的告警身份、持续触发、解除和再次触发记录，并通过远端真实 Job 故障验证压缩组进入多智能体诊断、任务恢复、告警解除及复发。实际测试还暴露了容器退出证据缺失和未证实故障类型被关键词改写的问题，本轮一并修复。

## 生命周期语义

Prometheus 告警按稳定标签和资源身份建立 incident ID，同一告警跨扫描保持身份。Job 告警不因采集器 Pod、instance 或严重度变化而变成新事故；不同 job_name、命名空间和集群保持隔离。时间字段表示系统首次和最近观测，不冒充上游首次触发时间。

状态为 `active → pending_resolution → resolved`。只有连续至少两次完整扫描未检出、且距首次未检出至少 30 秒，才能标记 resolved。再次出现时沿用 incident ID，episode 加一。resolved 的含义是“不再出现在完整 firing 快照中”，不是业务恢复已由系统独立确认，因此 `business_recovery_verified` 保持 false。

Kubernetes Warning 按事件 UID 记录为 observed。历史事件不等于当前故障；从完整事件列表消失时记录 event_missing，达到同样确认条件后为 expired，不能转换为业务恢复。旧 Warning 在任务成功后继续存在是正常现象。

SQLite 事务持久化状态。扫描 ID 保证重复处理幂等；按集群、命名空间和来源设置观测时间水位，旧的全局扫描不能覆盖更新的命名空间扫描。失败采集、异常返回格式、导入、人工编辑和乱序扫描不会关闭在线告警。空的合法结果与缺少结果字段明确区分。存储不可用时保留原始扫描并明确标记生命周期不可用。

实际检查还发现 Prometheus 工具默认只返回 50 条记录。告警采集现显式请求最多 5000 条，并检查总数与截断标记；Kubernetes 事件检查列表上限与分页标记。任一来源截断时扫描降级，禁止以缺失记录推断解除。普通工具查询仍保留默认 50 条上限，避免无界扩大诊断上下文。

## 接口与界面

- `/api/alerts/scan` 更新生命周期，并在分组中附带 incident ID、episode、首次/最近观测和观测次数。
- `/api/alerts/lifecycle?cluster_id=...&namespace=...` 返回指定作用域最近更新的最多 200 条记录，包括最近 20 条状态变迁。
- 告警压缩页面显示持续触发、历史事件、再次触发及解除状态，并提供历史查询。
- RCA 证据包保留有限的生命周期摘要；历史扫描仍表示当次观测，不应当作实时状态。

生命周期随完整扫描推进，本轮没有新增独立的定时扫描调度器。没有扫描时，系统不能推断告警已经解除。

## 实际测试发现并修复的诊断问题

首轮测试命名空间 `agenticsre-diag-e2e-lifecycle-81ea26`，诊断运行 `rca-e3269285`。

真实 Job 容器执行退出码 7 后进入 Failed。原事件智能体只保留 Pod 相位 Failed，丢失 terminated.exitCode，诊断正文因此明确表示具体机制不明。本轮将非零容器退出码、终止原因、信号、时间和容器名保留为 `container_terminated` 结构化证据；正常退出码 0 不生成故障证据。

此外，分类器在说明“无法确认镜像拉取失败”的文本中匹配关键词，将 `job_failed` 改成 `image_pull_error`，与诊断正文矛盾。现在对 job_failed、pod_failed、container_terminated 这类观测状态保留原类型，不能用被排除或未证实的机制词将其升级为具体根因。该修复不代表已解决任意自然语言否定语义问题。

首轮恢复、解除、复发及最终解除均通过，资源已清理，但诊断不通过严格验收。初版测试的宽松关键词判断曾将“退出码未采集”当作命中；现已修正，要求结构化证据确实含退出码 7，且根因正文引用该退出机制。原始文件保留，另存严格审计结论，未将其计入诊断成功样本。

第二轮命名空间 `agenticsre-diag-e2e-lifecycle-6b4330`，诊断运行 `rca-f6251ac6`。诊断已经获得真实退出码 7，正文也明确指出镜像拉取分类不成立，但最终分类字段仍为 image_pull_error。因此第二轮同样不计为诊断通过，尽管生命周期全部通过、原版脚本返回 passed。

进一步修复以目标对象匹配的实时结构化终止证据校验分类：目标容器非零退出，且没有匹配对象的当前镜像拉取等待状态时，不能保留 image_pull_error。OOMKilled 单独归为资源不足；不相关对象、混合退出类别及仍存在镜像等待证据的情况不做该改写。对第二轮原始证据执行隔离回放，分类已变为 application_crash，根因正文保持不变。最终验收新增分类与退出事实一致的要求。

## 远端回归

最终候选镜像全套 **605 项测试通过，104.00 秒**，包含生命周期、跨作用域水位、乱序、重复处理、进程重建读取、失败采集防误关闭、历史事件过期、退出码取证、结果截断和故障分类回归。测试在远端禁网隔离容器执行；依赖中旧 pytest 的 Python 3.12 AST 弃用警告不影响通过结果。已部署应用代码版本 `427a56b`，应用和 Skill runtime 均使用 `agenticsre:lifecycle-20260916`。

## 异常采集、持久化和界面验证

对实际 Prometheus 采集工具指定进程内不可连接的地址，连续两次得到连接拒绝；同次 Kubernetes 采集成功。扫描明确降级、生命周期处理拒绝推进，数据库中原有 active 告警没有被误解除。该测试没有修改线上配置。

容器重建后，通过真实 API 读取首轮测试记录，incident ID、episode=2 和 resolved 状态仍然保留。浏览器经 SSH 隧道访问远端应用，验证压缩分组生命周期信息及最近 200 条历史记录查询；截图保留于原始证据目录。

## 最终严格实测

第三轮命名空间 `agenticsre-diag-e2e-lifecycle-0907b4`，诊断运行 `rca-41ed9c4d`，输入快照 `scan-8122dac97da548cb9603dae61290f8fa`。请求只包含压缩组引用、命名空间和去重 ID，没有注入预设根因或退出码答案。

最终诊断类型为 `job_failed`，置信度 0.602，耗时 495.76 秒。正文确认“容器退出 7 → Pod Failed → Job 失败 → 触发 AgenticSRELifecycleProbe”的直接链路，并明确更上游原因尚无足够证据。真实结构化退出证据、正文及分类的一致性验收通过。

完整脚本退出码为 0，最终 `status=passed`、`diagnosis_correct=true`、`lifecycle_passed=true`、`cleanup_verified=true`。同一事故 `incident-ae1cfefe45b7c529ef4093fb` 的实测序列如下：

| 阶段 | 实际 Job 状态 | 生命周期 | episode |
| --- | --- | --- | --- |
| 首次故障 | Failed，退出 7 | active | 1 |
| 重复扫描 | 同一故障仍存在 | active，观测次数 1→2，ID 不变 | 1 |
| 首次恢复 | Complete，退出 0 | resolved | 1 |
| 再次故障 | Failed，退出 7 | active，ID 不变 | 2 |
| 最终恢复 | Complete，退出 0 | resolved | 2 |

测试命名空间已删除，远端没有遗留本轮 lifecycle 测试命名空间，应用及 Skill runtime 均为 healthy。最终原始报告为 `outputs/alert-lifecycle-20260916/agenticsre-lifecycle-live-verified.json`，精简汇总为同目录 `lifecycle-verified-summary.json`；前两轮原始报告及严格拒绝结论另行保留。

## 验证边界

实测使用真实 Kubernetes Job 和 kube_job_failed 指标，由独立命名空间中的测试规则生成告警。为缩短测试时间，规则设置 for=0s、interval=15s；这些时序不代表生产规则配置。恢复由测试脚本将本次 Job 从退出 7 替换为退出 0 完成，不能据此宣称系统已实现自主修复。

本场景验证直接退出机制、告警压缩到诊断的连接以及解除和复发记录，不能推出任意业务根因的准确率。退出码 7 的业务含义仍需业务证据。原设计中的全量日志/指标异常自动接入、完整 token 预算约束及反馈学习效果，不在本轮完成声明内。

第三轮进入诊断的实际快照为 2 条原始记录、2 个组，保留 2/2、遗漏 0、压缩率 0%。两条记录分别来自 Prometheus 告警和 Kubernetes BackoffLimitExceeded Warning；语义候选关联因缺少足够统计证据保持 unverified，未错误宣称因果确认。该快照适合闭环验收，不能用于证明告警风暴的压缩率。实时 Jaeger 查询返回 ConnectionError，Trace 上下文标记 unavailable；当前多源关联能力仍受观测源可用性限制。

诊断日志仍显示 Job 场景调用了 `k8s.service_unavailable` Skill，返回 unknown；本轮最终结论依靠实时退出证据得到纠正。因此不能把此次通过视为所有 Skill 路由都已有效。后续重点包括 Job 专用 Skill 的选择与有效性验证、Trace 接入，以及约 8 分钟诊断时延的优化。

原始证据与截图位于 `outputs/alert-lifecycle-20260916/`。实际故障脚本为 `tests/e2e/test_live_alert_lifecycle.py`，默认禁用，显式 opt-in 后才创建独立测试命名空间；清理前校验本次 run-id 和 managed-by 标签。
