---
name: GPU 分区与隔离诊断
description: Diagnose MIG, vGPU, MPS, time-slicing, GPU sharing, and isolation-related scheduling or performance faults.
---
# GPU 分区与隔离诊断

## 适用场景

用于 MIG/vGPU/MPS/time-slicing/GPU sharing 场景中切片资源不可见、隔离失败、Pod 争抢、
MIG profile 不匹配、单卡多租户干扰、显存/SM 共享导致性能抖动。

## 输入信号

- MIG profile、GPU slice、MPS、time-slicing、device plugin config、resource name。
- Pod request/limit、node label、RuntimeClass、GPU utilization skew、QoS。
- 业务延迟、KV Cache、显存和并发请求变化。

## 执行阶段

1. `detect`：识别 MIG/vGPU/MPS/GPU sharing 和隔离异常信号。
2. `diagnose`：判断是资源名/profile 不匹配、切片未暴露、共享干扰还是 QoS 配置问题。
3. `heal`：输出只读核查和审批后的 profile/runtime 配置建议。
4. `verify`：验证切片资源、Pod 调度、隔离和业务性能恢复。

## 风险与门禁

修改 MIG profile、device plugin sharing 配置或重启 GPU 节点风险高，必须审批。

## 验证方式

- 节点 allocatable 暴露预期 MIG/vGPU/MPS 资源。
- Pod 调度到正确切片，容器内 GPU 可见。
- 多租户压测下性能抖动下降。
