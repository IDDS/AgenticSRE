---
name: 智算拓扑感知调度诊断
description: Diagnose topology-aware scheduling, NUMA, PCIe/NVLink locality, cross-node placement, RDMA locality, and anti-affinity faults.
---
# 智算拓扑感知调度诊断

## 适用场景

用于多卡/多节点推理中 Pod 被调度到不合适节点、GPU/NIC/NUMA/PCIe/NVLink 拓扑不匹配、
Tensor Parallel 跨节点、RDMA locality 差、亲和/反亲和/拓扑 spread 配置导致通信延迟升高。

## 输入信号

- Pod placement、node label、topology key、GPU/NIC locality、NUMA、PCIe/NVLink 拓扑。
- RDMA/NCCL latency、cross-node traffic、TP/PP/world size、Pod anti-affinity。
- 调度事件、node selector、affinity、topology spread constraints。

## 执行阶段

1. `detect`：识别拓扑感知调度、NUMA/NVLink/RDMA locality 和跨节点并行异常。
2. `diagnose`：判断是调度约束缺失、拓扑标签错误、跨节点通信或 GPU/NIC locality 问题。
3. `heal`：输出只读核查和审批后的调度策略建议。
4. `verify`：验证 Pod placement、通信延迟和业务 P95 恢复。

## 风险与门禁

修改 affinity、nodeSelector、topology spread 或迁移大模型 Pod 会重启/迁移服务，必须审批。

## 验证方式

- Pod 落在预期节点/GPU/NIC locality。
- NCCL/RDMA latency 回落，GPU 间通信正常。
- vLLM TTFT/E2E P95 恢复。
