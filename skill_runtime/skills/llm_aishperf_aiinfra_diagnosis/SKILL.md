---
name: AISHPerf 智算基础设施诊断适配
description: 将 AISHPerf/Alcor AIOps 故障场景和 aiops-eval 诊断领域接入 AgenticSRE，
  作为只读证据和恢复建议来源。
---
# AISHPerf 智算基础设施诊断适配

本 Skill 是 review-only 外部故障/诊断方法集成适配器，将 AISHPerf/Alcor AIOps 的 aiops-chaos 与 aiops-eval 诊断域接入 AgenticSRE，仅作为证据与恢复建议来源，不直接执行任何变更或注入。

## 适用场景

- 需要复用 AISHPerf 的 AI 基础设施故障模式与诊断方法作为外部证据。
- 对 GPU、训练、推理等 AI Infra 场景进行根因分析补充。
- 引用 aiops-chaos 的故障定义与 aiops-eval 的诊断基准进行对照评审。
- 为 AgenticSRE 提供跨来源的诊断交叉验证。

## 输入信号

- AISHPerf `entities/aiops-chaos` 的故障场景与注入定义，仅用于读取和评审。
- `suites/aiops-eval` 的诊断域、评测样例与期望结论。
- 本地可观测性只读指标、日志与事件。
- 现有 Skill 输出的候选根因与证据链。

## 执行阶段

- `detect`: 从外部 chaos/eval 域映射匹配当前症状特征。
- `diagnose`: 引用外部诊断方法生成候选根因，标注来源与置信度，不执行外部注入。

本 Skill 只负责信号匹配与诊断。恢复建议可以作为诊断附件输出，但不构成本 Skill 的恢复或验证阶段。

## 风险与门禁

- 风险等级 high，涉及外部 chaos 方法集成，必须人工审批。
- `requires_approval=true`，`safe_auto_execute=false`。
- 本适配器为 review-only，永不主动注入故障或执行变更。
- 至少需 2 个独立证据来源方可给出结论。

## 验证方式

- 确认所有外部 chaos/eval 引用均为只读评审，无实际注入发生。
- 候选根因带外部来源标注、置信度与证据引用。
- 建议命令均为 dry-run 且经审批后方可执行。
- 对照 aiops-eval 的真实根因标签核对诊断准确性；恢复验证结果单独用于评价建议动作，不进入根因置信度或诊断准确率统计。
