"""
AgenticSRE Detection Agent
Continuous anomaly detection: polls Prometheus alerts, K8s events, ES errors, metric anomalies.
Phase 1 of the 5-phase pipeline.
"""

import time
import hashlib
import logging
import concurrent.futures
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field

from tools.base_tool import ToolRegistry
from tools.llm_client import LLMClient

logger = logging.getLogger(__name__)


@dataclass
class DetectionSignal:
    """A detected anomaly signal that may trigger the pipeline."""
    signal_id: str
    source: str             # prometheus | k8s_event | es_log | metric_detector
    severity: str           # critical | warning | info
    title: str
    description: str
    timestamp: float = 0.0
    namespace: str = ""
    service: str = ""
    fingerprint: str = ""
    raw_data: Any = None

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()
        if not self.fingerprint:
            content = f"{self.source}:{self.title}:{self.service}:{self.namespace}"
            self.fingerprint = hashlib.md5(content.encode()).hexdigest()[:12]
        if not self.signal_id:
            self.signal_id = f"sig-{self.fingerprint}-{int(self.timestamp)}"

    def to_dict(self) -> Dict:
        return {
            "signal_id": self.signal_id,
            "source": self.source,
            "severity": self.severity,
            "title": self.title,
            "description": self.description[:500],
            "timestamp": self.timestamp,
            "namespace": self.namespace,
            "service": self.service,
        }


class DetectionAgent:
    """
    Continuous anomaly detection agent.
    Polls multiple signal sources and produces DetectionSignals.
    """

    def __init__(self, llm: Optional[LLMClient], registry: ToolRegistry, config=None):
        self.llm = llm
        self.registry = registry
        from configs.config_loader import get_config
        cfg = config or get_config()
        det = cfg.detection
        self.sources_enabled = det.sources_enabled
        self.metric_checks = det.metric_checks or self._default_metric_checks()
        self.critical_event_reasons = set(det.critical_event_reasons)
        self.critical_pod_reasons = set(det.critical_pod_reasons)
        self._detection_cfg = {
            "default_detect_methods": getattr(det, "default_detect_methods", ["threshold", "zscore"]),
            "default_lookback_m": getattr(det, "default_lookback_m", 30),
            "default_z_threshold": getattr(det, "default_z_threshold", 3.0),
            "default_ewma_span": getattr(det, "default_ewma_span", 10),
            "categories_enabled": getattr(det, "categories_enabled", {}),
            "business_services": getattr(det, "business_services", []),
            "db_services": getattr(det, "db_services", []),
            "thresholds": getattr(det, "thresholds", {}),
            "namespace": getattr(cfg.kubernetes, "namespace", ""),
        }

    def detect(self, namespace: str = "") -> List[DetectionSignal]:
        """Run one detection cycle across all signal sources."""
        signals = []

        checks = []
        if self.sources_enabled.get("prometheus"):
            checks.append(("prometheus", self._check_prometheus_alerts, 4))
        if self.sources_enabled.get("k8s_event"):
            checks.append(("k8s_event", lambda: self._check_k8s_events(namespace), 6))
        if self.sources_enabled.get("pod_health"):
            checks.append(("pod_health", lambda: self._check_pod_health(namespace), 6))
        if self.sources_enabled.get("node_health"):
            checks.append(("node_health", self._check_node_health, 6))
        if self.sources_enabled.get("metric_anomaly"):
            checks.append(("metric_anomaly", lambda: self._check_metric_anomalies(namespace), 4))

        signals.extend(self._run_source_checks(checks))

        logger.info(f"Detection cycle: found {len(signals)} signals")
        return signals

    def _run_source_checks(self, checks) -> List[DetectionSignal]:
        """Run all enabled signal sources concurrently under a bounded wall-clock budget."""
        if not checks:
            return []
        results: List[DetectionSignal] = []
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(checks))
        future_meta = {
            executor.submit(fn): (name, timeout_s)
            for name, fn, timeout_s in checks
        }
        global_timeout = max(timeout_s for _, _, timeout_s in checks)
        try:
            done, pending = concurrent.futures.wait(
                future_meta,
                timeout=global_timeout,
                return_when=concurrent.futures.ALL_COMPLETED,
            )
            for future in done:
                name, _ = future_meta[future]
                try:
                    result = future.result(timeout=0)
                    if isinstance(result, list):
                        results.extend(result)
                except Exception as exc:
                    logger.warning("Detection source %s failed: %s", name, exc, exc_info=True)
            for future in pending:
                name, timeout_s = future_meta[future]
                logger.warning("Detection source %s timed out after %ss", name, timeout_s)
                future.cancel()
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return results

    def _run_source_check(self, name: str, fn, timeout_s: int) -> List[DetectionSignal]:
        """Run one signal source with a hard timeout so a bad backend does not block the page."""
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(fn)
        try:
            result = future.result(timeout=timeout_s)
            return result if isinstance(result, list) else []
        except concurrent.futures.TimeoutError:
            logger.warning("Detection source %s timed out after %ss", name, timeout_s)
            future.cancel()
            return []
        except Exception as exc:
            logger.warning("Detection source %s failed: %s", name, exc, exc_info=True)
            return []
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _check_prometheus_alerts(self) -> List[DetectionSignal]:
        """Check for firing Prometheus alerts."""
        signals = []
        prom = self.registry.get("prometheus")
        if prom is None:
            return signals
        
        result = prom.execute(query="ALERTS{alertstate='firing'}")
        if not result.success:
            return signals
        
        for item in (result.data or {}).get("results", []):
            metric = item.get("metric", {})
            signals.append(DetectionSignal(
                signal_id="",
                source="prometheus",
                severity=metric.get("severity", "warning"),
                title=f"Alert: {metric.get('alertname', 'unknown')}",
                description=metric.get("description", metric.get("summary", str(metric))),
                namespace=metric.get("namespace", ""),
                service=metric.get("pod", metric.get("service", metric.get("instance", ""))),
                raw_data=item,
            ))
        
        return signals

    def _check_k8s_events(self, namespace: str = "") -> List[DetectionSignal]:
        """Check for K8s Warning events."""
        signals = []
        kubectl = self.registry.get("kubectl")
        if kubectl is None:
            return signals
        
        cmd = "get events --field-selector type=Warning --sort-by='.lastTimestamp' -o json"
        if namespace:
            cmd += f" -n {namespace}"
        else:
            cmd += " --all-namespaces"
        
        result = kubectl.execute(command=cmd, timeout=15)
        if not result.success:
            return signals
        
        import json
        try:
            data = json.loads(result.data) if isinstance(result.data, str) else result.data
            events = (data or {}).get("items", [])
            for event in events[-20:]:
                obj = event.get("involvedObject", {})
                msg = event.get("message", "")
                reason = event.get("reason", "")
                
                # Determine severity
                severity = "warning"
                if reason in self.critical_event_reasons:
                    severity = "critical"
                
                signals.append(DetectionSignal(
                    signal_id="",
                    source="k8s_event",
                    severity=severity,
                    title=f"{reason}: {obj.get('kind', '')}/{obj.get('name', '')}",
                    description=msg[:500],
                    namespace=obj.get("namespace", event.get("metadata", {}).get("namespace", "")),
                    service=obj.get("name", ""),
                    raw_data=event,
                ))
        except (json.JSONDecodeError, TypeError):
            pass
        
        return signals

    def _check_pod_health(self, namespace: str = "") -> List[DetectionSignal]:
        """Check for unhealthy pods."""
        signals = []
        k8s_health = self.registry.get("k8s_health")
        if k8s_health is None:
            return signals
        
        result = k8s_health.execute(component="pods")
        if not result.success:
            return signals
        
        for pod in (result.data or {}).get("checks", {}).get("pods", {}).get("problem_pods", []):
            reason = pod.get("reason", pod.get("phase", "Unknown"))
            severity = "critical" if reason in self.critical_pod_reasons else "warning"
            
            signals.append(DetectionSignal(
                signal_id="",
                source="pod_health",
                severity=severity,
                title=f"Unhealthy Pod: {pod.get('name', '')} ({reason})",
                description=f"Pod {pod['name']} in namespace {pod.get('namespace', '')} is {reason}. "
                           f"Restarts: {pod.get('restart_count', 'N/A')}",
                namespace=pod.get("namespace", ""),
                service=pod.get("name", ""),
            ))
        
        return signals

    # ── Metric anomaly thresholds ──────────────────────────────────────────
    @staticmethod
    def _default_metric_checks():
        """Fallback metric checks when none provided in config."""
        return [
            {
                "name": "node_cpu_usage",
                "query": 'avg by(instance)(1 - rate(node_cpu_seconds_total{mode="idle"}[1m])) * 100',
                "unit": "%", "label_key": "instance", "level": "node",
                "warn": 85, "crit": 95,
            },
            {
                "name": "node_memory_usage",
                "query": '(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100',
                "unit": "%", "label_key": "instance", "level": "node",
                "warn": 85, "crit": 95,
            },
            {
                "name": "node_disk_usage",
                "query": '(1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}) * 100',
                "unit": "%", "label_key": "instance", "level": "node",
                "warn": 85, "crit": 95,
            },
            {
                "name": "container_cpu",
                "query": 'sum(rate(container_cpu_usage_seconds_total{container!=""}[1m])) by (pod, namespace) '
                         '/ sum(kube_pod_container_resource_limits{resource="cpu"}) by (pod, namespace) * 100',
                "unit": "%", "label_key": "pod", "ns_key": "namespace", "level": "container",
                "warn": 80, "crit": 95,
            },
            {
                "name": "container_memory",
                "query": 'sum(container_memory_working_set_bytes{container!=""}) by (pod, namespace) '
                         '/ sum(kube_pod_container_resource_limits{resource="memory"}) by (pod, namespace) * 100',
                "unit": "%", "label_key": "pod", "ns_key": "namespace", "level": "container",
                "warn": 80, "crit": 95,
            },
        ]

    def _check_metric_anomalies(self, namespace: str = "") -> List[DetectionSignal]:
        """Check key resource metrics via multi-algorithm detection engine."""
        prom = self.registry.get("prometheus")
        if prom is None:
            return []

        from agents.metric_anomaly_detector import MetricAnomalyDetector
        detector = MetricAnomalyDetector(
            prom_tool=prom,
            metric_checks=self.metric_checks,
            detection_cfg=self._detection_cfg,
        )
        return detector.detect(namespace)

    def _check_node_health(self) -> List[DetectionSignal]:
        """Check for unhealthy nodes."""
        signals = []
        k8s_health = self.registry.get("k8s_health")
        if k8s_health is None:
            return signals
        
        result = k8s_health.execute(component="nodes")
        if not result.success:
            return signals
        
        nodes = (result.data or {}).get("checks", {}).get("nodes", {})
        for node in nodes.get("nodes", []):
            if node.get("ready") != "True":
                signals.append(DetectionSignal(
                    signal_id="",
                    source="node_health",
                    severity="critical",
                    title=f"Node NotReady: {node.get('name', '')}",
                    description=f"Node {node['name']} is not ready. Conditions: {node.get('conditions', {})}",
                    service=node.get("name", ""),
                ))
        
        return signals
