---
name: 通用故障检测
description: 在专用场景 Skill 接管之前，对故障文本、告警、指标、事件和症状进行
  通用信号匹配。
---
# 通用故障检测

本 Skill 作为通用信号匹配器，在场景专用 Skill 接管之前，对故障文本、告警、指标、事件与症状进行初筛。

## 适用场景

- 事件刚进入系统，尚未确定归属领域，需要快速初判。
- 需要从多源信号中提取症状关键词并归类。
- 为后续场景专用 Skill 提供路由与优先级提示。
- 需要在早期阶段过滤噪声告警。

不要把该 Skill 当作最终 RCA 使用。它只负责检测和路由，不能单独驱动变更。

## 触发方式

- **告警中心**: 作为 Skill Runtime 直接编排链路的前置检测能力。
- **智算事件中心**: 对 vLLM、GPU、推理延迟、Prometheus target 等事件做初筛。
- **故障实验**: 对实验输出和注入反馈做统一信号抽取。
- **Agent 工具调用**: 当前尚未接入 Agent 自主调用；未来接入后，Agent 应把该 Skill 作为低成本前置检测工具。

## 输入信号

- 原始故障文本、工单描述与告警标题。
- 指标异常项，包括名称、当前值和阈值。
- 集群或系统事件流。
- 症状标签与历史相似信号，可选。
- 推荐字段：`source`、`domain`、`fault_type`、`message`、`namespace`、`service`、`deployment`、`pod`、`node`。
- 智算相关字段：`vllm_endpoint`、`prometheus_url`、`ttft_p95_seconds`、`e2e_p95_seconds`、`gpu_count`、`gpu_utilization`。

## 执行阶段

- `detect`: 对输入信号做关键词与模式匹配，输出候选症状与疑似领域。

本 Skill 只负责检测和路由。诊断、自愈与恢复验证由后续场景 Skill 或通用组合 Skill 完成。

## 输出契约

- `detected`: 是否检测到故障信号。
- `domain_hint`: `k8s`、`llm`、`db` 或 `generic`。
- `fault_type_hint`: 归一化后的候选故障类型。
- `symptoms`: 命中的症状关键词和来源。
- `confidence`: 初筛置信度，只代表“需要进一步诊断”的把握，不代表根因确定。
- `next_skills`: 推荐后续 Skill，例如 `k8s.service_unavailable` 或 `llm.k8s_vllm_observability`。

## 示例 payload

```json
{
  "source": "alert_center",
  "message": "Alert TargetDown: vllm-server endpoint timeout, TTFT P95 increased",
  "namespace": "llm-k8s",
  "deployment": "vllm-server",
  "service": "vllm-server",
  "ttft_p95_seconds": 4.2
}
```

## 风险与门禁

- 风险等级 low，仅读取可观测性数据。
- `requires_approval=false`，`safe_auto_execute=true`。
- 至少 1 个证据来源即可产出候选。
- 仅做信号匹配与路由，不做最终根因结论，不触发变更。
- 输出不能绕过后续诊断 Skill 的置信度门禁。
- 即使完整闭环以 dry-run 方式运行，该 Skill 也只输出候选信号，不产生任何写操作。

## 验证方式

- 检查是否输出候选症状及疑似领域标签。
- 校验置信度是否与匹配强度一致。
- 确认未越界产出最终根因或执行动作。
- 对误报与漏报记录样本，供规则迭代。
- 在完整闭环 trace 中，应能看到场景专用 Skill 接管后续诊断或自愈。
