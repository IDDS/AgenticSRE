# 恢复验证中的就绪证据修复

> 后续已完成累计发布，当前状态见 [发布验收记录](recovery-release-20260921.md)。下文保留本阶段验证时的状态。

## 范围与原因

沿外部系统调研中的恢复验证建议，检查 AgenticSRE 现有自愈链路。
发现命名空间检查仅排除几个状态字符串：空输出、`0/1 Running`、
`Terminating` 和未知状态可能被判为健康。单 Pod 检查还会忽略非字典
容器状态，将字符串 `false` 当作真值，并忽略 Ready=False 和删除时间。

本轮只修复上述已复现问题。修改 web_app/app.py 中两个就绪判断函数，
新增 tests/test_recovery_readiness_evidence.py；未修改自愈策略或集群资源。

## 行为

命名空间需要非空、可解析的 Pod 表格；Running 必须有正数容器且全部就绪，
Completed Job 保留终态成功语义，其他状态不通过。单 Pod 拒绝删除中对象、
缺失/非法容器证据、非布尔 ready，以及显式 Ready=False。
这仍是基础设施就绪判断，不证明业务指标恢复，也不证明根因已消除。

## 验证

全部运行在远端 snail，隔离容器基于正式镜像
`agenticsre:rca-acceptance-20260921`，修复通过只读源码挂载验证。
旧版新增用例为 **16 failed, 3 passed / 2.59 秒**。
修复后完整回归为 **980 passed / 208.42 秒**，33086 条依赖等警告，
没有失败用例。正式 Web 与 Skill Runtime 仍健康，未替换正式镜像。
用例覆盖空命名空间、部分容器就绪、未知/终止状态、非法容器结构、
Ready 条件与删除标记，并直接覆盖恢复验证调用路径。

只读采集实际 default 命名空间 18 个 Pod，逐个与 Kubernetes Ready 条件
对照，全部一致；命名空间包含既有 Pending 等异常，整体未通过健康判断。
这是实际状态核对，不是故障注入、自愈闭环或负载实验。

## 发布状态与后续

补丁已保存，正式容器未更新。本轮未提交或推送 GitCode。
还需独立实现业务错误率、延迟、下游状态的持续观察，以及知识版本绑定和
持久化执行接续；不能据本次检查宣称调研建议已全部落地。

证据目录：`outputs/recovery-readiness-20260921/`，远端对应
`/home/ubuntu/AgenticSRE-releases/recovery-readiness-20260921/`。
