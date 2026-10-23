---
name: TELLER Trace 持续采集诊断
description: Diagnose TELLER trace freshness, request context, NVTX/CUDA event coverage, and profiler collection health.
---
# TELLER Trace 持续采集诊断

## 适用场景

用于智算请求级 Trace 长时间不更新、只有 NVTX 没有 CUDA、缺少 request_id、
无法还原完整请求路径、并发压测下 Trace 丢失或前端时间轴为空等问题。

## 输入信号

- trace 文件更新时间、事件类型统计、request_id 覆盖率。
- NVTX、CUDA Runtime、CUDA Driver、Kernel 事件数量。
- TELLER collector/patcher Pod、采集路径和前端 Trace 摘要。

## 执行阶段

1. `detect`：识别 trace stale、request context missing、CUDA event missing。
2. `diagnose`：判断是采集进程、埋点注入、权限、缓冲区还是前端解析问题。
3. `verify`：验证最新 trace 时间、request_id、CUDA event 和前端请求路径。

本 Skill 不重新部署采集器；需要恢复采集时，由下游恢复 Skill 在审批后执行。

## 风险与门禁

默认只读。重新 patch vLLM、启用 CUPTI 或调整采集采样率可能影响推理性能，
必须审批并先在低流量窗口验证。

## 验证方式

- trace 文件持续更新。
- 每个请求有 request_id，且能关联 HTTP/vLLM/NVTX/CUDA Runtime/Kernel。
- 前端 Trace 时间轴能展开事件详情和瓶颈统计。
