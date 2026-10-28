# 有界自动恢复编排

Web 宿主可在诊断持久化完成后触发自动恢复编排。`remediation.auto_loop_enabled` 默认为 `false`；启用时，两条诊断链的宿主工具均使用只读配置，多智能体跳过旧直接写入阶段，Agent+Skill 固定为 dry-run 检测和诊断，由统一对象动作路径处理。Agent+Skill 的通用动作门禁保持原状；针对缩零场景，编排可在独立审核、结构化证据和唯一配置策略均匹配时建立仅供内部调用的有限动作契约。

当前支持范围是服务端明确配置的单个 Deployment 缩零恢复。配置中的 cluster、namespace、Service、Deployment 和正整数副本数必须与诊断对象、唯一 Service 模板关联及规则动作一致。配置示例仅说明格式：

```yaml
remediation:
  auto_loop_enabled: true
  auto_scale_targets:
    - cluster_id: east
      namespace: shop
      service: gateway
      deployment: engine
      replicas: 1
```

诊断须在十分钟内启动并完成，已有独立审核和对象动作门禁须通过。编排检查完整观测、Service 与 Deployment 的身份和资源版本、实时 selector、当前零副本状态及唯一规则动作。Service 和 Deployment 的查询不是跨对象原子快照，结构关联也不单独证明根因。现有动作路径仍以原子 UID/resourceVersion 前置条件更新 Deployment；审批、配置开关、置信度、动作风险、同动作同环境同业务验证标准的真实历史门禁均不放宽。

系统在预演前持久记录每次诊断的处理声明，在提交动作前记录预演编号，并再次检查配置、时间和 Service 身份。重复调用返回原记录；进程中断或写入结果不明时要求核对，不自动重放。`POST /api/rca/{run_id}/auto-heal` 可触发同一受控过程，不能提交自定义命令或冒充审批；`GET` 查询保存的记录，不因开关关闭或诊断过期而隐藏。动作结果中的业务验证沿用现有恢复合同；集群健康本身不代表业务恢复。

本编排不覆盖任意工作负载和任意自愈动作，依赖服务端显式配置及有足够真实样本的自动门禁。进程中断后的未完成记录不会自动推进，查询会保留未完成状态；诊断过程页面提供只读记录与刷新，区分阻断、待验证和结果未知。受控回归不等于真实故障闭环验收；实际故障后自主恢复仍需验证。测试范围与限制见[验证记录](validation/guarded-auto-recovery-20261028.md)。
