---
name: K8s 配置缺失
description: 检测、诊断 Kubernetes ConfigMap、Secret 或配置键引用缺失，并生成受控恢复建议。
---
# K8s 配置缺失诊断与守护式修复

本 Skill 用于检测、诊断并提出受控修复方案，处理 Kubernetes 工作负载引用的 ConfigMap、Secret 或配置键缺失问题。

## 适用场景

- Pod 因 `CreateContainerConfigError` 无法启动，事件提示 `configmap "xxx" not found` 或 `secret "xxx" not found`。
- Deployment/StatefulSet 挂载的 `configMapKeyRef` 或 `secretKeyRef` 指向不存在的键。
- `envFrom`、volume 挂载引用的配置对象被误删或未同步到目标命名空间。
- 配置回滚、命名空间迁移后引用与实际对象不一致。

## 输入信号

- `kubectl get events` 中的 `FailedMount` 或 `CreateContainerConfigError` 事件。
- `kubectl describe pod` 显示的挂载与环境变量引用错误。
- `kubectl get configmap,secret -n <namespace>` 的实际存在清单。
- 工作负载 spec 中的 `envFrom`、`valueFrom`、`volumes` 引用列表。

## 执行阶段

- `detect`: 采集异常 Pod 事件与状态，确认为配置引用缺失类故障。
- `diagnose`: 对比工作负载引用的配置对象与集群实际存在的对象，定位缺失对象、键名及命名空间。
- `heal`: 仅生成 dry-run 或建议命令，例如重新创建配置或重启工作负载的候选命令，供人工审批后执行。
- `verify`: 在人工批准并执行后，通过 Pod 就绪状态与事件复检确认恢复。

## 风险与门禁

- 风险等级 medium，真实变更必须审批。
- `requires_approval=true`，`safe_auto_execute=false`。
- 至少需要事件和 spec 比对两类证据方可进入修复建议。
- 进入自愈门禁所需的最低校准诊断置信度为 0.80，全自动执行门槛为 0.95；两者均不表示恢复成功概率。

## 验证方式

- `kubectl -n <namespace> get pods` 目标 Pod 进入 `Running` 或 `Ready`。
- `kubectl -n <namespace> describe pod <pod>` 不再出现配置引用错误事件。
- 关联 ConfigMap/Secret 存在且键完整。
