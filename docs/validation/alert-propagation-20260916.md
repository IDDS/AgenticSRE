# 告警传播方向与真实链路验收（2026-09-16）

## 本轮改进

源码提交：`e4b04d0`。本轮完善原设计中的 Trace 证据关联、SCAG 关系验证及可追溯性。

父子 Span 表达的是“调用方 → 被调用方”，不能直接解释为故障传播方向。现在将正常调用关联标记为 `observed_call`；对于“依赖故障影响调用方”这一类假设，只有同一调用链两端均存在明确错误标志，且下游完成时间落在调用方完成时间以内，才支持反向的“依赖 → 调用方”传播候选。

继续校验集群、资源身份和时间窗口；Pod UID 匹配但 namespace/name 冲突时拒绝映射。输入包含显式 Trace ID 时，必须与引用链路一致。无错误标志、只有单端错误、时间异常、过期、Trace ID 不符和错误方向均不提升为传播支持。模型样本补充实体标识，图中分别记录调用方向和假设传播方向。

这些检查支持的是待验证假设。`causal_confirmed` 仍为 false，统计证据不足的关系仍为 `unverified`。

## 远端回归

全部测试在 `snail` Docker 容器执行，本地没有运行服务或 pytest。

- 全量：**654 passed，104.97 秒**；有 27866 条既有警告，不能将该结果表述为零警告。
- 新增 9 个传播检查用例，覆盖正例及上述负例；调整旧测试，明确正常调用不能证明传播。
- 候选服务的异步任务、结果分页、旧结果接口、Job Skill 及浏览器检查均通过。候选页面 HTTP 200，加载 556 ms，控制台错误 0。

## 真实故障实验

复现入口：`tests/e2e/test_live_propagation.py`，配套 `propagation_probe_app.py` 与 `eval/alert_propagation_acceptance.py`。需在具备 Docker、kubectl 和现有 Jaeger 的远端显式设置 `AGENTICSRE_RUN_LIVE_PROPAGATION=1` 执行，可用 `AGENTICSRE_PROPAGATION_OUTPUT` 指定输出目录。当前脚本针对本次远端环境和候选镜像，不是通用跨环境测试工具。

在独立命名空间 `agenticsre-propagation-4d153380` 创建两个受控 Pod，以真实 HTTP 调用进行正常、依赖故障和恢复实验。两个 Pod 位于同一节点，减少跨节点时钟偏差。探针从实际请求采集时间和状态，使用 Downward API 获得真实 Pod UID，并向现有 Jaeger OTLP 接收端写入链路。JSON 编码依据 [OTLP 官方规范](https://opentelemetry.io/docs/specs/otlp/)。链路随后经 Jaeger 查询接口读取，并通过实际 `collect_traces` 采集器验证服务查询与错误 Span 映射。

| 阶段 | 实际 HTTP | 传播上下文 | 整体图关系 |
|---|---:|---|---|
| 正常 | 200 | unknown，仅调用关联 | unverified |
| 下游故障 | 503 | pass，依赖 → 调用方 | unverified |
| 恢复 | 200 | unknown，仅调用关联 | unverified |

每阶段获得 2 个真实采集 Span。原始输入完整保留，测试命名空间及其资源已清理，`cleanup_verified=true`。

验收边界：HTTP 观测被转换为日志来源输入，并非 Prometheus 已触发告警；SCAG 使用显式测试假设，模型调用数为 0。因此本实验验证真实链路采集、传播证据限定和图验证，不等于完成自主 RCA 端到端验收，也不用于证明压缩率。缺少统计序列时保持未验证状态是预期行为。

前两次实验未计为通过：首次 Jaeger 查询缺少 `/jaeger/ui` 前缀；第二次验收脚本遗漏结果顶层 cluster_id。修复的是实验适配代码，第三次完整重跑通过。远端保留失败日志供追溯。

## 证据与发布

远端目录：`/home/ubuntu/AgenticSRE-releases/propagation-20260916`。
本地证据目录：`outputs/propagation-20260916/`（未提交运行产物）。
核心文件：`full-suite-final.log`、`live-results-v3/live-report.json`、`live-results-v3/acceptance-report.json`、三阶段原始观测/链路/图、`live-collector.json`、候选及生产验证 JSON、页面截图和源码 SHA256 清单。

本轮通过既有自定义 Docker 部署流程发布，保留上一版 `agenticsre:alert-jobs-20260916` 及其 Compose 配置作为回滚目标。部署前后比较生产告警生命周期，校验运行容器源码摘要，并执行功能与浏览器验收；部署及验收脚本均设置失败自动回滚。

实际发布结果：`agenticsre` 与 `agenticsre-skill-runtime` 均运行 `agenticsre:propagation-20260916` 且健康。镜像 `sha256:84e73252ecde983851f8ff755b89a330ecdd7e3ebc858ae6b984621a9e4d34c5`，revision 标签为 `e4b04d0`（仅增加标签，应用源码与候选验收一致，4 个文件摘要匹配）。生产 25 条既有生命周期记录部署前后完全一致；异步任务、分页、旧接口与 Job Skill 验收通过。生产页面 HTTP 200、加载 525 ms、控制台错误 0。

## 尚未覆盖的原设计能力

此次没有证明整个原设计已经完整：多跳及其他类型的传播机制、具有充分时间序列的统计检验、反事实验证与自主假设搜索仍需分别验收。当前实现不应把调用关系、双端错误或高压缩率视为已确认根因。
