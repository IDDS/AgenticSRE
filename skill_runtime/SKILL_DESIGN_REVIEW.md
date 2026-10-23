# AgenticSRE Skill Runtime Design Review

## 背景

当前 Skill Runtime 已经能以 `skill.yaml + skill.py` 形式执行检测、诊断、自愈和验证，并能与原 AgenticSRE 自愈知识库并行/兜底。这个设计适合快速迁移，但与开源社区常见的 Skill 写法仍有差距：开源 Skill 通常以 `SKILL.md` 作为第一等契约，运行脚本、引用资料和适配器作为渐进加载资源，而不是只把 Markdown 当作说明文档。

Claude Code CLI 已通过精简代码快照完成评审。本审查综合 Claude Code 建议、本地代码、系统 Skill Creator 规范以及当前仓库实现。

需要明确的是，当前系统中的 Skill 分支主要由前端或后端直接调用 Skill Runtime 完成编排；这不是智能体在推理过程中自主选择并调用 Skill。Agent 链路仍然是多智能体 RCA 与自愈知识库链路。后续若要实现真正的 Agent 调用 Skill，需要把 Skill Runtime 暴露为 Agent 工具，并将调用、输入、输出和证据写入 Agent trace。

## Claude Code 关键建议

Claude Code 指出当前 Skill Runtime 的核心问题不是“没有执行函数”，而是 Skill 契约和运行入口割裂：`skill.yaml` 是运行时事实来源，`SKILL.md` 只是说明文档；registry 只扫描 `skill.yaml` 并强依赖 `skill.py`，导致社区常见的 Markdown-first Skill、脚本型 Skill 和文档型 Skill 都无法被自然接入。

建议目标形态是：

- `skills/<skill_id>/SKILL.md` 成为第一等契约，支持 YAML frontmatter。
- `SKILL.md frontmatter > skill.yaml > 默认值`，冲突时记录 metadata warnings。
- `skill.py`、`scripts/`、`references/` 作为渐进加载资源，而不是唯一入口。
- Runtime 保持 `detect / diagnose / heal / verify(context) -> dict` 的阶段接口，文档型 Skill 可进入 Catalog，但执行阶段标记为 skipped。
- Catalog/详情页显示 source of truth、contract status、frontmatter、适配器入口、缺失章节和元数据覆盖 warning。
- 后续再把 workflow 中按 skill id 写死的匹配逻辑迁移到 `matching.keywords`、规则文件或引用资料中。

第二轮 Claude Code 只读审查进一步指出，当前设计方向正确，但在接入外部 Skill 前必须先处理几个会随规模放大的结构性风险：

- `registry._load_module` 加载 `skill.py` 时会执行顶层代码，单个坏 Skill 不应拖垮整个 registry。
- `run_matching` 不应为了判断命中而跑完 `detect/diagnose/heal/verify`，未命中 Skill 不能进入 `heal`。
- `dry_run` 不能只作为约定传给 Skill，运行时需要检测 heal 阶段是否报告了真实执行。
- `workflow._capability` 与 `policy.build_policy` 存在门禁双写，后续需要统一到单一 policy 管道。
- `SkillRun.trace` 还不足以审计工具调用和证据流，后续需要升级为 stage event + tool event。

已落地的止血改动：

- registry 热加载前清理旧的 `skill_runtime.loaded.*` 模块，单个入口加载失败降级为不可执行 Skill，并把失败原因写入 warnings。
- `run_matching` 先只执行 `detect`，命中后才执行完整阶段，避免未命中 Skill 触发 heal/verify。
- `dry_run=True` 时，如果 heal 阶段返回 `executed=True`，executor 会标记为 `dry_run_contract_violation`。

## 主要差距

1. **契约中心不一致**
   当前运行时以 `skill.yaml` 和 `skill.py` 为中心，`SKILL.md` 可选。这样便于执行，但 Catalog 中很多 Skill 看起来更像插件函数，不像可被社区复用和审查的完整 Skill。

2. **通用 Skill 与场景 Skill 边界不清**
   `generic.fault_detection`、`generic.evidence_collection`、`generic.healing_planner` 是编排辅助能力；`k8s.service_unavailable`、`llm.k8s_vllm_observability` 是场景诊断能力；`tool.k8s_readiness` 是工具 Skill。现在三类都混在同一个列表中，用户不容易理解触发链路。

3. **Skill 过程和工具调用没有标准化事件模型**
   `SkillRun.trace` 只有 stage、status、confidence、duration，缺少标准化的 tool call、input/output 摘要、证据来源、门禁判定和验证结果。前端只能展示“跑了哪些 Skill”，但不够像可审计的运维流程。

4. **Markdown 渐进披露不足**
   部分 Skill 已有 `SKILL.md`，但很多 K8s 和 generic Skill 没有。已有 Markdown 也大多是说明，没有明确指向 `references/`、`scripts/`、`assets/` 等可复用资源。

5. **运行逻辑仍偏 hard-coded**
   `workflow.py` 中有较多按 `skill_id` 写死的 affinity、primary 选择和桥接逻辑。短期可接受，长期应移动到 Skill metadata 或规则引用中。

## 目标 Skill 合同

每个完整 Skill 推荐采用以下结构：

```text
skill_runtime/skills/<skill_name>/
  SKILL.md              # 第一等契约，面向智能体和审查者
  skill.yaml            # 运行时索引、权限、风险、置信度、UI 元数据
  skill.py              # 可测试的 runtime adapter
  references/           # 可选，规则表、指标映射、诊断树、SOP
  scripts/              # 可选，确定性采集/验证脚本
```

`SKILL.md` 至少包含：

- `适用场景`: 触发条件、适用对象、不要使用的场景。
- `输入信号`: 告警、指标、K8s 对象、TELLER trace、实验上下文等输入字段。
- `执行阶段`: detect / diagnose / heal / verify 的职责和输出。
- `风险与门禁`: 权限、审批、dry-run、回滚、自动执行阈值。
- `验证方式`: 恢复后的检查命令、指标阈值和成功条件。

`SKILL.md` frontmatter 和 `skill.yaml` 共同承担机器可读索引；其中 frontmatter 优先：

- `id/name/domain/stages/description`
- `risk/permissions/tool_dependencies/tags`
- `policy.requires_approval`
- `policy.safe_auto_execute`
- `confidence.min_evidence_sources`
- `confidence.min_confidence_for_heal`
- `confidence.min_confidence_for_auto_heal`
- `ui.icon/category`
- `entrypoint: skill.py | scripts/<name>.py`
- `matching.keywords`
- `allowed-tools`
- `license`

`skill.py` 只做可执行适配：

- 不嵌入长篇说明，说明进入 `SKILL.md` 或 `references/`。
- 所有真实变更必须先输出 dry-run plan。
- stage 输出遵循 `status/confidence/summary/evidence/output`。
- heal 输出 `suggestions`，verify 输出 `checks`。

## 目标框架分层

Claude Code 建议将当前系统从“可运行脚本集合”继续演进为“可治理的 Skill 编排框架”，核心分为五层：

1. **SKILL.md 契约层**
   - `SKILL.md` 是面向智能体、审查者和社区复用的第一等契约。
   - frontmatter 承担机器可读元数据，正文说明适用场景、输入信号、执行阶段、风险门禁和验证方式。
   - `skill.py` 只作为运行适配器，不再承载长篇说明。

2. **Registry / 加载治理层**
   - 负责发现 Skill、合并 `SKILL.md frontmatter` 与 `skill.yaml`、校验入口与记录 warnings。
   - 单个 Skill 加载失败必须隔离，不影响其他 Skill。
   - 后续外部 Skill 导入必须进入 review-only 或 sandbox 状态。

3. **Executor / 阶段执行层**
   - 执行 `detect / diagnose / heal / verify` 四阶段。
   - `run_matching` 先执行 `detect`，命中后才进入完整阶段，避免未命中 Skill 触发自愈逻辑。
   - `dry_run` 需要成为运行时强制约束，而不只是传给 Skill 的约定。

4. **Workflow / 编排决策层**
   - 负责多 Skill 匹配、主 Skill 选择、诊断融合、legacy fallback 和兼容输出。
   - 后续应将 `_payload_affinity`、primary 选择和桥接逻辑逐步迁移到 `matching.keywords` 或规则文件中。
   - `policy.build_policy` 应成为唯一门禁来源，减少 workflow 中二次拼装门禁。

5. **Trace / 审计展示层**
   - 当前 trace 只记录阶段状态、置信度和耗时，不足以解释 Skill 如何得出结论。
   - 后续应升级为 stage event + tool event，记录输入摘要、输出摘要、证据、门禁决策和验证结果。
   - 前端告警中心与 Skill Catalog 应基于 trace 展示完整过程。

## 推荐分类

- **Workflow Skills**
  - `generic.fault_detection`
  - `generic.evidence_collection`
  - `generic.root_cause_diagnosis`
  - `generic.diagnosis_fusion`
  - `generic.healing_planner`
  - `generic.risk_gatekeeper`
  - `generic.recovery_verifier`

- **Domain Skills**
  - `k8s.*`
  - `llm.k8s_vllm_observability`
  - `llm.gpu_resource_pressure`
  - `llm.business_slo`
  - `llm.aishperf_aiinfra_diagnosis`

- **Tool Skills**
  - `tool.k8s_readiness`
  - `tool.db_connection_pool`
  - `tool.llm_host_readiness`

前端 Catalog 应按这三类展示，不应把通用编排 Skill 与场景 Skill 混成同一层级。

## 迁移计划

1. **阶段 1: Contract 可见化**
   - 已新增 `contract_status`，在 Skill Catalog/detail 中展示完整度。
   - 已支持 `SKILL.md` frontmatter 作为优先元数据来源。
   - 已允许 Markdown-only 文档型 Skill 加载到 Catalog，运行时阶段返回 skipped。
   - 不影响现有 `skill.yaml + skill.py` 运行时和 API。

2. **阶段 2: 补齐核心 SKILL.md**
   - 优先补齐所有 `generic.*`、`k8s.*`、`llm.*` 中缺失的 `SKILL.md`。
   - 每个 Markdown 只保留核心流程，长规则进入 `references/`。

3. **阶段 3: 规范 trace**
   - 扩展 `SkillRun.trace` 为 stage event + tool event。
   - 每个事件包含 `type/stage/skill_id/input_summary/output_summary/evidence/policy/duration_ms`。
   - 前端分析弹窗和事件中心使用该 trace 展示 Skill 执行过程。

4. **阶段 4: 去 hard-code affinity**
   - 将 `_payload_affinity` 中的场景词、指标名、优先级逐步迁移到 `skill.yaml` 的 `match` 字段或 `references/match_rules.yaml`。
   - Workflow 只负责解释规则，不直接写死具体故障类型。

5. **阶段 5: 社区 Skill 导入**
   - 社区 Skill 先进入 `draft` 或 `review.only` 状态。
   - 转换后必须补齐 AgenticSRE 合同、权限、dry-run、测试和验证。
   - 不直接启用外部 Skill 的写操作。

## 前端影响

Catalog 每个卡片应展示：

- 类型: Workflow / Domain / Tool / Bridge。
- 完整度: 完整 Skill / 运行可用契约待补全 / 仅运行时 Skill / 草案。
- 风险: low / medium / high / critical。
- 权限: read/write/restart/cloud 等。
- 阶段覆盖: detect / diagnose / heal / verify。
- 文档来源: SKILL.md / generated_from_skill_yaml。

Detail 弹窗应展示：

- Markdown 全文。
- Contract status 和缺失项。
- skill.yaml。
- skill.py。
- 最近执行 trace。
- 测试覆盖状态。

## 用户视角优化

Skill Runtime 不应作为新的复杂入口暴露给一线用户。用户视角的主路径应保持稳定：

1. 生产故障从通算告警中心或智算事件中心进入。
2. 告警详情中选择 Agent 分析、Skill 分析、Agent 自愈或 Skill 自愈。
3. SkillOps 页面只承担三类任务：独立调试、能力目录、运行审计。
4. 前端默认展示闭环路径、覆盖状态和最近运行结果，减少解释性文字。
5. 每次运行必须把“检测、诊断、门禁、自愈、验证”五步压缩成可扫读摘要；需要深挖时再展开 trace、SKILL.md、skill.yaml 和源码。

面向用户的页面应优先回答四个问题：

- 该从哪里开始：通算告警、智算事件或独立调试。
- 当前用了哪个 Skill：主 Skill、匹配数量和迁移模式。
- 结果能不能信：证据、置信度、阈值和门禁。
- 是否可以恢复：Dry-run 方案、审批要求、回滚和验证项。

这意味着 Catalog 不应只按文件或目录展示，而应按用途归类：通用编排、场景诊断、工具采集、自愈能力。完整 Markdown 契约仍然保留，但默认以摘要和入口动作呈现，全文通过详情弹窗渐进展开。

## Agent 调用 Skill 的演进要求

当前状态：

- 告警中心的 Skill Runtime 分析/自愈是直接调用 `/api/skill-workflows/operate`。
- 智算事件中心和故障实验中的 Skill 闭环也是直接调用 Skill Runtime。
- Agent 分析按钮不会自动读取 `SKILL.md`，也不会把 Skill 作为工具自主调用。

要达到 “Agent 调用 Skill”：

1. 在 Agent 工具层注册 `skill_runtime.run`，参数包含 `payload`、`domain`、`stages`、`dry_run`。
2. Agent 在 RCA 过程中根据告警、证据和 Skill Catalog 选择候选 Skill。
3. Agent 调用 Skill 后把 `matched_count`、`primary_skill_id`、各阶段输出和证据写入 RCA event trace。
4. 自愈阶段必须复用 Skill policy，不能绕过 dry-run、审批和置信度门禁。
5. 前端对 Agent 链路展示 “Agent 调用 Skill”，对直接链路展示 “Skill Runtime 直接编排”。

## 测试策略

- **Schema tests**: 每个 Skill 必须可加载，metadata 合法。
- **Contract tests**: 完整 Skill 必须有 `SKILL.md` 且包含必需章节。
- **Stage tests**: detect/diagnose/heal/verify 对代表性 payload 输出稳定字段。
- **Policy tests**: 高风险 Skill 不允许自动执行，低置信度阻断 heal。
- **Workflow tests**: 同一事件下 primary skill 选择稳定，legacy fallback 标识正确。
- **Frontend tests**: Catalog 展示 contract status；详情弹窗可查看 Markdown/yaml/python。
- **Real fault tests**: 通算和智算场景分别用真实故障验证诊断、自愈建议和恢复校验。

## 结论

当前系统已经具备 Skill Runtime 的执行骨架，但还不是完全开源 Skill 风格。建议保留现有可运行的 `skill.py` 适配器，同时把 `SKILL.md` 提升为第一等契约，并用 `contract_status` 驱动前端 Catalog、测试和迁移节奏。这样可以在不破坏现有 AgenticSRE 闭环的前提下，逐步演进到更轻量、更可扩展、更接近社区 Skill 的架构。
