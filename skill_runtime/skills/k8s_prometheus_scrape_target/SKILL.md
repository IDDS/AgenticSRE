---
name: Prometheus 抓取目标诊断
description: Diagnose Kubernetes Prometheus scrape target, ServiceMonitor, endpoint, and metrics endpoint failures.
---
# Prometheus 抓取目标诊断

## 适用场景

用于通算或智算集群中的 `TargetDown`、ServiceMonitor selector 不匹配、metrics
endpoint 为空、`metricsBindAddress` 绑定 127.0.0.1、Prometheus 无法抓取目标等问题。

## 输入信号

- Prometheus alert、target、job、instance、scrape error。
- Service、EndpointSlice、Pod label、ServiceMonitor/PodMonitor 配置。
- 目标端口、metrics path、namespace。

## 执行阶段

1. `detect`：识别 Prometheus target/scrape/metrics 相关异常。
2. `diagnose`：判断是 selector、endpoint、端口、path、网络策略还是目标进程问题。
3. `heal`：输出只读核查、修正 ServiceMonitor 或目标配置建议。
4. `verify`：验证 target up 和目标指标恢复。

## 风险与门禁

默认只读。修改 ServiceMonitor、Service label 或组件启动参数需要审批。

## 验证方式

- Prometheus target 为 `up`。
- `/metrics` 可访问。
- 告警消除，相关 dashboard 曲线恢复。
