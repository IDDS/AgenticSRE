---
name: K8s 网络策略阻断
description: 诊断 NetworkPolicy 或 NetworkChaos 导致的流量阻断，不执行破坏性恢复动作。
---
# K8s 网络策略拦截诊断

本 Skill 用于诊断 NetworkPolicy 或 NetworkChaos 导致的流量阻断，仅提供只读诊断与非破坏性建议。

## 适用场景

- Pod 间或 Pod 到 Service 的连接被拒绝或超时，疑似策略拦截。
- NetworkPolicy 的 ingress/egress 规则过严，误伤正常流量。
- 故障注入造成丢包、延迟或分区。
- 命名空间默认拒绝策略未放行必要流量。

## 输入信号

- 源 Pod 到目标的连通性测试结果。
- NetworkPolicy 规则明细。
- 命名空间/Pod 标签与策略 selector 的匹配关系。
- 是否存在 NetworkChaos 等故障注入资源。

## 执行阶段

- `detect`: 确认流量阻断现象并锁定源/目标。
- `diagnose`: 分析生效的 NetworkPolicy 与标签选择器，确认是否存在 Chaos 注入。
- `verify`: 在外部批准执行调整后，复测连通性确认恢复。

本 Skill 不声明恢复阶段；需要放行规则或清理注入时，由受控恢复 Skill 在审批后执行。

## 风险与门禁

- 风险等级 medium，本 Skill 仅具备 `k8s.read` 权限。
- `requires_approval=true`，`safe_auto_execute=false`。
- 任何策略或 Chaos 变更由外部有写权限流程人工审批执行。
- 至少 2 个独立证据来源方可给出修复建议。

## 验证方式

- 源到目标的连通性测试恢复正常。
- 生效的 NetworkPolicy 明确放行所需流量，无遗留误拦截。
- 确认 NetworkChaos 等注入资源已按需移除或不再影响目标流量。
