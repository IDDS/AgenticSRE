# 自愈动作与恢复结果分离

> 后续已完成累计发布，当前状态见 [发布验收记录](recovery-release-20260921.md)。下文保留本阶段验证时的状态。

## 根因与修改

`/api/heal/execute` 过去只在 `verification.recovered == false` 时将动作成功
改为失败；恢复未知时仍返回 succeeded。网络故障删除后的验证器本已返回
business_probe_required，但顶层成功状态和绿色前端标记掩盖了这一限制。
另外，验证器异常会在保存运行记录之前退出请求，已执行动作的结果未被该路径保存。

当前行为：

- action_success 单独表示动作执行结果。
- 动作成功且验证明确为 verified/recovered=true 时保留 succeeded。
- 动作失败或明确未恢复返回 failed。
- 动作成功但证据未知、非终态或验证异常返回 verification_pending，success=null。
- 验证异常只返回稳定原因 verification_error，详细异常留在服务日志。
- 未知恢复结果不进入故障知识库的成功/失败样本；列表统计单列待验证。
- 前端使用黄色“动作已执行，恢复待验证”，不将其列入失败过滤结果。
- dry-run 保持原有语义。

success 新增 null 表示待定。外部消费者应读取 status 或按 `success is True`
判断成功，不能把 null 解释成确定失败。新增 pending 状态**没有后台自动重试
或自动业务探测**，也不代表所有执行入口已具备持久化工作流。

## 验证范围

测试在远端 snail 的隔离容器运行，基于现有正式镜像只读挂载修复源码。
API 回归覆盖明确恢复、明确未恢复、恢复未知、验证器异常，并检查保存后的
运行记录、统计和知识库写入。旧版四个参数用例失败，其中正常分支也因缺少
新增 action_success 字段失败，不能将四项都解释为旧版业务行为错误。

远端 Node 通过完整 app.js 语法检查，执行实际渲染函数核对中文标签、黄色徽标、
失败筛选和待验证统计。此项为渲染函数测试，未进行新的浏览器端到端验收。

完整回归 **983 passed / 207.82 秒**，33192 条依赖等警告，无失败；
包含上轮就绪判断修复和本轮 API 状态修复。结果已取回本地 full-suite.log。
正式 Web 和 Skill Runtime 均保持 healthy。本轮没有执行集群变更、真实业务
故障注入或生产部署；未提交或推送 GitCode。上轮只读核验 18 个真实 Pod 的
结果仍见 recovery-readiness-20260921.md，不能当作本轮自愈闭环实验。

证据：`outputs/recovery-outcome-20260921/`；远端同名目录位于
`/home/ubuntu/AgenticSRE-releases/`。

## 剩余工作

业务错误率、延迟及下游状态的持续探测；受控故障的完整恢复验收；统一知识与
Skill 版本；多副本持久化执行接续。这些均未由本轮状态处理修复完成。
