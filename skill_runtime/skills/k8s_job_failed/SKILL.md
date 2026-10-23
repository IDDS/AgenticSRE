---
name: Kubernetes Job 故障诊断
description: 针对明确的 Job 目标，使用同命名空间、控制器归属匹配的当前容器终止证据调查任务失败。
---
# Kubernetes Job 故障诊断

## 适用场景

明确指定 job/name 或结构化 job 字段的批处理任务调查。容器退出不等同于 Service 无 Endpoints；非零退出码也不能证明更上游的配置、依赖或业务根因。

## 输入信号

namespace、job 和 agent_evidence 中的 event_agent.evidence_items。终止证据必须包含当前非零整数 exit_code、Pod 名称、容器名、匹配命名空间，以及来自 controller ownerReference 的 job_name 和 job_uid。

## 执行阶段

- detect: 只接收明确的 Job 目标和 Job 调查类型，不抢占镜像拉取、OOM、CrashLoop 或 Service 等具体故障的既有路由。
- diagnose: 校验当前容器证据的 Job 归属，报告可证实的非零退出事实。缺失、无关或成功退出证据时输出 unknown，置信度为 0，不宣称恢复。
- verify: 生成限定命名空间和 Job 的只读核查命令。计划生成不表示检查已执行或恢复已验证。

## 输出契约

保留原始证据引用，输出 Job 目标、退出码及待补证说明。缺少匹配证据时 fault_type=unknown、confidence=0。verify 输出只读 checks、executed=false、recovery_verified=false。

## 风险与门禁

没有写操作或自主修复能力；禁止推断 Deployment 扩容、删除或重跑 Job。重新执行批处理任务可能有业务副作用，须单独决策。来自历史假设的退出码不能替代当前结构化观测。

## 验证方式

核查 Job 当前 conditions、失败与成功计数，以及所属 Pod 的 ownerReferences 和 containerStatuses。只有执行了实际检查且达到业务成功条件才能确认恢复，本 Skill 生成核查计划不等于完成验证。回归需覆盖不同命名空间、不同 Job、归属缺失、退出码 0 及没有实时证据的情况。
