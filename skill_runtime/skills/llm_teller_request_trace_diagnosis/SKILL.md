---
name: TELLER 请求级 Trace 诊断
description: 基于 TELLER 请求级调用路径、CUDA Runtime/Driver/Kernel 事件和 vLLM 请求上下文诊断智算推理瓶颈。
---
# TELLER 请求级 Trace 诊断

## 适用场景

用于智算集群中 vLLM 推理服务的请求级细粒度诊断。该 Skill 消费 TELLER 输出的请求窗口、CUDA Runtime、Driver、Kernel 和 Top Kernel 统计，用于判断慢请求是否来自请求编排、CUDA Runtime 调度、Kernel 执行或观测链路缺失。

它适合处理以下事件：

- `teller_request_latency_high`
- `teller_kernel_bottleneck`
- `teller_request_context_missing`
- vLLM 请求级 trace 已采集但 Dashboard 显示请求 span 或 Kernel P95 异常

## 输入信号

推荐输入字段：

- `fine_grained_trace.summary`
- `fine_grained_trace.request_context`
- `fine_grained_trace.top_kernels`
- `fine_grained_trace.event_type_stats`
- `namespace`
- `deployment`
- `service`
- `model`
- `prometheus_url`
- `vllm_endpoint`
- `evidence`
- `thresholds.teller_request_span_ms`
- `thresholds.teller_kernel_p95_ms`

其中 `request_context.requests[]` 应包含：

- `request_id`
- `span_ms`
- `event_count`
- `kernel_events`
- `runtime_events`
- `driver_events`
- `path_events`

注意：`teller_request_span_ms`、`teller_kernel_p95_ms` 如果出现在 payload 顶层，表示观测值，不表示阈值。阈值必须放在 `thresholds.*` 中；请求 span 事件也可从 `metrics.threshold` 读取阈值。

## 执行阶段

- `detect`: 判断 payload 是否包含 TELLER trace、request context、Kernel/Runtime 统计或 TELLER 事件文本。
- `diagnose`: 根据请求 span、Kernel P95、Runtime/Kernel 比例、Top Kernel 生成中文根因。

本 Skill 只负责请求级信号识别和根因诊断；恢复与恢复验证由下游 Skill 接管。

## 诊断分类

可能输出：

- `LLM::teller_request_context_missing`: 已有细粒度事件但缺少 request_id，无法归因到单个请求。
- `LLM::teller_runtime_overhead`: 请求 span 高但 Kernel P95 不高，倾向 Runtime 调度、同步或请求编排瓶颈。
- `LLM::teller_kernel_bottleneck`: Kernel P95 超阈值，倾向算子执行、GPU 调度或资源竞争瓶颈。
- `LLM::teller_request_path_latency`: 请求级执行路径整体变慢，需要沿 prefill/decode、Runtime、Driver 和 Kernel 阶段排查。
- `LLM::teller_trace_observed`: trace 可用但未达到内置异常阈值，作为辅助证据。

## 输出契约

诊断输出包含：

- `fault_type`
- `root_cause`
- `root_cause_component`
- `evidence`
- `confidence`
- `confidence_explanation`
- `trace_diagnosis`

`trace_diagnosis` 会保留请求上下文摘要、最慢请求、Top Kernel、事件类型统计和 trace summary，供前端展示完整 Skill 工作流。

## 风险与门禁

该 Skill 是只读诊断 Skill，不执行重启、扩缩容或网络变更。它可以自动参与分析，但恢复动作必须路由到 `llm.k8s_vllm_observability`、`llm.gpu_resource_pressure` 或其他具备审批门禁的恢复 Skill。

## 验证方式

- 诊断结论必须引用具体请求、Kernel 或事件统计，不能只复述告警文本。
- 请求 span 和 Kernel P95 必须区分观测值与 `thresholds.*` 中的阈值。
- 缺少 request_id 或事件覆盖不足时，应降低置信度并明确指出证据缺口。
- 恢复效果由下游验证 Skill 判断，不在本 Skill 中执行 `verify` 阶段。
