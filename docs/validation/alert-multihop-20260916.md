# 多跳告警传播验收（2026-09-16）

## 方法改进

上一轮只验证直接依赖故障对调用方的影响。本轮沿同一 Trace 中连续的父子 Span，构造反向故障传播候选，支持“依赖 → 中间服务 → 调用方”。即使中间服务没有告警输入，也可利用其真实链路证据召回两端告警。

每一跳必须具备双方明确错误标志、合法的嵌套时间、同集群同命名空间的已映射资源。相邻跳必须通过同一个 Span ID 连接，且中间 Span 完成时间一致；不能只依据服务名称或时间接近拼接路径。重复链路去重，循环实体路径拒绝。输入有显式 Trace ID 时继续限制匹配。五跳路径的证据引用完整保留，不再受三条代表性引用的限制。

SCAG 分别记录完整 `propagation_path` 与反向的 `observed_call_path`；保留已有单跳接口。路径搜索按待验证两端过滤起点。模型可收到路径候选，但路径本身不确认因果，也不授权告警抑制。

## 覆盖与边界

新增 11 项回归：无中间告警的多跳召回、不同 Trace、不同 Span、健康中间节点、缺失错误标志、时间断裂、不同集群、过期节点、五跳证据完整性、重复后端结果、循环路径。全部测试和服务验证在 `snail` 运行。

当前仅处理**跨资源边可以直接通过 Span ID 连续连接**的路径。若应用把一个服务拆成多个内部 server/client Span，现有映射未保留其内部桥接边时，本轮会保守返回证据不足；异步调用、跨命名空间传播、时钟偏差容忍和缺失链路推断也未在本轮支持。原有 Trace 采集范围和最多 200 条映射边限制仍存在，不能声称已覆盖任意规模、任意拓扑。

## 真实实验

复用 `tests/e2e/test_live_propagation.py`，增加三个环境变量控制三服务模式与候选镜像：

```sh
AGENTICSRE_RUN_LIVE_PROPAGATION=1 \
AGENTICSRE_PROPAGATION_MULTIHOP=1 \
AGENTICSRE_PROPAGATION_IMAGE=agenticsre:multihop-20260916 \
AGENTICSRE_PROPAGATION_OUTPUT=/home/ubuntu/AgenticSRE-releases/multihop-20260916/live-results-final \
python3 tests/e2e/test_live_propagation.py
```

在隔离命名空间内启动 caller、middle、dependency 三个真实 Pod，通过 HTTP 传播控制故障，在实际请求中采集 Span 并发送现有 Jaeger。使用真实 Pod UID 映射，三 Pod 位于同一节点。验收仅输入最下游和最上游的两条日志，中间服务没有输入告警；分别验证按 Trace ID 读取证据，以及生产采集函数 `collect_traces` 查询两端服务后获得完整链路。

| 阶段 | HTTP | Span 数 | 多跳传播上下文 | SCAG 总体状态 |
|---|---:|---:|---|---|
| 正常 | 200 | 3 | unknown | unverified |
| 下游故障 | 503 | 3 | pass，dependency → middle → caller | unverified |
| 恢复 | 200 | 3 | unknown | unverified |

本实验使用显式测试假设，模型调用 0；日志输入由真实 HTTP 观测转换，并非 Prometheus firing 告警。统计序列不足，因此即使传播上下文通过，完整关系仍不提升为 evidence_supported 或已确认根因。这不是完整自主 RCA 或反事实实验的验收。

## 证据目录

远端 `/home/ubuntu/AgenticSRE-releases/multihop-20260916/`；本地 `outputs/multihop-20260916/`。保留两轮完整测试日志、两轮三服务实验结果、源码摘要、候选和生产接口验收及浏览器证据。运行产物不提交 Git。

## 发布结果

源码提交 `03a4000`。最终远端全量回归 **665 passed，110.86 秒**，有 28035 条警告（主要为既有 pytest/AST 弃用警告）。首轮回归同为 665 passed，之后对完整调用路径输出和搜索过滤调整后重跑通过。

最终真实实验命名空间 `agenticsre-propagation-4eebf9f0`，三阶段各 3 个 Span，验收通过且 `cleanup_verified=true`。第一轮三服务实验亦通过并清理。

候选服务源码摘要匹配；异步压缩、结果分页、旧接口及 Job Skill 均通过。候选页面 HTTP 200、加载 556 ms、控制台错误 0。生产发布保留 `agenticsre:propagation-20260916` 的 Compose 配置作为自动回滚目标。

生产发布与验收通过：`agenticsre` 和 `agenticsre-skill-runtime` 均运行 `agenticsre:multihop-20260916`，健康状态 healthy。镜像 ID `sha256:ebd00365899e196a0068b642a90a8056f0934fcd8ea3b0c5fb4b56449edf3923`，revision 标签 `03a4000`。候选验收后只增加源码版本标签，运行源码经 SHA256 再次核对一致。

原有 25 条生产告警生命周期记录前后完全相同；异步压缩、分页、旧结果接口与 Job Skill 通过。生产页面 HTTP 200、加载 530 ms、控制台错误 0。本轮验收未发现回归；这一结论不表示已经证明所有原设计能力或所有生产场景均无问题。
