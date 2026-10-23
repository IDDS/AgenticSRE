"""
Debate Paradigm — Multi-perspective analysis with moderator synthesis.
Three independent "perspective agents" (infra, application, holistic) each analyze
the incident, then a Moderator LLM synthesizes their views into a consensus.
"""

import asyncio
import logging
from typing import Dict, List

from paradigms.base import AgentPool, ParadigmBase, ParadigmMetrics, ParadigmResult
from paradigms.registry import register_paradigm

logger = logging.getLogger(__name__)

PERSPECTIVE_PROMPT = """You are an expert SRE analyzing a Kubernetes incident from the {perspective} perspective.

Incident: {incident}

Evidence:
{evidence}

Analyze ONLY from the {perspective} perspective:
{perspective_instruction}

Respond in JSON:
{{
    "perspective": "{perspective}",
    "root_cause_hypothesis": "your root cause hypothesis from this perspective",
    "confidence": 0.0,
    "key_evidence": ["evidence1", "evidence2"],
    "fault_type": "category",
    "affected_services": ["svc"],
    "reasoning": "step-by-step reasoning from this perspective",
    "blind_spots": ["what this perspective might miss"]
}}"""

PERSPECTIVE_INSTRUCTIONS = {
    "infrastructure": (
        "Focus on node-level issues: CPU/memory/disk pressure, network partitions, "
        "kernel problems, hardware failures, node NotReady, kubelet issues. "
        "Consider OS-level metrics and events."
    ),
    "application": (
        "Focus on application-level issues: pod crashes, OOMKills, configuration errors, "
        "dependency failures, code bugs, resource limit misconfigurations, image pull errors. "
        "Consider container logs, restart counts, and readiness probes."
    ),
    "holistic": (
        "Focus on systemic / cross-cutting issues: cascading failures, resource contention "
        "between services, network policies blocking traffic, service mesh issues, "
        "deployment rollout problems. Consider the interactions between components."
    ),
}

MODERATOR_PROMPT = """You are a senior SRE moderator synthesizing three independent RCA perspectives
into a final consensus report.

Incident: {incident}

=== Infrastructure Perspective ===
{infra}

=== Application Perspective ===
{app}

=== Holistic Perspective ===
{holistic}

Synthesize these three perspectives:
1. Where do they agree? This strengthens confidence.
2. Where do they disagree? Analyze which is more likely correct and why.
3. What did each perspective catch that the others missed?
4. Produce a final consensus root cause that integrates the best insights.

Respond in JSON:
{{
    "root_cause": "consensus root cause integrating all perspectives",
    "confidence": 0.0,
    "fault_type": "category",
    "affected_services": ["svc"],
    "evidence_summary": {{
        "infrastructure": "key infra findings",
        "application": "key app findings",
        "holistic": "key systemic findings"
    }},
    "reasoning_chain": "how the three perspectives were synthesized",
    "consensus_points": ["point of agreement"],
    "dissent_points": ["point of disagreement and resolution"],
    "remediation_suggestion": "recommended fix"
}}"""


@register_paradigm
class DebateParadigm(ParadigmBase):
    """
    Multi-perspective debate: three independent analyses from infrastructure,
    application, and holistic viewpoints, synthesized by a moderator.
    """

    name = "debate"
    description = "Multi-perspective debate: infra/app/holistic viewpoints → moderator consensus"

    async def _execute(
        self,
        incident_query: str,
        namespace: str,
        metrics: ParadigmMetrics,
    ) -> ParadigmResult:
        pool = self.pool

        # Build unified context
        context = pool.build_context(incident_query, namespace=namespace)

        # Step 1: Gather evidence (shared across all perspectives, enriched)
        self.log("  [debate] Step 1: Gathering shared evidence (parallel)...")
        evidence = await pool.run_all_domain_agents_enriched(incident_query, namespace, context)
        metrics.agent_calls += 4
        evidence_text = self._format_evidence(evidence)

        # Step 2: Three independent perspective analyses (parallel)
        self.log("  [debate] Step 2: Running 3 perspective analyses in parallel...")
        perspectives = ["infrastructure", "application", "holistic"]

        async def analyze_perspective(perspective: str) -> Dict:
            return await pool.llm.async_json_chat([
                {"role": "system", "content": "You are an expert SRE. Respond with valid JSON only."},
                {"role": "user", "content": PERSPECTIVE_PROMPT.format(
                    perspective=perspective,
                    incident=incident_query,
                    evidence=evidence_text[:4000],
                    perspective_instruction=PERSPECTIVE_INSTRUCTIONS[perspective],
                )},
            ])

        results = await asyncio.gather(
            analyze_perspective("infrastructure"),
            analyze_perspective("application"),
            analyze_perspective("holistic"),
        )
        metrics.llm_calls += 3

        infra_view, app_view, holistic_view = results
        for p, v in zip(perspectives, results):
            conf = v.get("confidence", 0)
            hyp = v.get("root_cause_hypothesis", "")[:80]
            self.log(f"    [{p}] conf={conf:.2f} — {hyp}")

        # Step 3: Moderator synthesis
        self.log("  [debate] Step 3: Moderator synthesizing consensus...")
        report = await pool.llm.async_json_chat([
            {"role": "system", "content": "You are a senior SRE moderator. Respond with valid JSON only."},
            {"role": "user", "content": MODERATOR_PROMPT.format(
                incident=incident_query,
                infra=str(infra_view)[:2000],
                app=str(app_view)[:2000],
                holistic=str(holistic_view)[:2000],
            )},
        ])
        metrics.llm_calls += 1
        metrics.iterations = 1

        return ParadigmResult(
            root_cause=report.get("root_cause", ""),
            confidence=report.get("confidence", 0),
            fault_type=report.get("fault_type", ""),
            affected_services=report.get("affected_services", []),
            evidence_summary=report.get("evidence_summary", {}),
            reasoning_chain=report.get("reasoning_chain", ""),
            remediation_suggestion=report.get("remediation_suggestion", ""),
            raw_output=report,
        )

    def _format_evidence(self, evidence: Dict) -> str:
        parts = []
        for agent, result in evidence.items():
            summary = result.get("summary", str(result))[:800]
            parts.append(f"[{agent}]: {summary}")
        return "\n\n".join(parts)
