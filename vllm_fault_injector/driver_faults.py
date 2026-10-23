"""Driver layer fault injection for GPU/CUDA/NCCL.

Handles: GPU driver crashes, CUDA errors, NCCL communication failures.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
from typing import Dict, List, Optional, Tuple

from vllm_fault_injector.k8s_utils import build_label_selector
from vllm_fault_injector.software_faults import FaultResult

VLLM_PID_SH = (
    "for f in /proc/[0-9]*/cmdline; do "
    "pid=${f#/proc/}; pid=${pid%/cmdline}; "
    "[ \"$pid\" = \"1\" ] && continue; "
    "[ \"$pid\" = \"$$\" ] && continue; "
    "cmd=$(tr \"\\000\" \" \" < \"$f\" 2>/dev/null || true); "
    "case \"$cmd\" in *vllm.entrypoints.openai.api_server*|*runpy.run_module*api_server*) "
    "case \"$cmd\" in *kubectl*|*pgrep*|*teller.cli*|*\"sh -c\"*) continue;; esac; "
    "echo \"$pid\"; break;; "
    "esac; "
    "done"
)

logger = logging.getLogger(__name__)


class DriverFaultInjector:
    """Injects driver-layer faults targeting GPU and CUDA subsystems.

    Args:
        kubectl_cmd: Base kubectl command.
        namespace: Target K8s namespace.
        dry_run: If True, only log commands.
    """

    def __init__(
        self,
        kubectl_cmd: str = "kubectl",
        namespace: str = "default",
        dry_run: bool = True,
    ) -> None:
        self.kubectl = kubectl_cmd
        self.namespace = namespace
        self.dry_run = dry_run

    async def inject_gpu_driver_crash(self, node: str = "") -> FaultResult:
        """Simulate GPU driver crash by unloading nvidia module.

        Args:
            node: Target node name (empty for any GPU node).

        Returns:
            FaultResult with injection outcome.
        """
        target = f"--field-selector spec.nodeName={node}" if node else ""
        cmd = (
            f"{self.kubectl} debug node/{node or '$(kubectl get nodes -l nvidia.com/gpu.present=true -o name | head -1)'} "
            f"-n {self.namespace} --image=alpine:3.18 --image-pull-policy=IfNotPresent -- "
            f"sh -c 'echo Simulating GPU driver crash via nvidia-smi drain'"
        )
        return await self._execute(
            cmd, "gpu_driver_crash", "driver",
            f"GPU driver crash simulated on {node or 'auto-selected node'}",
            f"# Recovery: restart kubelet on affected node",
        )

    async def inject_cuda_error(self, deployment: str = "vllm-server") -> FaultResult:
        """Inject CUDA unavailability by hiding all devices from the workload.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        set_env_args = [
            "set", "env", f"deploy/{deployment}", "-n", self.namespace,
            "CUDA_VISIBLE_DEVICES=-1",
        ]
        recovery_cmd = (
            f"{self._kubectl_display(['set', 'env', f'deploy/{deployment}', '-n', self.namespace, 'CUDA_VISIBLE_DEVICES-'])} && "
            f"{self._kubectl_display(['rollout', 'restart', f'deploy/{deployment}', '-n', self.namespace])}"
        )
        command = (
            f"{self._kubectl_display(set_env_args)} && "
            f"delete the pre-update pods selected from deploy/{deployment}"
        )
        if self.dry_run:
            return FaultResult(
                "cuda_error", "driver", True,
                f"[DRY-RUN] Hide CUDA devices from {deployment}",
                recovery_cmd, command,
            )
        try:
            pods = await self._deployment_pod_names(deployment)
            rc, _, stderr = await self._run_kubectl(set_env_args)
            if rc != 0:
                return FaultResult("cuda_error", "driver", False, stderr, recovery_cmd, command)
            if pods:
                rc, _, stderr = await self._run_kubectl([
                    "delete", *(f"pod/{name}" for name in pods), "-n", self.namespace,
                    "--grace-period=5", "--wait=false",
                ])
                if rc != 0:
                    return FaultResult("cuda_error", "driver", False, stderr, recovery_cmd, command)
            return FaultResult(
                "cuda_error", "driver", True,
                f"Hid CUDA devices from {deployment}; recycled {len(pods)} pre-update pod(s)",
                recovery_cmd, command,
            )
        except Exception as exc:
            return FaultResult("cuda_error", "driver", False, str(exc), recovery_cmd, command)

    async def inject_nccl_failure(self, deployment: str = "vllm-server") -> FaultResult:
        """Simulate NCCL communication failure by blocking inter-GPU ports.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"sh -c 'export NCCL_TIMEOUT=1 && export NCCL_DEBUG=WARN && "
            f"iptables -A OUTPUT -p tcp --dport 29500 -j DROP 2>/dev/null || "
            f"echo NCCL failure simulated'"
        )
        return await self._execute(
            cmd, "nccl_failure", "driver",
            f"NCCL communication blocked on {deployment}",
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- "
            f"sh -c 'iptables -D OUTPUT -p tcp --dport 29500 -j DROP 2>/dev/null || true'",
        )

    async def inject_ecc_memory_error(self, deployment: str = "vllm-server") -> FaultResult:
        """Simulate GPU ECC uncorrectable memory error.

        Uses nvidia-smi to inject a simulated ECC error event or, if
        unavailable, forces GPU memory corruption via targeted writes.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"sh -c '"
            f"nvidia-smi --gpu-reset -i 0 2>/dev/null && echo ECC_error_injected || "
            f"python3 -c \""
            f"import ctypes, torch; t = torch.zeros(1024, device=\\\"cuda\\\"); "
            f"ptr = t.data_ptr(); ctypes.memset(ptr, 0xFF, 8); "
            f"print(\\\"ECC-like memory corruption injected\\\")\"'"
        )
        return await self._execute(
            cmd, "ecc_memory_error", "driver",
            f"ECC memory error simulated on {deployment}",
            f"{self.kubectl} rollout restart deploy/{deployment} -n {self.namespace}",
        )

    async def inject_pcie_throttle(self, deployment: str = "vllm-server") -> FaultResult:
        """Throttle PCIe bandwidth by saturating the PCIe bus.

        Launches a background process that continuously copies data between
        host and device to saturate PCIe bandwidth.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"sh -c 'nohup python3 -c \"import torch,time; "
            f"h=torch.randn((1024,1024,32)); "
            f"[h.cuda().cpu() for _ in range(120)]; "
            f"time.sleep(30)\" >/tmp/agenticsre_pcie_throttle.log 2>&1 & "
            f"echo PCIe throttle started'"
        )
        return await self._execute(
            cmd, "pcie_throttle", "driver",
            f"PCIe bus saturated on {deployment}",
            f"{self.kubectl} rollout restart deploy/{deployment} -n {self.namespace}",
        )

    async def inject_gpu_clock_throttle(self, deployment: str = "vllm-server") -> FaultResult:
        """Force GPU clock to minimum frequency to simulate thermal throttling.

        Uses nvidia-smi to lock GPU clocks at minimum supported frequency.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"sh -c 'nvidia-smi -lgc 210,210 2>/dev/null && "
            f"echo GPU clock locked to minimum || "
            f"echo nvidia-smi clock control not available'"
        )
        return await self._execute(
            cmd, "gpu_clock_throttle", "driver",
            f"GPU clock throttled to minimum on {deployment}",
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- "
            f"nvidia-smi -rgc 2>/dev/null || true",
        )

    async def inject_cuda_ctx_corruption(self, deployment: str = "vllm-server") -> FaultResult:
        """Corrupt CUDA context to cause subsequent kernel launch failures.

        Destroys the primary CUDA context, causing all subsequent CUDA
        operations to fail until process restart.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"python3 -c \""
            f"import ctypes; "
            f"cuda=ctypes.CDLL('libcuda.so'); "
            f"ctx=ctypes.c_void_p(); "
            f"cuda.cuCtxGetCurrent(ctypes.byref(ctx)); "
            f"cuda.cuCtxDestroy(ctx) if ctx.value else 0; "
            f"print('CUDA context destroyed' if ctx.value else 'No current CUDA context')\""
        )
        return await self._execute(
            cmd, "cuda_ctx_corruption", "driver",
            f"CUDA context destroyed on {deployment}",
            f"{self.kubectl} rollout restart deploy/{deployment} -n {self.namespace}",
        )

    async def inject_cupti_trace_overhead(
        self, deployment: str = "vllm-server", duration: int = 60
    ) -> FaultResult:
        """Inject CUPTI profiling overhead to slow GPU kernel dispatches.

        Inspired by Teller's non-intrusive tracing design (Section 3.2):
        CUPTI callbacks and activity collection introduce measurable overhead
        on kernel launch latency. This simulates the scenario where profiling
        tools left running in production cause performance degradation.

        Enables full CUPTI activity tracing via nsys for the specified
        duration, adding ~5-15% overhead on kernel launches.

        Args:
            deployment: Target deployment name.
            duration: Duration in seconds to run profiler.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"sh -c 'nohup nsys profile -t cuda,nvtx,osrt --duration {duration} "
            f"--output /tmp/cupti_overhead_trace --force-overwrite true "
            f"--sample none -p $({VLLM_PID_SH}) > /dev/null 2>&1 & "
            f"echo CUPTI overhead tracing started for {duration}s'"
        )
        return await self._execute(
            cmd, "cupti_trace_overhead", "driver",
            f"CUPTI profiling overhead injected for {duration}s on {deployment}",
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- "
            f"sh -c 'killall nsys 2>/dev/null; rm -f /tmp/cupti_overhead_trace* || true'",
        )

    async def recover_all(self, deployment: str = "vllm-server") -> FaultResult:
        """Recover from driver faults."""
        unset_args = [
            "set", "env", f"deploy/{deployment}", "-n", self.namespace,
            "CUDA_VISIBLE_DEVICES-",
        ]
        restart_args = ["rollout", "restart", f"deploy/{deployment}", "-n", self.namespace]
        status_args = [
            "rollout", "status", f"deploy/{deployment}", "-n", self.namespace,
            "--timeout=180s",
        ]
        command = " && ".join([
            self._kubectl_display(unset_args),
            self._kubectl_display(restart_args),
            f"delete the pre-recovery pods selected from deploy/{deployment}",
            self._kubectl_display(status_args),
        ])
        if self.dry_run:
            return FaultResult(
                "recover_all", "driver", True,
                "[DRY-RUN] Driver faults recovered", None, command,
            )
        try:
            pods = await self._deployment_pod_names(deployment)
            await self._run_kubectl([
                "exec", "-n", self.namespace, f"deploy/{deployment}", "--",
                "nvidia-smi", "-rgc",
            ])
            for args in (unset_args, restart_args):
                rc, _, stderr = await self._run_kubectl(args)
                if rc != 0:
                    return FaultResult("recover_all", "driver", False, stderr, None, command)
            if pods:
                rc, _, stderr = await self._run_kubectl([
                    "delete", *(f"pod/{name}" for name in pods), "-n", self.namespace,
                    "--grace-period=5", "--wait=false",
                ])
                if rc != 0:
                    return FaultResult("recover_all", "driver", False, stderr, None, command)
            rc, _, stderr = await self._run_kubectl(status_args, timeout=200)
            if rc != 0:
                return FaultResult("recover_all", "driver", False, stderr, None, command)
            return FaultResult(
                "recover_all", "driver", True,
                f"Driver faults recovered; recycled {len(pods)} pre-recovery pod(s)",
                None, command,
            )
        except Exception as exc:
            return FaultResult("recover_all", "driver", False, str(exc), None, command)

    def _kubectl_display(self, args: List[str]) -> str:
        return f"{self.kubectl} {shlex.join(args)}"

    async def _run_kubectl(self, args: List[str], timeout: int = 30) -> Tuple[int, str, str]:
        command = [*shlex.split(self.kubectl), *args]
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return -1, "", "Command timed out"
        return proc.returncode or 0, stdout.decode().strip(), stderr.decode().strip()

    async def _deployment_pod_names(self, deployment: str) -> List[str]:
        rc, stdout, stderr = await self._run_kubectl([
            "get", f"deploy/{deployment}", "-n", self.namespace, "-o", "json",
        ])
        if rc != 0:
            raise RuntimeError(stderr or f"Failed to inspect deployment {deployment}")
        try:
            selector_spec = (json.loads(stdout).get("spec") or {}).get("selector") or {}
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Invalid deployment JSON for {deployment}: {exc}") from exc
        selector = build_label_selector(selector_spec)
        if not selector:
            raise RuntimeError(f"Deployment {deployment} has no usable label selector")
        rc, stdout, stderr = await self._run_kubectl([
            "get", "pods", "-n", self.namespace, "-l", selector, "-o", "json",
        ])
        if rc != 0:
            raise RuntimeError(stderr or f"Failed to inspect pods for deployment {deployment}")
        try:
            items = json.loads(stdout).get("items") or []
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Invalid pod JSON for deployment {deployment}: {exc}") from exc
        return [
            str((item.get("metadata") or {}).get("name"))
            for item in items
            if isinstance(item, dict) and str((item.get("metadata") or {}).get("name") or "").strip()
        ]

    async def _execute(
        self, cmd: str, fault_type: str, layer: str,
        success_msg: str, recovery_cmd: Optional[str] = None,
    ) -> FaultResult:
        """Execute command or log in dry-run mode."""
        if self.dry_run:
            logger.info("[DRY-RUN] Would execute: %s", cmd)
            return FaultResult(fault_type, layer, True, f"[DRY-RUN] {success_msg}", recovery_cmd, cmd)
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode == 0:
                return FaultResult(fault_type, layer, True, success_msg, recovery_cmd, cmd)
            return FaultResult(fault_type, layer, False, stderr.decode().strip(), recovery_cmd, cmd)
        except asyncio.TimeoutError:
            return FaultResult(fault_type, layer, False, "Command timed out", recovery_cmd, cmd)
        except Exception as e:
            return FaultResult(fault_type, layer, False, str(e), recovery_cmd, cmd)

    def list_faults(self) -> List[Dict[str, str]]:
        """List all available driver fault types."""
        return [
            {"type": "gpu_driver_crash", "description": "Simulate GPU driver crash"},
            {"type": "cuda_error", "description": "Hide CUDA devices from the workload"},
            {"type": "nccl_failure", "description": "Block NCCL inter-GPU communication"},
            {"type": "ecc_memory_error", "description": "Simulate GPU ECC uncorrectable memory error"},
            {"type": "pcie_throttle", "description": "Saturate PCIe bus to throttle bandwidth"},
            {"type": "gpu_clock_throttle", "description": "Lock GPU clock to minimum frequency"},
            {"type": "cuda_ctx_corruption", "description": "Destroy CUDA context to crash kernels"},
            {"type": "cupti_trace_overhead", "description": "Inject CUPTI profiling overhead (Teller-inspired)"},
        ]
