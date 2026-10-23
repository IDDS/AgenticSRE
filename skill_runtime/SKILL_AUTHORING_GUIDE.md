# AgenticSRE Skill Markdown Authoring Guide

## 当前调用关系

AgenticSRE 目前有两条处置链路：

1. **Agent 链路**: 多智能体 RCA 和自愈知识库生成分析与恢复建议。
2. **Skill Runtime 链路**: 前端或后端根据告警/事件 payload 直接调用 `SkillWorkflow.run`，由 Registry、Executor 和 Workflow 匹配并执行 Skill。

当前告警中心的 “Skill Runtime 分析/自愈” 属于第二类。它不是 Agent 在推理过程中自主选择并调用 Skill。后续如果要实现 Agent 调用 Skill，需要在 Agent 工具层暴露 `skill_runtime.run(payload, stages)`，并把 Skill 调用结果写入 Agent trace。

## SKILL.md 最低内容

每个可执行 Skill 的 Markdown 不应只写简介，至少包含：

- `适用场景`: 触发条件、适用对象、不要使用的场景。
- `触发方式`: 告警中心、智算事件中心、故障实验、SkillOps 或 Agent 工具调用。
- `输入信号`: 必填字段、可选字段、关键关键词和指标。
- `执行阶段`: 只描述 `skill.yaml` 中实际声明的阶段及其职责、输出和跳过条件。
- `输出契约`: 诊断字段、证据字段、自愈建议、验证检查项。
- `工具与权限`: 会读取或修改什么系统，是否需要 kubectl/curl/prometheus。
- `风险与门禁`: dry-run、审批、置信度阈值、回滚要求。
- `验证方式`: 如何确认恢复有效，哪些信号可判定失败。
- `示例 payload`: 最小可复现实例。

## 写作边界

- `SKILL.md` 写核心流程，不堆长篇背景。
- 详细规则、指标映射、SOP 放入 `references/`，并在 `SKILL.md` 中说明何时读取。
- `skill.py` 只做运行适配，不承载说明性文档。
- `detect / diagnose / heal / verify` 是全局编排协议，不是每个 Skill 都必须实现的固定模板。
- 未声明阶段必须写成上游输入、下游交接或非目标，不能放在“执行阶段”中冒充本 Skill 能力。
- 任何可能改变系统状态的 Skill 必须说明 dry-run 和审批门禁。

## 完整度分级

- **Contract complete**: 字段齐全，可运行，但内容可能只是摘要。
- **Operational complete**: 包含触发方式、输入输出样例、工具权限、风险门禁和验证方法。
- **Agent-ready**: 额外说明 Agent 何时选择它、需要读取哪些 references、如何把结果写入 Agent trace。
