"""Approval-gated executors for registered fine-grained fault experiments.

The public contract contains no shell, command, source, or expression fields.
Each scenario ID is resolved again on the server to one fixed adapter.  An
adapter performs a complete baseline -> inject -> observe -> undo -> verify
transaction and must emit machine-readable evidence ending in
``validation=PASS`` before it is considered successful.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence


class PrecisionExecutionError(ValueError):
    """Raised when a reviewed fine-grained adapter cannot be used safely."""


@dataclass(frozen=True)
class PrecisionAdapterSpec:
    scenario_id: str
    profile: str
    method: str
    runner: str
    runner_args: tuple[str, ...] = ()
    environment: str = "kubernetes"
    required_tools: tuple[str, ...] = ("kubectl",)
    cleanup_namespaces: tuple[str, ...] = ()
    evidence_keys: tuple[str, ...] = ("validation",)
    timeout_s: int = 240

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "profile": self.profile,
            "method": self.method,
            "environment": self.environment,
            "required_tools": list(self.required_tools),
            "cleanup_namespaces": list(self.cleanup_namespaces),
            "evidence_keys": list(self.evidence_keys),
            "timeout_s": self.timeout_s,
            "execution_mode": "reviewed_transactional_experiment",
            "real_execution_ready": True,
        }


def _spec(
    scenario_id: str,
    method: str,
    runner: str,
    *,
    runner_args: Sequence[str] = (),
    environment: str = "kubernetes",
    required_tools: Sequence[str] = ("kubectl",),
    cleanup_namespaces: Sequence[str] = (),
    evidence_keys: Sequence[str] = ("validation",),
    timeout_s: int = 240,
) -> PrecisionAdapterSpec:
    return PrecisionAdapterSpec(
        scenario_id=scenario_id,
        profile=f"agenticsre.precision.{scenario_id.removeprefix('precise-').replace('-', '_')}.v1",
        method=method,
        runner=runner,
        runner_args=tuple(runner_args),
        environment=environment,
        required_tools=tuple(required_tools),
        cleanup_namespaces=tuple(cleanup_namespaces),
        evidence_keys=tuple(evidence_keys),
        timeout_s=timeout_s,
    )


_PLATFORM_RUNNER = "scripts/validate_platform_native_faults.sh"
_NATIVE_RUNNER = "scripts/validate_native_function_scenario.sh"

PRECISION_ADAPTERS: Dict[str, PrecisionAdapterSpec] = {
    "precise-http-header-delay": _spec(
        "precise-http-header-delay", "request",
        "scripts/validate_httpchaos_request_precision.sh",
        cleanup_namespaces=("agenticsre-http-validation",), timeout_s=180,
    ),
    "precise-strace-openat-error": _spec(
        "precise-strace-openat-error", "strace",
        "scripts/validate_strace_syscall_precision.sh",
        required_tools=("kubectl", "ssh", "strace"),
        cleanup_namespaces=("agenticsre-strace-validation",), timeout_s=180,
    ),
    "precise-seccomp-openat-errno": _spec(
        "precise-seccomp-openat-errno", "seccomp",
        "scripts/validate_seccomp_notify_precision.sh",
        required_tools=("kubectl", "gcc"),
        cleanup_namespaces=("agenticsre-seccomp-validation",), timeout_s=180,
    ),
    "precise-ebpf-connect-denial": _spec(
        "precise-ebpf-connect-denial", "ebpf",
        "scripts/validate_ebpf_network_precision.sh",
        required_tools=("kubectl", "ssh", "scp", "bpftool", "clang"),
        cleanup_namespaces=("agenticsre-ebpf-validation",), timeout_s=240,
    ),
    "precise-jvm-method-latency": _spec(
        "precise-jvm-method-latency", "bytecode",
        "scripts/validate_jvmchaos_bytecode_precision.sh",
        required_tools=("kubectl", "javac"),
        cleanup_namespaces=("agenticsre-bytecode-validation",), timeout_s=240,
    ),
    "precise-gdb-function-return": _spec(
        "precise-gdb-function-return", "gdb", _NATIVE_RUNNER,
        runner_args=("precise-gdb-function-return",),
        required_tools=("kubectl", "ssh", "scp", "gcc", "gdb", "readelf"),
        cleanup_namespaces=("agenticsre-c-native-validation",), timeout_s=240,
    ),
    "precise-elf-function-return": _spec(
        "precise-elf-function-return", "binary", _NATIVE_RUNNER,
        runner_args=("precise-elf-function-return",),
        required_tools=("kubectl", "gcc"),
        cleanup_namespaces=("agenticsre-c-native-validation",), timeout_s=180,
    ),
    "precise-elf-function-errno": _spec(
        "precise-elf-function-errno", "binary", _NATIVE_RUNNER,
        runner_args=("precise-elf-function-errno",),
        required_tools=("kubectl", "gcc"),
        cleanup_namespaces=("agenticsre-c-native-validation",), timeout_s=180,
    ),
    "precise-elf-function-delay": _spec(
        "precise-elf-function-delay", "binary", _NATIVE_RUNNER,
        runner_args=("precise-elf-function-delay",),
        required_tools=("kubectl", "gcc"),
        cleanup_namespaces=("agenticsre-c-native-validation",), timeout_s=180,
    ),
    "precise-network-loss": _spec(
        "precise-network-loss", "platform_native", _PLATFORM_RUNNER,
        runner_args=("precise-network-loss",),
        cleanup_namespaces=("agenticsre-platform-chaos-validation",), timeout_s=240,
    ),
    "precise-dns-error": _spec(
        "precise-dns-error", "platform_native", _PLATFORM_RUNNER,
        runner_args=("precise-dns-error",),
        cleanup_namespaces=("agenticsre-platform-chaos-validation",), timeout_s=240,
    ),
    "precise-container-cpu-stress": _spec(
        "precise-container-cpu-stress", "platform_native", _PLATFORM_RUNNER,
        runner_args=("precise-container-cpu-stress",),
        cleanup_namespaces=("agenticsre-platform-chaos-validation",), timeout_s=240,
    ),
    "precise-path-io-latency": _spec(
        "precise-path-io-latency", "platform_native", _PLATFORM_RUNNER,
        runner_args=("precise-path-io-latency",),
        cleanup_namespaces=("agenticsre-platform-chaos-validation",), timeout_s=300,
    ),
    "precise-container-time-offset": _spec(
        "precise-container-time-offset", "platform_native", _PLATFORM_RUNNER,
        runner_args=("precise-container-time-offset",),
        cleanup_namespaces=("agenticsre-platform-chaos-validation",), timeout_s=240,
    ),
    "precise-application-failpoint-error": _spec(
        "precise-application-failpoint-error", "failpoint",
        "scripts/validate_application_failpoint_precision.py",
        environment="local_or_application", required_tools=("python3",), timeout_s=60,
    ),
    "precise-cgroup-v2-cpu-quota": _spec(
        "precise-cgroup-v2-cpu-quota", "cgroup_v2",
        "scripts/validate_cgroup_v2_cpu_quota.sh",
        required_tools=("kubectl", "ssh"),
        cleanup_namespaces=("agenticsre-cgroup-validation",), timeout_s=180,
    ),
    "precise-dcgm-pcie-replay": _spec(
        "precise-dcgm-pcie-replay", "dcgm",
        "scripts/validate_dcgm_pcie_replay.sh",
        environment="gpu_kubernetes", required_tools=("kubectl", "dcgmi"),
        cleanup_namespaces=("agenticsre-dcgm-validation",), timeout_s=240,
    ),
    "precise-dm-flakey-write-error": _spec(
        "precise-dm-flakey-write-error", "dm_flakey",
        "scripts/validate_dm_flakey_write_error.sh",
        required_tools=("kubectl", "ssh", "dmsetup", "losetup"),
        cleanup_namespaces=("agenticsre-storage-validation",), timeout_s=240,
    ),
}


def precision_adapter(scenario_id: str) -> PrecisionAdapterSpec:
    try:
        return PRECISION_ADAPTERS[str(scenario_id)]
    except KeyError as exc:
        raise PrecisionExecutionError(f"未登记的细粒度执行适配器: {scenario_id}") from exc


def precision_adapter_catalog() -> list[Dict[str, Any]]:
    return [PRECISION_ADAPTERS[key].to_dict() for key in sorted(PRECISION_ADAPTERS)]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def precision_adapter_preflight(scenario_id: str, *, root_dir: Path) -> Dict[str, Any]:
    spec = precision_adapter(scenario_id)
    runner = (root_dir / spec.runner).resolve()
    expected_root = root_dir.resolve()
    if expected_root not in runner.parents:
        raise PrecisionExecutionError("执行适配器路径越界")
    blockers: list[str] = []
    if not runner.is_file():
        blockers.append(f"固定执行适配器不存在: {spec.runner}")
    return {
        "status": "ready" if not blockers else "blocked",
        "ready": not blockers,
        "scenario_id": scenario_id,
        "adapter": spec.to_dict(),
        "reviewed_asset": {
            "path": spec.runner,
            "sha256": _sha256(runner) if runner.is_file() else "",
        },
        "blockers": blockers,
        "checks": [
            "服务端场景与适配器一一映射",
            "真实执行需要 confirm=true 与 experiment_ack=true",
            "适配器必须输出故障前、故障中、Undo 后证据",
            "validation=PASS 且残留检查通过后才记为成功",
        ],
    }


_EVIDENCE_KEY = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


def parse_precision_evidence(stdout: str) -> Dict[str, str]:
    evidence: Dict[str, str] = {}
    for raw_line in str(stdout or "").splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if _EVIDENCE_KEY.fullmatch(key):
            evidence[key] = value.strip()
    return evidence


def execute_precision_adapter(
    scenario_id: str,
    *,
    root_dir: Path,
    dry_run: bool = True,
    confirmed: bool = False,
    experiment_ack: bool = False,
    env: Optional[Mapping[str, str]] = None,
    runner: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
) -> Dict[str, Any]:
    spec = precision_adapter(scenario_id)
    preflight = precision_adapter_preflight(scenario_id, root_dir=root_dir)
    if not preflight["ready"]:
        raise PrecisionExecutionError("; ".join(preflight["blockers"]))
    if not dry_run and not confirmed:
        raise PrecisionExecutionError("真实细粒度故障实验需要 confirm=true")
    if not dry_run and not experiment_ack:
        raise PrecisionExecutionError("真实细粒度故障实验需要 experiment_ack=true")

    executable = (root_dir / spec.runner).resolve()
    argv = [str(executable), *spec.runner_args]
    result: Dict[str, Any] = {
        "status": "dry_run" if dry_run else "running",
        "scenario_id": scenario_id,
        "adapter": spec.to_dict(),
        "reviewed_asset": preflight["reviewed_asset"],
        "transaction": {
            "stages": ["preflight", "baseline", "inject", "effect_verify", "undo", "recovery_verify", "residue_check"],
            "auto_cleanup": True,
            "cleanup_namespaces": list(spec.cleanup_namespaces),
        },
    }
    if dry_run:
        return result

    completed = (runner or subprocess.run)(
        argv,
        cwd=str(root_dir),
        text=True,
        capture_output=True,
        timeout=spec.timeout_s,
        check=False,
        env={**os.environ, **dict(env or {})},
    )
    evidence = parse_precision_evidence(completed.stdout)
    validation = evidence.get("validation", "")
    succeeded = completed.returncode == 0 and validation.startswith("PASS")
    result.update({
        "status": "succeeded" if succeeded else "failed",
        "returncode": completed.returncode,
        "stdout": (completed.stdout or "")[-12000:],
        "stderr": (completed.stderr or "")[-8000:],
        "evidence": evidence,
        "verification": {
            "status": "passed" if succeeded else "failed",
            "validation": validation,
            "cleanup_complete": "cleanup=complete" in validation,
        },
    })
    if not succeeded:
        detail = (completed.stderr or completed.stdout or "细粒度执行适配器未返回 PASS").strip()[-4000:]
        raise PrecisionExecutionError(detail)
    return result
