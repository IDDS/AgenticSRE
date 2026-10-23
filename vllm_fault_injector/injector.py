"""Unified fault injector entry point for vLLM inference scenarios.

Orchestrates fault injection across all 4 layers (software, driver,
network, OS) with automatic recovery and performance measurement.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from vllm_fault_injector.software_faults import SoftwareFaultInjector, FaultResult
from vllm_fault_injector.driver_faults import DriverFaultInjector
from vllm_fault_injector.network_faults import NetworkFaultInjector
from vllm_fault_injector.os_faults import OSFaultInjector
from vllm_fault_injector.collector import PerformanceCollector

logger = logging.getLogger(__name__)


class FaultInjector:
    """Unified fault injection orchestrator.

    Manages fault injection across software, driver, network, and OS layers
    with automatic recovery and performance impact measurement.

    Args:
        kubectl_cmd: Base kubectl command (may include SSH prefix).
        namespace: Target K8s namespace.
        deployment: Target deployment name.
        dry_run: If True, only log commands without executing.
        prometheus_url: Prometheus URL for metrics collection.
    """

    def __init__(
        self,
        kubectl_cmd: str = "kubectl",
        namespace: str = "default",
        deployment: str = "vllm-server",
        dry_run: bool = True,
        prometheus_url: str = "http://localhost:9090",
    ) -> None:
        self.namespace = namespace
        self.deployment = deployment
        self.dry_run = dry_run

        self.software = SoftwareFaultInjector(kubectl_cmd, namespace, dry_run)
        self.driver = DriverFaultInjector(kubectl_cmd, namespace, dry_run)
        self.network = NetworkFaultInjector(kubectl_cmd, namespace, dry_run)
        self.os_faults = OSFaultInjector(kubectl_cmd, namespace, dry_run)
        self.collector = PerformanceCollector(prometheus_url)

        self._fault_map = {
            # Software layer
            "process_crash": self.software.inject_process_crash,
            "model_load_failure": self.software.inject_model_load_failure,
            "tokenizer_error": self.software.inject_tokenizer_error,
            "process_hang": self.software.inject_process_hang,
            "model_permission_error": self.software.inject_model_permission_error,
            "kv_cache_exhaustion": self.software.inject_kv_cache_exhaustion,
            "scheduler_contention": self.software.inject_scheduler_contention,
            "request_queue_overflow": self.software.inject_request_queue_overflow,
            "continuous_batch_disruption": self.software.inject_continuous_batch_disruption,
            "weight_corruption": self.software.inject_weight_corruption,
            "prefill_decode_misclassification": self.software.inject_prefill_decode_misclassification,
            "attention_backend_mismatch": self.software.inject_attention_backend_mismatch,
            "state_contamination": self.software.inject_state_contamination,
            # Driver layer
            "gpu_driver_crash": self.driver.inject_gpu_driver_crash,
            "cuda_error": self.driver.inject_cuda_error,
            "nccl_failure": self.driver.inject_nccl_failure,
            "ecc_memory_error": self.driver.inject_ecc_memory_error,
            "pcie_throttle": self.driver.inject_pcie_throttle,
            "gpu_clock_throttle": self.driver.inject_gpu_clock_throttle,
            "cuda_ctx_corruption": self.driver.inject_cuda_ctx_corruption,
            "cupti_trace_overhead": self.driver.inject_cupti_trace_overhead,
            # Network layer
            "endpoint_timeout": self.network.inject_endpoint_timeout,
            "grpc_failure": self.network.inject_grpc_failure,
            "http_503": self.network.inject_http_503,
            "tp_communication_loss": self.network.inject_tp_communication_loss,
            "dns_failure": self.network.inject_dns_failure,
            "packet_loss": self.network.inject_packet_loss,
            "bandwidth_throttle": self.network.inject_bandwidth_throttle,
            "rdma_failure": self.network.inject_rdma_failure,
            "service_mesh_fault": self.network.inject_service_mesh_fault,
            # OS layer
            "gpu_oom": self.os_faults.inject_gpu_oom,
            "cpu_stress": self.os_faults.inject_cpu_stress,
            "memory_pressure": self.os_faults.inject_memory_pressure,
            "disk_pressure": self.os_faults.inject_disk_pressure,
            "numa_error": self.os_faults.inject_numa_error,
            "cgroup_limit": self.os_faults.inject_cgroup_limit,
            "hugepages_exhaustion": self.os_faults.inject_hugepages_exhaustion,
            "fd_exhaustion": self.os_faults.inject_fd_exhaustion,
            "io_scheduler_interference": self.os_faults.inject_io_scheduler_interference,
            "clock_skew": self.os_faults.inject_clock_skew,
        }

    async def inject(self, fault_type: str, **kwargs: Any) -> FaultResult:
        """Inject a specific fault by type name.

        Args:
            fault_type: Fault type identifier.
            **kwargs: Additional parameters for the injector.

        Returns:
            FaultResult with injection outcome.

        Raises:
            ValueError: If fault_type is unknown.
        """
        if fault_type not in self._fault_map:
            raise ValueError(
                f"Unknown fault type: {fault_type}. "
                f"Available: {list(self._fault_map.keys())}"
            )
        injector_fn = self._fault_map[fault_type]
        logger.info("Injecting fault: %s (deployment=%s)", fault_type, self.deployment)
        result = await injector_fn(self.deployment, **kwargs)
        logger.info("Injection result: %s", result.message)
        return result

    async def recover(self, layer: str = "all") -> List[FaultResult]:
        """Recover from faults in specified layer(s).

        Args:
            layer: Layer to recover ("software", "driver", "network", "os", "all").

        Returns:
            List of recovery results.
        """
        results = []
        injectors = {
            "software": self.software,
            "driver": self.driver,
            "network": self.network,
            "os": self.os_faults,
        }

        targets = injectors if layer == "all" else {layer: injectors.get(layer)}
        for name, inj in targets.items():
            if inj is None:
                continue
            try:
                result = await inj.recover_all(self.deployment)
                results.append(result)
            except Exception as e:
                results.append(FaultResult(
                    fault_type="recover", layer=name,
                    success=False, message=str(e),
                ))
        return results

    async def run_experiment(
        self,
        fault_type: str,
        baseline_samples: int = 3,
        fault_samples: int = 5,
        sample_interval: float = 5.0,
        recovery_layer: str = "all",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Run a complete fault injection experiment with metrics.

        Collects baseline metrics, injects fault, collects fault metrics,
        recovers, and returns comparison.

        Args:
            fault_type: Fault to inject.
            baseline_samples: Number of baseline metric samples.
            fault_samples: Number of during-fault metric samples.
            sample_interval: Seconds between metric samples.
            **kwargs: Additional injector parameters.

        Returns:
            Dict with experiment results including metrics comparison.
        """
        self.collector.reset()
        experiment_start = time.time()

        # Phase 1: Baseline
        logger.info("Collecting baseline metrics...")
        await self.collector.collect_baseline(baseline_samples, sample_interval)

        # Phase 2: Inject
        logger.info("Injecting fault: %s", fault_type)
        inject_result = await self.inject(fault_type, **kwargs)

        # Phase 3: Measure impact
        logger.info("Collecting fault-period metrics...")
        await asyncio.sleep(2)  # Wait for fault to take effect
        await self.collector.collect_during_fault(fault_samples, sample_interval)

        # Phase 4: Recover
        logger.info("Recovering...")
        recovery_results = await self.recover(recovery_layer or "all")

        # Phase 5: Compile results
        comparison = self.collector.get_comparison()
        return {
            "fault_type": fault_type,
            "deployment": self.deployment,
            "namespace": self.namespace,
            "dry_run": self.dry_run,
            "duration_s": round(time.time() - experiment_start, 3),
            "injection": {
                "success": inject_result.success,
                "message": inject_result.message,
            },
            "recovery": [
                {"layer": r.layer, "success": r.success, "message": r.message}
                for r in recovery_results
            ],
            "recovery_layer": recovery_layer or "all",
            "metrics_comparison": comparison,
            "all_snapshots": self.collector.get_all_snapshots(),
        }

    def list_all_faults(self) -> Dict[str, List[Dict[str, str]]]:
        """List all available fault types organized by layer.

        Returns:
            Dict mapping layer name to list of fault descriptors.
        """
        return {
            "software": self.software.list_faults(),
            "driver": self.driver.list_faults(),
            "network": self.network.list_faults(),
            "os": self.os_faults.list_faults(),
        }
