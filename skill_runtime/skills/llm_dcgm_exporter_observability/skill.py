from __future__ import annotations

from typing import Any, Dict, List


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _metrics(payload: Dict[str, Any]) -> Dict[str, Any]:
    value = payload.get("metrics")
    return value if isinstance(value, dict) else {}


def _text(payload: Dict[str, Any]) -> str:
    keys = ("fault_type", "raw_fault_type", "message", "metric", "service", "pod", "node", "target")
    parts = [str(payload.get(key) or "") for key in keys]
    for item in payload.get("evidence") or []:
        if isinstance(item, dict):
            parts.extend(str(item.get(key) or "") for key in ("source", "signal", "value", "message"))
        elif item:
            parts.append(str(item))
    return " ".join(parts).lower()


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _tool(context: Dict[str, Any], name: str, **kwargs: Any) -> Dict[str, Any]:
    tools = context.get("tools")
    if tools and hasattr(tools, "execute"):
        return tools.execute(name, **kwargs)
    return {"executed": False, "success": False, "tool": name, "reason": "未注入工具 runner"}


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    metrics = _metrics(payload)
    text = _text(payload)
    target_up = _num(metrics.get("up") or payload.get("target_up"), default=-1)
    if target_up < 0 and any(token in text for token in ("target down", "target_health down", "target_health down", "target_health")):
        target_up = 0
    has_gpu_series_signal = "DCGM_FI_DEV_GPU_UTIL" in metrics or "gpu_metric_series" in payload
    gpu_series = _num(metrics.get("DCGM_FI_DEV_GPU_UTIL") or payload.get("gpu_metric_series"))
    explicit_missing = any(token in text for token in ("gpu_metric_absent", "dcgm_fi_dev_gpu_util absent", "gpu metrics missing"))
    if gpu_series <= 0 and explicit_missing:
        gpu_series = 0
    matched = any(token in text for token in (
        "dcgm", "gpu metrics", "gpu_metric_absent", "dcgm_exporter", "dcgm_fi",
        "target down", "scrape", "gpu 指标", "显卡指标",
    )) or target_up == 0 or (has_gpu_series_signal and gpu_series == 0 and "gpu" in text) or explicit_missing
    evidence: List[str] = []
    if target_up == 0:
        evidence.append("Prometheus target up=0")
    if (has_gpu_series_signal or explicit_missing) and gpu_series == 0 and "gpu" in text:
        evidence.append("GPU 指标序列为空")
    if "dcgm" in text:
        evidence.append("输入包含 DCGM/exporter 信号")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.86 if matched else 0.0,
        "summary": "检测到 DCGM/GPU 指标采集异常信号" if matched else "未检测到 DCGM 指标异常",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    ns = payload.get("namespace") or "llm-k8s"
    node = payload.get("node") or payload.get("host") or ""
    prom = payload.get("prometheus_url") or "http://127.0.0.1:19090"
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    tool_results = [
        _tool(context, "prometheus", url=prom, query='up{job=~".*dcgm.*|.*gpu.*"}'),
        _tool(context, "prometheus", url=prom, query="DCGM_FI_DEV_GPU_UTIL"),
        _tool(context, "kubectl", namespace=ns, args=["get", "pods,svc,endpoints", "-o", "wide"]),
    ]
    executed = [item for item in tool_results if item.get("executed")]
    if executed:
        evidence.append("已调用受控工具检查 Prometheus target 和 K8S 对象")
    target_status = str(payload.get("target_status") or "").lower()
    if "down" in target_status or any("target up=0" in item.lower() for item in evidence):
        fault_type = "LLM::dcgm_exporter_target_down"
        root_cause = "DCGM exporter 或 Prometheus 抓取链路不可用，GPU 指标无法进入智算观测面"
    elif payload.get("missing_node") or node:
        fault_type = "LLM::gpu_metrics_missing_on_node"
        root_cause = f"节点 {node or 'unknown'} 的 GPU 指标缺失，可能是 exporter 未调度、端口不可达或驱动指标不可读"
    else:
        fault_type = "LLM::gpu_observability_incomplete"
        root_cause = "GPU 可观测链路不完整，需要同时核对 exporter、ServiceMonitor 和 Prometheus 查询"
    confidence = min(0.92, 0.68 + 0.05 * len(evidence) + (0.08 if executed else 0.0))
    return {
        "status": "ok",
        "confidence": round(confidence, 3),
        "summary": root_cause,
        "evidence": evidence,
        "fault_type": fault_type,
        "root_cause": root_cause,
        "root_cause_component": payload.get("component") or payload.get("service") or "dcgm-exporter",
        "tool_results": tool_results,
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    ns = payload.get("namespace") or "llm-k8s"
    daemonset = payload.get("daemonset") or "dcgm-exporter"
    return {
        "status": "ok",
        "confidence": 0.78,
        "summary": "生成 DCGM 指标链路恢复建议，变更动作默认需要审批。",
        "evidence": ["先恢复采集链路，再验证前端曲线"],
        "suggestions": [
            {"step": "复核 exporter 调度", "command": f"kubectl -n {ns} get pods -l app=dcgm-exporter -o wide", "risk": "low"},
            {"step": "复核 endpoints", "command": f"kubectl -n {ns} get svc,endpoints | grep -i dcgm", "risk": "low"},
            {"step": "审批后重启 exporter", "command": f"kubectl -n {ns} rollout restart daemonset/{daemonset}", "rollback_command": f"kubectl -n {ns} rollout status daemonset/{daemonset}", "risk": "medium"},
        ],
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    prom = payload.get("prometheus_url") or "http://127.0.0.1:19090"
    return {
        "status": "ok",
        "confidence": 0.76,
        "summary": "验证 DCGM target、GPU 利用率和显存指标是否恢复。",
        "evidence": ["DCGM 采集恢复验证计划"],
        "checks": [
            f"curl -fsSG '{prom}/api/v1/query' --data-urlencode 'query=up{{job=~\".*dcgm.*|.*gpu.*\"}}'",
            f"curl -fsSG '{prom}/api/v1/query' --data-urlencode 'query=DCGM_FI_DEV_GPU_UTIL'",
            f"curl -fsSG '{prom}/api/v1/query' --data-urlencode 'query=DCGM_FI_DEV_FB_USED'",
        ],
    }
