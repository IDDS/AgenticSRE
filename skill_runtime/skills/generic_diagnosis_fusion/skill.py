from __future__ import annotations

from typing import Any, Dict, List


HELPER_TAGS = {"gatekeeper", "verification", "risk", "tool"}
HELPER_IDS = {"generic.risk_gatekeeper", "generic.recovery_verifier"}


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _diagnoses(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = payload.get("diagnoses") or payload.get("candidate_diagnoses") or []
    if isinstance(raw, dict):
        raw = raw.get("items") or raw.get("diagnoses") or []
    items: List[Dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                items.append(item)
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    if diagnosis:
        items.append(diagnosis)
    runs = payload.get("runs") or payload.get("skill_runs") or []
    if isinstance(runs, list):
        for run in runs:
            if not isinstance(run, dict):
                continue
            stage = ((run.get("stages") or {}).get("diagnose") or {})
            output = stage.get("output") if isinstance(stage.get("output"), dict) else {}
            if output:
                skill = run.get("skill") or {}
                items.append({
                    **output,
                    "confidence": stage.get("confidence"),
                    "skill_id": skill.get("id"),
                    "skill_tags": skill.get("tags") or [],
                    "summary": stage.get("summary"),
                    "evidence": stage.get("evidence") or [],
                })
    return items


def _is_helper(item: Dict[str, Any]) -> bool:
    skill_id = str(item.get("skill_id") or item.get("source") or "")
    tags = {str(tag).lower() for tag in item.get("skill_tags") or item.get("tags") or []}
    return skill_id in HELPER_IDS or bool(tags.intersection(HELPER_TAGS))


def _confidence(item: Dict[str, Any]) -> float:
    try:
        return float(item.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _specificity(item: Dict[str, Any]) -> int:
    score = 0
    if item.get("fault_type"):
        score += 1
    if item.get("root_cause"):
        score += 2
    if item.get("root_cause_component"):
        score += 1
    if item.get("evidence"):
        score += min(len(item.get("evidence") or []), 3)
    return score


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    items = _diagnoses(payload)
    non_helper = [item for item in items if not _is_helper(item)]
    candidates = non_helper or items
    ranked = sorted(candidates, key=lambda item: (_specificity(item), _confidence(item)), reverse=True)
    primary = ranked[0] if ranked else {}
    conflict_types = sorted({str(item.get("fault_type")) for item in candidates if item.get("fault_type")})
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    if len(conflict_types) > 1:
        evidence.append(f"存在多个故障类型候选: {', '.join(conflict_types[:5])}")
    if non_helper:
        evidence.append("已排除风险门禁、恢复验证等辅助 skill 对主根因的干扰")
    confidence = min(max(_confidence(primary), 0.55) + (0.06 if len(candidates) >= 2 else 0.0), 0.94)
    return {
        "status": "ok" if primary else "skipped",
        "confidence": confidence if primary else 0.0,
        "summary": "已融合候选诊断并选择主根因。" if primary else "没有足够候选诊断完成融合。",
        "evidence": evidence + list(primary.get("evidence") or [])[:5],
        "fault_type": primary.get("fault_type") or payload.get("fault_type") or "",
        "root_cause": primary.get("root_cause") or payload.get("root_cause") or "候选诊断不足，需要继续采集证据",
        "root_cause_component": primary.get("root_cause_component") or payload.get("root_cause_component") or "",
        "primary_skill_id": primary.get("skill_id") or "",
        "candidate_count": len(items),
        "non_helper_count": len(non_helper),
        "conflict_fault_types": conflict_types,
    }
