---
name: K8s vLLM 可观测诊断
description: 检测、诊断 Kubernetes 上 vLLM 推理服务不可用和时延异常，并生成受控恢复建议。
---
# K8s vLLM 可观测诊断

## 适用场景

用于 Kubernetes 上部署的 vLLM 推理服务诊断，包括 Deployment 不可用、Service Endpoint 异常、Prometheus TargetDown、请求失败、TTFT/E2E 延迟异常和 vLLM 指标缺失。

该 Skill 是智算 K8S 集群闭环诊断和自愈的主 Skill 之一。

不要用于以下场景：

- 裸机 GPU 主机没有 K8s 工作负载上下文，应使用 `tool.llm_host_readiness`。
- 纯模型质量、回答错误或 prompt 问题，不属于运行时可观测故障。
- CUDA/NCCL 低层错误证据明确时，应与 GPU 资源压力或进程崩溃 Skill 联合使用。

## 触发方式

- **智算事件中心**: vLLM 指标、GPU 指标、推理延迟和事件检测后触发。
- **智算故障实验**: vLLM 进程崩溃、Endpoint 超时、指标缺失、GPU 压力等注入后触发。
- **告警中心**: Prometheus 产生 TargetDown、延迟或请求失败告警时由 Skill Runtime 直接编排调用。
- **Agent 工具调用**: 当前尚未接入 Agent 自主调用；未来 Agent 应在看到 vLLM/K8s/GPU 证据时选择该 Skill，并把 trace 写回 RCA 过程。

## 输入信号

推荐输入：

- `namespace`
- `deployment`
- `service`
- `pod`
- `endpoint_status`
- `metrics_status`
- `deployment_ready_replicas`
- `deployment_desired_replicas`
- `request_failures`
- `ttft_p95_seconds`
- `e2e_p95_seconds`
- `waiting_requests`
- `running_requests`
- `gpu_count`
- `metrics_count`
- `prometheus_url`
- `vllm_endpoint`
- `model_name`
- `pod_phase`
- `container_restarts`
- `gpu_utilization`
- `gpu_memory_used_ratio`
- `target_health`
- `fine_grained_trace_summary`

## 执行阶段

- `detect`: 判断是否存在 K8S vLLM 上下文和推理/指标异常。
- `diagnose`: 区分 Pod 不可用、Service Endpoint 异常、请求失败、延迟异常和指标抓取失败。
- `heal`: 生成受控恢复建议，包括只读检查、Service/Endpoint 检查、rollout 或 scale。
- `verify`: 生成 rollout、vLLM health、Prometheus 查询和业务小请求验证。

阶段跳过规则：

- 无 `namespace` 且无 vLLM/K8s 关键词时不命中。
- 只有 GPU 压力但没有 vLLM 服务异常时，降级为观测建议，不生成写操作。
- Prometheus 不可达时仍可基于 K8s 对象诊断，但必须降低置信度。

## 诊断分类

可能输出：

- `LLM::k8s_vllm_service_endpoints_empty`
- `LLM::k8s_vllm_unavailable`
- `LLM::k8s_vllm_request_failures`
- `LLM::k8s_vllm_high_latency`
- `LLM::k8s_vllm_metrics_target_down`
- `LLM::k8s_vllm_observed`

## 输出契约

诊断输出应包含：

- `fault_type`: 上述诊断分类之一。
- `root_cause`: 中文根因摘要。
- `root_cause_component`: Deployment、Service、Pod 或 Prometheus target。
- `evidence`: 至少两类证据，如 K8s 状态、vLLM 指标、业务请求、GPU 指标、细粒度 trace。
- `confidence`: 根据证据数量、指标一致性和门禁计算。

自愈建议应包含：

- 只读检查：Deployment、Pod、Service、Endpoint、Prometheus target。
- 低风险动作：重新采集指标、发送健康检查请求。
- 中高风险动作：rollout restart、scale，仅 dry-run 展示，真实执行需要审批。

## 示例 payload

```json
{
  "domain": "llm",
  "namespace": "llm-k8s",
  "deployment": "vllm-server",
  "service": "vllm-server",
  "fault_type": "endpoint_timeout",
  "message": "vLLM endpoint timeout, TTFT P95 increased and Prometheus target unhealthy",
  "prometheus_url": "http://127.0.0.1:19090",
  "vllm_endpoint": "http://127.0.0.1:18000/v1/models",
  "ttft_p95_seconds": 4.2,
  "e2e_p95_seconds": 12.8,
  "waiting_requests": 8,
  "gpu_count": 6
}
```

## 风险与门禁

该 Skill 具有 `k8s.write.restart` 权限，风险为高。恢复动作必须审批，默认只生成 dry-run 或建议。

需要至少两个证据源；进入自愈门禁所需的最低校准诊断置信度为 `0.82`，全自动执行门槛为 `0.95`，两者均不表示恢复成功概率。

真实执行前必须确认：

- 当前 vLLM 服务确实不可用或 SLA 已明显退化。
- 目标 Deployment/Pod 来自 K8s API 或事件上下文，不允许猜测。
- 重启不会中断仍在处理的重要推理任务，或已有业务侧确认。
- 已保留故障前后指标和必要 trace，便于复盘。

## 验证方式

恢复后验证：

- Deployment Ready/Desired 一致。
- Service Endpoints 非空。
- vLLM 健康接口和模型接口可访问。
- Prometheus target healthy。
- 业务请求成功率、TTFT、E2E P95 回到阈值内。
- GPU 利用率、显存、等待队列和 running requests 不再异常。
- 如果接入 TELLER 细粒度 trace，关键 kernel/runtime 阶段耗时应回落，且请求级路径完整。
