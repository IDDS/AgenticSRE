---
name: 通用证据采集
description: 为 Agent+Skill 工作流构建只读证据包，适用于采集指标、日志、Trace、Kubernetes 事件、主机状态、数据库信号以及 LLM/vLLM 运行时指标。
---
# 通用证据采集

在诊断或恢复之前构建只读证据包。Agent 使用本 Skill 确定调查范围，把采集事实写入 `agent_evidence`，再调用场景诊断或恢复 Skill。

## 适用场景

- 告警证据不足，无法形成可信根因。
- 需要统一采集 Kubernetes、LLM、数据库、主机、指标、日志、事件或 Trace。
- 场景 Skill 要求至少两个独立证据源后才能进入恢复流程。
- 告警已消失，但仍需根据缓存或近期证据完成历史分析。

本 Skill 只读，不修改生产状态。

## 输入信号

- `domain`: `k8s`、`llm`、`db`、`host` 或 `generic`。
- `namespace`、`service`、`deployment`、`pod`、`container`、`node`、`host`。
- `alert_name`、`severity`、`source`、`message`、`labels`、`annotations`。
- `time_window`: 默认使用告警窗口或最近 10 分钟。
- `topology`: 可选的上下游依赖关系。
- `prometheus_url`、`log_query`、`trace_id` 或已有证据引用。

证据必须包含来源、目标、时间窗口、事实、原始引用以及对置信度的影响，不得伪造缺失数据。

## 执行阶段

- `detect`: 解析告警上下文，选择需要采集的证据维度和目标范围。
- `diagnose`: 执行或生成只读采集计划，构建带来源标签的证据包，并推荐后续场景 Skill。

本 Skill 只声明上述两个阶段。恢复和恢复验证由下游 Skill 执行；需要前后对比时，应在恢复后重新运行一次本 Skill，形成新的证据快照。

## 输出契约

- `evidence_bundle`: 按来源归一化的事实集合。
- `next_skills`: 按优先级排列的后续 Skill、理由和置信度。
- `commands` 或 `queries`: 只读采集动作。
- `missing_sources`: 不可用的证据源及其影响。
- `summary`: 可直接显示在 Agent+Skill 时间线中的中文摘要。

## 风险与门禁

- 仅允许 `observability.read`、`k8s.read` 和 `host.read`。
- 不调用、不生成写操作。
- 缺失证据必须明确记录，不能用推测替代事实。
- 查询必须限制时间窗口、命名空间和目标对象。
- 查询成本过高时先缩小范围。

## 验证方式

- 每条证据都具有来源标签和时间窗口。
- 下游诊断前至少存在一个具体证据源。
- 缺失数据明确可见。
- 输出可以直接挂接到 Agent 调查轨迹，且不包含任何写操作。
