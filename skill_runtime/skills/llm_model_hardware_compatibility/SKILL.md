---
name: 模型与硬件兼容性诊断
description: Diagnose model, dtype, quantization, CUDA architecture, attention backend, and GPU memory compatibility for vLLM.
---
# 模型与硬件兼容性诊断

## 适用场景

用于模型无法在当前 GPU 上启动或性能异常：T4/老架构不支持某些 dtype、attention backend、
quantization kernel、CUDA arch、FlashAttention、BF16，或模型参数量超过显存能力。

## 输入信号

- 模型名、参数规模、dtype、quantization、attention backend、GPU 型号、compute capability。
- vLLM 日志中的 unsupported dtype/backend/kernel、CUDA arch mismatch、OOM。
- 用户选择 Gemma/Qwen 等模型部署到 T4 或多卡集群时的失败信息。

## 执行阶段

1. `detect`：识别模型与硬件、dtype、量化和 backend 兼容性信号。
2. `diagnose`：判断是显存不足、架构不支持、dtype/backend 不兼容还是模型资产问题。
3. `heal`：输出兼容模型、dtype、量化和 backend 调整建议。
4. `verify`：验证模型加载、一次小 token 请求和 GPU 指标。

## 风险与门禁

切换模型、镜像、dtype 或量化格式会改变实验基线，必须记录配置并审批。

## 验证方式

- vLLM 成功加载目标模型。
- `/v1/models` 返回模型，短请求成功。
- GPU 显存和 TTFT/E2E 在可接受范围。
