from __future__ import annotations

from typing import Any, Dict

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
    return " ".join(str(payload.get(k, "")) for k in (
        "fault_type", "raw_fault_type", "scenario_id", "scenario_name", "message", "error", "model"
    )).lower()


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    failures = _num(payload, "request_failures", "loadgen_errors")
    matched = any(token in text for token in (
        "nccl", "rdma", "tensor parallel", "tp_communication", "packet_loss",
        "bandwidth", "grpc_failure", "communication", "通信", "丢包", "带宽",
    )) or failures > 0 and _num(payload, "gpu_count", default=0) > 1
    evidence = []
    if failures > 0:
        evidence.append(f"请求失败 {failures:g}")
    if _num(payload, "gpu_count", default=0) > 1:
        evidence.append(f"多卡 GPU 数量 {_num(payload, 'gpu_count'):g}")
    if payload.get("node"):
        evidence.append(f"节点 {payload.get('node')}")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.86 if matched else 0.0,
        "summary": "检测到分布式推理通信异常信号" if matched else "未检测到分布式通信异常",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    text = _text(payload)
    if any(token in text for token in ("nccl", "allreduce", "tensor parallel")):
        fault_type = "LLM::nccl_tensor_parallel_communication_failure"
        root = "NCCL 或 Tensor Parallel 通信异常，导致多卡同步失败、请求失败或尾延迟升高"
    elif any(token in text for token in ("rdma", "ib", "infiniband")):
        fault_type = "LLM::rdma_network_degradation"
        root = "RDMA/IB 网络链路异常，导致分布式推理通信退化"
    else:
        fault_type = "LLM::distributed_inference_communication_failure"
        root = "分布式推理通信链路异常，NCCL/RDMA/TP 同步或网络带宽导致请求失败和尾延迟上升"
    return {
        "status": "ok",
        "confidence": 0.9 if len(evidence) >= 2 else 0.8,
        "summary": root,
        "evidence": evidence or ["命中分布式通信故障类型"],
        "fault_type": fault_type,
        "root_cause": root,
        "root_cause_component": payload.get("deployment") or payload.get("service") or "vllm-distributed",
        "observed": {
            "gpu_count": _num(payload, "gpu_count", default=0),
            "request_failures": _num(payload, "request_failures", "loadgen_errors"),
            "tp_size": payload.get("tensor_parallel_size") or payload.get("tp_size") or "",
        },
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    ns = payload.get("namespace") or "llm-k8s"
    deploy = payload.get("deployment") or "vllm-server"
    prom = payload.get("prometheus_url") or "${PROMETHEUS_URL:-http://127.0.0.1:30090}"
    return {
        "status": "ok",
        "confidence": 0.82,
        "summary": "生成分布式通信恢复建议；节点和网络操作必须审批。",
        "evidence": ["建议先核查 NCCL/RDMA 日志、Pod 分布和网络指标"],
        "suggestions": [
            {"step": "查看 NCCL/RDMA/vLLM 错误日志", "command": f"kubectl -n {ns} logs deploy/{deploy} --tail=200 | egrep -i 'nccl|rdma|grpc|timeout|connection|ib|tensor'", "risk": "low"},
            {"step": "查看 vLLM Pod 与节点分布", "command": f"kubectl -n {ns} get pods -o wide -l app=vllm", "risk": "low"},
            {"step": "查询网络和请求失败指标", "command": f"curl -fsSG '{prom}/api/v1/query' --data-urlencode 'query=vllm:request_failure_total or rate(container_network_transmit_errors_total[2m])'", "risk": "low"},
            {"step": "审批后滚动重启分布式 vLLM 工作负载", "command": f"kubectl rollout restart deployment/{deploy} -n {ns}", "rollback_command": f"kubectl rollout undo deployment/{deploy} -n {ns}", "risk": "medium"},
        ],
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    ns = payload.get("namespace") or "llm-k8s"
    deploy = payload.get("deployment") or "vllm-server"
    return {
        "status": "ok",
        "confidence": 0.78,
        "summary": "验证 NCCL/RDMA 错误、Pod 分布、请求失败率和 P95 是否恢复。",
        "evidence": ["分布式通信恢复验证计划"],
        "checks": [
            f"kubectl -n {ns} rollout status deployment/{deploy}",
            f"kubectl -n {ns} logs deploy/{deploy} --tail=100 | egrep -i 'nccl|rdma|grpc|timeout|error' || true",
            "执行多卡模型推理请求，确认请求成功率和 P95 恢复",
        ],
    }
