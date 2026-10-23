from __future__ import annotations

from typing import Any, Dict, List


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    keys = ("fault_type", "raw_fault_type", "message", "resource_name", "mig_profile", "gpu_sharing_mode")
    return " ".join(str(payload.get(key) or "") for key in keys).lower()


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    matched = any(token in text for token in (
        "mig", "vgpu", "mps", "time-slicing", "gpu sharing", "gpu slice",
        "mig profile", "分区", "切片", "隔离",
    ))
    evidence: List[str] = []
    if payload.get("mig_profile"):
        evidence.append(f"MIG profile={payload.get('mig_profile')}")
    if payload.get("gpu_sharing_mode"):
        evidence.append(f"GPU sharing mode={payload.get('gpu_sharing_mode')}")
    if payload.get("resource_name"):
        evidence.append(f"资源名={payload.get('resource_name')}")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.86 if matched else 0.0,
        "summary": "检测到 GPU 分区/共享/隔离异常信号" if matched else "未检测到 GPU 分区隔离异常",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    if "profile" in text or "mig" in text and "mismatch" in text:
        fault_type = "LLM::mig_profile_or_resource_mismatch"
        root_cause = "Pod 请求的 MIG profile/resource name 与节点暴露的切片资源不一致"
    elif "mps" in text or "time-slicing" in text or "gpu sharing" in text:
        fault_type = "LLM::gpu_sharing_isolation_interference"
        root_cause = "GPU sharing/MPS/time-slicing 隔离不足，多租户或多 Pod 干扰导致性能抖动"
    elif "not visible" in text or "不可见" in text:
        fault_type = "LLM::gpu_slice_not_visible"
        root_cause = "GPU 切片未正确暴露给容器或 device plugin"
    else:
        fault_type = "LLM::gpu_partition_isolation_degraded"
        root_cause = "GPU 分区、共享或隔离配置异常，需要核对 device plugin 和节点配置"
    return {
        "status": "ok",
        "confidence": min(0.92, 0.68 + 0.05 * len(evidence)),
        "summary": root_cause,
        "evidence": evidence,
        "fault_type": fault_type,
        "root_cause": root_cause,
        "root_cause_component": payload.get("node") or payload.get("resource_name") or "gpu-partition",
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    ns = _payload(context).get("namespace") or "kube-system"
    return {
        "status": "ok",
        "confidence": 0.76,
        "summary": "生成 GPU 分区/隔离恢复建议；修改 MIG 或 sharing 配置需审批。",
        "evidence": ["分区配置变更前必须记录当前 profile 和工作负载"],
        "suggestions": [
            {"step": "查看 GPU 资源名", "command": "kubectl get nodes -o json | jq '.items[].status.allocatable | with_entries(select(.key|test(\"gpu|mig|nvidia\")))'", "risk": "low"},
            {"step": "查看 device plugin 配置", "command": f"kubectl -n {ns} get configmap,daemonset | grep -Ei 'nvidia|gpu|mig|time-slicing'", "risk": "low"},
            {"step": "审批后调整 MIG/profile/sharing 配置", "command": "按目标模型和租户隔离要求调整 NVIDIA device plugin/MIG 配置并滚动验证", "risk": "critical"},
        ],
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "ok",
        "confidence": 0.74,
        "summary": "验证切片资源、Pod GPU 可见性和多租户性能隔离。",
        "evidence": ["GPU 分区隔离验证计划"],
        "checks": [
            "节点 allocatable 暴露预期 MIG/vGPU/MPS 资源",
            "Pod 调度到正确资源名且容器内 nvidia-smi 可见",
            "并发压测下热点 Pod 不再明显拖慢其他租户",
        ],
    }
