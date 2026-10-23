---
name: K8s 资源配额耗尽
description: 检测、诊断 Kubernetes 命名空间 ResourceQuota 耗尽，并生成只读恢复指导。
---
# K8s ResourceQuota 超限诊断与只读建议

本 Skill 用于诊断命名空间 ResourceQuota 耗尽问题，并给出只读的容量与整改建议。

## 适用场景

- 创建资源时报错 `exceeded quota`。
- 命名空间 CPU、内存、存储或对象数配额达到上限。
- Pod、PVC、Service 等对象因配额不足无法创建。
- 需要评估配额使用情况以规划扩容。

## 输入信号

- `kubectl describe resourcequota -n <namespace>` 的 Used/Hard 对比。
- 资源创建失败事件中的配额拒绝信息。
- 命名空间内各类对象数量与资源请求总量。
- 相关工作负载的 requests/limits 配置。

## 执行阶段

- `detect`: 识别配额拒绝错误，确认为 ResourceQuota 超限。
- `diagnose`: 分析 Used/Hard 差距，定位是哪类资源或对象耗尽配额。
- `verify`: 建议采纳后复核配额使用是否回到安全水位。

本 Skill 不修改配额或工作负载；调整方案由独立恢复流程审批和执行。

## 风险与门禁

- 风险等级 low，仅只读诊断。
- `requires_approval=false`，`safe_auto_execute=true`。
- 至少需 2 个独立证据来源方可给出结论。
- 进入自愈门禁所需的最低校准诊断置信度为 0.78，自动执行只读建议门槛为 0.90；两者均不表示恢复成功概率。

## 验证方式

- ResourceQuota Used 低于 Hard，留有余量。
- 之前失败的资源创建可成功执行。
- 无新增配额拒绝事件。
