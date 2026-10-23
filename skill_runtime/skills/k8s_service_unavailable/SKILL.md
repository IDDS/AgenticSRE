---
name: K8s Service 不可用
description: 当告警或 Agent 证据出现 HTTP 503、TargetDown、空 Endpoints、selector 不匹配、后端缩容为 0 或 Deployment 无可用副本时，诊断 ServiceUnavailable 并生成受控恢复计划。
---
# K8s Service 不可用

诊断 Kubernetes Service 无法把流量转发到健康后端的问题，并生成带审批和回滚约束的恢复方案。

## 适用场景

- HTTP 503、`ServiceUnavailable` 或业务探测失败指向 Kubernetes Service。
- Service 的 Endpoints 或 EndpointSlices 为空。
- Deployment 期望副本数大于就绪副本数。
- Deployment 被缩容为 0，或发布后没有 Ready Pod。
- Prometheus 报告 Service 抓取目标不可用，且 Kubernetes 证据指向 Service 或后端 Pod。

如果 Service Endpoints 健康而入口网关、网络策略、DNS、服务网格或下游依赖异常，应选择对应的专用 Skill。

## 输入信号

- `namespace`: 目标命名空间，默认 `default`。
- `service`: 受影响的 Service。
- `deployment`: 可能的后端 Deployment。
- `message`、`fault_type`、`root_cause` 或 `diagnosis`。
- `agent_evidence`: Agent 已采集的指标、日志、事件、对象快照和拓扑证据。

建议至少包含 Service/Endpoints、Deployment/Pod、近期事件和业务成功率中的两个独立证据源。

## 执行阶段

- `detect`: 只在 Service 不可用、503、无可用 Endpoints、selector 不匹配或后端缩容等信号明确时匹配。
- `diagnose`: 按 Endpoints 为空、后端缩容、发布未就绪、selector 不匹配和仅抓取异常的顺序识别根因。
- `heal`: 默认生成 dry-run 方案；仅在目标、动作、回滚和审批条件明确时提出扩容、重启、回滚或精确 patch。
- `verify`: 检查 Endpoints、Pod Ready、Deployment rollout、业务探测和原始告警是否恢复。

## 输出契约

- `detect.summary`: 中文命中或跳过原因。
- `diagnose.root_cause`: 中文根因。
- `diagnose.root_cause_component`: 精确的 Service、Deployment 或 Pod。
- `diagnose.evidence`: 带来源提示的具体事实。
- `heal.suggestions`: 带风险、审批要求和回滚命令的有序动作。
- `verify.checks`: 只读验证命令或指标检查项。

## 风险与门禁

- 默认 dry-run，真实变更必须显式审批。
- 变更前至少需要两个独立证据源，且校准后的诊断置信度不得低于 `0.85`；该阈值只是进入恢复门禁的必要条件，不表示恢复成功概率。
- 不猜测 Deployment、selector 或副本数；目标不明确时只输出调查命令。
- 禁止 delete、drain 和大范围标签修改。
- 只允许影响目标命名空间，并为每个写操作提供回滚命令。

## 验证方式

- Service Endpoints 或 EndpointSlices 非空。
- 后端 Pod Ready，Deployment rollout 完成。
- 业务探测或成功率恢复。
- 原始告警消退或明确持续下降。
- 任一验证失败时保持事件未关闭，并进入回滚或补充诊断流程。
