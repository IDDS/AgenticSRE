# 告警语义压缩与关联证据验证（2026-09-15）

本次在无损告警分组的基础上补齐符号摘要、候选召回和 SCAG 关联证据检查。算法、API 与证据包已经连接；设计图中的完整研究架构仍不能视为全部实现。

## 实现与适用边界

| 设计方法 | 当前实现 | 边界 |
|---|---|---|
| 日志模板压缩 | Drain3 模板挖掘；明确易变字段的安全模板控制自动合并 | 仅屏蔽 request/trace/span/correlation ID 和 duration_ms、latency_ms；状态码、版本、否定词和目标保留。Drain 泛化通配符不能直接授权合并 |
| 指标符号摘要 | 同对象、指标、单位、完整标签集及时间窗内归并；保留 min/max/mean、样本数、唯一样本数和峰谷原文引用 | 接口需要 metric_name、metric_value，可选 unit、series_labels。实时 Prometheus 告警快照不等同于历史指标序列，不凭空补数值 |
| 异常与频率排名 | 严重度优先，提供历史基线时计算单侧计数偏移，再保留低频事件优先级 | 基线由调用方显式提供并持久化；缺失时未知。计数偏移不是概率或统计显著性，不是季节性异常检测 |
| 语义候选召回 | 本地 ONNX all-MiniLM-L6-v2 实际神经向量，结合中英术语表与关键词，组内 Top-5 | 最多索引 256 个优先组、返回 200 对、交给 LLM 20 对。英文模型对中文能力有限；覆盖截断及降级明确返回 |
| SCAG 关系类型 | LLM 输出同义、共同根因、传播、共同依赖、变更触发、冲突六类候选；验证样本引用 | 关系类型是模型假设。物理拓扑不等于调用链；变更内容与反事实推断尚未自动采集 |
| 三项检查 | 新鲜拓扑路径/共同父依赖，时间窗/方向先后，至少五个精确对齐数值点的 Pearson 相关 | 全部支持仅称 evidence_supported；不证明因果。缺证据 unknown，矛盾 contradicted；常量序列不能作为统计支持 |
| 证据包与人工编辑 | 原文全量保存，峰谷样本与最多三条关联检查进入 RCA 证据包；诊断前重新检查拓扑 TTL | 人工拆合后旧关联失效；RCA 继续执行实时取证与审核 |

召回预算为 30 秒；超时工作线程只处理独立副本，不会延迟污染已保存结果。模型文件随运行镜像分发，请求时不下载模型。现有较大 RCA 和语义模型调用预算保持生效。关系分类输出上限按候选数量从 2,048 增至最多 12,288 tokens；DeepSeek v4 的该类调用使用非思考 JSON 模式，其他 RCA 调用不传此覆盖参数。

实时拓扑来自 Kubernetes 节点调度与 Service/Deployment 标签选择关系，TTL 为 300 秒。调用方导入的拓扑强制标为 `client_supplied`，不能伪装成实时采集。跨集群边不进入路径搜索。接口响应保留拓扑时间、来源、原始告警引用和每项检查结果。

## 可复现验证

全部测试与模型执行位于远端 `snail`，本地仅编辑和打包。

- 远端完整回归：554 passed，3 项既有弃用警告，96.57 秒。回归覆盖安全模板、指标序列边界与峰谷、基线时段校验、Top-N 限额、降级、拓扑 TTL、跨集群路径、共同依赖、方向时间矛盾和超时后禁止迟到写入。
- `scripts/evaluate_alert_semantics.py`：401 条受控告警压为 5 组，98.753% 条数压缩率；按独立事件标签计算的配对 precision/recall 均为 1，误合并 0、遗漏 0，1 条低频关键告警保留。
- 容量边界回放：10,000 条互异告警保留为 10,000 组、遗漏 0；明确报告仅索引 256 组和 200 对候选，符号处理 9.06 秒，含向量召回与图检查共 15.50 秒。此结果表明上限处理可运行，不代表高并发吞吐保证。
- 实际离线模型输出 384 维向量；四条正负描述中同义描述命中、与无关描述的交叉召回 0；模型及召回合计约 0.95 秒。无数值序列/拓扑的候选没有被提升为有充分证据支持。

- 部署后真实扫描采集 16 条、分为 14 组，Kubernetes 和 Prometheus 采集正常；本地语义向量覆盖 14/14 组，实时拓扑包含 460 条物理关系。该次 13 对候选的模型输出出现 JSON 解析失败，原始观测与规则分组保留；同批候选复验确认：2,048 和 8,192 tokens 均以 length 结束且正文为空，额度被思考消耗。改用分类专用非思考 JSON 模式后，13 对候选在 7.71 秒内返回 5 条合法类型和引用的关系，输出上限 6,656 tokens。
- 在线导入 API：101 条压为 2 组，模型提出 1 条带合法样本引用的待验证关联；12 个指标样本压为 2 组并保留峰值 10/20。伪装为实时来源的导入拓扑被重标为 client_supplied。
- 真实镜像拉取故障：`rca-240cfc7d`，100 次重复投递压为 1 组，409.88 秒完成，故障族 image_pull_error，输出对应不存在的测试镜像与部署未就绪证据。该次返回视图 human_review 字段为 null，不将其解释为人工批准。独立测试命名空间已清理。
- 浏览器通过 SSH 转发访问远端服务，确认 app.js?v=132、指标峰值、缺基线提示、关联检查展示，无控制台错误；截图见验收输出目录。

这些是可复现的受控验证，不是生产分布上的准确率评估，也未测量完整因果路径准确率。尚需真实标注事故集、多语言召回评测、季节性基线、Tracing/变更证据接入及过程监督/局部回放训练，才能覆盖原架构中的全部研究目标。

## 接口示例

`POST /api/alerts/compress` 原有 alerts、cluster_id、namespace、time_window 参数继续有效；可选 `semantic: true` 调用 LLM 关系提议。默认仍执行本地向量召回和证据检查。

指标记录除原有身份字段外，可包含：

```json
{"metric_name":"request_latency","metric_value":850,"unit":"ms","series_labels":{"service":"checkout"}}
```

基线参数为 `baseline: {"alerts": [...], "duration_s": 300, "end_time": 1234567890}`。全部历史记录必须在声明窗口内，窗口结束不能晚于当前记录开始。

拓扑参数为 `topology: {"observed_at": 1234567890, "ttl_s": 300, "edges": [{"source":"cluster|namespace|pod|api","target":"cluster|namespace|pod|db"}]}`。TTL 最大 3600 秒，路径搜索最多四跳。

## 来源与证据

- 项目实现：`agents/alert_semantics.py`、`agents/alert_compression.py`、`web_app/app.py`。
- [Drain3 官方项目](https://github.com/logpai/Drain3)：在线模板挖掘。本项目额外使用保守合并条件。
- [Chroma 官方嵌入说明](https://docs.trychroma.com/docs/embeddings/embedding-functions)：默认模型及本地向量生成。本次将模型预装到镜像。
- [DeepSeek 官方思考模式说明](https://api-docs.deepseek.com/guides/thinking_mode/)：单次 Chat Completions 请求可通过 thinking.type 控制思考模式。
- 原始验收文件保存在 `outputs/alert-semantics-20260915/`；发布目录 `/home/ubuntu/AgenticSRE-releases/semantic-20260915`，回滚目录 `/home/ubuntu/AgenticSRE-releases/alerts-20260915`。离线安装包尚未重新生成，不应把旧安装包视为包含本次功能。
