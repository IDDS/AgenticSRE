from __future__ import annotations

from typing import Any, Dict, List


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    keys = (
        "fault_type", "raw_fault_type", "message", "error", "endpoint",
        "model", "path", "endpoint_status", "service", "deployment",
    )
    return " ".join(str(payload.get(key) or "") for key in keys).lower()


def _status(payload: Dict[str, Any]) -> int:
    try:
        return int(payload.get("http_status") or payload.get("status_code") or 0)
    except (TypeError, ValueError):
        return 0


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    status = _status(payload)
    endpoint_status = str(payload.get("endpoint_status") or "").lower()
    try:
        endpoint_count = int(float(payload.get("endpoint_count")))
    except (TypeError, ValueError):
        endpoint_count = -1
    explicit_error = any(token in text for token in (
        "model_not_found", "model not found", "openai_api_error", "bad_request",
        "模型名", "请求参数",
        "endpoint_timeout", "observable_endpoint_unavailable", "service endpoints 为空",
        "端点不可用", "推理端点不可用",
    ))
    api_request_context = any(token in text for token in (
        "/v1/models", "/v1/completions", "chat/completions", "vllm", "model", "openai", "模型", "service",
    ))
    matched = explicit_error or (status in {400, 404, 422, 503} and api_request_context)
    matched = matched or endpoint_status in {"failed", "down", "unavailable"} or endpoint_count == 0
    evidence: List[str] = []
    if status:
        evidence.append(f"HTTP 状态码 {status}")
    if endpoint_status:
        evidence.append(f"推理端点状态 {endpoint_status}")
    if endpoint_count >= 0:
        evidence.append(f"Service endpoint 数 {endpoint_count}")
    if payload.get("model"):
        evidence.append(f"请求模型 {payload.get('model')}")
    if payload.get("endpoint"):
        evidence.append(f"请求入口 {payload.get('endpoint')}")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.84 if matched else 0.0,
        "summary": "检测到模型服务入口或 API 请求异常" if matched else "未检测到模型服务入口异常",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    status = _status(payload)
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    if "model_not_found" in text or "model not found" in text or status == 404:
        fault_type = "LLM::model_serving_model_not_found"
        root_cause = "请求模型名与 vLLM 已加载模型不一致，或模型服务未成功加载目标模型"
    elif status in {400, 422} or "bad_request" in text:
        fault_type = "LLM::model_serving_bad_request"
        root_cause = "OpenAI 兼容 API 请求参数不符合当前 vLLM endpoint 或模型能力"
    elif status == 503 or "connection refused" in text or str(payload.get("endpoint_status") or "").lower() in {"failed", "down", "unavailable"}:
        fault_type = "LLM::model_serving_endpoint_unavailable"
        if payload.get("endpoint_count") in {0, 0.0, "0"}:
            root_cause = "模型服务入口不可用，Kubernetes Service 当前没有可用 Endpoint，优先检查 selector、Pod Ready 和 Service 端口"
        else:
            root_cause = "模型服务入口不可用，可能是 Service/Pod/端口或 vLLM 进程异常"
    else:
        fault_type = "LLM::model_serving_endpoint_error"
        root_cause = "模型服务入口、模型名或请求路径配置不一致"
    confidence = min(0.9, 0.68 + 0.05 * len(evidence) + (0.08 if status else 0.0))
    return {
        "status": "ok",
        "confidence": round(confidence, 3),
        "summary": root_cause,
        "evidence": evidence,
        "fault_type": fault_type,
        "root_cause": root_cause,
        "root_cause_component": payload.get("service") or payload.get("deployment") or "vllm-endpoint",
        "observed": {
            "http_status": status,
            "endpoint": payload.get("endpoint") or payload.get("base_url"),
            "model": payload.get("model"),
        },
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    base = payload.get("base_url") or payload.get("endpoint_base") or "${VLLM_BASE_URL:-http://127.0.0.1:30081}"
    model = payload.get("model") or "${VLLM_MODEL}"
    return {
        "status": "ok",
        "confidence": 0.78,
        "summary": "生成模型服务入口修复建议；配置变更需审批。",
        "evidence": ["先确认模型列表，再修正请求路径和模型名"],
        "suggestions": [
            {"step": "查询模型列表", "command": f"curl -fsS {base}/v1/models", "risk": "low"},
            {"step": "小 token 探测", "command": f"curl -fsS {base}/v1/completions -H 'Content-Type: application/json' -d '{{\"model\":\"{model}\",\"prompt\":\"ping\",\"max_tokens\":8}}'", "risk": "low"},
            {"step": "审批后修正前端/负载生成器模型配置", "command": "将 loadgen/UI 中的 model 与 /v1/models 返回值保持一致", "risk": "medium"},
        ],
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "ok",
        "confidence": 0.76,
        "summary": "验证模型列表、小请求和 vLLM 指标均恢复。",
        "evidence": ["模型服务入口验证计划"],
        "checks": [
            "/v1/models 返回目标模型",
            "/v1/completions 或 /v1/chat/completions 小 token 请求返回 2xx",
            "vLLM request success/running 指标出现对应请求",
            "前端负载生成器使用同一 endpoint 和 model",
        ],
    }
