"""
Reflection Paradigm — Initial analysis → Critic evaluation → Targeted re-investigation → Improved report.
Up to 2 reflection rounds. Focuses on self-improvement of the analysis.
"""

import logging
from typing import Dict

from paradigms.base import AgentPool, ParadigmBase, ParadigmMetrics, ParadigmResult
from paradigms.registry import register_paradigm

logger = logging.getLogger(__name__)

MAX_REFLECTION_ROUNDS = 2

INITIAL_ANALYSIS_PROMPT = """You are an expert SRE performing root cause analysis on a Kubernetes cluster.

Incident: {incident}

Evidence from domain agents:
{evidence}

Produce an initial RCA report in JSON:
{{
    "root_cause": "specific root cause statement",
    "confidence": 0.0,
    "fault_type": "category",
    "affected_services": ["svc"],
    "evidence_summary": {{"metrics": "...", "logs": "...", "traces": "...", "events": "..."}},
    "reasoning_chain": "step-by-step reasoning",
    "remediation_suggestion": "recommended fix",
    "blind_spots": ["areas where more evidence is needed"]
}}"""

CRITIC_PROMPT = """You are a critical reviewer of SRE root cause analyses.
Review the following RCA report and identify weaknesses.

Incident: {incident}

Current RCA Report:
{report}

Available evidence:
{evidence}

Evaluate the report critically:
1. Is the root cause specific enough? Or too vague?
2. Does the confidence match the strength of evidence?
3. Are there alternative explanations not considered?
4. What additional investigation would strengthen the conclusion?

Respond in JSON:
{{
    "quality_score": 0.0,
    "weaknesses": ["weakness1", "weakness2"],
    "alternative_hypotheses": ["alt1"],
    "missing_investigation": ["what to check"],
    "specific_queries": ["specific query to investigate"],
    "adjusted_confidence": 0.0,
    "overall_assessment": "brief assessment"
}}"""

IMPROVED_REPORT_PROMPT = """You are an expert SRE improving a root cause analysis based on critique feedback.

Incident: {incident}

Original Report:
{original}

Critic Feedback:
{critique}

Additional Evidence (from targeted re-investigation):
{new_evidence}

Produce an IMPROVED RCA report that addresses the critique. Respond in JSON:
{{
    "root_cause": "more specific root cause",
    "confidence": 0.0,
    "fault_type": "category",
    "affected_services": ["svc"],
    "evidence_summary": {{"metrics": "...", "logs": "...", "traces": "...", "events": "..."}},
    "reasoning_chain": "improved step-by-step reasoning addressing critique",
    "remediation_suggestion": "recommended fix"
}}"""


@register_paradigm
class ReflectionParadigm(ParadigmBase):
    """
    Reflection paradigm: generate initial analysis, critique it, gather
    targeted additional evidence, and produce an improved report.
    """

    name = "reflection"
    description = "Self-reflection: initial analysis → critic review → targeted re-investigation → improved report"

    async def _execute(
        self,
        incident_query: str,
        namespace: str,
        metrics: ParadigmMetrics,
    ) -> ParadigmResult:
        pool = self.pool

        # Build unified context
        context = pool.build_context(incident_query, namespace=namespace)

        # Step 1: Parallel domain agent investigation (enriched)
        self.log("  [reflection] Step 1: Gathering initial evidence (parallel)...")
        evidence = await pool.run_all_domain_agents_enriched(incident_query, namespace, context)
        metrics.agent_calls += 4

        evidence_text = self._format_evidence(evidence)

        # Step 2: Initial LLM analysis
        self.log("  [reflection] Step 2: Generating initial analysis...")
        current_report = await pool.llm.async_json_chat([
            {"role": "system", "content": "You are an expert SRE. Respond with valid JSON only."},
            {"role": "user", "content": INITIAL_ANALYSIS_PROMPT.format(
                incident=incident_query,
                evidence=evidence_text,
            )},
        ])
        metrics.llm_calls += 1

        # Reflection rounds
        for round_num in range(1, MAX_REFLECTION_ROUNDS + 1):
            metrics.iterations = round_num
            self.log(f"  [reflection] Reflection round {round_num}/{MAX_REFLECTION_ROUNDS}")

            # Step 3a: Critic evaluation
            self.log(f"    Critic evaluating report...")
            critique = await pool.llm.async_json_chat([
                {"role": "system", "content": "You are a critical SRE reviewer. Respond with valid JSON only."},
                {"role": "user", "content": CRITIC_PROMPT.format(
                    incident=incident_query,
                    report=str(current_report)[:3000],
                    evidence=evidence_text[:2000],
                )},
            ])
            metrics.llm_calls += 1

            quality = critique.get("quality_score", 0.5)
            self.log(f"    Quality score: {quality}, Weaknesses: {len(critique.get('weaknesses', []))}")

            # If quality is already high, stop reflecting
            if quality >= 0.85:
                self.log(f"    Quality sufficient ({quality}), stopping reflection")
                break

            # Step 3b: Targeted re-investigation based on critique
            self.log(f"    Running targeted re-investigation...")
            specific_queries = critique.get("specific_queries", [])
            new_evidence = {}
            if specific_queries:
                # Use the first specific query to drive re-investigation
                requery = specific_queries[0] if specific_queries else incident_query
                new_evidence = await pool.run_all_domain_agents(
                    f"{incident_query}. Focus on: {requery}", namespace
                )
                metrics.agent_calls += 4

            # Step 3c: Generate improved report
            self.log(f"    Generating improved report...")
            current_report = await pool.llm.async_json_chat([
                {"role": "system", "content": "You are an expert SRE. Respond with valid JSON only."},
                {"role": "user", "content": IMPROVED_REPORT_PROMPT.format(
                    incident=incident_query,
                    original=str(current_report)[:3000],
                    critique=str(critique)[:2000],
                    new_evidence=self._format_evidence(new_evidence)[:2000] if new_evidence else "No additional evidence gathered.",
                )},
            ])
            metrics.llm_calls += 1

            # Merge new evidence into overall evidence
            for k, v in new_evidence.items():
                if k in evidence:
                    evidence[k + "_reflection"] = v
                else:
                    evidence[k] = v

        report = current_report
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
