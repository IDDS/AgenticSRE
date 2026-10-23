"""
MetricAnomalyDetector — Dual-track metric anomaly detection engine.

Track 1: User-configured metric_checks with multi-algorithm ensemble
  - threshold, zscore, ewma, spectral_residual, pearson_onset, rate_change

Track 2: Built-in 4-category detection (ported from agenticSnail)
  - Category 1: Infrastructure (node-level)
  - Category 2: Application (container/pod-level)
  - Category 3: Business Workloads (service-level)
  - Category 4: Database (database pod-level)
  - K8s Workload Health (pod restarts, deployment availability)

Each category can be independently enabled/disabled via categories_enabled.
"""

import math
import re
import time
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Avoid circular import — DetectionSignal imported lazily in detect()
SUPPORTED_METHODS = frozenset([
    "threshold", "zscore", "ewma",
    "spectral_residual", "pearson_onset", "rate_change",
])

# ── Module-level utility functions ────────────────────────────────────

# Default thresholds for built-in category checks (agenticSnail defaults)
DEFAULT_THRESHOLDS = {
    # Category 1: Infrastructure
    "node_cpu_warn": 0.85, "node_cpu_crit": 0.95,
    "node_mem_warn": 0.85, "node_mem_crit": 0.95,
    "node_load_warn": 1.5, "node_load_crit": 2.5,
    "node_disk_warn": 0.85, "node_disk_crit": 0.95,
    "node_io_warn": 0.10, "node_io_crit": 0.40,
    "node_net_err_warn": 1.0, "node_net_err_crit": 5.0,
    # Category 2: Application
    "cfs_throttle_warn": 0.25, "cfs_throttle_crit": 0.50,
    "container_mem_warn": 0.80, "container_mem_crit": 0.90,
    # Category 3: Business
    "biz_mem_warn": 0.80, "biz_mem_crit": 0.90,
    "nginx_latency_warn": 0.70, "nginx_latency_crit": 0.90,
    # Category 4: Database
    "db_mem_warn": 0.80, "db_mem_crit": 0.90,
    # K8s Workload
    "pod_restart_warn": 2, "pod_restart_crit": 5,
}


def _threshold_sev(val: float, warn: float, crit: float) -> Optional[str]:
    """Return 'critical'/'warning'/None based on static thresholds."""
    if val >= crit:
        return "critical"
    if val >= warn:
        return "warning"
    return None


def _instance_label(metric: Dict) -> str:
    """Extract node name from instance label, stripping :port suffix."""
    inst = metric.get("instance", "unknown")
    return re.sub(r":\d+$", "", inst)


def _pod_to_service(pod_name: str) -> str:
    """Convert pod name to service name by stripping ReplicaSet/StatefulSet hash."""
    # e.g. nginx-thrift-7b9f8c6d4f-x2k9p → nginx-thrift
    # e.g. redis-master-0 → redis-master
    parts = pod_name.rsplit("-", 2)
    if len(parts) >= 3 and len(parts[-1]) >= 4 and len(parts[-2]) >= 4:
        return "-".join(parts[:-2])
    parts2 = pod_name.rsplit("-", 1)
    if len(parts2) == 2 and len(parts2[-1]) >= 4:
        return parts2[0]
    return pod_name


class MetricAnomalyDetector:
    """
    Dual-track metric anomaly detection engine.

    Track 1: User-configured metric_checks (multi-algorithm ensemble).
    Track 2: Built-in 4-category detection (infrastructure / application /
             business / database / k8s_workload).
    """

    def __init__(self, prom_tool, metric_checks: List[Dict],
                 detection_cfg: Dict[str, Any]):
        self.prom = prom_tool
        self.checks = metric_checks or []
        self.default_methods: List[str] = detection_cfg.get(
            "default_detect_methods", ["threshold", "zscore"]
        )
        self.lookback_m: int = detection_cfg.get("default_lookback_m", 30)
        self.z_threshold: float = detection_cfg.get("default_z_threshold", 3.0)
        self.ewma_span: int = detection_cfg.get("default_ewma_span", 10)

        # Category detection config
        self.categories_enabled: Dict[str, bool] = detection_cfg.get(
            "categories_enabled", {
                "infrastructure": True, "application": True,
                "business": True, "database": True, "k8s_workload": True,
            }
        )
        self.namespace: str = detection_cfg.get("namespace", "")
        self.business_services: List[str] = detection_cfg.get(
            "business_services", []
        )
        self.db_services: List[str] = detection_cfg.get("db_services", [])
        # Merge user overrides on top of defaults
        self.thresholds: Dict[str, float] = dict(DEFAULT_THRESHOLDS)
        self.thresholds.update(detection_cfg.get("thresholds", {}))

    # ── public API ────────────────────────────────────────────────────

    def detect(self, namespace: str = ""):
        """Run both tracks: user metric_checks + built-in category detection."""
        from agents.detection_agent import DetectionSignal
        signals: List[DetectionSignal] = []
        ns = namespace or self.namespace

        # Track 1: User-configured metric_checks (unchanged)
        for check in self.checks:
            methods = check.get("detect_methods") or self.default_methods
            methods = [m for m in methods if m in SUPPORTED_METHODS]
            if not methods:
                methods = ["threshold"]
            try:
                raw = self._run_check(check, methods, ns)
                signals.extend(raw)
            except Exception as exc:
                logger.debug("[MetricAnomalyDetector] %s failed: %s",
                             check.get("name", "?"), exc)

        # Track 2: Built-in category detection
        category_handlers = {
            "infrastructure": self._check_infrastructure,
            "application": self._check_application,
            "business": self._check_business,
            "database": self._check_database,
            "k8s_workload": self._check_k8s_workload,
        }
        for cat, handler in category_handlers.items():
            if not self.categories_enabled.get(cat, True):
                continue
            try:
                cat_signals = handler(ns)
                signals.extend(cat_signals)
            except Exception as exc:
                logger.debug("[MetricAnomalyDetector] category %s failed: %s",
                             cat, exc)

        return self._deduplicate(signals)

    # ═════════════════════════════════════════════════════════════════
    # Track 2: Built-in Category Detection
    # ═════════════════════════════════════════════════════════════════

    # ── Category 1: Infrastructure (node-level) ───────────────────

    def _check_infrastructure(self, ns: str) -> list:
        signals = []

        # NodeCPUHigh — static threshold
        signals.extend(self._threshold_check(
            "NodeCPUHigh",
            '1 - avg by(instance)(rate(node_cpu_seconds_total{mode="idle"}[5m]))',
            self.thresholds["node_cpu_warn"], self.thresholds["node_cpu_crit"],
            level="node", unit="", label_key="instance", ns=ns,
        ))

        # NodeCPUSpikeZScore — z-score
        signals.extend(self._zscore_check(
            "NodeCPUSpikeZScore",
            '1 - avg by(instance)(rate(node_cpu_seconds_total{mode="idle"}[5m]))',
            level="node", label_key="instance", ns=ns,
        ))

        # NodeMemoryHigh — static threshold
        signals.extend(self._threshold_check(
            "NodeMemoryHigh",
            "1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes",
            self.thresholds["node_mem_warn"], self.thresholds["node_mem_crit"],
            level="node", unit="", label_key="instance", ns=ns,
        ))

        # NodeLoadHigh — static threshold (load per CPU)
        signals.extend(self._threshold_check(
            "NodeLoadHigh",
            'node_load1 / count without(cpu, mode)(node_cpu_seconds_total{mode="idle"})',
            self.thresholds["node_load_warn"], self.thresholds["node_load_crit"],
            level="node", unit="", label_key="instance", ns=ns,
        ))

        # NodeDiskFull — static threshold
        signals.extend(self._threshold_check(
            "NodeDiskFull",
            '1 - node_filesystem_avail_bytes{fstype!~"tmpfs|overlay"} '
            '/ node_filesystem_size_bytes{fstype!~"tmpfs|overlay"}',
            self.thresholds["node_disk_warn"], self.thresholds["node_disk_crit"],
            level="node", unit="", label_key="instance", ns=ns,
        ))

        # NodeIOPressure — static threshold
        signals.extend(self._threshold_check(
            "NodeIOPressure",
            "rate(node_pressure_io_stalled_seconds_total[5m])",
            self.thresholds["node_io_warn"], self.thresholds["node_io_crit"],
            level="node", unit="s/s", label_key="instance", ns=ns,
        ))

        # NodeNetworkErrors — floor threshold (>1/s is anomalous)
        signals.extend(self._threshold_check(
            "NodeNetworkErrors",
            "rate(node_network_receive_errs_total[5m]) + rate(node_network_transmit_errs_total[5m])",
            self.thresholds["node_net_err_warn"], self.thresholds["node_net_err_crit"],
            level="node", unit="err/s", label_key="instance", ns=ns,
        ))

        return signals

    # ── Category 2: Application (container/pod-level) ─────────────

    def _check_application(self, ns: str) -> list:
        signals = []
        ns_filter = '{namespace="%s"}' % ns if ns else ""

        # ContainerCPUThrottle — static threshold
        signals.extend(self._threshold_check(
            "ContainerCPUThrottle",
            f'sum by(pod, namespace)(rate(container_cpu_cfs_throttled_periods_total{ns_filter}[5m])) '
            f'/ sum by(pod, namespace)(rate(container_cpu_cfs_periods_total{ns_filter}[5m]))',
            self.thresholds["cfs_throttle_warn"], self.thresholds["cfs_throttle_crit"],
            level="container", unit="", label_key="pod", ns_key="namespace", ns=ns,
        ))

        # ContainerCPUSpike — z-score
        signals.extend(self._zscore_check(
            "ContainerCPUSpike",
            f'sum by(pod, namespace)(rate(container_cpu_usage_seconds_total{{container!=""{ns_filter and ","+ns_filter[1:]}}}[1m]))',
            level="container", label_key="pod", ns_key="namespace", ns=ns,
        ))

        # ContainerMemoryHigh — static threshold
        signals.extend(self._threshold_check(
            "ContainerMemoryHigh",
            f'sum by(pod, namespace)(container_memory_working_set_bytes{{container!=""{ns_filter and ","+ns_filter[1:]}}}) '
            f'/ sum by(pod, namespace)(kube_pod_container_resource_limits{{resource="memory"{ns_filter and ","+ns_filter[1:]}}})',
            self.thresholds["container_mem_warn"], self.thresholds["container_mem_crit"],
            level="container", unit="", label_key="pod", ns_key="namespace", ns=ns,
        ))

        # ContainerOOM — discrete event (increase > 0)
        signals.extend(self._threshold_check(
            "ContainerOOM",
            f"increase(container_oom_events_total{ns_filter}[5m])",
            warn=0.5, crit=1.0,
            level="container", unit="events", label_key="pod", ns_key="namespace", ns=ns,
        ))

        return signals

    # ── Category 3: Business Workloads ────────────────────────────

    def _check_business(self, ns: str) -> list:
        signals = []
        if not self.business_services:
            return signals

        # Build pod name regex for business services
        svc_regex = "|".join(re.escape(s) for s in self.business_services)
        ns_filter = f',namespace="{ns}"' if ns else ""

        # BizServiceCPUSpike — z-score on business pod CPU
        signals.extend(self._zscore_check(
            "BizServiceCPUSpike",
            f'sum by(pod, namespace)(rate(container_cpu_usage_seconds_total'
            f'{{pod=~"({svc_regex}).*",container!=""{ns_filter}}}[1m]))',
            level="service", label_key="pod", ns_key="namespace", ns=ns,
        ))

        # BizServiceMemHigh — static threshold on business pod memory/limit
        signals.extend(self._threshold_check(
            "BizServiceMemHigh",
            f'sum by(pod, namespace)(container_memory_working_set_bytes'
            f'{{pod=~"({svc_regex}).*",container!=""{ns_filter}}}) / '
            f'sum by(pod, namespace)(kube_pod_container_resource_limits'
            f'{{resource="memory",pod=~"({svc_regex}).*"{ns_filter}}})',
            self.thresholds["biz_mem_warn"], self.thresholds["biz_mem_crit"],
            level="service", unit="", label_key="pod", ns_key="namespace", ns=ns,
        ))

        # NginxQPSProxy — z-score on nginx-thrift CPU (QPS proxy)
        if any("nginx-thrift" in s for s in self.business_services):
            signals.extend(self._zscore_check(
                "NginxQPSProxy",
                f'sum by(pod, namespace)(rate(container_cpu_usage_seconds_total'
                f'{{pod=~"nginx-thrift.*",container!=""{ns_filter}}}[1m]))',
                level="service", label_key="pod", ns_key="namespace", ns=ns,
                bidirectional=True,
            ))

            # NginxLatencyProxy — static threshold on nginx-thrift CPU/limit
            signals.extend(self._threshold_check(
                "NginxLatencyProxy",
                f'sum by(pod, namespace)(rate(container_cpu_usage_seconds_total'
                f'{{pod=~"nginx-thrift.*",container!=""{ns_filter}}}[1m])) / '
                f'sum by(pod, namespace)(kube_pod_container_resource_limits'
                f'{{resource="cpu",pod=~"nginx-thrift.*"{ns_filter}}})',
                self.thresholds["nginx_latency_warn"], self.thresholds["nginx_latency_crit"],
                level="service", unit="", label_key="pod", ns_key="namespace", ns=ns,
            ))

        # DNSRPCRate — z-score on coredns request rate (bidirectional)
        signals.extend(self._zscore_check(
            "DNSRPCRate",
            "sum(rate(coredns_dns_requests_total[2m]))",
            level="cluster", label_key="instance", ns=ns,
            bidirectional=True,
        ))

        # TCPConnectionPressure — z-score on TCP socket usage
        signals.extend(self._zscore_check(
            "TCPConnectionPressure",
            "node_sockstat_TCP_inuse",
            level="node", label_key="instance", ns=ns,
        ))

        return signals

    # ── Category 4: Database ──────────────────────────────────────

    def _check_database(self, ns: str) -> list:
        signals = []
        if not self.db_services:
            return signals

        db_regex = "|".join(re.escape(s) for s in self.db_services)
        ns_filter = f',namespace="{ns}"' if ns else ""

        # DBMemoryPressure — static threshold
        signals.extend(self._threshold_check(
            "DBMemoryPressure",
            f'sum by(pod, namespace)(container_memory_working_set_bytes'
            f'{{pod=~"({db_regex}).*",container!=""{ns_filter}}}) / '
            f'sum by(pod, namespace)(kube_pod_container_resource_limits'
            f'{{resource="memory",pod=~"({db_regex}).*"{ns_filter}}})',
            self.thresholds["db_mem_warn"], self.thresholds["db_mem_crit"],
            level="database", unit="", label_key="pod", ns_key="namespace", ns=ns,
        ))

        # DBCPUSpike — z-score
        signals.extend(self._zscore_check(
            "DBCPUSpike",
            f'sum by(pod, namespace)(rate(container_cpu_usage_seconds_total'
            f'{{pod=~"({db_regex}).*",container!=""{ns_filter}}}[1m]))',
            level="database", label_key="pod", ns_key="namespace", ns=ns,
        ))

        return signals

    # ── K8s Workload Health ───────────────────────────────────────

    def _check_k8s_workload(self, ns: str) -> list:
        signals = []
        ns_filter = '{namespace="%s"}' % ns if ns else ""

        # PodRestartSpike — static threshold on restart increase
        signals.extend(self._threshold_check(
            "PodRestartSpike",
            f"increase(kube_pod_container_status_restarts_total{ns_filter}[5m])",
            self.thresholds["pod_restart_warn"], self.thresholds["pod_restart_crit"],
            level="pod", unit="restarts", label_key="pod", ns_key="namespace", ns=ns,
        ))

        # DeploymentUnavailable — discrete event (unavailable > 0)
        signals.extend(self._threshold_check(
            "DeploymentUnavailable",
            f"kube_deployment_status_replicas_unavailable{ns_filter}",
            warn=0.5, crit=1.0,
            level="deployment", unit="replicas", label_key="deployment",
            ns_key="namespace", ns=ns,
        ))

        return signals

    # ═════════════════════════════════════════════════════════════════
    # Generic detection methods for category checks
    # ═════════════════════════════════════════════════════════════════

    def _threshold_check(self, check_name: str, query: str,
                         warn: float, crit: float, *,
                         level: str = "node", unit: str = "",
                         label_key: str = "instance",
                         ns_key: str = "", ns: str = "") -> list:
        """Run a static threshold check via instant query."""
        signals = []
        result = self._instant_query(query)
        if not result:
            return signals

        for item in result:
            metric = item.get("metric", {})
            value_raw = item.get("value")
            if value_raw is None:
                continue
            try:
                value = float(value_raw[1]) if isinstance(value_raw, (list, tuple)) else float(value_raw)
            except (TypeError, ValueError, IndexError):
                continue

            if math.isnan(value) or math.isinf(value):
                continue

            sev = _threshold_sev(value, warn, crit)
            if sev is None:
                continue

            label = metric.get(label_key, "unknown")
            if label_key == "instance":
                label = _instance_label(metric)
            actual_ns = metric.get(ns_key, ns) if ns_key else ns

            signals.append(self._make_signal(
                check_name=check_name, severity=sev,
                label=label, value=value, unit=unit,
                level=level, namespace=actual_ns,
                detail=f"[threshold] {level.capitalize()} {check_name} for {label}: "
                       f"{value:.3f}{unit} (warn>{warn}, crit>{crit})",
            ))
        return signals

    def _zscore_check(self, check_name: str, query: str, *,
                      level: str = "node", label_key: str = "instance",
                      ns_key: str = "", ns: str = "",
                      z_thresh: float = 0, floor: float = 0,
                      bidirectional: bool = False) -> list:
        """Run a Z-score check via range query (30-min lookback)."""
        signals = []
        z_thresh = z_thresh or self.z_threshold

        series_list = self._range_query(query, self.lookback_m)
        if not series_list:
            return signals

        for series in series_list:
            values, timestamps, metric_labels = self._parse_series(series)
            n = len(values)
            if n < 6:
                continue

            label = metric_labels.get(label_key, "unknown")
            if label_key == "instance":
                label = _instance_label(metric_labels)
            actual_ns = metric_labels.get(ns_key, ns) if ns_key else ns

            # baseline = all but last 3, current = last 3
            baseline = values[:-3]
            current = values[-3:]
            mean_bl = sum(baseline) / len(baseline)
            std_bl = math.sqrt(sum((v - mean_bl) ** 2 for v in baseline) / len(baseline))

            if std_bl < 1e-9:
                continue

            mean_cur = sum(current) / len(current)

            if bidirectional:
                z = abs(mean_cur - mean_bl) / std_bl
            else:
                z = (mean_cur - mean_bl) / std_bl

            # Apply floor filter: ignore if absolute value is below floor
            if floor > 0 and abs(mean_cur) < floor:
                continue

            if z <= z_thresh:
                continue

            sev = "critical" if z > z_thresh * 1.5 else "warning"
            direction = ""
            if bidirectional:
                direction = " (spike)" if mean_cur > mean_bl else " (drop)"

            signals.append(self._make_signal(
                check_name=check_name, severity=sev,
                label=label, value=mean_cur, unit="",
                level=level, namespace=actual_ns,
                detail=f"[zscore] {check_name} for {label}: Z={z:.2f}{direction} "
                       f"(threshold {z_thresh}). "
                       f"Baseline mean={mean_bl:.4f}, current mean={mean_cur:.4f}",
            ))
        return signals

    def _make_signal(self, *, check_name: str, severity: str,
                     label: str, value: float, unit: str,
                     level: str, namespace: str, detail: str):
        """Construct a DetectionSignal for a category check."""
        from agents.detection_agent import DetectionSignal
        service = label
        if label and not any(c in label for c in (":", "/")):
            service = _pod_to_service(label)

        return DetectionSignal(
            signal_id="",
            source="metric_anomaly",
            severity=severity,
            title=f"{check_name}: {label} = {value:.3f}{unit}",
            description=detail[:500],
            namespace=namespace,
            service=service,
        )

    # ═════════════════════════════════════════════════════════════════
    # Track 1: User-configured metric_checks (unchanged)
    # ═════════════════════════════════════════════════════════════════

    # ── per-check orchestrator ────────────────────────────────────────

    def _run_check(self, check: Dict, methods: List[str],
                   namespace: str):
        from agents.detection_agent import DetectionSignal
        signals: List[DetectionSignal] = []

        needs_range = any(m != "threshold" for m in methods)

        # 1. Threshold uses instant query
        if "threshold" in methods:
            signals.extend(self._detect_threshold(check, namespace))

        # 2. Other algorithms share the same range-query data
        if needs_range:
            series_list = self._fetch_range(check)
            if not series_list:
                return signals
            for series in series_list:
                values, timestamps, metric_labels = self._parse_series(series)
                if len(values) < 6:
                    continue
                label = metric_labels.get(check.get("label_key", "instance"), "unknown")
                ns = metric_labels.get(check.get("ns_key", ""), namespace)
                for method in methods:
                    if method == "threshold":
                        continue
                    sig = self._apply_method(
                        method, check, values, timestamps,
                        label, ns,
                    )
                    if sig:
                        signals.append(sig)
        return signals

    # ── algorithm router ──────────────────────────────────────────────

    def _apply_method(self, method: str, check: Dict,
                      values: List[float], timestamps: List[float],
                      label: str, namespace: str):
        dispatch = {
            "zscore": self._detect_zscore,
            "ewma": self._detect_ewma,
            "spectral_residual": self._detect_spectral_residual,
            "pearson_onset": self._detect_pearson_onset,
            "rate_change": self._detect_rate_change,
        }
        fn = dispatch.get(method)
        if fn is None:
            return None
        return fn(check, values, timestamps, label, namespace)

    # ── 1. Static Threshold ───────────────────────────────────────────

    def _detect_threshold(self, check: Dict, namespace: str):
        from agents.detection_agent import DetectionSignal
        signals = []
        result = self._fetch_instant(check)
        if not result:
            return signals

        for item in result:
            metric = item.get("metric", {})
            value_raw = item.get("value")
            if value_raw is None:
                continue
            try:
                value = float(value_raw[1]) if isinstance(value_raw, (list, tuple)) else float(value_raw)
            except (TypeError, ValueError, IndexError):
                continue

            warn = check.get("warn", 85)
            crit = check.get("crit", 95)
            if value < warn:
                continue

            severity = "critical" if value >= crit else "warning"
            label = metric.get(check.get("label_key", "instance"), "unknown")
            ns = metric.get(check.get("ns_key", ""), namespace)

            signals.append(DetectionSignal(
                signal_id="",
                source="metric_anomaly",
                severity=severity,
                title=f"{check['name']}: {label} at {value:.1f}{check.get('unit', '%')}",
                description=(
                    f"[threshold] {check.get('level', 'node').capitalize()} "
                    f"{check['name']} for {label} is {value:.1f}{check.get('unit', '%')} "
                    f"(warn>{warn}, crit>{crit})"
                ),
                namespace=ns,
                service=label,
            ))
        return signals

    # ── 2. Z-score ────────────────────────────────────────────────────

    def _detect_zscore(self, check: Dict, values: List[float],
                       timestamps: List[float], label: str,
                       namespace: str):
        from agents.detection_agent import DetectionSignal
        n = len(values)
        if n < 6:
            return None
        # baseline = all but last 3, current = last 3
        baseline = values[:-3]
        current = values[-3:]
        mean_bl = sum(baseline) / len(baseline)
        std_bl = math.sqrt(sum((v - mean_bl) ** 2 for v in baseline) / len(baseline))
        if std_bl < 1e-9:
            return None
        mean_cur = sum(current) / len(current)
        z = abs(mean_cur - mean_bl) / std_bl
        if z <= self.z_threshold:
            return None

        severity = "critical" if z > self.z_threshold * 1.5 else "warning"
        return DetectionSignal(
            signal_id="",
            source="metric_anomaly",
            severity=severity,
            title=f"{check['name']}: {label} Z-score={z:.2f}",
            description=(
                f"[zscore] {check['name']} for {label}: Z-score {z:.2f} "
                f"(threshold {self.z_threshold}). "
                f"Baseline mean={mean_bl:.2f}, current mean={mean_cur:.2f}"
            ),
            namespace=namespace,
            service=label,
        )

    # ── 3. EWMA ──────────────────────────────────────────────────────

    def _detect_ewma(self, check: Dict, values: List[float],
                     timestamps: List[float], label: str,
                     namespace: str):
        from agents.detection_agent import DetectionSignal
        n = len(values)
        if n < 6:
            return None

        alpha = 2.0 / (self.ewma_span + 1)
        ewma = [values[0]]
        for i in range(1, n):
            ewma.append(alpha * values[i] + (1 - alpha) * ewma[-1])

        # Residuals
        residuals = [values[i] - ewma[i] for i in range(n)]
        mean_r = sum(residuals) / n
        std_r = math.sqrt(sum((r - mean_r) ** 2 for r in residuals) / n)
        if std_r < 1e-9:
            return None

        # Check latest value deviation from EWMA
        latest_residual = values[-1] - ewma[-1]
        z = abs(latest_residual - mean_r) / std_r
        if z <= self.z_threshold:
            return None

        severity = "critical" if z > self.z_threshold * 1.5 else "warning"
        return DetectionSignal(
            signal_id="",
            source="metric_anomaly",
            severity=severity,
            title=f"{check['name']}: {label} EWMA deviation={z:.2f}\u03c3",
            description=(
                f"[ewma] {check['name']} for {label}: current={values[-1]:.2f}, "
                f"EWMA baseline={ewma[-1]:.2f}, deviation={z:.2f}\u03c3 "
                f"(span={self.ewma_span})"
            ),
            namespace=namespace,
            service=label,
        )

    # ── 4. Spectral Residual ─────────────────────────────────────────

    def _detect_spectral_residual(self, check: Dict,
                                  values: List[float],
                                  timestamps: List[float],
                                  label: str, namespace: str):
        from agents.detection_agent import DetectionSignal
        n = len(values)
        if n < 10:
            return None

        # Log magnitude
        log_vals = [math.log(abs(v) + 1e-9) for v in values]

        # Smooth via moving average
        window = max(3, n // 10)
        smoothed = []
        for i in range(n):
            s = max(0, i - window // 2)
            e = min(n, s + window)
            smoothed.append(sum(log_vals[s:e]) / (e - s))

        # Spectral residuals
        residuals = [abs(log_vals[i] - smoothed[i]) for i in range(n)]

        # Z-score on residuals for last 3 points
        if len(residuals) < 6:
            return None
        baseline_r = residuals[:-3]
        current_r = residuals[-3:]
        mean_r = sum(baseline_r) / len(baseline_r)
        std_r = math.sqrt(sum((r - mean_r) ** 2 for r in baseline_r) / len(baseline_r))
        if std_r < 1e-9:
            return None

        mean_cur = sum(current_r) / len(current_r)
        z = (mean_cur - mean_r) / std_r  # one-sided: higher residual = more anomalous
        if z <= self.z_threshold:
            return None

        severity = "critical" if z > self.z_threshold * 1.5 else "warning"
        return DetectionSignal(
            signal_id="",
            source="metric_anomaly",
            severity=severity,
            title=f"{check['name']}: {label} SR anomaly score={z:.2f}",
            description=(
                f"[spectral_residual] {check['name']} for {label}: "
                f"spectral residual Z-score={z:.2f} (threshold {self.z_threshold}). "
                f"Indicates periodic pattern deviation."
            ),
            namespace=namespace,
            service=label,
        )

    # ── 5. Pearson Onset ─────────────────────────────────────────────

    def _detect_pearson_onset(self, check: Dict, values: List[float],
                              timestamps: List[float], label: str,
                              namespace: str):
        from agents.detection_agent import DetectionSignal
        from tools.hero_analysis import HeroMetricAnalyzer

        result = HeroMetricAnalyzer.pearson_onset_detection(
            values, window_size=min(10, len(values) // 4),
        )
        onset_points = result.get("onset_points", [])
        if not onset_points:
            return None

        # Use the latest onset point
        latest = onset_points[-1]
        shift_pct = latest["mean_shift"] * 100
        direction = latest["direction"]
        severity = "critical" if shift_pct > 60 else "warning"

        return DetectionSignal(
            signal_id="",
            source="metric_anomaly",
            severity=severity,
            title=f"{check['name']}: {label} step-change ({direction} {shift_pct:.0f}%)",
            description=(
                f"[pearson_onset] {check['name']} for {label}: "
                f"Pearson r={latest['pearson_corr']:.3f}, "
                f"mean shift={shift_pct:.1f}% ({direction}). "
                f"Step-change detected at sample index {latest['index']}."
            ),
            namespace=namespace,
            service=label,
        )

    # ── 6. Rate Change ───────────────────────────────────────────────

    def _detect_rate_change(self, check: Dict, values: List[float],
                            timestamps: List[float], label: str,
                            namespace: str):
        from agents.detection_agent import DetectionSignal
        n = len(values)
        if n < 6:
            return None

        mid = n // 2
        baseline = values[:mid]
        recent = values[mid:]
        mean_bl = sum(baseline) / len(baseline)
        mean_rc = sum(recent) / len(recent)

        if abs(mean_bl) < 1e-9:
            return None

        rate = (mean_rc - mean_bl) / abs(mean_bl)
        rate_threshold = 0.45  # 45%

        if abs(rate) <= rate_threshold:
            return None

        direction = "increase" if rate > 0 else "decrease"
        severity = "critical" if abs(rate) > 0.8 else "warning"

        return DetectionSignal(
            signal_id="",
            source="metric_anomaly",
            severity=severity,
            title=f"{check['name']}: {label} rate change {rate*100:.0f}%",
            description=(
                f"[rate_change] {check['name']} for {label}: "
                f"sustained {direction} of {abs(rate)*100:.1f}% "
                f"(baseline mean={mean_bl:.2f}, recent mean={mean_rc:.2f}). "
                f"Threshold: \u00b145%."
            ),
            namespace=namespace,
            service=label,
        )

    # ═════════════════════════════════════════════════════════════════
    # Prometheus helpers
    # ═════════════════════════════════════════════════════════════════

    def _instant_query(self, query: str) -> Optional[List[Dict]]:
        """Instant query via PrometheusTool (for category checks)."""
        try:
            result = self.prom.execute(query=query)
            if not result.success:
                return None
            return (result.data or {}).get("results", [])
        except Exception as exc:
            logger.debug("[MetricAnomalyDetector] instant query failed: %s", exc)
            return None

    def _range_query(self, query: str, lookback_m: int) -> Optional[List[Dict]]:
        """Range query via PrometheusTool (for category checks)."""
        try:
            now = int(time.time())
            start = str(now - lookback_m * 60)
            end = str(now)
            result = self.prom.execute(
                query=query, query_type="range",
                start=start, end=end, step="60s",
            )
            if not result.success:
                return None
            return (result.data or {}).get("results", [])
        except Exception as exc:
            logger.debug("[MetricAnomalyDetector] range query failed: %s", exc)
            return None

    def _fetch_instant(self, check: Dict) -> Optional[List[Dict]]:
        """Instant query via PrometheusTool (for user metric_checks)."""
        return self._instant_query(check["query"])

    def _fetch_range(self, check: Dict) -> Optional[List[Dict]]:
        """Range query for lookback_m minutes at 60s step (for user metric_checks)."""
        return self._range_query(check["query"], self.lookback_m)

    @staticmethod
    def _parse_series(series: Dict) -> Tuple[List[float], List[float], Dict]:
        """Extract (values, timestamps, metric_labels) from a Prometheus range result."""
        metric_labels = series.get("metric", {})
        raw_values = series.get("values", [])
        timestamps = []
        values = []
        for point in raw_values:
            try:
                ts = float(point[0])
                val = float(point[1])
                timestamps.append(ts)
                values.append(val)
            except (TypeError, ValueError, IndexError):
                continue
        return values, timestamps, metric_labels

    # ── Deduplication ─────────────────────────────────────────────────

    @staticmethod
    def _deduplicate(signals) -> list:
        """
        If the same metric+instance is flagged by multiple algorithms,
        keep the highest severity and merge descriptions.
        """
        from agents.detection_agent import DetectionSignal
        groups: Dict[str, list] = {}
        for sig in signals:
            key = f"{sig.title.split(':')[0]}:{sig.service}:{sig.namespace}"
            groups.setdefault(key, []).append(sig)

        result = []
        severity_rank = {"critical": 3, "warning": 2, "info": 1}

        for key, group in groups.items():
            if len(group) == 1:
                result.append(group[0])
                continue

            # Pick highest severity
            group.sort(key=lambda s: severity_rank.get(s.severity, 0), reverse=True)
            best = group[0]

            # Merge descriptions from all algorithms
            all_descs = [s.description for s in group]
            algo_names = []
            for d in all_descs:
                if d.startswith("["):
                    algo = d[1:d.index("]")] if "]" in d else ""
                    if algo and algo not in algo_names:
                        algo_names.append(algo)

            if len(algo_names) > 1:
                merged_desc = (
                    f"Detected by {len(algo_names)} algorithms: "
                    f"{', '.join(algo_names)}. " +
                    " | ".join(all_descs)
                )
                best = DetectionSignal(
                    signal_id="",
                    source=best.source,
                    severity=best.severity,
                    title=best.title,
                    description=merged_desc[:500],
                    namespace=best.namespace,
                    service=best.service,
                )
            result.append(best)

        return result
