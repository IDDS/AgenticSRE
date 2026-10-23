---
name: 模型仓库与对象存储访问诊断
description: Diagnose model repository, object storage, HuggingFace token, cache, download, checksum, and shard fetch failures.
---
# 模型仓库与对象存储访问诊断

## 适用场景

用于模型下载失败、对象存储不可达、HF token 权限不足、模型 cache 损坏、分片下载超时、
checksum mismatch、镜像缓存缺失、冷启动加载慢等模型供应链问题。

## 输入信号

- 模型仓库 URL、bucket/path、token/secret、HTTP 状态码、下载耗时、checksum。
- vLLM 启动日志、init container、PVC cache、对象存储 endpoint。
- 模型名、分片数量、缓存路径、节点磁盘/网络指标。

## 执行阶段

1. `detect`：识别模型仓库、对象存储、token、下载和 cache 异常。
2. `diagnose`：区分权限、网络、缓存损坏、对象不存在、checksum 和冷启动慢。
3. `heal`：输出只读核查、缓存重建、Secret 修复和预热建议。
4. `verify`：验证模型文件完整、cache 命中和 vLLM 模型加载成功。

## 风险与门禁

删除模型 cache、替换 Secret、预热大模型会消耗磁盘/网络/GPU 资源，必须审批。

## 验证方式

- 模型分片完整且 checksum 正确。
- `/v1/models` 返回目标模型。
- 模型加载时间和请求延迟恢复。
