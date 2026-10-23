---
name: K8s 镜像拉取失败
description: 诊断 ImagePullBackOff 和 ErrImagePull 故障，并生成受控发布恢复建议。
---
# K8s 镜像拉取失败诊断与守护式修复

本 Skill 用于诊断 `ImagePullBackOff` 或 `ErrImagePull` 故障，并提出受控的滚动恢复建议。

## 适用场景

- Pod 因镜像拉取失败无法启动。
- 镜像标签不存在、拼写错误或镜像已被删除。
- 私有仓库认证失败，`imagePullSecrets` 缺失或失效。
- 镜像仓库不可达、限流或网络问题。

## 输入信号

- `kubectl describe pod` 中的镜像拉取失败事件。
- 工作负载 spec 中的镜像地址、标签与 `imagePullSecrets`。
- pull secret 存在性与类型。
- 仓库可达性或认证测试结果。

## 执行阶段

- `detect`: 确认为镜像拉取类故障并采集失败事件。
- `diagnose`: 区分标签错误、认证失败与仓库不可达。
- `heal`: 仅生成 dry-run 或建议命令，例如修正镜像地址、补齐 pull secret 后滚动更新。
- `verify`: 批准执行后确认镜像成功拉取、Pod 就绪。

## 风险与门禁

- 风险等级 medium，镜像变更与凭证修改必须人工审批。
- `requires_approval=true`，`safe_auto_execute=false`。
- 至少 2 个独立证据来源方可进入修复建议。
- 进入自愈门禁所需的最低校准诊断置信度为 0.85，全自动执行门槛为 0.90；两者均不表示恢复成功概率。

## 验证方式

- `kubectl describe pod` 出现 `Successfully pulled image`。
- 目标 Pod 进入 `Running`/`Ready`，无新增拉取失败事件。
- 滚动更新完成且副本可用。
