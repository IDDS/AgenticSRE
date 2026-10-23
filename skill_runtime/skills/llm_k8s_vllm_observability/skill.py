from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from skill_runtime.confidence import calibrate_llm_fault_confidence
from skill_runtime.fault_taxonomy import classify_observable_signal

def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _text(payload: Dict[str, Any]) -> str:
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    parts = [
        payload.get("domain"),
        payload.get("fault_type"),
        payload.get("scenario"),
        payload.get("message"),
        payload.get("endpoint_status"),
        payload.get("metrics_status"),
        payload.get("target_health"),
        payload.get("deployment"),
        payload.get("service"),
        diagnosis.get("fault_type"),
        diagnosis.get("root_cause"),
    ]
    return " ".join(str(item or "") for item in parts).lower()


def _metrics(payload: Dict[str, Any]) -> Dict[str, Any]:
    raw = payload.get("metrics")
    return raw if isinstance(raw, dict) else {}


def _evidence(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = payload.get("evidence")
    return raw if isinstance(raw, list) else []


def _node_resource_signal_text(payload: Dict[str, Any]) -> str:
    metrics = _metrics(payload)
    metric_name = str(metrics.get("metric") or metrics.get("name") or metrics.get("signal") or payload.get("metric") or "").lower()
    if metric_name and not metric_name.startswith("node_"):
        return ""
    evidence_text = " ".join(
        " ".join(str(item.get(key) or "") for key in ("source", "signal", "metric", "label", "value"))
        for item in _evidence(payload)
        if isinstance(item, dict)
        and str(item.get("signal") or item.get("metric") or "").lower().startswith("node_")
    ).lower()
    metric_text = " ".join(str(metrics.get(key) or "") for key in ("metric", "name", "signal")).lower()
    payload_text = " ".join(str(payload.get(key) or "") for key in ("metric", "message", "raw_fault_type", "fault_type")).lower()
    return f"{metric_text} {payload_text} {evidence_text}"


def _has_node_resource_metric(payload: Dict[str, Any]) -> bool:
    metrics = _metrics(payload)
    metric_name = str(metrics.get("metric") or metrics.get("name") or metrics.get("signal") or payload.get("metric") or "").lower()
    if metric_name.startswith("node_"):
        return True
    if metric_name and not metric_name.startswith("node_"):
        metric_name = ""
    background_signals = {
        str(item.get("signal") or item.get("metric") or "").lower()
        for item in payload.get("background_observation_signals") or []
        if isinstance(item, dict)
    }
    active_node_signal = any(
        isinstance(item, dict)
        and str(item.get("signal") or item.get("metric") or "").lower().startswith("node_")
        and str(item.get("signal") or item.get("metric") or "").lower() not in background_signals
        for item in payload.get("observation_signals") or []
    )
    if active_node_signal:
        return True
    raw = " ".join(str(payload.get(key) or "") for key in ("fault_type", "raw_fault_type", "event_type")).lower()
    if "node_resource_pressure" in raw or "observable_resource_pressure" in raw:
        return True
    if "fault_experiment_signal" in raw:
        return False
    if any(
        key in payload and key not in background_signals
        for key in (
            "node_cpu_usage", "node_memory_usage", "node_rootfs_usage",
            "node_disk_read", "node_disk_write", "node_network_receive", "node_network_transmit",
        )
    ):
        return True
    has_node_evidence = any(
        isinstance(item, dict)
        and str(item.get("signal") or item.get("metric") or "").lower().startswith("node_")
        and str(item.get("signal") or item.get("metric") or "").lower() not in background_signals
        for item in _evidence(payload)
    )
    if not metric_name and not has_node_evidence:
        return False
    signal = classify_observable_signal("llm_node_resource_pressure", _node_resource_signal_text(payload))
    return bool(signal.get("fault_type")) and str(signal.get("name") or "") != "default"


def _resource_pressure_kind(payload: Dict[str, Any], text: str) -> Tuple[str, str]:
    signal = classify_observable_signal("llm_node_resource_pressure", f"{text} {_node_resource_signal_text(payload)}")
    return str(signal.get("label") or "OS/节点"), str(signal.get("fault_type") or "LLM::k8s_vllm_os_resource_pressure")


def _fault_type(payload: Dict[str, Any]) -> str:
    return str(payload.get("fault_type") or "").lower()


def _has_k8s_vllm_context(payload: Dict[str, Any], text: str) -> bool:
    has_k8s_object = bool(payload.get("namespace") or payload.get("deployment") or payload.get("service") or payload.get("pod"))
    has_vllm_signal = any(token in text for token in ("vllm", "inference", "推理", "ttft", "e2e"))
    has_vllm_metric = any(str(key).startswith("vllm") or key in {"e2e_p95_seconds", "ttft_p95_seconds"} for key in _metrics(payload))
    has_vllm_metric = has_vllm_metric or any(key in payload for key in ("e2e_p95_seconds", "ttft_p95_seconds", "request_failures"))
    if payload.get("host_id") and not has_k8s_object:
        return False
    return has_k8s_object and (has_vllm_signal or has_vllm_metric)


def _metric(payload: Dict[str, Any], *names: str, default: float = 0.0) -> float:
    metrics = _metrics(payload)
    for name in names:
        if name in payload:
            return _number(payload.get(name), default)
        if name in metrics:
            return _number(metrics.get(name), default)
    return default


def _completion_endpoint(payload: Dict[str, Any]) -> str:
    endpoint = payload.get("completion_endpoint") or payload.get("vllm_completion_endpoint")
    if endpoint:
        return str(endpoint)
    endpoint = payload.get("endpoint") or payload.get("vllm_endpoint")
    if not endpoint:
        return "${VLLM_ENDPOINT:-http://127.0.0.1:30080/v1/completions}"
    endpoint = str(endpoint)
    if endpoint.endswith("/v1/completions"):
        return endpoint
    if endpoint.endswith("/health"):
        return f"{endpoint[:-len('/health')]}/v1/completions"
    return f"{endpoint.rstrip('/')}/v1/completions"


def _status_from_payload(payload: Dict[str, Any]) -> Tuple[str, str]:
    endpoint_status = str(payload.get("endpoint_status") or "").lower()
    metrics_status = str(payload.get("metrics_status") or payload.get("target_health") or "").lower()
    if endpoint_status in {"ok", "healthy", "up", "200"}:
        endpoint_status = "ok"
    elif endpoint_status in {"failed", "unavailable", "down", "503", "000", "connection refused"}:
        endpoint_status = "failed"
    else:
        endpoint_status = "unknown"
    if metrics_status in {"ok", "healthy", "up"}:
        metrics_status = "ok"
    elif metrics_status in {"failed", "unavailable", "down", "targetdown"}:
        metrics_status = "failed"
    else:
        metrics_status = "unknown"
    return endpoint_status, metrics_status


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    if not _has_k8s_vllm_context(payload, text):
        return {
            "status": "skipped",
            "confidence": 0.0,
            "summary": "缺少 K8S vLLM 上下文，跳过该 skill",
            "evidence": [],
        }
    request_failures = _metric(payload, "request_failures", "vllm_request_failures_total")
    e2e_p95 = _metric(payload, "e2e_p95_seconds", "vllm_e2e_p95_seconds")
    ttft_p95 = _metric(payload, "ttft_p95_seconds", "vllm_ttft_p95_seconds")
    endpoint_status, metrics_status = _status_from_payload(payload)
    matched = any(token in text for token in (
        "vllm",
        "inference",
        "gpu",
        "targetdown",
        "latency",
        "p95",
        "ttft",
        "e2e",
        "connection refused",
        "pod delete",
        "process crash",
        "智算",
        "推理",
    ))
    matched = matched or endpoint_status == "failed" or metrics_status == "failed" or request_failures > 0 or e2e_p95 > 0
    evidence: List[str] = []
    if endpoint_status != "unknown":
        evidence.append(f"vLLM 推理端点状态: {endpoint_status}")
    if metrics_status != "unknown":
        evidence.append(f"Prometheus/vLLM 指标状态: {metrics_status}")
    if request_failures > 0:
        evidence.append(f"检测到推理请求失败 {request_failures:g} 次")
    if e2e_p95 > 0:
        evidence.append(f"E2E P95={e2e_p95:.3f}s")
    if ttft_p95 > 0:
        evidence.append(f"TTFT P95={ttft_p95:.3f}s")
    if any(token in text for token in ("targetdown", "scrape failed")):
        evidence.append("Prometheus 抓取目标异常")
    for item in _evidence(payload):
        if not isinstance(item, dict):
            continue
        source = item.get("source") or "event"
        signal = item.get("signal") or item.get("metric") or "evidence"
        value = item.get("value")
        if value not in (None, ""):
            evidence.append(f"{source}:{signal}={value}")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.82 if matched else 0.0,
        "summary": "检测到 K8S vLLM 智算观测异常" if matched else "未检测到 K8S vLLM 智算观测异常",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    explicit_fault_type = _fault_type(payload)
    endpoint_status, metrics_status = _status_from_payload(payload)
    deployment_ready = _metric(payload, "deployment_ready_replicas", "ready_replicas", default=-1)
    deployment_desired = _metric(payload, "deployment_desired_replicas", "desired_replicas", default=-1)
    request_failures = _metric(payload, "request_failures", "vllm_request_failures_total")
    loadgen_errors = _metric(payload, "loadgen_errors", default=0.0)
    loadgen_requests = _metric(payload, "loadgen_requests", default=0.0)
    recovery_success = bool(payload.get("recovery_success"))
    e2e_p95 = _metric(payload, "e2e_p95_seconds", "vllm_e2e_p95_seconds")
    ttft_p95 = _metric(payload, "ttft_p95_seconds", "vllm_ttft_p95_seconds")
    waiting_requests = _metric(payload, "waiting_requests", "vllm:num_requests_waiting")
    running_requests = _metric(payload, "running_requests", "vllm:num_requests_running")
    gpu_count = int(_metric(payload, "gpu_count", default=0))
    payload_evidence = _evidence(payload)
    metrics_count = int(_metric(payload, "metrics_count", default=0))
    if metrics_count <= 0:
        metrics_count = len([item for item in payload_evidence if isinstance(item, dict)])
    process_count = 1 if endpoint_status == "ok" and deployment_ready != 0 else 0
    detection_evidence = list((context.get("detect") or {}).get("evidence") or [])
    observation_present = bool(detection_evidence or payload_evidence or payload.get("observation") or payload.get("live_observation"))
    fault_execution_dry_run = payload.get("fault_execution_dry_run")
    if fault_execution_dry_run is None:
        fault_execution_dry_run = context.get("dry_run", True)

    confidence, explanation = calibrate_llm_fault_confidence(
        base_confidence=0.88,
        scenario_present=False,
        observation_present=observation_present,
        endpoint_status="ok" if endpoint_status == "ok" else ("failed" if endpoint_status == "failed" else "unknown"),
        metrics_status="ok" if metrics_status == "ok" else ("failed" if metrics_status == "failed" else "unknown"),
        process_count=process_count,
        gpu_count=gpu_count,
        metrics_count=metrics_count,
        waiting_requests=waiting_requests,
        running_requests=running_requests,
        dry_run=bool(fault_execution_dry_run),
    )
    evidence = detection_evidence
    if deployment_desired >= 0 and deployment_ready >= 0:
        evidence.append(f"Deployment Ready/Desired={deployment_ready:g}/{deployment_desired:g}")
    if request_failures > 0 or loadgen_errors > 0:
        evidence.append("业务请求失败窗口可从探测或请求计数确认")

    deployment_unavailable = deployment_desired > 0 and deployment_ready >= 0 and deployment_ready < deployment_desired
    raw_fault_type = str(payload.get("raw_fault_type") or "").lower()
    event_type = str(payload.get("event_type") or "").lower()
    fault_experiment_signal = explicit_fault_type == "fault_experiment_signal" or raw_fault_type == "fault_experiment_signal"
    service_impact_observed = (
        endpoint_status == "failed"
        or metrics_status == "failed"
        or deployment_unavailable
        or request_failures > 0
        or loadgen_errors > 0
        or waiting_requests > 0
        or e2e_p95 >= _metric(payload, "e2e_p95_threshold_seconds", default=2.0)
        or ttft_p95 >= _metric(payload, "ttft_p95_threshold_seconds", default=1.5)
    )
    explicit_endpoint_fault = any(token in text for token in (
        "endpoint_timeout", "http_503", "grpc_failure", "connection refused", "no endpoints",
        "service selector", "service endpoint", "端点", "服务发现",
    ))
    explicit_resource_fault = any(token in text for token in (
        "resource_pressure", "cpu_stress", "memory_pressure", "disk_pressure",
        "fd_exhaustion", "os", "cpu pressure", "资源压力", "cpu 压力",
        "node_resource_pressure", "rootfs", "node_rootfs", "filesystem",
        "node_cpu", "node_memory", "node_disk", "node_network",
    )) or bool(re.search(r"\b(os|cpu|memory|disk|network|io|i/o)\s+pressure\b", text))
    has_node_resource_metric = _has_node_resource_metric(payload)
    resource_event = raw_fault_type == "node_resource_pressure" or event_type == "node_resource_pressure"
    resource_causality_allowed = (
        has_node_resource_metric
        and (
            resource_event
            or not fault_experiment_signal
            or service_impact_observed
        )
    )
    explicit_resource_fault = (
        (explicit_resource_fault and has_node_resource_metric)
        or has_node_resource_metric
    ) and resource_causality_allowed
    service_miswired = endpoint_status == "failed" and deployment_desired > 0 and deployment_ready >= deployment_desired
    service_miswired = service_miswired or (
        endpoint_status == "failed"
        and any(token in text for token in ("endpoint", "endpoints", "selector", "service", "svc", "错配", "为空", "<none>"))
    )
    if explicit_resource_fault:
        resource_name, resource_fault_type = _resource_pressure_kind(payload, text)
        if recovery_success:
            root_cause = f"vLLM 所在节点出现 {resource_name} 资源压力，恢复动作已清理压力来源，业务服务保持可用"
        else:
            metric = _metrics(payload).get("metric") or payload.get("metric") or ""
            root_cause = f"vLLM 所在节点存在 {resource_name} 资源压力，可能导致预处理、模型加载、调度或请求排队变慢"
            if metric:
                root_cause = f"vLLM 所在节点存在 {resource_name} 资源压力（观测指标 {metric}），可能导致预处理、模型加载、调度或请求排队变慢"
        fault_type = resource_fault_type
    elif service_miswired:
        if recovery_success:
            root_cause = "vLLM Service 端点在故障注入期间被置空或不可达，恢复动作已将服务发现链路还原"
        else:
            root_cause = "vLLM Service 没有可用 Endpoint，可能是 selector 错配或服务发现配置错误"
        fault_type = "LLM::k8s_vllm_service_endpoints_empty"
    elif endpoint_status == "failed" or deployment_unavailable:
        root_cause = "K8S vLLM Pod 不可用或模型服务尚未完成重建"
        fault_type = "LLM::k8s_vllm_unavailable"
    elif request_failures > 0 and endpoint_status == "ok":
        root_cause = "vLLM 服务可用但业务请求失败，优先检查模型名、请求参数或客户端配置"
        fault_type = "LLM::k8s_vllm_request_failures"
    elif e2e_p95 >= _metric(payload, "e2e_p95_threshold_seconds", default=2.0):
        root_cause = "vLLM 端到端推理延迟超过阈值"
        fault_type = "LLM::k8s_vllm_high_latency"
    elif metrics_status == "failed":
        root_cause = "Prometheus 无法抓取 vLLM 指标目标"
        fault_type = "LLM::k8s_vllm_metrics_target_down"
    elif fault_experiment_signal and not service_impact_observed:
        root_cause = "故障实验已执行，但当前窗口未观测到端点失败、请求排队、P95 时延升高、请求失败或 Pod 不可用；现有证据不足以确认根因"
        fault_type = "LLM::k8s_vllm_experiment_effect_unconfirmed"
        confidence = min(confidence, 0.62)
    else:
        root_cause = "vLLM 当前可用，但需要结合请求量和 P95 趋势确认影响"
        fault_type = "LLM::k8s_vllm_observed"

    return {
        "status": "ok",
        "confidence": confidence,
        "summary": f"{root_cause}。建议先确认 Deployment/Service/Prometheus，再审批恢复动作。",
        "evidence": evidence,
        "fault_type": fault_type,
        "root_cause": root_cause,
        "root_cause_component": payload.get("deployment") or payload.get("service") or "vllm-server",
        "confidence_explanation": explanation,
        "observed": {
            "endpoint_status": endpoint_status,
            "metrics_status": metrics_status,
            "ready_replicas": deployment_ready,
            "desired_replicas": deployment_desired,
            "request_failures": request_failures,
            "loadgen_errors": loadgen_errors,
            "loadgen_requests": loadgen_requests,
            "recovery_success": recovery_success,
            "e2e_p95_seconds": e2e_p95,
            "ttft_p95_seconds": ttft_p95,
            "waiting_requests": waiting_requests,
            "running_requests": running_requests,
        },
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    namespace = payload.get("namespace") or "llm-k8s"
    deployment = payload.get("deployment") or "vllm-server"
    service = payload.get("service") or "vllm-server"
    prometheus_url = payload.get("prometheus_url") or "${PROMETHEUS_URL:-http://127.0.0.1:30090}"
    endpoint = payload.get("endpoint") or "${VLLM_ENDPOINT:-http://127.0.0.1:30080/health}"
    completion_endpoint = _completion_endpoint(payload)
    model = payload.get("model") or "${VLLM_MODEL:-vllm}"
    replicas = int(_metric(payload, "desired_replicas", "deployment_desired_replicas", default=1))
    suggestions = [
        {
            "step": "检查 vLLM Deployment、Pod 和 Service 端点",
            "command": f"kubectl -n {namespace} get deploy/{deployment},pods,svc/{service},endpoints/{service} -o wide",
            "risk": "low",
        },
        {
            "step": "检查 Prometheus 是否正在抓取 vLLM 指标",
            "command": f"curl -fsS '{prometheus_url}/api/v1/targets'",
            "risk": "low",
        },
        {
            "step": "查询 vLLM 请求成功数和 P95 时延",
            "command": f"curl -fsSG '{prometheus_url}/api/v1/query' --data-urlencode 'query=vllm:request_success_total' && curl -fsSG '{prometheus_url}/api/v1/query' --data-urlencode 'query=histogram_quantile(0.95, sum(rate(vllm:e2e_request_latency_seconds_bucket[2m])) by (le, model_name))'",
            "risk": "low",
        },
        {
            "step": f"审批后恢复 vLLM Deployment {deployment} 副本数",
            "command": f"kubectl scale deployment/{deployment} -n {namespace} --replicas={replicas}",
            "rollback_command": f"kubectl scale deployment/{deployment} -n {namespace} --replicas=0",
            "risk": "medium",
        },
        {
            "step": f"审批后滚动重启 vLLM Deployment {deployment}",
            "command": f"kubectl rollout restart deployment/{deployment} -n {namespace}",
            "rollback_command": f"kubectl rollout undo deployment/{deployment} -n {namespace}",
            "risk": "medium",
        },
        {
            "step": "恢复后执行一次中文推理请求验证业务可用",
            "command": f"curl -fsS -H 'Content-Type: application/json' -d '{{\"model\":\"{model}\",\"prompt\":\"用一句中文说明 Kubernetes 自愈机制。\",\"max_tokens\":16,\"temperature\":0}}' '{completion_endpoint}'",
            "risk": "low",
        },
    ]
    return {
        "status": "ok",
        "confidence": 0.84,
        "summary": "生成 K8S vLLM 观测、恢复和业务验证步骤；变更类动作需要审批。",
        "evidence": ["包含 Prometheus 指标确认、Deployment 恢复、rollout 回滚和推理请求验证"],
        "suggestions": suggestions,
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    namespace = payload.get("namespace") or "llm-k8s"
    deployment = payload.get("deployment") or "vllm-server"
    service = payload.get("service") or "vllm-server"
    prometheus_url = payload.get("prometheus_url") or "${PROMETHEUS_URL:-http://127.0.0.1:30090}"
    endpoint = payload.get("endpoint") or "${VLLM_ENDPOINT:-http://127.0.0.1:30080/health}"
    return {
        "status": "ok",
        "confidence": 0.78,
        "summary": "验证 vLLM Pod Ready、健康接口、Prometheus target 和 P95 指标是否恢复。",
        "evidence": ["验证计划已生成"],
        "checks": [
            f"kubectl -n {namespace} rollout status deployment/{deployment}",
            f"kubectl -n {namespace} get endpoints/{service} -o wide",
            f"curl -fsS {endpoint}",
            f"curl -fsSG '{prometheus_url}/api/v1/query' --data-urlencode 'query=vllm:num_requests_running'",
            f"curl -fsSG '{prometheus_url}/api/v1/query' --data-urlencode 'query=histogram_quantile(0.95, sum(rate(vllm:e2e_request_latency_seconds_bucket[2m])) by (le, model_name))'",
        ],
    }
