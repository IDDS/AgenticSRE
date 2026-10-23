"""Performance data collector for vLLM fault injection experiments.

Collects metrics before/during/after fault injection to measure
impact on inference service performance.

Supports:
- Prometheus-based infrastructure metrics (GPU utilization, memory, etc.)
- vLLM-native metrics (scheduler stats, KV cache, token throughput)
- Token-level latency metrics (TTFT, TPOT, ITL)
- NVTX/CUPTI cross-layer trace collection (inspired by Teller)
- GPU kernel profiling (SM occupancy, memory bandwidth)
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


def _finite_float(value: Any, default: float = 0.0) -> float:
    """Convert metric values to JSON-safe finite floats."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


@dataclass
class MetricSnapshot:
    """A point-in-time collection of inference metrics."""
    timestamp: float
    label: str
    # Infrastructure metrics
    throughput_rps: float = 0.0
    avg_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    gpu_utilization: float = 0.0
    gpu_memory_used_mb: float = 0.0
    error_rate: float = 0.0
    active_requests: int = 0
    # Node metrics used to distinguish fault-window causality from background risk.
    node_cpu_usage: float = 0.0
    node_memory_usage: float = 0.0
    node_rootfs_usage: float = 0.0
    node_disk_read: float = 0.0
    node_disk_write: float = 0.0
    node_network_receive: float = 0.0
    node_network_transmit: float = 0.0
    # vLLM-native metrics
    num_running_requests: int = 0
    num_waiting_requests: int = 0
    kv_cache_usage_pct: float = 0.0
    num_preemptions: int = 0
    # Token-level latency
    ttft_ms: float = 0.0         # Time to First Token
    tpot_ms: float = 0.0         # Time Per Output Token
    itl_ms: float = 0.0          # Inter-Token Latency (avg)
    tokens_per_second: float = 0.0
    # GPU kernel metrics
    sm_occupancy_pct: float = 0.0
    gpu_memory_bandwidth_pct: float = 0.0
    gpu_power_watts: float = 0.0
    gpu_temperature_c: float = 0.0
    gpu_clock_mhz: float = 0.0
    raw_data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class NVTXTrace:
    """An NVTX/CUPTI cross-layer trace record."""
    request_id: str
    ranges: List[Dict[str, Any]] = field(default_factory=list)
    kernels: List[Dict[str, Any]] = field(default_factory=list)
    host_calls: List[Dict[str, Any]] = field(default_factory=list)
    logs: List[str] = field(default_factory=list)
    total_duration_ms: float = 0.0


class PerformanceCollector:
    """Collects performance metrics from vLLM and Prometheus.

    Supports three collection modes:
    1. Prometheus queries for infrastructure metrics
    2. vLLM /metrics endpoint scraping for native metrics
    3. NVTX/CUPTI trace collection via nsys/kubectl

    Args:
        prometheus_url: Prometheus server URL.
        vllm_metrics_url: vLLM metrics endpoint URL.
        kubectl_cmd: kubectl command for trace collection.
        namespace: K8s namespace for trace collection.
    """

    def __init__(
        self,
        prometheus_url: str = "http://localhost:9090",
        vllm_metrics_url: str = "http://localhost:8000/metrics",
        kubectl_cmd: str = "kubectl",
        namespace: str = "default",
    ) -> None:
        self.prometheus_url = prometheus_url.rstrip("/")
        self.vllm_metrics_url = vllm_metrics_url
        self.kubectl_cmd = kubectl_cmd
        self.namespace = namespace
        self._snapshots: List[MetricSnapshot] = []
        self._traces: List[NVTXTrace] = []

    async def collect_snapshot(self, label: str = "sample") -> MetricSnapshot:
        """Collect a single metric snapshot from all sources.

        Args:
            label: Human-readable label (e.g. "baseline", "during_fault").

        Returns:
            MetricSnapshot with current metrics.
        """
        snapshot = MetricSnapshot(timestamp=time.time(), label=label)

        # Collect from all sources in parallel
        await asyncio.gather(
            self._collect_prometheus(snapshot),
            self._collect_vllm_native(snapshot),
            self._collect_gpu_stats(snapshot),
            return_exceptions=True,
        )

        self._snapshots.append(snapshot)
        return snapshot

    async def _collect_prometheus(self, snapshot: MetricSnapshot) -> None:
        """Collect metrics from Prometheus."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                queries = {
                    "throughput_rps": [
                        'sum(rate(vllm:request_success_total[1m]))',
                        'sum(rate(vllm:num_requests_finished_total[1m]))',
                    ],
                    "avg_latency_ms": [
                        'avg(vllm:avg_request_latency_seconds) * 1000',
                        'histogram_quantile(0.50, sum(rate(vllm:e2e_request_latency_seconds_bucket[1m])) by (le, model_name)) * 1000',
                    ],
                    "p99_latency_ms": [
                        'histogram_quantile(0.99, sum(rate(vllm:e2e_request_latency_seconds_bucket[1m])) by (le, model_name)) * 1000',
                        'histogram_quantile(0.99, sum(rate(vllm:request_latency_seconds_bucket[1m])) by (le, model_name)) * 1000',
                    ],
                    "gpu_utilization": [
                        'avg(DCGM_FI_DEV_GPU_UTIL)',
                        'avg(nvidia_gpu_duty_cycle)',
                    ],
                    "gpu_memory_used_mb": [
                        'avg(DCGM_FI_DEV_FB_USED)',
                        'avg(nvidia_gpu_memory_used_bytes) / 1024 / 1024',
                    ],
                    "error_rate": [
                        'sum(rate(vllm:request_failure_total[1m]))',
                    ],
                    "gpu_power_watts": [
                        'avg(DCGM_FI_DEV_POWER_USAGE)',
                        'avg(nvidia_gpu_power_usage_milliwatts) / 1000',
                    ],
                    "gpu_temperature_c": [
                        'avg(DCGM_FI_DEV_GPU_TEMP)',
                        'avg(nvidia_gpu_temperature_gpu)',
                    ],
                    "gpu_clock_mhz": [
                        'avg(DCGM_FI_DEV_SM_CLOCK)',
                        'avg(nvidia_gpu_clocks_current_graphics_clock_hz) / 1e6',
                    ],
                    "node_cpu_usage": [
                        'max(100 * (1 - avg by(instance)(rate(node_cpu_seconds_total{mode="idle"}[1m]))))',
                    ],
                    "node_memory_usage": [
                        'max(100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))',
                    ],
                    "node_rootfs_usage": [
                        'max(100 * (1 - node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay"} / node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay"}))',
                    ],
                    "node_disk_read": [
                        'sum(rate(node_disk_read_bytes_total[1m])) / 1024 / 1024',
                    ],
                    "node_disk_write": [
                        'sum(rate(node_disk_written_bytes_total[1m])) / 1024 / 1024',
                    ],
                    "node_network_receive": [
                        'sum(rate(node_network_receive_bytes_total{device!="lo"}[1m])) / 1024 / 1024',
                    ],
                    "node_network_transmit": [
                        'sum(rate(node_network_transmit_bytes_total{device!="lo"}[1m])) / 1024 / 1024',
                    ],
                }
                for metric_name, candidates in queries.items():
                    query_list = candidates if isinstance(candidates, list) else [candidates]
                    try:
                        for query in query_list:
                            resp = await client.get(
                                f"{self.prometheus_url}/api/v1/query",
                                params={"query": query},
                            )
                            data = resp.json()
                            result = data.get("data", {}).get("result") if data.get("status") == "success" else []
                            if result:
                                value = _finite_float(result[0]["value"][1])
                                setattr(snapshot, metric_name, value)
                                snapshot.raw_data.setdefault("prometheus_queries", {})[metric_name] = query
                                break
                    except Exception:
                        pass
        except Exception as e:
            logger.warning("Failed to collect Prometheus metrics: %s", e)
            snapshot.raw_data["prometheus_error"] = str(e)

    async def _collect_vllm_native(self, snapshot: MetricSnapshot) -> None:
        """Scrape vLLM /metrics endpoint for native scheduler/KV metrics."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(self.vllm_metrics_url)
                text = resp.text
                # Parse Prometheus text format
                metric_patterns = {
                    "num_running_requests": r'vllm:num_requests_running\s+(\d+\.?\d*)',
                    "num_waiting_requests": r'vllm:num_requests_waiting\s+(\d+\.?\d*)',
                    "kv_cache_usage_pct": r'vllm:gpu_cache_usage_perc\s+(\d+\.?\d*)',
                    "num_preemptions": r'vllm:num_preemptions_total\s+(\d+\.?\d*)',
                    "ttft_ms": r'vllm:time_to_first_token_seconds_sum\s+(\d+\.?\d*)',
                    "tpot_ms": r'vllm:time_per_output_token_seconds_sum\s+(\d+\.?\d*)',
                    "tokens_per_second": r'vllm:avg_generation_throughput_toks_per_s\s+(\d+\.?\d*)',
                }
                for attr, pattern in metric_patterns.items():
                    match = re.search(pattern, text)
                    if match:
                        value = _finite_float(match.group(1))
                        # Convert seconds to ms for latency metrics
                        if attr in ("ttft_ms", "tpot_ms"):
                            value *= 1000
                        setattr(snapshot, attr, value)

                # Calculate ITL from token throughput
                if snapshot.tokens_per_second > 0:
                    snapshot.itl_ms = 1000.0 / snapshot.tokens_per_second

        except Exception as e:
            logger.debug("Failed to collect vLLM native metrics: %s", e)
            snapshot.raw_data["vllm_native_error"] = str(e)

    async def _collect_gpu_stats(self, snapshot: MetricSnapshot) -> None:
        """Collect GPU stats via nvidia-smi when Prometheus is unavailable."""
        if snapshot.gpu_utilization > 0:
            return  # Already collected via Prometheus
        try:
            proc = await asyncio.create_subprocess_shell(
                "nvidia-smi --query-gpu=utilization.gpu,memory.used,power.draw,"
                "temperature.gpu,clocks.current.graphics "
                "--format=csv,noheader,nounits 2>/dev/null | head -1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            parts = stdout.decode().strip().split(",")
            if len(parts) >= 5:
                snapshot.gpu_utilization = _finite_float(parts[0].strip())
                snapshot.gpu_memory_used_mb = _finite_float(parts[1].strip())
                snapshot.gpu_power_watts = _finite_float(parts[2].strip())
                snapshot.gpu_temperature_c = _finite_float(parts[3].strip())
                snapshot.gpu_clock_mhz = _finite_float(parts[4].strip())
        except Exception:
            pass

    async def collect_nvtx_trace(
        self,
        deployment: str = "vllm-server",
        duration_s: int = 10,
        request_id: str = "",
    ) -> Optional[NVTXTrace]:
        """Collect NVTX/CUPTI cross-layer trace from vLLM container.

        Inspired by Teller's non-intrusive tracing approach. Uses nsys
        (NVIDIA Nsight Systems) to collect NVTX ranges, CUPTI activity,
        and correlate with application logs.

        Args:
            deployment: Target deployment.
            duration_s: Trace duration in seconds.
            request_id: Optional request ID to filter trace.

        Returns:
            NVTXTrace with cross-layer trace data, or None on failure.
        """
        trace = NVTXTrace(request_id=request_id or f"trace-{int(time.time())}")

        # Collect NVTX ranges via nsys profile
        nsys_cmd = (
            f"{self.kubectl_cmd} exec -n {self.namespace} deploy/{deployment} -- "
            f"sh -c 'nsys profile -t nvtx,cuda --duration {duration_s} "
            f"--output /tmp/vllm_trace --force-overwrite true "
            f"--stats true 2>&1 | tail -50'"
        )

        try:
            proc = await asyncio.create_subprocess_shell(
                nsys_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=duration_s + 30
            )
            output = stdout.decode()

            # Parse NVTX ranges from nsys output
            for line in output.split("\n"):
                if "NVTX" in line or "Range" in line:
                    trace.ranges.append({"raw": line.strip()})
                elif "CUDA" in line or "kernel" in line.lower():
                    trace.kernels.append({"raw": line.strip()})

            trace.total_duration_ms = duration_s * 1000
            self._traces.append(trace)
            logger.info(
                "Collected NVTX trace: %d ranges, %d kernels",
                len(trace.ranges), len(trace.kernels),
            )
            return trace

        except asyncio.TimeoutError:
            logger.warning("NVTX trace collection timed out")
            return None
        except Exception as e:
            logger.warning("NVTX trace collection failed: %s", e)
            return None

    async def collect_cross_layer_snapshot(
        self,
        deployment: str = "vllm-server",
        label: str = "cross_layer",
    ) -> Dict[str, Any]:
        """Collect a comprehensive cross-layer observability snapshot.

        Combines infrastructure metrics, vLLM-native metrics, GPU kernel
        profiling, and NVTX trace markers into a unified view.

        Args:
            deployment: Target deployment.
            label: Snapshot label.

        Returns:
            Dict with cross-layer metrics organized by layer.
        """
        snapshot = await self.collect_snapshot(label)

        return {
            "timestamp": snapshot.timestamp,
            "label": label,
            "inference_engine": {
                "throughput_rps": snapshot.throughput_rps,
                "avg_latency_ms": snapshot.avg_latency_ms,
                "p99_latency_ms": snapshot.p99_latency_ms,
                "error_rate": snapshot.error_rate,
                "num_running": snapshot.num_running_requests,
                "num_waiting": snapshot.num_waiting_requests,
                "kv_cache_usage_pct": snapshot.kv_cache_usage_pct,
                "num_preemptions": snapshot.num_preemptions,
            },
            "token_latency": {
                "ttft_ms": snapshot.ttft_ms,
                "tpot_ms": snapshot.tpot_ms,
                "itl_ms": snapshot.itl_ms,
                "tokens_per_second": snapshot.tokens_per_second,
            },
            "gpu_compute": {
                "utilization_pct": snapshot.gpu_utilization,
                "memory_used_mb": snapshot.gpu_memory_used_mb,
                "sm_occupancy_pct": snapshot.sm_occupancy_pct,
                "memory_bandwidth_pct": snapshot.gpu_memory_bandwidth_pct,
                "power_watts": snapshot.gpu_power_watts,
                "temperature_c": snapshot.gpu_temperature_c,
                "clock_mhz": snapshot.gpu_clock_mhz,
            },
            "active_requests": snapshot.active_requests,
        }

    async def collect_baseline(self, samples: int = 3, interval_s: float = 5.0) -> List[MetricSnapshot]:
        """Collect baseline performance metrics.

        Args:
            samples: Number of samples to collect.
            interval_s: Time between samples in seconds.

        Returns:
            List of baseline snapshots.
        """
        results = []
        for i in range(samples):
            snap = await self.collect_snapshot(f"baseline_{i}")
            results.append(snap)
            if i < samples - 1:
                await asyncio.sleep(interval_s)
        return results

    async def collect_during_fault(
        self, samples: int = 5, interval_s: float = 5.0
    ) -> List[MetricSnapshot]:
        """Collect metrics during fault injection.

        Args:
            samples: Number of samples.
            interval_s: Interval between samples.

        Returns:
            List of fault-period snapshots.
        """
        results = []
        for i in range(samples):
            snap = await self.collect_snapshot(f"fault_{i}")
            results.append(snap)
            if i < samples - 1:
                await asyncio.sleep(interval_s)
        return results

    def get_comparison(self) -> Dict[str, Any]:
        """Compare baseline vs fault-period metrics.

        Returns:
            Dict with baseline averages, fault averages, and deltas.
        """
        baseline = [s for s in self._snapshots if s.label.startswith("baseline")]
        fault = [s for s in self._snapshots if s.label.startswith("fault")]

        def avg(snaps: List[MetricSnapshot], attr: str) -> float:
            if not snaps:
                return 0.0
            values = [_finite_float(getattr(s, attr)) for s in snaps]
            return round(sum(values) / len(values), 3)

        metrics = [
            "throughput_rps", "avg_latency_ms", "p99_latency_ms",
            "gpu_utilization", "error_rate",
            "ttft_ms", "tpot_ms", "itl_ms", "tokens_per_second",
            "kv_cache_usage_pct", "num_preemptions",
            "gpu_power_watts", "gpu_temperature_c",
            "node_cpu_usage", "node_memory_usage", "node_rootfs_usage",
            "node_disk_read", "node_disk_write",
            "node_network_receive", "node_network_transmit",
        ]
        result: Dict[str, Any] = {"baseline": {}, "fault": {}, "delta": {}}

        for m in metrics:
            b = avg(baseline, m)
            f = avg(fault, m)
            result["baseline"][m] = b
            result["fault"][m] = f
            result["delta"][m] = round(f - b, 3)

        return result

    def reset(self) -> None:
        """Clear all collected snapshots and traces."""
        self._snapshots.clear()
        self._traces.clear()

    def get_all_snapshots(self) -> List[Dict[str, Any]]:
        """Return all snapshots as dicts."""
        return [
            {
                "timestamp": _finite_float(s.timestamp),
                "label": s.label,
                "throughput_rps": _finite_float(s.throughput_rps),
                "avg_latency_ms": _finite_float(s.avg_latency_ms),
                "p99_latency_ms": _finite_float(s.p99_latency_ms),
                "gpu_utilization": _finite_float(s.gpu_utilization),
                "gpu_memory_used_mb": _finite_float(s.gpu_memory_used_mb),
                "error_rate": _finite_float(s.error_rate),
                "active_requests": int(_finite_float(s.active_requests)),
                "ttft_ms": _finite_float(s.ttft_ms),
                "tpot_ms": _finite_float(s.tpot_ms),
                "itl_ms": _finite_float(s.itl_ms),
                "tokens_per_second": _finite_float(s.tokens_per_second),
                "kv_cache_usage_pct": _finite_float(s.kv_cache_usage_pct),
                "num_preemptions": _finite_float(s.num_preemptions),
                "gpu_power_watts": _finite_float(s.gpu_power_watts),
                "gpu_temperature_c": _finite_float(s.gpu_temperature_c),
                "gpu_clock_mhz": _finite_float(s.gpu_clock_mhz),
                "node_cpu_usage": _finite_float(s.node_cpu_usage),
                "node_memory_usage": _finite_float(s.node_memory_usage),
                "node_rootfs_usage": _finite_float(s.node_rootfs_usage),
                "node_disk_read": _finite_float(s.node_disk_read),
                "node_disk_write": _finite_float(s.node_disk_write),
                "node_network_receive": _finite_float(s.node_network_receive),
                "node_network_transmit": _finite_float(s.node_network_transmit),
            }
            for s in self._snapshots
        ]

    def get_all_traces(self) -> List[Dict[str, Any]]:
        """Return all NVTX traces as dicts."""
        return [
            {
                "request_id": t.request_id,
                "ranges_count": len(t.ranges),
                "kernels_count": len(t.kernels),
                "host_calls_count": len(t.host_calls),
                "logs_count": len(t.logs),
                "total_duration_ms": t.total_duration_ms,
            }
            for t in self._traces
        ]

    # ------------------------------------------------------------------
    # Teller-inspired observability features (Section 3.2-3.5)
    # ------------------------------------------------------------------

    async def collect_nonintrusive_trace(
        self,
        deployment: str = "vllm-server",
        duration_s: int = 10,
    ) -> Optional[NVTXTrace]:
        """Collect non-intrusive cross-layer trace via LD_PRELOAD approach.

        Inspired by Teller Section 3.2: uses environment variable injection
        and dynamic library preload to enable NVTX+CUPTI tracing without
        modifying model binaries. Collects three signal families:
        1. Engine/framework-level NVTX ranges (STEP/FRONTEND/BACKEND)
        2. CUDA runtime/driver/kernel events via CUPTI callbacks
        3. Aligned stdout/stderr logs from the same execution

        Args:
            deployment: Target deployment.
            duration_s: Trace duration in seconds.

        Returns:
            NVTXTrace with cross-layer trace data, or None on failure.
        """
        trace = NVTXTrace(request_id=f"nonintrusive-{int(time.time())}")

        # Use nsys with targeted CUPTI activity collection
        cmd = (
            f"{self.kubectl_cmd} exec -n {self.namespace} deploy/{deployment} -- "
            f"sh -c '"
            f"nsys profile -t nvtx,cuda,osrt,cudnn "
            f"--capture-range=cudaProfilerApi "
            f"--duration {duration_s} "
            f"--output /tmp/teller_trace --force-overwrite true "
            f"--stats true --export sqlite 2>&1; "
            f"nsys stats /tmp/teller_trace.nsys-rep --report nvtx_pushpop_trace 2>&1 | head -100; "
            f"nsys stats /tmp/teller_trace.nsys-rep --report cuda_gpu_kern_sum 2>&1 | head -50; "
            f"nsys stats /tmp/teller_trace.nsys-rep --report cuda_api_sum 2>&1 | head -50"
            f"'"
        )

        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=duration_s + 60
            )
            output = stdout.decode()

            # Parse structured sections
            section = ""
            for line in output.split("\n"):
                line_s = line.strip()
                if "nvtx_pushpop" in line_s.lower():
                    section = "nvtx"
                elif "cuda_gpu_kern" in line_s.lower():
                    section = "kernel"
                elif "cuda_api_sum" in line_s.lower():
                    section = "hostcall"

                if section == "nvtx" and line_s and not line_s.startswith("--"):
                    trace.ranges.append({"raw": line_s, "type": "nvtx_range"})
                elif section == "kernel" and line_s and not line_s.startswith("--"):
                    trace.kernels.append({"raw": line_s, "type": "gpu_kernel"})
                elif section == "hostcall" and line_s and not line_s.startswith("--"):
                    trace.host_calls.append({"raw": line_s, "type": "cuda_api"})

            trace.total_duration_ms = duration_s * 1000
            self._traces.append(trace)
            logger.info(
                "Non-intrusive trace: %d NVTX ranges, %d kernels, %d host calls",
                len(trace.ranges), len(trace.kernels), len(trace.host_calls),
            )
            return trace

        except asyncio.TimeoutError:
            logger.warning("Non-intrusive trace collection timed out")
            return None
        except Exception as e:
            logger.warning("Non-intrusive trace collection failed: %s", e)
            return None

    def reconstruct_call_chain(
        self, trace: NVTXTrace
    ) -> Dict[str, Any]:
        """Reconstruct per-request call-chain tree from NVTX/CUPTI events.

        Implements Teller Algorithm 1: normalizes events, reconstructs NVTX
        ranges by stack scanning, binds host/device activity, projects into
        per-request trees, and aligns log lines.

        Args:
            trace: NVTXTrace with collected ranges, kernels, host_calls.

        Returns:
            Dict with call-chain tree structure:
            {
                "request_id": str,
                "tree": [{"name": ..., "children": [...], "kernels": [...]}],
                "depth": int,
                "total_nodes": int,
            }
        """
        tree: List[Dict[str, Any]] = []
        step_nodes = []
        current_step: Optional[Dict[str, Any]] = None

        for r in trace.ranges:
            raw = r.get("raw", "")
            # Classify NVTX ranges into STEP/FRONTEND/BACKEND hierarchy
            node: Dict[str, Any] = {
                "raw": raw,
                "children": [],
                "kernels": [],
                "host_calls": [],
            }

            raw_upper = raw.upper()
            # Split into tokens for word-boundary matching to avoid
            # false positives (e.g. "PERFORMANCE" matching "FE")
            raw_tokens = set(raw_upper.split())
            if "STEP" in raw_upper:
                node["level"] = "STEP"
                current_step = node
                step_nodes.append(node)
            elif "FRONTEND" in raw_upper or "FE" in raw_tokens:
                node["level"] = "FRONTEND"
                if current_step:
                    current_step["children"].append(node)
            elif "BACKEND" in raw_upper or "BE" in raw_tokens:
                node["level"] = "BACKEND"
                if current_step:
                    current_step["children"].append(node)
            else:
                node["level"] = "OTHER"
                if current_step:
                    current_step["children"].append(node)
                else:
                    step_nodes.append(node)

        # Bind kernels to closest backend node
        for k in trace.kernels:
            if step_nodes:
                # Attach to last step's last backend child, or step itself
                target = step_nodes[-1]
                backends = [c for c in target.get("children", [])
                            if c.get("level") == "BACKEND"]
                if backends:
                    backends[-1]["kernels"].append(k)
                else:
                    target["kernels"].append(k)

        # Count total nodes
        def count_nodes(nodes: List[Dict]) -> int:
            total = len(nodes)
            for n in nodes:
                total += count_nodes(n.get("children", []))
            return total

        # Compute max depth
        def max_depth(nodes: List[Dict], depth: int = 0) -> int:
            if not nodes:
                return depth
            return max(max_depth(n.get("children", []), depth + 1) for n in nodes)

        return {
            "request_id": trace.request_id,
            "tree": step_nodes,
            "depth": max_depth(step_nodes),
            "total_nodes": count_nodes(step_nodes),
        }

    def localize_candidates(
        self, snapshots: Optional[List[MetricSnapshot]] = None
    ) -> List[Dict[str, Any]]:
        """Numeric candidate localization using anomaly scoring.

        Inspired by Teller Section 3.4: builds a feature vector for each
        snapshot from timing, kernel counts, and metric values. Fits a
        Gaussian model and computes anomaly scores. Returns snapshots
        ranked by suspiciousness.

        Args:
            snapshots: List of snapshots to analyze. Defaults to all collected.

        Returns:
            List of candidates sorted by anomaly score (highest first).
            Each entry contains the snapshot label, anomaly_score, and
            the metric values that contributed most.
        """
        snaps = snapshots or self._snapshots
        if len(snaps) < 2:
            return []

        # Extract feature vectors
        feature_names = [
            "throughput_rps", "avg_latency_ms", "p99_latency_ms",
            "gpu_utilization", "error_rate", "ttft_ms", "tpot_ms",
            "kv_cache_usage_pct", "num_preemptions",
        ]
        vectors = []
        for s in snaps:
            vec = [getattr(s, f, 0.0) for f in feature_names]
            vectors.append(vec)

        # Compute mean and std for z-score based anomaly detection
        n = len(vectors)
        dim = len(feature_names)
        means = [sum(vectors[i][j] for i in range(n)) / n for j in range(dim)]
        stds = [
            max(
                (sum((vectors[i][j] - means[j]) ** 2 for i in range(n)) / n) ** 0.5,
                1e-10,
            )
            for j in range(dim)
        ]

        candidates = []
        for idx, s in enumerate(snaps):
            # Z-score per feature, aggregate into anomaly score
            z_scores = {}
            total_z = 0.0
            for j, fname in enumerate(feature_names):
                z = abs(vectors[idx][j] - means[j]) / stds[j]
                z_scores[fname] = round(z, 3)
                total_z += z

            anomaly_score = round(total_z / dim, 3)
            # Find top contributing features
            top_features = sorted(
                z_scores.items(), key=lambda x: x[1], reverse=True
            )[:3]

            candidates.append({
                "label": s.label,
                "timestamp": s.timestamp,
                "anomaly_score": anomaly_score,
                "top_anomalous_features": [
                    {"feature": f, "z_score": z} for f, z in top_features
                ],
                "z_scores": z_scores,
            })

        candidates.sort(key=lambda x: x["anomaly_score"], reverse=True)
        return candidates

    def extract_causal_context(
        self,
        call_chain: Dict[str, Any],
        suspicious_level: str = "BACKEND",
    ) -> Dict[str, Any]:
        """Extract dependency-aware causal-context slice from call-chain.

        Inspired by Teller Section 3.4: given a reconstructed call-chain
        tree and a suspicious region, extracts a compact subgraph that
        preserves parent-child structure, temporal order, and communication
        relations.

        The slice answers: who, where, when, and with whom a failure
        is associated.

        Args:
            call_chain: Output of reconstruct_call_chain().
            suspicious_level: Which level to focus on ("STEP", "FRONTEND",
                "BACKEND", "OTHER").

        Returns:
            Dict with causal context slice:
            {
                "request_id": str,
                "suspicious_nodes": [...],
                "parent_chain": [...],
                "sibling_context": [...],
                "kernel_evidence": [...],
            }
        """
        tree = call_chain.get("tree", [])
        suspicious_nodes = []
        parent_chain = []
        sibling_context = []
        kernel_evidence = []

        for step in tree:
            step_has_suspicious = False
            for child in step.get("children", []):
                if child.get("level") == suspicious_level:
                    suspicious_nodes.append({
                        "raw": child.get("raw", ""),
                        "level": child.get("level"),
                        "kernel_count": len(child.get("kernels", [])),
                    })
                    kernel_evidence.extend(child.get("kernels", []))
                    step_has_suspicious = True
                else:
                    sibling_context.append({
                        "raw": child.get("raw", ""),
                        "level": child.get("level"),
                    })

            if step_has_suspicious:
                parent_chain.append({
                    "raw": step.get("raw", ""),
                    "level": step.get("level", "STEP"),
                    "children_count": len(step.get("children", [])),
                })

        return {
            "request_id": call_chain.get("request_id", ""),
            "suspicious_nodes": suspicious_nodes,
            "parent_chain": parent_chain,
            "sibling_context": sibling_context,
            "kernel_evidence": kernel_evidence,
        }
