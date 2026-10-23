---
name: K8s Deployment 异常
description: 检测、诊断探针失败或副本不可用的 Kubernetes Deployment，并生成受控恢复建议。
---
# K8s Deployment 不健康诊断与守护式修复

本 Skill 用于处理探针失败或副本不可用导致的 Deployment 不健康问题。

## 适用场景

- Deployment 可用副本数长期低于期望值。
- 就绪或存活探针失败导致 Pod 反复重启。
- 滚动更新卡住，出现 `progressDeadlineExceeded`。
- 资源不足或依赖未就绪造成 Pod 无法进入 Ready。

## 输入信号

- Deployment 的 `READY`、`AVAILABLE`、`UP-TO-DATE` 字段。
- `kubectl describe deployment` 的 rollout 条件与事件。
- Pod 的 `restartCount`、探针失败事件与容器退出码。
- `kubectl logs` 中的应用启动或健康检查错误。

## 执行阶段

- `detect`: 识别副本不可用或探针失败，确认为 Deployment 级健康故障。
- `diagnose`: 结合探针配置、容器日志、资源与事件定位根因。
- `heal`: 仅生成 dry-run 或建议命令，例如滚动重启、回滚上一个稳定版本。
- `verify`: 批准执行后确认副本恢复可用、探针通过。

## 风险与门禁

- 风险等级 medium，重启或回滚会中断服务副本，必须人工审批。
- `requires_approval=true`，`safe_auto_execute=false`。
- 至少 2 个独立证据来源方可进入修复建议。
- 进入自愈门禁所需的最低校准诊断置信度为 0.84，全自动执行门槛为 0.95；两者均不表示恢复成功概率。

## 验证方式

- `kubectl rollout status deployment/<name>` 显示成功。
- 可用副本数恢复到期望值，Pod `restartCount` 稳定。
- 就绪/存活探针连续通过，无新增失败事件。
