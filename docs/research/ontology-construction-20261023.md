# Ontology 构建方法与 AgenticSRE 落地方案

资料与源码核验日期：2026-10-23。本文区分公开机制、当前实现及后续建议；未将资料调研当作商业平台实测，也未将建议功能表述为已经实现。

## 核心判断

Ontology 的构建工作应分为领域模型、源数据映射、带身份与时间的事实、可验证逻辑、受控动作以及版本治理。图形展示或类型列表只是其中一部分。对 AgenticSRE，优先完善这六项契约，比立即采购图平台、增加向量库或引入全量 OWL 推理更有依据。此建议是本项目架构判断，不是比较实验结论。

Palantir 官方设计指南要求围绕现实领域实体和实际问题建模，并将实体与测量/事件分开；其操作型 Ontology 同时组织数据、逻辑和动作。W3C 标准提供互补机制：RDF 表达图数据，OWL 表达公理与语义推理，SHACL 验证约束，PROV 表达来源链。不能把这些标准说成 Palantir 的实现协议，也不能把当前 AgenticSRE 的 JSON 视图说成已具备这些标准的完整语义。[P1](https://www.palantir.com/docs/foundry/ontology/ontology-best-practices)、[W1](https://www.w3.org/TR/rdf11-concepts/)、[W2](https://www.w3.org/TR/owl2-primer/)、[W3](https://www.w3.org/TR/shacl/)、[W4](https://www.w3.org/TR/prov-o/)

## 任务契约与六个问题

目标是给出可实施、可反驳的构建方法，受众为技术评审者。问题限定为：

1. 从业务问题到对象、属性、关系、接口与动作，应产出什么 schema？
2. 实体如何消歧，如何区分身份、观测、时间与来源？
3. 源数据如何映射，新增、修改、删除及乱序怎样进入模型？
4. 语义推理、约束验证、故障因果判断分别负责什么？
5. 动作如何绑定函数、工具、实时前置条件及读写权限？
6. schema、映射、规则与动作如何演进并证明没有退化？

成功标准：每项外部机制有实际打开的一手链接和适用边界，当前能力有代码定位，建议有验收反例。未获取商业平台实例、未运行其 SDK，不验证其性能、全平台安全或效果数字。无需下载 PDF。

## 从需求到持续演进的构建流程

Stanford 的 Ontology Development 101 提倡按用途划定范围、复用已有词汇，迭代定义类、属性、约束与实例；它明确没有唯一正确的建模方法。EU ePO 对 LOT 的应用进一步把需求、概念模型、形式化、发布和维护连成闭环，并用 competency questions 验证模型是否回答了原始问题。[Ontology Development 101](https://protege.stanford.edu/publications/ontology_development/ontology101-noy-mcguinness.html)、[ePO/LOT 方法](https://docs.ted.europa.eu/epo-home/methodology2024.html)

建议对本项目采用混合构建：由故障问题自顶向下确定需要表达的语义，再由真实 API 样本自底向上验证哪些身份、关系和时间确实可取得。LLM 可辅助草拟类型、映射及假设，但进入事实库或动作链的内容必须经过确定性验证与审查。以下流程是项目建议，不是 Palantir 内部实现的复刻。

```mermaid
flowchart LR
 Q[可验收的诊断问题] --> S[类型和关系契约]
 S --> M[数据源映射与身份解析]
 M --> V[结构和证据有效性校验]
 V --> F[带来源与时间的事实视图]
 F --> H[竞争假设与鉴别取证]
 H --> A[动作前置条件和授权]
 A --> R[独立业务验证]
 R --> E[复核与版本回放]
 E --> Q
```

## 一手来源登记与证据边界

均在上述访问日实际打开并读取相关正文；搜索结果只用于定位。Palantir、OpenTelemetry、Kubernetes 为滚动文档，未将访问时间误写为发布日期。W3C 链接指向明确版本的标准；标准正文与其中非规范示例需区分。下表概述不作长段原文转载。

| ID / 原始链接 | 类型、实际读取节 | 支持的具体主张 | 不支持的外推 |
|---|---|---|---|
| P1 [Ontology best practices](https://www.palantir.com/docs/foundry/ontology/ontology-best-practices) | 官方技术设计指南；Design guidelines、Model reality | 先领域实体再映射数据；实体与观测分离，按真实问题验收 | 不能证明模型能自动发现正确领域概念或提高诊断准确率 |
| P2 [Create object type](https://www.palantir.com/docs/foundry/object-link-types/create-object-type) | 官方操作文档；Properties、Primary/title key、Property mapping | 字段映射、显示键与主键分开；主键唯一且确定，随机行号会破坏编辑与关联 | 主键要求不等于跨源实体消歧已正确完成 |
| P3 [Create link type](https://www.palantir.com/docs/foundry/object-link-types/create-link-type) | 官方操作文档；relationship/link resources | 外键、join table、对象承载关系；一对一是预期提示，文档明确不强制执行 | 不能把图上的基数标记当成数据库约束或因果证明 |
| P4 [Interfaces](https://www.palantir.com/docs/foundry/interfaces/interface-overview) | 官方技术文档；features、differences、support | 接口是共享形状/能力的抽象，不能直接实例化；可由多个具体类型实现 | 接口能力存在语言/服务差异；action type constraints 文档仍标为 beta，不能写全平台通用支持 |
| P5 [Functions in platform](https://www.palantir.com/docs/foundry/functions/use-functions) | 官方技术文档；Actions、Webhooks | 函数可计算逻辑、生成 Ontology edits 或外部请求参数；动作可触发外部 API | Ontology 对象编辑与外部系统事务不等价，不能推断跨系统原子性 |
| P6 [Action permissions](https://www.palantir.com/docs/foundry/action-types/permissions) | 官方权限文档；Apply action、read-time、submission criteria | 动作执行需满足相关资源权限与提交条件；读时行列过滤不自动延伸到写入，另有读写授权 | 不能把“看得见对象”或“LLM能调用工具”当充分写权限，也不能照搬旧权限模型 |
| P7 [AIP Chatbot tools](https://www.palantir.com/docs/foundry/chatbot-studio/tools) | 官方工具配置文档；Types of tools | 可配置对象查询的类型/属性；Action 可自动或确认后执行；Function 可固定发布版本 | 是工具接入契约，不证明模型会正确选工具、推理或安全执行 |
| P8 [CDC](https://www.palantir.com/docs/foundry/data-integration/change-data-capture) | 官方数据集成文档；backfill、CDC in Ontology | 回填与增量需配合主键/删除/排序信息；OSv2 Ontology 当前按到达顺序索引，忽略 changelog ordering 列 | 不能宣称平台自动解决事件时间乱序；上游需明确排序与回填策略 |
| P9 [How edits are applied](https://www.palantir.com/docs/foundry/object-edits/how-edits-applied) | 官方存储/冲突语义文档；persistent storage、conflict strategies | 数据源更新与用户编辑有独立冲突策略，默认编辑优先，可配置另一策略 | 不能把修改模型属性说成源系统已经改变；策略选择也不认证事实 |
| P10 [Branching ontology](https://www.palantir.com/docs/foundry/ontologies/branching-ontology) | 官方版本治理文档；resource protection、review/merge | 受保护资源通过分支与 proposal 变更；审批政策与普通 reviewer 列表不同 | 不代表所有资源/数据都被分支保护，也不保证迁移无错误 |
| P11 [Schema migrations](https://www.palantir.com/docs/foundry/object-edits/schema-migrations) | 官方版本迁移文档；breaking changes、new version | 主键、数据源、字段类型等改变可能破坏兼容；迁移后需重建并完成索引水合 | 部署 schema 不等于新数据视图已可用；平台迁移框架不是任意变更的自动修复器 |
| P12 [Ontology validation](https://www.palantir.com/docs/foundry/ontology/ontology-design-validation) | 官方方法指南；limits、real questions、people/agents | 结构审查不能代替真实问题验证；用未预演问题、非专用页面评估可发现性和表达性 | 厂商内部演练方法说明，不是独立公开效果实验 |
| P13 [Action test run](https://www.palantir.com/docs/foundry/action-types/test-run) | 官方执行文档；proposed changes、external calls | 预演不应用 Ontology edits；为计算结果而需执行的函数/外部调用仍可能运行，需确认 | 不能泛称 test-run 完全无副作用；“预演”和“沙箱”不能混用 |
| W1 [RDF 1.1 Concepts](https://www.w3.org/TR/rdf11-concepts/) | W3C Recommendation；图、IRI、dataset/named graphs | 三元组与图命名提供通用数据表达；图名的资源含义并非自动规定 | 三元组/图名本身不自动包含事实有效时间、授权或可信度 |
| W2 [OWL 2 Primer](https://www.w3.org/TR/owl2-primer/) | W3C Recommendation 的入门说明；knowledge、database differences | OWL 用公理表达知识并推导逻辑后果；开放世界下，缺失事实不自动为假 | OWL不是完整性检查数据库框架；推导不等于现实因果发现 |
| W3 [SHACL](https://www.w3.org/TR/shacl/) | W3C Recommendation；shapes、constraints、validation report | 可指定类型、基数、允许属性等约束并生成结果报告 | 只证明给定数据图对给定 shapes 的符合程度，不证明数据真实/最新/完整或动作获授权 |
| W4 [PROV-O](https://www.w3.org/TR/prov-o/) | W3C Recommendation；Entity/Activity/Agent、qualified relations | 可描述采集/转换使用与生成实体、责任主体、时间和修订/失效来源链 | 溯源是解释证据形成过程，不自动保证来源诚实或诊断因果正确 |
| O1 [OTel service conventions](https://opentelemetry.io/docs/specs/semconv/resource/service/) | 官方语义约定；Service Instance | namespace/name/instance 三层身份；不应在无法消歧时由 pod.name 猜 service.instance.id | OTel service 不等于 Kubernetes Service，名称相同不代表同一实体 |
| O2 [OTel Kubernetes conventions](https://opentelemetry.io/docs/specs/semconv/resource/k8s/) | 官方语义约定；names/UIDs、cluster | 推荐采集资源 UID；cluster.uid 是 kube-system namespace UID 的代理约定 | 不是 Kubernetes 原生 cluster-ID API，也不能直接等同 kubeconfig context 名称 |
| O3 [OTel Logs Data Model](https://opentelemetry.io/docs/specs/otel/logs/data-model/) | 官方规范；Timestamp/ObservedTimestamp | 来源事件时间与采集系统观测时间分开；来源时间未知允许缺失 | 不能用接收时间伪造事件时间；不保证不同机器时钟已同步 |
| K1 [Kubernetes names/IDs](https://kubernetes.io/docs/concepts/overview/working-with-objects/names/) | 官方 API 概念；UIDs | UID区分集群生命周期内相似实体的不同历史实例 | 名称复用不代表旧实例延续；跨集群仍应显式保存范围 |
| K2 [Kubernetes API concepts](https://kubernetes.io/docs/reference/using-api/api-concepts/) | 官方滚动文档；PATCH、resource versions、watch/410 | JSON Patch 可带 test 条件；watch需处理历史不可用；资源版本比较有版本/类型范围条件 | 资源版本不是事件时间、跨类型全局时钟，也不是跨资源事务保证 |

证据强度：标准定义和公开接口文档足以支持“规范/产品文档规定了何种机制”的窄主张；不能据此验证专有实现所有路径，也没有独立效果数据。不同 Palantir 页面属于同一厂商来源，不伪装成多方独立验证。未打开成功的早期导航不作为证据：Action test-run 初次点击失败，随后已直接打开并读取 External calls 正文；Functions TSv1 unit-test 导航未取得足够正文，因此不据其提出具体断言。

## 六个问题的构建方法

### 1. 从业务问题到 schema：先定义查询与决策闭环

建议先选少量可验收问题，例如“某业务请求失败影响哪些用户路径、对应哪个资源实例、最近有什么可核验变化、还缺什么证据、谁能执行哪个修复”。每个问题列出输入、需要的关系路径、必须展示的来源、预期未知和允许的动作，形成 competency questions。这里用的是领域建模通用方法名称，具体题目与验收方式是本项目建议；P1/P12为先领域后映射、真实问题验收提供一手依据。

再区分具体对象与抽象能力。例如 `LogicalService` 表示业务逻辑组件，`ServiceInstance` 表示某次运行实例，`KubernetesService` 表示服务发现资源；`Observation`、`DeploymentChange`、`Investigation`、`ActionProposal`、`ActionExecution` 是不同对象，不把日志行或告警文本直接视为资源自身。用接口表达 `ObservedResource`、`ScalableWorkload` 等跨类型共同能力，避免一个稀疏的大类型装下所有字段。P4支持接口抽象思想，具体接口名称与范围并非现有项目能力。

每个类型至少产出：语义定义/非含义、主键与显示键、属性类型/单位/枚举/可空性、生命周期与数据拥有者。每条关系至少产出：领域名称、domain/range、方向、预期基数、来源规则、身份/时间匹配条件、声明/观测/推导等级。P3明确提示基数未必被平台自动强制，本项目应自己验证，不能只画图。[P2](https://www.palantir.com/docs/foundry/object-link-types/create-object-type)、[P3](https://www.palantir.com/docs/foundry/object-link-types/create-link-type)

### 2. 实体消歧、时间与溯源：不要把“相似”变成“相同”

建议并列保存逻辑实体 ID 和资源实例 ID。前者可沿业务定义延续，后者至少绑定明确 cluster、namespace、API kind 和 UID；name用于查找和展示。OTel 的 `service.instance.id` 与 Pod UID 需要经可证明映射连接，不能自动替代。[O1](https://opentelemetry.io/docs/specs/semconv/resource/service/)、[O2](https://opentelemetry.io/docs/specs/semconv/resource/k8s/)、[K1](https://kubernetes.io/docs/concepts/overview/working-with-objects/names/)

映射证据应分级：UID/稳定业务键直接匹配；来自受控系统且可验证的引用；仅名称/标签候选。第三类保留候选与冲突，不进入实例级执行。LLM可提出候选或解释缺口，但不作为合并身份、补 UID、覆盖旧事实的权威。

观测记录建议有 `event_at`、`observed_at`、`ingested_at`，另用独立字段表达事实的有效区间与系统记录版本。有效时间没有来源时保存未知，不能从入库时刻补造。OTel 明确区分事件与观测时间；PROV 可用于描述哪个采集/转换活动由谁执行、使用哪些输入、生成哪个版本。[O3](https://opentelemetry.io/docs/specs/otel/logs/data-model/)、[W4](https://www.w3.org/TR/prov-o/)

溯源至少追到原始证据引用、采集主体/权限范围、工具/解析器版本、源对象定位、采集完整性和转换规则。事实、推导、用户声明和假设应分别建模。使用 RDF named graph 时仍需显式约定图的 run、时间和安全含义，RDF 本身不会自动赋予这些语义。[W1](https://www.w3.org/TR/rdf11-concepts/)

### 3. Source mappings 与增量：映射是需要版本化的契约

对每个 adapter 建映射表：源 API/字段路径 → 目标属性；转换/单位；身份字段；时间字段；缺失/null/删除语义；分页/截断限制；授权范围；刷新周期；来源权威与冲突策略。映射失败应生成缺口记录，不能产生“零后端”“对象已删除”等无依据结论。自动列名映射可以初始化候选，但不代替领域审查。

初始快照和后续增量应分开处理，增量记录含源身份、变更标识、操作类型和可用的顺序信息；明确去重、乱序、回填与重放行为。删除要有可靠 tombstone，或来自完整且范围一致的快照比较；不能因一次列表缺失就推断删除。P8的机制尤其说明：带有 ordering 字段不代表下游按它处理，须逐层验证真正使用的顺序语义。[P8](https://www.palantir.com/docs/foundry/data-integration/change-data-capture)

Kubernetes 可采用 list/watch 与断点管理，但必须处理分页及 `410 Gone` 后重建视图，并保留未知区间。当前官方文档已说明，1.35起的相应 API 实现对同 API group/resource type 的 RV 有数值单调比较要求；extension API需检查，不能跨类型/集群直接类比，也不能把 RV 转成事件时间。本项目维持不从 RV 推断跨对象时间是保守合理策略，不能把该策略写成“所有新版本 Kubernetes 永远禁止 RV 排序”。[K2](https://kubernetes.io/docs/reference/using-api/api-concepts/#resource-versions)

用户修订、期望配置和真实运行观测要分别保存。P9的编辑覆盖策略适合解释产品行为，但对 SRE 不能将“人工编辑优先”直接用于覆盖运行事实：期望状态变化仍需实际写入、重新取证和恢复验证。[P9](https://www.palantir.com/docs/foundry/object-edits/how-edits-applied)

### 4. 推理与验证：分清四种正确性

第一层是结构符合性：类型、单位、必填、引用、基数、范围、时间区间等。可先用现有 JSON/Python 校验实现，并按 SHACL 的 shapes/report 思路输出可定位违规；采用 RDF 时再考虑直接使用 SHACL 处理器。第二层是语义一致性：类型约束、关系规则是否矛盾，OWL可在其形式语义下支持推导。开放世界下没有记录不表示为假；若要以完整清单证明不存在，必须先定义并验证完整性边界。[W2](https://www.w3.org/TR/owl2-primer/)、[W3](https://www.w3.org/TR/shacl/)

第三层是证据有效性：来源、身份、时效、覆盖与冲突是否足以支持某条事实。第四层是诊断/业务结论：结构关联或一致的图路径不能单独证明故障根因；需要竞争假设、鉴别取证与动作后的独立业务指标。后两层是本项目必须另行设计的验证任务，不能由 OWL/SHACL 合格标记替代。

关系推导建议采用有界、可解释规则，输出输入观测 ID、规则版本、前提、适用时间和等级。`Service selects Pod` 可是标签匹配结构事实，`Pod causes outage` 必须保持假设或单独评估结论。缺失输入、冲突、历史证据、截断应导致无法确定，不应触发闭世界负结论。

### 5. 动作、函数与 AIP：把能力绑定到可执行契约

函数负责计算和推导，动作负责受控状态变化；工具只是暴露这些能力的调用接口。对象查询应按授权过滤类型、属性和范围；函数与动作绑定可追溯版本，不能把“自动最新”默认为安全升级。P7展示这种工具配置机制，但正确工具选择仍需评估。[P5](https://www.palantir.com/docs/foundry/functions/use-functions)、[P7](https://www.palantir.com/docs/foundry/chatbot-studio/tools)

每类动作建议定义：目标类型与实例 ID；参数 schema；意图与预计 diff；当前证据/版本；实时前置条件；执行主体与授权；风险/审批；幂等与未知结果处理；允许的后置条件与新回退方案。读权限、改 schema 权限和执行写权限是不同能力；必须在服务端执行每次检查，不依靠前端按钮或提示词。P6明确读时过滤不自动约束写入，值得直接借鉴为设计审查项。

预演还应分类：仅计划、纯数据模拟、Kubernetes server-side dry-run、允许外部调用的测试、真实受控执行。P13明确平台 test-run 也可能运行所需外部调用，因此不能仅凭名称证明无副作用。本项目现有计划记录应继续如实标识，不能升级宣称完整 dry-run 或业务恢复验证。[P13](https://www.palantir.com/docs/foundry/action-types/test-run)

### 6. 版本演进与评测：内容 revision 不等于 schema 迁移

分别版本化领域 schema、源映射、推导规则、工具/动作契约和每次事实快照。类型/属性改名、主键变化、基数收紧、单位改变、来源替换、权限收紧都需要兼容性评审及重放。保存迁移说明、旧数据如何解释、索引何时可用、撤销路径；受保护变更采用分支、评审和晋升，不能只改一个版本字符串。[P10](https://www.palantir.com/docs/foundry/ontologies/branching-ontology)、[P11](https://www.palantir.com/docs/foundry/object-edits/schema-migrations)

验收应包含未参与设计的真实问题，禁止依赖预置固定页面路径掩盖模型缺口；对人和 agent 分别检查能否自行找到对象、链接与证据。另做固定快照的旧/新版本对照，统计身份错误、过期事实、非法关系、证据引用、问题完成率与查询/工具成本；按故障域分组，不能只看整体均分。P12支持问题驱动的验证方法，具体回归指标和晋升门槛仍是本项目建议。

## 当前代码差距映射（仅据所读三份文件）

| 方面 | 已读代码证据 | 当前可谨慎确认 | 下一步缺口/验收 |
|---|---|---|---|
| schema 与对象 | `operational_ontology.py:18/27/76` | 有固定schema版本、能力列表和显式对象解析；文档说明schema不是已采集事实 | 缺少独立、可审查的类型/属性/单位/基数/接口定义注册；补真实业务服务与K8s资源的语义分离 |
| 实例身份 | `operational_ontology.py:76` | cluster/namespace/kind/name/UID生成ID；保留name-bound与scope-incomplete | 增加API group及多源身份映射契约、映射证据等级；同名跨域/重建不得自动合并。不能假定已有OTel跨源身份注册 |
| 时间与来源 | `operational_ontology.py:69/125/139/231` | 观测ID、观测/事件/采样时间与历史标记；保留多次采集与审核节选区别 | 未见完整双时态/有效区间、源offset与跨run历史物化；来源/转换版本可进一步统一，不把run视图当长期时态图 |
| 事实关系等级 | `operational_ontology.py:277`附近、`skill_runtime/service_topology.py:137`起 | 旧`owned_by`/`scheduled_on`可表达源字段引用；拓扑模块的严格关系要求已观测UID、范围、非历史、完整投影；明确非因果 | 将声明引用、候选、两端核验结构事实统一显式分级，避免界面/消费者把旧引用边与严格关系看成同级事实 |
| 结构拓扑 | `skill_runtime/service_topology.py:23/137/189`附近 | 白名单投影；selector/ownerUID/targetRef规则；冲突与上限生成缺口；关系有两端证据与非原子标识 | 扩展跨域适配器前先做映射注册与规则校验；不按IP/命名惯例猜Pod，不把未列出当不存在 |
| 增量与查询 | `operational_ontology.py:231/355/547` | 从已保存run确定性构建视图，带revision，支持集合/实体分页查询 | 未见通用CDC/watch、有效期索引或历史as-of查询；应按实际跨调查需求引入，不是立即升级为大型图平台 |
| 动作 | `operational_ontology.py:375/414/484` | recorded context不授权；有限Deployment动作编译UID/RV条件JSONPatch，单对象边界明确 | 类型化前后置条件、跨主体对象级权限与所有入口统一仍需专项工作；不能将该模块视为全平台授权实现 |
| 版本/评测 | `operational_ontology.py:18/371`，`docs/operational-ontology.md:55/73/110` | 有schema标记、内容revision和来源投影版本；文档记录局部远端回归与覆盖边界 | 本轮未重新核验测试；内容hash不是语义兼容性或迁移；需领域问题集/旧新映射回放/越权与缺口测试 |

上述行号是访问日定位，不是版本锁定。当前源码明确是 run-scoped 观测视图，未连接 Palantir 商业服务；文档还说明旧手工/告警入口未全部迁移、跨调查历史CMDB与外部Git/CD事实仍有边界。读取文档中测试结果只能证明项目已有记录，不能代替本轮亲自复现，也不证明全故障准确率。

## 建议分阶段产物与验收（尚未实现）

1. **领域问题与类型契约**：建立问题清单、对象/关系字典、身份与时间约定；每个类型/边能解释解决哪个问题。反例是同一“Service”混用业务逻辑组件、发现资源和运行进程。
2. **映射与校验注册**：将已存在的K8s/拓扑adapter整理为显式映射与规则版本，保留现有存储；每个事实可回到源字段、观测、规则及完整性。验收重建UID、同名跨cluster、null/缺失、截断与乱序。
3. **只读语义查询与缺口协议**：返回确定事实、候选、冲突和未知，支持受控关系遍历与查询解释；对多跳查询设范围/成本上限。验收不凭空补边、不用摘要替代原证据、旧事实不冒充现在。
4. **动作契约扩展**：复用现有门禁，补动作参数/权限/前后置条件、明确每种预演语义；验收版本过期、对象重建、拒绝写入、未知副作用与新取证回退。关系推导不能增加写权限。
5. **演进与独立问题回放**：保护模型/映射/规则变更，对留出问题及固定历史快照比较正确性和成本；未通过兼容、越权与故障域退化门槛不得晋升。结果应保留失败，不把“页面可打开”当模型正确。

## 竞争假设、反证条件与不确定性

H1：当前主要瓶颈是身份、映射、语义层级与评测契约，薄层加约束即可支持近期需求。H2：主要瓶颈是跨run、多跳、历史时态查询，须更强图/时态基础设施。H3：主要瓶颈是取证覆盖不足，改模型结构仍不能补回缺失事实。

当前更支持先完成H1并核查H3：代码已经有保守身份和结构关系规则，但仍以单run投影为中心。若完备映射与足够数据下，多跳/历史问题持续无法在成本预算内回答，且受控对照证实图/时态实现有净收益，才提高H2优先级。若新增schema只增加无法采集字段、查询仍依赖猜测，则H3优先，先接可靠源而不是增加类型。

对文档机制与所读代码边界置信度高；对建议能改善哪些问题为中等置信度；对改善幅度、产品全平台一致性与运行性能无本轮证据。最脆弱前提是源数据身份、时间及完整性可取得且可追溯。任何无法满足此前提的关系都应降级或显示缺口，而不是补成“完整数字孪生”。
