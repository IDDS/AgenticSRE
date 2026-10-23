---
name: LLM 主机就绪状态采集工具
description: 面向裸机 GPU 和 vLLM 主机的只读就绪状态检查工具 Skill。
---
# LLM 主机就绪检查工具

本 Skill 是只读的裸金属 GPU 与 vLLM 主机就绪检查工具，用于巡检 GPU 资源与推理服务健康状态。

## 适用场景

- vLLM 推理服务无响应、延迟升高或吞吐下降。
- GPU 显存占满、利用率异常或掉卡。
- 推理进程退出或健康端点不可达。
- 需要在裸金属主机上定位 GPU 与服务级问题。

## 输入信号

- 主机地址、GPU 编号与 vLLM 服务端口。
- `nvidia-smi` 输出的显存、利用率与温度。
- 推理进程状态与健康检查端点。
- vLLM 指标，例如队列长度、TTFT、吞吐。

## 执行阶段

- `detect`: 通过 SSH 远程只读执行 `nvidia-smi`、`pgrep` 与 `curl` 采集 GPU 与服务状态。
- `diagnose`: 分析显存饱和、掉卡、进程缺失与端点不可达的原因。
- `verify`: 恢复后只读复查 GPU 与 vLLM 指标是否回归正常。

本 Skill 不重启进程、不重置 GPU，也不修改主机配置。

## 风险与门禁

- 风险等级 low，仅具备 `host.read` 只读权限。
- `requires_approval=false`，`safe_auto_execute=true`，可自动巡检。
- 依赖 `ssh`、`nvidia-smi`、`curl`、`pgrep`，仅执行只读采集命令。
- 至少 1 个证据来源即可建议恢复。

## 验证方式

- 检查远程命令是否严格只读，无重启或 kill 操作。
- 校验诊断是否引用 GPU 指标与进程/端点证据。
- 确认 vLLM 指标与健康端点均被覆盖。
- 复查 GPU 与服务状态确认问题是否缓解。
