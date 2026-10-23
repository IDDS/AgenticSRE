"""
AgenticSRE Observability Tools
Prometheus, Elasticsearch, and Jaeger client tools.
"""

import json
import subprocess
import time
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests

from tools.base_tool import SRETool, ToolResult

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════
#  Prometheus Tool
# ═══════════════════════════════════════════

class PrometheusTool(SRETool):
    """Query Prometheus for metrics with PromQL, supports instant & range queries."""

    name = "prometheus"
    description = "Execute PromQL queries against Prometheus for metric data"

    def __init__(self, base_url: str = "", llm_client=None):
        self.base_url = base_url.rstrip("/") if base_url else ""
        self.llm = llm_client
        self.session = requests.Session()
        self._discovered = False

    def _execute(self, query: str = "", query_type: str = "instant",
                 start: str = "", end: str = "", step: str = "60s",
                 natural_language: str = "", url: str = "",
                 base_url: str = "", prometheus_url: str = "", max_results: int = 50) -> ToolResult:
        if type(max_results) is not int or not 1 <= max_results <= 5000:
            return ToolResult(success=False, error='max_results must be between 1 and 5000')
        explicit_url = (url or base_url or prometheus_url or "").strip().rstrip("/")
        original_url = self.base_url
        if explicit_url:
            self.base_url = explicit_url
        if not self.base_url:
            return self._stub_execute(query or natural_language)

        # Natural language → PromQL via LLM
        if natural_language and not query and self.llm:
            query = self._nl_to_promql(natural_language)

        if not query:
            return ToolResult(success=False, error="No query provided")

        try:
            self._ensure_reachable()
            if query_type == "range":
                url = f"{self.base_url}/api/v1/query_range"
                params = {"query": query, "step": step}
                if start:
                    params["start"] = start
                else:
                    params["start"] = str(int(time.time()) - 3600)  # last 1h
                if end:
                    params["end"] = end
                else:
                    params["end"] = str(int(time.time()))
            else:
                url = f"{self.base_url}/api/v1/query"
                params = {"query": query}

            resp = self.session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            if data.get("status") == "success":
                results = data.get("data", {}).get("result", [])
                return ToolResult(success=True, data={
                    "query": query,
                    "result_count": len(results),
                    "results": results[:max_results],
                    "truncated": len(results) > max_results,
                })
            else:
                return ToolResult(success=False, error=data.get("error", "Unknown error"))

        except requests.Timeout:
            return ToolResult(success=False, error="Prometheus query timed out")
        except Exception as e:
            if not self._discovered:
                discovered = self._discover_prometheus_url()
                if discovered:
                    self.base_url = discovered
                    self._discovered = True
                    return self._execute(
                        query=query,
                        query_type=query_type,
                        start=start,
                        end=end,
                        step=step,
                        natural_language="",
                        max_results=max_results,
                    )
            return ToolResult(success=False, error=str(e))
        finally:
            if explicit_url:
                self.base_url = original_url

    def _ensure_reachable(self):
        """Switch to a discovered endpoint if configured URL is not Prometheus."""
        if self._discovered:
            return
        if self._prometheus_api_ok(self.base_url):
            return
        discovered = self._discover_prometheus_url()
        if discovered:
            logger.info("PrometheusTool endpoint selected: %s", discovered)
            self.base_url = discovered
            self._discovered = True

    def _prometheus_api_ok(self, base_url: str) -> bool:
        if not base_url:
            return False
        try:
            resp = self.session.get(
                f"{base_url.rstrip('/')}/api/v1/query",
                params={"query": "up"},
                timeout=4,
            )
            if resp.status_code != 200:
                return False
            data = resp.json()
            return data.get("status") == "success" and "data" in data
        except Exception:
            return False

    def _discover_prometheus_url(self) -> str:
        """Discover Prometheus via K8S NodePort when localhost/ClusterIP is not reachable."""
        candidates: List[str] = []
        try:
            svc_raw = subprocess.run(
                "kubectl get svc -A -o json",
                shell=True,
                capture_output=True,
                text=True,
                timeout=8,
            )
            node_raw = subprocess.run(
                "kubectl get nodes -o json",
                shell=True,
                capture_output=True,
                text=True,
                timeout=8,
            )
            svc_data = json.loads(svc_raw.stdout) if svc_raw.returncode == 0 else {}
            node_data = json.loads(node_raw.stdout) if node_raw.returncode == 0 else {}
        except Exception:
            svc_data = {}
            node_data = {}

        nodes: List[str] = []
        for node in node_data.get("items", []):
            for addr in node.get("status", {}).get("addresses", []):
                if addr.get("type") == "InternalIP" and addr.get("address"):
                    nodes.append(addr["address"])

        for svc in svc_data.get("items", []):
            meta = svc.get("metadata", {})
            spec = svc.get("spec", {})
            ns_name = f"{meta.get('namespace', '')}/{meta.get('name', '')}".lower()
            if "prom" not in ns_name:
                continue
            for port in spec.get("ports", []):
                port_num = port.get("port")
                name = str(port.get("name", "")).lower()
                if port_num != 9090 and "web" not in name and "prom" not in name:
                    continue
                cluster_ip = spec.get("clusterIP")
                if cluster_ip and cluster_ip != "None":
                    candidates.append(f"http://{cluster_ip}:{port_num}")
                node_port = port.get("nodePort")
                if node_port:
                    for node_ip in nodes:
                        candidates.append(f"http://{node_ip}:{node_port}")

        seen = set()
        for url in candidates:
            if url in seen:
                continue
            seen.add(url)
            if self._prometheus_api_ok(url):
                return url
        return ""

    def _nl_to_promql(self, nl: str) -> str:
        """Convert natural language to PromQL using LLM."""
        if not self.llm:
            return nl
        try:
            result = self.llm.chat([
                {"role": "system", "content": (
                    "You are a Prometheus PromQL expert. Convert the natural language query to a valid PromQL expression. "
                    "Return ONLY the PromQL query, nothing else."
                )},
                {"role": "user", "content": nl}
            ])
            return result.strip().strip('`').strip()
        except Exception:
            return nl

    def _stub_execute(self, query: str) -> ToolResult:
        return ToolResult(
            success=True,
            data={"query": query, "results": [], "note": "STUB - no Prometheus configured"},
            source=self.name,
        )

    def _parameters_schema(self):
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "PromQL query"},
                "query_type": {"type": "string", "enum": ["instant", "range"], "default": "instant"},
                "start": {"type": "string", "description": "Range query start (RFC3339 or unix timestamp)"},
                "end": {"type": "string", "description": "Range query end"},
                "step": {"type": "string", "description": "Range query step", "default": "60s"},
                "natural_language": {"type": "string", "description": "Natural language metric query"},
            },
        }

    def health_check(self) -> bool:
        if not self.base_url:
            return False
        try:
            resp = self.session.get(f"{self.base_url}/-/healthy", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False


# ═══════════════════════════════════════════
#  Elasticsearch Tool
# ═══════════════════════════════════════════

class ElasticsearchTool(SRETool):
    """Search Elasticsearch for log data."""

    name = "elasticsearch"
    description = "Search Elasticsearch for log entries by keyword, time range, and severity level"

    def __init__(self, base_url: str = ""):
        self.base_url = base_url.rstrip("/") if base_url else ""
        self.session = requests.Session()

    def _execute(self, query: str = "", index: str = "filebeat-*",
                 time_range: str = "1h", level: str = "",
                 size: int = 100, namespace: str = "") -> ToolResult:
        if not self.base_url:
            return self._stub_execute(query)

        try:
            # Build ES query
            must_clauses = []
            if query:
                must_clauses.append({"query_string": {"query": query}})
            if level:
                must_clauses.append({"match": {"level": level}})
            if namespace:
                must_clauses.append({"match": {"kubernetes.namespace": namespace}})

            # Time range
            now_ms = int(time.time() * 1000)
            hours = int(time_range.replace("h", "").replace("m", "")) if time_range else 1
            multiplier = 3600000 if "h" in time_range else 60000
            gte = now_ms - hours * multiplier

            must_clauses.append({
                "range": {"@timestamp": {"gte": gte, "lte": now_ms, "format": "epoch_millis"}}
            })

            body = {
                "query": {"bool": {"must": must_clauses}},
                "sort": [{"@timestamp": {"order": "desc"}}],
                "size": min(size, 500),
            }

            url = f"{self.base_url}/{index}/_search"
            resp = self.session.post(url, json=body, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            hits = data.get("hits", {})
            total = hits.get("total", {}).get("value", 0) if isinstance(hits.get("total"), dict) else hits.get("total", 0)
            entries = []
            for hit in hits.get("hits", []):
                src = hit.get("_source", {})
                entries.append({
                    "timestamp": src.get("@timestamp", ""),
                    "level": src.get("level", src.get("log", {}).get("level", "")),
                    "message": src.get("message", "")[:500],
                    "pod": src.get("kubernetes", {}).get("pod", {}).get("name", ""),
                    "namespace": src.get("kubernetes", {}).get("namespace", ""),
                })

            return ToolResult(success=True, data={
                "total_hits": total,
                "returned": len(entries),
                "entries": entries,
            })
        except Exception as e:
            return ToolResult(success=False, error=str(e))

    def _stub_execute(self, query: str) -> ToolResult:
        return ToolResult(
            success=True,
            data={"query": query, "entries": [], "note": "STUB - no Elasticsearch configured"},
        )

    def _parameters_schema(self):
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "index": {"type": "string", "default": "filebeat-*"},
                "time_range": {"type": "string", "default": "1h"},
                "level": {"type": "string", "enum": ["", "error", "warn", "info", "debug"]},
                "size": {"type": "integer", "default": 100},
                "namespace": {"type": "string"},
            },
        }

    def health_check(self) -> bool:
        if not self.base_url:
            return False
        try:
            resp = self.session.get(f"{self.base_url}/_cluster/health", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False


# ═══════════════════════════════════════════
#  Jaeger Tool
# ═══════════════════════════════════════════

class JaegerTool(SRETool):
    """Query Jaeger for distributed traces."""

    name = "jaeger"
    description = "Fetch distributed traces from Jaeger by service, operation, and duration"

    def __init__(self, base_url: str = ""):
        self.base_url = base_url.rstrip("/") if base_url else ""
        self.session = requests.Session()
        self._discovered = False

    def _services_for(self, base_url: str):
        if not base_url:
            return None
        try:
            resp = self.session.get(f"{base_url.rstrip('/')}/api/services", timeout=5)
            if resp.status_code != 200:
                return None
            data = resp.json()
            services = data.get("data", [])
            return services if isinstance(services, list) else None
        except Exception:
            return None

    def _discover_base_url(self) -> str:
        """Discover Jaeger through K8s services when configured URL is stale."""
        if self._discovered and self.base_url:
            return self.base_url

        configured_services = self._services_for(self.base_url)
        preferred_markers = {
            "nginx-web-server", "compose-post-service", "home-timeline-service",
            "user-timeline-service", "post-storage-service",
        }
        if configured_services and preferred_markers.intersection(set(configured_services)):
            self._discovered = True
            return self.base_url

        candidates = [self.base_url] if self.base_url else []
        try:
            svc = subprocess.run(
                "kubectl get svc -A -o json",
                shell=True,
                capture_output=True,
                text=True,
                timeout=8,
            )
            nodes = subprocess.run(
                "kubectl get nodes -o json",
                shell=True,
                capture_output=True,
                text=True,
                timeout=8,
            )
            svc_data = json.loads(svc.stdout) if svc.returncode == 0 else {}
            node_data = json.loads(nodes.stdout) if nodes.returncode == 0 else {}
            node_ips = [
                addr.get("address")
                for node in node_data.get("items", [])
                for addr in node.get("status", {}).get("addresses", [])
                if addr.get("type") == "InternalIP" and addr.get("address")
            ]
            for item in svc_data.get("items", []):
                meta = item.get("metadata", {})
                spec = item.get("spec", {})
                ns_name = f"{meta.get('namespace', '')}/{meta.get('name', '')}".lower()
                if not any(token in ns_name for token in ("jaeger", "trace")):
                    continue
                for port in spec.get("ports", []):
                    port_num = port.get("port")
                    port_name = str(port.get("name", "")).lower()
                    if port_num != 16686 and "query" not in port_name and "ui" not in port_name:
                        continue
                    node_port = port.get("nodePort")
                    if node_port:
                        candidates.extend(f"http://{ip}:{node_port}" for ip in node_ips)
                    cluster_ip = spec.get("clusterIP")
                    if cluster_ip and cluster_ip != "None":
                        candidates.append(f"http://{cluster_ip}:{port_num}")
        except Exception:
            pass

        first_with_services = ""
        seen = set()
        for url in candidates:
            if not url or url in seen:
                continue
            seen.add(url)
            services = self._services_for(url)
            if not services:
                continue
            if preferred_markers.intersection(set(services)):
                self.base_url = url.rstrip("/")
                self._discovered = True
                return self.base_url
            if not first_with_services:
                first_with_services = url.rstrip("/")

        if first_with_services:
            self.base_url = first_with_services
        self._discovered = True
        return self.base_url

    def _execute(self, service: str = "", operation: str = "",
                 min_duration: str = "", max_duration: str = "",
                 limit: int = 20, lookback: str = "1h",
                 trace_id: str = "") -> ToolResult:
        base_url = self._discover_base_url()
        if not base_url:
            return self._stub_execute(service)

        try:
            if trace_id:
                url = f"{base_url}/api/traces/{trace_id}"
                resp = self.session.get(url, timeout=30)
                resp.raise_for_status()
                return ToolResult(success=True, data=resp.json())

            if not service:
                # List available services
                url = f"{base_url}/api/services"
                resp = self.session.get(url, timeout=10)
                resp.raise_for_status()
                return ToolResult(success=True, data=resp.json())

            params = {"service": service, "limit": limit, "lookback": lookback}
            if operation:
                params["operation"] = operation
            if min_duration:
                params["minDuration"] = min_duration
            if max_duration:
                params["maxDuration"] = max_duration

            url = f"{base_url}/api/traces"
            resp = self.session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            traces = data.get("data", [])
            summaries = []
            for trace in traces[:limit]:
                spans = trace.get("spans", [])
                processes = trace.get("processes", {})
                services_in_trace = sorted(set(
                    processes.get(s.get("processID", ""), {}).get("serviceName", "")
                    for s in spans
                    if processes.get(s.get("processID", ""), {}).get("serviceName", "")
                ))
                durations = [s.get("duration", 0) for s in spans]
                summaries.append({
                    "traceID": trace.get("traceID", ""),
                    "span_count": len(spans),
                    "services": services_in_trace,
                    "total_duration_us": max(durations) if durations else 0,
                    "avg_duration_us": sum(durations) // max(len(durations), 1),
                })

            return ToolResult(success=True, data={
                "service": service,
                "trace_count": len(summaries),
                "traces": summaries,
            })
        except Exception as e:
            return ToolResult(success=False, error=str(e))

    def _stub_execute(self, service: str) -> ToolResult:
        return ToolResult(
            success=True,
            data={"service": service, "traces": [], "note": "STUB - no Jaeger configured"},
        )

    def _parameters_schema(self):
        return {
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                "operation": {"type": "string"},
                "min_duration": {"type": "string"},
                "max_duration": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
                "lookback": {"type": "string", "default": "1h"},
                "trace_id": {"type": "string"},
            },
        }

    def health_check(self) -> bool:
        if not self.base_url:
            return False
        try:
            return bool(self._services_for(self._discover_base_url()))
        except Exception:
            return False
