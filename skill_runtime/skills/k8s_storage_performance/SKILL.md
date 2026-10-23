---
name: 存储性能退化诊断
description: Diagnose Kubernetes PVC, PV, disk IO, filesystem, and storage latency degradation.
---
# 存储性能退化诊断

## 适用场景

用于 PVC/PV 可用但性能退化、磁盘 I/O 延迟高、IOPS/吞吐受限、fsync 慢、节点磁盘压力、
模型 shard 加载慢或数据库/日志写入慢等问题。

## 输入信号

- PVC/PV、StorageClass、Pod 挂载、node disk io、filesystem、latency、iowait。
- Prometheus 指标、节点事件、应用日志中的 fsync、read/write timeout。
- 目标 namespace、pod、pvc、node。

## 执行阶段

1. `detect`：识别存储性能、磁盘 I/O、容量和挂载退化信号。
2. `diagnose`：区分容量耗尽、节点磁盘压力、后端存储延迟、挂载路径和应用 I/O 瓶颈。
3. `heal`：输出只读核查、扩容、迁移、限流等建议；真实变更需审批。
4. `verify`：验证容量、iowait、读写延迟和业务 P95 恢复。

## 风险与门禁

扩容 PVC、迁移 Pod、调整 StorageClass 或删除数据均为高风险动作。默认不删除数据。

## 验证方式

- PVC Bound，容量充足。
- node disk IO/iowait 回落。
- 应用读写延迟和业务请求延迟恢复。
