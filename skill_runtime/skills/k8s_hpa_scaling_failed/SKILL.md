---
name: K8s HPA 扩缩容失败
description: 检测、诊断 Kubernetes HPA 扩缩容和指标采集失败，并生成受控恢复建议。
---
# K8s HPA 扩缩容失败诊断与守护式修复

本 Skill 用于检测、诊断并提出受控修复方案，处理 HPA 扩缩容与指标采集失败问题。

## 适用场景

- HPA 无法获取指标，显示 `unknown` 或 `FailedGetResourceMetric`。
- metrics-server 不可用或返回异常导致扩缩容停滞。
- HPA 达到 `maxReplicas` 仍无法满足负载，或未按预期扩容。
- 目标工作负载缺少 resource requests 导致利用率无法计算。

## 输入信号

- HPA 的 `TARGETS`、`REPLICAS` 与状态。
- `kubectl describe hpa` 中的条件与事件。
- `kubectl top pods` 与 metrics-server 状态。
- 目标工作负载的 resources 配置。

## 执行阶段

- `detect`: 识别 HPA 未按预期扩缩容或指标缺失。
- `diagnose`: 区分指标链路故障、配置问题与容量上限。
- `heal`: 仅生成 dry-run 或建议命令，例如修复 metrics-server、调整 HPA 上限或 requests。
- `verify`: 批准执行后确认指标恢复采集且扩缩容生效。

## 风险与门禁

- 风险等级 medium，扩缩容边界与指标组件变更影响容量与成本，必须审批。
- `requires_approval=true`，`safe_auto_execute=false`。
- 至少 2 个独立证据来源方可进入修复建议。
- 进入自愈门禁所需的最低校准诊断置信度为 0.82，全自动执行门槛为 0.95；两者均不表示恢复成功概率。

## 验证方式

- HPA `TARGETS` 恢复为具体百分比而非 `unknown`。
- `ScalingActive` 条件为 True，扩缩容事件按负载正常触发。
- `kubectl top pods` 可正常返回指标。
