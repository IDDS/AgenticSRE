"""vLLM Fault Injector — AI inference scenario fault injection tool.

Provides fault injection capabilities across 4 layers:
- Software: vLLM process crashes, model loading failures, KV cache exhaustion
- Driver: GPU driver crashes, CUDA errors, NCCL communication failures
- Network: Inference endpoint timeouts, gRPC failures, TP communication loss
- OS: GPU memory OOM, NUMA config errors, Huge Pages exhaustion
"""

from vllm_fault_injector.injector import FaultInjector
from vllm_fault_injector.software_faults import SoftwareFaultInjector
from vllm_fault_injector.driver_faults import DriverFaultInjector
from vllm_fault_injector.network_faults import NetworkFaultInjector
from vllm_fault_injector.os_faults import OSFaultInjector
from vllm_fault_injector.collector import PerformanceCollector
from vllm_fault_injector.host_faults import build_host_fault_command

__all__ = [
    "FaultInjector",
    "SoftwareFaultInjector",
    "DriverFaultInjector",
    "NetworkFaultInjector",
    "OSFaultInjector",
    "PerformanceCollector",
    "build_host_fault_command",
]
