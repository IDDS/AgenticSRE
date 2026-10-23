---
name: 模型服务入口诊断
description: Diagnose OpenAI-compatible vLLM endpoint, model name, request parameter, and HTTP status failures.
---
# 模型服务入口诊断

## 适用场景

用于 vLLM/OpenAI 兼容接口返回 400、404、503、model not found、模型名不匹配、
`/v1/completions` 或 `/v1/chat/completions` 参数错误、前端负载生成无法请求模型等问题。

## 输入信号

- endpoint、model、HTTP 状态码、响应体、请求路径和请求参数。
- vLLM 日志、已加载模型列表、Service/Ingress/NodePort。
- 负载生成器错误和前端配置。

## 执行阶段

1. `detect`：识别模型服务入口、模型名和 API 参数异常。
2. `diagnose`：区分 endpoint 不通、模型未加载、路径错误和请求参数不兼容。
3. `heal`：输出低风险探活和配置修正建议，真实变更需审批。
4. `verify`：验证 `/v1/models` 和一次小 token 请求成功。

## 风险与门禁

修改模型名、endpoint、NodePort、Ingress 或 Deployment 参数会影响实验入口，
需要审批并保留原配置。

## 验证方式

- `/v1/models` 返回目标模型。
- 小 token 请求返回 2xx 且被 Prometheus/vLLM 指标记录。
- 前端负载生成器配置与服务端模型名一致。
