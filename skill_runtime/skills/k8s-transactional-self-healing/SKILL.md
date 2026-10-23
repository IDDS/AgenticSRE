---
name: k8s-transactional-self-healing
description: 将有证据支撑的诊断绑定到原故障交易的精确 Undo，并对照检查点验证恢复，禁止生成任意命令或扩大作用域。
---

# K8s 事务式自愈

该 Skill 仅用于 AgenticSRE 已登记故障实验的恢复。输入必须包含诊断、故障前检查点、强类型恢复策略，以及注入阶段生成并已获审批的 `agenticsre.k8s.fault.cleanup` 或 `agenticsre.precision.cleanup`。

## 适用场景

用于已获审批故障交易的精确恢复，特别是注入后需要保证不扩大作用域的自动清理。普通 RCA 变更、未知故障和临时修复不适用。

## 输入信号

需要 `incident_id`、已登记 `scenario_id`、集群与 Namespace、逐目标精确 Namespace 作用域、至少 80% 置信度的诊断、故障前检查点、恢复策略、原交易 Undo 和审批依据。

## 执行阶段

- `detect` 识别事务式 Undo 恢复请求。
- `diagnose` 比对诊断、场景、作用域、检查点、恢复策略和审批依据。
- `heal` 生成 `agenticsre.k8s.remediation.execute_undo` 操作，封装原 cleanup，不生成新命令。
- `verify` 对比检查点，并检查 Deployment、Pod、Endpoint 和辅助负载清理。

## 风险与门禁

- 不接受或输出 command、shell、script、kubectl。
- 场景、集群、每个目标及其 Namespace 作用域必须与原 Undo 完全一致。细粒度场景还允许已登记的单 GPU 和一次性卷目标，但不允许调用方修改目标类型。
- 诊断置信度不得低于 80%，恢复时间窗限制为 5 至 120 秒。
- 只有原故障交易已获审批时，精确 Undo 才复用该审批；普通 RCA 自愈仍走现有人工审批流程。
- Undo 后必须检查 Deployment、Pod、Endpoint、辅助负载清理和故障前检查点，无回归才算闭环完成。

## 验证方式

安全测试要覆盖低置信度、缺失检查点、作用域不一致、任意命令和无审批依据均被阻断。成功计划必须保留原 Undo、检查点指纹和基线对比项，执行失败或验证未通过时事件不能关闭。
