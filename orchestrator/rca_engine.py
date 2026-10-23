"""
AgenticSRE RCA Engine
Core hypothesis-driven RCA loop: the heart of the system.
Implements: Discovery → Hypothesis → Plan → Investigate → Reason
"""

import asyncio
import json
import logging
import math
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from configs.config_loader import get_config
from tools import build_tool_registry, LLMClient, ToolRegistry
from tools.llm_client import run_sync_in_daemon_thread, DeadlineLLMClient
from agents.evidence import format_evidence, ground_state_timeline
from agents.diagnosis_grounding import alert_payload, alert_report_prompt, apply_alert_grounding
from agents import (
    MetricAgent, LogAgent, TraceAgent, EventAgent,
    HypothesisAgent, CorrelationAgent, PlanningAgent,
    RemediationAgent, AlertAgent, ProfilingAgent, LLMInferenceAgent,
)
from memory import (
    FaultContextStore, ContextLearner, RCAJudge, TraceStore, AgentTrace,
    ExpertFeedbackStore, EvolutionTracker, HumanReviewStore,
)
from observability import AgentTracer, MetricsCollector
from orchestrator.collaboration_optimizer import CollaborationOptimizer, CollaborationPolicy, root_scoped_evidence
from diagnostic_strategy import (InvestigationController, evidence_catalog,
    hypothesis_review_context, validate_hypothesis_review)
from orchestrator.confidence_calibrator import calibrate_rca_confidence
from orchestrator.diagnosis_closure import DiagnosisClosureManager, bind_report_entity
from orchestrator.session import RCASession

logger = logging.getLogger(__name__)
ROOT_DIR = Path(__file__).resolve().parents[1]


def _apply_model_completion_gate(final_result, judge_result, llm, *, failed_stages=None):
    """A successful later reviewer cannot erase an incomplete model stage."""
    incomplete = list(getattr(llm, 'incomplete_responses', []))
    timeouts = getattr(llm, 'timeout_count', 0)
    failures = list(getattr(llm, 'failed_calls', []))
    stages = list(failed_stages or [])
    if not incomplete and not timeouts and not failures and not stages:
        return final_result, judge_result, False
    report = dict(final_result)
    confidence = report.get('confidence', 0)
    bounded = confidence if type(confidence) in (int, float) and math.isfinite(confidence) else 0
    report['model_response_validation'] = {'status': 'requires_review',
        'incomplete_responses': incomplete, 'call_timeouts': timeouts,
        'failed_calls': failures, 'failed_stages': stages,
        'confidence_before_cap': confidence, 'confidence_cap': 0.65}
    report['confidence'] = min(max(bounded, 0), 0.65)
    judge = dict(judge_result, needs_review=True, judge_level=3, combined_score=0.0)
    judge['review_blockers'] = list(dict.fromkeys(
        list(judge_result.get('review_blockers', [])) + ['incomplete_model_execution']))
    return report, judge, True


def _unavailable_model_report(query, namespace, top=None):
    """Keep request scope, without promoting a candidate to a causal finding."""
    names=set(re.findall(r'\bPod(?:/|:\s*)([a-z0-9][a-z0-9.-]*)',query,re.IGNORECASE))
    target={'namespace':namespace,'kind':'Pod','name':next(iter(names))} if len(names)==1 and namespace else None
    scope=f"{namespace}/Pod/{target['name']}" if target else f"命名空间 {namespace or '未指定'} 的本次调查"
    return {'root_cause':f'{scope} 的根因尚未完成模型核验；模型报告不可用，现有观测和候选仅供人工复核。',
        'root_entity':None,'incident_target':target,'confidence':0.0,'fault_type':'unknown',
        'candidate_hypothesis_id':getattr(top,'id','') if top else '',
        'affected_services':[],'timeline':[],'evidence_summary':{},
        'reasoning_chain':'最终模型报告未完成；已采集观测、候选及引用保留在过程记录中，需要人工复核。',
        'remediation_suggestion':'请核查模型服务状态，并根据已采集观测人工复核；不要据此执行自动处置。',
        'prevention':''}


def _agent_query(
    incident_query: str,
    agent_context,
    collaboration_policy: CollaborationPolicy,
    collaboration_optimizer: Optional[CollaborationOptimizer],
    agent_name: str,
) -> str:
    """Build the per-agent query, falling back to the raw incident when disabled."""
    if collaboration_optimizer is None:
        return incident_query
    return collaboration_optimizer.enrich_incident_query(
        incident_query, agent_context, collaboration_policy, agent_name
    )


def _is_k8s_lifecycle_incident(incident_query: str) -> bool:
    """True for incidents where K8s events are the primary evidence source."""
    text = incident_query.lower()
    hints = [
        "imagepull", "image pull", "errimagepull", "imagepullbackoff",
        "failed to pull image", "failed to resolve reference",
        "crashloopbackoff", "createcontainer", "pending", "failedscheduling",
        "pod/", "pod ", "containercreating",
    ]
    return any(h in text for h in hints)


def _is_llm_inference_incident(incident_query: str) -> bool:
    """True when incident likely targets vLLM/GPU inference runtime."""
    text = incident_query.lower()
    hints = [
        "vllm", "llm", "gpu", "cuda", "nccl", "kv cache", "kv-cache",
        "ttft", "tpot", "token", "tokenizer", "safetensor", "model weight",
        "inference", "openai", "推理", "大模型", "显存", "智算",
    ]
    return any(h in text for h in hints)


def _selected_evidence_agents(
    incident_query: str,
    cfg,
    policy: Optional[CollaborationPolicy] = None,
) -> List[str]:
    """Select a minimal useful evidence set for common incident classes."""
    if not getattr(cfg.pipeline, "enable_fast_evidence_selection", True):
        selected = ["metric_agent", "log_agent", "trace_agent", "event_agent"]
    else:
        text = incident_query.lower()
        if _is_llm_inference_incident(incident_query) and getattr(cfg.pipeline, "enable_llm_inference_agent", True):
            selected = ["llm_inference_agent", "metric_agent", "event_agent", "log_agent"]
        elif _is_k8s_lifecycle_incident(incident_query):
            # ImagePull/Pending/CrashLoop evidence is usually in events, pod status,
            # and node/CoreDNS metrics. Logs/traces are often empty or unrelated.
            selected = ["event_agent", "metric_agent"]
        elif any(h in text for h in ["dns", "network", "timeout", "connection", "连接", "超时"]):
            selected = ["event_agent", "metric_agent", "trace_agent"]
        elif any(h in text for h in ["latency", "p99", "5xx", "http", "service"]):
            selected = ["metric_agent", "trace_agent", "event_agent"]
        else:
            selected = ["metric_agent", "event_agent"]

    if policy and policy.agents:
        allowed = {
            "metric_agent", "log_agent", "trace_agent", "event_agent", "llm_inference_agent"
        }
        policy_agents = [a for a in policy.agents if a in allowed]
        if policy_agents:
            selected = list(dict.fromkeys(selected + policy_agents))
    return selected


def _planned_evidence_agents(plan: Dict[str, Any], *, limit: int = 2) -> List[str]:
    """Select the smallest high-priority evidence team from an Agent-generated plan."""
    allowed = {"metric_agent", "log_agent", "trace_agent", "event_agent", "llm_inference_agent"}
    steps = plan.get("plan") if isinstance(plan.get("plan"), list) else []
    ranked: List[str] = []
    for priority in ("high", "medium", "low", ""):
        for step in steps:
            if not isinstance(step, dict):
                continue
            agent = str(step.get("agent") or "")
            step_priority = str(step.get("priority") or "").lower()
            if agent in allowed and step_priority == priority and agent not in ranked:
                ranked.append(agent)
                if len(ranked) >= max(1, limit):
                    return ranked
    return ranked


def _extract_evidence_items(evidence: Dict[str, Dict]) -> List[Dict[str, Any]]:
    """Bound structured evidence fairly so metrics cannot crowd out other agents."""
    groups = []
    for agent_name, result in evidence.items():
        if agent_name == "structured_evidence" or not isinstance(result, dict):
            continue
        group = []
        for item in result.get("evidence_items", []) or []:
            if isinstance(item, dict):
                group.append({**item, "source_agent": agent_name})
        if group:
            groups.append(iter(group))
    items = []
    while groups and len(items) < 100:
        remaining = []
        for group in groups:
            item = next(group, None)
            if item is not None:
                items.append(item)
                remaining.append(group)
            if len(items) == 100:
                break
        groups = remaining
    return items


def _align_root_cause_with_targeted_evidence(
    final_result: Dict[str, Any],
    *,
    evidence: Dict[str, Dict],
    hypotheses: List[Any],
    targeted_evidence_complete: bool,
) -> Dict[str, Any]:
    """Prevent a generated report from contradicting decisive live state.

    The LLM report is allowed to explain and summarize, but it must not replace
    a confirmed Top1 mechanism with an unsupported alternative.  This guard is
    intentionally narrow: it only acts after the targeted evidence loop has
    completed and only for live Kubernetes states that are directly observed.
    """
    if not targeted_evidence_complete:
        return final_result

    items = _extract_evidence_items(root_scoped_evidence(final_result, evidence))
    item_types = {
        str(item.get("signal_type") or item.get("type") or "")
        for item in items
    }
    top = hypotheses[0] if hypotheses else None
    top_id = str(getattr(top, "id", "") or "")
    root_cause = str(final_result.get("root_cause") or "")
    root_text = root_cause.casefold()

    replacement = ""
    alignment_reason = ""
    decisive_types: List[str] = []

    scale_markers = ("replica", "replicas", "副本", "desired=0", "副本数为 0", "副本数被设置为 0")
    selector_markers = ("selector", "标签选择器", "标签不匹配", "选择器不匹配")
    if {
        "deployment_scaled_to_zero",
        "service_endpoints_empty",
    }.issubset(item_types) and (
        any(marker in root_text for marker in selector_markers)
        or not any(marker in root_text for marker in scale_markers)
    ):
        replacement = (
            f"当前 Deployment/{final_result['root_entity']['name']} 的副本数被设置为 0，因没有运行中的后端 Pod，"
            "Service Endpoints 的就绪地址为 0。"
        )
        alignment_reason = "deployment_scaled_to_zero_with_empty_endpoints"
        decisive_types = ["deployment_scaled_to_zero", "service_endpoints_empty"]

    image_waiting = next(
        (
            item
            for item in items
            if str(item.get("signal_type") or item.get("type") or "") == "pod_waiting_state"
            and any(
                marker in (
                    f"{item.get('observation', item.get('statement', ''))} "
                    f"{item.get('raw_ref', '')}"
                ).casefold()
                for marker in ("imagepullbackoff", "errimagepull", "image pull")
            )
        ),
        None,
    )
    image_markers = ("imagepull", "errimagepull", "image pull", "镜像拉取", "拉取镜像")
    if image_waiting is not None and not any(marker in root_text for marker in image_markers):
        pod_ref = final_result["root_entity"]["name"]
        replacement = (
            f"{pod_ref} 当前处于 ImagePullBackOff/ErrImagePull，容器镜像无法拉取，"
            "导致工作负载没有就绪副本。"
        )
        alignment_reason = "current_pod_image_pull_waiting_state"
        decisive_types = ["pod_waiting_state"]

    if not replacement or replacement == root_cause:
        return final_result

    aligned = dict(final_result)
    aligned["root_cause_original"] = root_cause
    aligned["root_cause"] = replacement
    aligned["root_cause_alignment"] = {
        "reason": alignment_reason,
        "evidence_types": decisive_types,
        "hypothesis_id": top_id if getattr(top, "service", "") == final_result["root_entity"]["name"] else "",
        "root_entity": dict(final_result["root_entity"]),
    }
    alignment_note = (
        "证据一致性校验使用与根因实体名称及命名空间匹配的结构化观测；其他对象的故障不参与根因改写。"
    )
    reasoning = str(aligned.get("reasoning_chain") or "").strip()
    aligned["reasoning_chain"] = f"{reasoning}\n{alignment_note}".strip()
    return aligned


def _lifecycle_ranking_evidence(query: str, namespace: str, evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Keep peer workload faults out of single Pod lifecycle reasoning inputs.

    Multi-object and propagation investigations retain their broader evidence.
    The caller keeps the original observations in the RCA session for review.
    """
    names = set(re.findall(r"\bPod(?:/|:\s*)([a-z0-9][a-z0-9.-]*)", query, re.IGNORECASE))
    # Object identity, rather than a presumed fault word, determines scope.
    # Neutral health checks need the same protection from peer observations.
    other_targets = re.findall(
        r"\b(?:Service|Deployment|StatefulSet|DaemonSet|ReplicaSet|Job|CronJob|Node)(?:/|:\s*)([a-z0-9][a-z0-9.-]*)",
        query, re.IGNORECASE)
    if len(names) != 1 or not namespace or other_targets:
        return evidence
    name = next(iter(names))
    scoped = root_scoped_evidence({"root_entity": {"namespace": namespace, "kind": "Pod", "name": name}}, evidence)
    for agent, payload in evidence.items():
        if not isinstance(payload, dict) or payload.get("error"):
            continue
        lines = []
        for line in str(payload.get("warning_events") or "").splitlines():
            parts = line.split()
            if any(a == namespace and b == f"Pod/{name}" for a, b in zip(parts[:3], parts[1:4])):
                lines.append(line)
        if lines:
            scoped.setdefault(agent, {"evidence_items": []})["summary"] = "\n".join(lines)
    scoped["incident_scope"] = {"summary": f"当前调查对象是 {namespace}/Pod/{name}。候选根因必须解释该对象的故障；其他工作负载的独立异常不能直接替代该对象的根因。"}
    return scoped


def _is_no_fault_result(result: Dict[str, Any]) -> bool:
    """Return True when RCA explicitly concluded that no active fault exists."""
    fault_type = str((result or {}).get("fault_type", "") or "").strip().lower()
    return fault_type in {"no_fault", "no_fault_detected", "healthy", "baseline_healthy"}


def _propagation_root_identity_observed(result, evidence, namespace):
    """Check a proposed Pod identity against acquired manifests, not candidate ranking."""
    entity = result.get('root_entity')
    if not isinstance(entity, dict) or entity.get('namespace') != namespace or entity.get('kind') != 'Pod':
        return False
    name = entity.get('name')
    if not isinstance(name, str) or not name:
        return False
    for group in evidence.values():
        if not isinstance(group, dict) or group.get('error'):
            continue
        for item in group.get('evidence_items', []):
            raw = item.get('raw_ref') or {}
            if (item.get('source') == 'kubernetes_manifest'
                    and item.get('signal_type') == 'pod_declared_configuration'
                    and item.get('service') == name and raw.get('pod') == name
                    and raw.get('namespace') == namespace and raw.get('uid')):
                return True
    return False


async def _run_agent_with_timeout(name: str, coro, timeout_s: int) -> Any:
    """Run one evidence agent with a bounded timeout."""
    try:
        return await asyncio.wait_for(coro, timeout=max(1, timeout_s))
    except asyncio.TimeoutError:
        raise TimeoutError(f"{name} exceeded {timeout_s}s evidence timeout")


# ── Final LLM Report Prompt ──

FINAL_REPORT_PROMPT = """你是资深 SRE，需要产出最终根因分析报告。
除 fault_type、服务名、命令、指标名、资源名等机器可读字段外，所有解释性内容必须使用中文。

Incident: {incident}

Hypotheses (ranked by confidence):
{hypotheses}

Evidence from investigation:
{evidence}

Cross-signal correlation:
{correlation}

结论约束：
- root_cause 和 reasoning_chain 不写具体时间戳；精确时间只放 timeline，必须来自原始事件而不是采集时间
- timeline 中每条状态变更必须同时保留原始 field、before、after 和 change_id；严格按时间排序，不得把恢复与故障发生互换
- 根因粒度不得超过观测证据；不得猜测未采集的配置参数、参数值、内部实现或人为操作
- 如果证据只确认某资源达到容量上限并导致排队，应将根因停留在该资源压力机制
- 未采集到的日志、Trace 或配置必须明确写为限制，不能反向编造成根因依据
- 未验证日志字段语义时，null/空值只表示原始字段值；绝不能据此推断内部依赖缺失、配置为空或未就绪
- 传播起点与应用内部故障机制必须区分：仅观测到服务返回 503 时，内部机制明确写为未证实
- root_cause 必须使用观测中的完整资源名称和 namespace，不得用 dependency、caller 等角色简称替代；affected_services 不替代根因定位
- root_entity 只填写实际证据支持的根因候选实体，包含 namespace、kind、name；若无法定位则填 null，不从受影响列表猜选
- 不能通过把就绪探针改成返回 200 的路径来掩盖业务 503；未取得探针故障证据时只建议只读检查
- 修复建议优先针对已确认机制采取最小影响动作，不得因缺少证据而扩大操作范围

请产出结构化 RCA JSON：
{{
    "root_cause": "具体、可执行的中文根因结论",
    "root_entity": {{"namespace": "观测命名空间", "kind": "Pod", "name": "完整资源名称"}},
    "confidence": 0.85,
    "fault_type": "category of fault",
    "affected_services": ["svc1", "svc2"],
    "timeline": [
        {{"time": "approximate time", "event": "中文事件描述"}}
    ],
    "evidence_summary": {{
        "metrics": "中文指标证据摘要",
        "logs": "中文日志证据摘要",
        "traces": "中文链路证据摘要",
        "events": "中文事件证据摘要"
    }},
    "reasoning_chain": "从证据到结论的中文推理链",
    "remediation_suggestion": "中文修复建议",
    "prevention": "中文预防措施"
}}"""


async def run_rca(
    incident_query: str,
    namespace: str = "",
    config=None,
    log_callback: Optional[Callable] = None,
    registry: Optional[ToolRegistry] = None,
    initial_evidence: Optional[Dict[str, Any]] = None,
    skill_workflow: Optional[Dict[str, Any]] = None,
    analysis_phases: Optional[List[str]] = None,
) -> Dict:
    """
    Execute the full hypothesis-driven RCA pipeline.
    
    Flow:
    1. Build ToolRegistry → Init Memory → Historical Context
    2. Generate hypotheses (with historical injection)
    3. Iterative evidence loop (Metric/Log/Trace/Event agents in parallel)
    4. Cross-signal correlation
    5. Graph-based RCA localization
    6. Final LLM report
    7. Quality judge → Auto-learning
    8. Optional self-healing
    """
    if skill_workflow:
        raise ValueError("Skill workflows must use the Agent+Skill entry point, not multi-agent RCA")
    if registry is not None and registry.get("skill_runtime_workflow") is not None:
        raise ValueError("Skill-capable registries cannot be used by multi-agent RCA")
    cfg = config or get_config()
    from memory.memory_scope import MemoryScope
    memory_scope = MemoryScope(cluster_id=str(getattr(cfg.kubernetes, 'cluster_id', '') or '').strip(),
                               namespace=namespace)
    source_payload = initial_evidence.get('payload', initial_evidence) if isinstance(initial_evidence, dict) else {}
    if (isinstance(source_payload, dict) and source_payload.get('cluster_id') == memory_scope.cluster_id
            and source_payload.get('namespace') == namespace):
        memory_scope = MemoryScope(cluster_id=memory_scope.cluster_id, namespace=namespace,
            as_of=memory_scope.as_of, entity_uid=str(source_payload.get('entity_uid') or ''),
            workload_version=str(source_payload.get('workload_version') or ''))
    enabled_analysis_phases = set(analysis_phases or ["context", "hypothesis", "plan", "investigation", "reasoning"])
    enabled_analysis_phases.update({"investigation", "reasoning"})
    session_id = f"rca-{uuid.uuid4().hex[:8]}"
    session = RCASession(
        session_id=session_id,
        incident_query=incident_query,
        namespace=namespace,
        status="running",
    )
    analysis_incident_query = incident_query
    propagation_lookup = {}
    if initial_evidence:
        evidence_payload = (
            initial_evidence.get("payload")
            if isinstance(initial_evidence.get("payload"), dict)
            else initial_evidence
        )
        from agents.alert_dossier_prompt import initial_incident
        from agents.alert_root_candidates import propagation_targets
        propagation_lookup = propagation_targets(evidence_payload, namespace)
        analysis_incident_query = initial_incident(incident_query, evidence_payload)
        session.evidence["agent_skill_context"] = {
            "agent": "agent_skill_context",
            "summary": "当前事件的集群、目标对象、时间窗和运行期观测已进入统一调查上下文。",
            "payload": evidence_payload,
        }

    def log(msg: str):
        session.log(msg)
        logger.info(msg)
        if log_callback:
            log_callback(msg)

    def emit(event: dict):
        """Emit structured event through log_callback."""
        if log_callback:
            log_callback(event)

    def record_evidence(agent: str, result: dict):
        start = len(session.observation_history)
        session.record_evidence(agent, result)
        # Checkpoint full observations at each attempt, so process interruption
        # can retain facts even before a final report exists.
        emit({"event": "observation_batch", "items": session.observation_history[start:],
              "collection": session.collection_history[-1]})

    def candidate_review_context():
        scoped=session.evidence if propagation_lookup else _lifecycle_ranking_evidence(
            analysis_incident_query,namespace,session.evidence)
        return hypothesis_review_context([h.to_dict() for h in session.hypotheses],
            evidence_catalog(scoped,session.observation_history))

    def record_candidate_review(quality, context, stage):
        review=quality.get("hypothesis_review") or validate_hypothesis_review(context,None)
        # An exception in the reviewer cannot leave an earlier passing verdict.
        if review["blockers"]:
            quality["needs_review"]=True
            quality["combined_score"]=0.0
            quality["review_blockers"]=list(dict.fromkeys(quality.get("review_blockers",[])+review["blockers"]))
        quality["hypothesis_review"]=review
        if scoped_lifecycle and not (session.result or {}).get('root_entity_validation',{}).get('observed'):
            quality['needs_review']=True
            quality['combined_score']=0.0
            quality['review_blockers']=list(dict.fromkeys(quality.get('review_blockers',[])+['root_entity_not_uid_bound']))
        record={"event":"hypothesis_independent_review","stage":stage,**review}
        session.hypothesis_review_history.append(record)
        emit(record)

    try:
        # ── Step 0: Setup ──
        log("🔧 Initializing tools and agents...")
        if registry is None:
            registry = build_tool_registry(cfg, allow_write=cfg.runtime.enable_self_healing, isolated=True)
        
        llm = DeadlineLLMClient(
            cfg.llm,
            total_s=getattr(cfg.pipeline, "rca_llm_budget_s", 480),
            call_s=getattr(cfg.pipeline, "rca_llm_call_timeout_s", 90),
        )
        
        # Init agents
        metric_agent = MetricAgent(llm, registry)
        log_agent = LogAgent(llm, registry)
        trace_agent = TraceAgent(llm, registry)
        event_agent = EventAgent(llm, registry)
        # Single-Pod lifecycle reasoning consumes collected facts, not these
        # namespace-wide prose summaries. Keep their display deterministic;
        # every requested reasoning/review call retains its completion gate.
        scoped_lifecycle = not propagation_lookup and 'incident_scope' in _lifecycle_ranking_evidence(
            analysis_incident_query, namespace, {})
        event_agent.summarize = alert_payload(session.evidence) is None and not scoped_lifecycle
        metric_agent.summarize = not scoped_lifecycle
        llm_inference_agent = LLMInferenceAgent(llm, registry)
        evidence_summary_max_tokens = getattr(cfg.pipeline, "evidence_summary_max_tokens", 1024)
        for agent in (metric_agent, log_agent, trace_agent, event_agent, llm_inference_agent):
            agent.summary_max_tokens = evidence_summary_max_tokens
        hypothesis_agent = HypothesisAgent(llm)
        correlation_agent = CorrelationAgent(llm)
        planning_agent = PlanningAgent(llm, registry)

        # Init memory
        # Initialization belongs inside the history deadline as well.
        from memory.history_retrieval import retrieve_history, empty_history, history_budget_s
        store = None
        learner = None
        judge = RCAJudge(llm, cfg)
        trace_store = TraceStore(cfg)
        feedback_store = ExpertFeedbackStore()
        evolution_tracker = EvolutionTracker.from_config() if cfg.evolution.enabled else None
        review_store = HumanReviewStore()
        closure_manager = DiagnosisClosureManager(llm=llm)
        collaboration_optimizer = None
        if cfg.pipeline.enable_collaboration_optimization:
            collaboration_optimizer = CollaborationOptimizer(
                cfg=cfg,
                fault_store=store,
                feedback_store=feedback_store,
                trace_store=trace_store,
                llm=llm,
            )
        
        # Start pipeline trace
        pipe_trace = trace_store.start_pipeline(session_id, incident_query)

        # ── Step 1: Historical Context ──
        if "context" in enabled_analysis_phases:
            session.add_phase(1, "CONTEXT_RETRIEVAL")
            emit({"event": "phase_start", "phase": 1, "name": "CONTEXT_RETRIEVAL"})
            log("📚 Retrieving historical context...")
        historical = empty_history('disabled' if not cfg.memory.enabled else 'not_requested')
        if cfg.memory.enabled and "context" in enabled_analysis_phases:
            lookup = retrieve_history(lambda: FaultContextStore(cfg), incident_query,
                n_rules=cfg.memory.max_similar_results, n_faults=cfg.memory.max_similar_results,
                memory_scope=memory_scope,
                timeout_s=min(history_budget_s(getattr(cfg.memory, 'retrieval_timeout_s', 5.0)), max(0.0, llm.remaining_s())))
            historical, store = lookup.context, lookup.store
            learner = ContextLearner(llm, store, cfg) if store else None
        session.metadata['history_retrieval'] = historical['retrieval']
        emit({'event': 'history_retrieval', 'data': historical['retrieval']})
        if collaboration_optimizer is not None and "context" in enabled_analysis_phases:
            agent_context = collaboration_optimizer.build_context(incident_query, historical_context=historical,
                memory_scope=memory_scope)
            collaboration_policy = collaboration_optimizer.select_policy(incident_query, agent_context)
            enriched_incident = collaboration_optimizer.enrich_incident_query(
                incident_query, agent_context, collaboration_policy
            )
            session.metadata["used_rule_ids"] = agent_context.get_used_rule_ids() if agent_context else []
            session.metadata["recalled_rule_refs"] = agent_context.get_recalled_rule_refs() if agent_context else []
        else:
            agent_context = None
            collaboration_policy = CollaborationPolicy(
                max_iterations=cfg.pipeline.max_evidence_iterations,
                confidence_threshold=cfg.pipeline.hypothesis_confidence_threshold,
                require_reflection=False,
                memory_mode="off",
                rationale="Collaboration optimization disabled by configuration.",
            )
            enriched_incident = incident_query
        rules = [r.get("text", r.get("condition", "")) for r in historical.get("rules", [])]
        faults = historical.get("faults", [])
        if "context" in enabled_analysis_phases:
            log(f"  Found {len(rules)} similar rules, {len(faults)} similar faults")
            log(f"  Collaboration strategy: {collaboration_policy.strategy} — {collaboration_policy.rationale}")
            session.complete_phase(1)
            emit({
                "event": "phase_complete",
                "phase": 1,
                "name": "CONTEXT_RETRIEVAL",
                "notes": f"history={historical['retrieval']['status']}; {len(rules)} rules, {len(faults)} faults; strategy={collaboration_policy.strategy}",
            })
            emit({"event": "collaboration_policy", "data": collaboration_policy.to_dict()})

        # ── Step 2: Hypothesis Generation ──
        if "hypothesis" in enabled_analysis_phases:
            session.add_phase(2, "HYPOTHESIS_GENERATION")
            emit({"event": "phase_start", "phase": 2, "name": "HYPOTHESIS_GENERATION"})
            log("🧠 Generating root cause hypotheses...")
        # Fast-path classification must only inspect the current incident.
        # Historical memory is passed separately so recalled faults cannot
        # accidentally turn a healthy OTel check into an unrelated incident.
            session.hypotheses = hypothesis_agent.generate(
                analysis_incident_query, historical_rules=rules, historical_faults=faults
            )
            for h in session.hypotheses:
                log(f"  [{h.id}] (conf={h.confidence:.2f}) {h.description[:100]}")
            session.complete_phase(2, notes=f"{len(session.hypotheses)} hypotheses generated")
            emit({"event": "phase_complete", "phase": 2, "name": "HYPOTHESIS_GENERATION", "notes": f"{len(session.hypotheses)} hypotheses generated"})
            emit({"event": "hypotheses", "items": [h.to_dict() for h in session.hypotheses]})
        else:
            log("⏭️ Hypothesis generation disabled for this run; investigating current evidence directly.")

        # ── Step 3: Iterative Evidence Loop ──
        max_iter = collaboration_policy.max_iterations
        confidence_threshold = collaboration_policy.confidence_threshold
        controller = InvestigationController(collaboration_policy.agents,
            max_iterations=max_iter, confidence_threshold=confidence_threshold)
        generation_record = controller.record_generation([h.to_dict() for h in session.hypotheses])
        generation_record["input_summary"]={"rules_recalled":len(rules),"similar_faults_recalled":len(faults),
            "history_is_current_observation":False,"generation_method":hypothesis_agent.last_generation_method}
        controller.generation = generation_record
        session.hypothesis_history.append(generation_record)
        emit(generation_record)
        pending_agents = []
        timeout_s = getattr(cfg.pipeline, "evidence_agent_timeout_s", 45)
        
        for iteration in range(max_iter):
            if llm.remaining_s() <= 0:
                log("  ⚠️ RCA model budget exhausted; preserving evidence for human review")
                break
            session.current_iteration = iteration + 1
            session.add_phase(3, f"INVESTIGATION_ITER_{iteration+1}")
            emit({"event": "iteration", "current": iteration + 1, "total": max_iter})
            emit({"event": "phase_start", "phase": 3, "name": f"INVESTIGATION_ITER_{iteration+1}"})
            log(f"\n🔍 Investigation iteration {iteration+1}/{max_iter}")

            timeout_s = getattr(cfg.pipeline, "evidence_agent_timeout_s", 45)
            top_before_investigation = session.top_hypothesis() if "hypothesis" in enabled_analysis_phases else None

            # Compare two competing mechanisms and carry the previous round's
            # evidence gaps into planning. The host bounds the next sources.
            if "plan" not in enabled_analysis_phases:
                selected_agents = _selected_evidence_agents(analysis_incident_query, cfg, collaboration_policy)
                plan = {"plan": [{"agent": name, "reason": "direct evidence collection"} for name in selected_agents]}
                log("  ⏭️ Verification planning disabled; using direct evidence collection")
            elif top_before_investigation is not None:
                plan = planning_agent.generate_plan(
                    [h.to_dict() for h in session.hypotheses if h.status != "rejected"][:2],
                    "Previous investigation feedback (observations, not ground truth):\n"
                        + json.dumps(controller.feedback(), ensure_ascii=False)[:1000] + "\nCurrent incident:\n" + analysis_incident_query,
                    iteration,
                )
                selected_agents = _planned_evidence_agents(plan, limit=2)
                if pending_agents:
                    selected_agents = pending_agents
                if not selected_agents:
                    selected_agents = _selected_evidence_agents(analysis_incident_query, cfg, collaboration_policy)[:2]
                # A planner may select generic metric/trace agents for a
                # Service/Deployment incident and omit the Kubernetes event
                # agent. Current replica/Endpoint state and live-vs-orphaned
                # Pod events are decisive for lifecycle incidents, so always
                # retain one event-agent pass for those symptoms.
                lifecycle_text = (
                    f"{analysis_incident_query} "
                    f"{top_before_investigation.description if top_before_investigation else ''}"
                ).casefold()
                lifecycle_markers = (
                    "service", "endpoint", "endpoints", "deployment", "pod", "workload",
                    "ready", "replica", "imagepull", "crashloop", "端点", "就绪", "副本",
                )
                if (
                    "event_agent" not in selected_agents
                    and any(marker in lifecycle_text for marker in lifecycle_markers)
                ):
                    selected_agents = [*selected_agents, "event_agent"][:3]
                log(
                    f"  📋 Competing hypothesis plan [leading={top_before_investigation.id}]: "
                    f"{', '.join(selected_agents)}"
                )
                emit({
                    "event": "hypothesis_focus",
                    "hypothesis_id": top_before_investigation.id,
                    "confidence": top_before_investigation.confidence,
                    "agents": selected_agents,
                })
            elif getattr(cfg.pipeline, "skip_planning_in_fast_evidence", True) and getattr(
                cfg.pipeline, "enable_fast_evidence_selection", True
            ):
                selected_agents = _selected_evidence_agents(analysis_incident_query, cfg, collaboration_policy)
                plan = {"plan": [{"agent": name, "reason": "fast evidence selection"} for name in selected_agents]}
                log(f"  📋 Fast evidence plan: {len(plan.get('plan', []))} agents")
            else:
                selected_agents = _selected_evidence_agents(analysis_incident_query, cfg, collaboration_policy)
                plan = planning_agent.generate_plan(
                    [h.to_dict() for h in session.hypotheses],
                    analysis_incident_query, iteration
                )
                log(f"  📋 Plan: {len(plan.get('plan', []))} steps")

            if pending_agents and top_before_investigation is None:
                selected_agents = pending_agents

            # Run domain agents in parallel
            if propagation_lookup:
                # A propagation hypothesis must actually inspect the supporting
                # Trace and the named dependency's logs, even if planning omits them.
                selected_agents = list(dict.fromkeys(['configuration_agent', 'trace_agent', 'log_agent', 'event_agent'] + selected_agents))[:5]
            log(f"  Running evidence agents: {', '.join(selected_agents)} (timeout={timeout_s}s)")
            start_time = time.time()

            def _agent_coro(name: str):
                if name == 'configuration_agent':
                    from agents.workload_artifacts import collect_workload_artifacts
                    return collect_workload_artifacts(registry, namespace, propagation_lookup['pod_name'])
                if name == "metric_agent":
                    return metric_agent.analyze(_agent_query(
                        analysis_incident_query, agent_context, collaboration_policy, collaboration_optimizer, name
                    ), namespace)
                if name == "log_agent":
                    return log_agent.analyze(_agent_query(
                        analysis_incident_query, agent_context, collaboration_policy, collaboration_optimizer, name
                    ), namespace, **({'pod_name': propagation_lookup['pod_name']} if propagation_lookup else {}))
                if name == "trace_agent":
                    return trace_agent.analyze(_agent_query(
                        analysis_incident_query, agent_context, collaboration_policy, collaboration_optimizer, name
                    ), namespace=namespace, **({'trace_ids': propagation_lookup['trace_ids']} if propagation_lookup else {}))
                if name == "event_agent":
                    return event_agent.analyze(_agent_query(
                        analysis_incident_query, agent_context, collaboration_policy, collaboration_optimizer, name
                    ), namespace)
                if name == "llm_inference_agent":
                    return llm_inference_agent.analyze(_agent_query(
                        analysis_incident_query, agent_context, collaboration_policy, collaboration_optimizer, name
                    ), namespace)
                raise ValueError(f"Unknown evidence agent: {name}")

            results = await asyncio.gather(
                *[
                    _run_agent_with_timeout(name, _agent_coro(name), timeout_s)
                    for name in selected_agents
                ],
                return_exceptions=True,
            )

            # Collect results
            agent_names = selected_agents
            new_evidence = {}
            for name, result in zip(agent_names, results):
                if isinstance(result, Exception):
                    log(f"  ⚠️ {name} failed: {result}")
                    new_evidence[name] = {"summary": f"Error: {result}", "error": True}
                    record_evidence(name, new_evidence[name])
                    emit({"event": "evidence", "agent": name, "summary": f"Error: {result}", "success": False})
                else:
                    if not isinstance(result, dict):
                        result = {'error': True, 'summary': 'Invalid evidence agent result'}
                    if result.get('error'):
                        new_evidence[name] = result
                        record_evidence(name, result)
                        emit({'event': 'evidence', 'agent': name, 'summary': result.get('summary', ''), 'success': False})
                        log(f"  ⚠️ {name}: {result.get('summary', 'evidence unavailable')[:100]}")
                        continue
                    log(f"  ✅ {name}: {result.get('summary', '')[:100]}")
                    new_evidence[name] = result
                    record_evidence(name, result)
                    emit({"event": "evidence", "agent": name, "summary": result.get("summary", "")[:200], "success": True})

            elapsed = time.time() - start_time
            log(f"  Evidence collection: {elapsed:.1f}s")
            if getattr(cfg.pipeline, "enable_structured_evidence", True):
                structured_items = _extract_evidence_items(session.evidence)
                if structured_items:
                    session.evidence["structured_evidence"] = {
                        "agent": "structured_evidence",
                        "items": structured_items,
                        "summary": f"{len(structured_items)} normalized evidence observations collected.",
                    }
                    emit({
                        "event": "structured_evidence",
                        "count": len(structured_items),
                        "items": structured_items[:20],
                    })

            # Keep valid earlier sources available when a later source provides
            # a discriminator. Replacing a failed source still removes its old
            # observations from the current catalogue; history remains intact.
            ranking_evidence = session.evidence if propagation_lookup else _lifecycle_ranking_evidence(
                analysis_incident_query, namespace, session.evidence)
            catalog = evidence_catalog(ranking_evidence, session.observation_history)
            novelty = controller.observe(catalog)
            hypotheses_before = [h.to_dict() for h in session.hypotheses]
            if "hypothesis" in enabled_analysis_phases and novelty["novel_observation_ids"]:
                log("  Re-ranking hypotheses...")
                hypothesis_agent.set_evidence_catalog(catalog)
                session.hypotheses = hypothesis_agent.rerank(session.hypotheses, ranking_evidence)
                top = session.top_hypothesis()
                if top:
                    log(f"  Top hypothesis: [{top.id}] conf={top.confidence:.2f} — {top.description[:80]}")
                emit({"event": "hypotheses", "items": [h.to_dict() for h in session.hypotheses]})
            else:
                top = session.top_hypothesis()
                if "hypothesis" in enabled_analysis_phases:
                    log("  无新增差异观测；跳过模型重复重排，保留未确认分支。")

            strategy_record = controller.decide(iteration=iteration+1, before=hypotheses_before,
                after=[h.to_dict() for h in session.hypotheses], catalog=catalog, novelty=novelty,
                selected_agents=selected_agents, results=new_evidence, plan=plan, budget_remaining=llm.remaining_s(),
                model_unavailable=bool(getattr(llm,'terminal_failure',None)))
            strategy_record["branch_proposals"] = hypothesis_agent.branch_decisions if novelty["novel_observation_ids"] else []
            session.strategy_history.append(strategy_record)
            session.hypothesis_history.append({"event":"hypothesis_revision", "iteration":iteration+1,
                "before":strategy_record["before"],"after":strategy_record["after"]})
            emit(strategy_record)
            pending_agents = strategy_record["decision"]["next_agents"]

            session.iterations.append({
                "iteration": iteration + 1,
                "evidence_agents": list(new_evidence.keys()),
                "top_confidence": top.confidence if top else 0,
                "duration_s": round(elapsed, 1),
            })
            session.complete_phase(3, notes=f"Top confidence: {top.confidence:.2f}" if top else "")
            emit({"event": "phase_complete", "phase": 3, "name": f"INVESTIGATION_ITER_{iteration+1}", "notes": f"Top confidence: {top.confidence:.2f}" if top else ""})

            # Evidence-linked stopping is still followed by the independent Judge.
            propagation_observed = not propagation_lookup or all(
                new_evidence.get(name, {}).get('evidence_items')
                and not new_evidence.get(name, {}).get('error')
                for name in ('trace_agent', 'log_agent'))
            if strategy_record["decision"]["action"] == "stop":
                session.metadata["investigation_stop_reason"] = strategy_record["decision"]["reason"]
                if strategy_record["decision"]["reason"] != "cited_hypothesis_ready_for_independent_review" or not propagation_observed:
                    if propagation_lookup and not propagation_observed and iteration+1 < max_iter:
                        continue
                    break
                session.metadata["targeted_evidence_complete"] = True
                session.metadata["targeted_hypothesis_id"] = top.id
                session.metadata["targeted_evidence_agents"] = list(new_evidence.keys())
                log("  已引用观测的候选满足本轮停止条件；继续独立审核，不视为因果已确认。")
                break

        reasoning_evidence = session.evidence if propagation_lookup else _lifecycle_ranking_evidence(
            analysis_incident_query, namespace, session.evidence)

        # ── Step 4: Cross-Signal Correlation ──
        if cfg.pipeline.enable_correlation:
            session.add_phase(4, "CORRELATION")
            emit({"event": "phase_start", "phase": 4, "name": "CORRELATION"})
            log("\n🔗 Running cross-signal correlation...")
            try:
                correlation_result = correlation_agent.correlate(reasoning_evidence)
                session.evidence["correlation"] = correlation_result
                log(f"  Top suspect: {correlation_result.get('top_suspect') or '未形成可比较的目标异常矩阵'}")
            except Exception as e:
                correlation_result = {}
                log(f"  ⚠️ Correlation failed: {e}")
            session.complete_phase(4)
            emit({"event": "phase_complete", "phase": 4, "name": "CORRELATION"})
        else:
            correlation_result = {}

        # ── Step 5: Graph RCA Localization ──
        if cfg.pipeline.enable_graph_rca:
            session.add_phase(5, "GRAPH_RCA")
            emit({"event": "phase_start", "phase": 5, "name": "GRAPH_RCA"})
            log("\n📊 Running graph-based RCA localization...")
            try:
                rca_tool = registry.get("rca_localization")
                if rca_tool and correlation_result:
                    ranked = correlation_result.get("anomaly_matrix", {}).get("ranked_services", [])
                    anomaly_scores = {s["service"]: s["composite_score"] for s in ranked}
                    if anomaly_scores:
                        rca_result = rca_tool.execute(anomaly_scores=anomaly_scores)
                        if rca_result.success:
                            session.evidence["graph_rca"] = rca_result.data
                            log(f"  Graph RCA top: {rca_result.data.get('top_root_cause', 'N/A')}")
                    else:
                        log("  当前目标缺少可用于图定位的异常评分，跳过图定位；保留原始观测供复核。")
            except Exception as e:
                log(f"  ⚠️ Graph RCA failed: {e}")
            session.complete_phase(5)
            emit({"event": "phase_complete", "phase": 5, "name": "GRAPH_RCA"})

        # ── Step 6: Final Report ──
        session.add_phase(6, "FINAL_REPORT")
        emit({"event": "phase_start", "phase": 6, "name": "FINAL_REPORT"})
        log("\n📝 Generating final RCA report...")

        hyp_text = "\n".join([
            f"[{h.id}] conf={h.confidence:.2f} — {h.description}"
            for h in session.hypotheses[:5]
        ])
        evidence_text = format_evidence(reasoning_evidence, limit=6000)
        report_prompt = (alert_report_prompt(analysis_incident_query, session.evidence, hyp_text)
            if alert_payload(session.evidence) else FINAL_REPORT_PROMPT.format(
                incident=analysis_incident_query[:8000], hypotheses=hyp_text,
                evidence=evidence_text[:6000], correlation=str(correlation_result.get('summary', ''))[:2000]))

        try:
            final_result = llm.json_chat([
                {"role": "system", "content": "你是资深 SRE，必须使用中文输出最终 RCA 报告。不要在根因、证据、建议中混入英文固定短语，除非是对象名、命令、指标名或 Kubernetes 状态枚举。"},
                {"role": "user", "content": report_prompt
                    + f"\n本次实际 Kubernetes namespace 是 {namespace!r}；cluster|namespace 是复合引用，不是 namespace。"}
            ], thinking=False)
            if (not isinstance(final_result,dict) or final_result.get('parse_error')
                    or not isinstance(final_result.get('root_cause'),str) or not final_result['root_cause'].strip()):
                raise ValueError('invalid_final_report')
        except Exception as e:
            from tools.llm_client import ModelCallError
            failure={'stage':'final_report','code':e.code if isinstance(e,ModelCallError) else 'invalid_or_unavailable_report'}
            session.metadata.setdefault('failed_model_stages',[]).append(failure)
            log('  ⚠️ 模型报告不可用；保留当前调查范围并要求人工复核。')
            top = session.top_hypothesis()
            final_result = _unavailable_model_report(analysis_incident_query,namespace,top)

        final_result = ground_state_timeline(final_result, session.evidence)
        final_result['diagnosis_status'] = 'pending_review'
        session.result = final_result
        log(f"\n🎯 根因：{final_result.get('root_cause', 'N/A')}")
        log(f"   置信度：{final_result.get('confidence', 0)}")
        report_status = 'degraded' if session.metadata.get('failed_model_stages') else 'completed'
        session.complete_phase(6, status=report_status)
        emit({"event": "phase_complete", "phase": 6, "name": "FINAL_REPORT", "status": report_status})
        emit({"event": "result", "data": final_result})

        # ── Step 6.5: Collaboration Critic / Reflection ──
        collaboration_critique = None
        if collaboration_optimizer is not None and collaboration_policy.require_reflection:
            session.add_phase(10, "COLLABORATION_REFLECTION")
            emit({"event": "phase_start", "phase": 10, "name": "COLLABORATION_REFLECTION"})
            log("\n🧩 Running collaboration critic reflection...")
            try:
                reflection_timeout_s = max(
                    1,
                    int(getattr(cfg.pipeline, "collaboration_reflection_timeout_s", 45) or 45),
                )
                collaboration_critique = await asyncio.wait_for(
                    run_sync_in_daemon_thread(
                        collaboration_optimizer.critique_report,
                        incident_query=incident_query,
                        final_result=final_result,
                        evidence=session.evidence,
                        policy=collaboration_policy,
                    ),
                    timeout=reflection_timeout_s,
                )
                if collaboration_critique.get("needs_revision"):
                    final_result = await asyncio.wait_for(
                        run_sync_in_daemon_thread(
                            collaboration_optimizer.improve_report,
                            incident_query=incident_query,
                            final_result=final_result,
                            evidence=session.evidence,
                            critique=collaboration_critique,
                            policy=collaboration_policy,
                        ),
                        timeout=reflection_timeout_s,
                    )
                    if final_result.get("revision_error"):
                        collaboration_critique["needs_human_review"] = True
                        collaboration_critique["revision_error"] = final_result["revision_error"]
                    session.result = final_result
                    log(f"  Refined confidence: {final_result.get('confidence', 0)}")
                log(f"  Critic quality: {collaboration_critique.get('quality_score', 0):.3f}")
                emit({"event": "collaboration_critique", "data": collaboration_critique})
                emit({"event": "result", "data": final_result})
            except asyncio.TimeoutError:
                log(
                    f"  ⚠️ Collaboration reflection exceeded {reflection_timeout_s}s; "
                    "continuing with review-required fallback"
                )
                collaboration_critique = {
                    "error": "collaboration_reflection_timeout",
                    "quality_score": 0.5,
                    "needs_revision": False,
                    "needs_human_review": True,
                    "weaknesses": ["Critic LLM 未在时限内返回，最终结论需要人工复核。"],
                    "missing_evidence": [],
                    "alternative_hypotheses": [],
                    "confidence_adjustment": -0.1,
                    "recommended_focus": ["复核当前状态证据与最终根因是否一致。"],
                }
            except Exception as e:
                log(f"  ⚠️ Collaboration reflection failed: {e}")
                collaboration_critique = {"error": str(e), "needs_human_review": True}
            session.complete_phase(10)
            emit({"event": "phase_complete", "phase": 10, "name": "COLLABORATION_REFLECTION"})

        root_before_alignment = final_result.get("root_cause")
        final_result = _align_root_cause_with_targeted_evidence(
            final_result,
            evidence=session.evidence,
            hypotheses=session.hypotheses,
            targeted_evidence_complete=bool(session.metadata.get("targeted_evidence_complete")),
        )
        if final_result.get("root_cause") != root_before_alignment:
            log(
                "  Current-state evidence aligned root cause: "
                f"{final_result.get('root_cause_alignment', {}).get('reason')}"
            )
            emit({"event": "result", "data": final_result})

        if collaboration_optimizer is not None:
            final_result = collaboration_optimizer.calibrate_fault_type(
                final_result=final_result,
                evidence=session.evidence,
                policy=collaboration_policy,
            )
            session.result = final_result
            if final_result.get("fault_type_original"):
                log(
                    "  Fault type calibrated: "
                    f"{final_result.get('fault_type_original')} → {final_result.get('fault_type')}"
                )
                emit({"event": "result", "data": final_result})

        final_result = ground_state_timeline(final_result, session.evidence)
        session.result = final_result

        # Expand only explicit catalog selections before the reviewer sees
        # claims. Free-form quotes remain unchanged and face literal validation.
        from agents.diagnosis_grounding import resolve_quote_selections
        final_result = resolve_quote_selections(final_result, session.evidence)
        session.result = final_result

        # ── Step 7: Quality Judge ──
        if scoped_lifecycle and not session.metadata.get('failed_model_stages'):
            final_result=bind_report_entity(final_result,session.evidence)
            session.result=final_result
        session.add_phase(7, "QUALITY_JUDGE")
        emit({"event": "phase_start", "phase": 7, "name": "QUALITY_JUDGE"})
        log("\n⚖️ Running quality assessment...")
        review_context=candidate_review_context()
        try:
            reasoning = final_result.get("reasoning_chain", str(final_result))
            judge_result = judge.judge(
                reasoning=reasoning,
                root_cause=final_result.get("root_cause", ""),
                confidence=final_result.get("confidence", 0),
                evidence=session.evidence,
                claims=final_result.get('diagnosis_claims') if alert_payload(session.evidence) else None,
                hypothesis_context=review_context,
            )
            log(f"  Judge level: {judge_result['judge_level']}, score: {judge_result['combined_score']:.3f}")
            if judge_result["needs_review"]:
                log("  ⚠️ Flagged for review — low quality reasoning")
        except Exception as e:
            log(f"  ⚠️ Quality judge failed: {e}")
            judge_result = {"judge_level": "bronze", "combined_score": 0.0, "needs_review": True}
        record_candidate_review(judge_result,review_context,"initial_quality_review")
        session.complete_phase(7)
        emit({"event": "phase_complete", "phase": 7, "name": "QUALITY_JUDGE"})
        emit({"event": "judge", "data": judge_result})

        recovery_history_stats = {}
        if store is not None:
            try:
                recovery_history_stats = store.resolution_stats(
                    fault_type=final_result.get("fault_type", ""),
                    error_pattern=final_result.get("error_pattern", ""),
                )
            except Exception as e:
                log(f"  ⚠️ Historical resolution stats failed: {e}")
                recovery_history_stats = {}
        try:
            history_stats = {
                **recovery_history_stats,
                **feedback_store.diagnosis_stats(
                    fault_type=final_result.get("fault_type", ""),
                    error_pattern=final_result.get("error_pattern", ""),
                    memory_scope=memory_scope,
                ),
            }
        except Exception as e:
            log(f"  ⚠️ Historical diagnosis stats failed: {e}")
            history_stats = recovery_history_stats

        final_result = calibrate_rca_confidence(
            final_result,
            evidence=session.evidence,
            hypotheses=session.hypotheses,
            judge_result=judge_result,
            critique=collaboration_critique,
            history_stats=history_stats,
        )
        session.result = final_result
        log(
            "  Calibrated confidence: "
            f"{final_result.get('confidence_raw', final_result.get('confidence', 0))} → {final_result.get('confidence', 0)}"
        )
        emit({"event": "result", "data": final_result})

        # ── Step 7.2: Diagnosis closure loop ──
        closure_result = None
        closure_plan = closure_manager.build_plan(
            rca_result=final_result,
            judge_result=judge_result,
            critique=collaboration_critique,
            evidence=session.evidence,
            targeted_evidence_complete=bool(session.metadata.get("targeted_evidence_complete")),
        )
        if llm.remaining_s() <= 0:
            closure_plan.should_iterate = False
            closure_plan.reason = "RCA model budget exhausted; preserve evidence and request human review."
            judge_result["needs_review"] = True
        if getattr(llm,'terminal_failure',None):
            closure_plan.should_iterate = False
            closure_plan.reason = 'Model service unavailable; preserve observations and request human review.'
            judge_result['needs_review'] = True
        # A timed-out critic already produced an explicit human-review flag.
        # Do not launch a second, broad closure round after targeted evidence
        # has confirmed the Top1 hypothesis; that would duplicate slow LLM
        # calls without adding a new signal and could leave RCA running for
        # minutes. Preserve the review gate instead.
        if (
            closure_plan.should_iterate
            and session.metadata.get("targeted_evidence_complete")
            and "hypothesis_review_disagreement" not in closure_plan.failure_modes
            and isinstance(collaboration_critique, dict)
            and collaboration_critique.get("error") == "collaboration_reflection_timeout"
        ):
            closure_plan.should_iterate = False
            closure_plan.reason = (
                "Targeted evidence confirmed Top1; critic timeout is retained as a human-review flag "
                "without starting a duplicate closure round."
            )
        if closure_plan.should_iterate:
            session.add_phase(11, "DIAGNOSIS_CLOSURE")
            emit({"event": "phase_start", "phase": 11, "name": "DIAGNOSIS_CLOSURE"})
            emit({"event": "diagnosis_closure", "data": {"plan": closure_plan.to_dict(), "status": "started"}})
            log("\n🔁 Running diagnosis closure loop...")
            log(f"  Closure reason: {closure_plan.reason}")
            log(f"  Target agents: {', '.join(closure_plan.target_agents)}")

            closure_evidence = {}
            agent_map = {
                "metric_agent": metric_agent,
                "log_agent": log_agent,
                "trace_agent": trace_agent,
                "event_agent": event_agent,
                "llm_inference_agent": llm_inference_agent,
            }

            async def _run_closure_agent(agent_name: str):
                query = closure_plan.focus_queries.get(agent_name, incident_query)
                query = _agent_query(query, agent_context, collaboration_policy, collaboration_optimizer, agent_name)
                agent = agent_map[agent_name]
                if agent_name == "trace_agent":
                    return agent_name, await _run_agent_with_timeout(agent_name, agent.analyze(query, namespace=namespace,
                        **({'trace_ids': propagation_lookup['trace_ids']} if propagation_lookup else {})), timeout_s)
                if agent_name == "log_agent":
                    return agent_name, await _run_agent_with_timeout(agent_name, agent.analyze(query, namespace,
                        **({'pod_name': propagation_lookup['pod_name']} if propagation_lookup else {})), timeout_s)
                if agent_name == "llm_inference_agent":
                    return agent_name, await _run_agent_with_timeout(agent_name, agent.analyze(query, namespace=namespace), timeout_s)
                return agent_name, await _run_agent_with_timeout(agent_name, agent.analyze(query, namespace), timeout_s)

            closure_tasks = [
                _run_closure_agent(agent_name)
                for agent_name in closure_plan.target_agents
                if agent_name in agent_map
            ]
            if closure_tasks:
                closure_results = await asyncio.gather(*closure_tasks, return_exceptions=True)
                for item in closure_results:
                    if isinstance(item, Exception):
                        key = f"closure_error_{len(closure_evidence)+1}"
                        closure_evidence[key] = {
                            "summary": f"Error: {item}",
                            "error": True,
                        }
                        record_evidence(key, closure_evidence[key])
                        emit({"event": "evidence", "agent": key, "success": False,
                              "summary": closure_evidence[key]["summary"]})
                        continue
                    agent_name, result = item
                    key = f"{agent_name}_closure"
                    closure_evidence[key] = result
                    record_evidence(key, result)
                    emit({
                        "event": "evidence",
                        "agent": key,
                        "summary": result.get("summary", "")[:200] if isinstance(result, dict) else str(result)[:200],
                        "success": isinstance(result, dict) and not result.get("error"),
                    })

            if closure_evidence:
                revised = closure_manager.revise_report(
                    incident_query=incident_query,
                    original_result=final_result,
                    original_evidence=session.evidence,
                    closure_evidence=closure_evidence,
                    closure_plan=closure_plan,
                )
                if collaboration_optimizer is not None:
                    revised = collaboration_optimizer.calibrate_fault_type(
                        final_result=revised,
                        evidence=session.evidence,
                        policy=collaboration_policy,
                    )
                if scoped_lifecycle:
                    revised=bind_report_entity(revised,session.evidence)
                final_result = resolve_quote_selections(revised, session.evidence)
                session.result = final_result
                reasoning = final_result.get("reasoning_chain", str(final_result))
                review_context=candidate_review_context()
                try:
                    judge_result = judge.judge(
                        reasoning=reasoning,
                        root_cause=final_result.get("root_cause", ""),
                        confidence=final_result.get("confidence", 0),
                        evidence=session.evidence,
                        claims=final_result.get('diagnosis_claims') if alert_payload(session.evidence) else None,
                        hypothesis_context=review_context,
                    )
                except Exception as e:
                    log(f"  ⚠️ Closure re-judge failed: {e}")
                    judge_result = {**judge_result, "needs_review": True}
                    judge_result.pop("hypothesis_review",None)
                record_candidate_review(judge_result,review_context,"closure_quality_review")

                final_result = calibrate_rca_confidence(
                    final_result,
                    evidence=session.evidence,
                    hypotheses=session.hypotheses,
                    judge_result=judge_result,
                    critique=collaboration_critique,
                    history_stats=history_stats,
                )
                session.result = final_result

                closure_result = {
                    "plan": closure_plan.to_dict(),
                    "evidence_agents": list(closure_evidence.keys()),
                    "revised_result": final_result,
                    "judge_after": judge_result,
                }
                log(f"  Closure judge score: {judge_result.get('combined_score', 0):.3f}")
                emit({"event": "result", "data": final_result})
                emit({"event": "judge", "data": judge_result})
                emit({"event": "diagnosis_closure", "data": {"status": "completed", **closure_result}})
            else:
                closure_result = {"plan": closure_plan.to_dict(), "evidence_agents": [], "status": "no_evidence"}
                emit({"event": "diagnosis_closure", "data": closure_result})

            session.complete_phase(11)
            emit({"event": "phase_complete", "phase": 11, "name": "DIAGNOSIS_CLOSURE"})

        final_result = ground_state_timeline(final_result, session.evidence)
        session.result = final_result

        if propagation_lookup:
            identity_ok = _propagation_root_identity_observed(final_result, session.evidence, namespace)
            final_result['root_entity_validation'] = {
                'observed': identity_ok, 'scope': 'entity identity only; not causal confirmation',
            }
            if not identity_ok:
                judge_result = {**judge_result, 'needs_review': True, 'judge_level': 3,
                                'identity_error': 'root entity missing or not observed in target namespace'}
                final_result['root_entity_validation']['confidence_before_cap'] = final_result.get('confidence', 0)
                final_result['root_entity_validation']['confidence_cap'] = 0.65
                final_result['confidence'] = min(float(final_result.get('confidence', 0)), 0.65)

        final_result, judge_result, model_execution_incomplete = _apply_model_completion_gate(
            final_result, judge_result, llm,failed_stages=session.metadata.get('failed_model_stages',[]))
        final_result, judge_result = apply_alert_grounding(final_result, judge_result, session.evidence)
        final_result = ground_state_timeline(final_result, session.evidence)
        from diagnostic_strategy import apply_investigation_readiness_gate
        final_result, judge_result = apply_investigation_readiness_gate(
            final_result, judge_result, session.strategy_history)
        action_blockers = list(judge_result.get('review_blockers', []))
        if judge_result.get('needs_review') is not False:
            action_blockers.append('quality_review_required')
        if (collaboration_critique or {}).get('needs_human_review'):
            action_blockers.append('collaboration_review_required')
        automatic_actions_blocked = bool(action_blockers)
        final_result['automation_gate'] = {'allowed': not automatic_actions_blocked,
            'blockers': list(dict.fromkeys(action_blockers))}
        final_result['diagnosis_status'] = ('requires_review' if automatic_actions_blocked
            else final_result.get('diagnosis_status', 'reviewed'))
        if final_result['diagnosis_status'] == 'pending_review':
            final_result['diagnosis_status'] = 'reviewed'
        final_result.update(memory_scope.source_fields())
        session.result = final_result
        emit({"event": "result", "data": final_result})
        emit({"event": "judge", "data": judge_result})
        if model_execution_incomplete:
            log("  ⚠️ 模型响应不完整或调用超时；保留观测并要求人工复核，停用自动学习与恢复。")
            emit({"event": "result", "data": final_result})
            emit({"event": "judge", "data": judge_result})

        # ── Step 7.5: Human-in-the-loop quality gate ──
        review_result = None
        if automatic_actions_blocked:
            try:
                priority = "high" if judge_result.get("combined_score", 0) < 0.45 else "medium"
                reasons = ["RCA quality judge marked the result as needing human review."] if judge_result.get("needs_review") else []
                if (collaboration_critique or {}).get("needs_human_review"):
                    reasons.append("Collaboration critic requested human review for weak or conflicting evidence.")
                review_result = review_store.create_review(
                    incident_id=session_id,
                    reason=" ".join(reasons),
                    rca_result=final_result,
                    judge={**judge_result, "collaboration_critique": collaboration_critique, "closure": closure_result},
                    priority=priority,
                    source="rca_quality_gate",
                )
                log(f"  Human review queued: {review_result.get('review_id')}")
                emit({"event": "human_review", "data": review_result})
            except Exception as e:
                log(f"  ⚠️ Failed to queue human review: {e}")

        # ── Step 8: Auto-Learning ──
        if learner and cfg.memory.auto_learn and not automatic_actions_blocked and not _is_no_fault_result(final_result):
            session.add_phase(8, "AUTO_LEARNING")
            emit({"event": "phase_start", "phase": 8, "name": "AUTO_LEARNING"})
            log("\n📖 Auto-learning from this incident...")
            learn_result = learner.learn_from_trace(
                reasoning_trace=reasoning,
                root_cause=final_result.get("root_cause", ""),
                confidence=final_result.get("confidence", 0),
                judge_level=judge_result["judge_level"],
                memory_scope=memory_scope, source_incident_id=session_id,
            )
            learner.store_fault_context(
                incident_query, final_result, session.evidence,
                [h.to_dict() for h in session.hypotheses],
                memory_scope=memory_scope, source_incident_id=session_id,
            )
            log(f"  Rules added: {learn_result.get('rules_added', 0)}")
            # Bump usage_count for rules that were surfaced into the prompt — feedback votes come later
            used_rule_ids = session.metadata.get("used_rule_ids", [])
            if used_rule_ids and store is not None:
                try:
                    bump = store.record_rule_usage(used_rule_ids, positive=None,
                        expected_revisions={ref['rule_id']: ref['revision'] for ref in session.metadata.get('recalled_rule_refs', [])})
                    log(f"  Rule usage bumped: {bump.get('updated', 0)} of {len(used_rule_ids)}")
                except Exception as e:
                    log(f"  ⚠️ record_rule_usage failed: {e}")
            session.complete_phase(8)
            emit({"event": "phase_complete", "phase": 8, "name": "AUTO_LEARNING"})
        elif learner and cfg.memory.auto_learn:
            log("\n📖 Skipping auto-learning while review is required" if automatic_actions_blocked
                else "\n📖 Skipping auto-learning for no-fault RCA result")

        # ── Step 9: Optional Self-Healing ──
        if cfg.pipeline.enable_recovery and not automatic_actions_blocked and not _is_no_fault_result(final_result) and final_result.get("confidence", 0) >= cfg.remediation.confidence_threshold:
            session.add_phase(9, "RECOVERY")
            emit({"event": "phase_start", "phase": 9, "name": "RECOVERY"})
            log("\n🛠️ Initiating self-healing...")
            remediation_agent = RemediationAgent(llm, registry, cfg)
            rem_result = await remediation_agent.remediate(final_result, final_result.get("confidence", 0))
            session.evidence["remediation"] = rem_result
            log(f"  Remediation status: {rem_result.get('status', 'N/A')}")
            if rem_result.get("status") == "pending_approval":
                log(f"  📋 Remediation plan generated, waiting for approval")
                plan_actions = rem_result.get("plan", {}).get("actions", [])
                for a in plan_actions:
                    log(f"    [{a.get('risk_level','?')}] {a.get('description','')}")
            emit({"event": "remediation", "data": rem_result})
            session.complete_phase(9)
            emit({"event": "phase_complete", "phase": 9, "name": "RECOVERY"})

        # Surface used rule_ids before the evolution snapshot reads them — the snapshot
        # populates rules_recalled_count / rules_used_count from final_result.used_rule_ids.
        final_result["used_rule_ids"] = session.metadata.get("used_rule_ids", [])
        final_result["recalled_rule_refs"] = session.metadata.get("recalled_rule_refs", [])
        final_result["rule_usage_semantics"] = "context_exposure_not_causal_contribution"

        # ── Step 10: Evolution Snapshot ──
        evolution_snapshot = None
        evolution_report = None
        if evolution_tracker is not None and cfg.evolution.auto_record:
            try:
                evolution_payload = {
                    "result": final_result,
                    "judge": judge_result,
                    "metrics": {"latency_s": time.time() - pipe_trace.start_time if hasattr(pipe_trace, "start_time") else 0},
                    "collaboration": {
                        "policy": collaboration_policy.to_dict(),
                        "critique": collaboration_critique,
                        "closure": closure_result,
                    },
                }
                evolution_snapshot = evolution_tracker.record_snapshot(
                    fault_store=store,
                    feedback_store=feedback_store,
                    trace_store=trace_store,
                    rca_result=evolution_payload,
                    paradigm_name="rca_engine",
                    incident_query=incident_query,
                )
                evolution_report = evolution_tracker.get_evolution_report(fault_store=store)
                emit({"event": "evolution", "data": {"snapshot": evolution_snapshot, "report": evolution_report}})
            except Exception as e:
                log(f"  ⚠️ Evolution snapshot failed: {e}")

        # Complete — used_rule_ids was already attached to final_result above the snapshot
        # block so trace_store/expert_feedback can later close the recall→feedback loop.
        session.status = "completed"
        final_result["model_budget"] = {
            "remaining_s": round(llm.remaining_s(), 1),
            "call_timeouts": llm.timeout_count,
            "incomplete_responses": list(getattr(llm, 'incomplete_responses', [])),
            "failed_calls": list(getattr(llm,'failed_calls',[])),
            "failed_call_count": getattr(llm,'failed_call_count',0),
            "failed_stages": list(session.metadata.get('failed_model_stages',[])),
            "terminal_failure": getattr(llm,'terminal_failure',None),
            "exhausted": llm.remaining_s() <= 0,
        }
        trace_store.complete_pipeline(session_id, final_result)
        
        return {
            "session_id": session_id,
            "status": "completed",
            "history_retrieval": session.metadata.get('history_retrieval', {}),
            "result": final_result,
            "judge": judge_result,
            "collaboration": {
                "policy": collaboration_policy.to_dict(),
                "critique": collaboration_critique,
                "closure": closure_result,
            },
            "human_review": review_result,
            "evolution": {
                "snapshot": evolution_snapshot,
                "report": evolution_report,
            },
            "hypotheses": [h.to_dict() for h in session.hypotheses],
            "phases": session.phases,
            "iterations": session.iterations,
            "evidence_agents": list(session.evidence.keys()),
            "observations": session.observation_history,
            "collection_history": session.collection_history,
            "hypothesis_history": session.hypothesis_history,
            "diagnostic_strategy": {"generation":controller.generation,"rounds":session.strategy_history,
                "independent_reviews":session.hypothesis_review_history,
                "stop_reason":session.metadata.get("investigation_stop_reason", "model_budget_or_loop_limit"),
                "cross_incident_policy_learned":False},
            "analysis_phases": [phase for phase in ["context", "hypothesis", "plan", "investigation", "reasoning"] if phase in enabled_analysis_phases],
        }

    except Exception as e:
        session.status = "failed"
        logger.error(f"RCA pipeline failed: {e}", exc_info=True)
        log(f"\n❌ Pipeline failed: {e}")
        return {
            "session_id": session_id,
            "status": "failed",
            "history_retrieval": session.metadata.get('history_retrieval', {}),
            "error": str(e),
            "phases": session.phases,
            "observations": session.observation_history,
            "collection_history": session.collection_history,
            "hypothesis_history": session.hypothesis_history,
            "diagnostic_strategy": {"rounds":session.strategy_history,
                "independent_reviews":session.hypothesis_review_history,"stop_reason":"pipeline_failed"},
        }
