# Train Ticket 真实查询链故障注入与恢复验证

2026-10-23 已在远端现有 Train Ticket 业务中完成两项有效故障实验：依赖网络延迟、
依赖定向丢包。两项均有同轮健康基线、故障期间实际业务影响、撤销和连续恢复请求。
**自动诊断准确性尚未验证**：诊断模型接口返回 HTTP 402，不能把业务实验通过等同于
AgenticSRE 根因诊断通过。

## 业务与实验范围

业务入口是现有 `train-ticket/ts-ui-dashboard`，请求为原生
`POST /api/v1/travelservice/trips/left`，查询 `shanghai → suzhou`，出发日期
`2026-10-24 00:00:00`。正确请求使用 `startPlace`；实际成功响应为 `status=1`、
非空 `data` 的对象，包含车次、余票和价格字段。后续实验每笔业务请求均执行站点查询对照，
成功判据不是首页 HTTP 200。

首先完成了 30 笔正常业务请求，成功率 100%，P95 422.126 ms，关联 Trace 可查询。
Trace 实际跨越 UI、travel、basic、route、train、price、station、seat、order、config
等 10 个服务。按 span 引用核对后，`travel → route` 并非直接依赖，预检拒绝该目标；
最终选择有直接调用证据的 `travel → seat`。

注入针对原有 Java 业务进程的网络路径，没有创建代替业务的 Python/nginx 响应夹具。
两端固定为当时 `ts-travel-service` 和 `ts-seat-service` 的具体 Pod 实例，
同时绑定 Pod、Deployment、Service UID，验证
Service selector 和 Ready Endpoint 确实对应这些实体。全部执行、采样、语法检查和
控制安全测试均在远端完成。

## 实际结果

以下基线来自各实验紧邻注入前的 15 笔查询；P95 使用 nearest-rank，样本较小，
不能外推总体长期 SLO。

| 实验 | 同轮基线 | 故障期观测 | 撤销后 3 个窗口，每窗 10 笔 | 判定 |
|---|---|---|---|---|
| 250 ms 延迟，按 Pod IP 匹配 | P95 718.232 ms | 51 笔全成功，P95 336.820 ms，无预设幅度的退化 | 全成功，P95 306.332 / 300.301 / 301.031 ms | **未通过业务影响验收，保留原记录** |
| 250 ms 延迟，按实际 Service IP 匹配 | P95 279.063 ms | 51 笔全成功，P95 784.789 ms，约 2.81 倍 | 全成功，P95 290.345 / 261.767 / 261.580 ms | **注入影响、撤销、恢复通过** |
| 100% 定向丢包，按实际 Service IP 匹配 | P95 338.621 ms | 6/6 查询在约 10 秒客户端超时，业务成功率 0%，站点对照全成功 | 全成功，P95 288.258 / 295.725 / 275.129 ms | **注入影响、撤销、恢复通过** |

延迟实验预先要求故障期至少 10 笔请求，P95 不低于同轮基线的 1.5 倍。丢包实验
在执行前单独固定为至少 3 笔请求、业务成功率不高于 50%、站点对照全部成功。
两项恢复均要求三个连续窗口成功率至少 99%，P95 不超过各自基线的 1.2 倍。
没有在观察结果后调整阈值。

首轮控制器虽报告 `AllInjected=True`，业务仍未受预期影响。随后读取源业务 Pod 的
`/proc/net/tcp6`，确认实际连接目的为 seat 的 Service ClusterIP、端口 8080，
而首轮规则针对目标 Pod IP。第二轮通过 `externalTargets`
精确匹配实际目的地址后观察到显著退化。首轮失败不计入有效故障覆盖。

## Trace 对业务影响的交叉核验

分析脚本仅根据请求 trace ID、span 引用和各 span 自身的 duration 比较调用边，
不读取注入目标或预期根因，也不比较不同节点的绝对时钟。它是实验的证据核验，
不等于模型诊断。

`travel → seat` 在延迟实验中的客户端调用中位耗时从 41.270 ms 升至 304.370 ms，
恢复后为 52.570 ms；seat 服务端对应中位耗时分别为 38.558 / 52.539 / 50.198 ms。
客户端增量约 263 ms，与受控网络延迟吻合。其他已采样下游调用边增量远小于该边。
这些观测支持依赖路径延迟，而不是仅凭注入清单宣布故障生效。

每阶段只取首尾两个请求的 Trace，部分请求 Trace 不完整；例如延迟恢复末笔为
41 个 span、丢包首笔为 46 个 span，不能把“查到 Trace”当作“链路完整”。因此不声称
覆盖所有请求、证明统计显著性或已自动定位所有超时链路。

## 自动诊断状态

首轮通过隔离的 18109 预览同时发起多智能体和 Agent+Skill 调查。输入只含业务
路径、观测窗口、测量值、实际 trace ID，没有携带注入命令、seat 故障目标或预期根因。
诊断环境关闭自动处置与自动学习，独立持久化，不替换生产服务。

多智能体任务 `rca-8de08a97` 的传输层明确记录 `provider_payment_required`、HTTP 402。
其最终结论为 `unknown`、`requires_review`、`root_entity=null`，自动化门禁
`allowed=false`。任务状态 `completed` 只说明流程结束，不能解释为诊断成功。
Agent+Skill 任务 `rca-c9c520bf` 为 `failed`，也未形成可验收诊断。

收到明确的提供方拒绝后，第二、三轮没有重复发送模型请求。两项有效故障的
`diagnosis_accuracy_verified` 均为 `false`。需要恢复模型接口访问后，开展新的实时
调查或明确标注为历史窗口的证据复核；不能把旧数据重放冒充新的真实注入诊断。

## 撤销、残留和验证边界

每个实验均配置 Chaos Mesh `duration=90s`，另启动脱离本地 SSH 会话的独立远端
看门狗，在 115 秒兜底暂停自有资源。故障期采样约 60 秒后主动暂停并确认
`AllRecovered=True`，再删除自有 NetworkChaos。官方文档说明了 duration 自动恢复和
暂停撤销的语义：[Chaos Mesh 实验生命周期](https://chaos-mesh.org/docs/run-a-chaos-experiment/)。
本轮实际走主动撤销，没有另测强杀主进程后的兜底路径。

5 项远端控制安全测试通过，验证拒绝资源 UID 替换、目标修改、归属标签修改，以及
资源不存在时不做写操作；暂停补丁包含原子 UID 前置条件并保留已有 annotation。
这些控制测试不计入真实业务故障数量。

最终核对：NetworkChaos 列表为空；两端 Pod UID 保持原值、Ready、重启数均为 0，
Ready Endpoint 仍匹配。控制器保留的两个 PodNetworkChaos 记录均为 `spec={}`，
`observedGeneration` 与当前 generation 一致，没有活动注入配置；未另行采集内核
`tc` 计数。生产 `agenticsre`、`agenticsre-skill-runtime` 仍健康，镜像 SHA 与实验前一致。
没有进行 git commit/push。

本次没有覆盖登录、预订、订单创建、支付或数据库写入，也没有验证自动修复、
反馈学习闭环、告警触发/压缩或全部前端页面。因此结论严格限于真实余票查询链的
两项故障影响和恢复。

## 可复核证据

本仓库提供以下从实际归档提取的结构化摘要，保留失败记录、样本分母与未完成的
验收状态。最终状态摘要去掉机器路径、IP、对象 UID 和镜像部署细节；完整运行输出
保留在私有实验归档，没有作为源码上传。

- [最终状态](evidence/train-ticket-20261023/final-status.json)
- [延迟实验](evidence/train-ticket-20261023/delay-summary.json)与[Trace 分析](evidence/train-ticket-20261023/delay-trace-analysis.json)
- [丢包实验](evidence/train-ticket-20261023/loss-summary.json)
- [未生效首轮](evidence/train-ticket-20261023/ineffective-summary.json)与[诊断受阻记录](evidence/train-ticket-20261023/diagnosis-summary.json)

完整证据包已下载并验证 SHA-256，解包后 85 项逐文件哈希全部匹配。
SHA-256：`c94b9235e0680b81cc2fc519cea9f86dde12829fa365fb12e61e12a86041ba2b`。
原文件清单描述归档时的最终任务文件，不构成此前各进程启动时的逐次源码证明；
公开摘要不能替代原始记录进行独立复算。
