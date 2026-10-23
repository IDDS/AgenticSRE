"""
ReAct Paradigm — Thought → Action → Observation loop.
LLM decides which agent to call at each step, dynamically adapting its investigation.
Most flexible paradigm but highest token overhead.
"""

import json
import logging
from typing import Dict, List

from paradigms.base import AgentPool, ParadigmBase, ParadigmMetrics, ParadigmResult
from paradigms.registry import register_paradigm

logger = logging.getLogger(__name__)

REACT_SYSTEM = """You are an expert SRE performing root cause analysis on a Kubernetes cluster
using the ReAct (Reasoning + Acting) paradigm.

Available actions (choose ONE per step):
- metric_agent: Analyze Prometheus metrics for anomalies
- log_agent: Search and analyze Elasticsearch logs
- trace_agent: Analyze Jaeger distributed traces
- event_agent: Check Kubernetes events and status
- conclude: Produce final RCA conclusion (use when you have enough evidence)

At each step, output exactly ONE JSON object:
{{
    "thought": "your reasoning about what to investigate next and why",
    "action": "agent_name or conclude",
    "action_input": "specific query or investigation focus"
}}

When action is "conclude", action_input should be your final analysis in JSON:
{{
    "thought": "final reasoning",
    "action": "conclude",
    "action_input": {{
        "root_cause": "specific root cause",
        "confidence": 0.85,
        "fault_type": "category",
        "affected_services": ["svc"],
        "reasoning_chain": "step-by-step reasoning from evidence",
        "remediation_suggestion": "recommended fix"
    }}
}}"""

MAX_REACT_STEPS = 8


@register_paradigm
class ReActParadigm(ParadigmBase):
    """
    ReAct loop: LLM reasons about the situation, picks an action (agent),
    observes the result, and repeats until concluding.
    """

    name = "react"
    description = "ReAct loop: Thought→Action→Observation, LLM dynamically selects agents"

    async def _execute(
        self,
        incident_query: str,
        namespace: str,
        metrics: ParadigmMetrics,
    ) -> ParadigmResult:
        pool = self.pool

        # Build unified context and enrich the initial prompt
        context = pool.build_context(incident_query, namespace=namespace)
        enriched_query = incident_query
        if context is not None and pool._context_builder is not None:
            enriched_query = pool._context_builder.enrich_query(
                incident_query, context, agent_name=""
            )

        conversation: List[Dict[str, str]] = [
            {"role": "system", "content": REACT_SYSTEM},
            {"role": "user", "content": f"Incident: {enriched_query}\nNamespace: {namespace or 'all'}"},
        ]
        evidence_summary: Dict[str, str] = {}

        for step in range(1, MAX_REACT_STEPS + 1):
            metrics.iterations = step
            self.log(f"  [react] Step {step}/{MAX_REACT_STEPS}")

            # Get LLM decision
            response_text = await pool.llm.async_chat(conversation)
            metrics.llm_calls += 1

            # Parse the decision
            decision = self._parse_decision(response_text)
            thought = decision.get("thought", "")
            action = decision.get("action", "conclude")
            action_input = decision.get("action_input", incident_query)

            self.log(f"    Thought: {thought[:120]}")
            self.log(f"    Action: {action}")

            # Check for conclusion
            if action == "conclude":
                self.log("  [react] Concluding...")
                return self._build_result(action_input, evidence_summary)

            # Execute the chosen agent
            observation = await self._run_agent(pool, action, action_input, namespace)
            metrics.agent_calls += 1
            obs_summary = observation.get("summary", str(observation))[:800]
            evidence_summary[action] = obs_summary

            self.log(f"    Observation: {obs_summary[:120]}")

            # Feed back into conversation
            conversation.append({"role": "assistant", "content": response_text})
            conversation.append({"role": "user", "content": f"Observation from {action}:\n{obs_summary}"})

        # Reached max steps — force conclusion
        self.log("  [react] Max steps reached, forcing conclusion...")
        force_prompt = (
            "You have reached the maximum number of investigation steps. "
            "Based on all evidence gathered so far, produce your final conclusion now. "
            'Use action "conclude".'
        )
        conversation.append({"role": "user", "content": force_prompt})
        response_text = await pool.llm.async_chat(conversation)
        metrics.llm_calls += 1
        decision = self._parse_decision(response_text)
        return self._build_result(decision.get("action_input", {}), evidence_summary)

    async def _run_agent(self, pool: AgentPool, action: str, query: str, namespace: str) -> Dict:
        """Execute one of the domain agents by name."""
        agents = pool.domain_agents()
        agent = agents.get(action)
        if agent is None:
            return {"summary": f"Unknown agent: {action}", "error": True}
        try:
            if action == "trace_agent":
                return await agent.analyze(query, namespace=namespace)
            return await agent.analyze(query, namespace)
        except Exception as e:
            return {"summary": f"Error running {action}: {e}", "error": True}

    def _parse_decision(self, text: str) -> Dict:
        """Best-effort parse of the LLM's JSON decision."""
        text = text.strip()
        # Try direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Try extracting JSON block
        if "```" in text:
            lines = text.split("\n")
            json_lines = []
            inside = False
            for line in lines:
                if line.strip().startswith("```") and not inside:
                    inside = True
                    continue
                elif line.strip() == "```" and inside:
                    break
                elif inside:
                    json_lines.append(line)
            try:
                return json.loads("\n".join(json_lines))
            except json.JSONDecodeError:
                pass
        # Try finding JSON object in text
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
        # Fallback
        return {"thought": text[:200], "action": "conclude", "action_input": text}

    def _build_result(self, conclusion: any, evidence_summary: Dict) -> ParadigmResult:
        """Build ParadigmResult from conclusion data."""
        if isinstance(conclusion, str):
            try:
                conclusion = json.loads(conclusion)
            except (json.JSONDecodeError, TypeError):
                conclusion = {"root_cause": conclusion, "confidence": 0.5}

        if not isinstance(conclusion, dict):
            conclusion = {"root_cause": str(conclusion), "confidence": 0.5}

        return ParadigmResult(
            root_cause=conclusion.get("root_cause", ""),
            confidence=conclusion.get("confidence", 0),
            fault_type=conclusion.get("fault_type", ""),
            affected_services=conclusion.get("affected_services", []),
            evidence_summary={k: v[:300] for k, v in evidence_summary.items()},
            reasoning_chain=conclusion.get("reasoning_chain", ""),
            remediation_suggestion=conclusion.get("remediation_suggestion", ""),
            raw_output=conclusion if isinstance(conclusion, dict) else {},
        )
