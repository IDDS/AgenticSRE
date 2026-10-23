---
name: vLLM 队列与调度退化
description: 检测和诊断 vLLM 请求队列溢出、连续批处理退化、Prefill/Decode 调度异常导致的推理时延问题。
---
# vLLM 队列与调度退化

## 适用场景

- vLLM `waiting_requests` 持续升高，TTFT/E2E P95 变差。
- 连续批处理、Prefill/Decode 阶段或 attention backend 被扰动。
- 请求队列堆积、调度并发过高或业务侧排队传播。

## 输入信号

- `fault_type`: `request_queue_overflow`、`continuous_batch_disruption`、`prefill_decode_misclassification`、`attention_backend_mismatch`
- vLLM 队列、运行中请求、TTFT/TPOT/E2E 指标。
- 负载生成器成功率、错误率、并发度。
- vLLM Pod、Deployment、模型名和 Prometheus URL。

## 执行阶段

- `detect`: 识别调度、队列、连续批处理和首 token 延迟信号。
- `diagnose`: 结合队列、运行中请求和时延定位调度瓶颈。
- `heal`: 生成限流、降低并发、调整调度参数或滚动重启前检查建议。
- `verify`: 验证等待队列、TTFT/E2E P95 和请求成功率恢复。

## 风险与门禁

- 风险等级 high，调整调度参数或重启 vLLM 需审批。
- 自动执行关闭；恢复建议默认 dry-run。
- 至少需要队列/时延/请求成功率中的两个证据来源。

## 验证方式

- `waiting_requests` 回落。
- TTFT/E2E P95 下降。
- 请求成功率恢复。
- vLLM Deployment rollout 正常，模型端点可响应。
