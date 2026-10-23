# RCA 六因子置信度校准实现说明

## 1. 文档目的

本文档面向 AMS、华为 2012 实验室 SOW 评审人员以及 AgenticSRE 开发与运维人员，说明 RCA 结果中置信度的数据来源、大模型与规则脚本的责任边界、六因子计算方法、动态上限以及当前局限。

文档对应当前实现 `evidence_calibrated_v2`，核心代码位于 `orchestrator/confidence_calibrator.py`；Skill Runtime 的轻量路径为 `skill_evidence_calibrated_v2`。

## 2. 结论摘要

AgenticSRE 不直接把大模型自报的 confidence 作为最终置信度，而是采用“模型提出判断，外部证据与规则负责校准，弱证据和冲突触发硬上限”的工程方法。

本次修正明确了两个此前混淆的统计对象：

- 历史诊断正确性（History）只使用经确认的历史诊断正确/错误标签。
- 恢复成功/失败历史只用于自动执行门禁，不参与 RCA 根因置信度。

因此，某个故障很难自动恢复，不等于系统的根因诊断一定不正确；反过来，恢复动作偶然成功，也不能证明诊断正确。

当前六因子分为三类：

| 类型 | 因子 | 说明 |
|---|---|---|
| 大模型为主 | 校准前置信度（Raw） | 大模型在最终 RCA 报告中给出校准前 confidence，最终报告不可用时以 `0` 为弃答初始值 |
| 大模型与规则混合 | 假设分支稳定性（Hypothesis）、诊断质量评审（Quality） | 大模型生成候选分数或评审分数，脚本负责转换、融合和边界约束 |
| 规则或统计为主 | 多源证据充分性（Evidence）、根因具体性（Specificity）、历史诊断正确性（History） | 不让大模型直接给因子分，脚本根据证据数量、字段完整性和经确认的历史诊断标签计算 |

六因子最终加权、`[0, 1]` 裁剪和动态 cap 均由确定性代码执行，大模型无法绕过 cap。

## 3. 整体计算流程

```text
Prometheus / Logs / Traces / K8s Events / Skill 运行结果
                              │
                              ▼
                  多智能体证据收集与假设排序
                              │
                              ▼
                  LLM 生成 RCA + Judge + Critic
                              │
                              ▼
                  六因子确定性规则校准
                              │
                              ▼
                 证据/质量/诊断历史动态 cap
                              │
                              ▼
                confidence + confidence_explanation
```

加权公式为：

```text
Cweighted =
    0.18 × 校准前置信度（Raw）
  + 0.18 × 假设分支稳定性（Hypothesis）
  + 0.27 × 多源证据充分性（Evidence）
  + 0.17 × 诊断质量评审（Quality）
  + 0.10 × 根因具体性（Specificity）
  + 0.10 × 历史诊断正确性（History）

Cfinal = min(Cweighted, DynamicCap)
```

上述是 `configs/config.yaml > confidence_calibration` 中的默认工程权重。权重、多源证据充分性（Evidence）子权重、历史样本门槛和动态 cap 可由运维人员调整，运行时会自动校验并归一化。它们仍然不是从标注数据中训练得到的概率模型参数。因此，例如 `0.865` 应解释为“证据校准后的 RCA 操作置信指数”，不应直接解释为“根因有 86.5% 的统计概率正确”。输出中的 `confidence_kind=heuristic_support_score` 明确表达这一边界。

## 4. 校准前置信度（Raw）

### 4.1 数据来源

`orchestrator/rca_engine.py` 要求大模型输出结构化 RCA JSON，其中包含 `confidence`。在协作 Critic 启用时，Critic 可能先要求改写报告或调整 confidence，六因子校准器随后才将该值记录为 `raw_confidence`。

因此，校准前置信度（Raw）的严格含义是“六因子校准前置信度”，不一定是大模型第一次输出的值。

### 4.2 降级方式

如果最终大模型报告不可用、JSON 无效或根因为空：

- 不采用 Top1 假设描述或分数作为最终根因，降级初始 `confidence=0`、`root_entity=null`，仅按明确请求保留 `incident_target`。
- 降级报告明确说明根因未完成模型核验；保留实际观测和候选供人工复核，不保存 SDK 异常正文。
- 后续六因子校准仍可能改变数值；模型完整性门禁将最终值限制在 `0.65` 以内，并要求人工复核、阻断自动学习和恢复。降级初始值不能当作所有后续路径最终分数均为零的保证。

失败调用、超时、响应截断及高分 Judge 不能覆盖未完成状态的契约见[诊断策略说明](diagnostic-strategy.md#模型调用失败与报告弃答)；本次发布范围见[发布核验](validation/agentic-sre-release-20261022.md)。

## 5. 假设分支稳定性（Hypothesis）

### 5.1 上游分数来源

假设 confidence 有两类来源：

- 通用场景由大模型生成 3–5 个根因假设及初始 confidence。
- NetworkChaos、Deployment 缩容到 0、ImagePull、CrashLoopBackOff 等高信号场景使用 `HypothesisAgent._fast_generate()` 中的确定性快速规则。

采集到新证据后，系统通常调用大模型重新评估现有假设，并在证据确实无法被现有分支解释时补充新分支。

### 5.2 因子公式

```text
margin = max(0, Top1 - Top2)

Hypothesis =
    0.75 × Top1
  + 0.25 × min(1, 2 × margin)
```

该因子同时考虑最佳假设自身强度与领先幅度。当 Top1 和 Top2 非常接近时，系统不会因为 Top1 绝对分数较高就认为分支已经稳定。

如果未启用假设阶段，则使用校准前置信度（Raw）作为该因子的先验。

## 6. 多源证据充分性（Evidence）

多源证据充分性（Evidence）因子由确定性规则计算：

```text
coverage = min(1, 成功证据 Agent 数 / 4)

signal = min(1, 结构化证据严重度总分 / 3)

alignment = min(1, RCA 结论与证据的关键词命中数 / 5)

Evidence =
    0.45 × coverage
  + 0.35 × signal
  + 0.20 × alignment
```

结构化证据严重度权重为：

| 级别 | 权重 |
|---|---:|
| critical | 1.00 |
| error | 0.90 |
| warning / warn | 0.65 |
| info | 0.35 |

成功证据 Agent 指不带 `error` 且输出了 `summary` 或 `evidence_items` 的证据条目。Metric、Log、Trace 和 Event Agent 通过 Prometheus、Elasticsearch/容器日志、Jaeger 和 Kubernetes API 获取真实观测，其中统计异常检测和资源状态解析是脚本，文本摘要可由大模型生成。

因此，多源证据充分性（Evidence）是“规则打分、混合输入”，不是大模型直接自评。

实现注意点：

- 当前“独立证据源”是指不同 Agent/观测通道，不等价于统计意义上完全独立的样本。
- 实现层面按 `evidence` 字典条目名计数，除明确排除的 `structured_evidence`、`correlation`、`graph_rca` 和 `remediation` 外，其他带摘要的上下文条目也可能进入计数。
- `support_count` 当前用于审计展示，不直接加到 Evidence 分数中。
- `contradict_count > 0` 会触发最终置信度 `0.70` 上限。

## 7. 诊断质量评审（Quality）：Judge 与 Critic

```text
Quality = 0.65 × JudgeScore + 0.35 × CriticScore
```

### 7.1 RCAJudge

RCAJudge 同时执行规则评价和可选的大模型评价。当前默认 `judge_llm_weight=0.4`：

```text
JudgeScore =
    0.60 × RuleJudge
  + 0.40 × (1 - LLMJudgeLevel / 3)
```

RuleJudge 使用：

- 高、中、低确定性表达模式。
- 推理文本长度。
- 当前校准前 confidence。
- sigmoid 归一化。

LLMJudge 要求大模型评价逻辑连贯性、证据质量、根因具体性和可执行性。当前融合主要使用大模型返回的 `judge_level`，而不是直接对四个子分数加权。

### 7.2 Critic

Critic 读取完整 RCA、证据摘要和协作策略，输出：

- `quality_score`
- `needs_revision`
- `needs_human_review`
- 弱点、缺失证据、备选假设和建议补充方向

正常情况下 Critic 由大模型执行；大模型不可用时，系统根据根因长度、证据 Agent 数、fault type 完整性等进行规则降级。

在 Judge 和 Critic 的大模型调用都成功时，诊断质量评审（Quality）的有效构成约为：

```text
39% RuleJudge + 26% LLMJudge + 35% LLMCritic
```

## 8. 根因具体性（Specificity）规则

根因具体性（Specificity）不让大模型自己给分，而是对 RCA 字段进行确定性检查：

| 条件 | 加分 |
|---|---:|
| 基础分 | 0.25 |
| `root_cause` 长度不少于 30 个字符 | +0.25 |
| `fault_type` 存在且不是 `unknown` | +0.20 |
| `affected_services` 非空 | +0.15 |
| 根因和类型不包含“无法确定”、“证据不足”等模糊表达 | +0.15 |

最高值为 `1.00`。当根因具体性（Specificity）低于 `0.55` 时，最终置信度上限为 `0.65`。

该因子是“脚本评分、大模型提供被检查字段”。

## 9. 历史诊断正确性（History）

历史诊断正确性（History）只读取 `ExpertFeedbackStore` 中带明确 `correct=true/false` 标签的历史诊断反馈，并按 `fault_type` 或 `error_pattern` 精确匹配。提交专家反馈时，系统从对应 RCA Trace 保存当时的故障类型、根因和集群标识，再由监督比较结果形成正确/错误标签。

```text
无确认标签：历史诊断正确性（History）= 0.50
确认标签少于 5：历史诊断正确性（History）= 0.50
确认标签不少于 5：历史诊断正确性（History）= 诊断正确率的 95% 单侧 Clopper–Pearson 下界
```

设同类历史诊断共有 `n` 条，其中 `x` 条确认正确，置信水平为 `1-α`。单侧下界 `L` 满足：

```text
Σ[k=x..n] C(n,k) · L^k · (1-L)^(n-k) = α
```

对任意真实正确率 `p`，Clopper–Pearson 构造具有保守覆盖性质：

```text
Pp(L(X) ≤ p) ≥ 1-α
```

当前默认 `α=0.05`。例如全部样本都正确时，`L=α^(1/n)`：`5/5` 的下界只有约 `0.549`，`13/13` 约 `0.794`，到 `14/14` 才约 `0.807`。这避免了小样本“表面 100%”造成过度增信。

如果样本充分但诊断正确率下界不高于 `0.30`，最终置信度上限为 `0.65`。恢复成功率、恢复失败、人工恢复和代码修复次数均不会改变该因子；它们只在第 11 节的自动恢复门禁中使用。

## 10. 动态置信度上限

| 触发条件 | 最终上限 |
|---|---:|
| 默认上限 | 0.95 |
| 成功证据 Agent 不超过 1 个 | 0.72 |
| 没有结构化证据且证据 Agent 少于 3 个 | 0.78 |
| 存在 `contradicts` 反证 | 0.70 |
| RCAJudge 或 Critic 要求人工复核 | 0.75 |
| Specificity 低于 0.55 | 0.65 |
| 历史诊断标签充分但正确率置信下界不高于 0.30 | 0.65 |

当多个条件同时命中时，取最低上限。因此即使 Raw、Judge 或 Critic 的大模型分数很高，也不能绕过证据不足或证据冲突的硬限制。

## 11. 自动恢复门禁：独立使用恢复历史

自动恢复使用 `FaultContextStore.resolution_stats()` 返回的 `history_kind=recovery_outcome`，不从诊断 History 反推恢复能力。默认必须同时满足：

1. RCA 校准置信度不低于 `0.90`。
2. 只使用“同故障类型 + 同恢复动作指纹 + 同环境指纹”的真实、已验证恢复结果，且有成功/失败原始计数。
3. 历史恢复成功率的 95% 单侧 Clopper–Pearson 下界不低于 `0.80`。
4. 自愈规则显式标记 `safe_auto_execute`、`safe_rerun` 或 `allow_auto_execute`。
5. 故障未标记 `manual_only`，且 `auto_recovery_supported` 不是 `false`。
6. 未检测到近期代码或发布变更。
7. 动作风险不超过配置上限；中高风险动作必须具备回滚操作。

恢复动作指纹只对实际变更命令建模，`kubectl get/describe/logs/top` 和 `rollout status` 等只读检查不会把同一恢复动作切分成不同历史簇。环境指纹由集群、namespace、恢复目标和可用的工作负载版本字段组成。只有 `dry_run=false` 且恢复验证明确返回 `recovered=true/false` 的运行才入库；dry-run、验证结果未知和旧的无指纹记录都不能为全自动执行增信。

默认最低恢复样本门槛已与 `0.80` 的统计下界口径对齐为 `14`。当 14 条样本全部成功时，95% 单侧 Clopper–Pearson 下界约为 `0.807`；`13/13` 仍只有约 `0.794`。样本存在失败时，通过门禁所需的总样本数会更高。

没有诊断历史时，RCA History 取中性值 `0.5`；没有符合动作与环境指纹的恢复历史时，仍可继续输出 RCA、建议和人工操作步骤，但全自动执行门禁必定阻断。对 NodeNotReady、控制面故障、配置缺失、存储挂载失败和安全策略不兼容等默认难以安全自动恢复的类型，规则显式标记 `manual_only`：诊断置信度仍可由证据正常计算，但不允许把“根因诊断正确”等同于“可安全自动修复”。

## 12. 可审计输出

校准后的 RCA 结果保留 `confidence_raw`，并输出 `confidence_explanation`：

```json
{
  "method": "evidence_calibrated_v2",
  "confidence_kind": "heuristic_support_score",
  "profile": "configurable_v2",
  "formula": "0.1800*raw + 0.1800*hypothesis + 0.2700*evidence + 0.1700*quality + 0.1000*specificity + 0.1000*history, then apply configured evidence/quality/history caps",
  "raw_confidence": 0.90,
  "pre_cap_confidence": 0.873,
  "calibrated_confidence": 0.873,
  "delta": -0.027,
  "factors": {
    "hypothesis": 0.978,
    "evidence": 0.883,
    "quality": 0.86,
    "specificity": 1.0,
    "history": 0.5
  },
  "weights": {
    "raw": 0.18,
    "hypothesis": 0.18,
    "evidence": 0.27,
    "quality": 0.17,
    "specificity": 0.10,
    "history": 0.10
  },
  "contributions": {
    "raw": 0.162,
    "hypothesis": 0.176,
    "evidence": 0.2384,
    "quality": 0.1462,
    "specificity": 0.10,
    "history": 0.05
  },
  "hypothesis": {
    "top": 0.97,
    "margin": 0.55,
    "count": 2
  },
  "evidence": {
    "successful_agents": ["event_agent", "log_agent", "metric_agent", "trace_agent"],
    "agent_count": 4,
    "structured_count": 2,
    "support_count": 2,
    "contradict_count": 0,
    "coverage_score": 1.0,
    "signal_score": 0.667,
    "alignment_score": 1.0
  },
  "quality": {
    "judge_score": 0.86,
    "critic_score": 0.86
  },
  "history": {
    "history_kind": "diagnosis_correctness",
    "diagnosis_sample_count": 0,
    "diagnosis_correct_count": 0,
    "diagnosis_accuracy": null,
    "diagnosis_accuracy_lower_bound": null,
    "sufficient_samples": false,
    "used_for_diagnosis": false
  },
  "cap": 0.95,
  "cap_triggers": [],
  "reasons": [],
  "limitations": [],
  "caps": []
}
```

注：上述 JSON 仅展示字段结构，各分数应从真实 RCA 运行结果中读取，不应手工填写为审计记录。

## 13. 演示页数值复算注意事项

如果演示页假设 `Raw=0.90`、`Hypothesis=0.97`、`Evidence=0.88`、`Quality=0.86`、`Specificity=0.95`、`History=0.50`，则手工加权结果确实为：

```text
0.18×0.90 + 0.18×0.97 + 0.27×0.88
+ 0.17×0.86 + 0.10×0.95 + 0.10×0.50
= 0.8654 ≈ 0.865
```

但该数组是说明性示例，不是当前代码的严格复现：

1. 如果 `Top1=0.97`、`Top2=0.42`，当前 Hypothesis 公式结果为 `0.9775`，而不是直接取 `0.97`。
2. 当前六因子 Specificity 采用 `0.25 + 0.25 + 0.20 + 0.15 + 0.15` 的离散加分方式，全部满足时是 `1.00`，不会产生 `0.95`。
3. `Quality=0.86` 需要同时给出 `judge_score` 和 `critic_score` 才能审计复算。
4. `Evidence=0.88` 需要成功 Agent 数、结构化证据严重度和关键词对齐结果，只有一句证据说明无法严格复算。

正式展示时建议二选一：

- 将页面标注为“原理示意数值”。
- 直接从 `confidence_explanation` 提取真实运行数据，保证可复算。

六因子置信度不应与 Skill Runtime 的四因子 `skill_evidence_calibrated_v2` 示例数值混用。后者使用 `0.35*raw + 0.35*observed_evidence + 0.20*specificity + 0.10*diagnosis_history`，是独立的轻量校准路径。告警文本、Skill 模板生成的证据句子和上游诊断摘要只能作为支持上下文；只有 payload 中带明确来源的成功观测或实际工具执行记录才计入 `observed_evidence`。诊断型 Skill 收到的旧 confidence 会记录为审计字段，但不会通过 `max()` 抬高新分数。唯一例外是带 `approved_fault_transaction_undo` 授权依据的强类型事务 Undo：它消费已经校准的诊断作为恢复门禁，不生成新的 RCA 分数。

## 14. 大模型推理置信度研究与本项目的借用方式

学术界并不存在一个可以直接从任意大模型回答中读取“真实正确概率”的通用接口。现有路线主要分为以下几类：

| 研究路线 | 代表工作 | 可借用内容 | 在 AgenticSRE 中的边界 |
|---|---|---|---|
| 模型自评 | Kadavath et al., *Language Models (Mostly) Know What They Know*（2022，arXiv 预印本） | 用 `P(True)` 或“我是否知道”作为自评信号 | 只作为 Raw；论文也报告跨任务 `P(IK)` 校准困难，不能单独作为生产门禁 |
| 语言化置信度与多次采样 | Xiong et al., *Can LLMs Express Their Uncertainty?*（2023，arXiv 预印本） | 提示模型表达置信度，并用多响应一致性缓解过度自信 | 可作为 Hypothesis 的附加信号；专业领域和困难任务仍可能失效 |
| 黑盒一致性检查 | Manakul et al., *SelfCheckGPT*（EMNLP 2023，已发表会议论文） | 多次采样发生分歧时降低事实可信度 | 可用于离线回放或高风险 RCA 复核；同一模型的相关错误不等于独立证据 |
| 概率后校准 | Guo et al., *On Calibration of Modern Neural Networks*（ICML 2017，已发表会议论文） | 用标注验证集做 temperature scaling 等后校准 | 等积累足够专家标签后，可按故障域校准最终分数 |
| Proper scoring rules | Brier（1950，已发表期刊论文）；Gneiting & Raftery（2007，已发表期刊论文） | 用 Brier Score、log score 等评价概率预测是否诚实、可校准 | 用于离线评估；不能凭工程公式本身宣称已校准 |
| Conformal prediction | Angelopoulos & Bates（2021，arXiv 教程）；Ren et al. KnowNo（CoRL 2023，已发表会议论文） | 在交换性等条件下构造有限样本覆盖保证，并在不确定时请求人工帮助 | 适合未来实现“候选根因集合 + 拒答/转人工”，而不是把单个启发式分数包装成概率保证 |

当前实现借用了上述研究的结构，而不是直接复制某篇论文的分数：LLM 自评进入 Raw，候选分支稳定性进入 Hypothesis，真实 telemetry 与工具结果进入 Evidence，专家确认标签进入 History，不满足安全门槛时选择人工复核或拒绝自动恢复。SelfCheckGPT 式多次采样和语义熵尚未默认启用，因为它们增加推理成本，而且多次采样仍可能产生高度相关的系统性错误。

原始来源：

- [Brier, Verification of Forecasts Expressed in Terms of Probability, Monthly Weather Review, 1950](https://doi.org/10.1175/1520-0493(1950)078%3C0001:VOFEIT%3E2.0.CO;2)
- [Gneiting & Raftery, Strictly Proper Scoring Rules, Prediction, and Estimation, JASA, 2007](https://doi.org/10.1198/016214506000001437)
- [Guo et al., On Calibration of Modern Neural Networks, ICML 2017](https://proceedings.mlr.press/v70/guo17a.html)
- [Kadavath et al., Language Models (Mostly) Know What They Know, arXiv:2207.05221](https://arxiv.org/abs/2207.05221)
- [Xiong et al., Can LLMs Express Their Uncertainty?, arXiv:2306.13063](https://arxiv.org/abs/2306.13063)
- [Manakul et al., SelfCheckGPT, EMNLP 2023](https://aclanthology.org/2023.emnlp-main.557/)
- [Clopper & Pearson, The Use of Confidence or Fiducial Limits Illustrated in the Case of the Binomial, Biometrika, 1934](https://doi.org/10.1093/biomet/26.4.404)
- [Angelopoulos & Bates, A Gentle Introduction to Conformal Prediction and Distribution-Free Uncertainty Quantification, arXiv:2107.07511](https://arxiv.org/abs/2107.07511)
- [Ren et al., Robots That Ask For Help: Uncertainty Alignment for Large Language Model Planners, CoRL 2023](https://proceedings.mlr.press/v229/ren23a.html)

## 15. 哪些结论有数学保证

| 对象 | 当前是否有保证 | 保证与前提 |
|---|---|---|
| 六因子/四因子最终分数 | 否 | 是确定、可复算的工程支持指数；未经过足量标注校准前不能解释为根因正确概率 |
| Clopper–Pearson 历史比例下界 | 是 | 对二项计数给出保守的频率学覆盖保证；样本需属于定义一致的同类总体，分布漂移会削弱业务解释 |
| DynamicCap 和自动恢复门禁 | 有确定性软件行为保证 | 相同输入和配置产生相同门禁结果；不等同于统计正确性保证 |
| 未来 conformal 根因集合 | 尚未实现 | 若校准集与线上样本满足交换性，可提供边际覆盖保证；不能自动推出每个故障子群都满足条件覆盖 |

结论是：当前可以数学上证明 Clopper–Pearson 下界算法及门禁逻辑的性质，但不能证明整个 RCA 启发式分数就是正确概率。要达到概率口径，必须积累专家确认标签，按故障域划分训练/校准/测试集，报告可靠性曲线、Brier Score、ECE 及置信区间，并监控分布漂移。

## 16. 可复现实验与质量门

仓库回归测试覆盖以下性质：

1. 枚举 `n=1..30` 和 `p=0.00..1.00` 的二项分布网格，验证实现的 95% Clopper–Pearson 下界覆盖率不低于 95%。
2. 验证 `5/5` 下界约为 `0.549`、`13/13<0.80`、`14/14>0.80`。
3. 验证恢复历史从高成功率改为低成功率时，RCA 诊断置信度保持不变。
4. 验证专家确认的历史诊断正确率下界会调整 History，并在下界过低时触发 cap。
5. 验证上游 confidence 不会通过 `max()` 污染 Skill 新分数，告警文本和模板证据不冒充成功观测。
6. 验证无恢复计数、小样本、人工恢复标记、近期发布变更和缺少回滚操作都会阻断自动执行。
7. 验证门禁文案保留足够小数精度，不会把 `0.898 < 0.900` 显示成容易误解的“90% 未达到 90%”。
8. 验证恢复统计只匹配同动作指纹和同环境指纹，不同命令、不同集群和旧无指纹记录不能混入全自动样本。
9. 验证只读检查命令不改变恢复动作指纹，且默认 `14` 条样本门槛与 `14/14` 的统计下界一致。
10. 验证难恢复故障可以通过诊断门禁并输出人工建议，但 `manual_only` 阻断全自动执行；显式请求全自动时在运行命令前执行门禁，原有人工审批路径保持兼容。

执行命令：

```bash
pytest -q tests/e2e/test_confidence_calibration.py \
  tests/e2e/test_skill_runtime.py \
  tests/e2e/test_agent_skill_orchestration.py \
  tests/e2e/test_ai_event_center.py
```

在合并或发布前还应执行主项目全量 `pytest -q tests`。具体执行日期和结果应以提交记录及 CI 日志为准，不把一次本地结果当成永久保证。

2026-08-06 本次修正的提交前验证结果：

| 验证 | 结果 |
|---|---|
| 主项目全量 `PYTHONPATH=. pytest -q tests` | `413 passed, 2 warnings` |
| Clopper–Pearson 数值网格，`n=1..30`、`p` 步长 `0.001` | 最小覆盖率 `0.950001868`，不低于名义 `0.95` |
| 全成功样本下界 | `5/5=0.549280272`；`13/13=0.794183335`；`14/14=0.807363824` |

同日还启动了真实 Dashboard（`127.0.0.1:8080`）和 Skill Runtime（`127.0.0.1:8090`）进程，通过 HTTP 而不是 `TestClient` 执行了一次端到端 dry-run：

| 链路环节 | 实测结果 |
|---|---|
| Dashboard → Skill Runtime | `dashboard_bridge.mode=http_proxy`，工作流状态 `completed`，命中 `k8s.service_unavailable` |
| 置信度 | 输入的上游自报值 `0.99` 仅作审计记录；Metric、Event、Trace 三类观测经 `skill_evidence_calibrated_v2` 计算为 `0.898` |
| 历史边界 | `history_kind=diagnosis_correctness`，当前确认诊断样本为 `0`；恢复样本为 `0`，未参与诊断打分 |
| 诊断门禁 | `0.898 >= 0.85`，允许输出恢复建议 |
| 全自动门禁 | 正确阻断：`0.898 < 0.900`、恢复历史不足、规则未标记全自动安全、回滚条件不完整 |
| 恢复执行 | 6 条命令全部以 `dry_run=true` 返回成功，其中 3 条变更动作生成了稳定动作指纹；运行记录明确标记 `recovery_history_record.recorded=false, reason=dry_run`，不对集群执行真实变更，也不污染恢复成功率 |
| 持久化回查 | Workflow `skillwf-ui-d5012223b3` 和 Heal Run `heal-e0854efb0e` 均可通过查询 API 读回，后者状态为 `dry_run` |
| Dashboard 页面 | HTTP `200`，`Content-Type: text/html; charset=utf-8` |

该端到端试验证明代码的路由、字段传递、门禁和持久化行为在当前环境中一致；它不代替连接真实 Kubernetes 集群的变更执行和恢复验证，因为本次有意使用 dry-run 避免产生资源变更。

### 16.1 远端真实故障注入验证（2026-08-06）

同日进一步在远端 `snail/lsy-0` 控制节点连接的四节点 Kubernetes 集群上执行了真实故障注入。测试使用隔离 namespace `agenticsre-diag-e2e-0806`，请求只包含业务症状、namespace 和资源坐标，脚本明确拒绝 `fault_type`、`root_cause`、预填 confidence、候选故障、预填证据和实验场景 ID 等真值泄漏字段。

| 场景与链路 | 远端观测与最终结果 |
|---|---|
| Deployment 缩容为 0，多智能体 | `rca-7cd3953c`，根因明确为期望副本数为 0，导致 Pod 终止、Service 无就绪端点；Raw `0.98` 经人工复核 cap 校准为 `0.75` |
| Deployment 缩容为 0，Agent+Skill | `rca-ac83439f`，6 条只读命令成功，主 Skill `k8s.service_unavailable`；结构化证据 6 条，最终置信度 `0.806` |
| 不存在镜像标签，多智能体 | `rca-227726a3`，真实 Pod 状态为 `ImagePullBackOff`，根因定位到 `nginx:agenticsre-missing-live-diagnosis-tag` 不存在或不可访问；置信度受人工复核 cap 限制为 `0.75` |
| 不存在镜像标签，Agent+Skill | `rca-b319b887`，主 Skill `k8s.image_pull_error`，最终置信度 `0.809`，LLM reasoning 与只读 `kubectl` 证据一致 |

这次真实测试证明 History 在没有专家确认标签时仍取中性值 `0.5`：四条结果的 `confidence_explanation.history` 均显示 `diagnosis_sample_count=0`、`used_for_diagnosis=false`，恢复成功历史没有进入根因置信度。多智能体两条结果因 Judge/Critic 要求人工复核，尽管 Raw 较高，最终都被确定性限制在 `0.75`；Agent+Skill 路径在补齐真实命令结构化证据后，不再因“缺少结构化证据”被错误压低到 `0.341`。

远端流程还暴露并修复了单元模拟未覆盖的整链路问题：已删除 Pod 的旧 Warning 事件覆盖当前状态、被取消的同步 LLM 请求拖住 `asyncio.run()` 退出、最终报告与已确认 Top1 冲突、Agent+Skill 推理输入截断命令输出、LLM 因“证据已充分”省略必需 Skill 调用，以及明确 `ImagePullBackOff` 被泛化为 Service/Deployment 故障。修复后，当前 Deployment/Endpoint/Pod 状态优先于旧事件和生成文本；命令输出以紧凑结构进入 reasoning；具体运行态优先选择最具体 Skill 和故障类型。

恢复验证不是用于增加上述诊断置信度，而是单独验证操作闭环：缩容场景恢复到 `Deployment 1/1`、Service Endpoints 恢复且业务探针成功；镜像场景改回 `nginx:1.27-alpine` 后 rollout 达到 `1/1`。测试 namespace 随后按 owner 标签校验后删除。收尾检查显示 `lsy-1` 至 `lsy-4` 全部 `Ready`，`kube-system` 没有非 Running Pod。

### 16.2 最终工作树版本远端闭环复验（2026-08-06）

为确认最后的实时证据对齐修正没有回归，在不停止或修改生产 `8080/8090` 服务的前提下，将待提交工作树同步到远端隔离副本，使用临时 `18080/18090` 再次从头执行两类真实故障的双路径诊断。驱动运行 ID 为 `live-diag-bfa12c41f5`，namespace 为 `agenticsre-diag-e2e-0806-final`，请求真值泄漏防护仍然开启。

| 场景与链路 | 最终远端结果 |
|---|---|
| Deployment 缩容为 0，多智能体 | `rca-b06e6559`，识别期望副本数为 0、无 Pod、Endpoints 为空；`service_disruption`，置信度 `0.75`，耗时 `323.52s` |
| Deployment 缩容为 0，Agent+Skill | `rca-4de72f55`，主 Skill `k8s.service_unavailable`，`ServiceUnavailable`，置信度 `0.814`，耗时 `140.37s` |
| 不存在镜像标签，多智能体 | `rca-6f8922d6`，当前 `pod_waiting_state` 将历史 `replicas=0` Top1 纠正为 `ImagePullBackOff/ErrImagePull`；`image_pull_error`，置信度 `0.75`，耗时 `311.92s` |
| 不存在镜像标签，Agent+Skill | `rca-7552a55e`，主 Skill `k8s.image_pull_error`，`ImagePullError`，置信度 `0.723`，耗时 `115.364s` |

首次最终复验在缩容 Agent+Skill 路径中捕获到真实回归：LLM 根因已正确识别 `replicas=0`，但 Skill 类型被历史 ImagePull 假设污染为 `ImagePullError`。修正后，专项 Skill 覆盖只依据当前成功的只读命令输出，近期运行历史按 cluster 与 namespace 双重隔离，最终故障类型再与已被证据证实的根因进行一致性校验。多智能体单次诊断曾因质量闭环耗时 `411.94s`，验收驱动的可配置等待上限因此从固定 `360s` 调整为默认 `720s`，避免在 API 即将完成时误报失败。

最终驱动输出对两个场景均记录 `recovery_verified=true`，并记录 `cleanup_verified=true`。收尾时 namespace 已不存在，临时 `18080/18090` 进程已停止，四个节点全部 `Ready`，`kube-system` 无异常 Pod，生产 Dashboard 和 Skill Runtime 健康接口仍正常。可复核的完整 JSON 证据保存于 `docs/validation/remote-live-diagnosis-20260806.json`。

直接在仓库根目录执行无范围的 `pytest -q` 会额外递归收集独立 `hermes-agent/` 子项目和多个 `release/` 快照，当前环境因缺少 `acp`、`prompt_toolkit`、`fire` 等可选依赖以及同名测试模块冲突而在收集阶段停止。因此本次主项目质量门采用明确的 `tests/` 边界；该仓库级收集配置问题应单独治理，不能把“收集失败”误报成本次置信度修正失败。

## 17. 是否需要继续优化

当前机制作为 `v2` 工程置信指数已具备多源证据、诊断/恢复历史解耦、质量评审、动态封顶、保守统计下界、运维可配置和结果可追溯能力。配置异常时会自动归一化或回退到安全默认值，实际权重、加权贡献、限制项和 cap 触发项会记录在 `confidence_explanation`。但如果 AMS 希望将其作为严格的概率校准机制，则仍需要标注数据验证。

### 17.1 近期优化

1. 在前端将已输出的 `weights`、`contributions`、`limitations` 和 `cap_triggers` 做成更细的因子面板。
2. 对跨 Agent 引用的原始 telemetry ID 进行进一步去重。当前已排除纯上下文条目，并合并同一 Agent 的 closure 复查结果。
3. 将当前的命令级动作指纹扩展为显式的恢复策略 ID，并对等价命令参数做语义归一化。
4. 结合回放数据为不同故障域提供推荐配置 profile，而不是让运维人员从空白参数开始调整。

### 17.2 中期优化

1. 使用标注 RCA 结果评估 Raw、Hypothesis、Evidence、Quality 之间的相关性和重复计分。
2. 使用逻辑回归、Platt scaling 或 isotonic regression 学习权重或校准最终分数。
3. 按通算 K8s、智算 vLLM/GPU、数据库和网络等故障域分别评估可靠性曲线。
4. 持续记录 Brier Score、Expected Calibration Error（ECE）与分箱准确率，确认置信度和真实正确率是否一致。

## 18. 实现文件索引

| 功能 | 文件 |
|---|---|
| 六因子公式、Evidence/Specificity/History、动态 cap | `orchestrator/confidence_calibrator.py` |
| 最终 RCA 生成、Judge/Critic 调用、校准调用 | `orchestrator/rca_engine.py` |
| 假设生成、快速规则、大模型重排 | `agents/hypothesis_agent.py` |
| RCAJudge 规则与大模型混合评审 | `memory/rca_judge.py` |
| Critic 大模型评审与规则降级 | `orchestrator/collaboration_optimizer.py` |
| 历史诊断正确性统计 | `memory/expert_feedback.py` |
| 历史恢复成功率统计（仅自动恢复） | `memory/fault_context_store.py` |
| 恢复动作/环境指纹 | `remediation_history.py` |
| Clopper–Pearson 精确单侧下界 | `confidence_statistics.py` |
| 自动恢复统计门禁 | `web_app/app.py` |
| Skill Runtime 四因子轻量校准 | `skill_runtime/confidence.py` |
| 置信度回归测试 | `tests/e2e/test_confidence_calibration.py` |

## 19. 建议的 AMS 对外口径

> AgenticSRE 使用的不是大模型单点自报置信度，而是以模型判断为先验、以多源观测和质量评审为校准、以专家确认的历史诊断正确性为历史因子、以证据不足和冲突规则为硬约束的可审计 RCA 置信指数。恢复成功率与根因置信度严格分离，只用于自动执行安全门禁；没有可靠历史或证据时，系统保留诊断和人工建议，但不会冒险自动恢复。
