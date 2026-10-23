# 审核完成状态与供应商用量验证（2026-10-16）

## 本轮变更

在[事故轮次与时效候选](alert-review-context-20261016.md)基础上，为告警关系审核接入独立的供应商响应回执。此前客户端只保留文本，合法 JSON 即使伴随 `finish_reason=length` 也可能完成审核；现在真实 `LLMClient` 必须取得完整结束回执，才能继续关系解析与引用验证。

本轮仍是未发布候选，没有提交、推送、部署或迁移生产数据。跨任务复用保持关闭；原始告警继续完整保存。

## 完成状态与隔离

完成要求为一个 choice、字符串内容、`finish_reason=stop`、无供应商拒绝字段、无工具或函数调用。长度截断、内容过滤、拒绝、结束状态缺失/未知、空 choice 或多个 choice 均不能完成审核，即使文本包含合法 `{"relations":[]}`。未完成的候选对仍通过原有预算与恢复逻辑处理，不解释为空关系或无故障。

回执从 SDK 返回对象提取，模型生成 JSON 中同名字段不能覆盖它。只保存固定类别、布尔状态、整数用量和返回模型/系统指纹的 SHA256；不记录拒绝原文、响应 ID、请求头或凭据。解析失败的正文也不进入审核 checkpoint。客户端错误日志改为异常类型，避免把供应商异常正文写入日志。

`json_chat` / `async_json_chat` 默认保留原先的 JSON 返回值；告警审核显式请求 `JSONCompletion`。每次调用拥有自己的回执容器，经现有 `chat` 路径执行，因此仍经过 `DeadlineLLMClient` 的截止时间控制。并发调用和取消后迟到的线程响应不能改写另一调用的回执。

已有模型替身/其他适配器仍可按原协议返回 JSON，但不会因此取得供应商完成验证；观测中继续显示元数据缺失。返回模型名称及系统指纹只是响应审计事实，不能证明提供方使用不可变模型版本，也不替代现有 `model_revision_missing` 条件。

## 用量与失败恢复

硬预算继续在调用前预留输入字节、调用次数和输出 token 上限。供应商用量是另一个维度，不退还预留额度，不把字节数换算成 token，也不宣称等于账单金额。

每个已预留尝试取得单调序号；其供应商 `prompt_tokens` / `completion_tokens` 在 JSON 或完成状态校验失败后仍保存。记录进入任务预算 JSON，独立于可降级的重复请求观测日志，按尝试序号只写一次。重试会累加新尝试，覆盖同一批 checkpoint、进程重建或仅重试发布不会抹去或重复统计旧用量。

| API 字段 | 含义 |
|---|---|
| `budget.actual_input_tokens` / `actual_output_tokens` | 所有已预留尝试都有有效输入及输出统计时，返回供应商报告总量；否则均为 `null` |
| `budget.provider_usage.status` | `no_calls` / `complete` / `partial` / `unavailable`，明确统计覆盖程度 |
| `known_input_tokens` / `known_output_tokens` | 收到的有效数值之和；完全缺失时为 `null`，供应商显式报告 0 时才可为 0 |
| `reserved_attempts` / `recorded_attempts` | 预留尝试数 / 已落盘回执数；预留不保证网络请求已经发出 |
| `input_reported_attempts` / `output_reported_attempts` | 各方向有效统计覆盖的尝试数 |
| `review_observation.provider_verified_reviews` | 通过供应商完成检查、正常解析与引用校验，且未因上下文变化失效的审核数 |

数值必须为非负、可精确表示的 JSON 整数；布尔值、字符串、负数、浮点数及超界整数不参与统计。供应商同时提供有效的输入、输出及总量，但三者不一致时，该次用量不可用。缺失用量本身不撤销已完成的审核，仍保留 `provider_usage_unavailable` 复用阻断项。

超时、调用异常、进程崩溃或预留后尚未发出请求的尝试可能没有可用回执，不能补成零。已经取消的调用若稍后返回，系统丢弃其结果；这可能低于供应商最终账单，因此部分统计只表示已知量。旧任务缺少回执也不回填估算值。

## 访问范围核查

当前 `auth_middleware` 根据全局登录 session 或 bearer 认证放行；session 保存用户名和到期时间，没有租户授权版本。作业 payload 固定 `cluster_id` / `namespace`，`_cluster_scoped_config` 绑定采集配置，但没有权威的访问策略 revision 或集群实例 UID 纪元。[认证及任务入口](../../web_app/app.py)

因此不能用客户端提交的 scope 字段、配置文件路径、模型响应字段或凭据哈希代替授权版本。本轮保留 `access_scope_unversioned`，没有临时增加环境变量来宣称该条件已满足。开放跨任务复用前，还需服务端提供随授权和集群绑定变化而递增的非敏感范围版本，并在请求准备、读取和完成时核对。

## 远程验证与证据

所有构建、pytest 及 SDK/worker/API 探针在 snail 的隔离禁网容器中运行，使用已有依赖；本机只编辑、打包和核对下载文件。模型响应由替身或 `httpx.MockTransport` 提供，无真实供应商调用及费用。

候选镜像为 `agenticsre:alert-review-provider-20261016-r1`，继承上一轮 context r2，镜像 ID：`sha256:911399685a8c341c54c9ddfa46eb350c593ea9913a0e4afce313472728c16ad8`。

| 检查 | 结果 |
|---|---|
| 针对性回归 | **197 passed，5 warnings，20.48 秒** |
| 全量回归 | **1,266 passed，5 warnings，233.16 秒** |
| 累计源码校验 | 14 个变更/新增实现及测试文件的本地与候选镜像 SHA256 一致 |
| SDK / worker / API | 4 个场景全部通过，job 与结果查询均 HTTP 200 |

新增 30 个参数化用例覆盖截断、拒绝、过滤、工具调用、缺失/多 choice、缺失/畸形/不一致用量、解析失败、模型正文伪造元数据、重试与重建、发布重试、观测写入故障、并发、超时迟到、旧 JSON 接口、RCA 截止时间及日志脱敏。[测试实现](../../tests/test_alert_review_provider.py)

SDK 探针使用实际 OpenAI 客户端的请求与响应解析，HTTP 仅由内存 `MockTransport` 应答；实际 Web worker、持久任务、finalizer 和 ASGI 查询均执行。每场景有 2 条合成告警，全部保留。正常非空答复接受 1 条 `unverified` 关系；截断时即使有完整非空 JSON 也接受 0 条，重试后接受 1 条。其两次用量累计为输入 30、输出 18 token。缺少用量的完整空答复可完成审核但统计为 `unavailable`；缺少结束状态的空答复维持 `partial`。

每个场景的生命周期 episode 均为 1，观测次数只从预置的 1 增至 2。截断后的恢复沿用原快照，首次发布推进生命周期后，上下文版本检查会阻止原快照获得复用资格；正常重试审核仍可进行。返回模型指纹及供应商完成验证不会解除这个阻断。

5 条 warning 是既有依赖弃用提示。2026-10-16 11:53（北京时间）只读核查，生产 Web 和 Skill Runtime 仍为 `running healthy`，镜像均是 `sha256:cefa5ebf8ae6df50143a5fd10bf5aa1329d74ad22b2e5291e9a644b1165dd3a1`，检索模型仍为 `all-MiniLM-L6-v2`。

远程证据目录 `/home/ubuntu/AgenticSRE-releases/alert-review-provider-20261016`；本地目录 [outputs/alert-review-provider-20261016](../../outputs/alert-review-provider-20261016/) 保存源码包、构建日志、JUnit、SDK/worker 探针及源码/生产只读核查。运行产物不承诺由 Git 托管。

[证据归档](../../outputs/alert-review-provider-20261016/provider-evidence.tar.gz) 已下载到本地，含 18 个文件、79,690 字节；远程与本地 SHA256 一致：`8d90956368dd340fd1a700198213f0816aaa0c7a368d5fc5c10d930a98b64d36`。包内逐文件清单、14 个本地源码摘要及 JUnit 计数均已核验。包内报告不含本段对归档自身的说明。

本轮没有建立模型质量、延迟改善或节省费用的实测结论；前端没有新增用量展示，新增状态通过任务、结果 API 和 checkpoint 提供。真实供应商兼容性、独立事故划分下的诊断质量和收益、观测记录留存政策仍需后续验收。
