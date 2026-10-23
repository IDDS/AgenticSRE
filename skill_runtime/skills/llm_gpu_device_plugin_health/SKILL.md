---
name: GPU 设备插件与资源暴露诊断
description: Diagnose NVIDIA device plugin, GPU allocatable, runtime class, node registration, and K8S GPU resource exposure failures.
---
# GPU 设备插件与资源暴露诊断

## 适用场景

用于智算 K8S 集群中 GPU 节点加入异常、`nvidia.com/gpu` allocatable 为 0、
NVIDIA device plugin 不运行、GPU Pod Pending、RuntimeClass/驱动不可见、
某节点如 dds-127 没有 GPU 指标或没有被纳入集群。

## 输入信号

- Node allocatable/capacity、device plugin Pod、RuntimeClass、nvidia-container-runtime。
- Pod Pending、FailedScheduling、Insufficient `nvidia.com/gpu`。
- 节点名、GPU 卡数、驱动版本、K8S 节点状态。

## 执行阶段

1. `detect`：识别 GPU device plugin、GPU 资源暴露和节点注册异常。
2. `diagnose`：判断是节点未注册、device plugin 异常、runtime 缺失还是 GPU allocatable 不一致。
3. `heal`：输出只读核查和审批后的插件/节点修复建议。
4. `verify`：验证节点 Ready、GPU allocatable、device plugin Ready 和 GPU Pod 可调度。

## 风险与门禁

重启 device plugin、修改 runtime、重新 join 节点或重启 kubelet 都可能影响 GPU 工作负载，
必须审批。

## 验证方式

- `kubectl get nodes` 显示目标 GPU 节点 Ready。
- allocatable 中存在 `nvidia.com/gpu`。
- device plugin DaemonSet Ready，GPU Pod 可以调度。
