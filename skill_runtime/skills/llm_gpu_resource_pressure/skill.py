from __future__ import annotations

from typing import Any, Dict

def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _metrics(payload: Dict[str, Any]) -> Dict[str, Any]:
    raw = payload.get("metrics")
    return raw if isinstance(raw, dict) else {}


def _number(payload: Dict[str, Any], *names: str, default: float = 0.0) -> float:
    metrics = _metrics(payload)
    metric_values = metrics.get("value") if isinstance(metrics.get("value"), dict) else {}
    aliases = {
        "gpu_memory_usage": ("gpu_memory_used",),
        "gpu_memory_used_ratio": ("gpu_memory_used",),
        "gpu_memory_used_pct": ("gpu_memory_used",),
        "kv_cache_usage": ("kv_cache", "kv_cache_usage_pct"),
        "vllm:gpu_cache_usage_perc": ("kv_cache", "kv_cache_usage_pct"),
        "waiting_requests": ("waiting",),
        "vllm:num_requests_waiting": ("waiting",),
        "running_requests": ("running",),
        "vllm:num_requests_running": ("running",),
    }
    candidates = []
    for name in names:
        candidates.append(name)
        candidates.extend(aliases.get(name, ()))
    for name in candidates:
        try:
            if name in payload:
                return float(payload.get(name))
            if name in metrics:
                return float(metrics.get(name))
            if name in metric_values:
                return float(metric_values.get(name))
        except (TypeError, ValueError):
            continue
    return default


def _text(payload: Dict[str, Any]) -> str:
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    return " ".join(str(item or "") for item in (
        payload.get("fault_type"),
        payload.get("raw_fault_type"),
        payload.get("scenario_id"),
        payload.get("scenario_name"),
        payload.get("message"),
        diagnosis.get("fault_type"),
        diagnosis.get("root_cause"),
    )).lower()


def _ratio(value: float) -> float:
    if value > 1.0:
        return value / 100.0
    return value


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    gpu_mem = _ratio(_number(payload, "gpu_memory_usage", "gpu_memory_used_ratio", "gpu_memory_used_pct"))
    kv_cache = _ratio(_number(payload, "kv_cache_usage", "vllm:gpu_cache_usage_perc"))
    matched = any(token in text for token in (
        "gpu_oom", "cuda out of memory", "out of memory", "kv_cache", "kv cache",
        "显存", "oom", "cuda", "memory pressure", "gpu memory high", "gpu pressure",
        "kv cache pressure",
    ))
    matched = matched or gpu_mem >= 0.9 or kv_cache >= 0.9
    evidence = []
    if gpu_mem > 0:
        evidence.append(f"GPU 显存使用率 {gpu_mem:.1%}")
    if kv_cache > 0:
        evidence.append(f"KV Cache 使用率 {kv_cache:.1%}")
    if any(token in text for token in ("oom", "out of memory", "显存")):
        evidence.append("故障文本包含 OOM/显存压力信号")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.86 if matched else 0.0,
        "summary": "检测到 GPU/KV Cache 资源压力" if matched else "未检测到 GPU 资源压力",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    gpu_mem = _ratio(_number(payload, "gpu_memory_usage", "gpu_memory_used_ratio", "gpu_memory_used_pct"))
    kv_cache = _ratio(_number(payload, "kv_cache_usage", "vllm:gpu_cache_usage_perc"))
    waiting = _number(payload, "waiting_requests", "vllm:num_requests_waiting")
    running = _number(payload, "running_requests", "vllm:num_requests_running")
    gpu_count = int(_number(payload, "gpu_count", default=0))
    if waiting > 0:
        evidence.append(f"等待队列 {waiting:g}")
    if running > 0:
        evidence.append(f"运行中请求 {running:g}")
    if gpu_count:
        evidence.append(f"GPU 数量 {gpu_count}")

    if kv_cache >= 0.95:
        fault_type = "LLM::kv_cache_exhaustion"
        root_cause = "KV Cache 接近或已经耗尽，连续批处理/长上下文请求导致新请求排队或失败"
    elif gpu_mem >= 0.95:
        fault_type = "LLM::gpu_memory_oom"
        root_cause = "GPU 显存接近耗尽，vLLM 可能触发 CUDA OOM 或无法继续分配 KV Cache"
    elif gpu_mem >= 0.9 or kv_cache >= 0.9:
        fault_type = "LLM::gpu_memory_pressure"
        root_cause = "GPU 显存或 KV Cache 高水位，推理吞吐和首 token 延迟存在退化风险"
    else:
        fault_type = "LLM::gpu_resource_pressure"
        root_cause = "智算 GPU 资源压力信号存在，需要结合 DCGM/vLLM 指标确认"

    confidence = 0.9 if len(evidence) >= 3 else 0.78
    return {
        "status": "ok",
        "confidence": confidence,
        "summary": root_cause,
        "evidence": evidence,
        "fault_type": fault_type,
        "root_cause": root_cause,
        "root_cause_component": payload.get("deployment") or payload.get("host_id") or "vllm-server",
        "resource_snapshot": {
            "gpu_memory_usage": gpu_mem,
            "kv_cache_usage": kv_cache,
            "waiting_requests": waiting,
            "running_requests": running,
            "gpu_count": gpu_count,
        },
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    namespace = payload.get("namespace") or "llm-k8s"
    deployment = payload.get("deployment") or "vllm-server"
    prometheus_url = payload.get("prometheus_url") or "${PROMETHEUS_URL:-http://127.0.0.1:30090}"
    endpoint = payload.get("endpoint") or payload.get("vllm_endpoint") or "${VLLM_ENDPOINT:-http://127.0.0.1:30080/v1/completions}"
    model = payload.get("model") or "vllm"
    return {
        "status": "ok",
        "confidence": 0.82,
        "summary": "生成 GPU/KV Cache 压力恢复建议；真实变更必须审批，避免误重启推理服务。",
        "evidence": ["恢复建议包含指标确认、业务限流、受控重启和回滚验证"],
        "suggestions": [
            {
                "step": "查询 GPU 显存和 KV Cache 指标",
                "command": f"curl -fsSG '{prometheus_url}/api/v1/query' --data-urlencode 'query=DCGM_FI_DEV_FB_USED or vllm:gpu_cache_usage_perc'",
                "risk": "low",
            },
            {
                "step": "查询 vLLM 队列堆积情况",
                "command": f"curl -fsSG '{prometheus_url}/api/v1/query' --data-urlencode 'query=vllm:num_requests_waiting or vllm:num_requests_running'",
                "risk": "low",
            },
            {
                "step": "审批后滚动重启 vLLM Deployment 释放异常显存状态",
                "command": f"kubectl rollout restart deployment/{deployment} -n {namespace}",
                "rollback_command": f"kubectl rollout undo deployment/{deployment} -n {namespace}",
                "risk": "medium",
            },
            {
                "step": "恢复后执行低并发推理请求验证",
                "command": f"curl -fsS -H 'Content-Type: application/json' -d '{{\"model\":\"{model}\",\"prompt\":\"请输出一句中文健康检查。\",\"max_tokens\":16,\"temperature\":0}}' '{endpoint}'",
                "risk": "low",
            },
        ],
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    namespace = payload.get("namespace") or "llm-k8s"
    deployment = payload.get("deployment") or "vllm-server"
    prometheus_url = payload.get("prometheus_url") or "${PROMETHEUS_URL:-http://127.0.0.1:30090}"
    return {
        "status": "ok",
        "confidence": 0.78,
        "summary": "验证 GPU 显存、KV Cache、请求队列和 Deployment rollout 是否恢复。",
        "evidence": ["GPU 资源压力恢复验证计划"],
        "checks": [
            f"kubectl -n {namespace} rollout status deployment/{deployment}",
            f"curl -fsSG '{prometheus_url}/api/v1/query' --data-urlencode 'query=DCGM_FI_DEV_FB_USED or DCGM_FI_DEV_GPU_UTIL'",
            f"curl -fsSG '{prometheus_url}/api/v1/query' --data-urlencode 'query=vllm:gpu_cache_usage_perc or vllm:num_requests_waiting'",
        ],
    }
