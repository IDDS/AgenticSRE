# 服务内部 Span 衔接验收（2026-09-16）

## 修复的缺口

上一版只保留跨 Pod 的父子 Span。当中间服务同时有接收请求的 server Span 和发起下游请求的 client Span 时，两个外部调用边不能直接相接，传播验证会保守返回证据不足。

本轮将同一 Pod 内的父子关系保存在独立的 `internal_span_edges` 中，并贯通直接采集和命名空间代理采集。外部调用边仍放在原 `edges` 字段，内部关系不会单独成为跨服务调用。

传播验证仍要求同一 Trace、精确的 Span 父子连接、双方显式错误、嵌套时间，以及有效资源身份。内部边只允许连接连续的 Span，不能用服务名称替代链路证据。路径展示压缩相邻相同实体；证据保留全部 Span 连接，分别输出服务跳数 `hops` 和 `span_link_count`。优先保留更完整的有效 Span 路径，继续拒绝 Span 循环及跨实体回环。

新增 9 项回归：内部衔接正例、映射器保留内部连接，以及缺失连接、健康、错误未知、不同 Trace、错误父 Span、时间断裂、未标明内部关系七类负例。全部回归在远端 `snail` 执行。

## 真实实验

复用远端三服务实验，增加 `AGENTICSRE_PROPAGATION_SPLIT_SPANS=1`，caller 与 middle 分别生成真实接收请求和下游 HTTP 调用的两个 Span，dependency 生成一个 Span。总计 5 个 Span、4 条父子连接，其中 2 条为内部连接。

仅将 dependency 和 caller 的实际 HTTP 观测转为两条日志输入；middle 不提供告警。正常 200、下游故障 503、恢复 200 三阶段均通过实际 Jaeger 接收和查询，再由生产采集函数 `collect_traces` 获取链路用于验证。命名空间独立，Pod 采用真实 UID，部署于同一节点以减少时钟偏差。

预期：只有 503 阶段支持 dependency → middle → caller，两跳路径保留完整四条 Span 连接；正常和恢复阶段均不支持传播。统计序列不足时整体关系保持 `unverified`，`causal_confirmed=false`。

验收使用显式假设，模型调用数为 0；输入属于实际 HTTP 观测日志，并非 Prometheus firing 告警。它验证采集与传播证据衔接，不代表自主 RCA、统计因果或告警抑制已经通过验收。

## 适用边界

要求完整、同步、严格嵌套的父子链路，以及每个内部连接两端的错误标志。未标记错误的内部 Span 不猜测为故障支持。跨命名空间、异步调用、缺失 Span 和时钟偏差容忍尚未在本轮支持。原有 Trace 采集限制仍保留；内部连接单独最多保留 200 条，超出时标记 truncated。

## 证据与发布

远端目录 `/home/ubuntu/AgenticSRE-releases/spanbridge-20260916/`；本地目录 `outputs/spanbridge-20260916/`。保留完整回归日志、三阶段原始观测及链路、传播图、采集器结果、源码摘要、接口与浏览器验收。运行产物不提交 Git。

源码提交 `48ab7d9`。远端完整回归 **674 passed，106.39 秒**；存在 28147 条警告（主要为既有 pytest/AST 弃用警告），不应表述为零警告。

真实实验命名空间 `agenticsre-propagation-9a274a75`。三个阶段均获得 5 个 Span；故障阶段验证通过，两跳服务路径保留四条 Span 引用；正常与恢复阶段为 unknown。原始日志输入完整保留，`cleanup_verified=true`。

候选镜像与本地已提交源码摘要一致；异步压缩、分页、旧结果接口和 Job Skill 通过。候选页面 HTTP 200、加载 554 ms、无控制台错误。发布保留上一版 `agenticsre:multihop-20260916` 作为自动回滚目标。

生产部署与验收通过：`agenticsre` 和 `agenticsre-skill-runtime` 均运行 `agenticsre:spanbridge-20260916` 且 healthy。镜像 ID `sha256:135e78e2d1251de93dd1b4d3544499958f242b5a5a8cf5599a131740ebee6781`，revision `48ab7d9`。候选通过后仅增加镜像版本标签；生产运行源码再次经过 SHA256 核对一致。

原有 25 条告警生命周期记录前后完全一致，异步任务、分页、旧接口、Job Skill 验收通过。生产页面 HTTP 200、加载 587 ms、控制台错误 0。本轮验收未发现回归，不能据此宣称完整原设计已实现或所有生产场景无问题。
