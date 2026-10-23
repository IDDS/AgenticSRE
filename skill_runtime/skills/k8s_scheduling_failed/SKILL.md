---
name: K8s 调度失败
description: 检测、诊断资源、污点或配额限制导致的 Kubernetes 调度失败，并生成只读处置指导。
---
# K8s 调度失败诊断与只读建议

本 Skill 用于诊断由资源、污点或配额约束导致的 Pod 调度失败，并给出只读整改建议。

## 适用场景

- Pod 长期处于 `Pending`，`FailedScheduling` 事件持续出现。
- 无节点满足资源请求、节点选择器或亲和性约束。
- 节点污点与 Pod 容忍度不匹配。
- 配额不足或拓扑约束导致无法调度。

## 输入信号

- `kubectl describe pod` 中的 `FailedScheduling` 原因说明。
- Pod 的 requests、nodeSelector、affinity、tolerations 配置。
- 节点资源余量、污点与标签信息。
- 命名空间配额与 PVC 绑定状态。

## 执行阶段

- `detect`: 识别 Pending 与 FailedScheduling，确认为调度失败。
- `diagnose`: 排查资源、亲和性、污点、配额与拓扑约束，定位不可调度原因。
- `verify`: 建议采纳后复核 Pod 是否被成功调度。

本 Skill 不修改调度约束或工作负载；修复动作由独立恢复流程审批和执行。

## 风险与门禁

- 风险等级 low，仅只读诊断。
- `requires_approval=false`，`safe_auto_execute=true`。
- 至少需 2 个独立证据来源方可给出结论。
- 进入自愈门禁所需的最低校准诊断置信度为 0.78，自动执行只读建议门槛为 0.90；两者均不表示恢复成功概率。

## 验证方式

- Pod 从 `Pending` 变为 `Running`，成功绑定节点。
- `FailedScheduling` 事件停止新增。
- 调度约束满足，资源与配额校验通过。
