"""
Plan-and-Execute Paradigm — Hypothesis-driven structured investigation.
Wraps the existing rca_engine logic: Hypothesis → Plan → Parallel Investigate → Re-rank → Correlate → Report.
"""

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional, Set

from agents.evidence_adjudicator import (
    build_evidence_adjudication_prompt,
    build_hypothesis_refinement_prompt,
)
from agents.evidence_coverage import (
    assess_evidence_coverage,
    build_coverage_rewrite_prompt,
)
from paradigms.base import AgentPool, ParadigmBase, ParadigmMetrics, ParadigmResult
from paradigms.registry import register_paradigm

logger = logging.getLogger(__name__)

FINAL_REPORT_PROMPT = """You are an expert SRE producing the final Root Cause Analysis report.

Incident: {incident}

Hypotheses (ranked by confidence):
{hypotheses}

Evidence from investigation:
{evidence}

Cross-signal correlation:
{correlation}

Produce a comprehensive RCA report in JSON:
{{
    "root_cause": "specific, actionable root cause statement",
    "confidence": 0.0,
    "fault_type": "category of fault",
    "affected_services": ["svc1", "svc2"],
    "evidence_summary": {{
        "metrics": "key metric findings",
        "logs": "key log findings",
        "traces": "key trace findings",
        "events": "key event findings"
    }},
    "reasoning_chain": "step-by-step reasoning from evidence to conclusion",
    "remediation_suggestion": "recommended fix"
}}"""


PRECISION_VERIFIER_SUFFIX = """

Precision-preservation requirements for this final read-only pass:
1. Copy literal identifiers, call numbers, paths, error codes, numeric values,
   scopes, and recovery markers from the evidence whenever they are decisive.
2. If the evidence contains an explicit marker such as injected=1, errno=5,
   cpu.max=20000 100000, call=4, or a named mapping, preserve that marker in
   the root cause instead of replacing it with a broad category.
3. Never infer a deployment, disk, race, hypervisor, network device, or other
   hidden mechanism unless the evidence directly names it. When a mechanism is
   underdetermined, say so and report the exact observed fault boundary.
4. The answer must explain both the fault and the observed recovery/control
   contrast. Do not claim evidence that is absent.
"""


@register_paradigm
class PlanAndExecuteParadigm(ParadigmBase):
    """
    Hypothesis-driven plan-and-execute: generate hypotheses, plan investigation,
    run domain agents in parallel, re-rank hypotheses, correlate signals, report.
    Re-implements the core rca_engine logic as a paradigm.
    """

    name = "plan_and_execute"
    description = "Hypothesis-driven: generate hypotheses → plan → parallel investigate → re-rank → correlate"
    DOMAIN_AGENTS = {"metric_agent", "log_agent", "trace_agent", "event_agent"}
    EVIDENCE_TYPE_TO_AGENT = {
        "metric": "metric_agent",
        "log": "log_agent",
        "trace": "trace_agent",
        "event": "event_agent",
    }

    @classmethod
    def _planned_agent_queries(cls, plan: Dict, incident_query: str) -> Dict[str, str]:
        """Translate the structured plan into concrete domain-agent queries.

        Older code generated a plan and then ignored it, running all four agents
        on the same generic query in every iteration.  Keep one focused query per
        planned domain agent; fall back to all domains when the plan is malformed
        or contains only tool-runtime steps that this paradigm cannot execute.
        """
        grouped: Dict[str, List[str]] = {}
        for step in plan.get("plan", []) if isinstance(plan, dict) else []:
            if not isinstance(step, dict):
                continue
            agent = str(step.get("agent", "")).strip()
            if agent not in cls.DOMAIN_AGENTS:
                continue
            details = [
                str(step.get("action", "")).strip(),
                str(step.get("expected_evidence", "")).strip(),
                str(step.get("command", "")).strip(),
            ]
            focus = "; ".join(item for item in details if item)
            grouped.setdefault(agent, []).append(focus or "Collect evidence for the leading hypothesis")

        if not grouped:
            return {name: incident_query for name in sorted(cls.DOMAIN_AGENTS)}
        return {
            name: f"{incident_query}\n\nPlanned investigation focus:\n- " + "\n- ".join(items)
            for name, items in grouped.items()
        }

    @classmethod
    def _required_domain_agents(cls, evidence_contract: Optional[Dict[str, Any]]) -> Set[str]:
        """Resolve a case-level evidence contract to domain-agent names."""
        if not isinstance(evidence_contract, dict):
            return set()
        required = evidence_contract.get("required", [])
        return {
            cls.EVIDENCE_TYPE_TO_AGENT[item]
            for item in required
            if item in cls.EVIDENCE_TYPE_TO_AGENT
        }

    @staticmethod
    def _has_evidence(result: Optional[Dict[str, Any]]) -> bool:
        """Treat explicit availability/error fields and empty summaries safely."""
        if not isinstance(result, dict) or result.get("error"):
            return False
        if "evidence_available" in result:
            return bool(result.get("evidence_available"))
        summary = str(result.get("summary", "")).strip()
        return bool(summary) and not summary.lower().startswith("no domain-specific observation")

    async def _execute_planned_agents(
        self,
        pool: AgentPool,
        plan: Dict,
        incident_query: str,
        namespace: str,
        required_agents: Optional[Set[str]] = None,
    ) -> Dict[str, Dict]:
        queries = self._planned_agent_queries(plan, incident_query)
        required_agents = set(required_agents or set())
        # A plan may legitimately focus on only part of the evidence graph,
        # but it may not skip a contract-required domain.  Add a clearly
        # labelled supplemental collection query for each missing requirement.
        supplemental = required_agents - set(queries)
        for name in sorted(supplemental):
            queries[name] = (
                f"{incident_query}\n\nEvidence completeness supplement: collect the "
                f"{name.replace('_agent', '')} evidence required by the case contract."
            )
        agents = pool.domain_agents()

        async def run(name: str, query: str) -> Dict:
            agent = agents[name]
            if name == "trace_agent":
                return await agent.analyze(query, namespace=namespace)
            return await agent.analyze(query, namespace)

        names = list(queries)
        results = await asyncio.gather(
            *(run(name, queries[name]) for name in names),
            return_exceptions=True,
        )
        output: Dict[str, Dict] = {}
        for name, result in zip(names, results):
            if isinstance(result, Exception):
                output[name] = {"summary": f"Error: {result}", "error": True}
            else:
                output[name] = dict(result)
                output[name]["collection_mode"] = (
                    "supplemental" if name in supplemental else "planned"
                )

        # One retry is allowed for a required domain that returned no usable
        # observation.  A second miss is retained as INCOMPLETE_EVIDENCE by the
        # caller; it is never silently removed from the denominator.
        missing_after_first = {
            name for name in required_agents
            if not self._has_evidence(output.get(name))
        }
        if missing_after_first:
            retry_results = await asyncio.gather(
                *(run(
                    name,
                    f"{incident_query}\n\nRequired-evidence retry: return the "
                    f"{name.replace('_agent', '')} observation or explicitly state that it is unavailable.",
                ) for name in sorted(missing_after_first)),
                return_exceptions=True,
            )
            for name, result in zip(sorted(missing_after_first), retry_results):
                if isinstance(result, Exception):
                    output[name] = {"summary": f"Error: {result}", "error": True}
                else:
                    output[name] = dict(result)
                    output[name]["collection_mode"] = "required_retry"
        return output

    @staticmethod
    def _evidence_signature(evidence: Dict[str, Dict]) -> str:
        normalized = {
            name: str(result.get("summary", result))
            for name, result in sorted(evidence.items())
        }
        return json.dumps(normalized, ensure_ascii=False, sort_keys=True)

    async def _execute(
        self,
        incident_query: str,
        namespace: str,
        metrics: ParadigmMetrics,
    ) -> ParadigmResult:
        pool = self.pool
        cfg = pool.cfg
        evidence_contract = getattr(pool, "evidence_contract", None) or {}
        required_agents = self._required_domain_agents(evidence_contract)

        # Build unified context (domain hints + historical rules + feedback)
        context = pool.build_context(incident_query, namespace=namespace)

        # Step 1: Generate hypotheses (with historical context injection)
        self.log("  [plan_and_execute] Step 1: Generating hypotheses...")
        historical_rules = []
        historical_faults = []
        if context is not None:
            historical_rules = [
                r.get("text", r.get("condition", str(r)))
                for r in context.historical_rules
            ]
            historical_faults = context.similar_faults
        hypotheses = pool.hypothesis_agent.generate(
            incident_query,
            historical_rules=historical_rules,
            historical_faults=historical_faults,
        )
        initial_hypotheses = [h.to_dict() for h in hypotheses]
        metrics.llm_calls += 1
        for h in hypotheses:
            self.log(f"    [{h.id}] conf={h.confidence:.2f} — {h.description[:80]}")

        # Step 2: Iterative evidence loop
        max_iter = cfg.pipeline.max_evidence_iterations
        confidence_threshold = cfg.pipeline.hypothesis_confidence_threshold
        all_evidence: Dict[str, Dict] = {}
        evidence_signatures = set()

        for iteration in range(max_iter):
            metrics.iterations = iteration + 1
            self.log(f"  [plan_and_execute] Iteration {iteration + 1}/{max_iter}")

            # Generate plan
            plan = pool.planning_agent.generate_plan(
                [h.to_dict() for h in hypotheses],
                incident_query, iteration
            )
            metrics.llm_calls += 1
            self.log(f"    Plan: {len(plan.get('plan', []))} steps")

            # Execute the plan instead of discarding it and blindly calling all
            # domains.  This preserves the intended Plan-and-Execute semantics.
            self.log("    Executing planned domain-agent steps...")
            new_evidence = await self._execute_planned_agents(
                pool, plan, incident_query, namespace, required_agents=required_agents
            )
            metrics.agent_calls += len(new_evidence)

            signature = self._evidence_signature(new_evidence)
            if signature in evidence_signatures:
                self.log("    No new evidence delta, stopping repeated investigation")
                break
            evidence_signatures.add(signature)

            for name, result in new_evidence.items():
                summary = result.get("summary", "")[:100]
                self.log(f"    {name}: {summary}")
                # Preserve an earlier usable observation when a later
                # iteration returns an empty/error response for the same
                # domain.  The contract is about whether evidence was ever
                # collected successfully during this run.
                if name not in all_evidence or self._has_evidence(result):
                    all_evidence[name] = result

            # Re-rank hypotheses
            self.log("    Re-ranking hypotheses...")
            hypotheses = pool.hypothesis_agent.rerank(hypotheses, new_evidence)
            metrics.llm_calls += 1
            top = max(hypotheses, key=lambda h: h.confidence) if hypotheses else None
            if top:
                self.log(f"    Top: [{top.id}] conf={top.confidence:.2f} — {top.description[:60]}")

            # Early exit
            if top and top.confidence >= confidence_threshold:
                self.log(f"    High confidence ({top.confidence:.2f}), stopping iterations")
                break

        # Step 3: Cross-signal correlation
        if cfg.pipeline.enable_correlation:
            self.log("  [plan_and_execute] Running cross-signal correlation...")
            correlation_result = pool.correlation_agent.correlate(all_evidence)
            metrics.llm_calls += 1
            all_evidence["correlation"] = correlation_result
            self.log(f"    Top suspect: {correlation_result.get('top_suspect', 'N/A')}")
        else:
            correlation_result = {}

        # Step 4: Final evidence adjudication.  The selected hypothesis is a
        # proposal, not an anchor: the Critic may replace it when exact evidence
        # proves an uncovered mechanism or a shorter causal chain.
        self.log("  [plan_and_execute] Running final evidence adjudication...")
        hyp_text = "\n".join([
            f"[{h.id}] conf={h.confidence:.2f} — {h.description}"
            for h in hypotheses[:5]
        ])
        evidence_text = ""
        for agent, result in all_evidence.items():
            if agent == "correlation":
                continue
            summary = result.get("summary", str(result))[:500]
            evidence_text += f"\n[{agent}]: {summary}\n"

        top = max(hypotheses, key=lambda h: h.confidence) if hypotheses else None
        selected_id = top.id if top else ""
        report = await pool.llm.async_json_chat([
            {"role": "system", "content": "You are an expert SRE. Respond with valid JSON only."},
            {"role": "user", "content": build_hypothesis_refinement_prompt(
                incident=incident_query,
                evidence=evidence_text[:6000],
                initial_hypotheses=initial_hypotheses,
                reranked_hypotheses=[h.to_dict() for h in hypotheses],
                selected_id=selected_id,
            )},
        ])
        metrics.llm_calls += 1

        # Fail soft for providers that return an incomplete Critic schema.
        if not report.get("root_cause"):
            self.log("  [plan_and_execute] Critic incomplete, using legacy synthesis fallback...")
            report = await pool.llm.async_json_chat([
                {"role": "system", "content": "You are an expert SRE. Respond with valid JSON only."},
                {"role": "user", "content": FINAL_REPORT_PROMPT.format(
                    incident=incident_query,
                    hypotheses=hyp_text,
                    evidence=evidence_text[:6000],
                    correlation=str(correlation_result.get("summary", ""))[:2000],
                )},
            ])
            metrics.llm_calls += 1

        coverage_audit = {}
        if getattr(cfg.pipeline, "enable_evidence_coverage_gate", False):
            coverage_audit = assess_evidence_coverage(
                incident=incident_query,
                evidence=evidence_text[:6000],
                report=report,
            )
            self.log(
                "  [plan_and_execute] Evidence coverage: "
                f"{coverage_audit.get('covered_fact_count', 0)}/"
                f"{coverage_audit.get('candidate_fact_count', 0)}"
            )
            if coverage_audit.get("needs_rewrite"):
                rewrite = await pool.llm.async_json_chat([
                    {"role": "system", "content": "You are a generic SRE evidence-preservation gate. Respond with valid JSON only."},
                    {"role": "user", "content": build_coverage_rewrite_prompt(
                        incident=incident_query,
                        evidence=evidence_text[:6000],
                        previous_report=report,
                        coverage=coverage_audit,
                    )},
                ])
                metrics.llm_calls += 1
                if rewrite.get("root_cause"):
                    report = {**report, **rewrite}

        # Optional accuracy-oriented pass.  This is deliberately separate from
        # the planning loop: it cannot collect new evidence or see ground truth;
        # it only repairs loss of literal identifiers and unsupported causal
        # elaboration in the previous Critic output.
        if getattr(cfg.pipeline, "enable_precision_verifier", False):
            self.log("  [plan_and_execute] Running precision evidence verifier...")
            verifier_prompt = build_evidence_adjudication_prompt(
                incident=incident_query,
                evidence=evidence_text[:6000],
            ) + PRECISION_VERIFIER_SUFFIX + (
                "\n\nPrevious candidate to audit (may be wrong or too broad):\n"
                + json.dumps(report, ensure_ascii=False)[:3000]
            )
            if coverage_audit:
                verifier_prompt += "\n\nGeneric evidence coverage audit:\n" + json.dumps(
                    coverage_audit, ensure_ascii=False
                )
            verified_report = await pool.llm.async_json_chat([
                {"role": "system", "content": "You are an exacting SRE evidence verifier. Respond with valid JSON only."},
                {"role": "user", "content": verifier_prompt},
            ])
            metrics.llm_calls += 1
            if verified_report.get("root_cause"):
                # Preserve verifier fields while retaining any useful structured
                # fields the earlier Critic emitted but the verifier omitted.
                report = {**report, **verified_report}

        if getattr(cfg.pipeline, "enable_evidence_coverage_gate", False):
            coverage_audit = assess_evidence_coverage(
                incident=incident_query,
                evidence=evidence_text[:6000],
                report=report,
            )

        causal_chain = report.get("causal_chain", report.get("reasoning_chain", ""))
        if isinstance(causal_chain, list):
            causal_chain = " → ".join(str(item) for item in causal_chain)
        evidence_summary = report.get("evidence_summary")
        if not isinstance(evidence_summary, dict):
            evidence_summary = {
                "decisive_evidence": report.get("decisive_evidence", []),
                "contradictions_checked": report.get("contradictions_checked", []),
            }
        affected = report.get("affected_services", [])
        if not affected and report.get("component"):
            affected = [str(report["component"])]

        collected_types = sorted({
            evidence_type
            for evidence_type, agent_name in self.EVIDENCE_TYPE_TO_AGENT.items()
            if self._has_evidence(all_evidence.get(agent_name))
        })
        required_types = [
            str(item) for item in evidence_contract.get("required", [])
            if item in self.EVIDENCE_TYPE_TO_AGENT
        ]
        missing_types = sorted(set(required_types) - set(collected_types))
        evidence_status = (
            "COMPLETE" if evidence_contract and not missing_types
            else "INCOMPLETE_EVIDENCE" if evidence_contract
            else "NOT_ASSESSED"
        )

        return ParadigmResult(
            root_cause=report.get("root_cause", ""),
            confidence=report.get("confidence", 0),
            fault_type=report.get("fault_type", ""),
            affected_services=affected,
            evidence_summary=evidence_summary,
            reasoning_chain=str(causal_chain),
            remediation_suggestion=report.get("remediation_suggestion", ""),
            raw_output=report,
            evidence_status=evidence_status,
            evidence_contract=evidence_contract,
            evidence_collected=collected_types,
            evidence_missing=missing_types,
            evidence_coverage=coverage_audit,
        )
