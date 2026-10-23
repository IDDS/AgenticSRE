from __future__ import annotations

from typing import Any, Dict, List


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _metrics(payload: Dict[str, Any]) -> Dict[str, Any]:
    value = payload.get("metrics")
    return value if isinstance(value, dict) else {}


def _text(payload: Dict[str, Any]) -> str:
    keys = ("fault_type", "raw_fault_type", "message", "service", "deployment")
    return " ".join(str(payload.get(key) or "") for key in keys).lower()


def _num(payload: Dict[str, Any], *keys: str, default: float = 0.0) -> float:
    metrics = _metrics(payload)
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
    skew = _num(payload, "replica_qps_skew", "gpu_utilization_skew")
    hot = _num(payload, "hot_replica_qps_ratio")
    matched = any(token in text for token in (
        "replica skew", "traffic imbalance", "hot replica", "endpoint imbalance",
        "sticky session", "副本不均", "流量倾斜",
    )) or skew >= 0.5 or hot >= 0.7
    evidence: List[str] = []
    if skew:
        evidence.append(f"副本/ GPU 利用率偏斜={skew:g}")
    if hot:
        evidence.append(f"热点副本流量占比={hot:g}")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.84 if matched else 0.0,
        "summary": "检测到 vLLM 多副本流量不均信号" if matched else "未检测到多副本流量不均",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    skew = _num(payload, "replica_qps_skew", "gpu_utilization_skew")
    ready = _num(payload, "ready_replicas")
    desired = _num(payload, "desired_replicas")
    if desired and ready < desired:
        fault_type = "LLM::replica_readiness_imbalance"
        root_cause = "部分 vLLM 副本未 Ready，Service 只能把流量转发到少数后端"
    elif "sticky" in text:
        fault_type = "LLM::sticky_session_replica_hotspot"
        root_cause = "入口或客户端存在 sticky session/连接复用，导致请求集中到单个副本"
    elif skew >= 0.5:
        fault_type = "LLM::replica_traffic_skew"
        root_cause = "vLLM 多副本请求或 GPU 利用率分布明显不均，热点副本拖高尾延迟"
    else:
        fault_type = "LLM::replica_load_balancing_degraded"
        root_cause = "多副本负载均衡退化，需要核对 endpoints、Pod Ready 和请求分布"
    return {
        "status": "ok",
        "confidence": min(0.91, 0.68 + 0.05 * len(evidence)),
        "summary": root_cause,
        "evidence": evidence,
        "fault_type": fault_type,
        "root_cause": root_cause,
        "root_cause_component": payload.get("service") or payload.get("deployment") or "vllm-service",
        "observed": {"skew": skew, "ready_replicas": ready, "desired_replicas": desired},
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    ns = payload.get("namespace") or "llm-k8s"
    svc = payload.get("service") or "vllm-server"
    deploy = payload.get("deployment") or "vllm-server"
    return {
        "status": "ok",
        "confidence": 0.76,
        "summary": "生成多副本流量均衡恢复建议；扩缩容和重启需审批。",
        "evidence": ["先核查 endpoint、Ready 状态和按 Pod 分组指标"],
        "suggestions": [
            {"step": "查看 endpoints", "command": f"kubectl -n {ns} get endpoints {svc} -o wide", "risk": "low"},
            {"step": "按 Pod 查询 vLLM 请求指标", "command": "Prometheus 查询按 pod 分组的 running/waiting/QPS/GPU 利用率", "risk": "low"},
            {"step": "审批后滚动恢复异常副本", "command": f"kubectl -n {ns} rollout restart deployment/{deploy}", "risk": "high"},
        ],
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "ok",
        "confidence": 0.74,
        "summary": "验证多副本请求、GPU 利用率和 P95 分布恢复。",
        "evidence": ["多副本流量验证计划"],
        "checks": [
            "按 pod 分组的 request rate 差异降低",
            "每个 Ready 副本都有 running/waiting 请求或 GPU 活动",
            "TTFT/E2E P95 回到基线",
        ],
    }
