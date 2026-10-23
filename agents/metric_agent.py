"""
AgenticSRE Metric Agent
Fetches and analyzes Prometheus metrics with Hero 3σ + WeRCA onset detection.
"""

import logging
import asyncio
import json
import math
import re
import time
from typing import Any, Dict, List, Optional

from tools.base_tool import ToolRegistry, ToolResult
from tools.llm_client import LLMClient, run_sync_in_daemon_thread
from agents.evidence import make_evidence_item
from tools.hero_analysis import HeroMetricAnalyzer

logger = logging.getLogger(__name__)


class MetricAgent:
    """
    Metric analysis agent: fetches Prometheus metrics, runs statistical
    anomaly detection (Hero 3σ + WeRCA Pearson onset), and produces
    LLM-summarized metric insights.
    """

    SYSTEM_PROMPT = """You are a Kubernetes SRE metric analysis expert.
Analyze the provided Prometheus metric data and anomaly detection results.
Identify:
1. Which metrics show anomalous behavior
2. The severity and timing of anomalies
3. Correlation with potential infrastructure or application issues
4. Specific services/pods affected

Be precise and cite specific metric values. Write an incident-focused summary
in at most 250 Chinese characters or 150 English words. Select only the most
relevant measurements; do not enumerate all series or repeat the input.
State missing evidence explicitly; original samples remain available separately."""

    # Key metric queries for different fault categories
    METRIC_QUERIES = {
        "infrastructure": [
            ('node_cpu_usage', 'avg by(instance)(1 - rate(node_cpu_seconds_total{mode="idle"}[1m])) * 100'),
            ('node_memory_usage', '(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100'),
            ('node_disk_usage', '(1 - node_filesystem_avail_bytes / node_filesystem_size_bytes) * 100'),
            ('node_network_errors', 'rate(node_network_receive_errs_total[5m])'),
        ],
        "application": [
            ('container_cpu', 'sum(rate(container_cpu_usage_seconds_total[1m])) by (pod, namespace)'),
            ('container_memory', 'container_memory_working_set_bytes / container_spec_memory_limit_bytes * 100'),
            ('container_restarts', 'increase(kube_pod_container_status_restarts_total[1h])'),
            ('pod_not_ready', 'kube_pod_status_ready{condition="false"}'),
        ],
        "workload": [
            ('http_request_rate', 'sum(rate(http_requests_total[5m])) by (service, namespace)'),
            ('http_error_rate', 'sum(rate(http_requests_total{code=~"5.."}[5m])) by (service, namespace)'),
            ('http_latency_p99', 'histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, service, namespace))'),
        ],
    }
    # Semantics of the query result, rather than the underlying raw counter.
    METRIC_SEMANTICS = {
        "node_cpu_usage": ("percent", "ratio"), "node_memory_usage": ("percent", "ratio"),
        "node_disk_usage": ("percent", "ratio"), "node_network_errors": ("errors/second", "rate"),
        "container_cpu": ("cores", "rate"), "container_memory": ("percent", "ratio"),
        "container_restarts": ("count", "increase_over_1h"), "pod_not_ready": ("boolean", "gauge"),
        "http_request_rate": ("requests/second", "rate"), "http_error_rate": ("requests/second", "rate"),
        "http_latency_p99": ("seconds", "quantile_0.99"),
    }

    def __init__(self, llm: LLMClient, registry: ToolRegistry):
        self.llm = llm
        self.registry = registry
        self.hero = HeroMetricAnalyzer()
        self.summary_max_tokens = 1024
        self.summarize = True

    async def analyze(self, query: str, namespace: str = "",
                      categories: Optional[List[str]] = None) -> Dict:
        """Run metric analysis for the given incident query."""
        categories = categories or ["infrastructure", "application", "workload"]
        
        all_results = {}
        all_anomalies = {}
        evidence_items = []
        query_errors = []
        
        for category in categories:
            # Node metrics lack namespace identity; do not mix unrelated nodes
            # into a namespaced incident without a verified pod-to-node mapping.
            if namespace and category == "infrastructure":
                continue
            queries = self.METRIC_QUERIES.get(category, [])
            for metric_name, promql in queries:
                result = await run_sync_in_daemon_thread(self._fetch_metric, promql, namespace)
                if result and result.get("collection_error"):
                    query_errors.append({"metric": metric_name, "error": result["collection_error"]})
                if result and result.get("results"):
                    all_results[metric_name] = result
                    
                    # Run Hero 3σ anomaly detection on each series
                    for series in result.get("results", []):
                        values = self._extract_values(series)
                        if values:
                            labels = series.get("metric", {})
                            entity = labels.get("pod") or labels.get("service") or labels.get("instance") or metric_name
                            samples = self._samples(series)
                            evidence_items.append(make_evidence_item(
                                "metric_series_observed", service=entity, source="prometheus",
                                timestamp=str(samples[-1][0]),
                                observation=json.dumps({"metric": metric_name, "labels": labels,
                                    "latest": samples[-1][1], "min": min(values), "max": max(values),
                                    "sample_count": len(samples)}, ensure_ascii=False),
                                raw_ref={"query": result.get("query", promql), "namespace": namespace,
                                         "labels": labels, "samples": samples,
                                         "metric_name": metric_name,
                                         "unit": self.METRIC_SEMANTICS.get(metric_name, (None, None))[0],
                                         "value_type": self.METRIC_SEMANTICS.get(metric_name, (None, None))[1],
                                         "truncated": result.get("truncated", False)},
                            ))
                            service = entity
                            anomaly = self.hero.three_sigma_detect(values)
                            if anomaly.get("anomaly_count", 0) > 0:
                                label = series.get("metric", {})
                                service = (label.get("pod", "") or 
                                         label.get("instance", "") or 
                                         label.get("service", metric_name))
                                all_anomalies[f"{metric_name}:{service}"] = anomaly
                            
                            # WeRCA onset detection
                            onset = self.hero.pearson_onset_detection(values)
                            if onset.get("onset_points"):
                                all_anomalies[f"{metric_name}:{service}:onset"] = onset

        if not evidence_items:
            return {'agent': 'metric_agent', 'metrics_fetched': 0, 'error': True,
                    'evidence_items': [], 'collection_errors': query_errors,
                    'summary': '未取得目标范围内的有效指标，不能据此判定正常或确认根因。'}

        context = json.dumps({"incident": query, "observations": [
            {"observation": item["observation"], "timestamp": item["timestamp"]}
            for item in evidence_items], "anomalies": all_anomalies}, ensure_ascii=False)
        summary = ""
        summary_error = None
        if self.summarize:
            try:
                summary = await asyncio.wait_for(self.llm.async_chat([
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {"role": "user", "content": context[:8000]}
                ], max_tokens=self.summary_max_tokens, thinking=False), timeout=20)
                if not isinstance(summary, str) or not summary.strip():
                    summary = ""
                    summary_error = "empty_model_response"
            except Exception as exc:
                # Retain measured values after a bounded summary failure; external
                # task cancellation still propagates.
                summary_error = type(exc).__name__
        else:
            summary = f"已采集 {len(evidence_items)} 条指标观测；本次未请求模型摘要，请依据带来源的结构化观测进行推理。"
        return {
            "agent": "metric_agent", "query": query,
            "metrics_fetched": len(all_results), "anomalies_found": len(all_anomalies),
            "anomaly_details": all_anomalies, "evidence_items": evidence_items,
            "collection_errors": query_errors,
            "summary": summary or f"已采集 {len(evidence_items)} 条指标序列；模型摘要不可用，请依据原始观测复核，不能据此判定正常。",
            "summary_status": "not_requested" if not self.summarize else "ok" if summary else "unavailable",
            "summary_error": summary_error, "error": not bool(summary),
            "collection_status": "ok",
            "observations_truncated": any(data.get("truncated", False) for data in all_results.values()),
        }

    def _fetch_metric(self, promql: str, namespace: str = "") -> Optional[Dict]:
        """Fetch metric from Prometheus."""
        prom = self.registry.get("prometheus")
        if prom is None:
            return None
        if namespace:
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", namespace):
                return {"collection_error": "invalid_namespace"}
            def scoped(match):
                labels = match.group(2)
                return match.group(1) + '{' + (labels + ',' if labels else '') + 'namespace=' + json.dumps(namespace) + '}'
            promql = re.sub(r"\b((?:container|kube|http)_[a-zA-Z0-9_:]+)(?:\{([^}]*)\})?", scoped, promql)
        end = time.time()
        result = prom.execute(query=promql, query_type="range", start=str(end-600),
                              end=str(end), step="30s", max_results=32)
        if not result.success:
            return {"collection_error": result.error or "query_failed"}
        data = dict(result.data or {})
        data["results"] = [row for row in data.get("results", [])
                           if isinstance(row, dict) and (not namespace or
                              row.get("metric", {}).get("namespace") == namespace)]
        return data

    @staticmethod
    def _samples(series: Dict) -> List:
        rows = series.get("values") or ([series["value"]] if "value" in series else [])
        samples = []
        for row in rows:
            try:
                timestamp, value = float(row[0]), float(row[1])
                if math.isfinite(timestamp) and math.isfinite(value):
                    samples.append([timestamp, value])
            except (TypeError, ValueError, IndexError, OverflowError):
                continue
        return sorted(samples)[-21:]

    def _extract_values(self, series: Dict) -> List[float]:
        return [row[1] for row in self._samples(series)]
