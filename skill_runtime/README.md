# AgenticSRE Skill 运行时

Skill 运行时是一条轻量的故障检测、诊断、自愈和验证链路。它可以独立于
现有多智能体看板部署，从而保留原系统作为稳定基线，同时逐步引入
Agent+Skill 机制。

## 本地启动

```bash
python -m uvicorn skill_runtime.app:app --host 0.0.0.0 --port 8090
```

## 与现有系统并行部署

```bash
docker compose -f docker-compose.skill-runtime.yaml up -d
```

服务默认监听 `http://localhost:8090`。Web 看板读取 `SKILL_RUNTIME_URL`，
默认值为 `http://127.0.0.1:8090`。如果独立服务不可用，Agent+Skill
链路会回退到本地 `AgentSkillBridge`，并在 RCA 事件轨迹中记录回退模式。

## Skill 目录结构

```text
skill_runtime/skills/<skill_name>/
  SKILL.md        # 标准 Agent 可读说明
  skill.yaml      # AgenticSRE 运行时 manifest
  skill.py        # 可选的确定性运行适配器
```

`SKILL.md` 遵循 Codex/Hermes/龙虾机器人这类主流 Skill 包约定：
YAML frontmatter 只包含 `name` 和 `description`，Markdown 正文提供简洁的
运维流程说明。`id`、`domain`、`stages`、`policy`、`confidence`、
`matching`、`entrypoint` 等运行时字段不要放在 `SKILL.md` 中。这样外部
Agent 即使不了解 AgenticSRE 内部实现，也可以读取和复用这些 Skill。

`skill.yaml` 是 AgenticSRE 的运行时 manifest，用于声明元数据、阶段、
权限、置信度门禁、审批策略、匹配关键词、UI 信息和风险等级。四个阶段构成
全局编排协议，单个 Skill 只需声明并实现自身负责的阶段。`skill.py` 可按需实现：

- `detect(context)`
- `diagnose(context)`
- `heal(context)`
- `verify(context)`

执行器只调用 `skill.yaml` 的 `stages` 中声明的函数；未声明阶段即使存在兼容
适配函数也不会执行。`SKILL.md` 的执行流程必须与该声明一致，其他阶段只能写成
上游输入、下游交接或非目标。

每个函数返回 JSON 友好的字典：

- `status`: `matched`, `ok`, `skipped`, `blocked`, or `error`
- `confidence`: `0.0` to `1.0`
- `summary`: 中文可读的简短结论
- `evidence`: 证据字符串列表
- 其他阶段输出，例如 `suggestions` 或 `checks`

建议每个 Skill 都提供 `SKILL.md`。它应该用运维人员可审查的方式说明：
何时使用、需要哪些证据、输出什么结果、可能提出哪些动作，以及涉及哪些
风险和审批。Web 看板的 Skill Catalog 会为每个 Skill 打开详情弹窗：

- 如果存在 `SKILL.md`，弹窗按 Markdown 渲染全文；
- 如果缺少 `SKILL.md`，弹窗从 `skill.yaml` 生成说明；
- 同一弹窗还展示完整 `skill.yaml`、`skill.py`、元数据、权限、依赖和
  置信度门禁，便于审计。

推荐的包契约：

- `SKILL.md`: Agent 中立说明、触发条件、执行流程、输出契约、安全规则和验证清单。
- `skill.yaml`: AgenticSRE 路由和策略所需的可执行 manifest。
- `skill.py`: 仅为 manifest 已声明阶段提供确定性适配器。
- `references/` 或 `scripts/`: 复杂 Skill 的可选资源。

注册表会故意忽略 `SKILL.md` frontmatter 中出现的运行时字段，并记录警告。
这能保护互操作性：Hermes/龙虾式 Agent 可以加载同一份 `SKILL.md`，而
AgenticSRE 使用 `skill.yaml` 判断哪个 Skill 可执行以及需要哪些策略门禁。

## 工具调用边界

Skill 可以通过 `context["tools"].execute(tool, **kwargs)` 请求受控工具。当前
Agent+Skill 路径会注入宿主侧工具运行器，已支持只读 `kubectl` 调查，并预留
`web_search`、`browser`、`prometheus`、`teller`、`ssh/nvidia-smi` 等受控
runner。每次工具调用都会写入 Skill run 的 `trace[].tool_calls`。例如工具类
Skill 可以在 `diagnose` 阶段实际执行 `kubectl get/describe/logs/top/events`，
再把 stdout 摘要作为证据进入诊断置信度计算。

这不是开放 shell。运行时会拒绝非只读 `kubectl` 动词；自愈写操作仍然只能生成
dry-run 计划，必须走置信度、审批和风险门禁。独立 Skill Runtime 服务如果没有
宿主 Agent 注入工具运行器，只能基于 payload/已有证据推理或生成计划，不能声称
已经访问了真实集群或互联网。

看板桥接接口：

```bash
curl -s http://127.0.0.1:8080/api/skill-workflows/skills/<skill-id>
```

独立运行时也提供简要元数据接口：

```bash
curl -s http://127.0.0.1:8090/api/skills/<skill-id>
```

## 复用旧知识库

Skill 可以通过 `context["adapters"]["legacy"]` 调用已验证的自愈知识库，
但不直接耦合旧编排内部实现。置信度校准由 `skill_runtime.confidence`
负责，因此 LLM/vLLM 和通用 Skill 的诊断不依赖旧多智能体编排器。迁移可
按以下阶段推进：

1. 增加轻量 Skill 定义。
2. 将现有自愈 recipe 封装成适配器，同时把置信度校准等共享评分逻辑迁入 Skill 运行时。
3. 原系统和 Skill 运行时并行运行。
4. 对通过验证的场景逐步切换到 Skill 运行时。

## Skill 工作流

`POST /api/workflows/operate` 是 Skill 原生编排入口。它不是只运行单个 Skill：

1. 推断或接收目标领域，例如 `k8s`、`llm`、`db` 或 generic。
2. 让匹配的 Skill 执行 detect、diagnose、heal、verify。
3. 根据诊断置信度、可执行自愈方案和策略门禁选择主 Skill。
4. 聚合所有命中 Skill 的自愈建议和验证检查项。
5. 返回统一对象，包括 `diagnosis`、`capability`、`execution_plan`、
   `verification_plan`、`migration`、原始 `skill_runtime` runs，以及可选
   `legacy` 对比/回退结果。

工作流默认 `dry_run=true` 且 `allow_legacy_fallback=true`。优先使用专用
Skill。长尾 Kubernetes recipe 场景可由 `k8s.legacy_recipe_bridge` 处理，
并标记 `migration.mode=skill_bridge` 和 `migration.coverage_gap=true`；
如果没有 Skill 能生成可执行方案，工作流仍会回退到现有自愈知识库，并标记
`migration.mode=legacy_fallback`。这样可以在不破坏当前看板的前提下，
逐步把缺失场景转换为专用 Skill。

现有看板通过 `POST /api/skill-workflows/operate` 提供桥接接口。部署时它会
代理到独立 Skill 运行时 URL，默认 `SKILL_RUNTIME_URL` 或
`http://127.0.0.1:8090`，因此主 Web 容器不需要挂载或导入 `skill_runtime`。
本地开发时，如果独立运行时未启动，也可回退到进程内工作流。

## Agent+Skill 集成

当前看板保留两条可见运维路径：

- **纯多智能体路径**：由专用智能体完成检测、假设、调查、推理和按配置触发的
  恢复；不注册或调用 Skill，不自动桥接到 Agent+Skill。
- **Agent+Skill 路径**：Agent 启动 RCA 任务，必须先由 LLM 结合告警、
  历史规则、相似故障和只读观测上下文生成候选假设，同时选择具体 Skill。
  Skill 通过宿主工具运行器调查，Agent 根据返回证据裁决。运行过程会产生
  `llm_hypothesis_request`、`llm_hypotheses`、`skill_runtime` 等事件，
  包含候选假设、证据、主 Skill、置信度、阶段 trace、自愈动作和验证检查项。

`POST /api/rca/run` 在请求包含非空 `skill_workflow` 时进入独立 Agent+Skill
执行器；不传该字段时进入纯多智能体流程。旧配置
`pipeline.enable_agent_skill_bridge` 默认 false，仅为兼容旧配置保留；即使设为
true 也不能启用多智能体调用 Skill。直接向多智能体 RCA 引擎传入 Skill 工作流
或含 `skill_runtime_workflow` 的工具注册表会被拒绝。

宿主侧调用者是 `orchestrator.skill_agent.SkillAgent`。它相当于
AgenticSRE 中的轻量 Hermes/龙虾式运维 Agent：

1. 将告警或智算事件压缩为结构化工作上下文。
2. 一次 LLM 调用返回候选根因假设和目录内的 `skill_calls`；若 LLM 未配置、超时或失败，Agent+Skill
   链路显式失败，不使用规则结果冒充智能体推理。
3. 以 `agenticsre_skill_run` 审计事件调用具体 Skill，由 Skill 通过宿主注入的
   `kubectl` 运行器取证。默认完整流程不再另做一次模型规划和 Agent 预调查。
4. Agent 调用 LLM，对当前证据与 Skill 输出做最终裁决。
5. 向前端返回可审计事件轨迹，包括上下文、证据采集、Skill 选择、置信度、
   恢复方案和验证结果。

正常诊断路径需要两次模型调用。旧模型适配器未在假设响应中返回 Skill 调用时，
仍可执行有界的选择和纠错调用，因此两次并非所有请求的硬性上限。阶段消融路径
保留独立调查，以支持既有评估协议。

`k8s.image_pull_error` 直接读取目标 Pod 当前容器状态，区分镜像不存在、
访问被拒绝及连接异常；仅有告警文本不构成诊断证据。尚未自带调查工具的专项
Skill 由 `generic.evidence_collection` 补充只读取证，并把实际结果传入专项
Skill。模型可指定请求 namespace 内最多四条只读采集命令。
若这些命令均未取得有效对象输出，取证 Skill 会追加一次 Pod 名称发现，
并读取最多三个真实 Pod 的当前日志；空标签匹配不能直接证明服务不存在。

默认完整流程完成必须同时具备有效模型假设、Skill 实际成功取证及有效模型裁决。
`detect=skipped` 时 Runtime 会跳过本次 `diagnose`，避免不匹配故障产生模板结论。
当前成功查询到的 Pod Ready 状态参与证据一致性检查，旧启动事件不能直接覆盖
当前状态。`memory.enabled=false` 时上下文提供器跳过历史库访问。

告警分析前端不应直接调用 Skill 运行时。Skill 链路应始终表现为“Agent 调用
Skill 工具”，这样用户才能看到是谁采集了证据、选择了哪个 Skill，以及最终
诊断或自愈方案为什么成立。

诊断输入不得使用 `experiment_fault_type`、`scenario_id`、注入场景名称等
实验真值标签。此类字段只可用于离线评估和实验报告，不得用于假设生成、
Skill 路由或置信度计算。

完整泛化方法论见
[`docs/agent_skill_generalization_methodology.md`](../docs/agent_skill_generalization_methodology.md)。
新增故障类型时应按该文档执行：检测层只生成观测信号和候选 Skill，LLM 生成
候选假设，Skill 通过工具和标准观测字段验证证据，最终置信度统一走
`skill_evidence_calibrated_v2`。不得为了某个实验场景在 Agent、Runtime 或
Skill 中增加基于 `scenario_id`、`experiment_fault_type`、注入动作名的诊断
分支。

Frontend entrypoints:

- Alert Center -> alert row -> **分析** -> **分析分支** ->
  **启动 Agent+Skill 分析**.
- Alert Center -> **自愈分支** -> **启动 Agent+Skill 自愈**.
- Common Fault Lab and LLM Fault Lab -> **Agent+Skill 诊断/闭环**.
- AI Event Center -> selected event -> Agent+Skill analysis.

The Agent+Skill payload carries structured `skill_workflow` data instead of
only natural-language text. This prevents broad skills such as observability
signals from overriding explicit fault types. For example, a Prometheus
`TargetDown` alert with `fault_type=service_unavailable` is routed to
`k8s.service_unavailable` as the primary Skill, while
`k8s.observability_signal` may still appear as supporting trace evidence.

Example:

```bash
curl -s http://127.0.0.1:8090/api/workflows/operate \
  -H 'Content-Type: application/json' \
  -d '{
    "domain": "k8s",
    "dry_run": true,
    "payload": {
      "namespace": "default",
      "fault_type": "ServiceUnavailable",
      "message": "service frontend returns 503 because no endpoints",
      "service": "frontend",
      "deployment": "frontend",
      "diagnosis": {
        "fault_type": "ServiceUnavailable",
        "root_cause": "frontend has no endpoints",
        "root_cause_component": "frontend",
        "confidence": 0.93,
        "evidence_summary": {"endpoints": "empty", "status": "503"}
      }
    }
  }'
```

Migration status:

- Ready for skill-primary routing: `ServiceUnavailable`, `PodCrashLoop`,
  `ImagePullError`, `NodeNotReady`, `DNSResolution`, `ConfigMissing`,
  `VolumeMountFailed`, `HPAScalingFailed`, `ResourceQuotaExceeded`,
  `DeploymentUnhealthy`, `SchedulingFailed`, `OOMKilled`, resource pressure,
  control-plane health, observability-signal incidents, vLLM process crash, and
  read-only K8s/DB/LLM host tool flows.
- Bridge routing available: long-tail Kubernetes recipe scenarios not yet
  mirrored as dedicated skills, such as security-policy and host-level clock
  variants. These are intentionally marked as coverage gaps until forward-tested
  dedicated skills replace the bridge.
- Real execution should remain behind the existing guarded execution path until
  each skill has independent forward tests, rollback verification, and human
  approval evidence.

Verification commands:

```bash
pytest -q \
  tests/e2e/test_skill_runtime.py::test_rca_run_with_skill_workflow_emits_skill_runtime_event \
  tests/e2e/test_skill_runtime.py::test_rca_run_skill_workflow_falls_back_when_tool_missing \
  tests/e2e/test_skill_runtime.py::test_skill_workflow_prefers_explicit_service_unavailable_over_targetdown_signal \
  tests/e2e/test_frontend_refresh_behavior.py \
  tests/e2e/test_ai_event_center.py
```

For deployed systems, trigger an alert-center Agent+Skill run and check:

```bash
curl -s http://127.0.0.1:8080/api/rca/<run_id> | python -m json.tool
```

The event list should contain `event=skill_runtime`; for the service
unavailable example, `primary_skill_id` should be `k8s.service_unavailable`.

Confidence calibration:

- `llm_fault_observable_signals_v1` scores vLLM diagnosis with observable
  endpoint, metrics, process, GPU, queue, and dry-run signals.
- `skill_evidence_calibrated_v2` scores generic skill workflow diagnoses with
  raw skill confidence, explicitly sourced successful observations, diagnosis
  specificity, and confirmed historical diagnosis correctness. Alert text,
  diagnosis summaries, and stage-generated template evidence remain support
  context and do not count as successful observations. Historical recovery
  outcomes are excluded from diagnosis scoring and are checked separately by
  the auto-execution gate. Weak evidence and bridge skills apply explicit
  confidence caps, returned in `diagnosis.confidence_explanation`.
- For diagnostic workflows, an upstream diagnosis confidence is recorded for
  audit but stripped before Skill execution. The compatibility payload reuses
  the calibrated Skill score exactly; it does not restore the old score with
  `max(upstream, calibrated)`. The only exception is the strongly typed,
  already-approved transactional Undo contract, which consumes an existing
  calibrated diagnosis as a recovery gate and does not generate a new RCA.

## Initial Skills

- `k8s.service_unavailable`
- `k8s.pod_crashloop`
- `k8s.image_pull_error`
- `k8s.node_not_ready`
- `k8s.dns_resolution`
- `k8s.network_policy_deny`
- `k8s.config_missing`
- `k8s.volume_mount_failed`
- `k8s.hpa_scaling_failed`
- `k8s.resource_quota_exceeded`
- `k8s.deployment_unhealthy`
- `k8s.scheduling_failed`
- `k8s.oom_killed`
- `k8s.resource_pressure`
- `k8s.control_plane_unhealthy`
- `k8s.observability_signal`
- `k8s.legacy_recipe_bridge`
- `llm.vllm_process_crash`
- `llm.k8s_vllm_observability`
- `tool.k8s_readiness`
- `tool.db_connection_pool`
- `tool.llm_host_readiness`

Tool skills are split by permission level. Read-only K8s/DB/LLM host skills may
generate diagnostic commands. Write operations, process restarts, DB admin
operations, and destructive K8s actions must remain behind approval, dry-run,
rollback, and verification gates.

K8S-hosted vLLM scenarios are handled by `llm.k8s_vllm_observability`. It uses
Deployment readiness, vLLM health/model APIs, Prometheus target health, request
success counters, TTFT P95, and E2E latency P95 to produce Chinese diagnosis,
guarded recovery steps, and post-healing verification checks.

## Skill Evolution And Community Imports

The runtime supports review-only draft generation:

- `POST /api/skills/draft`: create a candidate skill from incident material,
  execution traces, and expert feedback.
- `GET /api/community/hermes/skills`: list local Hermes optional skills as
  import candidates.
- `POST /api/community/hermes/convert`: convert a Hermes skill into a disabled
  AgenticSRE draft.
- `GET /api/community/sources`: list locally registered community sources,
  including Anthropic skill-creator and Alibaba Cloud AIOps skills.
- `POST /api/community/local/convert`: convert a locally registered `SKILL.md`
  into a disabled AgenticSRE draft.
- `POST /api/community/markdown/convert`: convert pasted SKILL.md content, or
  fetch from allowlisted GitHub sources such as `anthropics/skills` and
  `aliyun/alibabacloud-aiops-skills`.
- `POST /api/compare/legacy`: run matching skills beside legacy healing logic
  so migration coverage can be compared before routing production incidents to
  Skill Runtime.

Drafts are not written to `skills/` and are not enabled automatically. This
matches the conservative pattern used by skill-creator-style workflows:

1. Keep the skill concise.
2. Put detailed references/scripts/assets behind progressive disclosure.
3. Generate a candidate.
4. Add tests and dry-run validation.
5. Forward-test with independent incidents.
6. Require human review before publishing.

Alibaba Cloud AIOps skills should be treated as cloud/AIOps tool sources:
prioritize read-only diagnostics first, never store AccessKey/SecretKey in
skill files, and require explicit approval plus rollback/verification for any
cloud resource mutation.

External AIOps skill references are registered as review-only sources:

- VMware AIOps skill references can guide infrastructure skill boundaries, but
  vCenter/ESXi write actions must be separated from read-only inventory and
  alarm diagnostics.
- Microsoft SAP AIOps agent patterns can guide observability-to-action
  workflows after human review of the protected article content.
- EvoSkills-style skill evolution should use generated skill drafts plus
  verifier feedback, held-out incident tests, and controlled dry-run/real
  validation before publishing.
