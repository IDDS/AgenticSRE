from __future__ import annotations

from typing import Any, Dict, List


RISK_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _actions(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    for key in ("actions", "suggestions", "edited_actions"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    value = diagnosis.get("actions")
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _max_risk(actions: List[Dict[str, Any]]) -> str:
    max_risk = "low"
    for action in actions:
        risk = str(action.get("risk") or "low").lower()
        if RISK_RANK.get(risk, 2) > RISK_RANK.get(max_risk, 1):
            max_risk = risk
    return max_risk


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    actions = _actions(payload)
    max_risk = _max_risk(actions)
    rollback_count = sum(1 for action in actions if action.get("rollback_command"))
    blockers: List[str] = []
    if max_risk in {"high", "critical"}:
        blockers.append("高风险动作需要人工确认")
    if actions and rollback_count == 0:
        blockers.append("没有记录回滚动作")
    if not bool(payload.get("dry_run", True)):
        blockers.append("真实执行必须走审批")
    return {
        "status": "ok",
        "confidence": 0.74,
        "summary": "风险门禁已评估恢复动作、审批和回滚条件。",
        "evidence": [f"最高风险 {max_risk}", f"回滚动作 {rollback_count}"],
        "fault_type": payload.get("fault_type") or "RiskGate",
        "root_cause": "; ".join(blockers) if blockers else "Dry-run 或低风险动作已通过风险门禁",
        "root_cause_component": "policy",
        "max_risk": max_risk,
        "blockers": blockers,
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    output = (context.get("diagnose") or {}).get("output", {})
    blockers = output.get("blockers") or []
    return {
        "status": "ok",
        "confidence": 0.7,
        "summary": "输出风险门禁建议，不直接恢复业务。",
        "evidence": blockers or ["风险门禁通过"],
        "suggestions": [
            {"step": "执行前确认风险门禁", "command": "check approval, dry-run, rollback, and blast radius before execution", "risk": "low"}
        ],
    }
