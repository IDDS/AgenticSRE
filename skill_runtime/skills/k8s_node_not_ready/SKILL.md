---
name: K8s 节点 NotReady
description: 诊断 NodeNotReady 和节点条件异常，并生成受控 cordon 等处置建议。
---
# K8s 节点 NotReady 诊断与守护式处置

本 Skill 用于诊断 NodeNotReady 及节点 Condition 异常，并给出受控的 cordon/drain 建议。

## 适用场景

- 节点状态长期处于 `NotReady` 或 `Unknown`。
- kubelet 心跳丢失、节点 Lease 过期。
- 节点出现 `MemoryPressure`、`DiskPressure`、`PIDPressure`、`NetworkUnavailable` 等 Condition。
- 节点上 Pod 被驱逐或长时间无法调度。

## 输入信号

- `kubectl get nodes` 中节点的 `STATUS` 与 `AGE`。
- `kubectl describe node` 的 Conditions、Events 与最近心跳时间。
- kubelet、容器运行时日志与 systemd 状态。
- 节点 Lease 更新时间与网络、磁盘、内存资源指标。

## 执行阶段

- `detect`: 确认节点 NotReady/Unknown 并识别触发的 Condition。
- `diagnose`: 结合 kubelet 状态、租约、运行时与资源指标定位根因。
- `heal`: 仅生成 dry-run 或建议命令，例如 `kubectl cordon`、`kubectl drain --dry-run`、kubelet 重启建议，不自行执行。
- `verify`: 审批执行后确认节点恢复 Ready、Condition 清除、Pod 正常调度。

## 风险与门禁

- 风险等级 high，cordon/drain 会影响节点上所有工作负载，必须人工审批。
- `requires_approval=true`，`safe_auto_execute=false`。
- 至少需 2 个独立证据来源方可进入修复建议。
- 进入自愈门禁所需的最低校准诊断置信度为 0.88，全自动执行门槛为 0.95；两者均不表示恢复成功概率。

## 验证方式

- `kubectl get node <name>` 恢复为 `Ready`。
- describe 中异常 Condition 消失，心跳或租约恢复更新。
- 节点上关键 Pod 重新进入 Running，无持续驱逐事件。
- kubelet 与运行时服务状态正常且无新增错误日志。
