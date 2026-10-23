---
name: vLLM 进程崩溃
description: 检测、诊断 vLLM 推理进程崩溃，并生成恢复建议。
---
# vLLM 进程崩溃

## 适用场景

用于检测和诊断 vLLM 主进程崩溃、推理端点连接拒绝、HTTP 503、K8S Pod CrashLoopBackOff 或裸机 vLLM 服务退出。

该 Skill 可从智算故障实验、智算事件中心、告警中心 Skill 分支或独立 Skill 调试器触发。

## 输入信号

推荐输入：

- `fault_type`、`scenario_id`、`message`
- `endpoint_status`
- `metrics_status`
- `process_count`
- `gpu_count`
- `metrics_count`
- `waiting_requests`
- `running_requests`
- `namespace`
- `deployment`
- `host_id`
- `vllm_endpoint`

## 执行阶段

- `detect`: 识别进程崩溃、连接拒绝、HTTP 503、CrashLoopBackOff 等信号。
- `diagnose`: 结合端点状态、指标状态、进程数、GPU 和队列指标生成根因。
- `heal`: 生成健康检查、Deployment scale、rollout restart 或裸机 systemd 重启建议。
- `verify`: 验证 rollout、健康接口和推理服务状态。

## 输出

诊断输出：

- `fault_type`: `LLM::vllm_process_crash`
- `root_cause`: `vLLM 主进程崩溃或退出，导致推理端点不可用`
- `root_cause_component`: Deployment、host 或 vLLM 实例
- `confidence_explanation`: 使用 `llm_fault_observable_signals_v1`

## 风险与门禁

该 Skill 包含 K8S restart/scale 和裸机 process restart 能力，风险为高。真实执行必须审批，禁止静默重启推理服务。

进入自愈门禁所需的最低校准诊断置信度为 `0.85`，全自动执行门槛为 `0.90`；两者均不表示恢复成功概率，且当前策略不允许自动执行。

## 验证方式

恢复后至少验证：

- vLLM 健康接口可访问。
- Deployment rollout 完成或裸机进程恢复。
- 请求队列、TTFT/TPOT、E2E P95 恢复正常。
- 故障告警从智算事件中心消退。
