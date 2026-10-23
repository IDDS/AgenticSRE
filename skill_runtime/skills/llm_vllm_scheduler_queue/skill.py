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
    parts = [str(payload.get(k, "")) for k in (
        "fault_type", "raw_fault_type", "scenario_id", "scenario_name", "message", "model", "metric"
    )]
    for item in payload.get("evidence") or []:
        if isinstance(item, dict):
            parts.extend(str(item.get(key) or "") for key in ("source", "signal", "value", "message"))
        elif item:
            parts.append(str(item))
    return " ".join(parts).lower()


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    waiting = _num(payload, "waiting_requests", "vllm:num_requests_waiting")
    ttft = _num(payload, "ttft_p95_seconds", "vllm_ttft_p95_seconds")
    e2e = _num(payload, "e2e_p95_seconds", "vllm_e2e_p95_seconds")
    explicit_degradation = any(token in text for token in (
        "request_queue_overflow", "continuous_batch", "prefill_decode",
        "attention_backend", "scheduler backlog", "queue backlog",
        "队列堆积", "调度退化", "首 token 延迟升高",
    ))
    matched = any(token in text for token in (
        "request_queue_overflow", "continuous_batch", "prefill_decode",
        "attention_backend",
    )) or explicit_degradation or waiting > 0 or ttft >= 1.0 or e2e >= 3.0
    evidence = []
    if waiting > 0:
        evidence.append(f"vLLM 等待队列 {waiting:g}")
    if ttft > 0:
        evidence.append(f"TTFT P95={ttft:.3f}s")
    if e2e > 0:
        evidence.append(f"E2E P95={e2e:.3f}s")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.86 if matched else 0.0,
        "summary": "检测到 vLLM 队列或调度退化信号" if matched else "未检测到 vLLM 调度退化信号",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    waiting = _num(payload, "waiting_requests", "vllm:num_requests_waiting")
    running = _num(payload, "running_requests", "vllm:num_requests_running")
    loadgen_errors = _num(payload, "loadgen_errors")
    ttft = _num(payload, "ttft_p95_seconds", "vllm_ttft_p95_seconds")
    e2e = _num(payload, "e2e_p95_seconds", "vllm_e2e_p95_seconds")
    if running > 0:
        evidence.append(f"运行中请求 {running:g}")
    if loadgen_errors > 0:
        evidence.append(f"负载生成器错误 {loadgen_errors:g}")
    if waiting > 0 and ttft >= 1.0:
        fault_type = "LLM::prefill_or_scheduler_queue_backlog"
        root_cause = "vLLM 等待队列堆积并伴随首 token 延迟升高，疑似 Prefill 或调度路径成为瓶颈"
    elif waiting > 0:
        fault_type = "LLM::vllm_waiting_queue_backlog"
        root_cause = "vLLM 等待队列堆积，服务处理能力低于当前请求负载"
    elif e2e >= 3.0 or ttft >= 1.0:
        fault_type = "LLM::vllm_scheduler_latency_regression"
        root_cause = "vLLM 调度或执行路径时延升高，导致 TTFT/E2E P95 超过阈值"
    else:
        fault_type = "LLM::vllm_scheduler_queue_pressure"
        root_cause = "vLLM 请求队列或调度路径退化，导致业务请求排队和首 token 延迟升高"
    confidence = 0.9 if len(evidence) >= 3 else 0.8
    return {
        "status": "ok",
        "confidence": confidence,
        "summary": root_cause,
        "evidence": evidence,
        "fault_type": fault_type,
        "root_cause": root_cause,
        "root_cause_component": payload.get("deployment") or "vllm-server",
        "observed": {
            "waiting_requests": waiting,
            "running_requests": running,
            "loadgen_errors": loadgen_errors,
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
        "summary": "生成 vLLM 队列/调度恢复建议；调参和重启需审批。",
        "evidence": ["恢复建议包含队列指标复核、限流和受控 rollout"],
        "suggestions": [
            {"step": "查询 vLLM 队列和 P95", "command": f"curl -fsSG '{prom}/api/v1/query' --data-urlencode 'query=vllm:num_requests_waiting or vllm:num_requests_running'", "risk": "low"},
            {"step": "临时降低压测并发或业务入口限流", "command": "调整负载生成器并发或上游限流参数，先 dry-run 评估影响", "risk": "medium"},
            {"step": "审批后滚动重启 vLLM", "command": f"kubectl rollout restart deployment/{deploy} -n {ns}", "rollback_command": f"kubectl rollout undo deployment/{deploy} -n {ns}", "risk": "medium"},
        ],
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    ns = payload.get("namespace") or "llm-k8s"
    deploy = payload.get("deployment") or "vllm-server"
    prom = payload.get("prometheus_url") or "${PROMETHEUS_URL:-http://127.0.0.1:30090}"
    return {
        "status": "ok",
        "confidence": 0.78,
        "summary": "验证等待队列、TTFT/E2E P95 和 Deployment 状态。",
        "evidence": ["vLLM 调度恢复验证计划"],
        "checks": [
            f"kubectl -n {ns} rollout status deployment/{deploy}",
            f"curl -fsSG '{prom}/api/v1/query' --data-urlencode 'query=vllm:num_requests_waiting'",
            f"curl -fsSG '{prom}/api/v1/query' --data-urlencode 'query=histogram_quantile(0.95, sum(rate(vllm:e2e_request_latency_seconds_bucket[2m])) by (le, model_name))'",
        ],
    }
