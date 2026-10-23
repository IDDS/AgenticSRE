from __future__ import annotations

from typing import Any, Dict

from skill_runtime.runtime_evidence import (
    cuda_runtime_evidence,
    hidden_accelerator_visibility_variables,
    runtime_observation_text,
)

def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _metrics(payload: Dict[str, Any]) -> Dict[str, Any]:
    raw = payload.get("metrics")
    return raw if isinstance(raw, dict) else {}


def _num(payload: Dict[str, Any], *names: str, default: float = 0.0) -> float:
    metrics = _metrics(payload)
    for name in names:
        try:
            if name in payload:
                return float(payload.get(name))
            if name in metrics:
                return float(metrics.get(name))
        except (TypeError, ValueError):
            continue
    return default


def _text(payload: Dict[str, Any]) -> str:
    return runtime_observation_text(payload)


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    kernel_p95 = _num(payload, "kernel_p95_ms", "teller_kernel_p95_ms")
    evidence = cuda_runtime_evidence(payload)
    matched = bool(evidence) or kernel_p95 >= 50
    if kernel_p95 > 0:
        evidence.append(f"Kernel P95={kernel_p95:g}ms")
    if payload.get("gpu_uuid"):
        evidence.append(f"GPU UUID {payload.get('gpu_uuid')}")
    if payload.get("node"):
        evidence.append(f"节点 {payload.get('node')}")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": (0.9 if len(evidence) >= 2 else 0.84) if matched else 0.0,
        "summary": "检测到 CUDA/GPU 驱动异常信号" if matched else "未检测到 CUDA/GPU 驱动异常",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    text = _text(payload)
    kernel_p95 = _num(payload, "kernel_p95_ms", "teller_kernel_p95_ms")
    hidden_visibility = hidden_accelerator_visibility_variables(payload)
    if hidden_visibility:
        fault_type = "LLM::cuda_device_visibility_misconfiguration"
        root = "工作负载已申请 GPU 资源，但容器运行时配置禁用了加速器可见性，导致 vLLM 无法初始化 CUDA 设备"
    elif any(token in text for token in ("xid", "gpu lost", "fallen off the bus", "nvidia driver")):
        fault_type = "LLM::gpu_driver_xid_or_device_error"
        root = "GPU 驱动或设备健康异常，可能导致 CUDA 调用失败、Kernel 执行中断或推理服务退化"
    elif _num(payload, "cuda_context_errors") > 0 or any(token in text for token in ("cuda context", "cucontext", "context is destroyed")):
        fault_type = "LLM::cuda_context_error"
        root = "CUDA Context 或 Runtime 状态异常，导致推理 Kernel 启动或执行失败"
    elif kernel_p95 >= 50:
        fault_type = "LLM::cuda_kernel_latency_bottleneck"
        root = "CUDA Kernel P95 耗时异常，可能存在算子瓶颈、GPU 调度或资源竞争"
    else:
        fault_type = "LLM::cuda_runtime_or_context_error"
        root = "CUDA runtime、GPU 驱动或 Profiler 开销异常，导致推理 kernel 或请求时延退化"
    return {
        "status": "ok",
        "confidence": 0.9 if len(evidence) >= 2 else 0.8,
        "summary": root,
        "evidence": evidence or ["命中 CUDA/GPU 驱动故障类型"],
        "fault_type": fault_type,
        "root_cause": root,
        "root_cause_component": payload.get("node") or payload.get("deployment") or "gpu-node",
        "observed": {
            "kernel_p95_ms": _num(payload, "kernel_p95_ms", "teller_kernel_p95_ms"),
            "gpu_uuid": payload.get("gpu_uuid") or "",
        },
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    ns = payload.get("namespace") or "llm-k8s"
    deploy = payload.get("deployment") or "vllm-server"
    node = payload.get("node") or "<gpu-node>"
    visibility_vars = hidden_accelerator_visibility_variables(payload)
    suggestions = [
        {"step": "查看 vLLM CUDA 错误日志", "command": f"kubectl -n {ns} logs deploy/{deploy} --tail=200 | egrep -i 'cuda|xid|nccl|kernel|cupti|pcie'", "risk": "low"},
        {"step": "查看节点 GPU/驱动事件", "command": f"kubectl describe node {node} | egrep -i 'gpu|nvidia|xid|pressure|taint' || true", "risk": "low"},
        {"step": "审批后滚动重启 vLLM 工作负载", "command": f"kubectl rollout restart deployment/{deploy} -n {ns}", "rollback_command": f"kubectl rollout undo deployment/{deploy} -n {ns}", "risk": "medium"},
        {"step": "如 Xid/驱动持续异常，审批后隔离节点并转人工维护", "command": f"kubectl cordon {node}", "rollback_command": f"kubectl uncordon {node}", "risk": "critical"},
    ]
    if visibility_vars:
        unset_args = " ".join(f"{name}-" for name in visibility_vars)
        suggestions.insert(1, {
            "step": "恢复工作负载的加速器可见性配置",
            "command": f"kubectl set env deployment/{deploy} -n {ns} {unset_args}",
            "rollback_command": f"kubectl rollout undo deployment/{deploy} -n {ns}",
            "risk": "medium",
        })
    return {
        "status": "ok",
        "confidence": 0.82,
        "summary": "生成 CUDA/GPU 驱动故障恢复建议；节点级操作必须审批。",
        "evidence": ["建议先只读核查 GPU/Xid/Trace，再审批节点或工作负载恢复"],
        "suggestions": suggestions,
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    ns = payload.get("namespace") or "llm-k8s"
    deploy = payload.get("deployment") or "vllm-server"
    return {
        "status": "ok",
        "confidence": 0.78,
        "summary": "验证 CUDA/Xid 错误、Kernel P95 和请求时延是否恢复。",
        "evidence": ["CUDA/GPU 驱动恢复验证计划"],
        "checks": [
            f"kubectl -n {ns} rollout status deployment/{deploy}",
            f"kubectl -n {ns} logs deploy/{deploy} --tail=100 | egrep -i 'cuda|xid|error|cupti' || true",
            "检查 TELLER kernel P95、TTFT/E2E P95 是否回落",
        ],
    }
