# Kubernetes 代理精确 Trace 取证验收（2026-09-16）

## 上一轮待办恢复

SSH 连接已恢复。上一轮两个 statistics-canary 容器已删除，`agenticsre` 和 `agenticsre-skill-runtime` 均确认 healthy；此前因超时未下载的完整回归、接口、统计 API 和浏览器证据已补齐到 `outputs/statistics-20260916/`。

## 本轮缺口与修复

上一轮采集器增加 `/api/traces/{trace_id}` 精确取证，但 Web 层 Kubernetes namespace proxy 仍只允许 `/api/services` 和 `/api/traces`，导致代理通道拒绝精确查询。直连 Jaeger 的验收不能证明代理通道也能工作。

源码提交 `5dd0b1e` 将原代理逻辑提取为 `namespace_trace_query` 并接入现有 Web 调用点。固定 Jaeger 服务代理，仅增加 16/32 位十六进制 Trace ID 路径；路径穿越、查询后缀和额外路径段在调用远端 shell 前拒绝。namespace 与查询参数编码，命令参数经过 shell 引号处理。保留空前缀与 `/jaeger/ui` 自动探测；成功 HTTP 响应如果实际为 HTML，也会继续尝试下一前缀。

## 远端验证

全部回归在 `snail` 执行：**695 passed，106.79 秒**，有 28539 条警告（主要为既有 pytest/AST 弃用警告）。新增 8 个代理测试覆盖精确路径、前缀发现/复用、参数引用及非法路径拒绝。

只读真实验收脚本：`eval/alert_trace_proxy_acceptance.py`。在候选容器中调用 Web 层实际 `_shell_sync`，通过集群 `default` 的 Kubernetes API service proxy 访问 `train-ticket` 的 Jaeger，取回上一轮实际实验 Trace `f61c35f2d865475dab415b453ba1f8df`，返回 5 个 Span，探测前缀 `/jaeger/ui`。共 3 次只读命令；两类非法路径没有触发任何远端命令。本轮没有新增故障或实验 Pod。

候选源码摘要一致，异步压缩、分页、旧结果接口和 Job Skill 均通过；页面 HTTP 200、加载 516 ms、控制台错误 0。

## 范围与证据

本轮完成代理通道精确取证修复，没有新增实时底层指标的自动查询。统计验证目前仍依赖已有数值观测；完整自主 RCA、反事实验证、异步及跨命名空间传播仍未完成。

远端目录 `/home/ubuntu/AgenticSRE-releases/traceproxy-20260916/`，本地 `outputs/traceproxy-20260916/`。生产验收也执行真实代理读取，并设失败自动回滚，回滚目标 `agenticsre:statistics-20260916`。保留 full-suite.log、proxy-canary.json、proxy-production.json、源码摘要、接口与浏览器验收。

## 生产验收

已部署 `agenticsre:traceproxy-20260916`，源码 revision `5dd0b1e`，镜像 ID `sha256:08b237d2567acf6c0adf0dfd0d6df5b2a0c3b1e9c6994c5dda1f241d1df8eb6b`。候选通过后仅添加源码标签，生产源码摘要核对一致。

两个服务均 healthy，生产容器使用真实集群配置的代理验收通过，同一链路返回 5 个 Span，非法路径没有执行远端命令。原有 25 条告警生命周期记录保持不变，异步任务、分页、旧结果接口和 Job Skill 通过。生产页面 HTTP 200、加载 528 ms、控制台错误 0。本轮未发现回归。
