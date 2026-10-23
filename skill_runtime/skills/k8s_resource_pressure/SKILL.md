---
name: K8s 资源压力
description: 检测、诊断 Kubernetes CPU、内存和磁盘压力故障，并生成受控恢复建议。
---
# K8s 资源压力诊断与守护式处置

本 Skill 用于处理集群 CPU、内存、磁盘资源压力事件，并给出受控的容量与处置建议。

## 适用场景

- 节点出现 `MemoryPressure`、`DiskPressure` 或高 CPU 负载。
- Pod 被驱逐或调度受阻。
- 容器因资源受限被限流或反复重启。
- 集群整体资源使用率逼近容量上限。

## 输入信号

- `kubectl top nodes/pods` 的 CPU/内存使用率。
- `kubectl describe node` 的 Pressure Condition 与驱逐事件。
- 磁盘使用率、inode 使用与 kubelet 磁盘阈值配置。
- Pod requests/limits 配置与实际用量对比。

## 执行阶段

- `detect`: 识别资源压力类型与影响范围。
- `diagnose`: 结合节点与 Pod 指标判断负载激增、资源分配不合理或磁盘堆积。
- `heal`: 仅生成 dry-run 或建议命令，例如 cordon/drain 建议、扩容、清理磁盘、调整 requests，不自行执行。
- `verify`: 审批执行后确认资源压力解除。

## 风险与门禁

- 风险等级 high，涉及节点级操作与工作负载迁移，必须人工审批。
- `requires_approval=true`，`safe_auto_execute=false`。
- 至少需 2 个独立证据来源方可进入修复建议。
- 进入自愈门禁所需的最低校准诊断置信度为 0.80，全自动执行门槛为 0.95；两者均不表示恢复成功概率。

## 验证方式

- 节点 Pressure Condition 清除，`kubectl top` 使用率回落。
- 磁盘/inode 使用率恢复到阈值以下。
- 无新增 Pod 驱逐或调度失败事件。
- 关键工作负载恢复稳定运行。
