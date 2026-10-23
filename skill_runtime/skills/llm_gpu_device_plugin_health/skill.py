from __future__ import annotations

from typing import Any, Dict, List


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    keys = ("fault_type", "raw_fault_type", "message", "node", "pod", "reason")
    return " ".join(str(payload.get(key) or "") for key in keys).lower()


def _num(payload: Dict[str, Any], *keys: str, default: float = 0.0) -> float:
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    for key in keys:
        try:
            if key in payload:
                return float(payload.get(key))
            if key in metrics:
                return float(metrics.get(key))
        except (TypeError, ValueError):
            pass
    return default


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    allocatable = _num(payload, "gpu_allocatable", "nvidia_gpu_allocatable", default=-1)
    matched = any(token in text for token in (
        "nvidia-device-plugin", "nvidia.com/gpu", "gpu allocatable",
        "insufficient nvidia.com/gpu", "runtimeclass", "device plugin",
        "gpu 节点", "设备插件",
    )) or allocatable == 0
    evidence: List[str] = []
    if allocatable == 0:
        evidence.append("节点 GPU allocatable=0")
    if payload.get("node"):
        evidence.append(f"节点={payload.get('node')}")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.88 if matched else 0.0,
        "summary": "检测到 GPU 设备插件或资源暴露异常" if matched else "未检测到 GPU 资源暴露异常",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    allocatable = _num(payload, "gpu_allocatable", "nvidia_gpu_allocatable", default=-1)
    plugin_ready = str(payload.get("device_plugin_ready") or "").lower()
    node_ready = str(payload.get("node_ready") or "").lower()
    if node_ready in {"false", "0", "notready"} or "node not found" in text or "not in k8s" in text:
        fault_type = "LLM::gpu_node_not_registered"
        root_cause = "GPU 节点未正确加入 K8S 或处于 NotReady，智算资源无法调度"
    elif plugin_ready in {"false", "0", "notready"} or "device plugin" in text:
        fault_type = "LLM::nvidia_device_plugin_unhealthy"
        root_cause = "NVIDIA device plugin 未正常运行，K8S 无法暴露 GPU 资源"
    elif allocatable == 0 or "insufficient nvidia.com/gpu" in text:
        fault_type = "LLM::gpu_allocatable_missing"
        root_cause = "K8S 节点未暴露 nvidia.com/gpu allocatable，GPU Pod 无法调度"
    elif "runtimeclass" in text:
        fault_type = "LLM::gpu_container_runtime_misconfigured"
        root_cause = "GPU 容器运行时或 RuntimeClass 配置异常，容器内无法访问 GPU"
    else:
        fault_type = "LLM::gpu_device_plugin_or_resource_exposure_failure"
        root_cause = "GPU device plugin、节点注册或 runtime 配置存在不一致"
    return {
        "status": "ok",
        "confidence": min(0.93, 0.7 + 0.05 * len(evidence)),
        "summary": root_cause,
        "evidence": evidence,
        "fault_type": fault_type,
        "root_cause": root_cause,
        "root_cause_component": payload.get("node") or "nvidia-device-plugin",
        "observed": {"gpu_allocatable": allocatable, "device_plugin_ready": plugin_ready, "node_ready": node_ready},
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    ns = _payload(context).get("namespace") or "kube-system"
    return {
        "status": "ok",
        "confidence": 0.78,
        "summary": "生成 GPU 资源暴露恢复建议；重启插件或 kubelet 需审批。",
        "evidence": ["先只读确认节点、插件和 runtime"],
        "suggestions": [
            {"step": "查看 GPU allocatable", "command": "kubectl get nodes -o custom-columns=NAME:.metadata.name,GPU:.status.allocatable.nvidia\\.com/gpu,READY:.status.conditions[-1].type", "risk": "low"},
            {"step": "查看 device plugin", "command": f"kubectl -n {ns} get ds,pods -l name=nvidia-device-plugin-ds -o wide", "risk": "low"},
            {"step": "审批后重启 device plugin", "command": f"kubectl -n {ns} rollout restart daemonset/nvidia-device-plugin-daemonset", "risk": "high"},
        ],
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "ok",
        "confidence": 0.76,
        "summary": "验证 GPU 节点、device plugin 和 GPU Pod 调度能力。",
        "evidence": ["GPU 资源暴露验证计划"],
        "checks": [
            "kubectl get nodes -o custom-columns=NAME:.metadata.name,GPU:.status.allocatable.nvidia\\.com/gpu",
            "kubectl -n kube-system get pods | grep -i nvidia",
            "提交一个 nvidia.com/gpu: 1 的短生命周期探测 Pod",
        ],
    }
