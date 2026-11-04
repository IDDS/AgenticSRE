"""
AgenticSRE Planning Agent
Generates investigation plans based on hypotheses and available tools.
SOW Requirement: "规划智能体"
"""

import logging
from typing import Any, Dict, List, Optional

from tools.base_tool import ToolRegistry
from tools.llm_client import LLMClient

logger = logging.getLogger(__name__)


class PlanningAgent:
    """
    Planning agent that generates structured investigation plans 
    based on hypotheses and available tools.
    
    SOW: "构建专用智能体（比如规划、告警、日志、指标、调用链、Profiling、操作系统运维）"
    """

    SYSTEM_PROMPT = """You are an expert SRE planning agent for Kubernetes clusters.
Given root cause hypotheses and available investigation tools, generate a structured 
investigation plan that maximizes evidence collection efficiency.

Available investigation capabilities:
- Kubernetes: kubectl commands, pod/node/service inspection, events
- Metrics: Prometheus PromQL queries 
- Logs: Elasticsearch search, pod log retrieval
- Traces: Jaeger distributed trace analysis
- Anomaly Detection: Z-score, IQR, rate change detection

Plan principles:
1. Start with quick, cheap checks before expensive ones
2. Gather multi-signal evidence (metrics + logs + events) for correlation
3. Focus investigation on the most likely hypotheses first
4. Name concrete resources and data sources; do not generate shell commands
5. Define clear success/failure criteria for each step
6. Use only the listed investigation agents and tools; this is a pure multi-agent workflow.
7. Compare the first two active hypotheses. Prefer a read-only observation whose
   outcomes distinguish them, including an explicit refuting outcome. Do not only
   collect confirmations of Top1. Collector errors are missing data, not refutations.
8. Use previous attempt feedback to change sources or questions; repeated unchanged
   observations do not increase confidence. Tool actions remain host controlled.
9. Return no more than two plan steps: the host selects at most two agents from
   this plan; required collectors may be added separately. Use concise field values
   (at most 160 characters each), with
   no explanatory text outside the JSON. If two hypotheses need the same agent,
   describe the distinguishing observation in one step rather than duplicating it.

Respond in JSON:
{
    "plan": [
        {
            "step": 1,
            "agent": "metric_agent|log_agent|trace_agent|event_agent|llm_inference_agent",
            "action": "specific action description",
            "target_hypothesis": "H1",
            "expected_evidence": "what we expect to find",
            "supports_if": "observable result supporting the target hypothesis",
            "refutes_if": "observable result contradicting the target hypothesis",
            "priority": "high|medium|low"
        }
    ],
    "parallel_groups": [[1, 2], [3, 4]],
    "estimated_duration_seconds": 60
}"""

    def __init__(self, llm: LLMClient, registry: ToolRegistry):
        self.llm = llm
        self.registry = registry
        self.bounded_plan = False

    def _system_prompt(self) -> str:
        if self.bounded_plan:
            return self.SYSTEM_PROMPT
        # Other paradigms may dispatch more than two agents and use plan
        # details differently. Preserve their existing, broader contract.
        bounded_rule = ("9. Return no more than two plan steps: the host selects at most two agents from\n"
            "   this plan; required collectors may be added separately. Use concise field values\n"
            "   (at most 160 characters each), with\n"
            "   no explanatory text outside the JSON. If two hypotheses need the same agent,\n"
            "   describe the distinguishing observation in one step rather than duplicating it.\n\n")
        return (self.SYSTEM_PROMPT.replace(bounded_rule, '')
            .replace('4. Name concrete resources and data sources; do not generate shell commands',
                     '4. Include specific commands/queries, not vague instructions')
            .replace('metric_agent|log_agent|trace_agent|event_agent|llm_inference_agent',
                     'metric_agent|log_agent|trace_agent|event_agent|kubectl')
            .replace('            "action": "specific action description",\n',
                     '            "action": "specific action description",\n'
                     '            "command": "specific command or query",\n'))

    def generate_plan(self, hypotheses: List[Dict], incident_signal: str,
                      iteration: int = 0) -> Dict:
        """Generate an investigation plan for the current iteration."""
        
        # List available tools
        available_tools = self.registry.list_tools()
        tool_summary = ", ".join([t["name"] for t in available_tools])

        hyp_text = ""
        for h in hypotheses:
            hyp_text += (
                f"[{h.get('id', '?')}] (confidence={h.get('confidence', 0):.2f}) "
                f"{h.get('description', '')[:200]}\n"
                f"  Service: {h.get('service', '?')}, Type: {h.get('fault_type', '?')}\n"
            )

        try:
            result = self.llm.json_chat([
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": (
                    f"Incident: {incident_signal[:2000]}\n\n"
                    f"Investigation Iteration: {iteration + 1}\n\n"
                    f"Current Hypotheses:\n{hyp_text}\n\n"
                    f"Available Tools: {tool_summary}\n"
                )}
            ], max_tokens=1536, thinking=False)
            if not self.bounded_plan:
                return result
            steps = result.get('plan') if isinstance(result, dict) and not result.get('parse_error') else None
            allowed = {'metric_agent', 'log_agent', 'trace_agent', 'event_agent', 'llm_inference_agent'}
            if (not isinstance(steps, list) or not 1 <= len(steps) <= 2
                    or any(not isinstance(step, dict) or step.get('agent') not in allowed
                           or not isinstance(step.get('action'), str) or not step['action'].strip()
                           or str(step.get('priority', '')).lower() not in ('high', 'medium', 'low', '')
                           for step in steps)):
                raise ValueError('invalid_model_plan')
            return {**result, 'planning_status': 'complete'}
        except Exception as e:
            logger.error("Plan generation failed: %s", type(e).__name__)
            return {
                "planning_status": "fallback",
                "planning_error": "model_plan_unavailable_or_invalid",
                "plan": [
                    {"step": 1, "agent": "event_agent", "action": "Check K8s events and pod status",
                     "priority": "high"},
                    {"step": 2, "agent": "metric_agent", "action": "Analyze Prometheus metrics",
                     "priority": "high"},
                    {"step": 3, "agent": "log_agent", "action": "Search error logs",
                     "priority": "medium"},
                ],
                "parallel_groups": [[1, 2, 3]],
            }
