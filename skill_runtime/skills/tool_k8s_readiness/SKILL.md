---
name: K8s 就绪状态采集工具
description: 当告警涉及 Pod、Service、Deployment、Endpoint、发布状态、就绪探针、Kubernetes 事件、CrashLoopBackOff、Pending 或对象健康检查时，为 Agent 采集只读 Kubernetes 就绪证据。
---
# K8s 就绪状态采集工具

为 Agent 提供只读 Kubernetes 调查能力，采集场景诊断和恢复验证所需的对象状态、事件与就绪证据。

## 适用场景

- Pod 处于 `CrashLoopBackOff`、`Pending`、`ImagePullBackOff`、未 Ready 或频繁重启。
- Service Endpoints 为空，或流量无法到达 Ready Pod。
- Deployment rollout 卡住，期望副本与就绪副本不一致。
- 需要使用 Kubernetes 事件确认调度、镜像、探针、配额或挂载问题。
- Prometheus 告警引用 Kubernetes 对象，需要对象级证据确认。

本 Skill 是只读工具，不执行重启、扩缩容、patch、删除或回滚。

## 输入信号

- `namespace`: 目标命名空间，默认 `default`。
- `pod`、`deployment`、`service` 或 `object`: 可选目标对象。
- `selector`: 可选标签选择器。
- `time_window`: 可选事件或指标窗口。
- `agent_evidence`: 已有证据，用于避免重复采集。

## 执行阶段

- `detect`: 识别工作负载、就绪状态、Endpoint、rollout 或事件相关信号。
- `diagnose`: 生成命名空间限定的只读调查计划；有工具运行器时采集并总结 Pod、Deployment、Service 和事件事实。
- `verify`: 在恢复后重复只读检查，对比 Ready 副本、Endpoints、Warning 事件和原始告警状态。

本 Skill 不声明 `heal`。所有写操作必须交由独立恢复 Skill，并通过审批、风险和回滚门禁。

## 输出契约

- `commands`: 精确、只读且限定命名空间的命令。
- `target`: 命名空间和对象名称。
- `evidence`: 已采集事实或计划采集的证据源。
- `summary`: 中文单行调查结论。
- 任何输出中都不得包含写命令。

## 风险与门禁

- 仅允许 `get`、`describe`、`logs`、`top` 和只读事件查询。
- 禁止 `apply`、`patch`、`edit`、`delete`、`scale`、`rollout restart`、`rollout undo`、`cordon` 和 `drain`。
- 缺少命名空间时只检查 `default`，除非告警标签明确提供目标命名空间。
- kubeconfig 缺失时返回工具错误，不得声称已访问集群。

## 验证方式

- 所有命令尽可能限定命名空间和目标对象。
- 输出不包含任何写操作。
- 证据明确标注来源，例如 Pod 状态、Deployment 状态、Service Endpoints 或 Kubernetes 事件。
- 结果可以直接挂接到 Agent+Skill 调查时间线，并供下游场景 Skill 使用。
