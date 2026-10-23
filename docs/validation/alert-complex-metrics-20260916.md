# 现网复杂告警规则的测量适配

## 实现及边界

本轮补齐上一版剩余十个复杂规则分组的测量查询。在 `agents/alert_metric_adapters.py` 中记录四个已审阅完整表达式及对应测量表达式、版本标识、测量含义。运行时精确匹配完整表达式，不按告警名称猜测，也不修改 Prometheus 规则。表达式变化（包括格式变化）不会继承旧映射，可重新审阅或显式配置 `agenticsre_metric_query`。现有显式注解优先。

| 规则 | 测量含义 | 保留条件 |
| --- | --- | --- |
| TargetDown | 不可达抓取目标百分比 | 原始分组与比值计算 |
| KubePodNotReady | Pending/Unknown 阶段测量值 | 原始聚合及非 Job owner 的向量关联 |
| etcdMembersDown | 原始复合规则比较左侧的值 | 保留不可达成员与 peer failure 的 `or` 分支；不称为单纯成员数量 |
| etcdInsufficientMembers | 可达成员数减去规则的动态阈值 | 原始两侧表达式及默认向量匹配；负值表示不足 |

这一选择依据 [Prometheus 运算符文档](https://prometheus.io/docs/prometheus/latest/querying/operators/) 中比较过滤、向量匹配和集合运算的语义：直接使用触发后的过滤结果会丢失未触发时刻，去掉整个复杂表达式内部比较则会改变原规则含义。这里只替换已审阅的最外层比较，内部聚合、比较、关联和集合运算保持原样；动态阈值使用两侧差值。

返回序列仍必须符合原告警保留标签和资源身份、唯一性、有限数值、时间网格及历史覆盖检查。规则添加或覆盖的标签不用于消除原测量序列歧义。原始查询、标签、规则、适配版本与测量含义均进入证据，不提升为因果确认。

另外一条没有资源身份的告警确认为 Watchdog，规则是 `vector(1)`。唯一健康 firing 规则匹配成功后，将这类无业务资源的恒定心跳标记为 `not_applicable`，保留原始告警与分组，不伪造资源或查询业务测量。未知的无身份规则仍是 unknown。总体证据状态保留 partial，另列 `not_applicable_groups`，不把心跳算作成功取证。

## 真实现网验收

所有测试在 snail 进行，候选服务使用独立数据目录和生产观测配置，只读访问原 Prometheus。

- 16 条原始告警全部保留；14 个 Prometheus 分组中，13 个适用分组取得证据，1 个 Watchdog 明确不适用。其他来源告警不冒充 Prometheus 指标证据。
- 13 个分组各 11 个历史点，查询窗口网格覆盖率 100%。与上一版相比，可用分组从 3 增至 13。
- 对 etcdInsufficientMembers 另行查询原规则左右两侧：可达成员 0、规则阈值 1，证据差值为 -1，独立数值检查通过。
- 13 个分组当前均为恒定序列，78 组两两统计检查全为 unknown，无 pass。取证成功不意味着有共变信息，更不意味着合并或抑制告警。
- 候选旧接口、异步压缩、分页和 Job Skill 检查通过，导入测试不改变生命周期。

## 证据与复现

远端目录 `/home/ubuntu/AgenticSRE-releases/complexmetrics-20260916/`，本地归档 `outputs/complexmetrics-20260916/`。`observed-rules.json` 保存本轮规则快照；`verify-complex-live.py` 只读采集并检查分组、序列与动态阈值，`complex-live-verification.json` 保存结果。验收脚本中的 13 个分组断言针对本次现网快照，规则或告警状态变化后应重新核对预期。

本轮覆盖的是这批实际规则，不是通用 PromQL 解析器；新版本规则、其他命名空间/资源映射、缺失历史仍须实际验证。亦不代表原架构中的所有自主根因验证方法已经完成。

## 完整回归

源代码提交 `caa5114`。远端完整套件 **725 passed**，耗时 107.62 秒。新增检查覆盖四种复杂表达式的证据来源、同名规则语义变化拒绝旧映射、显式注解优先和心跳原始记录保留。没有在本地运行测试或服务。

## 正式部署验收

已部署 `agenticsre:complexmetrics-20260916`，镜像 ID 为 `sha256:d2ddbb2f627a50d45a1af848bfbc8e6a341b601618d407ba6685c0b1b80cba04`。三个变更文件与正式容器逐项哈希一致。主服务与 Skill 服务健康，原有 25 条生命周期记录与部署前完全一致；旧接口、分页、异步压缩和 Job Skill 检查通过。远端 Chromium 页面 HTTP 200、加载 550 毫秒、无浏览器错误。

部署与验收脚本保留失败自动回退，回退版本为 `agenticsre:metriclabels-20260916`，本轮未触发回退。

正式容器再次通过 `verify-complex-live.py`，13 个适用分组、心跳分类和动态阈值核对结果保存在 `complex-production-verification.json`。本轮两个临时候选容器均已停止并移除。所有验证证据归档为 `verification-artifacts.tar.gz`，没有操作其他业务容器。
