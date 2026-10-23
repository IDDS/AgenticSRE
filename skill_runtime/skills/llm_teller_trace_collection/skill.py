from __future__ import annotations

from typing import Any, Dict, List


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _trace(payload: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("fine_grained_trace", "teller_trace", "trace"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _text(payload: Dict[str, Any]) -> str:
    keys = ("fault_type", "raw_fault_type", "message", "metric", "scenario", "component")
    return " ".join(str(payload.get(key) or "") for key in keys).lower()


def _is_collection_health_signal(payload: Dict[str, Any], text: str) -> bool:
    return any(token in text for token in (
        "teller_trace_stale", "trace_collection_stale", "cuda_trace_missing",
        "nvtx_only", "cupti_trace_overhead", "profiler_overhead", "request_id",
        "collector", "采集", "未更新", "只有 nvtx",
    )) or payload.get("trace_age_seconds") is not None


def _tool(context: Dict[str, Any], name: str, **kwargs: Any) -> Dict[str, Any]:
    tools = context.get("tools")
    if tools and hasattr(tools, "execute"):
        return tools.execute(name, **kwargs)
    return {"executed": False, "success": False, "tool": name, "reason": "未注入工具 runner"}


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    trace = _trace(payload)
    text = _text(payload)
    age = _num(trace.get("age_seconds") or payload.get("trace_age_seconds"), default=-1)
    cuda_events = _num(trace.get("cuda_events") or trace.get("kernel_events") or payload.get("cuda_events"))
    nvtx_events = _num(trace.get("nvtx_events") or payload.get("nvtx_events"))
    request_count = _num(trace.get("request_count") or payload.get("request_count"))
    matched = _is_collection_health_signal(payload, text) or age > 300 or (nvtx_events > 0 and cuda_events == 0)
    evidence: List[str] = []
    if age > 300:
        evidence.append(f"Trace 已 {age:.0f}s 未更新")
    if nvtx_events > 0 and cuda_events == 0:
        evidence.append("存在 NVTX 事件但 CUDA/Kernel 事件为 0")
    if request_count == 0 and _is_collection_health_signal(payload, text):
        evidence.append("Trace 缺少请求级上下文")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.86 if matched else 0.0,
        "summary": "检测到 TELLER 采集链路或请求级 Trace 质量异常" if matched else "未检测到 TELLER 采集异常",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    trace = _trace(payload)
    ns = payload.get("namespace") or "llm-k8s"
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    tool_results = [
        _tool(context, "kubectl", namespace=ns, args=["get", "pods", "-o", "wide"]),
        _tool(context, "kubectl", namespace=ns, args=["get", "configmap", "-o", "name"]),
    ]
    age = _num(trace.get("age_seconds") or payload.get("trace_age_seconds"), default=-1)
    cuda_events = _num(trace.get("cuda_events") or trace.get("kernel_events") or payload.get("cuda_events"))
    nvtx_events = _num(trace.get("nvtx_events") or payload.get("nvtx_events"))
    request_count = _num(trace.get("request_count") or payload.get("request_count"))
    if any(item.get("executed") for item in tool_results):
        evidence.append("已调用受控 kubectl 检查 TELLER/vLLM 工作负载")
    collection_signal = _is_collection_health_signal(payload, _text(payload))
    text = _text(payload)
    if "cupti_trace_overhead" in text or "profiler_overhead" in text:
        fault_type = "LLM::teller_trace_collection_degraded"
        root_cause = "TELLER 的 CUPTI/Profiler 采集开销可能影响推理执行，需要核对采样率、采集窗口和事件缓冲策略"
    elif age > 300:
        fault_type = "LLM::teller_trace_collection_stale"
        root_cause = "TELLER trace 文件长时间未更新，当前不是持续采集状态"
    elif nvtx_events > 0 and cuda_events == 0:
        fault_type = "LLM::teller_cuda_event_missing"
        root_cause = "TELLER 只采集到 NVTX，未采集到 CUDA Runtime/Kernel，可能是 CUPTI/LD_PRELOAD/权限配置缺失"
    elif request_count == 0 and collection_signal:
        fault_type = "LLM::teller_request_context_missing"
        root_cause = "Trace 缺少 request_id 或请求窗口，无法还原端到端请求执行路径"
    else:
        fault_type = "LLM::teller_trace_collection_degraded"
        root_cause = "TELLER 采集质量不足，需要核对采集器、埋点和前端解析链路"
    confidence = min(0.91, 0.66 + 0.05 * len(evidence) + (0.08 if any(item.get("executed") for item in tool_results) else 0.0))
    return {
        "status": "ok",
        "confidence": round(confidence, 3),
        "summary": root_cause,
        "evidence": evidence,
        "fault_type": fault_type,
        "root_cause": root_cause,
        "root_cause_component": payload.get("component") or payload.get("deployment") or "teller-collector",
        "observed": {
            "trace_age_seconds": age,
            "nvtx_events": nvtx_events,
            "cuda_events": cuda_events,
            "request_count": request_count,
        },
        "tool_results": tool_results,
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "ok",
        "confidence": 0.76,
        "summary": "验证 trace 持续更新、request_id 覆盖和 CUDA 事件完整性。",
        "evidence": ["TELLER 采集恢复验证计划"],
        "checks": [
            "最新 trace 文件更新时间小于采集窗口",
            "每个推理请求有 request_id 或可关联的请求窗口",
            "同一请求下存在 HTTP/vLLM/NVTX/CUDA Runtime/Kernel 事件",
            "前端 Trace 时间轴可展开事件耗时和瓶颈统计",
        ],
    }
