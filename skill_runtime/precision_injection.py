"""Typed, non-executable contracts for fine-grained fault injection.

The runtime deliberately separates an Agent-authored intent from any backend
executor.  Contracts produced here contain only reviewed fields, bounded scope,
automatic cleanup requirements, and the evidence needed to prove isolation and
recovery.  This module never constructs or executes shell commands.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional


class PrecisionInjectionError(ValueError):
    """Raised when a precision fault request is ambiguous or unsafe."""


METHOD_ALIASES = {
    "native": "platform_native",
    "platform": "platform_native",
    "platform_native": "platform_native",
    "chaos_mesh": "platform_native",
    "request": "request",
    "request_level": "request",
    "strace": "strace",
    "ptrace": "strace",
    "syscall": "strace",
    "seccomp": "seccomp",
    "seccomp_user_notif": "seccomp",
    "ebpf": "ebpf",
    "e_bpf": "ebpf",
    "bytecode": "bytecode",
    "instrumentation": "bytecode",
    "gdb": "gdb",
    "debugger": "gdb",
    "binary": "binary",
    "elf": "binary",
    "ld_preload": "binary",
    "failpoint": "failpoint",
    "application_failpoint": "failpoint",
    "cgroup": "cgroup_v2",
    "cgroup_v2": "cgroup_v2",
    "dcgm": "dcgm",
    "nvidia_dcgm": "dcgm",
    "dm_flakey": "dm_flakey",
    "device_mapper_flakey": "dm_flakey",
}

METHOD_METADATA = {
    "platform_native": ("workload_or_resource", ["platform_event", "target_health"]),
    "request": ("matched_request", ["request_id", "route", "response_outcome"]),
    "strace": ("process_syscall", ["pid", "syscall", "errno_or_delay", "trace_summary"]),
    "seccomp": (
        "process_syscall_argument_hit",
        ["pid", "syscall", "argument_match", "notification_id", "injected_errno"],
    ),
    "ebpf": ("cgroup_network_tuple", ["cgroup_id", "attach_point", "probe_event"]),
    "bytecode": (
        "runtime_class_method",
        ["runtime", "module_or_class", "function", "instrumentation_event"],
    ),
    "gdb": (
        "native_process_symbol_hit",
        ["pid", "binary_build_id", "abi_profile", "symbol", "breakpoint_hit"],
    ),
    "binary": (
        "native_module_symbol_hit",
        ["pid", "binary_build_id", "abi_profile", "symbol", "instrumentation_hit"],
    ),
    "failpoint": (
        "matched_application_failpoint_hit",
        ["failpoint_id", "request_id", "activation_token", "hit_count"],
    ),
    "cgroup_v2": (
        "single_cgroup_controller",
        ["cgroup_path", "controller", "baseline_value", "applied_value", "restored_value"],
    ),
    "dcgm": (
        "single_gpu_field",
        ["gpu_identity", "field_id", "injected_value", "policy_event"],
    ),
    "dm_flakey": (
        "single_disposable_device_mapper_volume",
        ["mapping_name", "backing_device", "snapshot_id", "write_error", "cleanup_state"],
    ),
}

PLATFORM_FAULTS = {
    "network_loss": ("chaos_mesh.networkchaos", ["destination_entity_ids", "loss_percent"]),
    "dns_error": ("chaos_mesh.dnschaos", ["dns_patterns"]),
    "cpu_stress": ("chaos_mesh.stresschaos", ["container", "workers", "load_percent"]),
    "io_latency": ("chaos_mesh.iochaos", ["container", "volume_path", "path", "delay_ms"]),
    "time_offset": ("chaos_mesh.timechaos", ["container", "offset", "clock_ids"]),
}

_SAFE_TARGET = re.compile(
    r"^(?:deployment|service|pod|node|process|gpu|volume)/[A-Za-z0-9][A-Za-z0-9._:@-]{0,191}$"
)
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,191}$")
_SAFE_SYMBOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_:$@.]{0,191}$")
_SAFE_CLASS = re.compile(r"^[A-Za-z_$][A-Za-z0-9_.$/]{0,255}$")
_SAFE_SYSCALL = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_SAFE_ARTIFACT = re.compile(r"^agenticsre\.[a-z0-9][a-z0-9._-]{2,120}$")
_SAFE_FAILPOINT = re.compile(r"^[a-z][a-z0-9_.-]{2,127}$")
_SAFE_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SAFE_CGROUP = re.compile(r"^/sys/fs/cgroup/[A-Za-z0-9_.@:/-]{1,512}$")
_SAFE_GPU_UUID = re.compile(r"^(?:GPU-)?[A-Fa-f0-9][A-Fa-f0-9-]{7,63}$")
_SAFE_DM_NAME = re.compile(r"^agenticsre-[a-z0-9][a-z0-9-]{2,62}$")
_FORBIDDEN_KEYS = {
    "command", "commands", "shell", "script", "source", "expression",
    "python", "kubectl", "gdb_commands", "bpf_program",
}


def _dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _int(value: Any, field: str, minimum: int, maximum: int, *, default: Any = None) -> int:
    if value is None and default is not None:
        value = default
    if isinstance(value, bool) or not re.fullmatch(r"-?[0-9]+", str(value).strip()):
        raise PrecisionInjectionError(f"{field} must be an integer between {minimum} and {maximum}")
    result = int(value)
    if result < minimum or result > maximum:
        raise PrecisionInjectionError(f"{field} must be an integer between {minimum} and {maximum}")
    return result


def _float(value: Any, field: str, minimum: float, maximum: float, *, default: Any = None) -> float:
    if value is None and default is not None:
        value = default
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise PrecisionInjectionError(f"{field} must be between {minimum} and {maximum}") from None
    if result < minimum or result > maximum:
        raise PrecisionInjectionError(f"{field} must be between {minimum} and {maximum}")
    return result


def _contains_forbidden_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).strip().lower() in _FORBIDDEN_KEYS or _contains_forbidden_key(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set)):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def _reject_unknown_fields(value: Mapping[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(str(key) for key in value if str(key) not in allowed)
    if unknown:
        raise PrecisionInjectionError(f"{context} contains unsupported fields: {', '.join(unknown)}")


def _exact_path(value: Any, field: str) -> str:
    path = str(value or "").strip()
    if (
        not path.startswith("/")
        or ".." in path.split("/")
        or "//" in path
        or any(char in path for char in "*?[]{}")
        or len(path) > 4096
    ):
        raise PrecisionInjectionError(f"{field} must be one exact normalized absolute path")
    return path


def normalize_method(value: Any) -> str:
    key = str(value or "platform_native").strip().lower().replace("-", "_")
    method = METHOD_ALIASES.get(key, key)
    if method not in METHOD_METADATA:
        raise PrecisionInjectionError(f"unsupported injection_method '{value}'")
    return method


def _validate_request(match: Dict[str, Any], fault: Dict[str, Any], budget: Dict[str, Any]) -> None:
    allowed = {"route", "path", "http_method", "header", "request_id", "tenant_id", "sample_rate"}
    _reject_unknown_fields(match, allowed, "request match")
    _reject_unknown_fields(fault, {"type", "action", "delay_ms", "status_code"}, "request fault")
    selectors = [value for key, value in match.items() if key != "sample_rate" and value not in (None, "", {})]
    if not selectors:
        raise PrecisionInjectionError("request injection requires an exact route, header, tenant, or request_id matcher")
    if any(isinstance(value, str) and any(char in value for char in "*?[]{}") for value in selectors):
        raise PrecisionInjectionError("request selectors must be exact and cannot contain wildcards")
    route = str(match.get("route") or match.get("path") or "")
    if route and (not route.startswith("/") or ".." in route.split("/") or any(ord(char) < 32 for char in route)):
        raise PrecisionInjectionError("request route must be one exact normalized path")
    header = str(match.get("header") or "")
    if header and not re.fullmatch(r"[A-Za-z0-9-]{1,64}=[A-Za-z0-9._:@/-]{1,128}", header):
        raise PrecisionInjectionError("request header must be one exact name=value selector")
    method = str(match.get("http_method") or "GET").upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
        raise PrecisionInjectionError("request http_method is not allowlisted")
    match["http_method"] = method
    if match.get("sample_rate") is not None:
        sample_rate = _float(match.get("sample_rate"), "sample_rate", 0.000001, 1.0)
        maximum = _float(budget.get("max_request_sample_rate"), "max_request_sample_rate", 0.000001, 1.0, default=0.10)
        if sample_rate > maximum:
            raise PrecisionInjectionError(f"sample_rate={sample_rate} exceeds max_request_sample_rate={maximum}")
        match["sample_rate"] = sample_rate
    action = str(fault.get("action") or "delay").lower()
    if action not in {"delay", "abort"}:
        raise PrecisionInjectionError("request fault action must be delay or abort")
    fault["action"] = action
    if action == "delay":
        fault["delay_ms"] = _int(fault.get("delay_ms"), "delay_ms", 1, 10_000)
    else:
        fault["status_code"] = _int(fault.get("status_code"), "status_code", 400, 599)
    fault["backend_profile"] = "chaos_mesh.httpchaos"


def _validate_strace(scope: Dict[str, Any], fault: Dict[str, Any]) -> None:
    _reject_unknown_fields(scope, {"target_entity_ids", "pid", "pod", "container"}, "strace scope")
    _reject_unknown_fields(fault, {"type", "syscalls", "behavior", "errno", "delay_ms", "trigger"}, "strace fault")
    if not (scope.get("pid") or (scope.get("pod") and scope.get("container"))):
        raise PrecisionInjectionError("strace injection requires pid or pod+container scope")
    syscalls = [str(item).strip().lower() for item in _list(fault.get("syscalls")) if str(item).strip()]
    if not 1 <= len(syscalls) <= 8 or any(not _SAFE_SYSCALL.fullmatch(item) for item in syscalls):
        raise PrecisionInjectionError("strace requires 1-8 explicit safe syscall names")
    behavior = str(fault.get("behavior") or "error").lower()
    if behavior not in {"error", "delay"}:
        raise PrecisionInjectionError("strace behavior must be error or delay")
    fault.update({"syscalls": list(dict.fromkeys(syscalls)), "behavior": behavior})
    if behavior == "error":
        errno = str(fault.get("errno") or "").upper()
        if not re.fullmatch(r"E[A-Z0-9_]{1,31}", errno):
            raise PrecisionInjectionError("strace error injection requires an explicit errno name")
        fault["errno"] = errno
    else:
        fault["delay_ms"] = _int(fault.get("delay_ms"), "delay_ms", 1, 10_000)
    fault["trigger"] = _trigger(fault, maximum_hits=100)
    fault["backend_profile"] = "linux.strace_syscall_inject"


def _trigger(fault: Dict[str, Any], *, maximum_hits: int) -> Dict[str, int]:
    trigger = _dict(fault.get("trigger"))
    hit_count = _int(trigger.get("hit_count"), "hit_count", 1, 1000, default=1)
    max_hits = _int(trigger.get("max_hits"), "max_hits", 1, maximum_hits, default=1)
    if max_hits > hit_count:
        raise PrecisionInjectionError("trigger requires max_hits<=hit_count")
    return {"hit_count": hit_count, "max_hits": max_hits}


def _validate_seccomp(scope: Dict[str, Any], match: Dict[str, Any], fault: Dict[str, Any]) -> None:
    _reject_unknown_fields(scope, {"target_entity_ids", "pid", "pod", "container"}, "seccomp scope")
    _reject_unknown_fields(match, {"path"}, "seccomp match")
    _reject_unknown_fields(fault, {"type", "syscalls", "action", "errno", "trigger"}, "seccomp fault")
    if not (scope.get("pid") or (scope.get("pod") and scope.get("container"))):
        raise PrecisionInjectionError("seccomp injection requires pid or pod+container scope")
    syscalls = [str(item).strip().lower() for item in _list(fault.get("syscalls"))]
    if syscalls != ["openat"]:
        raise PrecisionInjectionError("seccomp reviewed profile requires syscalls=[openat]")
    if str(fault.get("action") or "").lower() != "errno":
        raise PrecisionInjectionError("seccomp reviewed profile requires action=errno")
    match["path"] = _exact_path(match.get("path"), "match.path")
    fault.update({
        "syscalls": ["openat"],
        "action": "errno",
        "errno": _int(fault.get("errno"), "errno", 1, 4095),
        "trigger": _trigger(fault, maximum_hits=100),
        "backend_profile": "linux.seccomp_user_notif_openat_errno",
        "install_mode": "process_start",
        "rollback_mode": "restart_without_filter",
    })


def _validate_ebpf(scope: Dict[str, Any], match: Dict[str, Any], fault: Dict[str, Any]) -> None:
    _reject_unknown_fields(
        scope, {"target_entity_ids", "cgroup_id", "pid", "pod", "container"}, "eBPF scope",
    )
    _reject_unknown_fields(match, {"destination_ip", "destination_port"}, "eBPF match")
    _reject_unknown_fields(fault, {"type", "attach_point", "trigger"}, "eBPF fault")
    if not any(scope.get(key) for key in ("cgroup_id", "pod", "container", "pid")):
        raise PrecisionInjectionError("eBPF injection requires a pod/container/cgroup/process scope")
    attach_point = str(fault.get("attach_point") or "").strip().lower()
    if attach_point not in {"cgroup/connect4", "cgroup/connect6"}:
        raise PrecisionInjectionError("eBPF reviewed profile requires cgroup/connect4 or cgroup/connect6")
    address = str(match.get("destination_ip") or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F:.]{2,64}", address):
        raise PrecisionInjectionError("eBPF network injection requires one exact destination_ip")
    match["destination_port"] = _int(match.get("destination_port"), "destination_port", 1, 65535)
    fault.update({
        "attach_point": attach_point,
        "action": "deny_connect",
        "trigger": _trigger(fault, maximum_hits=100),
        "backend_profile": "linux.ebpf_cgroup_connect_deny",
    })


def _validate_bytecode(scope: Dict[str, Any], fault: Dict[str, Any]) -> None:
    _reject_unknown_fields(scope, {"target_entity_ids", "pod", "container"}, "bytecode scope")
    _reject_unknown_fields(
        fault, {"type", "runtime", "module_or_class", "class", "function", "method", "delay_ms", "trigger"},
        "bytecode fault",
    )
    if not (scope.get("pod") and scope.get("container")):
        raise PrecisionInjectionError("bytecode injection requires pod+container scope")
    runtime = str(fault.get("runtime") or "").lower()
    class_name = str(fault.get("module_or_class") or fault.get("class") or "").strip()
    function = str(fault.get("function") or fault.get("method") or "").strip()
    if runtime not in {"java", "jvm"} or not _SAFE_CLASS.fullmatch(class_name) or not _SAFE_SYMBOL.fullmatch(function):
        raise PrecisionInjectionError("bytecode injection requires exact JVM class and method")
    fault.update({
        "runtime": "java",
        "module_or_class": class_name,
        "function": function,
        "action": "delay",
        "delay_ms": _int(fault.get("delay_ms"), "delay_ms", 1, 10_000),
        "trigger": _trigger(fault, maximum_hits=100),
        "backend_profile": "chaos_mesh.jvmchaos_method_latency",
    })


def _validate_native(method: str, scope: Dict[str, Any], fault: Dict[str, Any]) -> None:
    _reject_unknown_fields(scope, {"target_entity_ids", "pid", "pod", "container"}, f"{method} scope")
    native_fields = {
        "type", "binary_path", "abi_profile", "symbol", "action", "return_value", "errno",
        "delay_ms", "trigger",
    }
    if method == "binary":
        native_fields.update({"provider", "instrumentation_artifact_id"})
    _reject_unknown_fields(
        fault,
        native_fields,
        f"{method} fault",
    )
    if not (scope.get("pid") or (scope.get("pod") and scope.get("container"))):
        raise PrecisionInjectionError(f"{method} injection requires pid or pod+container scope")
    binary_path = _exact_path(fault.get("binary_path"), "binary_path")
    symbol = str(fault.get("symbol") or "").strip()
    if not _SAFE_SYMBOL.fullmatch(symbol):
        raise PrecisionInjectionError(f"{method} injection requires one explicit safe function symbol")
    if str(fault.get("abi_profile") or "") != "int32_int32":
        raise PrecisionInjectionError("native injection requires reviewed abi_profile=int32_int32")
    action = str(fault.get("action") or "").lower()
    allowed = {"return_override"} if method == "gdb" else {"return_override", "errno", "delay"}
    if action not in allowed:
        raise PrecisionInjectionError(f"{method} action must be one of {sorted(allowed)}")
    if action in {"return_override", "errno"}:
        fault["return_value"] = _int(fault.get("return_value"), "return_value", -(2**31), 2**31 - 1)
    if action == "errno":
        fault["errno"] = _int(fault.get("errno"), "errno", 1, 4095)
    if action == "delay":
        fault["delay_ms"] = _int(fault.get("delay_ms"), "delay_ms", 1, 10_000)
    fault.update({
        "binary_path": binary_path,
        "symbol": symbol,
        "action": action,
        "trigger": _trigger(fault, maximum_hits=100),
        "backend_profile": "linux.gdb_function_override" if method == "gdb" else "linux.elf_function_interposition",
        "rollback_mode": "detach" if method == "gdb" else "expire_bounded_hook",
    })
    if method == "gdb":
        fault["provider"] = "gdb"
    else:
        provider = str(fault.get("provider") or "").lower()
        artifact = str(fault.get("instrumentation_artifact_id") or "")
        if provider not in {"ld_preload", "frida", "dynamorio"}:
            raise PrecisionInjectionError("binary injection requires a reviewed provider")
        if not _SAFE_ARTIFACT.fullmatch(artifact):
            raise PrecisionInjectionError("binary injection requires an AgenticSRE reviewed artifact id")


def _validate_failpoint(scope: Dict[str, Any], match: Dict[str, Any], fault: Dict[str, Any]) -> None:
    _reject_unknown_fields(scope, {"target_entity_ids", "service"}, "failpoint scope")
    _reject_unknown_fields(match, {"request_id", "tenant_id"}, "failpoint match")
    _reject_unknown_fields(
        fault, {"type", "failpoint_id", "action", "error_code", "delay_ms", "trigger"}, "failpoint fault",
    )
    service = str(scope.get("service") or "").strip()
    if not _SAFE_NAME.fullmatch(service):
        raise PrecisionInjectionError("failpoint injection requires one exact service")
    selectors = {key: str(match.get(key) or "").strip() for key in ("request_id", "tenant_id") if match.get(key)}
    if not selectors or any(not _SAFE_NAME.fullmatch(value) for value in selectors.values()):
        raise PrecisionInjectionError("failpoint requires one exact request_id or tenant_id")
    failpoint_id = str(fault.get("failpoint_id") or "")
    if not _SAFE_FAILPOINT.fullmatch(failpoint_id):
        raise PrecisionInjectionError("failpoint requires one reviewed failpoint_id")
    action = str(fault.get("action") or "").lower()
    if action not in {"error", "delay"}:
        raise PrecisionInjectionError("failpoint action must be error or delay")
    if action == "error":
        error_code = str(fault.get("error_code") or "").upper()
        if not _SAFE_ERROR_CODE.fullmatch(error_code):
            raise PrecisionInjectionError("failpoint error requires one reviewed error_code")
        fault["error_code"] = error_code
    else:
        fault["delay_ms"] = _int(fault.get("delay_ms"), "delay_ms", 1, 10_000)
    match.clear()
    match.update(selectors)
    fault.update({
        "failpoint_id": failpoint_id,
        "action": action,
        "trigger": _trigger(fault, maximum_hits=100),
        "backend_profile": "agenticsre.application_failpoint.v1",
        "activation_mode": "scoped_token",
        "rollback_mode": "disable_activation_token",
    })


def _validate_cgroup(scope: Dict[str, Any], fault: Dict[str, Any]) -> None:
    _reject_unknown_fields(
        scope, {"target_entity_ids", "pod", "container", "cgroup_path"}, "cgroup_v2 scope",
    )
    _reject_unknown_fields(
        fault,
        {
            "type", "kind", "quota_us", "period_us", "memory_high_bytes", "device",
            "rbps", "wbps", "riops", "wiops",
        },
        "cgroup_v2 fault",
    )
    path = str(scope.get("cgroup_path") or "")
    if not _SAFE_CGROUP.fullmatch(path) or ".." in path.split("/") or "//" in path:
        raise PrecisionInjectionError("cgroup_v2 requires one exact non-root cgroup_path")
    if not (scope.get("pod") and scope.get("container")):
        raise PrecisionInjectionError("cgroup_v2 requires pod+container scope")
    kind = str(fault.get("kind") or "").lower()
    if kind == "cpu_quota":
        quota = _int(fault.get("quota_us"), "quota_us", 1000, 1_000_000)
        period = _int(fault.get("period_us"), "period_us", 1000, 1_000_000)
        if quota >= period:
            raise PrecisionInjectionError("cgroup_v2 cpu_quota requires quota_us<period_us")
        values, controller = {"quota_us": quota, "period_us": period}, "cpu.max"
    elif kind == "memory_high":
        values = {"memory_high_bytes": _int(fault.get("memory_high_bytes"), "memory_high_bytes", 64 * 1024**2, 1024**4)}
        controller = "memory.high"
    elif kind == "io_max":
        device = str(fault.get("device") or "")
        if not re.fullmatch(r"[0-9]{1,7}:[0-9]{1,7}", device):
            raise PrecisionInjectionError("cgroup_v2 io_max requires an exact major:minor device")
        values = {"device": device}
        for key in ("rbps", "wbps", "riops", "wiops"):
            if key in fault:
                values[key] = _int(fault.get(key), key, 1, 10**12)
        if len(values) == 1:
            raise PrecisionInjectionError("cgroup_v2 io_max requires at least one limit")
        controller = "io.max"
    else:
        raise PrecisionInjectionError("cgroup_v2 kind must be cpu_quota, memory_high, or io_max")
    fault.clear()
    fault.update({
        "kind": kind,
        **values,
        "backend_profile": "linux.cgroup_v2_temporary_limit",
        "controller_file": controller,
        "baseline_capture_required": True,
        "rollback_mode": "restore_captured_controller_value",
    })


def _validate_dcgm(scope: Dict[str, Any], fault: Dict[str, Any]) -> None:
    _reject_unknown_fields(scope, {"target_entity_ids", "gpu_uuid", "gpu_id"}, "dcgm scope")
    _reject_unknown_fields(fault, {"type", "kind", "field_id", "value"}, "dcgm fault")
    uuid_value = str(scope.get("gpu_uuid") or "")
    gpu_id = scope.get("gpu_id")
    if bool(uuid_value) == (gpu_id is not None):
        raise PrecisionInjectionError("dcgm requires exactly one gpu_uuid or gpu_id")
    if uuid_value and not _SAFE_GPU_UUID.fullmatch(uuid_value):
        raise PrecisionInjectionError("dcgm gpu_uuid is invalid")
    if gpu_id is not None:
        scope["gpu_id"] = _int(gpu_id, "gpu_id", 0, 255)
    profiles = {"pcie_replay": (202, 1_000_000_000), "ecc_dbe": (319, 1_000_000)}
    kind = str(fault.get("kind") or "").lower()
    if kind not in profiles:
        raise PrecisionInjectionError("dcgm kind must be pcie_replay or ecc_dbe")
    field_id, maximum = profiles[kind]
    if fault.get("field_id") is not None and _int(fault.get("field_id"), "field_id", 1, 10000) != field_id:
        raise PrecisionInjectionError("dcgm field_id does not match the reviewed kind")
    value = _int(fault.get("value"), "value", 1, maximum)
    fault.clear()
    fault.update({
        "kind": kind,
        "field_id": field_id,
        "value": value,
        "backend_profile": "nvidia.dcgm_field_injection",
        "simulation_only": True,
        "rollback_mode": "expire_injected_sample_and_verify_baseline",
    })


def _validate_dm_flakey(scope: Dict[str, Any], fault: Dict[str, Any]) -> None:
    _reject_unknown_fields(
        scope,
        {
            "target_entity_ids", "mapping_name", "backing_device", "mount_path", "snapshot_id",
            "disposable_volume",
        },
        "dm_flakey scope",
    )
    _reject_unknown_fields(fault, {"type", "mode", "up_interval_s", "down_interval_s"}, "dm_flakey fault")
    mapping = str(scope.get("mapping_name") or "")
    device = str(scope.get("backing_device") or "")
    mount = str(scope.get("mount_path") or "")
    snapshot = str(scope.get("snapshot_id") or "")
    if scope.get("disposable_volume") is not True:
        raise PrecisionInjectionError("dm_flakey requires disposable_volume=true")
    if not _SAFE_DM_NAME.fullmatch(mapping):
        raise PrecisionInjectionError("dm_flakey mapping_name must use the agenticsre- prefix")
    if not (
        device == "/dev/loop-auto"
        or re.fullmatch(r"/dev/loop[0-9]+", device)
        or re.fullmatch(r"/dev/mapper/agenticsre-[a-z0-9-]+", device)
    ):
        raise PrecisionInjectionError("dm_flakey requires an AgenticSRE loop or mapper device")
    if not (mount.startswith("/var/lib/agenticsre/fault-volumes/") or mount.startswith("/tmp/agenticsre-fault-")):
        raise PrecisionInjectionError("dm_flakey requires a dedicated AgenticSRE fault-volume mount")
    if not _SAFE_ARTIFACT.fullmatch(snapshot):
        raise PrecisionInjectionError("dm_flakey requires an AgenticSRE reviewed snapshot id")
    if str(fault.get("mode") or "").lower() != "error_writes":
        raise PrecisionInjectionError("dm_flakey supports mode=error_writes only")
    up_interval_s = _int(fault.get("up_interval_s"), "up_interval_s", 1, 3600)
    down_interval_s = _int(fault.get("down_interval_s"), "down_interval_s", 1, 60)
    fault.clear()
    fault.update({
        "mode": "error_writes",
        "up_interval_s": up_interval_s,
        "down_interval_s": down_interval_s,
        "backend_profile": "linux.dm_flakey_disposable_volume",
        "baseline_checksum_required": True,
        "rollback_mode": "remove_mapping_and_restore_snapshot",
    })


def _validate_platform(scope: Dict[str, Any], match: Dict[str, Any], fault: Dict[str, Any]) -> List[str]:
    kind = str(fault.get("kind") or "").lower()
    if kind not in PLATFORM_FAULTS:
        raise PrecisionInjectionError(f"unsupported platform fault kind '{kind}'")
    backend, extra_evidence = PLATFORM_FAULTS[kind]
    allowed_scope = {"target_entity_ids", "pod", "container"}
    allowed_match = {"destination_entity_ids"} if kind == "network_loss" else ({"dns_patterns"} if kind == "dns_error" else set())
    allowed_fault = {
        "network_loss": {"type", "kind", "loss_percent"},
        "dns_error": {"type", "kind"},
        "cpu_stress": {"type", "kind", "workers", "load_percent"},
        "io_latency": {"type", "kind", "volume_path", "path", "delay_ms"},
        "time_offset": {"type", "kind", "offset", "clock_ids"},
    }[kind]
    _reject_unknown_fields(scope, allowed_scope, f"{kind} scope")
    _reject_unknown_fields(match, allowed_match, f"{kind} match")
    _reject_unknown_fields(fault, allowed_fault, f"{kind} fault")
    if kind == "network_loss":
        destinations = [str(item).strip() for item in _list(match.get("destination_entity_ids")) if str(item).strip()]
        if not destinations or any(not _SAFE_TARGET.fullmatch(item) for item in destinations):
            raise PrecisionInjectionError("network_loss requires exact destination_entity_ids")
        match["destination_entity_ids"] = list(dict.fromkeys(destinations))
        fault["loss_percent"] = _float(fault.get("loss_percent"), "loss_percent", 0.000001, 100.0)
    elif kind == "dns_error":
        patterns = [str(item).strip() for item in _list(match.get("dns_patterns")) if str(item).strip()]
        if not patterns or any(any(char in item for char in "*?[]{}") for item in patterns):
            raise PrecisionInjectionError("dns_error requires exact DNS names without wildcards")
        match["dns_patterns"] = list(dict.fromkeys(patterns))
    elif kind == "cpu_stress":
        if not str(scope.get("container") or ""):
            raise PrecisionInjectionError("cpu_stress requires one container")
        fault["workers"] = _int(fault.get("workers"), "workers", 1, 32)
        fault["load_percent"] = _int(fault.get("load_percent"), "load_percent", 1, 100)
    elif kind == "io_latency":
        if not str(scope.get("container") or ""):
            raise PrecisionInjectionError("io_latency requires one container")
        fault["volume_path"] = _exact_path(fault.get("volume_path"), "volume_path")
        fault["path"] = _exact_path(fault.get("path"), "path")
        if not fault["path"].startswith(fault["volume_path"].rstrip("/") + "/"):
            raise PrecisionInjectionError("io_latency path must be inside volume_path")
        fault["delay_ms"] = _int(fault.get("delay_ms"), "delay_ms", 1, 10_000)
    else:
        if not str(scope.get("container") or ""):
            raise PrecisionInjectionError("time_offset requires one container")
        offset = str(fault.get("offset") or "")
        clocks = [str(item) for item in _list(fault.get("clock_ids"))]
        if not re.fullmatch(r"[+-][0-9]{1,4}[smh]", offset) or clocks != ["CLOCK_REALTIME"]:
            raise PrecisionInjectionError("time_offset requires bounded offset and clock_ids=[CLOCK_REALTIME]")
    fault["backend_profile"] = backend
    return extra_evidence


def build_precision_contract(
    payload: Optional[Mapping[str, Any]],
    *,
    target_entity_ids: Iterable[str],
    fault_type: str,
    duration_s: int,
    blast_radius_budget: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Normalize one registered scenario into an approval-gated adapter contract."""

    data = dict(payload or {})
    if _contains_forbidden_key(data):
        raise PrecisionInjectionError("precision contracts accept typed fields only; commands and scripts are forbidden")
    nested = _dict(data.get("precision") or data)
    _reject_unknown_fields(nested, {"method", "scope", "match", "fault", "safety"}, "precision contract")
    method = normalize_method(nested.get("method") or data.get("injection_method") or data.get("method"))
    budget = dict(blast_radius_budget or {})
    targets = list(dict.fromkeys(str(item).strip() for item in target_entity_ids if str(item).strip()))
    if not targets or any(not _SAFE_TARGET.fullmatch(item) for item in targets):
        raise PrecisionInjectionError("precision injection requires explicit typed target entities")
    max_entities = _int(budget.get("max_affected_entities"), "max_affected_entities", 1, 100, default=1)
    if len(targets) > max_entities:
        raise PrecisionInjectionError(f"target count {len(targets)} exceeds max_affected_entities={max_entities}")

    scope = _dict(nested.get("scope"))
    scoped_targets = [str(item).strip() for item in _list(scope.get("target_entity_ids")) if str(item).strip()]
    if scoped_targets and scoped_targets != targets:
        raise PrecisionInjectionError("precision scope target_entity_ids must exactly match the registered request scope")
    scope["target_entity_ids"] = targets
    match = _dict(nested.get("match"))
    fault = _dict(nested.get("fault"))
    fault["type"] = str(fault_type or fault.get("type") or "")
    safety = _dict(nested.get("safety"))
    _reject_unknown_fields(safety, {"duration_s", "max_injections", "auto_cleanup"}, "precision safety")
    maximum_duration = _int(budget.get("max_duration_s"), "max_duration_s", 1, 3600, default=120)
    requested_duration = _int(safety.get("duration_s"), "duration_s", 1, maximum_duration, default=duration_s)
    maximum_injections = _int(budget.get("max_injections"), "budget.max_injections", 1, 100, default=1)
    max_injections = _int(safety.get("max_injections"), "max_injections", 1, maximum_injections, default=1)
    if safety.get("auto_cleanup", True) is not True:
        raise PrecisionInjectionError("auto_cleanup cannot be disabled")

    extra_evidence: List[str] = []
    if method == "platform_native":
        extra_evidence = _validate_platform(scope, match, fault)
    elif method == "request":
        _validate_request(match, fault, budget)
    elif method == "strace":
        _validate_strace(scope, fault)
    elif method == "seccomp":
        _validate_seccomp(scope, match, fault)
    elif method == "ebpf":
        _validate_ebpf(scope, match, fault)
    elif method == "bytecode":
        _validate_bytecode(scope, fault)
    elif method in {"gdb", "binary"}:
        _validate_native(method, scope, fault)
    elif method == "failpoint":
        _validate_failpoint(scope, match, fault)
    elif method == "cgroup_v2":
        _validate_cgroup(scope, fault)
    elif method == "dcgm":
        _validate_dcgm(scope, fault)
    elif method == "dm_flakey":
        _validate_dm_flakey(scope, fault)

    fault.setdefault("type", str(fault_type or ""))
    granularity, evidence = METHOD_METADATA[method]
    return {
        "version": "agenticsre.precision.v1",
        "method": method,
        "granularity": granularity,
        "scope": scope,
        "match": match,
        "fault": fault,
        "safety": {
            "duration_s": requested_duration,
            "max_injections": max_injections,
            "auto_cleanup": True,
            "requires_approval": True,
            "abort_on_scope_miss": True,
        },
        "evidence": {
            "required_fields": list(dict.fromkeys([*evidence, *extra_evidence, "before_during_after"])),
            "correlation_keys": ["experiment_id", "target_entity_id", "request_id"],
        },
        "execution": {
            "mode": "approval_gated_transactional_adapter",
            "reviewed_adapter_required": True,
            "environment_validation_required": True,
            "effect_evidence_required": True,
            "undo_evidence_required": True,
            "residue_check_required": True,
        },
    }
