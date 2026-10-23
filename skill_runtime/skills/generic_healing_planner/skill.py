from __future__ import annotations

from typing import Any, Dict, List


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _diagnosis(payload: Dict[str, Any]) -> Dict[str, Any]:
    return payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    namespace = payload.get("namespace") or "default"
    diagnosis = _diagnosis(payload)
    deployment = payload.get("deployment") or (diagnosis.get("root_cause_component") or "")
    service = payload.get("service") or ""
    allow_mutation = payload.get("allow_generic_restart") is True
    suggestions: List[Dict[str, Any]] = [
        {"step": "确认当前对象状态", "command": f"kubectl get pods,svc,endpoints -n {namespace} -o wide", "risk": "low"},
        {"step": "确认最近事件", "command": f"kubectl get events -n {namespace} --sort-by=.lastTimestamp | tail -40", "risk": "low"},
    ]
    if service:
        suggestions.append({"step": f"检查 Service {service} 端点", "command": f"kubectl describe svc {service} -n {namespace}", "risk": "low"})
    if deployment and allow_mutation:
        suggestions.append({
            "step": f"审批后滚动重启 {deployment}",
            "command": f"kubectl rollout restart deployment/{deployment} -n {namespace}",
            "rollback_command": f"kubectl rollout undo deployment/{deployment} -n {namespace}",
            "risk": "medium",
        })
    elif deployment:
        suggestions.append({
            "step": f"确认 Deployment {deployment} 变更前状态",
            "command": f"kubectl describe deployment/{deployment} -n {namespace}",
            "risk": "low",
        })
    return {
        "status": "ok",
        "confidence": 0.74 if allow_mutation else 0.68,
        "summary": "生成通用恢复计划；默认只做只读确认，写动作需要显式授权。",
        "evidence": ["包含只读确认", "写动作仅在 allow_generic_restart=true 时生成"],
        "suggestions": suggestions,
    }
