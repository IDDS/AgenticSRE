---
name: 模型资产与 Tokenizer 故障
description: 检测和诊断模型加载失败、模型权限错误、Tokenizer 异常、权重损坏等模型资产类故障。
---
# 模型资产与 Tokenizer 故障

## 适用场景

- vLLM Pod 启动或重建时模型加载失败。
- 模型目录权限、路径、权重文件或 Tokenizer 异常。
- 请求进入推理前因模型名、Tokenizer 或预处理资产失败。

## 输入信号

- `model_load_failure`、`model_permission_error`、`tokenizer_error`、`weight_corruption`
- Pod 日志、K8S Event、模型路径、模型名、镜像和挂载信息。
- vLLM 服务重建失败、Image/PVC/权限相关证据。

## 执行阶段

- `detect`: 识别模型资产、Tokenizer、权重和权限异常。
- `diagnose`: 区分路径不存在、权限错误、Tokenizer 错误和权重损坏。
- `heal`: 生成只读核查、回滚模型版本、修复挂载/权限前检查建议。
- `verify`: 验证模型加载、服务健康和一次最小推理请求。

## 风险与门禁

- 风险等级 high，模型版本切换、权限修改和重启需审批。
- 不自动修改模型文件或权限。
- 至少需要日志/Event/模型路径三个维度之一作为证据。

## 验证方式

- vLLM 日志不再出现模型加载/Tokenizer 错误。
- Pod Ready，模型端点可用。
- 用目标模型执行最小推理请求成功。
