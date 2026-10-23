from __future__ import annotations

from typing import Any, Dict, List


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    keys = ("fault_type", "raw_fault_type", "message", "model", "gpu_model", "dtype", "quantization", "backend")
    return " ".join(str(payload.get(key) or "") for key in keys).lower()


def _num(payload: Dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        try:
            if key in payload:
                return float(payload.get(key))
        except (TypeError, ValueError):
            pass
    return default


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    has_hardware_fields = any(key in payload for key in (
        "gpu_model", "dtype", "quantization", "backend", "compute_capability",
        "gpu_memory_gb", "model_params_b",
    ))
    matched = any(token in text for token in (
        "hardware compatibility", "compute capability", "unsupported dtype",
        "unsupported backend", "quantization", "flashattention", "flash-attn",
        "bf16", "cuda arch", "兼容性",
    )) or has_hardware_fields
    evidence: List[str] = []
    if payload.get("model"):
        evidence.append(f"模型={payload.get('model')}")
    if payload.get("gpu_model"):
        evidence.append(f"GPU={payload.get('gpu_model')}")
    if payload.get("dtype"):
        evidence.append(f"dtype={payload.get('dtype')}")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.86 if matched else 0.0,
        "summary": "检测到模型与硬件兼容性信号" if matched else "未检测到模型硬件兼容性异常",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    cc = _num(payload, "compute_capability", default=0)
    memory_gb = _num(payload, "gpu_memory_gb", default=0)
    params_b = _num(payload, "model_params_b", default=0)
    if "bf16" in text and ("t4" in text or cc and cc < 8.0):
        fault_type = "LLM::dtype_not_supported_by_gpu"
        root_cause = "当前 GPU 架构不适合 BF16，vLLM 应使用 FP16 或兼容量化格式"
    elif "flashattention" in text or "unsupported backend" in text or "cuda arch" in text:
        fault_type = "LLM::attention_backend_or_cuda_arch_incompatible"
        root_cause = "attention backend 或 CUDA kernel 与当前 GPU compute capability 不兼容"
    elif memory_gb and params_b and params_b * 2 > memory_gb * 0.8:
        fault_type = "LLM::model_memory_footprint_exceeds_gpu"
        root_cause = "模型参数规模和运行时 KV Cache 需求超过当前 GPU 显存能力"
    elif "quantization" in text:
        fault_type = "LLM::quantization_kernel_incompatible"
        root_cause = "量化格式或 kernel 与当前 vLLM 镜像/GPU 架构不兼容"
    else:
        fault_type = "LLM::model_hardware_compatibility_risk"
        root_cause = "模型、dtype、量化或 attention backend 与当前 GPU 环境存在兼容性风险"
    if cc:
        evidence.append(f"compute capability={cc:g}")
    if memory_gb:
        evidence.append(f"GPU 显存={memory_gb:g}GB")
    return {
        "status": "ok",
        "confidence": min(0.92, 0.68 + 0.05 * len(evidence)),
        "summary": root_cause,
        "evidence": evidence,
        "fault_type": fault_type,
        "root_cause": root_cause,
        "root_cause_component": payload.get("model") or "vllm-model",
        "observed": {"compute_capability": cc, "gpu_memory_gb": memory_gb, "model_params_b": params_b},
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    model = payload.get("model") or "<model>"
    return {
        "status": "ok",
        "confidence": 0.78,
        "summary": "生成模型硬件兼容性修复建议；切换模型或 dtype 需审批。",
        "evidence": ["修复建议优先保持实验可复现"],
        "suggestions": [
            {"step": "查看 GPU 能力", "command": "nvidia-smi --query-gpu=name,memory.total,compute_cap --format=csv", "risk": "low"},
            {"step": "查看 vLLM 启动日志", "command": "kubectl logs deployment/vllm-server -n llm-k8s --tail=200", "risk": "low"},
            {"step": "审批后切换兼容配置", "command": f"将 {model} 的 dtype/backend/quantization 调整为当前 GPU 支持的组合，并记录基线", "risk": "high"},
        ],
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "ok",
        "confidence": 0.76,
        "summary": "验证模型加载、短请求和 GPU 指标。",
        "evidence": ["模型兼容性验证计划"],
        "checks": [
            "/v1/models 返回目标模型",
            "短 token 请求成功",
            "GPU 显存、TTFT/E2E 和错误率在可接受范围",
        ],
    }
