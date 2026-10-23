# 自动历史基线、Tracing 与发布证据接入

本轮继续补齐告警语义压缩的数据闭环。所有运行验证都在远端 snail 完成，本地只编辑和打包。

## 当前行为

**自动历史基线。** 完整实时扫描按集群、请求命名空间与 5 分钟时间桶保存在 SQLite 中，同桶只保留最后一次完整快照。最近 24 小时至少有 3 个历史时间桶才能形成基线；当前桶、采集失败、导入数据和人工编辑后的分组不参与。分值描述“每次完整扫描中同类告警数量的变化”，不是新事件到达率，也不是显著性概率。调用方显式提交的时间窗基线仍使用原接口。

历史索引引用原扫描 ID，7 天后移除索引行，原始扫描 JSON 不删除。初次启用可从最多 100 个既有扫描中迁移：新记录必须有相同作用域；旧记录只接受能证明确为跨命名空间完整实时扫描的记录。样本不足显示 warming_up，不生成虚构基线。

**Tracing。** 查询集群配置中的 Jaeger 地址，匹配当前告警涉及的 Kubernetes Service，最多查询 3 个服务、每服务 5 条 Trace。不会调用全局后端自动发现。配置后端不可用时，只允许回退到告警命名空间中唯一的 jaeger Service（16686 端口）；最多两个命名空间，地址来自当前集群资源清单，存在歧义则不选。回退使用 Kubernetes 只读服务代理，兼容根路径与本环境的 `/jaeger/ui` 查询前缀，不需要更改业务网络。映射要求实际 Pod UID，或明确匹配的集群、命名空间和 Pod 名；仅凭 serviceName 不建立关联。CHILD_OF 引用必须属于同一 Trace，父 Span 必须包含子 Span 的起始时间。重复 Trace 去重，截断显式报告。

实际调用边可召回文本不相似的告警，候选仍限于前 256 个优先组和最多 200 对。历史时间不匹配的调用不参与当前告警关系。Trace 证据是被观测的调用，不等于故障传播已被证明。

**发布变更。** 读取 ReplicaSet，按 Deployment UID 比较相邻且连续的 revision。记录前后镜像、模板指纹、ReplicaSet UID 和创建时间；环境变量等内容只参与哈希，不复制到证据包。Pod 必须通过 owner UID 匹配新 ReplicaSet；只返回当前告警相关资源的变化。跳过回滚复用、时间倒序、重复 revision、缺失版本等无法安全推断的情况。

ReplicaSet 创建时间只是发布证据，不代表准确的业务生效时间。配置内容变化、代码提交、外部发布系统及审计事件尚未全部接入。`change_trigger` 必须有告警发生前的匹配版本变化；`propagates_to` 必须有匹配的调用上下文。所有关联仍保持 `causal_confirmed=false`。

**诊断与页面。** 上下文引用进入关系提议和 RCA 证据包；人工拆分、合并使旧关联和自动排名状态失效。页面显示历史样本数、Tracing/发布采集状态及引用数量。原始告警仍全量保存。

## 远端验证

首轮上线发现既有地址 `localhost:16686` 在容器内不可达；集群内存在两个不同命名空间的 Jaeger。进一步只读核验发现 ClusterIP 无法从应用主机直达，Jaeger 的查询扩展配置前缀为 `/jaeger/ui`。通过 Kubernetes 服务代理与正确前缀，实测成功返回 7 个服务。修复按命名空间约束后端回退，并保持资源标识验证，不能因为成功连上后端就把同名服务当作同一 Pod。

受控跨模块验收确认：Trace ID、父子 Span ID 能通过关联图与持久化进入 RCA 证据包，且不变成已确认因果。首次在线扫描采集 16 条告警、14 组、遗漏 0，自动迁移 2 个历史时间桶，样本不足时正确显示 warming_up。该次告警相关资源没有可比较的发布版本，没有制造变更原因。下一轮真实扫描迁入 3 个历史时间桶，基线状态变为 ready，14 组告警均使用自动快照基线评分。

真实发布验收在独立命名空间 `agenticsre-diag-e2e-context-af23ce` 内完成：先启动健康镜像，再更新为不存在的镜像；扫描采集 3 条告警，识别相邻 ReplicaSet 版本、前后镜像及所属 Pod，在 3 条关联中保留变更引用。测试命名空间已清理，详细记录见 `live-rollout.json`。

最终部署验收：562 项测试通过，3 项既有弃用警告，97.55 秒。两个服务运行 `021790a`，均为 healthy，逐文件 SHA-256 校验通过。

最终扫描 `scan-05a3f9026b754844b2e35ec04f0a3841`：16 条告警分为 14 组、遗漏 0，27.90 秒完成；自动基线包含 4 个历史桶，语义模型在 13 对候选中接受 5 条带引用的待验证关系。命名空间 Jaeger 后端能列出 7 个服务，本次告警没有匹配到其中的目标服务，故没有生成 Trace 边。单独读取 ts-order-service 最近 1 小时 Trace 也返回 0 条，不能声称生产 Trace 关联准确率已验证。

远端页面使用 app.js?v=134，显示“自动历史基线：已就绪”和真实 Trace 覆盖状态，无控制台错误。离线安装包未重新生成。

## 验证范围

回归覆盖：同桶去重、跨集群与命名空间隔离、当前窗口排除、历史过期、失败/导入记录排除、旧扫描迁移、Trace 精确身份与父子时间校验、重复 Trace 去重、回滚复用排除、敏感字段不进入发布证据、非相似文本的调用证据召回。

受控样本验证的是实现边界，不代表生产事故根因准确率。生产事故集仍需要真实标签和处置结果，不能用模型自己生成的结论作为独立真值。

## 来源

- [Jaeger API 官方说明](https://www.jaegertracing.io/docs/2.2/apis/)：查询接口与 JSON/HTTP 数据访问。
- [Kubernetes Deployment 官方说明](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/)：ReplicaSet 与发布版本。
- [Kubernetes 服务访问说明](https://kubernetes.io/docs/tasks/access-application-cluster/access-cluster-services/)：通过集群 API 访问服务。
- [Kubernetes 标准注解说明](https://kubernetes.io/docs/reference/labels-annotations-taints/)：回滚复用 ReplicaSet 时的 revision-history 注解。
- 实现：`agents/alert_history.py`、`agents/alert_context.py`、`agents/alert_semantics.py`、`web_app/app.py`。
- 验收记录：`outputs/alert-context-20260915/`。发布目录 `/home/ubuntu/AgenticSRE-releases/context-20260915`；回滚目录 `/home/ubuntu/AgenticSRE-releases/semantic-20260915`。
