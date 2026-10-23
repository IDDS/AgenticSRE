---
name: 通用自愈规划
description: 生成受控恢复方案，输出低风险检查动作和需要审批门禁的恢复动作。
---
# 通用自愈规划

## 适用场景

用于在没有专用自愈 Skill 或专用 Skill 只给出诊断时，生成保守的通用恢复计划。

该 Skill 是迁移期兜底能力，目标是提供可审计、低风险、可审批的恢复建议，而不是替代场景专用 Skill。

## 输入信号

推荐输入：

- `fault_type`
- `root_cause`
- `namespace`
- `deployment`
- `service`
- `diagnosis.fault_type`
- `diagnosis.root_cause`
- `diagnosis.root_cause_component`
- `allow_generic_restart`

## 执行阶段

- `heal`: 生成只读检查命令；只有显式允许时才加入滚动重启建议。

本 Skill 接收上游诊断结论并只负责恢复规划；恢复后的判定由独立验证 Skill 完成。

## 自愈策略

默认输出只读命令：

- `kubectl get pods,svc,endpoints -n <namespace> -o wide`
- `kubectl get events -n <namespace> --sort-by=.lastTimestamp | tail -40`
- `kubectl describe svc <service> -n <namespace>`
- `kubectl describe deployment/<deployment> -n <namespace>`

只有满足以下条件时，才会输出写动作：

- `allow_generic_restart=true`

写动作示例：

- `kubectl rollout restart deployment/<deployment> -n <namespace>`
- rollback: `kubectl rollout undo deployment/<deployment> -n <namespace>`

## 风险与门禁

该 Skill 具有 `k8s.write.restart` 权限，真实执行必须审批。它不应绕过风险门禁，也不应在证据不足时自动重启业务。

## 验证方式

恢复后检查：

- Pod、Service、Endpoint 状态。
- Deployment rollout 状态。
- 原始告警是否消退。
- 业务探测或 SLO 指标是否恢复。
