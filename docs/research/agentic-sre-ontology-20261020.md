# AgenticSRE Operational Ontology 深度调研与集成决策

研究日期：2026-10-20（Asia/Shanghai）。适用对象：AgenticSRE 维护者、运维使用者与研究评审。状态：公开来源与现有代码的架构调研；文中的验收项是实施标准，不代表已经通过。本文件不修改研究报告主文，不涉及 Palantir 商业平台采购或已经完成的平台接入。

首批实际集成及其最终验证范围另见 [验收记录](../../outputs/ontology-20261020/verification.md)，不将本文后续路线自动计作已交付功能。

## 决策摘要

**建议以 Palantir Operational Ontology 思想为主线，把对象、观测、判断、动作及其权限和执行反馈统一为可核验的运维工作流；以 UModel 借鉴运维域的实体与观测查询组织方式。** 这一判断的置信度为中高：官方文档能证明这些产品的设计，但无法证明照搬后一定提高本项目准确率。收益须由同模型、同故障集的对照实验决定。

首阶段应实现跨多智能体与 Agent+Skill 的 per-run `InvestigationModel`，贯通真实取证、持久化、Judge 引用、API、页面与动作门禁。先复用已有存储和采集器，以薄语义层建立可信契约；若只有生成报告时才补一个图 JSON，则不算完成深度集成。跨事件长期对象库、历史拓扑和更多数据源属于后续明确里程碑。

关键边界：**schema 表示允许什么结构，事实记录表示实际观察到什么；关系表示可追踪联系，根因结论另需因果证据；数据编辑事务不能保证 Kubernetes 等外部动作原子回滚。** Palantir 明确将外部调用排除在 Ontology 编辑的原子性之外，并默认避免自动重试含外部调用的动作。[S01](https://www.palantir.com/docs/foundry/ontology/overview)、[S04](https://www.palantir.com/docs/foundry/action-types/consistency-guarantees)

## 一、任务契约与研究问题

目标：确定哪些公开设计能改善现有 AgenticSRE 的正确性、可核验性、操作闭环和扩展性，并转化成可反证的实施与验收标准。检索截止 2026-10-20；不把厂商性能宣传、预印本实验或两类本地故障验收外推成生产准确率。研究不安装第三方平台、不迁移生产存储、不开放自愈，不执行提交或发布。

1. 怎样同时表达稳定业务对象、短生命周期 Kubernetes 实例与同名重建？
2. 怎样区分 schema、采集事实、推导关系与根因假设，并保存来源和时间？
3. 统一查询应在哪一层屏蔽存储差异，同时保留底层可核查入口？
4. 两种诊断模式怎样共享证据和过程契约，防止最终摘要成为唯一事实源？
5. 动作怎样绑定对象、版本、权限、前置条件、审批和执行后验证？
6. 怎样用独立真值与重复实验判断收益，并识别失败和未知状态？
7. 什么证据足以支持从薄语义层升级为持续图平台？

成功标准：上述问题都有来源锚点、代码映射和可测验收；必须显式登记资料冲突与不可获取项。不可获得商业系统内部源码、生产数据或独立复现实验时，结论降级为“设计参考”。

## 二、来源登记与采集记录

所有以下链接均在 2026-10-20 通过联网工具核实；未明示更新日的页面标为滚动文档，不将搜索抓取日当发布日期。公开网页内容可能继续变动。本文保存的是来源地址、版本、可复核定位和研究解释，未完整归档所有网页原件；长期审计前应补充允许保存的快照与哈希。本次无下载 PDF，因而没有把 HTML 入口当作已校验 PDF。

已补充归档六份关键原件：Palantir Ontology、Action consistency、Submission criteria，UModel v1 HTML 预印本、STAROps 确定性脚本规范、Kubernetes API Concepts。六次抓取均 HTTP 200，已核对页面标题；获取时间、最终 URL、内容类型、长度、截断标记与 SHA-256 见 [快照清单](../../outputs/ontology-20261020/research-snapshots/manifest.json)。这些快照不代表其余来源均已归档。

| ID | 来源及版本/时间 | 类型与可用证据 | 可信度、偏见及适用边界 |
|---|---|---|---|
| S01 | [Palantir Ontology overview](https://www.palantir.com/docs/foundry/ontology/overview)，滚动文档 | 官方产品技术文档；对象、关系、行为、安全的组合 | 设计描述高；供应商自述，不能证明本项目收益 |
| S02 | [Submission criteria](https://www.palantir.com/docs/foundry/action-types/submission-criteria)，滚动文档 | 用户、参数、执行上下文组成提交条件；不等同编辑 Action 定义的权限 | 规则语义高；不等于本项目已有同等实现 |
| S03 | [Action test run](https://www.palantir.com/docs/foundry/action-types/test-run)，滚动文档 | 在实际权限与提交条件下预览拟议修改和执行细节 | 功能描述高；不能推断外部工具支持无副作用模拟 |
| S04 | [Consistency and isolation](https://www.palantir.com/docs/foundry/action-types/consistency-guarantees)，滚动文档 | 对象编辑事务、隔离级别与外部副作用重试边界 | 约束说明高；商业平台机制不可直接移植成外部动作保证 |
| S05 | [AIP trace view](https://www.palantir.com/docs/foundry/aip-observability/trace-view)，滚动文档 | 层级 span、调用输入输出、耗时、模型 token、错误与 trace ID | 功能描述高；不是要求保存模型私有推理全文 |
| S06 | [AIP Evals](https://www.palantir.com/docs/foundry/aip-evals/overview)，滚动文档 | 测试输入、期望输出、评估函数、版本比较和多次运行方差 | 评估设计高；提供评估功能本身不证明模型正确 |
| S07 | [AIP log permissions](https://www.palantir.com/docs/foundry/aip-observability/log-permissioning)，滚动文档 | trace/log 的独立访问控制；源数据标记不会自动全部传播到日志 | 边界说明高；提醒我们不能默认原始观测任意回显 |
| S08 | [UModel 原始论文 HTML v1](https://arxiv.org/html/2606.04799v1)，2026-06-03 提交 | 作者原始 arXiv 预印本；对象层、查询层、应用层，§IV-B/IV-C 与 Table I | 设计中高；非已核实同行评议论文，作者含平台建设方；实验不作为本项目收益保证 |
| S09 | [STAROps RCA 基准设计](https://sls.aliyun.com/doc/starops/benchmark/rca/rca_benchmark_dataset.html)，滚动文档 | 官方数据集和评分协议、开源入口声明 | 协议描述中高；仍需独立下载、运行与核对样本污染 |
| S10 | [STAROps RCA 结果](https://sls.aliyun.com/doc/starops/benchmark/rca/rca_benchmark_results.html)，滚动文档 | 将实体一致性、调查路径、结论复核分开讨论 | 工程结构中高；厂商自评，不能作为跨系统排名或独立准确性证据 |
| S11 | [STAROps DevOps 接入](https://sls.aliyun.com/doc/starops/practices/devops-code-to-runtime/article.html)，滚动文档 | 服务/实例到发布、制品、代码和负责人的可追溯映射 | 参考架构高；客户采集器与映射需实际建设 |
| S12 | [确定性 Skill 脚本](https://sls.aliyun.com/doc/starops/practices/skill-script-deterministic/article.html)，滚动文档 | 单位、计算、阈值等应固化为脚本 | 实践建议中高；稳定取数不等于因果判断正确 |
| S13 | [STAROps MCP 治理](https://sls.aliyun.com/doc/starops/practices/mcp-integration/article.html)，滚动文档 | 外部能力的结构化输入输出、风险分层、权限及动作后验证 | 设计高；需检查每个具体 server 的真实实现 |
| S14 | [阿里可观测 MCP 官方源码](https://github.com/aliyun/alibabacloud-observability-mcp-server)，master，滚动分支 | Go 版 PaaS/IaaS 工具结构、时间参数和服务接口；Python 版迁至 v1 | 可查源码高；接入依赖阿里云 workspace/数据与授权；部分 AI 工具计费，HTTP 访问控制需自行配置 |
| S15 | [HolmesGPT Custom Toolsets](https://holmesgpt.dev/dev/data-sources/custom-toolsets/)，dev 文档 | prerequisites、输入参数与解释说明 | 设计中高；dev 可能先于发行版本 |
| S16 | [HolmesGPT Cross-Cluster Tools](https://holmesgpt.dev/dev/data-sources/cross-cluster-tools/)，dev 文档 | 按实例和 toolset 显式控制远程暴露 | 边界描述中高；环境内网 URL 启发式不应当作可靠租户隔离 |
| S17 | [HolmesGPT OpenTelemetry](https://holmesgpt.dev/dev/reference/opentelemetry/)，dev 文档 | 调查、LLM、MCP 的标准 trace/metrics | 设计中高；输出脱敏和成本须自行验收 |
| S18 | [HolmesGPT eval 运行](https://holmesgpt.dev/dev/development/evaluations/running-evals/)与[添加用例](https://holmesgpt.dev/dev/development/evaluations/adding-evals/)，dev | 实验运行与场景维护入口 | 机制中高；其现有成绩不等于本项目成绩 |
| S19 | [AIOpsLab 原始预印本](https://arxiv.org/abs/2501.06706)，2025-01-12 v1；[官方源码](https://github.com/microsoft/AIOpsLab)，main | 环境、工作负载、故障、Agent 与 evaluator 分离 | 机制高；可复现程度取决于目标环境和版本锁定 |
| S20 | [ITBench ICML 2025 正式论文](https://proceedings.mlr.press/v267/jha25a.html)，PMLR 267:27134–27197；[官方源码](https://github.com/itbench-hub/ITBench) | 正式发表研究与场景运行框架；任务完成度评价 | 机制高；论文基线是当时模型/配置，不能当今日性能上限 |
| S21 | [Kubernetes 对象身份](https://kubernetes.io/docs/concepts/overview/working-with-objects/names/)，页面标注 2026-01-27 更新 | 同种资源同 namespace 的名称约束；UID 区分历史对象实例 | 平台语义高；多集群身份仍须本项目增加 cluster_id |

采集采用“官方产品文档 → 原始论文 → 官方实现 → 现有代码”的互补路线。检索串包括 `site:sls.aliyun.com/doc/starops umodel`、`site:palantir.com/docs/foundry action submission criteria permissions ontology`、`site:holmesgpt.dev toolsets evaluation`、`ITBench: Evaluating AI Agents across Diverse Real-World IT Automation Tasks arxiv`。第三方论文摘要站、DeepWiki 和商业二手评论只作发现线索，不支撑技术结论。

重要核查结果：S09 所列 `https://github.com/alibaba/UModel` 在本次访问返回 404，不能认定可下载复用的 UModel 独立实现已核实。阿里可观测 MCP 官方仓库可访问，但这是云服务访问适配层，不能等同整个 UModel 的开源实现。`IBM/ITBench` 已重定向到 `itbench-hub/ITBench`，`robusta-dev/holmesgpt` 已重定向到 `HolmesGPT/holmesgpt`。误猜的 `jha25b` 实为其他论文，已剔除并改为标题核验的 `jha25a`；OpenReview 页面遇浏览器验证，不作为正文证据。

## 三、证据矩阵与可反证判断

| 可核查主张 | 证据与强度 | 对现有系统的推断 | 冲突、反例与下一步 |
|---|---|---|---|
| 运维对象应连接数据、逻辑、动作和权限 | S01、S02 官方设计；强（仅设计） | 两模式共享 InvestigationModel，避免各自生成不兼容的报告对象 | 若对象只存在报告而不约束取证/处置，主张的工程收益尚未实现 |
| 名称不足以标识历史实例 | S21 原生平台语义；强 | canonical instance identity 含 cluster、group/resource 或规范 kind、namespace、UID；名称仅作定位 | UID 缺失时标记 unresolved；不能把缺失 UID 当通配符合并多个实例 |
| 统一对象查询可降低跨存储映射负担 | S08 §IV 与 S14；中高 | 给 Agent 提供实体查询、邻居查询和证据查询契约；保留原始 query/result | 同样模型与故障集没有降低错误对象引用或无效调用，则重评抽象成本 |
| 结构关系支持探索但不足以认证根因 | S08 说明拓扑组织；现有取证语义；因果限制为研究判断 | 将 observed、derived、hypothesized 分开；owner/node/image 边附 evidence_id | 若仅由“相邻”生成 caused_by，视为失败；未知关联不得编成事实 |
| Action 条件与权限须在执行时检查 | S02、S04；强 | 审批绑定 run、对象身份、plan digest、model revision；执行前复查当前状态 | 审批后计划/对象变化仍能执行，或未知执行结果触发重试，视为失败 |
| 执行 trace 和原始事实须可回查 | S05、S07、S17；强（功能） | run→stage→tool attempt→evidence→claim 串联，保存 committed 状态 | trace 记录了调用但无事实来源，或日志泄漏权限外数据，均不能算闭环 |
| 判断收益需要外部真值和重复试验 | S06、S19、S20；强（方法） | 以注入记录/独立集群核查为真值；Judge 仅为辅助质量信号 | Judge 分上升但目标命中率不变/下降，不能宣布准确性提高 |

上述判断分别由至少两个互补来源支撑；Kubernetes 实例身份使用单一平台权威定义足够，且不会用另一厂商重复说明伪装独立验证。未得到独立重现实验的效益结论仍为推断。

## 四、以 Palantir 思想形成真正的运维闭环

### 4.1 Data：对象、观测与时间

建立两种身份：逻辑服务/工作负载保持长期身份；Pod 等运行实例以 UID 表达具体 incarnation。必须同时保留 cluster_id、namespace、规范 kind、名称与 UID 原文，并记录 identity_quality。不要跨 API version 创建假对象；也不要将同名 Pod 和 Deployment 合并。字段缺失需要明确范围限制。

时间模型分开记录 `event_time`（源事件发生）、`observed_at`（采集）、`ingested_at`（入库）、`valid_from/valid_to`（关系有效区间，未知可为空）。查询固定一个 UTC 窗口和 cutoff，不能每个 Agent 各自重算“近 5 分钟”。`resourceVersion` 保留为不透明版本标识，不当时间戳或跨资源全局顺序使用。这是针对本项目的设计要求，并非声称被调研系统都提供相同的双时间实现。

证据最少包含 evidence_id、subject_ref、signal_type、source、raw_ref、采集状态、工具调用 ID 和时间；数值含单位、聚合和采样窗口。采集失败、空结果、缺数据、不适用、成功但未发现异常分别表达。原始观测先持久化，摘要只作派生投影；页面截断必须可取完整受控内容。

### 4.2 Logic：从关系探索到证据判断

UModel 区分实体集合、关系、数据定义和数据绑定；它的高级实体工具用于先找对象再深入观测。本文借鉴这一分层，不要求实现新的 U-SPL 解析器。先提供 `resolve_entity`、`get_neighbors`、`query_observations` 等受约束接口，再由已有 Kubernetes/指标/日志适配器执行。[S08](https://arxiv.org/html/2606.04799v1)、[S14](https://github.com/aliyun/alibabacloud-observability-mcp-server)

每条关系必须标记来源类别、证据引用和有效范围。`scheduled_on` 可由 Pod.spec.nodeName 支持；`owned_by` 可由 ownerReferences 支持；`uses_image` 区分声明 tag 与实际 imageID digest。没有发布采集器时，不生成 `introduced_by_commit` 事实。配置变更接近异常时间只能支持候选原因，不能直接声明因果。

计算与推断分开：单位转换、调度约束匹配、指标聚合、Diff、阈值和持续时间交由确定性函数；模型负责选择下一步、解释候选及反证、整理结论。对应 STAROps 对确定性脚本的实践，但必须由本项目用例检查实现是否成立。[S12](https://sls.aliyun.com/doc/starops/practices/skill-script-deterministic/article.html)

### 4.3 Action：从建议到受控执行

ActionDefinition 描述适用对象、参数 schema、权限、风险、前后条件、回退能力；ActionPlan 绑定具体 run、目标 UID/版本、证据集 revision、计划 hash；ActionExecution 保存具体 attempt 和确认的副作用。三者分离。

执行前依次检查用户身份和实际平台权限、现有质量门禁、对象/计划是否变化、前置条件、授权范围和所需确认。预览接口必须返回 blocked/unknown 条件及原因，不能在客户端隐藏按钮就算权限控制。Palantir 的提交条件和 test run 是设计依据；本项目不继承其权限实现。[S02](https://www.palantir.com/docs/foundry/action-types/submission-criteria)、[S03](https://www.palantir.com/docs/foundry/action-types/test-run)

失败后区分 confirmed_failed 与 outcome_unknown。网络超时不证明没有发生副作用；未知状态先核对真实资源，不能无条件重试或回滚。执行后验证必须针对目标对象和业务后置条件，命令 exit 0 不等于恢复。回退只处理已确认发生、具备记录的可逆动作，且再次核查对象版本。该限制与 Palantir 对外部调用的原子性和重试边界一致。[S04](https://www.palantir.com/docs/foundry/action-types/consistency-guarantees)

### 4.4 Security 与反馈：一条可核验链路

前端应能从结论逐层打开支持/反驳证据、源对象、采样时间及工具执行；Object View 展示事实状态、关系及可用动作，状态未知也可见。过程页展示阶段输入范围、承诺的工具调用结果、失败和下一步决策摘要，不要求模型隐藏推理全文。

日志和证据详情应沿用运行身份和数据源权限，做参数与敏感信息脱敏。Palantir 官方明确指出日志访问与源数据权限传播存在独立边界，因此不能因为可看诊断摘要就默认可看全部原始日志。[S05](https://www.palantir.com/docs/foundry/aip-observability/trace-view)、[S07](https://www.palantir.com/docs/foundry/aip-observability/log-permissioning)

反馈应分人类修订、工具结果、处置后验证和实验真值；LLM Judge 分数属于质量判断，不是准确率或可执行授权本身。训练/记忆晋升须经过独立验收，不能把模型自证循环写回为事实。

## 五、架构竞争假设与反证条件

| 假设 | 能解释什么/适用条件 | 主要代价 | 区分性证据和翻转条件 |
|---|---|---|---|
| H1：薄语义层 + 每 run 快照 + 已有存储，当前最合适 | 已有采集器多，近期主要缺口是身份、证据、两模式契约与页面一致性 | 长期历史拓扑、多租户高并发查询仍有限 | 固定数据集对照若对象错误不降、接口新增映射反而更多，推翻收益；若持续多集群历史查询成为常态且性能/一致性不达标，转 H2 |
| H2：立即引入持续图平台和集中实体库 | 多集群/多租户、历史变更追溯、高频多跳查询是当前主要业务 | 新采集/回填、schema治理、数据库运维和迁移风险 | 需要真实规模、查询分布、刷新SLO与数据所有者证明；仅因“先进”或想展示图不能支持 H2 |
| H3：只增强提示词、Skill 与工具描述 | 主要错误源只是工具选择和表述，证据对象本已一致 | 快，但无法系统解决源身份歧义、审批过期、缺口持久化 | 若在相同输入上修提示词即可达到相同对象正确性与审计闭环，应缩小 H1 实现；跨 UID/跨 cluster 反例仍错则否定 H3 充分性 |

当前优先 H1，置信度中高，属于可逆实施选择。最脆弱假设是“现有观测中对象身份覆盖率足以支持统一匹配”；若大量数据只有服务别名而无可靠映射，必须先做 identity resolution 和缺口治理，不能假装统一主键已建立。UModel v1 自述 graph-match 有循环处理限制，也说明统一语义不等于任意图推理能力完备。[S08](https://arxiv.org/html/2606.04799v1)

反事实攻击：①同名 Pod 重建后旧 Warning 被当现状；②图上相邻服务异常被当目标根因；③取证后配置变化，旧审批仍执行；④工具超时但已生效，重试造成重复变更。任一发生都足以否决对应阶段的生产就绪声明。

## 六、现有代码映射与集成边界

以下为研究时读取的现有代码，不是“系统尚无任何基础”的假设；实施并行进行时应以最终 diff 与远端结果为准。

| 现有位置 | 已具备基础 | 应统一或补齐 |
|---|---|---|
| `agents/evidence.py::EvidenceItem/format_evidence` | 结构化观测、raw_ref、先事实后摘要 | 稳定 evidence_id、公共 identity、精确时间/状态与完整事实持久化 |
| `agents/pod_state_evidence.py`、`agents/event_agent.py` | UID/resourceVersion/observed_at，当前事件身份核对 | 采集输出直接进入公共模型，缺失/冲突身份保持可见 |
| `orchestrator/collaboration_optimizer.py::root_scoped_evidence` | 根对象限定，避免同 namespace 污染 | 共享身份匹配规则，拒绝跨 cluster/kind/UID，未知值不作通配 |
| `orchestrator/rca_engine.py` | 完整 session.evidence、structured_evidence、completion 与 automation gate | 两模式统一 InvestigationModel；流式阶段、最终结果、历史加载使用同模型 |
| `orchestrator/skill_agent.py` | 工具 trace、结构化证据、已承诺的调用结果 | 关联 run/tool call/evidence identity；晚到回调不改写终态 |
| `memory/rca_judge.py` | supported_evidence_ids、unsupported_claims、contradictions | 引用能映射到公共 evidence_id；悬空引用与未审查状态不可显示通过 |
| `orchestrator/diagnosis_closure.py` | 按缺口和失败模式追加取证/修订 | 用 typed gap、对象及时间约束安排下一次取证 |
| `agents/remediation_agent.py`、`tools/action_stack.py` | 审批、执行 checkpoint、记录回退、未知结果阻断机制 | 计划绑定 run/对象/revision；目标后置条件与实时前置检查 |
| `memory/trace_store.py`、`tools/observability.py` | pipeline/agent trace、状态和统计 | 统一父子 span/call ID；总调用数不能混同 Agent 次数 |
| `web_app/app.py`、`web_app/static/js/app.js` | 历史run、过程、报告与质量展示 | 对象/证据/关系/工具/缺口视图及只读可核验 API；打开历史不得重新诊断 |

阿里 MCP 的真实接入是可选外部适配：必须有目标 workspace、云授权和可用数据，验证后才能称“已接入”。Palantir 在本阶段提供架构参考，不声明接入 Foundry/AIP、取得其权限保证或兼容其 SDK。第三方代码复用先固定 commit、核验许可证和接口，不能从可访问 README 推导可直接商用。

## 七、分阶段验收与实验设计

| 阶段 | 交付与范围 | 必须通过的验收 |
|---|---|---|
| P0 统一事实契约 | 两模式每 run 对象、证据、事实关系、工具状态、Judge 引用/缺口；API/UI共享 | 调度/镜像两类 × 两模式真实前端；同名异UID、同名异kind/cluster/namespace反例不合并；无事实不产事实边；历史打开零新诊断；模型/工具失败如实展示 |
| P1 统一查询与可靠过程 | 源适配器、固定时间窗、typed gaps、trace、版本化结果 | 采集失败/空值/缺失分开；重连乱序不回退；超时晚回调不假成功；cutoff后事实不可悄悄进入旧结论；完整原始证据可受控取回 |
| P2 Action 治理 | 定义/计划/执行分离；预览、审批、前置/后置条件与回退 | 执行前故意替换UID、修改计划/revision应阻断；并发点击不重复执行；未知副作用先核对；无写权限不能借Agent提升权限；失败验证不能显示恢复 |
| P3 变更与对象工作台 | 运行对象到镜像/发布/代码/负责人的真实映射 | 有数据才显示关系；缺失有缺口；区分声明镜像和实际digest；仅时间关联不认定代码根因；业务对象与短生命周期实例分别追踪 |
| P4 开放评估与主动巡检 | 固定AIOpsLab/ITBench版本，扩展故障与无故障对照，SLO巡检 | 检测/定位/原因/修复分别评分；独立注入真值；每配置重复运行；记录超时/费用/错误执行与未解决比例；不删失败样本 |
| P5 规模化平台 | 按真实需求引入持久实体库、历史图、多集群多租户 | 明确容量、查询P95、刷新延迟、权限隔离和恢复目标；只有达到既定SLO才升级生产范围 |

所有编译、依赖安装、测试、容器构建及浏览器 QA 在远端 snail/集群完成；本机只编辑和读取。新故障注入应在隔离测试环境、限定对象执行，不在共享生产环境泛化注入。现有验收材料只能证明其明确覆盖的故障与代码版本。

评估固定同一模型与预算，比较 A=现状、B=语义契约、C=语义契约+统一查询；保持工具能力与输入告警一致，排除“换了模型/多给数据”的混杂。核心指标为目标实例命中率、故障类型/根因命中、错误对象引用率、可核验证据覆盖、无证据断言率、任务恢复率、错误变更率、耗时和调用/token成本。按故障族报告重复运行差异；样本少时不把小数点分数包装成稳定结论。[S06](https://www.palantir.com/docs/foundry/aip-evals/overview)、[S19](https://github.com/microsoft/AIOpsLab)、[S20](https://proceedings.mlr.press/v267/jha25a.html)

## 八、不确定性、资料缺口与质量门

高置信度事实：Kubernetes 名称可重用而 UID 区分历史实例；Palantir 官方描述对象/行为/权限协同，且外部动作不受对象编辑原子性保证；公开评估框架将真实环境任务与评价器分开。

中高置信度判断：本项目近期适合薄语义层先统一现有机制，因为核心问题已经出现于身份、证据范围和过程一致性。该判断会被数据覆盖率、真实规模或对照实验翻转。

尚未证明：采用 Ontology 后的准确率提升幅度；所有故障族可用；跨集群历史图的吞吐；商业平台内部机制；外部账号下可用工具和权限；独立 UModel 仓库的当前发布状态。本文不引用厂商性能数字来填补这些缺口。

质量门记录：Q1（任务/KIQ≤7）通过；Q2（竞争假设/采集路线）通过；Q3（核心主张来源覆盖）通过，原件完整归档仍待补；Q4（反事实/翻转条件/不确定性）通过；Q5（设计交付）通过，**实施/生产就绪须另以远端验收判定**。本轮输出是可实施决策包，不等同功能已全部落地。

## 九、P3 首个实现切片：有来源的工作负载与镜像追溯

继续实施时选择了有界只读采集：已记录的 UID 对象 → controlling ownerReference → 每级真实资源 UID 核对。Kubernetes 明确要求命名空间内的 owner 与 dependent 处于同一命名空间，因此采集器固定继承源 namespace，拒绝用标签、名称前缀或模型叙述补出跨域归属。这个设计借鉴 Ontology 的对象与有来源关系，仍是项目内部实现。[Kubernetes Owners and Dependents](https://kubernetes.io/docs/concepts/overview/working-with-objects/owners-dependents/)

Deployment revision 描述 Pod 模板的修订，扩缩容本身不会创建这种 revision；因此页面仅展示观测到的控制器修订号，不把它视作 Git SHA，也不把创建时间当成发布时间。镜像侧分别保存 spec 引用与 Pod status imageID。tag 可以重新指向不同内容，而运行时 imageID 也有不同格式；在没有注册表解析与供应链证据时，不进一步声称已经建立镜像到代码提交的映射。[Kubernetes Deployment revisions](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#rolling-back-a-deployment)、[Kubernetes Images](https://kubernetes.io/docs/concepts/containers/images/)

诊断后采集的数据与原始推理证据分开持久化，明确原 Judge 未审核这些补充结果。原调查 revision、结论与动作门禁不因补充查询而改变。实际覆盖、失败记录、代码/镜像身份和复现步骤见 [本阶段验收](../../outputs/lineage-20261020/verification.md)。该切片不包含 Git/CD 适配器、负责人映射、完整发布事件历史或准确率提升实验。

## 十、P3 第二个实现切片：来源声明与观测变化

只读盘点显示：当前默认命名空间十二个 Deployment 未提供受支持的来源声明，集群未发现 Argo CD/Flux CRD。因此本阶段没有安装新的发布控制器或声称已连接现有 Git/CD，而是先建立可核验的来源声明契约与快照比较。OCI 对 source、revision 等键给出了含义，但将同名键复制到 Kubernetes 注解，并不能证明代码提交和运行镜像之间存在经过验证的供应链关系；本项目将这种输入保留为有来源路径的未核验声明。[OCI Image Annotations](https://github.com/opencontainers/image-spec/blob/main/annotations.md)

Argo CD 与 Flux 的管理标记可帮助定位后续适配器的查询目标，但标签本身不能代替控制器状态和对象身份核验。因此当前实现只展示管理标记，不从它们推导发布成功、Git 提交存在或镜像构建来源。[Argo CD annotations and labels](https://argo-cd.readthedocs.io/en/stable/user-guide/annotations-and-labels/)、[Flux Kustomization](https://fluxcd.io/flux/components/kustomize/kustomizations/)

快照比较按同一 UID 和可验证的先后采集时间约束，区分观测值变化、字段未采集和资源只在一端出现。变化提供两端观测 ID，不升级为故障因果；无变化也不排除中间出现过发布。这延续了对象/事实/判断分离的实施判断。真实验收只覆盖无来源声明的当前目标；正向声明、变化与失配边界通过明确的合成契约案例验证，不包装成生产 Git/CD 集成。见 [验收记录](../../outputs/provenance-20261020/verification.md)。
