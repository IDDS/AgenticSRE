"""Reusable runtime evidence extraction for workload-level diagnosis."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List


def _number(payload: Dict[str, Any], name: str, default: float = 0.0) -> float:
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    value = payload.get(name) if name in payload else metrics.get(name)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _flatten_text(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for item in value.values():
            yield from _flatten_text(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _flatten_text(item)
    elif value not in (None, ""):
        yield str(value)


def container_lifecycle(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize container restart and termination observations."""
    restart_count = int(_number(payload, "pod_restart_count", 0.0))
    exit_code = int(_number(payload, "last_exit_code", 0.0))
    reason = str(payload.get("last_terminated_reason") or "").strip()
    abnormal_reason = bool(reason and reason.lower() not in {"completed", "succeeded"})
    restarted_in_window = _truthy(payload.get("container_restarted_in_window"))
    terminated_in_window = _truthy(payload.get("container_terminated_in_window"))
    abnormal = (
        restarted_in_window
        or terminated_in_window and (exit_code != 0 or abnormal_reason)
        or restart_count > 0 and (exit_code != 0 or abnormal_reason)
    )
    return {
        "restart_count": restart_count,
        "exit_code": exit_code,
        "reason": reason,
        "restarted_in_window": restarted_in_window,
        "terminated_in_window": terminated_in_window,
        "abnormal": bool(abnormal),
    }


def has_abnormal_container_termination(payload: Dict[str, Any]) -> bool:
    return bool(container_lifecycle(payload)["abnormal"])


def has_abnormal_container_restart(payload: Dict[str, Any]) -> bool:
    """Backward-compatible alias for abnormal lifecycle evidence."""
    return has_abnormal_container_termination(payload)


_RUNTIME_TEXT_FIELDS = (
    "container_waiting_reasons",
    "container_waiting_messages",
    "container_terminated_reasons",
    "container_terminated_messages",
    "container_log_excerpt",
    "container_log_samples",
    "runtime_error",
    "driver_error",
    "cuda_error",
)


def _accelerator_resource_total(payload: Dict[str, Any]) -> float:
    total = 0.0
    for field in ("accelerator_resource_requests", "accelerator_resource_limits"):
        values = payload.get(field) if isinstance(payload.get(field), dict) else {}
        for value in values.values():
            try:
                total += float(value)
            except (TypeError, ValueError):
                continue
    return total


def hidden_accelerator_visibility_variables(payload: Dict[str, Any]) -> List[str]:
    visibility = payload.get("accelerator_visibility") if isinstance(payload.get("accelerator_visibility"), dict) else {}
    hidden_values = {"-1", "none", "void", "disabled"}
    variables = []
    for name, value in visibility.items():
        if str(value).strip().lower() not in hidden_values:
            continue
        variable = str(name).rsplit(":", 1)[-1].strip()
        if variable and variable.replace("_", "a").isalnum() and not variable[0].isdigit():
            variables.append(variable)
    return list(dict.fromkeys(variables))


def runtime_observation_text(payload: Dict[str, Any]) -> str:
    """Return text sourced only from runtime state, not experiment labels."""
    parts: List[str] = []
    for field in _RUNTIME_TEXT_FIELDS:
        parts.extend(_flatten_text(payload.get(field)))
    for item in payload.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "").lower()
        signal = str(item.get("signal") or item.get("metric") or "").lower()
        if source not in {"container_log", "k8s_container_state", "runtime", "observation"}:
            continue
        if not any(token in signal for token in ("container", "runtime", "cuda", "gpu", "driver", "log")):
            continue
        parts.extend(_flatten_text(item.get("value")))
        parts.extend(_flatten_text(item.get("message")))
    return " ".join(parts).lower()


_CUDA_RUNTIME_MARKERS = {
    "cuda error": "容器运行时报告 CUDA error",
    "cuda runtime": "容器运行时报告 CUDA runtime 异常",
    "cuda initialization": "CUDA 初始化失败",
    "cuda driver initialization": "CUDA 驱动初始化失败",
    "no cuda gpu": "容器未发现可用 CUDA GPU",
    "no cuda-capable device": "容器未发现 CUDA-capable device",
    "no available gpu": "容器未发现可用 GPU",
    "no gpu is available": "容器未发现可用 GPU",
    "found no nvidia driver": "容器未发现 NVIDIA 驱动",
    "libcuda.so": "容器无法加载 CUDA Driver Library",
    "failed call to cuinit": "CUDA cuInit 调用失败",
    "cuda_visible_devices": "容器 CUDA 设备可见性异常",
    "failed to infer device type": "推理运行时无法识别加速设备类型",
    "device-side assert": "CUDA device-side assert",
}


def cuda_runtime_evidence(payload: Dict[str, Any]) -> List[str]:
    """Extract CUDA/driver evidence from structured metrics and runtime logs."""
    evidence: List[str] = []
    for name, label in (
        ("cuda_error_count", "CUDA error count"),
        ("cuda_context_errors", "CUDA context error count"),
    ):
        value = _number(payload, name, 0.0)
        if value > 0:
            evidence.append(f"{label}={value:g}")
    visibility = payload.get("accelerator_visibility") if isinstance(payload.get("accelerator_visibility"), dict) else {}
    hidden_variables = set(hidden_accelerator_visibility_variables(payload))
    hidden = [
        f"{name}={value}"
        for name, value in visibility.items()
        if str(name).rsplit(":", 1)[-1] in hidden_variables
    ]
    if hidden and _accelerator_resource_total(payload) > 0:
        evidence.append(f"GPU 资源已分配但加速器可见性被禁用: {', '.join(hidden)}")
    text = runtime_observation_text(payload)
    for marker, summary in _CUDA_RUNTIME_MARKERS.items():
        if marker in text:
            evidence.append(summary)
    return list(dict.fromkeys(evidence))


def has_cuda_runtime_evidence(payload: Dict[str, Any]) -> bool:
    return bool(cuda_runtime_evidence(payload))
