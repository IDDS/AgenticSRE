from __future__ import annotations

from typing import Any, Dict, List


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    keys = (
        "fault_type", "raw_fault_type", "alertname", "message", "job",
        "target", "metric", "service", "observed_evidence_summary", "agent_evidence_summary",
    )
    parts = [str(payload.get(key) or "") for key in keys]
    for item in payload.get("evidence") or []:
        if isinstance(item, dict):
            parts.extend(str(item.get(key) or "") for key in ("source", "signal", "value", "message", "summary"))
        elif item:
            parts.append(str(item))
    return " ".join(parts).lower()


def _tool(context: Dict[str, Any], name: str, **kwargs: Any) -> Dict[str, Any]:
    tools = context.get("tools")
    if tools and hasattr(tools, "execute"):
        return tools.execute(name, **kwargs)
    return {"executed": False, "success": False, "tool": name, "reason": "未注入工具 runner"}


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    explicit_scrape_context = any(token in text for token in (
        "servicemonitor", "podmonitor", "metrics endpoint", "metricsbindaddress",
        "metrics bind", "/metrics", "target label", "target labels", "selector",
        "127.0.0.1", "localhost", "bound to localhost", "绑定在本地",
    ))
    prometheus_context = any(token in text for token in (
        "prometheus", "servicemonitor", "podmonitor", "scrape", "metrics endpoint",
        "/metrics", "metricsbindaddress",
    ))
    matched = explicit_scrape_context and any(token in text for token in (
        "targetdown", "target down", "scrape_error", "scrape failed", "failed", "timeout", "connection refused",
        "127.0.0.1", "metricsbindaddress",
        "localhost",
    )) and prometheus_context
    evidence: List[str] = []
    if matched:
        evidence.append("输入包含 Prometheus target/scrape 异常信号")
    if "metricsbindaddress" in text or "127.0.0.1" in text or "localhost" in text:
        evidence.append("目标 metrics 可能绑定在本地地址")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.86 if matched else 0.0,
        "summary": "检测到 Prometheus 抓取目标异常" if matched else "未检测到 Prometheus 抓取目标异常",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    ns = payload.get("namespace") or "default"
    service = payload.get("service") or payload.get("target") or ""
    prom = payload.get("prometheus_url") or "http://127.0.0.1:9090"
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    tool_results = [
        _tool(context, "prometheus", url=prom, query=f'up{{job=~".*{service}.*"}}' if service else "up == 0"),
        _tool(context, "kubectl", namespace=ns, args=["get", "svc,endpoints,pods", "-o", "wide"]),
        _tool(context, "kubectl", namespace=ns, args=["get", "servicemonitor,podmonitor", "-o", "wide"]),
    ]
    if any(item.get("executed") for item in tool_results):
        evidence.append("已调用受控工具检查 target、endpoint 和 monitor 配置")
    text = _text(payload)
    if "metricsbindaddress" in text or "127.0.0.1" in text or "localhost" in text:
        fault_type = "K8S::prometheus_metrics_bind_localhost"
        root_cause = "目标组件 metricsBindAddress 绑定在 127.0.0.1，Prometheus 无法从其他节点抓取"
    elif "servicemonitor" in text or "selector" in text:
        fault_type = "K8S::prometheus_servicemonitor_selector_mismatch"
        root_cause = "ServiceMonitor/PodMonitor selector、namespace 或端口名与目标 Service 不匹配"
    elif "no endpoint" in text:
        fault_type = "K8S::prometheus_scrape_endpoint_empty"
        root_cause = "Prometheus 抓取目标没有可用 endpoints，可能是 Service selector 或 Pod Ready 状态异常"
    else:
        fault_type = "K8S::prometheus_scrape_target_unhealthy"
        root_cause = "Prometheus 抓取目标不可达，需要核查 discovery、端口、path 和网络策略"
    confidence = min(0.92, 0.68 + 0.05 * len(evidence) + (0.08 if any(item.get("executed") for item in tool_results) else 0.0))
    return {
        "status": "ok",
        "confidence": round(confidence, 3),
        "summary": root_cause,
        "evidence": evidence,
        "fault_type": fault_type,
        "root_cause": root_cause,
        "root_cause_component": service or payload.get("job") or "prometheus-target",
        "tool_results": tool_results,
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    ns = payload.get("namespace") or "default"
    target = payload.get("target") or payload.get("service") or "<target>"
    return {
        "status": "ok",
        "confidence": 0.78,
        "summary": "生成 Prometheus 抓取目标恢复建议，配置变更需审批。",
        "evidence": ["先修复 discovery/endpoint，再验证告警消除"],
        "suggestions": [
            {"step": "核查 Service/Endpoint", "command": f"kubectl -n {ns} get svc,endpoints {target} -o wide", "risk": "low"},
            {"step": "核查 ServiceMonitor", "command": f"kubectl -n {ns} get servicemonitor,podmonitor -o yaml", "risk": "low"},
            {"step": "审批后修正 selector/端口/path", "command": "修正 ServiceMonitor selector、namespaceSelector、endpoints.port/path 或组件 metricsBindAddress", "risk": "medium"},
        ],
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    prom = _payload(context).get("prometheus_url") or "http://127.0.0.1:9090"
    return {
        "status": "ok",
        "confidence": 0.76,
        "summary": "验证 Prometheus target up 和相关指标恢复。",
        "evidence": ["Prometheus 抓取恢复验证计划"],
        "checks": [
            f"curl -fsSG '{prom}/api/v1/targets'",
            f"curl -fsSG '{prom}/api/v1/query' --data-urlencode 'query=up == 0'",
            "确认 TargetDown 告警在一个评估窗口后消除",
        ],
    }
