---
name: Prometheus 查询工具 Skill
description: Plan and execute controlled PromQL instant/range queries for thresholds, current values, peaks, and alert evidence.
---
# Prometheus 查询工具 Skill

## 适用场景

用于 Agent 或其他 Skill 需要查询 Prometheus 当前值、峰值、时间窗口、阈值是否生效、
故障前后曲线变化和告警证据时。

## 输入信号

- Prometheus URL、PromQL、时间窗口、step、阈值、比较方式。
- 当前值、峰值、告警规则、指标名。

## 执行阶段

1. `detect`：识别 PromQL、阈值、时间窗口或指标查询需求。
2. `diagnose`：通过 `prometheus` runner 执行查询；没有 runner 时生成查询计划。
3. `verify`：确认查询结果可用于告警、假设验证和前端展示。

本 Skill 不执行恢复，只输出查询事实和下一步调查建议。

## 风险与门禁

只读工具 Skill，不修改集群。大范围 range query 可能给 Prometheus 带来压力，应限制窗口和 step。

## 验证方式

- 查询返回值、时间戳、PromQL 和窗口被记录。
- 结果可解释为当前值、峰值或阈值判断。
- Agent+Skill 报告中保留查询证据。
