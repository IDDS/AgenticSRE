"""
AgenticSRE Diagnosis Closure Manager

Closes the loop after an RCA result:
quality check -> failure classification -> targeted re-investigation ->
revised RCA -> re-judge/HITL/learning.
"""

from __future__ import annotations

import logging
import json
import copy
from operational_ontology import observation_entity
from agents.evidence import format_evidence
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def bind_report_entity(report,evidence):
    """Bind an explicitly selected Pod to a unique observed incarnation.

    No prose parsing and no name-based replacement of a supplied UID. Fresh
    event-agent closure reads take precedence over earlier snapshots, including
    failures. This validates recorded identity, never causal responsibility.
    """
    result=copy.deepcopy(report)
    entity=result.get('root_entity')
    validation={'observed':False,'scope':'identity_from_run_observations_only'}
    result['root_entity_validation']=validation
    if not isinstance(entity,dict) or not all(isinstance(entity.get(k),str) and entity[k]
            for k in ('namespace','kind','name')):
        validation['status']='missing_or_unresolved_identity';return result
    if entity['kind']!='Pod':
        validation['status']='identity_type_not_covered';return result
    agents=['event_agent_closure'] if 'event_agent_closure' in evidence else ['event_agent']
    candidates=[]
    for agent in agents:
        payload=evidence.get(agent) or {}
        if (not isinstance(payload,dict) or payload.get('error') or payload.get('collection_errors')
                or payload.get('outcome_unknown') or payload.get('success') is False):
            continue
        for fact in payload.get('evidence_items',[]) or []:
            if not isinstance(fact,dict) or fact.get('source')!='kubectl get pods -o json':
                continue
            raw=fact.get('raw_ref') or {}
            if not isinstance(raw,dict) or raw.get('previous') or raw.get('stale'):
                continue
            if fact.get('signal_type') not in ('pod_waiting_state','pod_scheduling_constraints','pod_spec_checks','container_terminated'):
                continue
            observed=observation_entity(fact)
            if (observed and all(observed.get(k)==entity[k] for k in ('namespace','kind','name'))
                    and observed.get('uid') and isinstance(raw.get('resource_version'),str) and raw['resource_version']):
                candidates.append((observed,raw['resource_version']))
    uids={row[0]['uid'] for row in candidates}
    if len(uids)!=1:
        validation['status']='identity_not_observed' if not uids else 'multiple_incarnations_observed'
        return result
    uid=next(iter(uids))
    if entity.get('uid') and entity['uid']!=uid:
        validation['status']='pod_incarnation_changed';return result
    versions=sorted(set(row[1] for row in candidates))
    result['root_entity']={**entity,'uid':uid}
    result['root_entity'].pop('resource_version',None)
    if len(versions)==1:
        result['root_entity']['resource_version']=versions[0]
    validation.update(observed=True,status='uid_bound',source_agents=agents,resource_versions=versions)
    return result


@dataclass
class ClosurePlan:
    """A concrete plan for closing a weak diagnosis."""

    should_iterate: bool = False
    reason: str = ""
    failure_modes: List[str] = field(default_factory=list)
    target_agents: List[str] = field(default_factory=list)
    focus_queries: Dict[str, str] = field(default_factory=dict)
    max_rounds: int = 1
    hitl_if_unresolved: bool = True
    hypothesis_gaps: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class DiagnosisClosureManager:
    """Turns diagnosis quality gaps into targeted follow-up work."""

    AGENT_ORDER = ["metric_agent", "log_agent", "trace_agent", "event_agent"]

    def __init__(self, llm=None):
        self.llm = llm

    def build_plan(
        self,
        rca_result: Dict[str, Any],
        judge_result: Dict[str, Any],
        critique: Optional[Dict[str, Any]],
        evidence: Dict[str, Any],
        targeted_evidence_complete: bool = False,
    ) -> ClosurePlan:
        """Classify RCA gaps and decide whether to run a targeted closure round."""
        critique = critique or {}
        review=judge_result.get("hypothesis_review") or {}
        gaps=[item for item in review.get("items",[]) if isinstance(item,dict)
              and item.get("assertion_requires_review") and not item.get("assertion_verified")]
        if gaps:
            # A proposer meeting its stopping condition cannot suppress an
            # independent review disagreement. Keep the original one-round cap.
            selected=list(dict.fromkeys(a for item in gaps for a in item.get("next_agents",[])
                                         if a in self.AGENT_ORDER))[:2]
            if not selected:
                selected=["event_agent","metric_agent"]
            focus="核验独立评审指出的竞争假设缺口，不预设原候选正确："+json.dumps([
                {key:item.get(key) for key in ("id","description","proposer_status","status","reason","missing_evidence","validation_errors","rationale_limits")}
                for item in gaps],ensure_ascii=False)
            return ClosurePlan(should_iterate=True,reason="Independent review did not verify asserted hypotheses.",
                failure_modes=["hypothesis_review_disagreement"],target_agents=selected,
                focus_queries={agent:focus+"\n"+self._focus_query(agent,[],rca_result,critique) for agent in selected},
                hypothesis_gaps=gaps,max_rounds=1,hitl_if_unresolved=True)
        score = self._to_float(judge_result.get("combined_score", 0))
        critic_score = self._to_float(critique.get("quality_score", 1))
        needs_review = bool(judge_result.get("needs_review")) or bool(critique.get("needs_revision"))

        missing = [
            a for a in critique.get("missing_evidence", [])
            if a in self.AGENT_ORDER
        ]
        if not targeted_evidence_complete:
            for agent in self.AGENT_ORDER:
                item = evidence.get(agent)
                if not item or item.get("error"):
                    missing.append(agent)

        modes = self._classify_failure_modes(rca_result, judge_result, critique, evidence)
        target_agents = self._target_agents(modes, missing)

        if (
            targeted_evidence_complete
            and self._to_float(rca_result.get("confidence", 0)) >= 0.60
            and set(modes).issubset({
                "missing_signal",
                "insufficient_evidence",
                "scope_or_target_ambiguity",
            })
        ):
            return ClosurePlan(
                should_iterate=False,
                reason=(
                    "Top1 hypothesis was confirmed by its targeted evidence budget; "
                    "missing secondary signals or compact scope wording do not justify "
                    "expanding dormant branches."
                ),
            )

        should_iterate = needs_review or score < 0.65 or critic_score < 0.72
        if not should_iterate:
            return ClosurePlan(should_iterate=False, reason="RCA quality is sufficient.")

        if not target_agents:
            target_agents = ["metric_agent", "event_agent"]

        return ClosurePlan(
            should_iterate=True,
            reason=self._reason(score, critic_score, modes),
            failure_modes=modes,
            target_agents=target_agents,
            focus_queries={agent: self._focus_query(agent, modes, rca_result, critique) for agent in target_agents},
            max_rounds=1,
            hitl_if_unresolved=True,
        )

    def revise_report(
        self,
        incident_query: str,
        original_result: Dict[str, Any],
        original_evidence: Dict[str, Any],
        closure_evidence: Dict[str, Any],
        closure_plan: ClosurePlan,
    ) -> Dict[str, Any]:
        """Produce a revised RCA using closure evidence."""
        if not self.llm:
            return self._heuristic_revision(original_result, closure_evidence, closure_plan)

        prompt = f"""You are an expert SRE performing a second-pass RCA after quality gates found weaknesses.
Use the new targeted evidence to correct the diagnosis. Do not invent evidence.
If the new evidence does not resolve the weakness, lower confidence and state what remains uncertain.

Incident:
{incident_query[:1500]}

Original RCA:
{str(original_result)[:3500]}

Closure Plan:
{closure_plan.to_dict()}

Original Evidence:
{self._format_evidence(original_evidence, limit=2500)}

Targeted Closure Evidence:
{self._format_evidence(closure_evidence, limit=3500)}

Return JSON:
{{
  "root_cause": "specific corrected root cause",
  "confidence": 0.0,
  "fault_type": "canonical category if possible",
  "root_entity": {{"namespace":"observed namespace","kind":"Pod","name":"exact observed name","uid":"observed UID"}},
  "affected_services": ["svc"],
  "timeline": [],
  "evidence_summary": {{"metrics": "...", "logs": "...", "traces": "...", "events": "..."}},
  "reasoning_chain": "explain how the second-pass evidence changes or supports the conclusion",
  "remediation_suggestion": "recommended fix",
  "prevention": "recurrence prevention",
  "closure_applied": true
}}
root_entity must be explicitly selected from observed objects. Use null when unresolved.
Do not drop the root_entity field or replace an original UID with a same-name Pod."""
        from agents.diagnosis_grounding import alert_payload, alert_report_prompt
        combined_evidence = {**original_evidence, **closure_evidence}
        if alert_payload(combined_evidence):
            prompt = alert_report_prompt(incident_query, combined_evidence, original=original_result)
        try:
            revised = self.llm.json_chat([
                {"role": "system", "content": "You are an SRE RCA closure agent. Respond with valid JSON only."},
                {"role": "user", "content": prompt},
            ])
            if isinstance(revised, dict) and revised.get("root_cause"):
                revised["closure_applied"] = True
                revised["closure_failure_modes"] = closure_plan.failure_modes
                if 'root_entity' not in revised:
                    retained=bind_report_entity(original_result,combined_evidence)
                    if retained['root_entity_validation']['observed']:
                        # Reject the entire incomplete rewrite, rather than
                        # attaching an old identity to a new causal conclusion.
                        retained.update(closure_applied=True,closure_revision_validation={
                            'status':'rejected_missing_root_entity','original_report_retained':True})
                        return retained
                    return bind_report_entity(self._guard_revision(original_result,revised,closure_plan),combined_evidence)
                if revised.get('root_entity') is None:
                    return bind_report_entity(revised,combined_evidence)
                return bind_report_entity(self._guard_revision(original_result, revised, closure_plan),combined_evidence)
        except Exception as e:
            logger.warning("[DiagnosisClosure] revise_report failed: %s", e)

        return self._heuristic_revision(original_result, closure_evidence, closure_plan)

    def _classify_failure_modes(
        self,
        rca_result: Dict[str, Any],
        judge_result: Dict[str, Any],
        critique: Dict[str, Any],
        evidence: Dict[str, Any],
    ) -> List[str]:
        text = " ".join([
            str(rca_result.get("root_cause", "")),
            str(rca_result.get("fault_type", "")),
            str(rca_result.get("reasoning_chain", "")),
            " ".join(critique.get("weaknesses", []) or []),
            " ".join(critique.get("alternative_hypotheses", []) or []),
            str(judge_result.get("llm_based", "")),
        ]).lower()

        modes: List[str] = []
        if any(a not in evidence or evidence.get(a, {}).get("error") for a in self.AGENT_ORDER):
            modes.append("missing_signal")
        if re.search(r"lack|missing|no specific|generic|insufficient evidence|unsupported", text):
            modes.append("insufficient_evidence")
        if re.search(r"alternative|rule out|competing|not explicitly rule", text):
            modes.append("unresolved_competing_hypotheses")
        if re.search(r"fault.?type|category|software bug|runaway|infinite loop", text):
            modes.append("fault_type_ambiguity")
        if re.search(r"node|pod|service|component|wrong target|blast radius", text):
            modes.append("scope_or_target_ambiguity")
        if re.search(r"timeline|temporal|time window|recent", text):
            modes.append("temporal_ambiguity")
        if not modes and self._to_float(judge_result.get("combined_score", 1)) < 0.65:
            modes.append("low_quality_reasoning")
        return self._dedupe(modes)

    def _target_agents(self, modes: List[str], missing: List[str]) -> List[str]:
        agents: List[str] = list(missing)
        if "insufficient_evidence" in modes or "fault_type_ambiguity" in modes:
            agents.extend(["metric_agent", "event_agent"])
        if "unresolved_competing_hypotheses" in modes:
            agents.extend(["metric_agent", "log_agent", "trace_agent", "event_agent"])
        if "scope_or_target_ambiguity" in modes:
            agents.extend(["trace_agent", "event_agent", "metric_agent"])
        if "temporal_ambiguity" in modes:
            agents.extend(["event_agent", "metric_agent"])
        if "low_quality_reasoning" in modes:
            agents.extend(["metric_agent", "event_agent"])
        return [a for a in self._dedupe(agents) if a in self.AGENT_ORDER]

    def _focus_query(
        self,
        agent: str,
        modes: List[str],
        rca_result: Dict[str, Any],
        critique: Dict[str, Any],
    ) -> str:
        root = rca_result.get("root_cause", "")
        fault_type = rca_result.get("fault_type", "")
        weakness = "; ".join((critique.get("weaknesses") or [])[:3])
        base = (
            f"Second-pass RCA closure. Original root cause: {root}. "
            f"Fault type: {fault_type}. Weaknesses: {weakness}. "
            f"Failure modes: {', '.join(modes)}. "
        )
        if agent == "metric_agent":
            return base + (
                "Collect quantitative resource, latency, saturation, throttling, and time-window evidence. "
                "Use the most recent 5-15 minute window and prioritize target services named in the incident/root cause. "
                "For Kubernetes CPU issues, explicitly check container CPU usage, CPU throttling, CPU quota/limits, pod-to-node placement, and node CPU. "
                "Distinguish resource exhaustion from software bug and dependency failure."
            )
        if agent == "log_agent":
            return base + "Search for concrete error lines, exception signatures, timeout messages, and evidence that supports or refutes the claimed mechanism."
        if agent == "trace_agent":
            return base + "Check service-to-service path, bottleneck span, upstream/downstream propagation, and whether the suspected service is the first anomalous hop."
        if agent == "event_agent":
            return base + (
                "Check recent Kubernetes events, pod lifecycle, node readiness, image pulls, scheduling, and whether events are temporally aligned with the incident. "
                "Down-rank stale events that started days ago unless their last-seen time and affected pod placement directly align with the current incident."
            )
        return base

    def _reason(self, score: float, critic_score: float, modes: List[str]) -> str:
        return (
            f"Quality gate requested closure: judge_score={score:.3f}, "
            f"critic_score={critic_score:.3f}, modes={', '.join(modes) or 'unknown'}."
        )

    def _heuristic_revision(
        self,
        original_result: Dict[str, Any],
        closure_evidence: Dict[str, Any],
        closure_plan: ClosurePlan,
    ) -> Dict[str, Any]:
        revised = dict(original_result)
        revised["closure_applied"] = True
        revised["closure_failure_modes"] = closure_plan.failure_modes
        revised["closure_note"] = "Second-pass evidence was collected; LLM revision was unavailable."
        if closure_evidence:
            revised.setdefault("evidence_summary", {})
            if isinstance(revised["evidence_summary"], dict):
                revised["evidence_summary"]["closure"] = self._format_evidence(closure_evidence, limit=600)
        if "insufficient_evidence" in closure_plan.failure_modes:
            conf = self._to_float(revised.get("confidence", 0))
            revised["confidence"] = round(max(0.0, min(1.0, conf - 0.1)), 3)
        return revised

    def _guard_revision(
        self,
        original_result: Dict[str, Any],
        revised: Dict[str, Any],
        closure_plan: ClosurePlan,
    ) -> Dict[str, Any]:
        """
        Prevent a weak second pass from erasing a plausible first-pass RCA.

        Closure may lower confidence when evidence is insufficient, but it should
        not replace a concrete RCA with "unknown" unless the closure evidence
        explicitly contradicts the original conclusion.
        """
        root = str(revised.get("root_cause", "")).lower()
        fault_type = str(revised.get("fault_type", "")).lower()
        says_unknown = (
            fault_type in {"", "unknown"}
            or "insufficient evidence" in root
            or "cannot determine" in root
            or "unknown" == root.strip()
        )
        contradicts = any(
            mode in closure_plan.failure_modes
            for mode in ["unresolved_competing_hypotheses", "scope_or_target_ambiguity"]
        )
        if not says_unknown:
            return revised

        guarded = dict(original_result)
        original_conf = self._to_float(original_result.get("confidence", 0))
        penalty = 0.2 if contradicts else 0.12
        guarded["confidence"] = round(max(0.1, original_conf - penalty), 3)
        guarded["closure_applied"] = True
        guarded["closure_guarded"] = True
        guarded["closure_failure_modes"] = closure_plan.failure_modes
        guarded["closure_note"] = (
            "Second-pass evidence was insufficient to replace the original RCA; "
            "the system preserved the original diagnosis with reduced confidence and routed it to HITL."
        )
        return guarded

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
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out
