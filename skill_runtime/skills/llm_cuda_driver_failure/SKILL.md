---
name: CUDA 与 GPU 驱动故障
description: 检测和诊断 CUDA runtime/context、GPU Xid、时钟降频、PCIe 退化和 Profiler/CUPTI 开销导致的推理异常。
---
# CUDA 与 GPU 驱动故障

## 适用场景

- vLLM 日志出现 CUDA runtime、CUDA context、kernel launch 或 GPU Xid 异常。
- GPU 时钟、功耗、PCIe 或驱动状态导致 Decode/Kernel 性能退化。
- CUPTI/Profiler 开销过高，Trace 采集影响推理。

## 输入信号

- `cuda_error`、`cuda_ctx_corruption`、`gpu_clock_throttle`、`pcie_throttle`、`cupti_trace_overhead`
- DCGM/NVIDIA-SMI 指标、GPU Xid 日志、TELLER/CUPTI Trace、vLLM 错误日志。
- Pod、节点、GPU UUID、模型和 Prometheus URL。

## 执行阶段

- `detect`: 识别 CUDA/驱动/PCIe/Profiler 相关异常信号。
- `diagnose`: 判断是 CUDA 上下文、驱动性能、PCIe 还是采集开销问题。
- `heal`: 输出节点隔离、重启工作负载、关闭高开销 profiler、驱动检查等受控建议。
- `verify`: 验证 GPU 错误消失、kernel P95 和请求时延恢复。

## 风险与门禁

- 风险等级 critical，可能涉及节点维护、驱动或 GPU 状态。
- 不自动执行节点级修复；必须审批并确认业务影响。
- 至少需要日志、GPU 指标或 Trace 证据之一。

## 验证方式

- vLLM 日志无新增 CUDA/Xid 错误。
- GPU util、显存、时钟和 PCIe 指标正常。
- TELLER kernel P95 和请求 E2E P95 恢复。
