from __future__ import annotations

from typing import Any, Dict, List

from skill_runtime.fault_taxonomy import is_k8s_orchestration_text


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _metrics(payload: Dict[str, Any]) -> Dict[str, Any]:
    raw = payload.get("metrics")
    return raw if isinstance(raw, dict) else {}


def _number(payload: Dict[str, Any], *names: str, default: float = 0.0) -> float:
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
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    return " ".join(str(item or "") for item in (
        payload.get("domain"), payload.get("fault_type"), payload.get("message"),
        payload.get("scenario"), diagnosis.get("fault_type"), diagnosis.get("root_cause"),
    )).lower()


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    if is_k8s_orchestration_text(text):
        return {
            "status": "skipped",
            "confidence": 0.0,
            "summary": "K8s 编排类事件由通算 Kubernetes Skill 处理，跳过智算业务 SLO Skill",
            "evidence": [],
        }
    metric_names = {
        "request_success_ratio", "loadgen_success_rate", "success_rate", "request_success_rate",
        "ttft_p95_seconds", "tpot_p95_seconds",
        "e2e_p95_seconds", "waiting_requests", "running_requests", "kv_cache_usage",
        "gpu_utilization", "gpu_memory_usage",
    }
    has_metric = any(name in payload or name in _metrics(payload) for name in metric_names)
    matched = has_metric or any(token in text for token in (
        "vllm", "llm", "ttft", "tpot", "kv cache", "推理", "智算",
        "timeout", "latency", "p95", "degraded",
    ))
    evidence = []
    if has_metric:
        evidence.append("存在 vLLM 业务 SLO 或 GPU 指标")
    if "ttft" in text or "tpot" in text:
        evidence.append("故障文本包含 TTFT/TPOT 指标")
    if "timeout" in text or "latency" in text or "p95" in text:
        evidence.append("故障文本包含超时或 P95 延迟退化信号")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.8 if matched else 0.0,
        "summary": "检测到智算业务 SLO 上下文" if matched else "缺少智算业务 SLO 上下文",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    success_rate = _number(
        payload,
        "request_success_ratio", "loadgen_success_rate", "success_rate", "request_success_rate",
        default=1.0,
    )
    if 1.0 < success_rate <= 100.0:
        success_rate /= 100.0
    ttft = _number(payload, "ttft_p95_seconds", "vllm:time_to_first_token_seconds_p95")
    tpot = _number(payload, "tpot_p95_seconds", "vllm:time_per_output_token_seconds_p95")
    e2e = _number(payload, "e2e_p95_seconds", "vllm:e2e_request_latency_seconds_p95")
    waiting = _number(payload, "waiting_requests", "vllm:num_requests_waiting")
    running = _number(payload, "running_requests", "vllm:num_requests_running")
    kv_cache = _number(payload, "kv_cache_usage", "vllm:gpu_cache_usage_perc")
    gpu_util = _number(payload, "gpu_utilization", "gpu_util")
    gpu_mem = _number(payload, "gpu_memory_usage", "gpu_memory_used_ratio")
    # Public inputs accept both fractions and percentages; normalize before
    # comparing thresholds and exposing the snapshot.
    if 1.0 < kv_cache <= 100.0:
        kv_cache /= 100.0
    if 1.0 < gpu_mem <= 100.0:
        gpu_mem /= 100.0
    ttft_threshold = _number(payload, "ttft_p95_threshold_seconds", default=1.5)
    tpot_threshold = _number(payload, "tpot_p95_threshold_seconds", default=0.08)
    e2e_threshold = _number(payload, "e2e_p95_threshold_seconds", default=2.0)
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    if success_rate < 0.99:
        evidence.append(f"请求成功率 {success_rate:.1%} 低于 99%")
    if ttft > 0:
        evidence.append(f"TTFT P95={ttft:.3f}s")
    if tpot > 0:
        evidence.append(f"TPOT P95={tpot:.3f}s")
    if e2e > 0:
        evidence.append(f"E2E P95={e2e:.3f}s")
    if waiting > 0:
        evidence.append(f"等待队列 {waiting:g}")
    if kv_cache > 0:
        evidence.append(f"KV Cache 使用率 {kv_cache:.1%}" if kv_cache <= 1 else f"KV Cache 使用率 {kv_cache:.1f}%")
    if gpu_util > 0 or gpu_mem > 0:
        evidence.append(f"GPU util={gpu_util:g}, GPU memory={gpu_mem:g}")

    if success_rate < 0.99:
        root_cause = "vLLM 请求成功率下降，业务侧已经受到故障影响"
        fault_type = "LLM::business_success_rate_drop"
    elif waiting > 0 and (ttft >= ttft_threshold or e2e >= e2e_threshold):
        root_cause = "vLLM 请求队列堆积导致首 token 或端到端延迟升高"
        fault_type = "LLM::queue_latency_propagation"
    elif kv_cache >= 0.9 or gpu_mem >= 0.9:
        root_cause = "KV Cache 或 GPU 显存接近耗尽，推理调度存在退化风险"
        fault_type = "LLM::kv_cache_or_gpu_memory_pressure"
    elif ttft >= ttft_threshold:
        root_cause = "首 token 延迟超过阈值，Prefill 或模型加载阶段可能退化"
        fault_type = "LLM::ttft_regression"
    elif tpot >= tpot_threshold:
        root_cause = "输出 token 生成间隔超过阈值，Decode 阶段吞吐下降"
        fault_type = "LLM::tpot_regression"
    elif e2e >= e2e_threshold:
        root_cause = "端到端请求延迟超过阈值"
        fault_type = "LLM::e2e_latency_regression"
    else:
        root_cause = "智算业务 SLO 当前未见明显违约，但需要持续观察趋势"
        fault_type = "LLM::business_slo_observed"

    confidence = 0.88 if len(evidence) >= 3 else 0.72
    return {
        "status": "ok",
        "confidence": confidence,
        "summary": root_cause,
        "evidence": evidence,
        "fault_type": fault_type,
        "root_cause": root_cause,
        "root_cause_component": payload.get("deployment") or payload.get("service") or payload.get("host_id") or "vllm",
        "slo_snapshot": {
            "success_rate": success_rate,
            "ttft_p95_seconds": ttft,
            "tpot_p95_seconds": tpot,
            "e2e_p95_seconds": e2e,
            "waiting_requests": waiting,
            "running_requests": running,
            "kv_cache_usage": kv_cache,
            "gpu_utilization": gpu_util,
            "gpu_memory_usage": gpu_mem,
        },
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    prometheus_url = payload.get("prometheus_url") or "${PROMETHEUS_URL:-http://127.0.0.1:30090}"
    return {
        "status": "ok",
        "confidence": 0.7,
        "summary": "验证请求成功率、TTFT、TPOT、E2E P95、等待队列和 KV Cache 是否回到阈值内。",
        "evidence": ["业务 SLO 验证计划"],
        "checks": [
            f"curl -fsSG '{prometheus_url}/api/v1/query' --data-urlencode 'query=vllm:request_success_total'",
            f"curl -fsSG '{prometheus_url}/api/v1/query' --data-urlencode 'query=histogram_quantile(0.95, sum(rate(vllm:e2e_request_latency_seconds_bucket[2m])) by (le, model_name))'",
            f"curl -fsSG '{prometheus_url}/api/v1/query' --data-urlencode 'query=vllm:num_requests_waiting or vllm:gpu_cache_usage_perc'",
        ],
    }
