# Agent+Skill 泛化诊断方法论

本文档定义 AgenticSRE 中 Agent+Skill 诊断、自愈闭环的通用设计约束。目标是让通算和智算故障都能按同一套机制扩展，而不是为每个实验场景堆叠 if/else 或使用注入标签作弊。

## 核心原则

Agent+Skill 链路必须遵循四段式流程：

1. **检测信号化**：检测层只把告警、指标、K8s Event、Trace 和日志转换为标准观测信号。
2. **LLM 假设生成**：Agent 使用当前上下文、历史规则、相似故障、近期 RCA/Skill 运行和经确认的历史诊断反馈生成候选根因假设。
3. **Skill 工具验证**：Skill 根据自身能力声明和观测信号执行 detect/diagnose/heal/verify，必要时调用受控工具采集证据。
4. **证据校准决策**：最终根因和自愈门禁由证据覆盖度、定位具体性、历史一致性和反证约束共同决定。

检测规则可以存在，但它只能回答“哪些信号异常、建议验证哪些方向”。它不能直接回答“根因是什么”。

## 输入分层

诊断输入分为四类，每类用途不同：

| 输入 | 示例 | 允许用途 | 禁止用途 |
| --- | --- | --- | --- |
| 观测事实 | Prometheus 指标、K8s Event、kubectl 输出、TELLER Trace | 生成候选信号、LLM 假设、Skill 验证、置信度校准 | 无 |
| 候选线索 | `observation_signals`、`candidate_skills`、`candidate_faults` | 提示 Agent/Runtime 优先验证哪些方向 | 直接作为最终根因 |
| 历史诊断上下文 | 历史规则、相似故障、专家确认的诊断正确/错误标签 | LLM 假设参考、置信度 History 因子 | 使用恢复成败代替诊断正确性；覆盖当前反证 |
| 历史恢复上下文 | 恢复成功/失败、人工恢复、代码修复 | 自动恢复安全门禁 | 进入 RCA 置信度或根因排序 |
| 实验审计字段 | `scenario_id`、`experiment_fault_type`、注入动作名 | 离线评估、实验报告、可追溯审计 | 假设生成、Skill 路由、置信度计算、根因输出 |

智算故障注入产生的事件必须以 blind diagnosis 方式进入分析：诊断开始时不能知道注入类型，只能看到运行期观测信号。

## 标准 payload 契约

事件进入 Agent+Skill 前应被规整为以下结构：

```json
{
  "domain": "llm",
  "namespace": "llm-k8s",
  "deployment": "vllm-server",
  "message": "运行期观测到推理服务异常",
  "evidence": [
    {"source": "prometheus", "signal": "node_cpu_usage", "value": 93.5, "threshold": 85}
  ],
  "observation": {
    "node_cpu_usage": 93.5,
    "waiting_requests": 2,
    "ttft_p95_seconds": 1.3
  },
  "observation_signals": [
    {
      "signal": "node_cpu_usage",
      "value": 93.5,
      "threshold": 85,
      "candidate_skill": "k8s.resource_pressure",
      "candidate_fault": "HighCPUUsage",
      "reason": "节点 CPU 使用率超过阈值"
    }
  ],
  "candidate_skills": ["k8s.resource_pressure"],
  "candidate_faults": ["HighCPUUsage"]
}
```

`candidate_skills` 是软提示。Runtime 可以跨 domain 纳入这些 Skill，但最终主 Skill 必须通过自己的 `detect` 和 `diagnose` 产出证据后才能被选中。

## LLM 假设生成

Agent 生成假设时必须使用以下上下文：

- 当前告警或智算事件的结构化 payload。
- 当前 evidence、observation、observation_signals。
- 历史规则 TopK。
- 相似故障 TopK。
- 近期 RCA/Skill 运行 TopK。
- 经确认的历史诊断统计，例如正确/错误样本数和正确率置信下界。

Prompt 必须明确：

- 输出中文根因假设。
- 每个假设包含 `expected_evidence`、`investigation_plan`、`supporting_evidence`。
- `observation_signals` 和 `candidate_skills` 只是待验证线索。
- 禁止使用 `scenario_id`、`experiment_fault_type`、注入动作名或测试标签。

如果 LLM 未配置、超时或失败，Agent+Skill 链路必须显式失败，不能退化为规则结果并伪装成智能体分析。

默认完整诊断把假设与 Skill 选择放在同一次模型调用中；Skill 随后执行取证，
第二次模型调用负责证据裁决。规划和调查仍有审计事件，但不要求各自增加模型
调用。旧协议缺少 `skill_calls` 时允许有界选择重试。优化目标是减少重复决策
和重复取证，同时保留有效假设、真实工具结果及模型裁决三项完成条件。

模型提出的对象名和标签同样需要验证。只读查询返回空匹配时，通用取证 Skill
可在请求 namespace 内发现实际 Pod 名称，再读取最多三个 Pod 的当前日志。
若超过这个调查范围仍无法取得证据，应明确说明不足。`detect=skipped` 必须使
同一次执行的 `diagnose` 跳过，不能依靠固定模板生成已确认故障。

## Skill 设计规范

Skill 不是某个实验场景的 if/else。一个合格 Skill 应该描述一类可复用能力：

- `k8s.resource_pressure`：识别 CPU、内存、磁盘、IO 等资源压力。
- `llm.gpu_resource_pressure`：识别 GPU 显存、KV Cache、GPU 利用率等资源压力。
- `llm.vllm_scheduler_queue`：识别 vLLM 队列堆积、TTFT/E2E 退化。
- `llm.model_serving_endpoint`：识别 Service Endpoint、HTTP 状态、模型 API 可用性。
- `llm.teller_request_trace_diagnosis`：识别请求级 Trace、CUDA Kernel、NVTX/CUPTI 事件瓶颈。

Skill 可以包含通用判据，例如“CPU 使用率超过配置阈值”“Endpoint 数量为 0”“KV Cache 超过阈值”。这些判据必须来自标准观测字段或配置中心，而不是实验标签。

Skill 禁止读取以下字段作为诊断依据：

- `scenario_id`
- `scenario_name`
- `experiment_fault_type`
- 注入动作名
- 注入层级
- 实验 ground truth

这些字段只能用于 UI 审计、实验报告和离线评估。

## 置信度方法

最终诊断置信度必须采用证据校准，而不是单条规则赋分。当前 Skill Runtime 使用：

```text
0.35 * raw_confidence
+ 0.35 * observed_evidence_factor
+ 0.20 * specificity_factor
+ 0.10 * history_factor
```

随后应用上限约束：

- 没有带来源的成功观测时，上限不超过 `0.60`。
- 只有一个观测来源时，上限不超过 `0.72`。
- 根因或组件定位不具体时降低上限。
- 经确认的历史诊断正确率置信下界低时降低上限。
- 桥接旧知识库的 Skill 设置更低上限。

告警文本、上游诊断摘要和 Skill 模板生成的证据句子只作为支持上下文，不能冒充已执行成功的 Prometheus、K8s、日志或 Trace 观测。上游 confidence 也不能通过取最大值进入新的 Skill 分数。

学术界的模型自评、重复采样一致性、SelfCheckGPT 和语义熵可以作为 Raw 或 Hypothesis 的附加信号，但不能替代外部运维证据。没有标注校准前，最终值仍是 `heuristic_support_score`，不是根因正确概率。

前端展示的置信度应同时展示方法、关键因子和上限原因，避免用户误以为它是 LLM 自评分或规则分数。

## 自愈门禁

自愈不由“诊断命中”直接触发。必须同时满足：

- 主 Skill 给出有效诊断。
- 校准置信度达到 Skill 的 `min_confidence_for_heal`。
- 证据数达到 `min_evidence_sources`。
- 风险策略允许执行。
- 高风险动作需要审批和回滚计划。
- 默认 dry-run，不允许直接执行真实变更。

全自动自愈需要更高置信度阈值，并且必须通过验证计划。恢复历史单独进入自动执行门禁：只统计同故障类型、同变更动作指纹和同集群/namespace/目标环境指纹下的已验证真实恢复结果。默认要求至少 `14` 个成功/失败样本，且恢复成功率的 95% 单侧 Clopper–Pearson 下界不低于 `0.80`，规则显式标记安全，无近期发布变更，非 `manual_only`，中高风险动作具备回滚操作。dry-run、验证结果未知和旧无指纹记录不能为全自动执行增信；没有合格恢复历史时允许诊断和人工建议，但禁止自动执行。

## 扩展新故障类型

新增故障不应修改核心 if/else。推荐流程：

1. 定义新的观测信号或复用已有标准字段。
2. 在配置中心声明阈值或检测窗口。
3. 增加或扩展一个通用 Skill，完善 `SKILL.md`、`skill.yaml`、`skill.py`。
4. 在 Skill detect 中基于标准观测字段判断是否匹配。
5. 在 diagnose 中输出中文根因、组件、证据、建议动作。
6. 增加 e2e 测试，验证无注入标签时仍能正确诊断。
7. 通过前端从故障注入、事件生成、Agent+Skill 分析、自愈建议、验证结果完整跑通。

## 当前已落地的护栏

- 智算事件诊断输入会过滤 `scenario_id`、`experiment_fault_type`、注入真值等字段。
- 故障实验事件统一转换为 `fault_experiment_signal`，不把真实注入类型作为 Skill 诊断输入。
- 观测字段只生成 `observation_signals` 和 `candidate_skills`。
- Runtime 支持候选 Skill 跨 domain 纳入验证，但最终主 Skill 仍按 detect/diagnose 证据选择。
- Agent+Skill 要求 LLM 参与假设生成；失败时显式失败。
- Skill 诊断置信度走 `skill_evidence_calibrated_v2` 校准方法。
- 专家反馈通过 Trace 保存故障类型和诊断正确/错误标签；恢复历史不参与诊断置信度。
- 自动执行使用恢复成功率的保守统计下界，小样本“全成功”不能直接视为安全。

## 验证基线

至少保留以下回归测试：

- 无注入标签，仅凭 `node_cpu_usage` 等观测字段，能路由到 `k8s.resource_pressure`。
- Service Endpoint 为空时，能路由到 `llm.model_serving_endpoint`。
- ImagePullBackOff、Unschedulable 等 K8s 事件不依赖实验标签即可诊断。
- GPU/KV Cache、vLLM 队列、TELLER Trace 等智算信号能触发对应 Skill。
- Agent+Skill 流程中能看到 LLM 假设、验证规划、只读调查、Skill 调用和校准置信度。
