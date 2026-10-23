---
name: Ingress Gateway 流量入口诊断
description: Diagnose Kubernetes Ingress, Gateway, LoadBalancer, route, upstream, and 4xx/5xx traffic entry failures.
---
# Ingress Gateway 流量入口诊断

## 适用场景

用于通算或智算平台入口流量异常：Ingress/Gateway 返回 404、502、503、504，
LoadBalancer 无地址，路由规则不匹配，入口转发到错误 Service，或网关上游超时。

## 输入信号

- Ingress、Gateway、HTTPRoute、Service、Endpoint、LoadBalancer 状态。
- HTTP 状态码、Host、Path、TLS Secret、网关日志和 Prometheus 指标。
- 目标 namespace、service、deployment、ingress 或 gateway 名称。

## 执行阶段

1. `detect`：识别入口流量、路由、网关和 LB 异常信号。
2. `diagnose`：区分路由规则、后端 endpoint、网关上游、LB 地址或 TLS 引用问题。
3. `heal`：默认生成只读核查和审批后的修复建议，不自动改网关规则。
4. `verify`：验证入口地址、路由命中、后端 endpoint 和 HTTP 探测恢复。

## 风险与门禁

修改 Ingress/Gateway/HTTPRoute 会影响外部流量，必须审批。默认只执行只读调查。

## 验证方式

- Ingress/Gateway 地址存在且 Ready。
- Host/Path 路由到正确 Service/Port。
- 后端 endpoints 非空，HTTP 探测返回预期状态。
