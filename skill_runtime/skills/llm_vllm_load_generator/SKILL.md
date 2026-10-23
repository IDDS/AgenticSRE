---
name: vLLM 负载生成器诊断
description: Diagnose load generator readiness, traffic absence, concurrency settings, and fault-effect observability for vLLM experiments.
---
# vLLM 负载生成器诊断

## 适用场景

用于智算实验中没有背景流量、前端指标不变化、请求成功率为 0、并发压测没有进入
vLLM、负载生成器参数不合理或连续实验缺少稳定请求源等问题。

## 输入信号

- loadgen 请求数、成功率、错误数、并发、QPS、模型名和 endpoint。
- vLLM running/waiting requests、TTFT/E2E、HTTP 状态码。
- 实验窗口、故障注入时间和前端观测曲线。

## 执行阶段

1. `detect`：识别 loadgen 流量缺失、请求失败或压测参数异常。
2. `diagnose`：判断是 endpoint/model 配置、并发不足、网络不可达还是服务不可用。
3. `verify`：验证请求进入 vLLM、曲线产生变化、故障前后有可比性。

本 Skill 不执行服务恢复；负载生成参数建议只用于调查和验证。

## 风险与门禁

压测会消耗 GPU 和业务资源。真实执行需限制 QPS、并发、持续时间和目标集群，
避免影响 SSH、控制面或生产业务。

## 验证方式

- vLLM 指标中 running/waiting/success rate 有变化。
- 前端观测页能看到注入前、注入中、恢复后的曲线差异。
- 连续实验报告中保留压测参数、窗口和结果。
