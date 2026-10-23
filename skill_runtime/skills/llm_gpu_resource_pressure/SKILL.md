---
name: 智算 GPU 资源压力
description: 检测、诊断并生成受控恢复建议，用于 GPU OOM、KV Cache 耗尽、显存高水位和 CUDA 资源压力。
---
# 智算 GPU 资源压力

## 适用场景

用于诊断 GPU OOM、KV Cache 耗尽、显存高水位、CUDA 资源压力和推理请求因 GPU 资源不足导致的性能退化。

该 Skill 适合从智算事件中心、智算 Dashboard 或 GPU Lab 故障实验触发。

## 输入信号

推荐输入：

- `fault_type`
- `message`
- `gpu_memory_usage`
- `gpu_memory_used_ratio`
- `kv_cache_usage`
- `waiting_requests`
- `running_requests`
- `request_failures`
- `namespace`
- `deployment`
- `prometheus_url`

也可以通过 `metrics` 字段传入 Prometheus 查询结果。

## 执行阶段

- `detect`: 识别 GPU OOM、KV Cache、CUDA OOM、显存压力等信号。
- `diagnose`: 基于 GPU 显存、KV Cache、队列和请求失败判断资源瓶颈。
- `heal`: 生成只读指标查询、降低负载、重启或扩容前检查建议。
- `verify`: 验证 GPU 显存、KV Cache 和请求队列是否恢复。

## 风险与门禁

该 Skill 风险为高，可能建议重启 vLLM 或调整工作负载。真实执行必须审批，且应先确认当前请求负载和模型服务影响范围。

进入自愈门禁所需的最低校准诊断置信度为 `0.82`，全自动执行门槛为 `0.95`；两者均不表示恢复成功概率，且当前策略不允许自动执行。

## 验证方式

恢复后检查：

- GPU 显存水位下降。
- KV Cache 使用率回落。
- 等待队列下降。
- 请求成功率恢复。
- TTFT/TPOT/E2E 延迟恢复。
