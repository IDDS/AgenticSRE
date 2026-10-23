"""
Chain Paradigm — Sequential serial pipeline.
Event → Metric → Log → Trace, each step accumulates context passed to the next.
Simplest paradigm: deterministic, no iteration.
"""

import logging
from typing import Dict

from paradigms.base import AgentPool, ParadigmBase, ParadigmMetrics, ParadigmResult
from paradigms.registry import register_paradigm

logger = logging.getLogger(__name__)

CHAIN_REPORT_PROMPT = """You are an expert SRE. Based on the following sequential investigation chain,
produce a root cause analysis report.

Incident: {incident}

Chain of Evidence (gathered sequentially):

Step 1 — Events:
{events}

Step 2 — Metrics:
{metrics}

Step 3 — Logs:
{logs}

Step 4 — Traces:
{traces}

Synthesize all evidence into a final conclusion. Respond in JSON:
{{
    "root_cause": "specific root cause statement",
    "confidence": 0.0,
    "fault_type": "category",
    "affected_services": ["svc"],
    "evidence_summary": {{
        "events": "key findings",
        "metrics": "key findings",
        "logs": "key findings",
        "traces": "key findings"
    }},
    "reasoning_chain": "step-by-step reasoning",
    "remediation_suggestion": "recommended fix"
}}"""


@register_paradigm
class ChainParadigm(ParadigmBase):
    """
    Sequential chain: Event → Metric → Log → Trace.
    Each step's output is accumulated and passed as context to the next LLM call.
    """

    name = "chain"
    description = "Sequential chain: Event→Metric→Log→Trace, accumulating context at each step"

    async def _execute(
        self,
        incident_query: str,
        namespace: str,
        metrics: ParadigmMetrics,
    ) -> ParadigmResult:
        pool = self.pool
        evidence = {}

        # Build unified context and enrich the base query
        context = pool.build_context(incident_query, namespace=namespace)
        if context is not None and pool._context_builder is not None:
            base_query = pool._context_builder.enrich_query(
                incident_query, context, agent_name="event_agent"
            )
        else:
            base_query = incident_query

        # Step 1: Events
        self.log("  [chain] Step 1/4: Event analysis...")
        evidence["event_agent"] = await pool.event_agent.analyze(base_query, namespace)
        metrics.agent_calls += 1

        # Step 2: Metrics (with event context)
        self.log("  [chain] Step 2/4: Metric analysis...")
        event_summary = evidence["event_agent"].get("summary", "")
        enriched_query = f"{incident_query}\n\nEvent context: {event_summary[:500]}"
        evidence["metric_agent"] = await pool.metric_agent.analyze(enriched_query, namespace)
        metrics.agent_calls += 1

        # Step 3: Logs (with event + metric context)
        self.log("  [chain] Step 3/4: Log analysis...")
        metric_summary = evidence["metric_agent"].get("summary", "")
        enriched_query = (f"{incident_query}\n\nEvent context: {event_summary[:300]}"
                          f"\nMetric context: {metric_summary[:300]}")
        evidence["log_agent"] = await pool.log_agent.analyze(enriched_query, namespace)
        metrics.agent_calls += 1

        # Step 4: Traces (with all prior context)
        self.log("  [chain] Step 4/4: Trace analysis...")
        log_summary = evidence["log_agent"].get("summary", "")
        enriched_query = (f"{incident_query}\n\nEvent context: {event_summary[:200]}"
                          f"\nMetric context: {metric_summary[:200]}"
                          f"\nLog context: {log_summary[:200]}")
        evidence["trace_agent"] = await pool.trace_agent.analyze(enriched_query, namespace=namespace)
        metrics.agent_calls += 1

        # Final synthesis via LLM
        self.log("  [chain] Synthesizing final report...")
        report = await pool.llm.async_json_chat([
            {"role": "system", "content": "You are an expert SRE. Respond with valid JSON only."},
            {"role": "user", "content": CHAIN_REPORT_PROMPT.format(
                incident=incident_query,
                events=str(evidence["event_agent"].get("summary", ""))[:1500],
                metrics=str(evidence["metric_agent"].get("summary", ""))[:1500],
                logs=str(evidence["log_agent"].get("summary", ""))[:1500],
                traces=str(evidence["trace_agent"].get("summary", ""))[:1500],
            )},
        ])
        metrics.llm_calls += 5  # 4 agents + 1 synthesis

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
