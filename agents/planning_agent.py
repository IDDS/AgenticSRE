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
4. Include specific commands/queries, not vague instructions
5. Define clear success/failure criteria for each step
6. Use only the listed investigation agents and tools; this is a pure multi-agent workflow.
7. Compare the first two active hypotheses. Prefer a read-only observation whose
   outcomes distinguish them, including an explicit refuting outcome. Do not only
   collect confirmations of Top1. Collector errors are missing data, not refutations.
8. Use previous attempt feedback to change sources or questions; repeated unchanged
   observations do not increase confidence. Tool actions remain host controlled.

Respond in JSON:
{{
    "plan": [
        {{
            "step": 1,
            "agent": "metric_agent|log_agent|trace_agent|event_agent|kubectl",
            "action": "specific action description",
            "command": "specific command or query",
            "target_hypothesis": "H1",
            "expected_evidence": "what we expect to find",
            "supports_if": "observable result supporting the target hypothesis",
            "refutes_if": "observable result contradicting the target hypothesis",
            "priority": "high|medium|low"
        }}
    ],
    "parallel_groups": [[1, 2], [3, 4]],
    "estimated_duration_seconds": 60
}}"""

    def __init__(self, llm: LLMClient, registry: ToolRegistry):
        self.llm = llm
        self.registry = registry

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
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": (
                    f"Incident: {incident_signal[:2000]}\n\n"
                    f"Investigation Iteration: {iteration + 1}\n\n"
                    f"Current Hypotheses:\n{hyp_text}\n\n"
                    f"Available Tools: {tool_summary}\n"
                )}
            ], max_tokens=1536, thinking=False)
            return result
        except Exception as e:
            logger.error(f"Plan generation failed: {e}")
            return {
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
