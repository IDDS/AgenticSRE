---
name: vLLM 多副本流量不均诊断
description: Diagnose vLLM multi-replica or multi-model traffic skew, hot replica, endpoint imbalance, and uneven GPU utilization.
---
# vLLM 多副本流量不均诊断

## 适用场景

用于多副本或多模型服务中某个 vLLM Pod/GPU 过热、其他副本空闲、请求分布不均、
Service endpoints 异常、sticky session 或负载均衡策略导致尾延迟升高。

## 输入信号

- 每个 Pod/GPU 的 QPS、running/waiting requests、TTFT/E2E、GPU 利用率。
- Service endpoints、Pod Ready、模型名、replica_count、load balancer 策略。
- 前端拓扑图中的服务、Pod、模型和 GPU 依赖关系。

## 执行阶段

1. `detect`：识别流量倾斜、热点副本、GPU 利用率不均和 endpoint 异常。
2. `diagnose`：区分 Service/endpoint、Pod Ready、模型副本不一致、sticky session 和负载生成器偏置。
3. `heal`：输出只读核查、重建 endpoint、调整副本和负载生成策略建议。
4. `verify`：验证多副本流量和 GPU 利用率趋于均衡。

## 风险与门禁

扩缩容、重启副本或调整负载均衡策略会影响在线推理服务，必须审批。

## 验证方式

- 每个 Ready 副本都有请求和 GPU 活动。
- 单副本 QPS/等待队列/TTFT 不再明显偏离。
- 业务成功率和 P95 恢复。
