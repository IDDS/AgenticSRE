from __future__ import annotations

from typing import Any, Dict, List, Tuple


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _trace(payload: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("fine_grained_trace", "teller_trace", "trace"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _summary(trace: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    value = trace.get("summary")
    if isinstance(value, dict):
        return value
    value = payload.get("fine_grained_trace_summary")
    return value if isinstance(value, dict) else {}


def _request_context(trace: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    value = trace.get("request_context")
    if isinstance(value, dict):
        return value
    value = payload.get("request_context")
    return value if isinstance(value, dict) else {}


def _top_kernels(trace: Dict[str, Any], payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    value = trace.get("top_kernels")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    value = payload.get("top_kernels")
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _event_type_stats(trace: Dict[str, Any], payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    value = trace.get("event_type_stats")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _requests(request_ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    value = request_ctx.get("requests")
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _max_request_span(requests: List[Dict[str, Any]]) -> float:
    return max([_number(item.get("span_ms")) for item in requests] or [0.0])


def _max_request_row(requests: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not requests:
        return {}
    return max(requests, key=lambda item: (_number(item.get("span_ms")), _number(item.get("event_count"))))


def _contains_teller_signal(payload: Dict[str, Any]) -> bool:
    text = " ".join(str(payload.get(key) or "") for key in ("message", "fault_type", "raw_fault_type", "metric")).lower()
    if any(token in text for token in (
        "teller_trace_stale", "trace_collection_stale", "cuda_trace_missing",
        "nvtx_only", "collector", "采集", "未更新", "只有 nvtx",
    )):
        return False
    return any(token in text for token in ("teller", "trace", "kernel", "cuda", "request_span", "请求级", "调用路径"))


def _threshold(payload: Dict[str, Any], name: str, default: float) -> float:
    thresholds = payload.get("thresholds") if isinstance(payload.get("thresholds"), dict) else {}
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    metric_threshold = metrics.get("threshold") if name == "teller_request_span_ms" else None
    return _number(thresholds.get(name) or metric_threshold, default)


def _diagnose_type(
    *,
    summary: Dict[str, Any],
    request_ctx: Dict[str, Any],
    requests: List[Dict[str, Any]],
    payload: Dict[str, Any],
) -> Tuple[str, str, List[str]]:
    kernel_p95 = _number(summary.get("kernel_p95_ms"))
    kernel_events = _number(summary.get("kernel_events"))
    runtime_events = _number(summary.get("runtime_events"))
    driver_events = _number(summary.get("driver_events"))
    request_count = int(_number(request_ctx.get("request_count") or summary.get("request_count")))
    max_span = _max_request_span(requests)
    span_threshold = _threshold(payload, "teller_request_span_ms", 1000.0)
    kernel_threshold = _threshold(payload, "teller_kernel_p95_ms", 50.0)
    evidence: List[str] = []
    if request_count:
        evidence.append(f"TELLER 已识别 {request_count} 个请求级上下文")
    if max_span:
        evidence.append(f"最大请求执行跨度 {max_span:.3f}ms")
    if kernel_events:
        evidence.append(f"CUDA Kernel 事件 {int(kernel_events)} 条")
    if runtime_events:
        evidence.append(f"CUDA Runtime 事件 {int(runtime_events)} 条")
    if driver_events:
        evidence.append(f"CUDA Driver 事件 {int(driver_events)} 条")
    if kernel_p95:
        evidence.append(f"Kernel P95={kernel_p95:.3f}ms")

    if not request_ctx.get("available"):
        return (
            "LLM::teller_request_context_missing",
            "TELLER 已采集到细粒度事件，但缺少 request_id 或请求窗口，无法把 CUDA 事件可靠归因到单个推理请求",
            evidence,
        )
    if max_span >= span_threshold and kernel_p95 < kernel_threshold and runtime_events >= max(kernel_events, 1) * 1.2:
        return (
            "LLM::teller_runtime_overhead",
            "请求级 span 异常升高，但 Kernel P95 不高，瓶颈更可能在 CUDA Runtime 调度、同步或 vLLM 请求编排阶段",
            evidence,
        )
    if kernel_p95 >= kernel_threshold:
        return (
            "LLM::teller_kernel_bottleneck",
            "TELLER 发现 CUDA Kernel P95 耗时超过阈值，瓶颈更可能位于算子执行、GPU 调度或资源竞争",
            evidence,
        )
    if max_span >= span_threshold:
        return (
            "LLM::teller_request_path_latency",
            "TELLER 请求级执行跨度超过阈值，需要沿请求调用路径检查 prefill/decode、Runtime、Driver 与 Kernel 阶段",
            evidence,
        )
    return (
        "LLM::teller_trace_observed",
        "TELLER 请求级调用路径已采集，当前未达到内置异常阈值，可作为其他诊断的辅助证据",
        evidence,
    )


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    trace = _trace(payload)
    summary = _summary(trace, payload)
    request_ctx = _request_context(trace, payload)
    matched = bool(trace.get("available") or summary or request_ctx or _contains_teller_signal(payload))
    evidence: List[str] = []
    if trace.get("available"):
        evidence.append("TELLER trace 可用")
    if request_ctx.get("available"):
        evidence.append(f"请求级上下文可用，request_count={request_ctx.get('request_count')}")
    if summary.get("kernel_events"):
        evidence.append(f"Kernel 事件数 {summary.get('kernel_events')}")
    if summary.get("runtime_events"):
        evidence.append(f"Runtime 事件数 {summary.get('runtime_events')}")
    if _contains_teller_signal(payload):
        evidence.append("事件文本包含 TELLER/CUDA/请求级 trace 信号")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.84 if matched else 0.0,
        "summary": "检测到可用于请求级诊断的 TELLER trace 信号" if matched else "未检测到 TELLER 请求级 trace 信号",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    trace = _trace(payload)
    summary = _summary(trace, payload)
    request_ctx = _request_context(trace, payload)
    requests = _requests(request_ctx)
    top_kernels = _top_kernels(trace, payload)
    event_stats = _event_type_stats(trace, payload)
    fault_type, root_cause, evidence = _diagnose_type(
        summary=summary,
        request_ctx=request_ctx,
        requests=requests,
        payload=payload,
    )
    evidence = list((context.get("detect") or {}).get("evidence") or []) + evidence
    max_request = _max_request_row(requests)
    if max_request:
        evidence.append(
            "最慢请求 "
            f"{max_request.get('request_id') or '-'}: span={_number(max_request.get('span_ms')):.3f}ms, "
            f"kernel={int(_number(max_request.get('kernel_events')))}, runtime={int(_number(max_request.get('runtime_events')))}"
        )
    if top_kernels:
        kernel = top_kernels[0]
        evidence.append(
            f"Top Kernel: {kernel.get('name') or '-'}，count={kernel.get('count') or 0}，"
            f"p95={_number(kernel.get('p95_ms')):.3f}ms"
        )

    sources = 0
    if request_ctx.get("available"):
        sources += 1
    if summary.get("kernel_events") or summary.get("runtime_events"):
        sources += 1
    if top_kernels:
        sources += 1
    if payload.get("evidence"):
        sources += 1
    confidence = min(0.93, 0.58 + 0.1 * sources + min(len(evidence), 5) * 0.03)
    if fault_type == "LLM::teller_request_context_missing":
        confidence = min(confidence, 0.72)

    return {
        "status": "ok",
        "confidence": round(confidence, 3),
        "summary": f"{root_cause}。",
        "evidence": evidence,
        "fault_type": fault_type,
        "root_cause": root_cause,
        "root_cause_component": payload.get("deployment") or payload.get("service") or "vllm",
        "confidence_explanation": {
            "method": "teller_request_trace_rules_v1",
            "signals": {
                "request_context_available": bool(request_ctx.get("available")),
                "request_count": request_ctx.get("request_count") or summary.get("request_count") or 0,
                "kernel_events": summary.get("kernel_events") or 0,
                "runtime_events": summary.get("runtime_events") or 0,
                "kernel_p95_ms": summary.get("kernel_p95_ms") or 0,
                "max_request_span_ms": _max_request_span(requests),
                "top_kernel_count": len(top_kernels),
                "event_type_stat_count": len(event_stats),
            },
            "reason": "按请求窗口、CUDA Runtime/Driver/Kernel 统计和 Top Kernel 证据加权。",
        },
        "trace_diagnosis": {
            "request_context": {
                "available": bool(request_ctx.get("available")),
                "request_count": request_ctx.get("request_count") or 0,
                "coverage": request_ctx.get("coverage") or "none",
            },
            "slowest_request": max_request,
            "top_kernels": top_kernels[:5],
            "event_type_stats": event_stats[:8],
            "summary": summary,
        },
    }
