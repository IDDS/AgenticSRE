# 同类产品调研：AgenticSRE 的下一批能力

研究日期：2026-10-22，Asia/Shanghai。依据公开一手文档和当前仓库 `530e0cc`；运行时源码对应 `afa9dd9`。本文是研究与实施建议，没有新增运行能力、修改生产或进行本轮应用测试。现有部署和 1867 项冻结回归见[发布核验](../validation/agentic-sre-release-20261022.md)，不能替代新增能力的验收。

建议先补可信反馈与记忆的准入、归因及评估，再扩展真实变更归因、鉴别性取证和业务影响。当前系统已经有 Ontology、谱系、假设引用、有限重规划、冻结回放、规则学习、人工审核和 Skill 草案门禁；继续建设应连接这些机制，避免按产品名称再造一套。反馈链路存在可定位的类型、状态、重复和版本归因缺口，详见[反馈与记忆专项](feedback-memory-loop-20261022.md)。

## 研究结论与证据强度

**高置信事实：同类产品已经把调查、经验、发布与防复发结合起来，但“使用反馈”没有统一含义。** Datadog 描述反馈形成并检索 memories，Dynatrace Assist 明确其反馈不自动训练模型，AWS 描述跨事故改进事项和面向 Coding Agent 的规格。这些是官方流程声明，可以支持功能设计，不能证明厂商效果或本项目收益。[Datadog 知识源](https://docs.datadoghq.com/bits_ai/bits_investigation/knowledge_sources.md)、[Dynatrace Assist](https://docs.dynatrace.com/docs/dynatrace-intelligence/agentic-and-generative-ai/chat-with-dynatrace-assist)、[AWS 事故预防](https://docs.aws.amazon.com/devopsagent/latest/userguide/production-operations-proactive-incident-prevention.html)

**中高置信判断：对本项目，补足事实关联与评估闭环很可能比增加 Agent 数量更有价值。** 依据是当前已有丰富执行机制，缺口集中在外部变更事实、业务口径及学习结果如何晋升。反例是未来测得现有多源覆盖已足够、错误主要来自某专项工具或模型；届时应调整到该工具或模型，而非坚持扩建平台。本判断尚无本项目固定预算的新增对照试验。

**高置信边界：目前不能宣布反馈闭环已经准确、记忆持续有效或跨事故策略已学成。** 代码可以证明入口、存储和回流存在，独立效果必须由冻结基线、留出事故和分故障域结果支持。厂商自述、规则数、点赞数、Judge 分数和回归测试通过数均不能替代这种证据。[Datadog 评测工程经验](https://www.datadoghq.com/blog/engineering/bits-ai-eval-platform/)、[本项目成对评测实现](../../eval/evolution_accuracy_benchmark.py)

## 本轮核实的七类产品

以下只列与增量直接相关的官方能力。滚动文档的访问日不等于发布日期；没有登录商业账户做产品实操，不评价厂商内部算法或采购性价比。

| 产品与资料 | 本轮核实的公开能力 | 对本项目的增量启发及边界 |
| --- | --- | --- |
| [阿里 STAROps/UModel：代码与运行态](https://sls.aliyun.com/doc/starops/practices/devops-code-to-runtime/article.html) | 用应用、运行资源、流水线和代码关系接续追查部署及提交 | 在已有 Kubernetes 归属上接真实 Git/CD 数据。对应关系、时间邻近和故障因果分开，不按名称相似推断 |
| [STAROps：业务可靠性](https://sls.aliyun.com/doc/starops/practices/business-reliability-flow/article.html)、[容量风险](https://sls.aliyun.com/doc/starops/practices/capacity-risk-prediction/article.html) | 业务动作/SLI 基线持续检查；数值算法做容量预测，Agent 关联风险 | 补服务级业务口径和预防性风险事项；先有合格数据与阈值，再预测，不由 LLM 编数值 |
| [Datadog Bits Investigation：改进准确性](https://docs.datadoghq.com/bits_ai/bits_investigation/improve_accuracy.md)、[评测平台](https://www.datadoghq.com/blog/engineering/bits-ai-eval-platform/) | 反馈需实际根因和遥测上下文；隔离历史环境并保留背景噪声做回放 | 把反馈作为待验证输入，防止只记住已反馈案例；第三方取证集成的 Preview 状态不能泛化为全平台可用 |
| [Dynatrace Intelligence：RCA 概念](https://docs.dynatrace.com/docs/dynatrace-intelligence/root-cause-analysis/concepts)、[Assist](https://docs.dynatrace.com/docs/dynatrace-intelligence/agentic-and-generative-ai/chat-with-dynatrace-assist) | 将问题、依赖和影响关联；Agent 工具权限与反馈路径有明确边界 | 技术传播与业务影响分别建模；知识更新、反馈审核和权重训练分别呈现。Assist 页标注更新于 2026-10-15 |
| [AWS DevOps Agent：生产运营](https://docs.aws.amazon.com/devopsagent/latest/userguide/working-with-devops-agent-production-operations-index.html)、[预防改进](https://docs.aws.amazon.com/devopsagent/latest/userguide/production-operations-proactive-incident-prevention.html) | 预防事项有保留、丢弃、已实施状态，关联事故；可输出给 Coding Agent 的变更规格；事故经验接发布评审 | 将报告里的 prevention 文本接到责任人、实施证据和后续复发复核。标记实施完成本身不证明措施有效 |
| [PagerDuty：事件编排](https://docs.pagerduty.com/ai-automation/aiops/event-orchestration)、[AI 编排建议](https://docs.pagerduty.com/ai-automation/aiops/event-orchestration/ai-orchestrations) | 规则控制路由、富化、通知暂停和抑制；建议可说明预期影响并由用户应用 | 在压缩之外补通知决策与规则影子评估。保留原事件；该产品文档区分可审计抑制与不可审计丢弃，不能照搬丢弃为保真降噪 |
| [Palantir：Ontology Scenarios](https://www.palantir.com/docs/foundry/ontology/overview-ontology-scenario)、[AIP 实验](https://www.palantir.com/docs/foundry/aip-evals/experiments) | 场景分支比较假设操作；参数组合实验；场景与主数据动作条件可以分开 | 发展有边界的处置比较与策略实验。Scenarios 文档标为 Beta，而且不是历史版本快照；不能冒充 Kubernetes 仿真器 |
| [HolmesGPT：发布验证](https://holmesgpt.dev/0.44.0/operator/deployment-verification/)、[Skill 最佳实践](https://holmesgpt.dev/0.44.0/reference/skills-best-practices/) | rollout 可触发健康检查；调查经验可生成为经人审阅的 Skill | 将发布前后观测接成对象级护航，复用既有 Skill 草案与门禁；引用的是所访问版本文档，不推断全部版本均支持 |

## 能力增量与实施顺序

优先级是本项目的工程判断，不是厂商排名或已经测得的收益。P0 是当前可信闭环的前置；P1 是下一批用户价值；P2 依赖前述事实与数据质量成熟。

| 优先级 | 能力 | 已有基础与真实增量 | 首个可验收切片 |
| --- | --- | --- | --- |
| P0 | **可信反馈及记忆晋升** | 已有学习、CAS、审核、草案门禁。补严格标签类型、待补证不晋升、幂等、规则 revision 归因和隔离评测 | 同一事件重复提交不重复计票；字符串 `"false"` 不得成为正票；无原事故/旧版本反馈不能更新当前活跃规则 |
| P0 | **策略实验与效果看板** | 已有冻结回放、消融、Wilson/McNemar/bootstrap。补统一策略/记忆版本、事故集注册、时间留出和可撤销晋升 | 冻结学习前后两版本，在相同留出事故、模型与预算下比较正确率、错误确认、弃答、调用成本及故障域退化；证据不足显示未证实 |
| P1 | **真实变更归因与发布护航** | 已有相邻 ReplicaSet 关联和镜像/来源声明。补 Git/CD/registry 已核验链、发布事件及业务后置条件 | 从目标 UID 的实际镜像关联到可核验 build/commit；变更前后同口径检查，缺失返回 unknown；时间最近的无关发布不成为根因 |
| P1 | **假设鉴别检查计划** | 已有按缺口选来源和两个窄类型反证检查。补具体检查的候选区分价值、预期结果、成本与执行记录 | 两候选竞争时选择能区分它们的受控只读检查，保存支持/反驳/不确定条件；超时、无权限及空数据不成为反证 |
| P1 | **服务级 SLO 与业务影响** | 已有 vLLM 阈值型 SLO 和真实错误 span 传播。补业务服务目录、SLI 分母、预算消耗、入口影响及负责人 | 一个明确业务入口的成功率/延迟 SLI、评估周期和多窗口 burn rate；缺流量分母时不计算用户损失或错误预算 |
| P1 | **防复发事项闭环** | 已有诊断人工复核、规则/recipe/Skill 草案。补工程事项 owner、期限、实施变更、观察窗和复发复核 | 一次已接受诊断关联一项措施，复用现有审批；实施后有可核验证据，复发时重开或撤回经验，不能把没再告警当有效 |
| P1 | **告警通知编排与影子评估** | 已有完整分区、生命周期、批内排序、水位与 TTL 去重。补维护窗、作用域、到期、依赖抑制、升级路由及解释 | 先只计算“原本会通知/暂缓/抑制”的差异和漏报；保留逐条事件和规则版本，经独立验收再接通知执行器 |
| P2 | **有约束的处置比较** | 已有对象动作与预演记录，尚非真实环境仿真。补可比较的动作假设、容量/PDB/依赖约束与 unknown 项 | 对一个受支持动作比较“不操作/操作”及适用前提；无模型的影响不预测，仿真结果不替代实时授权或恢复验证 |
| P2 | **容量预测与预防性风险** | 已有当前水位与压力检查。补预测区间、需求假设、耗尽时间及风险事项 | 先选磁盘或连接数一类，冻结回测并覆盖季节性/缺测/突变；误报、漏报与提前量合格才进入持续巡检 |

业务 SLO 的计算依据应采用明确事件口径和多窗口预算消耗，不把单次指标越线改名为预算管理。[Google SRE：SLO 告警](https://sre.google/workbook/alerting-on-slos/)

如果进入多团队服务，主体和资源授权应作为横向前置：已有登录、集群选择和 UID 门禁仍需衔接用户/组可读哪些事故、可用哪些工具、可审核哪些资源。知识和预算也要沿同一作用域隔离；namespace 字符串不是租户授权。Holmes 的 namespaced Role 与预声明 scope 是可借鉴的执行边界，不是完整租户设计。[Holmes namespace scope](https://holmesgpt.dev/0.43.0/installation/namespace-scoped-access/)

## 集成方式与反例

保留当前 `operational_ontology.py`、`diagnostic_strategy.py`、`memory/`、`eval/` 与 Skill Runtime 的责任。变更、业务和反馈作为有身份、来源、时间与版本的对象接入；UI 展示已核验关系与缺口，Agent 从受控工具目录选择动作。新增对象/API 名称应在实施时确定，本文不把设计中的模型列入现有 schema。

“假设鉴别检查计划”建议保存检查 ID、要区分的假设、绑定 UID 的对象、只读工具、时间窗、预期结果分支、预计成本/超时及实际观测引用。先用可解释启发式排序，再用回放校准。现有新颖观测数不等于信息增益 bit，没有概率模型时也不生成伪精确效用数。

最重要的反例是：反馈后系统只会回答同一道题；优先关注变更后误判了同时发生的故障；新策略平均分提高但把低频高风险故障判错；通知显著减少却漏掉一个关键事故；容量预测看似合理但历史分母或阈值根本不存在。出现任一情形就应拒绝相关收益声明，必要时撤销候选版本。

替代路线 A 是只改提示词或增加 Agent，成本较低且应保留为对照；若同预算下它与新增机制等效，应减少平台复杂度。替代路线 C 是先做全面图数据库与数字孪生，只有数据规模、关系查询或仿真需求的实际瓶颈已测得时才考虑。当前没有支持立即迁移所有存储的证据。

建议三个交付批次：先做反馈/记忆可信性和成对评测；再做一个真实变更数据源与一类鉴别检查；随后接一个业务 SLO、防复发事项和通知影子模式。真实模型最近 HTTP402 尚无恢复成功证据，依赖模型的新增验收须先恢复可用性并固定模型与预算；本轮没有充值、切换模型或发起诊断。

## 来源与质量记录

公开材料均于 2026-10-22 访问；主要是官方滚动技术文档，Datadog 评测文章为 2026-04-07 官方工程经验，Google SRE 为公开方法资料，不冒充同行评审实验。官方文档对“公开提供什么接口/流程”可信，对效果和自身优越性存在厂商偏向，故不采用其性能数字作为本项目验收。

阿里同一 URL 的搜索缓存和打开正文存在表述差异；本轮以实际打开正文为准，不沿用缓存中的实体数或适配器数量。Datadog 部分 HTML 入口实际返回 Markdown，web 解析失败后已通过 curl 读取官方 Markdown 原件。Palantir/阿里/Holmes 等另存的是工具提取文本，并非完整网页或商业运行实例。

内部研究底稿位于 `outputs/product-research-20261022/`：`task-contract.md`、`agent-sources.json`、`code-gap-audit.md`、`feedback-loop-audit.md`、`memory-code-audit.md`、来源原文/摘录和质量记录。关键判断均可回到本文链接及代码；关于效果的结论均保留待独立验证。研究文档可交付不等于能力可上线。
