---
name: k8s-guarded-fault-injection
description: 为 AgenticSRE Kubernetes 故障演练生成受控、可审批、可自动清理和可恢复验证的强类型注入契约。
---

# K8s 受控故障注入

该 Skill 把故障演练意图转换为 `agenticsre.k8s.fault.inject` 结构化操作。它只规划和校验，不执行 shell；可信执行器必须通过 `scenario_id` 重新读取服务端登记场景。

## 适用场景

用于 AgenticSRE 已登记的 Kubernetes 常规故障演练和细粒度故障预演。不用于运行临时命令、未登记场景或不明确的生产目标。

## 输入信号

必填输入为 `scenario_id`、`registered_scenario=true`、`namespace`、`target_entity_ids`、逐目标 `target_scopes` 和 `duration_s`。每个作用域都必须把目标实体绑定到精确 Namespace；安全输入必须包含爆炸半径预算，并保持 `approval_required=true`、`auto_cleanup=true`。

## 执行阶段

- `detect` 识别明确的故障注入意图。
- `diagnose` 检查场景登记、目标、时间、审批、爆炸半径和细粒度字段。
- `heal` 生成强类型 inject、cleanup 和 verification 操作，不执行变更。
- `verify` 给出目标组、对照组、清理和基线恢复检查项。

## 风险与门禁

- 不接受未登记场景、任意命令、脚本或 kubectl 文本。
- 真实注入必须显式确认，并严格限制目标数量和故障持续时间。
- 输出必须绑定 `agenticsre.k8s.fault.cleanup` 精确回滚和检查点恢复验证，跨 Namespace 场景不得丢失逐目标作用域。
- 注入真值不得进入后续被动诊断 Agent 的输入。

对细粒度场景，`precision` 只允许服务端登记的请求、系统调用、eBPF、字节码、原生函数、Failpoint、cgroup v2、DCGM 或临时卷强类型参数。在 AgenticSRE 环境完成独立验收前，这些场景一律为 Dry-run，不标记任何未由 AgenticSRE 独立验证的实测状态。

## 验证方式

预演必须输出精确目标、持续时间、有限命中、终止条件、Undo 和需要的 before/during/control/recovery 证据。真实执行验收还必须证明对照组未受影响、资源已自动清理且基线已恢复。
