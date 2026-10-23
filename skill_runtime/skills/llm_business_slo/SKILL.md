---
name: 智算业务 SLO
description: 基于成功率、TTFT、TPOT、端到端时延、队列、KV Cache 和 GPU 指标诊断 vLLM 业务影响。
---
# 智算业务 SLO

## 适用场景

用于诊断 vLLM 推理服务的业务影响，包括成功率下降、TTFT P95 升高、TPOT P95 升高、端到端时延升高、等待队列堆积、KV Cache 或 GPU 显存压力。

该 Skill 主要用于智算事件中心、智算 Dashboard 和连续故障实验后的观测分析。它不直接重启服务，而是输出观测、验证和下一步路由建议。

## 输入信号

推荐输入 `metrics` 或同名顶层字段：

- `success_rate` 或 `request_success_rate`
- `ttft_p95_seconds`
- `tpot_p95_seconds`
- `e2e_p95_seconds`
- `waiting_requests`
- `running_requests`
- `kv_cache_usage`
- `gpu_utilization`
- `gpu_memory_usage`
- `prometheus_url`
- `endpoint`
- `model`

## 执行阶段

- `detect`: 判断输入是否包含 vLLM、推理、TTFT、TPOT、KV Cache 或 GPU 指标。
- `diagnose`: 按成功率、队列、TTFT、TPOT、E2E、KV Cache 和 GPU 压力分类业务影响。
- `verify`: 验证请求成功率、延迟分位、等待队列和 KV Cache 是否恢复。

本 Skill 不执行恢复；只读观测和小请求用于诊断或恢复验证，变更动作交由下游恢复 Skill。

## 诊断分类

可能输出的故障类型包括：

- `LLM::business_success_rate_drop`
- `LLM::queue_latency_propagation`
- `LLM::kv_cache_or_gpu_memory_pressure`
- `LLM::ttft_regression`
- `LLM::tpot_regression`
- `LLM::e2e_latency_regression`
- `LLM::business_slo_observed`

## 风险与门禁

该 Skill 是低风险只读能力，权限为 `prometheus.read` 和 `observability.read`。它可以参与自动分析，但不应单独执行重启、扩缩容或模型服务变更。

如果诊断显示需要恢复动作，应路由到 `llm.k8s_vllm_observability`、`llm.vllm_process_crash` 或其他专用恢复 Skill。

## 验证方式

前端应在智算 Dashboard 中展示：

- 当前值和峰值。
- 时间窗口内曲线。
- 请求成功率和延迟分位。
- 队列、KV Cache、GPU 使用率。

故障恢复后，成功率应回升，TTFT/TPOT/E2E P95 和等待队列应回落。
