---
name: K8s 内存溢出终止
description: 检测、诊断因 OOMKilled 或退出码 137 终止的 Kubernetes 容器，并生成受控恢复建议。
---
# K8s OOMKilled 诊断与守护式修复

本 Skill 用于处理容器因 OOMKilled 或退出码 137 被终止的内存问题。

## 适用场景

- 容器状态显示 `OOMKilled` 或 `Last State` 退出码为 137。
- Pod 因内存超限反复重启，`restartCount` 持续增长。
- 容器内存使用逼近或超过 `resources.limits.memory`。
- 节点内存紧张触发对 Pod 的 OOM 驱逐。

## 输入信号

- `kubectl describe pod` 中的 `Last State: Terminated (OOMKilled)` 与退出码。
- 容器内存指标与 limits/requests 配置对比。
- Pod 的 `restartCount` 与重启时间分布。
- 节点 `MemoryPressure` Condition 与内核 OOM 日志。

## 执行阶段

- `detect`: 识别 OOMKilled/137，确认为内存类故障。
- `diagnose`: 结合内存趋势、limits 配置与负载判断是配置过低、内存泄漏还是负载激增。
- `heal`: 仅生成 dry-run 或建议命令，例如调整内存 limits、滚动重启、建议排查泄漏，不自行执行。
- `verify`: 审批执行后确认容器不再 OOM、内存使用稳定。

## 风险与门禁

- 风险等级 medium，重启与配置调整会影响服务，必须人工审批。
- `requires_approval=true`，`safe_auto_execute=false`。
- 至少需 2 个独立证据来源方可进入修复建议。
- 进入自愈门禁所需的最低校准诊断置信度为 0.82，全自动执行门槛为 0.95；两者均不表示恢复成功概率。

## 验证方式

- `kubectl describe pod` 不再出现新的 OOMKilled 事件。
- 容器内存使用稳定在 limits 以下，`restartCount` 不再增长。
- 服务恢复可用，无内核 OOM 日志新增。
