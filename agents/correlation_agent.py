"""
AgenticSRE Correlation Agent
Cross-signal correlation analysis across Metric × Log × Trace × Event.
"""

import logging
from agents.evidence import format_evidence
from typing import Any, Dict, List, Optional

from tools.llm_client import LLMClient
from tools.hero_analysis import HeroCrossSignalCorrelator

logger = logging.getLogger(__name__)


class CorrelationAgent:
    """
    Cross-signal correlation agent that integrates evidence from all domain agents
    to build an anomaly matrix, compute composite scores, and identify the most
    affected services with multi-signal corroboration.
    """

    SYSTEM_PROMPT = """You are an expert SRE performing cross-signal correlation analysis.
You have anomaly data from metrics, logs, traces, and K8s events.
Analyze the cross-signal correlation to identify:
1. Services with anomalies across multiple signal types (strongest indicators)
2. Temporal correlation between different signal anomalies
3. Observed propagation paths, with unverified causal hypotheses labelled explicitly
4. Candidate origin services and the mechanism evidence still missing

Latency, error ratios, alert names and correlated traces do not prove internal
CPU, configuration, probe or application mechanisms. Do not infer absent
measurements or equate an application alert name with a Kubernetes event.

Be specific about correlation patterns. Use at most four concise bullets and
350 Chinese characters (or 200 English words). Do not reproduce raw spans,
identifiers or every measurement. This is an intermediate observation summary,
not the final report."""

    def __init__(self, llm: LLMClient):
        self.llm = llm
        self.correlator = HeroCrossSignalCorrelator()

    def correlate(self, evidence: Dict[str, Dict]) -> Dict:
        """
        Correlate evidence from all agents.
        
        evidence: {
            "metric_agent": {...},
            "log_agent": {...},
            "trace_agent": {...},
            "event_agent": {...},
        }
        """
        # Extract per-service anomalies from each agent's results
        metric_anomalies = self._extract_service_anomalies(evidence.get("metric_agent", {}), "metric")
        log_anomalies = self._extract_service_anomalies(evidence.get("log_agent", {}), "log")
        trace_anomalies = self._extract_service_anomalies(evidence.get("trace_agent", {}), "trace")
        event_anomalies = self._extract_service_anomalies(evidence.get("event_agent", {}), "event")

        # Build Hero anomaly matrix
        matrix = self.correlator.build_anomaly_matrix(
            metric_anomalies, log_anomalies, trace_anomalies, event_anomalies
        )

        # LLM interpretation
        context = "Cross-Signal Anomaly Matrix:\n"
        for svc_data in matrix.get("ranked_services", [])[:15]:
            context += (
                f"\n[{svc_data['service']}]: "
                f"metric={svc_data['metric_anomalies']} "
                f"log={svc_data['log_anomalies']} "
                f"trace={svc_data['trace_anomalies']} "
                f"event={svc_data['event_anomalies']} "
                f"signals={svc_data['signal_count']} "
                f"score={svc_data['composite_score']}"
            )
        
        # Add evidence summaries
        context += "\n\n" + format_evidence(evidence, limit=5000)

        summary = self.llm.chat([
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": context[:8000]}
        ], max_tokens=1536, thinking=False)

        return {
            "agent": "correlation_agent",
            "anomaly_matrix": matrix,
            "top_suspect": matrix.get("top_suspect", ""),
            "summary": summary,
        }

    def _extract_service_anomalies(self, agent_result: Dict, signal_type: str) -> Dict[str, List]:
        """Extract per-service anomaly list from an agent's results."""
        anomalies = {}
        
        if signal_type == "metric":
            for key, data in agent_result.get("anomaly_details", {}).items():
                # key format: "metric_name:service_name"
                parts = key.split(":")
                svc = parts[1] if len(parts) > 1 else parts[0]
                if svc not in anomalies:
                    anomalies[svc] = []
                anomalies[svc].extend(data.get("anomalies", []))
        
        elif signal_type == "log":
            # Extract from pattern analysis
            for entry in agent_result.get("pattern_analysis", {}).get("error_samples", []):
                svc = "unknown"  # Would need pod name from log entry
                if svc not in anomalies:
                    anomalies[svc] = []
                anomalies[svc].append(entry)
        
        elif signal_type == "trace":
            for svc, result in agent_result.get("latency_results", {}).items():
                if result.get("slow_trace_count", 0) > 0:
                    anomalies[svc] = result.get("slow_traces", [])
        
        elif signal_type == "event":
            for pod in agent_result.get("problem_pods", []):
                svc = pod.get("name", "unknown")
                if svc not in anomalies:
                    anomalies[svc] = []
                anomalies[svc].append(pod)
        
        return anomalies
