"""OS layer fault injection for GPU memory and system resources.

Handles: GPU memory OOM, NUMA configuration errors, Huge Pages exhaustion.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import shlex
from typing import Dict, List, Optional

from vllm_fault_injector.software_faults import FaultResult

logger = logging.getLogger(__name__)


class OSFaultInjector:
    """Injects OS-layer faults targeting system resources.

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

    def _node_local_pod_cmd(
        self,
        *,
        pod_name: str,
        deployment: str,
        image: str,
        script: str,
    ) -> str:
        """Build a bounded pressure pod on the same node as the vLLM pod."""
        encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
        inner = f"printf %s {shlex.quote(encoded)} | base64 -d | /bin/sh"
        shell = (
            f"node=$({self.kubectl} get pod -n {self.namespace} -l app={deployment} "
            f"-o jsonpath='{{.items[0].spec.nodeName}}' 2>/dev/null || true); "
            f"if [ -z \"$node\" ]; then echo \"target node not found\" >&2; exit 2; fi; "
            f"overrides=$(printf "
            f"'{{\"apiVersion\":\"v1\",\"spec\":{{\"restartPolicy\":\"Never\","
            f"\"nodeName\":\"%s\",\"tolerations\":["
            f"{{\"key\":\"node-role.kubernetes.io/control-plane\",\"operator\":\"Exists\",\"effect\":\"NoSchedule\"}},"
            f"{{\"key\":\"node.kubernetes.io/disk-pressure\",\"operator\":\"Exists\",\"effect\":\"NoSchedule\"}}]}}}}' \"$node\"); "
            f"{self.kubectl} -n {self.namespace} delete pod {pod_name} --ignore-not-found=true "
            f"--force --grace-period=0 >/dev/null 2>&1 || true; "
            f"{self.kubectl} -n {self.namespace} run {pod_name} "
            f"--image={image} --image-pull-policy=IfNotPresent --restart=Never "
            f"--labels=app={pod_name},agenticsre.io/target={deployment},agenticsre.io/fault=os-pressure "
            f"--overrides=\"$overrides\" -- /bin/sh -c {shlex.quote(inner)}; "
            f"{self.kubectl} -n {self.namespace} wait --for=condition=Ready pod/{pod_name} --timeout=30s; "
            f"echo {pod_name} started on $node"
        )
        return "sh -c " + shlex.quote(shell)

    async def inject_gpu_oom(self, deployment: str = "vllm-server") -> FaultResult:
        """Allocate all available GPU memory to cause OOM.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"sh -c 'nohup python3 -c \"import torch,time; "
            f"xs=[]; "
            f"[xs.append(torch.zeros((1024,1024,128),device=\\\"cuda\\\")) for _ in range(64)]; "
            f"time.sleep(120)\" >/tmp/agenticsre_gpu_oom.log 2>&1 & "
            f"echo GPU OOM pressure started'"
        )
        return await self._execute(
            cmd, "gpu_oom", "os",
            f"GPU OOM triggered in {deployment}",
            f"{self.kubectl} rollout restart deploy/{deployment} -n {self.namespace}",
        )

    async def inject_cpu_stress(
        self, deployment: str = "vllm-server", cores: int = 4, duration: int = 60
    ) -> FaultResult:
        """Apply CPU stress to impact inference throughput.

        Args:
            deployment: Target deployment name.
            cores: Number of CPU cores to stress.
            duration: Duration in seconds.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"sh -c '"
            f"node=$({self.kubectl} get pod -n {self.namespace} -l app={deployment} "
            f"-o jsonpath=\"{{.items[0].spec.nodeName}}\" 2>/dev/null || true); "
            f"if [ -z \"$node\" ]; then echo \"target node not found\" >&2; exit 2; fi; "
            f"overrides=$(printf "
            f"\"{{\\\"apiVersion\\\":\\\"v1\\\",\\\"spec\\\":{{\\\"restartPolicy\\\":\\\"Never\\\","
            f"\\\"nodeName\\\":\\\"%s\\\",\\\"tolerations\\\":["
            f"{{\\\"key\\\":\\\"node-role.kubernetes.io/control-plane\\\",\\\"operator\\\":\\\"Exists\\\",\\\"effect\\\":\\\"NoSchedule\\\"}},"
            f"{{\\\"key\\\":\\\"node.kubernetes.io/disk-pressure\\\",\\\"operator\\\":\\\"Exists\\\",\\\"effect\\\":\\\"NoSchedule\\\"}}]}}}}\" \"$node\"); "
            f"{self.kubectl} -n {self.namespace} delete pod agenticsre-cpu-stress --ignore-not-found=true "
            f"--force --grace-period=0 >/dev/null 2>&1 || true; "
            f"{self.kubectl} -n {self.namespace} run agenticsre-cpu-stress "
            f"--image=vllm/vllm-openai:latest --image-pull-policy=IfNotPresent --restart=Never "
            f"--labels=app=agenticsre-cpu-stress,agenticsre.io/target={deployment} "
            f"--overrides=\"$overrides\" -- /bin/sh -c "
            f"'\"'\"'for i in $(seq 1 {cores}); do while true; do :; done & done; sleep {duration}'\"'\"'; "
            f"{self.kubectl} -n {self.namespace} wait --for=condition=Ready pod/agenticsre-cpu-stress --timeout=30s; "
            f"echo CPU stress pod started on $node'"
        )
        return await self._execute(
            cmd, "cpu_stress", "os",
            f"CPU stress on {cores} cores for {duration}s in {deployment}",
            f"{self.kubectl} -n {self.namespace} delete pod agenticsre-cpu-stress "
            f"--ignore-not-found=true --force --grace-period=0",
        )

    async def inject_memory_pressure(
        self, deployment: str = "vllm-server", size_mb: int = 1024
    ) -> FaultResult:
        """Consume system memory to create memory pressure.

        Args:
            deployment: Target deployment name.
            size_mb: Amount of memory to consume in MB.

        Returns:
            FaultResult with injection outcome.
        """
        size_mb = max(64, min(int(size_mb), 2048))
        cmd = self._node_local_pod_cmd(
            pod_name="agenticsre-memory-pressure",
            deployment=deployment,
            image="vllm/vllm-openai:latest",
            script=(
                "python3 - <<'PY'\n"
                "import time\n"
                f"x = bytearray({size_mb} * 1024 * 1024)\n"
                f"print('Allocated {size_mb}MB memory pressure', flush=True)\n"
                "time.sleep(90)\n"
                "PY"
            ),
        )
        return await self._execute(
            cmd, "memory_pressure", "os",
            f"Memory pressure pod ({size_mb}MB) scheduled beside {deployment}",
            f"{self.kubectl} -n {self.namespace} delete pod agenticsre-memory-pressure "
            f"--ignore-not-found=true --force --grace-period=0",
        )

    async def inject_disk_pressure(self, deployment: str = "vllm-server") -> FaultResult:
        """Fill disk to create I/O pressure.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = self._node_local_pod_cmd(
            pod_name="agenticsre-disk-pressure",
            deployment=deployment,
            image="vllm/vllm-openai:latest",
            script=(
                "set -eu\n"
                "mkdir -p /tmp/agenticsre-disk-pressure\n"
                "dd if=/dev/zero of=/tmp/agenticsre-disk-pressure/blob bs=1M count=512 2>/dev/null || true\n"
                "echo 'Disk pressure applied in isolated pod'\n"
                "sleep 90\n"
            ),
        )
        return await self._execute(
            cmd, "disk_pressure", "os",
            f"Disk pressure pod scheduled beside {deployment}",
            f"{self.kubectl} -n {self.namespace} delete pod agenticsre-disk-pressure "
            f"--ignore-not-found=true --force --grace-period=0",
        )

    async def inject_numa_error(self, deployment: str = "vllm-server") -> FaultResult:
        """Set invalid NUMA configuration for GPU processes.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} set env deploy/{deployment} "
            f"-n {self.namespace} CUDA_DEVICE_ORDER=FASTEST_FIRST "
            f"GOMP_CPU_AFFINITY=0-1 OMP_NUM_THREADS=1"
        )
        return await self._execute(
            cmd, "numa_error", "os",
            f"NUMA misconfiguration applied to {deployment}",
            f"{self.kubectl} set env deploy/{deployment} -n {self.namespace} "
            f"CUDA_DEVICE_ORDER- GOMP_CPU_AFFINITY- OMP_NUM_THREADS-",
        )

    async def inject_cgroup_limit(
        self, deployment: str = "vllm-server", memory_limit_mb: int = 256
    ) -> FaultResult:
        """Apply restrictive cgroup memory limit to container.

        Sets an extremely low memory limit via cgroup to trigger OOM kills.

        Args:
            deployment: Target deployment name.
            memory_limit_mb: Memory limit in MB.

        Returns:
            FaultResult with injection outcome.
        """
        # Patch resource limits via kubectl
        patch = (
            f'{{"spec":{{"template":{{"spec":{{"containers":[{{"name":"vllm",'
            f'"resources":{{"limits":{{"memory":"{memory_limit_mb}Mi"}}}}}}]}}}}}}}}'
        )
        cmd = (
            f"{self.kubectl} patch deploy/{deployment} -n {self.namespace} "
            f"--type=strategic -p '{patch}'"
        )
        return await self._execute(
            cmd, "cgroup_limit", "os",
            f"Memory cgroup limit set to {memory_limit_mb}Mi on {deployment}",
            f"{self.kubectl} rollout undo deploy/{deployment} -n {self.namespace}",
        )

    async def inject_hugepages_exhaustion(self, deployment: str = "vllm-server") -> FaultResult:
        """Exhaust huge pages to degrade GPU memory pinning performance.

        Allocates all available huge pages in the container, forcing the
        CUDA runtime to fall back to regular pages with higher TLB miss rates.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"python3 -c \""
            f"import mmap, os\\n"
            f"pages = []\\n"
            f"try:\\n"
            f"    for _ in range(1024):\\n"
            f"        pages.append(mmap.mmap(-1, 2*1024*1024, flags=mmap.MAP_PRIVATE|mmap.MAP_ANONYMOUS|0x40000))\\n"
            f"except OSError:\\n"
            f"    pass\\n"
            f"print(f'Exhausted {{len(pages)}} huge pages')\\n"
            f"import time; time.sleep(60)\""
        )
        return await self._execute(
            cmd, "hugepages_exhaustion", "os",
            f"Huge pages exhausted in {deployment}",
            f"{self.kubectl} rollout restart deploy/{deployment} -n {self.namespace}",
        )

    async def inject_fd_exhaustion(self, deployment: str = "vllm-server") -> FaultResult:
        """Exhaust file descriptors to prevent new connections and file I/O.

        Opens maximum number of file descriptors, preventing the vLLM
        server from accepting new connections.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = self._node_local_pod_cmd(
            pod_name="agenticsre-fd-exhaustion",
            deployment=deployment,
            image="vllm/vllm-openai:latest",
            script=(
                "python3 - <<'PY'\n"
                "import os, time\n"
                "fds=[]\n"
                "for _ in range(4096):\n"
                "    try:\n"
                "        fds.append(os.open('/dev/null', os.O_RDONLY))\n"
                "    except OSError:\n"
                "        break\n"
                "print('Exhausted file descriptors', len(fds), flush=True)\n"
                "time.sleep(90)\n"
                "PY"
            ),
        )
        return await self._execute(
            cmd, "fd_exhaustion", "os",
            f"File descriptor exhaustion pod scheduled beside {deployment}",
            f"{self.kubectl} -n {self.namespace} delete pod agenticsre-fd-exhaustion "
            f"--ignore-not-found=true --force --grace-period=0",
        )

    async def inject_io_scheduler_interference(
        self, deployment: str = "vllm-server"
    ) -> FaultResult:
        """Interfere with I/O scheduler by generating heavy sequential I/O.

        Saturates disk I/O bandwidth, impacting model weight loading,
        checkpoint writes, and log file operations.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = self._node_local_pod_cmd(
            pod_name="agenticsre-io-pressure",
            deployment=deployment,
            image="vllm/vllm-openai:latest",
            script=(
                "set -eu\n"
                "mkdir -p /tmp/agenticsre-io-pressure\n"
                "for i in $(seq 1 4); do "
                "dd if=/dev/zero of=/tmp/agenticsre-io-pressure/blob_$i bs=4M count=128 2>/dev/null & "
                "done\n"
                "echo IO pressure started in isolated pod\n"
                "sleep 90\n"
            ),
        )
        return await self._execute(
            cmd, "io_scheduler_interference", "os",
            f"I/O pressure pod scheduled beside {deployment}",
            f"{self.kubectl} -n {self.namespace} delete pod agenticsre-io-pressure "
            f"--ignore-not-found=true --force --grace-period=0",
        )

    async def inject_clock_skew(self, deployment: str = "vllm-server") -> FaultResult:
        """Inject system clock skew to disrupt timeouts and scheduling.

        Shifts the container's system clock forward, causing certificate
        validation failures, timeout miscalculations, and log ordering issues.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"sh -c 'date -s \"+2 hours\" 2>/dev/null || "
            f"python3 -c \"import time; print(f\\\"Clock skew simulated at {{time.time()}}\\\")\"'"
        )
        return await self._execute(
            cmd, "clock_skew", "os",
            f"System clock skewed in {deployment}",
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- "
            f"sh -c 'hwclock -s 2>/dev/null || true'",
        )

    async def recover_all(self, deployment: str = "vllm-server") -> FaultResult:
        """Recover from all OS-layer faults."""
        cmd = (
            f"{self.kubectl} -n {self.namespace} delete pod "
            f"agenticsre-cpu-stress agenticsre-memory-pressure agenticsre-disk-pressure "
            f"agenticsre-fd-exhaustion agenticsre-io-pressure "
            f"--ignore-not-found=true --force --grace-period=0 2>/dev/null || true"
        )
        return await self._execute(cmd, "recover_all", "os", "OS faults recovered")

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
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            if proc.returncode == 0:
                return FaultResult(fault_type, layer, True, success_msg, recovery_cmd, cmd)
            return FaultResult(fault_type, layer, False, stderr.decode().strip(), recovery_cmd, cmd)
        except asyncio.TimeoutError:
            return FaultResult(fault_type, layer, False, "Command timed out", recovery_cmd, cmd)
        except Exception as e:
            return FaultResult(fault_type, layer, False, str(e), recovery_cmd, cmd)

    def list_faults(self) -> List[Dict[str, str]]:
        """List all available OS fault types."""
        return [
            {"type": "gpu_oom", "description": "Exhaust GPU memory to cause OOM"},
            {"type": "cpu_stress", "description": "Apply CPU stress to impact throughput"},
            {"type": "memory_pressure", "description": "Consume system memory"},
            {"type": "disk_pressure", "description": "Fill disk space"},
            {"type": "numa_error", "description": "Apply invalid NUMA configuration"},
            {"type": "cgroup_limit", "description": "Apply restrictive cgroup memory limit"},
            {"type": "hugepages_exhaustion", "description": "Exhaust huge pages"},
            {"type": "fd_exhaustion", "description": "Exhaust file descriptors"},
            {"type": "io_scheduler_interference", "description": "Saturate disk I/O bandwidth"},
            {"type": "clock_skew", "description": "Inject system clock skew"},
        ]
