from __future__ import annotations

from typing import Any, Dict, List


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    keys = ("fault_type", "raw_fault_type", "message", "ingress", "gateway", "route", "host", "path", "service")
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
    matched = any(token in text for token in (
        "ingress", "gateway", "httproute", "loadbalancer", "upstream",
        "route mismatch", "入口", "网关", "路由",
    )) or status in {404, 502, 503, 504} and any(token in text for token in ("host", "path", "service", "gateway", "ingress"))
    evidence: List[str] = []
    if status:
        evidence.append(f"入口 HTTP 状态码 {status}")
    if payload.get("host"):
        evidence.append(f"Host={payload.get('host')}")
    if payload.get("path"):
        evidence.append(f"Path={payload.get('path')}")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.86 if matched else 0.0,
        "summary": "检测到 Ingress/Gateway 入口流量异常" if matched else "未检测到入口流量异常",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    status = _status(payload)
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    if "loadbalancer" in text and any(token in text for token in ("pending", "no address", "无地址")):
        fault_type = "K8S::loadbalancer_address_missing"
        root_cause = "LoadBalancer 尚未分配入口地址，外部流量无法进入集群"
    elif status == 404 or "route mismatch" in text:
        fault_type = "K8S::ingress_route_mismatch"
        root_cause = "入口 Host/Path 路由规则未命中目标 Service"
    elif status in {502, 503, 504} or "upstream" in text:
        fault_type = "K8S::gateway_upstream_unavailable"
        root_cause = "网关上游 Service 或 endpoints 不可用，导致入口 5xx 或超时"
    else:
        fault_type = "K8S::ingress_gateway_traffic_failure"
        root_cause = "入口网关、路由或后端服务链路异常"
    confidence = min(0.92, 0.68 + 0.05 * len(evidence) + (0.08 if status else 0.0))
    return {
        "status": "ok",
        "confidence": round(confidence, 3),
        "summary": root_cause,
        "evidence": evidence,
        "fault_type": fault_type,
        "root_cause": root_cause,
        "root_cause_component": payload.get("ingress") or payload.get("gateway") or payload.get("service") or "ingress-gateway",
        "observed": {"http_status": status, "host": payload.get("host"), "path": payload.get("path")},
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    ns = payload.get("namespace") or "default"
    svc = payload.get("service") or "<service>"
    return {
        "status": "ok",
        "confidence": 0.78,
        "summary": "生成入口链路恢复建议；修改路由或网关配置需审批。",
        "evidence": ["入口链路只读核查优先"],
        "suggestions": [
            {"step": "查看入口资源", "command": f"kubectl -n {ns} get ingress,gateway,httproute -o wide", "risk": "low"},
            {"step": "查看后端 Service/Endpoint", "command": f"kubectl -n {ns} get svc,endpoints {svc} -o wide", "risk": "low"},
            {"step": "审批后修正 Host/Path/Backend", "command": "修正 Ingress/Gateway/HTTPRoute 的 host、path、backendRef 或 service port", "risk": "medium"},
        ],
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    ns = payload.get("namespace") or "default"
    host = payload.get("host") or "<host>"
    path = payload.get("path") or "/"
    return {
        "status": "ok",
        "confidence": 0.76,
        "summary": "验证入口地址、路由和后端 endpoints。",
        "evidence": ["入口链路验证计划"],
        "checks": [
            f"kubectl -n {ns} get ingress,gateway,httproute -o wide",
            f"kubectl -n {ns} get svc,endpoints -o wide",
            f"curl -fsS -H 'Host: {host}' http://<ingress-address>{path}",
        ],
    }
