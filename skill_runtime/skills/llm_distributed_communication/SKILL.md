---
name: 分布式推理通信故障
description: 检测和诊断 NCCL、RDMA、Tensor Parallel 通信、丢包和带宽受限导致的多卡/多节点推理故障。
---
# 分布式推理通信故障

## 适用场景

- 多卡或多节点 vLLM 推理出现 NCCL/RDMA/TP 通信异常。
- Tensor Parallel 同步阻塞，请求吞吐下降或超时。
- 推理服务网络丢包、带宽限制或 gRPC 通信失败。

## 输入信号

- `nccl_failure`、`tp_communication_loss`、`rdma_failure`、`packet_loss`、`bandwidth_throttle`、`grpc_failure`
- NCCL 日志、RDMA/IB 设备状态、Pod/Node 拓扑、网络指标、请求失败率。
- 多卡数量、模型并行配置、vLLM Pod 分布。

## 执行阶段

- `detect`: 识别 NCCL/RDMA/TP/网络通信异常。
- `diagnose`: 判断通信链路、并行同步或推理入口网络是否异常。
- `heal`: 生成只读核查、降低并行压力、重启工作负载或隔离异常节点建议。
- `verify`: 验证通信错误、请求失败率和尾延迟恢复。

## 风险与门禁

- 风险等级 critical，多节点通信和节点隔离需审批。
- 不自动执行网络破坏性操作。
- 至少需要日志、网络指标或拓扑证据之一。

## 验证方式

- NCCL/RDMA 错误不再新增。
- 请求成功率恢复，TTFT/E2E P95 下降。
- Pod 分布、GPU 数量和模型并行配置符合预期。
