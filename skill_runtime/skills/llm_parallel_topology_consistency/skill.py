from __future__ import annotations

from typing import Any, Dict, List


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    keys = ("fault_type", "raw_fault_type", "message", "model", "deployment", "scenario_name")
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
    tp = _num(payload, "tensor_parallel_size", "tp_size")
    pp = _num(payload, "pipeline_parallel_size", "pp_size")
    world = _num(payload, "world_size")
    matched = any(token in text for token in (
        "tensor_parallel", "pipeline_parallel", "world_size", "rank mismatch",
        "tp_size_mismatch", "model shard", "nccl rank", "多卡", "张量并行", "流水并行",
    )) or tp > 1 or pp > 1 or world > 1
    evidence: List[str] = []
    if tp:
        evidence.append(f"tensor_parallel_size={tp:g}")
    if pp:
        evidence.append(f"pipeline_parallel_size={pp:g}")
    if world:
        evidence.append(f"world_size={world:g}")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.86 if matched else 0.0,
        "summary": "检测到多卡并行拓扑一致性信号" if matched else "未检测到多卡并行拓扑异常",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    tp = _num(payload, "tensor_parallel_size", "tp_size", default=1)
    pp = _num(payload, "pipeline_parallel_size", "pp_size", default=1)
    world = _num(payload, "world_size", default=max(tp * pp, 1))
    gpu_count = _num(payload, "gpu_count", "available_gpus")
    expected = max(tp * pp, world, 1)
    if gpu_count and expected > gpu_count:
        fault_type = "LLM::parallel_topology_gpu_count_mismatch"
        root_cause = "vLLM 并行拓扑需要的 GPU 数量超过当前可用 GPU，服务无法稳定启动或只能部分运行"
    elif "rank mismatch" in text or "world_size" in text:
        fault_type = "LLM::tensor_parallel_world_size_mismatch"
        root_cause = "tensor/pipeline parallel 参数与实际 rank/world size 不一致，导致多卡初始化异常"
    elif "model shard" in text or "shard" in text:
        fault_type = "LLM::model_shard_topology_mismatch"
        root_cause = "模型分片与并行拓扑不一致，部分 rank 无法加载对应 shard"
    else:
        fault_type = "LLM::parallel_topology_inconsistent"
        root_cause = "多卡并行拓扑、GPU placement 或 vLLM 启动参数存在不一致"
    if gpu_count:
        evidence.append(f"可用 GPU={gpu_count:g}，期望并行规模={expected:g}")
    return {
        "status": "ok",
        "confidence": min(0.93, 0.68 + 0.05 * len(evidence)),
        "summary": root_cause,
        "evidence": evidence,
        "fault_type": fault_type,
        "root_cause": root_cause,
        "root_cause_component": payload.get("deployment") or "vllm-server",
        "observed": {"tp": tp, "pp": pp, "world_size": world, "gpu_count": gpu_count},
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    ns = payload.get("namespace") or "llm-k8s"
    deploy = payload.get("deployment") or "vllm-server"
    return {
        "status": "ok",
        "confidence": 0.78,
        "summary": "生成并行拓扑修复建议；修改 vLLM 参数需审批。",
        "evidence": ["先核对启动参数、GPU allocatable 和 Pod placement"],
        "suggestions": [
            {"step": "查看 vLLM 启动参数", "command": f"kubectl -n {ns} get deployment/{deploy} -o jsonpath='{{.spec.template.spec.containers[*].args}}'", "risk": "low"},
            {"step": "查看 GPU 资源", "command": "kubectl get nodes -o custom-columns=NAME:.metadata.name,GPU:.status.allocatable.nvidia\\.com/gpu", "risk": "low"},
            {"step": "审批后修正 TP/PP 参数", "command": f"调整 deployment/{deploy} 的 tensor_parallel_size/pipeline_parallel_size 与 GPU 数量一致后 rollout", "risk": "high"},
        ],
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    ns = _payload(context).get("namespace") or "llm-k8s"
    deploy = _payload(context).get("deployment") or "vllm-server"
    return {
        "status": "ok",
        "confidence": 0.76,
        "summary": "验证多卡 rank、GPU 利用率和业务请求恢复。",
        "evidence": ["多卡拓扑验证计划"],
        "checks": [
            f"kubectl -n {ns} rollout status deployment/{deploy}",
            "检查 vLLM 日志中的 rank/world size 初始化成功",
            "检查每张 GPU 的利用率和显存均按预期变化",
        ],
    }
