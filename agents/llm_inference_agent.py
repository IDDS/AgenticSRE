"""vLLM/GPU inference evidence agent."""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from agents.evidence import make_evidence_item
from tools.base_tool import ToolRegistry
from tools.llm_client import LLMClient

logger = logging.getLogger(__name__)


class LLMInferenceAgent:
    """Collects vLLM and GPU-specific evidence for inference incidents."""

    SYSTEM_PROMPT = """You are an SRE specializing in vLLM/GPU inference systems.
Analyze vLLM runtime metrics, Kubernetes pod state, GPU/NCCL/CUDA symptoms, and model asset errors.
Focus on TTFT, TPOT, waiting/running requests, KV cache, GPU utilization, model loading, tokenizer, and endpoint availability.
Return concise, evidence-grounded findings."""

    VLLM_QUERIES = [
        ("vllm_waiting_requests", "sum(vllm:num_requests_waiting)"),
        ("vllm_running_requests", "sum(vllm:num_requests_running)"),
        ("vllm_gpu_cache_usage", "avg(vllm:gpu_cache_usage_perc)"),
        ("vllm_generation_throughput", "avg(vllm:avg_generation_throughput_toks_per_s)"),
        ("vllm_request_success_rate", "sum(rate(vllm:request_success_total[5m]))"),
        ("vllm_request_failure_rate", "sum(rate(vllm:request_failure_total[5m]))"),
        ("container_gpu_utilization", "avg(DCGM_FI_DEV_GPU_UTIL) by (pod)"),
        ("container_gpu_memory", "avg(DCGM_FI_DEV_FB_USED) by (pod)"),
    ]

    def __init__(self, llm: LLMClient, registry: ToolRegistry):
        self.llm = llm
        self.registry = registry
        self.summary_max_tokens = 1024

    async def analyze(self, query: str, namespace: str = "", deployment: str = "vllm-server") -> Dict:
        metrics = self._fetch_metrics()
        pod_state = self._kubectl(f"get pods -n {namespace or 'default'} -o wide | grep -E '{deployment}|vllm|llm|gpu' || true")
        recent_events = self._kubectl(f"get events -n {namespace or 'default'} --sort-by=.lastTimestamp | tail -40")
        logs = self._kubectl(
            f"logs deploy/{deployment} -n {namespace or 'default'} --tail=120 2>/dev/null | "
            "grep -Ei 'error|exception|cuda|nccl|oom|kv|tokenizer|model|safetensor|permission|timeout' | tail -60 || true"
        )

        evidence_items = self._build_evidence_items(metrics, pod_state, recent_events, logs, deployment)
        context = self._format_context(query, metrics, pod_state, recent_events, logs, evidence_items)
        try:
            summary = await self.llm.async_chat([
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": context[:9000]},
            ], max_tokens=self.summary_max_tokens)
        except Exception as e:
            logger.warning("LLMInferenceAgent summary failed: %s", e)
            summary = self._fallback_summary(evidence_items, metrics, pod_state, logs)

        return {
            "agent": "llm_inference_agent",
            "query": query,
            "deployment": deployment,
            "metrics": metrics,
            "pod_state": pod_state,
            "recent_events": recent_events,
            "logs": logs,
            "evidence_items": evidence_items,
            "summary": summary,
        }

    def _fetch_metrics(self) -> Dict[str, Dict]:
        prom = self.registry.get("prometheus")
        if prom is None:
            return {}
        out = {}
        for name, promql in self.VLLM_QUERIES:
            try:
                result = prom.execute(query=promql, query_type="instant")
                if result.success:
                    out[name] = result.data
                else:
                    out[name] = {"error": result.error}
            except Exception as e:
                out[name] = {"error": str(e)}
        return out

    def _kubectl(self, command: str) -> Dict[str, str]:
        kubectl = self.registry.get("kubectl")
        if kubectl is None:
            return {"ok": False, "output": "kubectl tool unavailable"}
        try:
            result = kubectl.execute(command=command)
            return {"ok": result.success, "output": str(result.data or result.error or "")[-5000:]}
        except Exception as e:
            return {"ok": False, "output": str(e)}

    def _build_evidence_items(self, metrics, pod_state, recent_events, logs, deployment: str) -> List[Dict]:
        items: List[Dict] = []
        for name, data in metrics.items():
            if data.get("error"):
                continue
            result_count = data.get("result_count", 0)
            if result_count:
                items.append(make_evidence_item(
                    "llm_metric",
                    f"{name} returned {result_count} Prometheus series",
                    service=deployment,
                    source="prometheus",
                    severity="warning" if any(k in name for k in ["failure", "waiting", "cache"]) else "info",
                    raw_ref={"metric": name, "data": data},
                ))
        for signal_type, payload, source in [
            ("k8s_pod_state", pod_state, "kubectl get pods"),
            ("k8s_event", recent_events, "kubectl get events"),
            ("llm_log", logs, "kubectl logs"),
        ]:
            text = payload.get("output", "")
            if text.strip():
                severity = "critical" if any(k in text.lower() for k in ["cuda", "nccl", "oom", "permission", "crash"]) else "warning"
                items.append(make_evidence_item(
                    signal_type,
                    text[:800],
                    service=deployment,
                    source=source,
                    severity=severity,
                    raw_ref={"ok": payload.get("ok")},
                ))
        return items[:20]

    def _format_context(self, query, metrics, pod_state, recent_events, logs, evidence_items) -> str:
        metric_lines = []
        for name, data in metrics.items():
            metric_lines.append(f"- {name}: count={data.get('result_count', 0)} error={data.get('error', '')}")
        return "\n".join([
            f"Incident: {query}",
            "vLLM/GPU Metrics:",
            "\n".join(metric_lines),
            f"Pod State:\n{pod_state.get('output', '')[:1600]}",
            f"Recent Events:\n{recent_events.get('output', '')[:1600]}",
            f"Relevant Logs:\n{logs.get('output', '')[:2200]}",
            f"Structured Evidence:\n{evidence_items}",
        ])

    def _fallback_summary(self, evidence_items, metrics, pod_state, logs) -> str:
        parts = [f"Collected {len(evidence_items)} vLLM/GPU evidence items."]
        if pod_state.get("output"):
            parts.append("Pod state was available.")
        if logs.get("output"):
            parts.append("Relevant model/CUDA/NCCL/error logs were found.")
        if metrics:
            parts.append(f"Queried {len(metrics)} vLLM/GPU metric families.")
        return " ".join(parts)

