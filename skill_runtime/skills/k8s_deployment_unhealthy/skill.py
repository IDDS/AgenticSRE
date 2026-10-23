from __future__ import annotations

from typing import Any, Dict, List


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    alert = payload.get("alert") if isinstance(payload.get("alert"), dict) else {}
    raw = alert.get("raw_data") if isinstance(alert.get("raw_data"), dict) else {}
    parts = [
        payload.get("fault_type"),
        payload.get("message"),
        payload.get("deployment"),
        payload.get("service"),
        diagnosis.get("fault_type"),
        diagnosis.get("root_cause"),
        raw.get("reason"),
        raw.get("message"),
    ]
    return " ".join(str(item or "") for item in parts).lower()


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    text = _text(_payload(context))
    matched = any(token in text for token in (
        "deploymentunhealthy",
        "readiness probe failed",
        "liveness probe failed",
        "replicas unavailable",
        "minimum availability",
        "progress deadline exceeded",
        "deployment unavailable",
    ))
    evidence: List[str] = []
    if "readiness probe failed" in text:
        evidence.append("Deployment/Pod 事件显示 readiness probe failed")
    if "liveness probe failed" in text:
        evidence.append("Deployment/Pod 事件显示 liveness probe failed")
    if "replicas unavailable" in text or "minimum availability" in text:
        evidence.append("Deployment 可用副本不足")
    if "progress deadline exceeded" in text:
        evidence.append("Deployment rollout 超过 progress deadline")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.84 if matched else 0.0,
        "summary": "检测到 Deployment 不健康或探针失败信号" if matched else "未检测到 Deployment 不健康信号",
        "evidence": evidence,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    deployment = payload.get("deployment") or diagnosis.get("root_cause_component") or "unknown"
    service = payload.get("service") or payload.get("object") or ""
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    if deployment != "unknown":
        evidence.append(f"目标 Deployment: {deployment}")
    if service:
        evidence.append(f"关联 Service: {service}")
    confidence = max(float(diagnosis.get("confidence") or 0.0), 0.86 if len(evidence) >= 2 else 0.66)
    return {
        "status": "ok",
        "confidence": min(confidence, 0.94),
        "summary": f"Deployment {deployment} 不健康，需检查 rollout、Pod 事件、探针和服务端点。",
        "evidence": evidence,
        "fault_type": "DeploymentUnhealthy",
        "root_cause": diagnosis.get("root_cause") or f"deployment {deployment} has unavailable replicas or failed probes",
        "root_cause_component": deployment,
        "service": service,
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    namespace = payload.get("namespace") or "default"
    deployment = payload.get("deployment") or (payload.get("diagnosis") or {}).get("root_cause_component") or "<deployment>"
    service = payload.get("service") or ""
    suggestions = [
        {
            "step": f"检查 Deployment {deployment} 发布状态",
            "command": f"kubectl rollout status deployment/{deployment} -n {namespace}",
            "risk": "low",
        },
        {
            "step": f"查看 Deployment {deployment} 详情和事件",
            "command": f"kubectl describe deployment/{deployment} -n {namespace}",
            "risk": "low",
        },
        {
            "step": "查看相关 Pod 状态",
            "command": f"kubectl get pods -n {namespace} -l app={deployment} -o wide",
            "risk": "low",
        },
    ]
    if service:
        suggestions.append({
            "step": f"检查 Service {service} 端点",
            "command": f"kubectl get endpoints {service} -n {namespace} -o wide",
            "risk": "low",
        })
    suggestions.extend([
        {
            "step": f"审批后滚动重启 Deployment {deployment}",
            "command": f"kubectl rollout restart deployment/{deployment} -n {namespace}",
            "rollback_command": f"kubectl rollout undo deployment/{deployment} -n {namespace}",
            "risk": "medium",
        },
        {
            "step": f"审批后回滚 Deployment {deployment} 到上一版本",
            "command": f"kubectl rollout undo deployment/{deployment} -n {namespace}",
            "rollback_command": "",
            "risk": "medium",
        },
    ])
    return {
        "status": "ok",
        "confidence": 0.82,
        "summary": "生成 Deployment 不健康恢复建议；重启和回滚均需审批。",
        "evidence": ["包含 rollout/事件/端点检查和可回滚恢复动作"],
        "suggestions": suggestions,
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    namespace = payload.get("namespace") or "default"
    deployment = payload.get("deployment") or (payload.get("diagnosis") or {}).get("root_cause_component") or "<deployment>"
    service = payload.get("service") or ""
    checks = [
        f"kubectl rollout status deployment/{deployment} -n {namespace}",
        f"kubectl get deployment/{deployment} -n {namespace} -o wide",
        f"kubectl get pods -n {namespace} -l app={deployment} -o wide",
    ]
    if service:
        checks.append(f"kubectl get endpoints {service} -n {namespace} -o wide")
    return {
        "status": "ok",
        "confidence": 0.78,
        "summary": "验证 Deployment rollout 完成、可用副本恢复、相关端点恢复。",
        "evidence": ["验证计划已生成"],
        "checks": checks,
    }
