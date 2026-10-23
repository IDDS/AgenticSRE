from __future__ import annotations

from typing import Any, Dict, List


DEDICATED_FAULT_TYPES = {
    "ServiceUnavailable",
    "PodCrashLoop",
    "ImagePullError",
    "NodeNotReady",
    "DNSResolution",
    "NetworkPolicyDeny",
}


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _legacy(context: Dict[str, Any]) -> Any:
    return (context.get("adapters") or {}).get("legacy")


def _text(payload: Dict[str, Any]) -> str:
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    parts = [
        payload.get("fault_type"),
        payload.get("message"),
        payload.get("root_cause"),
        payload.get("root_cause_component"),
        diagnosis.get("fault_type"),
        diagnosis.get("root_cause"),
        diagnosis.get("remediation_suggestion"),
    ]
    return " ".join(str(item or "") for item in parts)


def _fault_type(context: Dict[str, Any]) -> str:
    payload = _payload(context)
    explicit = str(payload.get("fault_type") or (payload.get("diagnosis") or {}).get("fault_type") or "").strip()
    if explicit:
        return explicit
    legacy = _legacy(context)
    if legacy:
        return str(legacy.normalize_heal_fault_type(_text(payload)))
    return "Unknown"


def _legacy_heal(context: Dict[str, Any]) -> Dict[str, Any]:
    cached = context.get("legacy_heal")
    if isinstance(cached, dict):
        return cached
    legacy = _legacy(context)
    if not legacy:
        return {"status": "unavailable", "suggestions": [], "fault_type": _fault_type(context)}
    result = legacy.build_heal_suggestions(_payload(context))
    context["legacy_heal"] = result
    return result


def _evidence_from_payload(payload: Dict[str, Any]) -> List[str]:
    evidence: List[str] = []
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    summary = diagnosis.get("evidence_summary")
    if isinstance(summary, dict):
        evidence.extend(str(value) for value in summary.values() if value)
    elif isinstance(summary, list):
        evidence.extend(str(value) for value in summary if value)
    for item in payload.get("evidence") or []:
        if item:
            evidence.append(str(item))
    return evidence


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    fault_type = _fault_type(context)
    matched = bool(fault_type and fault_type not in {"Unknown", *DEDICATED_FAULT_TYPES})
    evidence = _evidence_from_payload(payload)
    if matched:
        evidence.append(f"旧自愈知识库识别故障类型 {fault_type}")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.76 if matched else 0.0,
        "summary": f"命中 K8s 旧自愈知识库桥接 skill: {fault_type}" if matched else "未命中旧自愈知识库桥接 skill",
        "evidence": evidence,
        "fault_type": fault_type,
    }


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    fault_type = _fault_type(context)
    confidence = max(float(diagnosis.get("confidence") or 0.0), 0.8)
    evidence = list((context.get("detect") or {}).get("evidence") or []) or _evidence_from_payload(payload)
    return {
        "status": "ok",
        "confidence": min(confidence, 0.92),
        "summary": f"{fault_type} 暂由旧自愈知识库桥接 skill 诊断，后续应拆分为专用 skill。",
        "evidence": evidence or [f"fault_type={fault_type}"],
        "fault_type": fault_type,
        "root_cause": diagnosis.get("root_cause") or payload.get("root_cause") or f"{fault_type} matched by legacy recipe bridge",
        "root_cause_component": diagnosis.get("root_cause_component") or payload.get("root_cause_component") or payload.get("deployment") or payload.get("pod") or payload.get("node") or "",
        "migration_note": "bridge_skill_requires_dedicated_skill",
    }


def heal(context: Dict[str, Any]) -> Dict[str, Any]:
    result = _legacy_heal(context)
    suggestions = []
    for item in result.get("suggestions") or []:
        if not isinstance(item, dict) or not item.get("command"):
            continue
        suggestions.append({
            **item,
            "source": "legacy_recipe_bridge",
            "skill_bridge": True,
        })
    if not suggestions:
        return {
            "status": "blocked",
            "confidence": 0.0,
            "summary": "旧自愈知识库未生成可执行动作，需补充专用 skill 或手工 runbook。",
            "evidence": ["legacy recipe bridge returned no suggestions"],
            "suggestions": [],
        }
    return {
        "status": "ok",
        "confidence": 0.78,
        "summary": "生成旧自愈知识库桥接动作；真实执行仍必须审批，后续应迁移为专用 skill。",
        "evidence": [f"桥接动作 {len(suggestions)} 个"],
        "suggestions": suggestions,
    }


def verify(context: Dict[str, Any]) -> Dict[str, Any]:
    result = _legacy_heal(context)
    plan = result.get("verification_plan") if isinstance(result.get("verification_plan"), dict) else {}
    checks = [str(item) for item in plan.get("checks") or [] if str(item).strip()]
    if not checks:
        payload = _payload(context)
        namespace = payload.get("namespace") or "default"
        deployment = payload.get("deployment") or (payload.get("diagnosis") or {}).get("root_cause_component") or ""
        checks = [
            f"kubectl get pods -n {namespace}",
            f"kubectl get events -n {namespace} --sort-by=.lastTimestamp | tail -40",
        ]
        if deployment:
            checks.append(f"kubectl rollout status deployment/{deployment} -n {namespace}")
    return {
        "status": "ok",
        "confidence": 0.72,
        "summary": "生成桥接验证计划；专用 skill 发布前需补充更精确的验证条件。",
        "evidence": ["bridge verification plan"],
        "checks": checks,
    }
