from __future__ import annotations

from typing import Any, Dict, List


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _metrics(payload: Dict[str, Any]) -> Dict[str, Any]:
    value = payload.get("metrics")
    return value if isinstance(value, dict) else {}


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
    keys = ("fault_type", "message", "scenario", "endpoint", "model")
    return " ".join(str(payload.get(key) or "") for key in keys).lower()


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    loadgen_requests = _num(payload, "loadgen_requests", "request_count")
    request_rate = _num(payload, "request_rate", "request_rate_5m", "successful_requests_per_second")
    errors = _num(payload, "loadgen_errors", "request_errors")
    has_loadgen_metric = any(key in payload or key in _metrics(payload) for key in (
        "loadgen_requests", "request_count", "request_rate", "request_rate_5m",
        "successful_requests_per_second", "loadgen_errors", "request_errors",
    ))
    negated_loadgen_context = any(token in text for token in (
        "没有负载生成器指标", "缺少负载生成器指标", "无负载生成器指标",
        "no loadgen metric", "without loadgen metric",
    ))
    matched = any(token in text for token in (
        "loadgen", "background traffic", "traffic_zero", "request_rate_zero", "no traffic",
        "负载生成器", "背景流量", "请求为 0",
    )) or (has_loadgen_metric and loadgen_requests == 0 and "vllm" in text) or errors > 0
    if negated_loadgen_context and not has_loadgen_metric and errors <= 0:
        matched = False
    evidence: List[str] = []
    if has_loadgen_metric and loadgen_requests == 0 and "vllm" in text:
        evidence.append("负载生成请求数为 0")
    if has_loadgen_metric and request_rate == 0 and "vllm" in text:
        evidence.append("成功请求速率为 0")
    if errors > 0:
        evidence.append(f"负载生成错误 {errors:g}")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.84 if matched else 0.0,
        "summary": "检测到 vLLM 负载生成或背景流量异常" if matched else "未检测到负载生成异常",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    endpoint_status = str(payload.get("endpoint_status") or "").lower()
    model = payload.get("model") or ""
    endpoint = payload.get("endpoint") or payload.get("base_url") or ""
    loadgen_requests = _num(payload, "loadgen_requests", "request_count")
    request_rate = _num(payload, "request_rate", "request_rate_5m", "successful_requests_per_second")
    errors = _num(payload, "loadgen_errors", "request_errors")
    if endpoint:
        evidence.append(f"目标 endpoint={endpoint}")
    if model:
        evidence.append(f"目标模型={model}")
    if "failed" in endpoint_status or "down" in endpoint_status:
        fault_type = "LLM::load_generator_target_unreachable"
        root_cause = "负载生成器目标 vLLM endpoint 不可达，请求没有稳定进入推理服务"
    elif errors > 0:
        fault_type = "LLM::load_generator_request_errors"
        root_cause = "负载生成请求出现错误，可能是模型名、API 参数、并发或服务状态不匹配"
    elif loadgen_requests == 0 or request_rate == 0:
        fault_type = "LLM::load_generator_no_traffic"
        root_cause = "实验窗口内没有有效背景流量，因此注入故障后前端指标不一定出现变化"
    else:
        fault_type = "LLM::load_generator_needs_calibration"
        root_cause = "负载生成器已产生流量，但仍需校准并发、持续时间和模型参数以稳定放大故障效果"
    confidence = min(0.9, 0.68 + 0.05 * len(evidence))
    return {
        "status": "ok",
        "confidence": round(confidence, 3),
        "summary": root_cause,
        "evidence": evidence,
        "fault_type": fault_type,
        "root_cause": root_cause,
        "root_cause_component": payload.get("component") or "vllm-loadgen",
        "observed": {
            "loadgen_requests": loadgen_requests,
            "request_rate": request_rate,
            "loadgen_errors": errors,
        },
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    prom = (_payload(context).get("prometheus_url") or "http://127.0.0.1:19090")
    return {
        "status": "ok",
        "confidence": 0.76,
        "summary": "验证背景流量进入 vLLM 且前端曲线可观察。",
        "evidence": ["负载生成验证计划"],
        "checks": [
            f"curl -fsSG '{prom}/api/v1/query' --data-urlencode 'query=vllm:num_requests_running or vllm:num_requests_waiting'",
            f"curl -fsSG '{prom}/api/v1/query' --data-urlencode 'query=rate(vllm:request_success_total[2m])'",
            "前端观测页在 baseline/fault/recovery 三段均有数据点",
        ],
    }
