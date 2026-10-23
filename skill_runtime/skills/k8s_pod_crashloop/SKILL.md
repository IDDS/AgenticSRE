---
name: K8s Pod 循环崩溃
description: 检测、诊断 CrashLoopBackOff Pod，并生成受控自愈建议。
---
# K8s Pod CrashLoopBackOff 诊断与守护式修复

本 Skill 用于处理进入 CrashLoopBackOff 的 Pod，定位崩溃根因并给出受控修复建议。

## 适用场景

- Pod 状态为 `CrashLoopBackOff`，容器反复启动失败。
- 容器启动后立即退出，`restartCount` 快速增长。
- 应用启动异常、配置错误或依赖未就绪导致崩溃。
- 入口命令、健康检查或权限问题引发启动失败。

## 输入信号

- Pod 的 `CrashLoopBackOff` 状态与重启次数。
- `kubectl logs --previous` 中上一次容器退出前的错误堆栈。
- `kubectl describe pod` 的退出码、事件与探针失败信息。
- ConfigMap/Secret、启动命令与依赖服务可用性。

## 执行阶段

- `detect`: 识别 CrashLoopBackOff 并确认崩溃频率。
- `diagnose`: 结合上一次容器日志、退出码与配置定位根因。
- `heal`: 仅生成 dry-run 或建议命令，例如回滚版本、修正配置后滚动更新，不自行执行。
- `verify`: 审批执行后确认容器稳定运行、不再崩溃。

## 风险与门禁

- 风险等级 medium，回滚或滚动更新会影响服务，必须人工审批。
- `requires_approval=true`，`safe_auto_execute=false`。
- 至少需 2 个独立证据来源方可进入修复建议。
- 进入自愈门禁所需的最低校准诊断置信度为 0.85，全自动执行门槛为 0.90；两者均不表示恢复成功概率。

## 验证方式

- Pod 进入并维持 `Running`，`restartCount` 稳定。
- `kubectl logs` 显示应用正常启动，无崩溃堆栈。
- 探针连续通过，无新增 BackOff 事件。
