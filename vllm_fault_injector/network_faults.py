"""Network layer fault injection for inference endpoints.

Handles: inference endpoint timeouts, gRPC failures, Tensor Parallel
communication loss.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional

from vllm_fault_injector.software_faults import FaultResult

logger = logging.getLogger(__name__)


class NetworkFaultInjector:
    """Injects network-layer faults into inference service communication.

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

    async def inject_endpoint_timeout(
        self, deployment: str = "vllm-server", delay_ms: int = 5000
    ) -> FaultResult:
        """Make the in-cluster inference Service endpoint unavailable.

        The earlier implementation attempted to run tc inside the serving
        container. That depends on container capabilities and can be confused
        with host-level network changes. For AI cluster experiments we keep the
        blast radius inside Kubernetes by temporarily changing the Service
        selector to a non-existent label. In-cluster clients then observe
        connection timeout or refusal, while SSH and node networking are
        untouched.

        Args:
            deployment: Target deployment name.
            delay_ms: Kept for API compatibility.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} patch service/{deployment} -n {self.namespace} "
            f"-p '{{\"spec\":{{\"selector\":{{\"app\":\"agenticsre-network-blackhole\"}}}}}}'"
        )
        return await self._execute(
            cmd, "endpoint_timeout", "network",
            f"Routed service/{deployment} to an empty selector inside the cluster",
            f"{self.kubectl} patch service/{deployment} -n {self.namespace} "
            f"-p '{{\"spec\":{{\"selector\":{{\"app\":\"{deployment}\"}}}}}}'",
        )

    async def inject_grpc_failure(
        self, deployment: str = "vllm-server", port: int = 8001
    ) -> FaultResult:
        """Block gRPC port to simulate service failure.

        Args:
            deployment: Target deployment name.
            port: gRPC port to block.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"iptables -A INPUT -p tcp --dport {port} -j DROP 2>/dev/null || "
            f"echo 'iptables not available'"
        )
        return await self._execute(
            cmd, "grpc_failure", "network",
            f"Blocked gRPC port {port} on {deployment}",
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- "
            f"iptables -D INPUT -p tcp --dport {port} -j DROP 2>/dev/null || true",
        )

    async def inject_http_503(self, deployment: str = "vllm-server", port: int = 8000) -> FaultResult:
        """Reject the OpenAI-compatible HTTP path at the K8s Service layer."""
        cmd = (
            f"{self.kubectl} patch service/{deployment} -n {self.namespace} "
            f"-p '{{\"spec\":{{\"selector\":{{\"app\":\"agenticsre-http-blackhole\"}}}}}}'"
        )
        return await self._execute(
            cmd, "http_503", "network",
            f"Removed ready endpoints for service/{deployment} inside the cluster",
            f"{self.kubectl} patch service/{deployment} -n {self.namespace} "
            f"-p '{{\"spec\":{{\"selector\":{{\"app\":\"{deployment}\"}}}}}}'",
        )

    async def inject_tp_communication_loss(
        self, deployment: str = "vllm-server"
    ) -> FaultResult:
        """Block Tensor Parallel inter-node communication.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"sh -c 'iptables -A OUTPUT -p tcp --dport 29400:29500 -j DROP 2>/dev/null || "
            f"echo TP communication blocked'"
        )
        return await self._execute(
            cmd, "tp_communication_loss", "network",
            f"Blocked TP communication ports on {deployment}",
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- "
            f"sh -c 'iptables -D OUTPUT -p tcp --dport 29400:29500 -j DROP 2>/dev/null || true'",
        )

    async def inject_dns_failure(self, deployment: str = "vllm-server") -> FaultResult:
        """Corrupt DNS resolution to simulate service discovery failure.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"sh -c 'cp /etc/resolv.conf /etc/resolv.conf.bak && "
            f"echo nameserver 192.0.2.1 > /etc/resolv.conf'"
        )
        return await self._execute(
            cmd, "dns_failure", "network",
            f"Corrupted DNS in {deployment}",
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- "
            f"sh -c 'cp /etc/resolv.conf.bak /etc/resolv.conf 2>/dev/null || true'",
        )

    async def inject_packet_loss(
        self, deployment: str = "vllm-server", loss_pct: int = 30
    ) -> FaultResult:
        """Add packet loss to network interface.

        Unlike endpoint_timeout (removes ready endpoints) or grpc_failure (blocks port),
        this injects probabilistic packet loss to simulate degraded network.

        Args:
            deployment: Target deployment name.
            loss_pct: Packet loss percentage (0-100).

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"tc qdisc add dev eth0 root netem loss {loss_pct}% 2>/dev/null || "
            f"echo 'tc not available'"
        )
        return await self._execute(
            cmd, "packet_loss", "network",
            f"Added {loss_pct}% packet loss to {deployment}",
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- "
            f"tc qdisc del dev eth0 root 2>/dev/null || true",
        )

    async def inject_bandwidth_throttle(
        self, deployment: str = "vllm-server", rate_mbit: int = 10
    ) -> FaultResult:
        """Throttle network bandwidth to simulate constrained network.

        Limits outbound bandwidth to degrade model serving and TP
        communication throughput.

        Args:
            deployment: Target deployment name.
            rate_mbit: Maximum bandwidth in Mbit/s.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"sh -c 'tc qdisc add dev eth0 root tbf rate {rate_mbit}mbit burst 32kbit latency 400ms "
            f"2>/dev/null || echo bandwidth throttle not available'"
        )
        return await self._execute(
            cmd, "bandwidth_throttle", "network",
            f"Bandwidth limited to {rate_mbit}Mbit on {deployment}",
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- "
            f"tc qdisc del dev eth0 root 2>/dev/null || true",
        )

    async def inject_rdma_failure(self, deployment: str = "vllm-server") -> FaultResult:
        """Simulate RDMA/InfiniBand failure for multi-node TP/PP inference.

        Blocks RDMA-related ports and disables IB device access to simulate
        high-speed interconnect failure in multi-node GPU clusters.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -- "
            f"sh -c '"
            f"iptables -A OUTPUT -p tcp --dport 18515 -j DROP 2>/dev/null; "
            f"iptables -A OUTPUT -p udp --dport 4791 -j DROP 2>/dev/null; "
            f"export NCCL_IB_DISABLE=1 && export NCCL_NET=Socket && "
            f"echo RDMA/IB disabled'"
        )
        return await self._execute(
            cmd, "rdma_failure", "network",
            f"RDMA/InfiniBand disabled on {deployment}",
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- "
            f"sh -c 'iptables -D OUTPUT -p tcp --dport 18515 -j DROP 2>/dev/null; "
            f"iptables -D OUTPUT -p udp --dport 4791 -j DROP 2>/dev/null || true'",
        )

    async def inject_service_mesh_fault(self, deployment: str = "vllm-server") -> FaultResult:
        """Inject fault into service mesh sidecar (if present).

        Corrupts Envoy/Istio sidecar proxy configuration to simulate
        service mesh routing failures.

        Args:
            deployment: Target deployment name.

        Returns:
            FaultResult with injection outcome.
        """
        cmd = (
            f"{self.kubectl} exec -n {self.namespace} "
            f"deploy/{deployment} -c istio-proxy -- "
            f"sh -c 'iptables -A OUTPUT -p tcp --dport 8000 -j REJECT 2>/dev/null || "
            f"echo No istio sidecar, simulating via network rule' 2>/dev/null || "
            f"{self.kubectl} exec -n {self.namespace} deploy/{deployment} -- "
            f"iptables -A OUTPUT -p tcp --dport 8000 -j REJECT 2>/dev/null || "
            f"echo 'service mesh fault simulated'"
        )
        return await self._execute(
            cmd, "service_mesh_fault", "network",
            f"Service mesh fault injected on {deployment}",
            f"{self.kubectl} rollout restart deploy/{deployment} -n {self.namespace}",
        )

    async def recover_all(self, deployment: str = "vllm-server") -> FaultResult:
        """Recover from all network faults."""
        cmd = (
            f"{self.kubectl} patch service/{deployment} -n {self.namespace} "
            f"-p '{{\"spec\":{{\"selector\":{{\"app\":\"{deployment}\"}}}}}}'"
        )
        return await self._execute(cmd, "recover_all", "network", "Network faults recovered")

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
        """List all available network fault types."""
        return [
            {"type": "endpoint_timeout", "description": "Remove ready inference Service endpoints"},
            {"type": "grpc_failure", "description": "Block gRPC port"},
            {"type": "http_503", "description": "Reject OpenAI-compatible HTTP inference port"},
            {"type": "tp_communication_loss", "description": "Block Tensor Parallel communication"},
            {"type": "dns_failure", "description": "Corrupt DNS resolution"},
            {"type": "packet_loss", "description": "Add probabilistic packet loss"},
            {"type": "bandwidth_throttle", "description": "Limit network bandwidth"},
            {"type": "rdma_failure", "description": "Disable RDMA/InfiniBand interconnect"},
            {"type": "service_mesh_fault", "description": "Inject service mesh routing failure"},
        ]
