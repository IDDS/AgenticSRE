---
name: DCGM GPU 指标采集诊断
description: Diagnose DCGM exporter, GPU metric target, and Prometheus scrape issues for AI clusters.
---
# DCGM GPU 指标采集诊断

## 适用场景

用于智算集群中 GPU 指标缺失、DCGM exporter 不可用、Prometheus target down、
某个节点没有 GPU 曲线、前端 GPU 利用率/显存显示为 0 或空白等问题。

## 输入信号

- Prometheus target、scrape 错误、`DCGM_FI_*` 指标缺失。
- `dcgm-exporter` Pod、Service、ServiceMonitor 或 endpoint 状态。
- 节点名、GPU 卡数、namespace、Prometheus 地址。

## 执行阶段

1. `detect`：识别 GPU 指标缺失、DCGM target down、scrape 失败等信号。
2. `diagnose`：优先读取 Prometheus target 和 Kubernetes 对象，判断是 exporter、
   service discovery、scrape 配置还是单节点 GPU/驱动问题。
3. `heal`：输出只读复核、重建 ServiceMonitor、重启 exporter 等建议，真实变更需审批。
4. `verify`：验证 target up、`DCGM_FI_DEV_GPU_UTIL`、显存指标和前端曲线恢复。

## 风险与门禁

默认只读。重启 DaemonSet、修改 ServiceMonitor 或部署 exporter 属于中风险变更，
必须审批并保留回滚方案。

## 验证方式

- Prometheus `/api/v1/targets` 中 DCGM target 为 `up`。
- 每个 GPU 节点都有 `DCGM_FI_DEV_GPU_UTIL` 和显存指标。
- 前端智算观测页能按节点和 GPU 卡展示曲线。
