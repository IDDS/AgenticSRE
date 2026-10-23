---
name: K8s 控制面异常
description: 检测、诊断 Kubernetes apiserver、etcd、kube-proxy 和 kubelet 健康故障，并生成受控恢复建议。
---
# K8s 控制平面健康诊断与守护式修复

本 Skill 处理 Kubernetes 控制平面组件健康事件，进行检测、诊断并给出受控恢复建议。

## 适用场景

- kube-apiserver 不可达、响应超时或频繁重启。
- etcd 集群失去法定人数、延迟升高或数据落后。
- kube-proxy 规则未下发导致 Service 转发异常。
- kubelet 状态 `NotReady`、心跳丢失或节点组件崩溃。

## 输入信号

- `/healthz`、控制平面 Pod 状态与重启计数。
- 节点 `Ready` 条件与 kubelet 心跳。
- etcd 健康检查、apiserver 审计或慢日志中的错误。
- kube-system 命名空间组件事件。

## 执行阶段

- `detect`: 识别控制平面组件异常，区分单组件故障与集群级故障。
- `diagnose`: 交叉分析组件日志、健康探针、节点状态与 etcd 法定人数，定位根因。
- `heal`: 仅生成 dry-run 或建议命令，例如重启静态 Pod、恢复 kubelet、隔离故障节点的候选步骤。
- `verify`: 批准执行后复检控制平面健康与集群可用性。

## 风险与门禁

- 风险等级 high，控制平面与节点级写操作影响面大，必须人工审批。
- `requires_approval=true`，`safe_auto_execute=false`。
- 至少 2 个独立证据来源方可进入修复建议。
- 进入自愈门禁所需的最低校准诊断置信度为 0.82，全自动执行门槛为 0.98；两者均不表示恢复成功概率。

## 验证方式

- `/healthz` 返回健康。
- 控制平面 Pod 稳定运行，重启计数不再增长。
- `kubectl get nodes` 目标节点恢复 `Ready`，集群 API 调用恢复正常延迟。
