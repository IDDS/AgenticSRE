"""
AgenticSRE Collaboration Optimizer

Lightweight production-oriented optimizer for multi-agent RCA orchestration.
It operationalizes ideas from the research report:
- A-Mem / ExpeL: retrieve and compress reusable diagnostic memory.
- OMAC / AgentFlow: choose a collaboration workflow from task and history.
- Reflection / Critic: review the final RCA and trigger targeted refinement.
"""

from __future__ import annotations

import logging
import math
from agents.evidence import format_evidence
from operational_ontology import observation_entity
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from agents.evidence_adjudicator import CAUSAL_ADJUDICATION_RULES
from memory import AgentContext, ContextBuilder, DomainAdapter

logger = logging.getLogger(__name__)


def root_scoped_evidence(report: Dict[str, Any], evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Select explicit object identities; namespace-wide summaries prove no cause.

    Unscoped observations remain in the session for investigation and review,
    but cannot authorize a deterministic rewrite of a named object's diagnosis.
    """
    entity = report.get("root_entity")
    if not isinstance(entity, dict) or not all(isinstance(entity.get(k), str) and entity[k].strip()
                                             for k in ("namespace", "kind", "name")):
        return {}
    name, namespace, kind = entity["name"], entity["namespace"], entity["kind"].lower()
    scoped = {}
    for agent, payload in evidence.items():
        if not isinstance(payload, dict) or payload.get("error"):
            continue
        items = []
        for item in payload.get("evidence_items") or []:
            if not isinstance(item, dict) or not isinstance(item.get("raw_ref"), dict):
                continue
            raw = item["raw_ref"]
            observed_entity = observation_entity(item)
            # An explicit cluster or UID is a hard boundary, including missing
            # identity when a report claims one particular incarnation.
            if any(entity.get(key) and (not observed_entity or observed_entity.get(key) != entity[key])
                   for key in ("uid", "cluster_id")):
                continue
            observed_name = raw.get("name") or raw.get("pod") or raw.get("deployment") or item.get("service")
            if observed_name != name or raw.get("namespace") != namespace:
                continue
            signal = str(item.get("signal_type") or "")
            observed_kind = str(raw.get("kind") or ("Pod" if signal.startswith(("pod_", "container_"))
                else "Deployment" if signal.startswith("deployment_") else "")).lower()
            # An explicitly different resource kind is a different object.
            # Endpoint evidence can join a Deployment only when its raw reference
            # names that Deployment; matching names alone do not establish a join.
            if observed_kind and observed_kind != kind:
                continue
            if signal.startswith("service_") and kind != "service" and raw.get("deployment") != name:
                continue
            items.append(item)
        if items:
            scoped[agent] = {"evidence_items": items}
    return scoped


@dataclass
class CollaborationPolicy:
    """Runtime policy for one RCA session."""

    strategy: str = "plan_and_execute"
    rationale: str = ""
    max_iterations: int = 3
    confidence_threshold: float = 0.85
    agents: List[str] = field(default_factory=lambda: [
        "metric_agent", "log_agent", "trace_agent", "event_agent"
    ])
    require_reflection: bool = True
    reflection_rounds: int = 1
    require_human_review: bool = False
    memory_mode: str = "enriched"
    evidence_focus: List[str] = field(default_factory=list)
    sow_methods: List[str] = field(default_factory=lambda: [
        "A-Mem/ExpeL memory injection",
        "OMAC-style dynamic team selection",
        "Reflection critic quality gate",
    ])

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class CollaborationOptimizer:
    """
    Selects and applies multi-agent collaboration policy.

    This class intentionally avoids replacing existing agents. It optimizes
    orchestration around them so the Web UI, daemon, and evaluation runners keep
    using the same RCA entry point.
    """

    _FAULT_HINTS = {
        "cpu": ["cpu", "throttl", "stress", "load", "saturat", "高负载", "CPU"],
        "memory": ["memory", "oom", "mem", "内存", "OOMKilled"],
        "network": ["network", "dns", "timeout", "latency", "连接", "超时"],
        "k8s": ["CrashLoopBackOff", "ImagePull", "NodeNotReady", "Evicted", "pod", "node"],
        "dependency": ["redis", "mongo", "mysql", "dependency", "downstream", "上游", "下游"],
        "llm_inference": [
            "vllm", "llm", "gpu", "cuda", "nccl", "kv cache", "ttft", "tpot",
            "tokenizer", "safetensor", "inference", "推理", "大模型", "显存", "智算",
        ],
    }

    def __init__(
        self,
        cfg,
        fault_store=None,
        feedback_store=None,
        trace_store=None,
        domain_adapter=None,
        llm=None,
    ):
        self.cfg = cfg
        self.fault_store = fault_store
        self.feedback_store = feedback_store
        self.trace_store = trace_store
        self.domain_adapter = domain_adapter or self._safe_domain_adapter()
        self.llm = llm
        self.context_builder = ContextBuilder(
            fault_store=fault_store,
            feedback_store=feedback_store,
            domain_adapter=self.domain_adapter,
            trace_store=trace_store,
        )

    def _safe_domain_adapter(self):
        try:
            return DomainAdapter.from_config()
        except Exception as e:
            logger.debug("DomainAdapter init failed: %s", e)
            return None

    def build_context(self, incident_query: str, historical_context=None, memory_scope=None) -> AgentContext:
        """Build compact reusable context for the RCA session."""
        try:
            return self.context_builder.build_context(incident_query, historical_context=historical_context,
                memory_scope=memory_scope)
        except Exception as e:
            logger.warning("[CollaborationOptimizer] context build failed: %s", e)
            return AgentContext()

    def select_policy(self, incident_query: str, context: Optional[AgentContext] = None) -> CollaborationPolicy:
        """
        Choose a collaboration policy from incident features and recent memory.

        Heuristics are deliberately conservative and explainable; they can later
        be replaced by learned policy search when enough labelled traces exist.
        """
        context = context or AgentContext()
        text = incident_query.lower()
        signals = self._detect_signals(incident_query)
        fault_count = len([k for k, v in signals.items() if v])
        similar_faults = len(context.similar_faults)
        expert_feedback = len(context.expert_feedback)
        recent_traces = context.recent_traces or []

        policy = CollaborationPolicy(
            max_iterations=getattr(self.cfg.pipeline, "max_evidence_iterations", 3),
            confidence_threshold=getattr(self.cfg.pipeline, "hypothesis_confidence_threshold", 0.85),
        )

        if signals.get("llm_inference"):
            policy.strategy = "llm_inference_plan"
            policy.rationale = "Incident targets vLLM/GPU inference runtime; include specialized inference evidence."
            policy.agents = ["llm_inference_agent", "metric_agent", "event_agent", "log_agent"]
            policy.confidence_threshold = min(policy.confidence_threshold, 0.8)
            policy.evidence_focus = ["vLLM runtime metrics", "GPU/CUDA/NCCL symptoms", "model asset and tokenizer state"]
        elif fault_count >= 3 or any(w in text for w in ["multi", "cascade", "级联", "复合", "multiple"]):
            policy.strategy = "debate_reflection"
            policy.rationale = "Incident spans multiple signal families; use bounded debate plus critic review."
            policy.reflection_rounds = 2
            policy.evidence_focus = ["competing hypotheses", "cross-service propagation", "blast radius"]
        elif any(signals[k] for k in ["network", "dependency"]):
            policy.strategy = "react_plan_hybrid"
            policy.rationale = "Network/dependency incidents need adaptive tool selection while preserving auditability."
            policy.confidence_threshold = min(policy.confidence_threshold, 0.8)
            policy.evidence_focus = ["dependency health", "timeouts", "service-to-service correlation"]
        elif similar_faults >= 2 or expert_feedback:
            policy.strategy = "memory_augmented_plan"
            policy.rationale = "Similar memory exists; prioritize memory-guided plan-and-execute."
            policy.evidence_focus = ["memory-matched symptoms", "known remediation checks"]
        else:
            policy.strategy = "plan_and_execute"
            policy.rationale = "Default auditable hypothesis-driven workflow."
            policy.evidence_focus = ["events", "metrics", "logs", "traces"]

        if signals["cpu"]:
            policy.evidence_focus.extend(["CPU saturation", "CFS throttling", "noisy neighbor"])
        if signals["memory"]:
            policy.evidence_focus.extend(["OOMKilled", "working set growth", "memory limits"])
        if signals["k8s"]:
            policy.evidence_focus.extend(["pod lifecycle", "node readiness", "recent Kubernetes events"])
        if signals.get("llm_inference"):
            policy.evidence_focus.extend(["TTFT/TPOT", "KV cache", "request queue", "GPU utilization", "model loading errors"])

        if self._recent_quality_is_weak(recent_traces):
            policy.require_reflection = True
            policy.reflection_rounds = max(policy.reflection_rounds, 2)
            policy.require_human_review = True
            policy.rationale += " Recent quality signals are weak; tighten review gate."

        policy.evidence_focus = self._dedupe(policy.evidence_focus)[:10]
        return policy

    def enrich_incident_query(
        self,
        incident_query: str,
        context: Optional[AgentContext],
        policy: CollaborationPolicy,
        agent_name: str = "",
    ) -> str:
        """Inject compact memory, domain hints, and policy focus into an agent query."""
        if context is None or policy.memory_mode == "off":
            return incident_query

        context_block = context.to_context_string(agent_name=agent_name)
        policy_block = self._policy_context(policy, agent_name=agent_name)
        parts = [p for p in [context_block, policy_block] if p]
        if not parts:
            return incident_query
        return "\n\n".join(parts + [f"# Current Incident\n{incident_query}"])

    def critique_report(
        self,
        incident_query: str,
        final_result: Dict[str, Any],
        evidence: Dict[str, Any],
        policy: CollaborationPolicy,
    ) -> Dict[str, Any]:
        """Run a bounded critic pass over the final RCA report."""
        fallback = self._heuristic_critique(final_result, evidence, policy)
        if not self.llm or not policy.require_reflection:
            return fallback

        fallback = {**fallback, "needs_human_review": True, "error": "critic_unavailable_or_invalid"}
        prompt = f"""你是严格的 SRE Critic Agent。
请评审该 RCA 的证据支撑、根因具体性、故障类型准确性和运维可用性。
除 fault_type、服务名、命令、指标名、资源名等机器字段外，所有解释性内容必须使用中文。

Incident:
{incident_query[:1500]}

Collaboration Policy:
{policy.to_dict()}

RCA Report:
{str(final_result)[:3500]}

Evidence Summaries:
{self._format_evidence(evidence, limit=3500)}

只返回 JSON：
{{
  "quality_score": 0.0,
  "needs_revision": true,
  "needs_human_review": false,
  "weaknesses": ["中文问题说明"],
  "missing_evidence": ["中文缺失证据说明"],
  "alternative_hypotheses": ["中文备选假设"],
  "confidence_adjustment": -0.1,
  "recommended_focus": ["中文补充排查方向"]
}}"""
        try:
            critique = self.llm.json_chat([
                {"role": "system", "content": "你是 SRE RCA 评审专家。只返回合法 JSON，解释性内容使用中文。"},
                {"role": "user", "content": prompt},
            ], max_tokens=1536, thinking=False)
            if not isinstance(critique, dict) or critique.get("parse_error"):
                return fallback
            score = critique.get("quality_score")
            if (type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1
                    or any(type(critique.get(key)) is not bool for key in ("needs_revision", "needs_human_review"))
                    or any(not isinstance(critique.get(key), list) or
                           any(not isinstance(item, str) for item in critique[key])
                           for key in ("weaknesses", "missing_evidence", "alternative_hypotheses", "recommended_focus"))):
                return fallback
            for key, value in fallback.items():
                if key == "error":
                    continue
                critique.setdefault(key, value)
            return critique
        except Exception as e:
            logger.warning("[CollaborationOptimizer] critic failed: %s", e)
            return fallback

    def improve_report(
        self,
        incident_query: str,
        final_result: Dict[str, Any],
        evidence: Dict[str, Any],
        critique: Dict[str, Any],
        policy: CollaborationPolicy,
    ) -> Dict[str, Any]:
        """Generate a revised RCA report when critic identifies concrete gaps."""
        if not self.llm or not critique.get("needs_revision"):
            return self._apply_confidence_adjustment(final_result, critique)

        prompt = f"""你是资深 SRE，正在改进一份根因分析报告。
请根据 Critic 反馈让报告更具体、更有证据支撑。
不得编造证据；证据间接或冲突时要降低置信度。
root_cause 和 reasoning_chain 不写具体时间戳；时间只放 timeline，使用原始事件时间，不得把恢复与故障发生互换。
除 fault_type、服务名、命令、指标名、资源名等机器字段外，所有解释性内容必须使用中文。

在改写前必须逐项执行以下证据裁决规则；这些规则仅使用当前事故证据，不能使用注入动作、实验编号或隐藏真值：
{CAUSAL_ADJUDICATION_RULES}

Incident:
{incident_query[:1500]}

Original RCA:
{str(final_result)[:3500]}

Critic Feedback:
{str(critique)[:2500]}

Evidence Summaries:
{self._format_evidence(evidence, limit=3500)}

返回与原 RCA 报告一致的 JSON 结构，必须包含 root_entity。保留证据支持的实体身份；只有新证据支持另一根因时才改变实体，无法定位时填 null。
{{
  "root_cause": "具体、可执行的中文根因结论",
  "root_entity": {{"namespace": "观测命名空间", "kind": "资源类型", "name": "完整资源名称"}},
  "confidence": 0.0,
  "fault_type": "category",
  "affected_services": ["svc"],
  "timeline": [],
  "evidence_summary": {{"metrics": "中文指标证据", "logs": "中文日志证据", "traces": "中文链路证据", "events": "中文事件证据"}},
  "reasoning_chain": "中文证据推理链",
  "remediation_suggestion": "中文修复建议",
  "prevention": "中文预防措施"
}}"""
        try:
            improved = self.llm.json_chat([
                {"role": "system", "content": "你是资深 SRE RCA 作者。只返回合法 JSON，解释性内容使用中文。"},
                {"role": "user", "content": prompt},
            ], max_tokens=3072, thinking=False)
            if isinstance(improved, dict) and improved.get("root_cause"):
                if "root_entity" in final_result and "root_entity" not in improved:
                    retained = self._apply_confidence_adjustment(final_result, critique)
                    retained["revision_error"] = "missing_root_entity"
                    return retained
                improved["collaboration_refined"] = True
                return self._apply_confidence_adjustment(improved, critique)
        except Exception as e:
            logger.warning("[CollaborationOptimizer] report improvement failed: %s", e)

        return self._apply_confidence_adjustment(final_result, critique)

    def calibrate_fault_type(
        self,
        final_result: Dict[str, Any],
        evidence: Dict[str, Any],
        policy: CollaborationPolicy,
    ) -> Dict[str, Any]:
        """
        Normalize free-form LLM fault types to the operational taxonomy used by
        evaluation, dashboards, and SOW reporting.

        This preserves the detailed root cause while making the category
        comparable across incidents and paradigms.
        """
        adjusted = dict(final_result)
        original = str(adjusted.get("fault_type", "") or "")
        scoped = root_scoped_evidence(adjusted, evidence)
        text = " ".join([
            str(adjusted.get("root_cause", "")),
            original,
            self._format_evidence(scoped, limit=2500),
        ]).lower()

        mapped = self._map_fault_type(text, original)
        scoped_items = [item for value in scoped.values() for item in value.get('evidence_items', [])]
        pending = any(item.get('signal_type') == 'pod_waiting_state'
            and (item.get('raw_ref') or {}).get('phase') == 'Pending'
            and not (item.get('raw_ref') or {}).get('previous') for item in scoped_items)
        scheduling_warning = any((item.get('raw_ref') or {}).get('reason') == 'FailedScheduling'
                                 for item in scoped_items)
        if pending and scheduling_warning:
            mapped = 'scheduling_failure'
        observed_exit = self._target_exit_category(adjusted, scoped if adjusted.get("root_entity") else evidence)
        if mapped == 'image_pull_error' and observed_exit:
            mapped = observed_exit
        if mapped:
            adjusted["fault_type"] = mapped
            if original and original.lower() != mapped.lower():
                adjusted["fault_type_original"] = original
            adjusted["fault_type_reason"] = self._fault_type_reason(mapped, text)
            if pending and scheduling_warning:
                adjusted['fault_type_reason'] = 'Matching current target Pod is Pending and has an observed FailedScheduling event.'
            if observed_exit and mapped == observed_exit:
                adjusted['fault_type_reason'] = 'Matching live target container terminated with a nonzero exit code; no matching current image-pull waiting state was observed.'
        return adjusted

    @staticmethod
    def _target_exit_category(result, evidence):
        """A running container's observed exit must not become an image-pull failure."""
        services = result.get('affected_services')
        targets = {s for s in services if isinstance(s, str)} if isinstance(services, list) else set()
        root = str(result.get('root_cause') or '')
        exits, image_waiting = set(), False
        for payload in evidence.values():
            if not isinstance(payload, dict):
                continue
            for item in payload.get('evidence_items') or []:
                if not isinstance(item, dict) or not isinstance(item.get('raw_ref'), dict):
                    continue
                raw = item['raw_ref']
                name = str(raw.get('name') or '')
                if not name or not (name in targets or re.search(r'(?<![A-Za-z0-9_.-])'+re.escape(name)+r'(?![A-Za-z0-9_.-])', root)):
                    continue
                if raw.get('reason') in {'ImagePullBackOff', 'ErrImagePull'}:
                    image_waiting = True
                if item.get('signal_type') == 'container_terminated' and type(raw.get('exit_code')) is int and raw['exit_code'] != 0:
                    exits.add('resource_exhaustion' if raw.get('reason') == 'OOMKilled' else 'application_crash')
        return next(iter(exits)) if len(exits) == 1 and not image_waiting else None

    def _detect_signals(self, incident_query: str) -> Dict[str, bool]:
        return {
            name: any(h.lower() in incident_query.lower() for h in hints)
            for name, hints in self._FAULT_HINTS.items()
        }

    def _map_fault_type(self, text: str, original: str = "") -> str:
        text = str(text or "").lower()
        original_l = original.lower()
        canonical = {
            "scheduling_failure",
            "resource_exhaustion",
            "dependency_failure",
            "network_issue",
            "service_disruption",
            "image_pull_error",
            "application_crash",
            "configuration_error",
            "infrastructure",
            "compound_failure",
            "no_fault",
            "no_fault_detected",
            "unknown",
        }

        # Explicit Skill/LLM service-unavailable labels should normalize to
        # the dashboard taxonomy before free-form evidence is scanned. This
        # prevents a generic “connection failed” phrase from being promoted to
        # network_issue when current Endpoints/replica evidence is decisive.
        if original_l == "service_unavailable":
            return "service_disruption"
        if original_l in {"scheduling_failed", "failedscheduling", "schedulingfailure"}:
            return "scheduling_failure"
        if original_l in {"imagepullbackoff", "errimagepull", "imagepullerror", "image_pull_failure"}:
            return "image_pull_error"
        if original_l in canonical:
            return original_l
        # Observed failure states are not mechanisms. A report explaining that
        # ImagePullBackOff/OOM/exit evidence is missing must not be promoted by
        # keyword matching to one of those unconfirmed root causes.
        if original_l in {'job_failed', 'pod_failed', 'container_terminated'}:
            return original_l

        # Explicit runtime states are more specific than the downstream
        # symptom (zero ready endpoints) that they often cause.
        if any(k in text for k in [
            "imagepullbackoff", "errimagepull", "failed to pull image", "image pull failed",
            "镜像拉取失败", "拉取镜像失败",
        ]):
            return "image_pull_error"
        if any(k in text for k in [
            "crashloopbackoff", "crash loop", "exit code", "back-off restarting",
            "liveness probe", "readiness probe",
        ]):
            return "application_crash"
        if any(k in text for k in ["oomkilled", "out of memory"]):
            return "resource_exhaustion"

        # Endpoint/replica-zero is more specific than generic words such as
        # "resource" or "connection" in evidence summaries.
        if any(k in text for k in [
            "no ready endpoints", "no available ready replicas", "replicas=0", "replicas zero",
            "replica_zero", "scaled to 0", "service unavailable", "service endpoints empty",
            "endpoints empty", "无就绪端点", "无可用就绪副本", "没有提供任何就绪端点",
            "无就绪后端端点", "副本数为 0", "spec.replicas",
        ]):
            return "service_disruption"

        if any(k in text for k in [
            "cpu saturation", "cpu saturat", "cpu pressure", "cpu thrott", "cfs thrott",
            "resource exhaustion", "resource pressure", "memory pressure", "oom", "oomkilled",
            "quota", "insufficient resource", "noisy neighbor", "runaway process", "infinite loop",
        ]):
            return "resource_exhaustion"
        if any(k in text for k in [
            "redis", "mongodb", "mongo", "mysql", "postgres", "memcached", "cache",
            "dependency", "downstream", "upstream", "connection refused",
        ]):
            return "dependency_failure"
        if any(k in text for k in ["503"]):
            return "service_disruption"
        if any(k in text for k in [
            "network", "dns", "packet loss", "netem", "latency injection", "connection timeout",
        ]):
            return "network_issue"
        if any(k in text for k in [
            "configmap", "secret", "mount", "misconfiguration",
            "invalid configuration",
        ]):
            return "configuration_error"
        if any(k in text for k in [
            "nodenotready", "disk pressure", "node pressure", "kubelet", "container runtime",
            "infrastructure",
        ]):
            return "infrastructure"
        if any(k in text for k in ["compound", "multiple independent", "two independent", "co-occurring"]):
            return "compound_failure"
        return original or "unknown"

    def _fault_type_reason(self, mapped: str, text: str) -> str:
        reasons = {
            "resource_exhaustion": "CPU/memory/resource pressure terms were present in the RCA, evidence, or collaboration focus.",
            "dependency_failure": "Dependency/cache/database/upstream failure terms were present.",
            "network_issue": "Network/DNS/timeout/latency injection terms were present.",
            "service_disruption": "Replica/endpoints/service unavailable terms were present.",
            "image_pull_error": "ImagePullBackOff/ErrImagePull or explicit image pull failure evidence was present.",
            "application_crash": "CrashLoopBackOff/restart/exit/probe failure terms were present.",
            "configuration_error": "Image/config/secret/mount/misconfiguration terms were present.",
            "scheduling_failure": "Scheduling failure label normalized to the operational taxonomy.",
            "infrastructure": "Node/kubelet/runtime/infrastructure pressure terms were present.",
            "compound_failure": "Multiple independent or co-occurring failure terms were present.",
        }
        return reasons.get(mapped, "No strong canonical mapping signal was found.")

    def _recent_quality_is_weak(self, traces: List[Dict[str, Any]]) -> bool:
        if not traces:
            return False
        quality_values: List[float] = []
        for trace in traces[-5:]:
            for key in ("judge_score", "quality_score", "combined_score"):
                val = trace.get(key)
                if isinstance(val, (int, float)) and val > 0:
                    quality_values.append(float(val))
        return bool(quality_values) and sum(quality_values) / len(quality_values) < 0.65

    def _policy_context(self, policy: CollaborationPolicy, agent_name: str = "") -> str:
        focus = "\n".join(f"- {item}" for item in policy.evidence_focus[:8])
        agent_hint = f"\nTarget Agent: {agent_name}" if agent_name else ""
        return (
            "# Collaboration Policy\n"
            f"Strategy: {policy.strategy}\n"
            f"Rationale: {policy.rationale}\n"
            f"Evidence Focus:\n{focus or '- standard multi-signal evidence'}"
            f"{agent_hint}"
        )

    def _heuristic_critique(
        self,
        final_result: Dict[str, Any],
        evidence: Dict[str, Any],
        policy: CollaborationPolicy,
    ) -> Dict[str, Any]:
        weaknesses: List[str] = []
        missing: List[str] = []
        confidence = self._to_float(final_result.get("confidence", 0))
        root_cause = str(final_result.get("root_cause", ""))
        fault_type = str(final_result.get("fault_type", ""))

        if len(root_cause.strip()) < 30 or re.search(r"\bunknown|general investigation|manual\b", root_cause, re.I):
            weaknesses.append("Root cause is too vague for actionable SRE remediation.")
        if confidence >= 0.85 and len(evidence) < 3:
            weaknesses.append("High confidence is not backed by enough independent evidence sources.")
        if not fault_type or fault_type.lower() == "unknown":
            weaknesses.append("Fault type is missing or unknown.")

        for agent in ("metric_agent", "log_agent", "trace_agent", "event_agent"):
            item = evidence.get(agent)
            if not item or item.get("error"):
                missing.append(agent)

        score = 0.9 - 0.12 * len(weaknesses) - 0.04 * len(missing)
        score = max(0.0, min(1.0, score))
        return {
            "quality_score": score,
            "needs_revision": score < 0.82 or bool(weaknesses),
            "needs_human_review": policy.require_human_review or score < 0.65,
            "weaknesses": weaknesses,
            "missing_evidence": missing,
            "alternative_hypotheses": [],
            "confidence_adjustment": -0.1 if weaknesses else 0.0,
            "recommended_focus": policy.evidence_focus[:5],
        }

    def _apply_confidence_adjustment(self, result: Dict[str, Any], critique: Dict[str, Any]) -> Dict[str, Any]:
        adjusted = dict(result)
        delta = self._to_float(critique.get("confidence_adjustment", 0))
        if delta:
            conf = self._to_float(adjusted.get("confidence", 0))
            adjusted["confidence"] = round(max(0.0, min(1.0, conf + delta)), 3)
        adjusted["collaboration_refined"] = adjusted.get("collaboration_refined", False)
        return adjusted

    def _format_evidence(self, evidence: Dict[str, Any], limit: int = 3500) -> str:
        return format_evidence(evidence, limit=limit)

    def _to_float(self, value: Any) -> float:
        try:
            return float(value)
        except Exception:
            return 0.0

    def _dedupe(self, items: List[str]) -> List[str]:
        seen = set()
        out = []
        for item in items:
            key = item.lower()
            if key not in seen:
                seen.add(key)
                out.append(item)
        return out
