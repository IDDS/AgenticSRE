---
name: K8s 卷挂载失败
description: 检测、诊断 Kubernetes volume mount、attach 和 PVC 绑定失败，并生成受控恢复建议。
---
# K8s 存储卷挂载失败诊断与守护式修复

本 Skill 用于处理卷挂载、Attach 及 PVC 绑定失败问题，并给出受控修复建议。

## 适用场景

- Pod 卡在 `ContainerCreating`，出现 `FailedMount` 或 `FailedAttachVolume` 事件。
- PVC 长期 `Pending`，无法与 PV 绑定。
- 存储后端不可用、AccessMode 或 StorageClass 不匹配。
- 卷被其他节点占用导致 Multi-Attach 错误。

## 输入信号

- `kubectl describe pod` 中的挂载或 attach 失败事件。
- PVC 与 PV 的状态、容量、AccessMode 与 StorageClass。
- CSI 驱动或存储插件状态与日志。
- 节点上的卷挂载点与 Multi-Attach 冲突信息。

## 执行阶段

- `detect`: 识别挂载、attach 或绑定失败，确认为存储类故障。
- `diagnose`: 结合 PVC/PV 状态、StorageClass、CSI 日志定位根因。
- `heal`: 仅生成 dry-run 或建议命令，例如删除卡住 Pod 触发重新挂载、修正 PVC/StorageClass 配置，不自行执行。
- `verify`: 审批执行后确认卷成功挂载、Pod 正常运行。

## 风险与门禁

- 风险等级 medium，删除 Pod 可能影响服务与数据可用性，必须人工审批。
- `requires_approval=true`，`safe_auto_execute=false`。
- 至少需 2 个独立证据来源方可进入修复建议。
- 进入自愈门禁所需的最低校准诊断置信度为 0.82，全自动执行门槛为 0.95；两者均不表示恢复成功概率。

## 验证方式

- PVC 变为 `Bound`，卷成功 attach 与 mount。
- Pod 从 `ContainerCreating` 进入 `Running`。
- 无新增 `FailedMount` 或 Multi-Attach 事件，应用可正常读写卷。
