"""
Voting Paradigm — Ensemble voting with diverse LLM analyses.
Runs 3 independent LLM analyses with different temperatures,
then aggregates via algorithmic voting for a robust consensus.
"""

import asyncio
import logging
from typing import Dict, List

from paradigms.base import AgentPool, ParadigmBase, ParadigmMetrics, ParadigmResult
from paradigms.registry import register_paradigm

logger = logging.getLogger(__name__)

VOTER_PROMPT = """You are an expert SRE performing root cause analysis on a Kubernetes cluster.

Incident: {incident}

Evidence from domain agents:
{evidence}

{instruction}

Analyze the evidence and produce your independent RCA conclusion in JSON:
{{
    "root_cause": "specific root cause statement",
    "confidence": 0.0,
    "fault_type": "category",
    "affected_services": ["svc"],
    "key_evidence": ["evidence1", "evidence2"],
    "reasoning_chain": "step-by-step reasoning",
    "remediation_suggestion": "recommended fix"
}}"""

VOTER_INSTRUCTIONS = [
    "Be precise and conservative. Only claim high confidence if evidence is strong.",
    "Think broadly about possible causes. Consider less obvious explanations.",
    "Focus on the most likely explanation given the evidence. Prioritize simplicity (Occam's razor).",
]

VOTER_TEMPERATURES = [0.1, 0.5, 0.8]

AGGREGATION_PROMPT = """You are a senior SRE aggregating three independent RCA analyses into a final report.

Incident: {incident}

=== Analysis 1 (conservative, T=0.1) ===
{vote1}

=== Analysis 2 (exploratory, T=0.5) ===
{vote2}

=== Analysis 3 (creative, T=0.8) ===
{vote3}

Voting Summary:
- Agreement on root cause: {agreement}
- Confidence range: {conf_range}
- Fault types: {fault_types}

Aggregate these analyses using majority voting principles:
1. Where 2+ analyses agree, that's the likely root cause
2. Average the confidence scores, weighted by agreement
3. Include unique insights from minority views
4. If all 3 disagree, pick the one with strongest evidence chain

Respond in JSON:
{{
    "root_cause": "aggregated root cause",
    "confidence": 0.0,
    "fault_type": "category",
    "affected_services": ["svc"],
    "evidence_summary": {{"metrics": "...", "logs": "...", "traces": "...", "events": "..."}},
    "reasoning_chain": "how the votes were aggregated",
    "agreement_level": "unanimous|majority|split",
    "remediation_suggestion": "recommended fix"
}}"""


@register_paradigm
class VotingParadigm(ParadigmBase):
    """
    Ensemble voting: 3 independent LLM analyses at different temperatures,
    then algorithmic + LLM aggregation for robust consensus.
    """

    name = "voting"
    description = "Ensemble voting: 3 independent LLM analyses (different temperatures) → majority vote"

    async def _execute(
        self,
        incident_query: str,
        namespace: str,
        metrics: ParadigmMetrics,
    ) -> ParadigmResult:
        pool = self.pool

        # Build unified context
        context = pool.build_context(incident_query, namespace=namespace)

        # Step 1: Gather shared evidence (enriched)
        self.log("  [voting] Step 1: Gathering evidence (parallel)...")
        evidence = await pool.run_all_domain_agents_enriched(incident_query, namespace, context)
        metrics.agent_calls += 4
        evidence_text = self._format_evidence(evidence)

        # Step 2: 3 independent LLM votes (parallel, different temperatures)
        self.log("  [voting] Step 2: Running 3 independent analyses in parallel...")

        async def cast_vote(idx: int) -> Dict:
            temp = VOTER_TEMPERATURES[idx]
            instruction = VOTER_INSTRUCTIONS[idx]
            return await pool.llm.async_json_chat(
                [
                    {"role": "system", "content": "You are an expert SRE. Respond with valid JSON only."},
                    {"role": "user", "content": VOTER_PROMPT.format(
                        incident=incident_query,
                        evidence=evidence_text[:5000],
                        instruction=instruction,
                    )},
                ],
                temperature=temp,
            )

        votes = await asyncio.gather(
            cast_vote(0), cast_vote(1), cast_vote(2),
        )
        metrics.llm_calls += 3

        for i, v in enumerate(votes):
            conf = v.get("confidence", 0)
            rc = v.get("root_cause", "")[:80]
            self.log(f"    Vote {i+1} (T={VOTER_TEMPERATURES[i]}): conf={conf:.2f} — {rc}")

        # Step 3: Algorithmic pre-analysis
        root_causes = [v.get("root_cause", "") for v in votes]
        confidences = [v.get("confidence", 0) for v in votes]
        fault_types = [v.get("fault_type", "") for v in votes]

        # Simple agreement check (substring matching)
        agreement = self._check_agreement(root_causes)
        conf_range = f"{min(confidences):.2f} - {max(confidences):.2f}"
        fault_type_str = ", ".join(set(ft for ft in fault_types if ft))

        self.log(f"    Agreement: {agreement}, Confidence range: {conf_range}")

        # Step 4: LLM aggregation
        self.log("  [voting] Step 3: Aggregating votes...")
        report = await pool.llm.async_json_chat([
            {"role": "system", "content": "You are a senior SRE. Respond with valid JSON only."},
            {"role": "user", "content": AGGREGATION_PROMPT.format(
                incident=incident_query,
                vote1=str(votes[0])[:2000],
                vote2=str(votes[1])[:2000],
                vote3=str(votes[2])[:2000],
                agreement=agreement,
                conf_range=conf_range,
                fault_types=fault_type_str,
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

    def _check_agreement(self, root_causes: List[str]) -> str:
        """Simple heuristic check for agreement among 3 root causes."""
        if len(root_causes) < 2:
            return "insufficient"
        # Check pairwise overlap (shared keywords)
        rc_lower = [rc.lower() for rc in root_causes]
        words = [set(rc.split()) for rc in rc_lower]
        overlaps = []
        for i in range(len(words)):
            for j in range(i + 1, len(words)):
                shared = words[i] & words[j]
                # Remove common stop words
                shared -= {"the", "a", "an", "is", "are", "was", "were", "in", "on", "of", "to", "and", "or", "for"}
                overlap_ratio = len(shared) / max(len(words[i] | words[j]), 1)
                overlaps.append(overlap_ratio)
        avg_overlap = sum(overlaps) / len(overlaps) if overlaps else 0
        if avg_overlap > 0.4:
            return "high"
        elif avg_overlap > 0.2:
            return "moderate"
        else:
            return "low"
