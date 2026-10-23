---
name: 多卡并行拓扑一致性诊断
description: Diagnose vLLM tensor/pipeline parallel topology, world size, GPU count, model shard, and multi-card placement mismatches.
---
# 多卡并行拓扑一致性诊断

## 适用场景

用于智算集群多卡推理中 tensor parallel、pipeline parallel、world size、GPU 数量、
模型 shard、NCCL rank、Pod placement 或多卡拓扑不一致导致 vLLM 启动失败、请求卡住、
吞吐异常或部分 GPU 空闲。

## 输入信号

- `tensor_parallel_size`、`pipeline_parallel_size`、`world_size`、`gpu_count`、`replica_count`。
- vLLM 启动参数、环境变量、NCCL rank、Pod 调度节点、GPU 拓扑。
- 日志中的 rank mismatch、world size mismatch、model shard missing、NCCL init。

## 执行阶段

1. `detect`：识别多卡并行、TP/PP/world size、rank 和 shard 异常信号。
2. `diagnose`：判断 GPU 数量、并行参数、Pod placement、模型分片和通信拓扑是否一致。
3. `heal`：输出只读核查和审批后的并行参数修正建议。
4. `verify`：验证 GPU 全部参与推理、rank 正常、请求吞吐和延迟恢复。

## 风险与门禁

修改 vLLM 并行参数、模型副本数或 Pod 调度策略会重启推理服务，必须审批。

## 验证方式

- vLLM 启动日志中 rank/world size 正常。
- GPU 利用率按预期分布，多卡没有长期空闲或单卡过载。
- 负载请求成功，TTFT/E2E P95 回到基线。
