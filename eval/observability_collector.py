"""
AgenticSRE Observability Data Collector
Collects Prometheus metrics, ES logs, K8s events, and Jaeger traces
into a unified snapshot — used by baselines that cannot call tools themselves.
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional

from tools.base_tool import ToolRegistry

logger = logging.getLogger(__name__)


class ObservabilityCollector:
    """
    Collects a snapshot of all relevant observability data for a given
    incident / namespace.  Produces two formats:

    - text_snapshot: A single formatted string suitable for direct LLM prompts.
    - structured_snapshot: A dict suitable for programmatic consumption.
    """

    def __init__(self, registry: ToolRegistry, namespace: str = ""):
        self.registry = registry
        self.namespace = namespace

    def collect(self, incident_query: str, lookback: str = "30m") -> Dict[str, Any]:
        """Collect all signals and return both text and structured formats."""
        start = time.time()

        metrics = self._collect_metrics()
        logs = self._collect_logs(incident_query, lookback)
        events = self._collect_events()
        pods = self._collect_pod_status()
        nodes = self._collect_node_status()
        traces = self._collect_traces(lookback)

        elapsed = round(time.time() - start, 2)

        structured = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "namespace": self.namespace,
            "incident_query": incident_query,
            "collection_time_s": elapsed,
            "metrics": metrics,
            "logs": logs,
            "events": events,
            "pods": pods,
            "nodes": nodes,
            "traces": traces,
        }

        text = self._format_text(structured)

        return {
            "text_snapshot": text,
            "structured_snapshot": structured,
            "collection_time_s": elapsed,
        }

    # ── Metric Collection ──

    def _collect_metrics(self) -> Dict[str, Any]:
        """Query key Prometheus metrics."""
        prom = self.registry.get("prometheus")
        if not prom:
            return {"available": False, "note": "Prometheus not configured"}

        queries = {
            "node_cpu": 'avg by(instance)(1 - rate(node_cpu_seconds_total{mode="idle"}[1m])) * 100',
            "node_memory": '(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100',
            "node_disk": '(1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}) * 100',
            "container_cpu": 'sum(rate(container_cpu_usage_seconds_total{container!=""}[1m])) by (pod, namespace) * 100',
            "container_memory": 'sum(container_memory_working_set_bytes{container!=""}) by (pod, namespace)',
            "container_restarts": 'sum(kube_pod_container_status_restarts_total) by (pod, namespace)',
            "pod_not_ready": 'kube_pod_status_ready{condition="false"}',
            "network_receive_errors": 'sum(rate(node_network_receive_errs_total[5m])) by (instance)',
            "network_transmit_errors": 'sum(rate(node_network_transmit_errs_total[5m])) by (instance)',
            "http_error_rate": 'sum(rate(http_requests_total{code=~"5.."}[5m])) by (service)',
        }

        results = {}
        for name, query in queries.items():
            result = prom.execute(query=query)
            if result.success and result.data:
                results[name] = result.data.get("results", [])
            else:
                results[name] = []

        return {"available": True, "metrics": results}

    # ── Log Collection ──

    def _collect_logs(self, query: str, lookback: str) -> Dict[str, Any]:
        """Search ES for error and warning logs."""
        es = self.registry.get("elasticsearch")
        if not es:
            return {"available": False, "note": "Elasticsearch not configured"}

        entries = []
        for level in ["error", "warn"]:
            result = es.execute(
                query=query, level=level,
                time_range=lookback, size=50,
                namespace=self.namespace,
            )
            if result.success and result.data:
                entries.extend(result.data.get("entries", []))

        return {"available": True, "entry_count": len(entries), "entries": entries[:100]}

    # ── K8s Events ──

    def _collect_events(self) -> Dict[str, Any]:
        """Collect recent K8s warning events."""
        kubectl = self.registry.get("kubectl")
        if not kubectl:
            return {"available": False, "note": "kubectl not configured"}

        result = kubectl.execute(
            command="get events --field-selector type=Warning "
                    "--sort-by='.lastTimestamp' -o json",
            namespace=self.namespace,
        )
        if not result.success:
            return {"available": True, "events": [], "error": result.error}

        try:
            data = json.loads(result.data) if isinstance(result.data, str) else result.data
            events = []
            for item in (data.get("items", []) or [])[:50]:
                obj = item.get("involvedObject", {})
                events.append({
                    "reason": item.get("reason", ""),
                    "message": item.get("message", "")[:300],
                    "kind": obj.get("kind", ""),
                    "name": obj.get("name", ""),
                    "count": item.get("count", 1),
                    "last_timestamp": item.get("lastTimestamp", ""),
                })
            return {"available": True, "event_count": len(events), "events": events}
        except (json.JSONDecodeError, TypeError):
            return {"available": True, "events": [], "error": "Failed to parse events"}

    # ── Pod Status ──

    def _collect_pod_status(self) -> Dict[str, Any]:
        """Collect pod status summary."""
        kubectl = self.registry.get("kubectl")
        if not kubectl:
            return {"available": False}

        result = kubectl.execute(
            command="get pods -o json",
            namespace=self.namespace,
        )
        if not result.success:
            return {"available": True, "pods": [], "error": result.error}

        try:
            data = json.loads(result.data) if isinstance(result.data, str) else result.data
            pods = []
            for item in (data.get("items", []) or []):
                meta = item.get("metadata", {})
                status = item.get("status", {})
                containers = status.get("containerStatuses", [])
                restart_count = sum(c.get("restartCount", 0) for c in containers)
                ready_count = sum(1 for c in containers if c.get("ready", False))
                total_count = len(containers)

                pods.append({
                    "name": meta.get("name", ""),
                    "namespace": meta.get("namespace", ""),
                    "phase": status.get("phase", ""),
                    "ready": f"{ready_count}/{total_count}",
                    "restarts": restart_count,
                    "conditions": [
                        {"type": c.get("type"), "status": c.get("status")}
                        for c in status.get("conditions", [])
                    ],
                })
            return {"available": True, "pod_count": len(pods), "pods": pods}
        except (json.JSONDecodeError, TypeError):
            return {"available": True, "pods": [], "error": "Failed to parse pods"}

    # ── Node Status ──

    def _collect_node_status(self) -> Dict[str, Any]:
        """Collect node status summary."""
        kubectl = self.registry.get("kubectl")
        if not kubectl:
            return {"available": False}

        result = kubectl.execute(command="get nodes -o json")
        if not result.success:
            return {"available": True, "nodes": [], "error": result.error}

        try:
            data = json.loads(result.data) if isinstance(result.data, str) else result.data
            nodes = []
            for item in (data.get("items", []) or []):
                meta = item.get("metadata", {})
                status = item.get("status", {})
                conditions = {
                    c.get("type"): c.get("status")
                    for c in status.get("conditions", [])
                }
                nodes.append({
                    "name": meta.get("name", ""),
                    "ready": conditions.get("Ready", "Unknown"),
                    "memory_pressure": conditions.get("MemoryPressure", "False"),
                    "disk_pressure": conditions.get("DiskPressure", "False"),
                    "pid_pressure": conditions.get("PIDPressure", "False"),
                })
            return {"available": True, "node_count": len(nodes), "nodes": nodes}
        except (json.JSONDecodeError, TypeError):
            return {"available": True, "nodes": [], "error": "Failed to parse nodes"}

    # ── Trace Collection ──

    def _collect_traces(self, lookback: str) -> Dict[str, Any]:
        """Collect recent slow traces from Jaeger."""
        jaeger = self.registry.get("jaeger")
        if not jaeger:
            return {"available": False, "note": "Jaeger not configured"}

        # Get services first
        svc_result = jaeger.execute()
        if not svc_result.success or not svc_result.data:
            return {"available": True, "services": [], "traces": []}

        services = svc_result.data.get("data", []) or []
        all_traces = []

        for svc in services[:10]:
            result = jaeger.execute(service=svc, limit=5, lookback=lookback)
            if result.success and result.data:
                for t in result.data.get("traces", []):
                    t["service"] = svc
                    all_traces.append(t)

        return {"available": True, "services": services, "traces": all_traces[:30]}

    # ── Text Formatting ──

    def _format_text(self, data: Dict) -> str:
        """Format structured data as a readable text snapshot for LLM prompts."""
        parts = []
        parts.append(f"=== Observability Snapshot ===")
        parts.append(f"Time: {data['timestamp']}")
        parts.append(f"Namespace: {data['namespace'] or 'all'}")
        parts.append(f"Incident: {data['incident_query']}")
        parts.append("")

        # Metrics
        parts.append("--- METRICS (Prometheus) ---")
        metrics = data.get("metrics", {})
        if metrics.get("available"):
            for name, results in metrics.get("metrics", {}).items():
                if results:
                    parts.append(f"\n[{name}]:")
                    for r in results[:10]:
                        metric = r.get("metric", {})
                        value = r.get("value", ["", ""])[1] if isinstance(r.get("value"), list) else ""
                        label = metric.get("instance", "") or metric.get("pod", "") or str(metric)
                        parts.append(f"  {label}: {value}")
        else:
            parts.append("  (not available)")

        # Logs
        parts.append("\n--- LOGS (Elasticsearch) ---")
        logs = data.get("logs", {})
        if logs.get("available"):
            parts.append(f"  Total entries: {logs.get('entry_count', 0)}")
            for entry in logs.get("entries", [])[:30]:
                parts.append(
                    f"  [{entry.get('level', '?')}] {entry.get('timestamp', '')} "
                    f"pod={entry.get('pod', '?')}: {entry.get('message', '')[:200]}"
                )
        else:
            parts.append("  (not available)")

        # Events
        parts.append("\n--- K8S EVENTS (Warning) ---")
        events = data.get("events", {})
        if events.get("available"):
            for ev in events.get("events", [])[:30]:
                parts.append(
                    f"  [{ev.get('reason', '?')}] {ev.get('kind', '')}/{ev.get('name', '')}: "
                    f"{ev.get('message', '')[:200]} (count={ev.get('count', 1)})"
                )
            if not events.get("events"):
                parts.append("  (no warning events)")
        else:
            parts.append("  (not available)")

        # Pods
        parts.append("\n--- POD STATUS ---")
        pods_data = data.get("pods", {})
        if pods_data.get("available"):
            for pod in pods_data.get("pods", []):
                status_flag = ""
                if pod.get("phase") != "Running":
                    status_flag = " ⚠"
                if pod.get("restarts", 0) > 3:
                    status_flag = " ⚠ HIGH RESTARTS"
                parts.append(
                    f"  {pod.get('name', '?')}: phase={pod.get('phase', '?')} "
                    f"ready={pod.get('ready', '?')} restarts={pod.get('restarts', 0)}{status_flag}"
                )
        else:
            parts.append("  (not available)")

        # Nodes
        parts.append("\n--- NODE STATUS ---")
        nodes_data = data.get("nodes", {})
        if nodes_data.get("available"):
            for node in nodes_data.get("nodes", []):
                pressure = []
                if node.get("memory_pressure") == "True":
                    pressure.append("MemoryPressure")
                if node.get("disk_pressure") == "True":
                    pressure.append("DiskPressure")
                if node.get("pid_pressure") == "True":
                    pressure.append("PIDPressure")
                pressure_str = f" PRESSURE=[{','.join(pressure)}]" if pressure else ""
                parts.append(
                    f"  {node.get('name', '?')}: ready={node.get('ready', '?')}{pressure_str}"
                )

        # Traces
        parts.append("\n--- TRACES (Jaeger) ---")
        traces_data = data.get("traces", {})
        if traces_data.get("available"):
            for t in traces_data.get("traces", [])[:15]:
                parts.append(
                    f"  traceID={t.get('traceID', '?')[:12]}... "
                    f"service={t.get('service', '?')} "
                    f"spans={t.get('span_count', 0)} "
                    f"duration={t.get('total_duration_us', 0)}us"
                )
            if not traces_data.get("traces"):
                parts.append("  (no traces)")

        return "\n".join(parts)
