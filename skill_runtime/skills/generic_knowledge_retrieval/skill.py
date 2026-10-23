from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List


ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = ROOT / "data"


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    parts = [
        payload.get("fault_type"),
        payload.get("message"),
        payload.get("root_cause"),
        payload.get("namespace"),
        payload.get("service"),
        payload.get("deployment"),
        payload.get("pod"),
        diagnosis.get("fault_type"),
        diagnosis.get("root_cause"),
    ]
    return " ".join(str(item or "") for item in parts).lower()


def _tokens(text: str) -> set[str]:
    tokens = set()
    for raw in text.replace("/", " ").replace("_", " ").replace("-", " ").split():
        item = raw.strip(" ,.;:()[]{}'\"").lower()
        if len(item) >= 3:
            tokens.add(item)
    return tokens


def _load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return default


def _as_list(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("rules", "faults", "feedback", "runs"):
            if isinstance(value.get(key), list):
                return [item for item in value[key] if isinstance(item, dict)]
    return []


def _payload_items(payload: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    value = payload.get(key)
    if isinstance(value, dict):
        return _as_list(value)
    if isinstance(value, list):
        return _as_list(value)
    knowledge = payload.get("knowledge") if isinstance(payload.get("knowledge"), dict) else {}
    return _as_list(knowledge.get(key))


def _candidate_text(item: Dict[str, Any]) -> str:
    return " ".join(str(item.get(key) or "") for key in (
        "condition", "condition_zh", "conclusion", "conclusion_zh", "description",
        "description_zh", "root_cause", "root_cause_zh", "fault_type", "text", "text_zh",
        "expert_diagnosis", "query", "message",
    )).lower()


def _score(query_tokens: set[str], item: Dict[str, Any]) -> float:
    if not query_tokens:
        return 0.0
    item_tokens = _tokens(_candidate_text(item))
    overlap = len(query_tokens.intersection(item_tokens))
    confidence = 0.0
    try:
        confidence = float(item.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return overlap + min(max(confidence, 0.0), 1.0)


def _top_matches(query: str, items: Iterable[Dict[str, Any]], limit: int = 5) -> List[Dict[str, Any]]:
    query_tokens = _tokens(query)
    ranked = []
    for item in items:
        score = _score(query_tokens, item)
        if score > 0:
            copy = dict(item)
            copy["match_score"] = round(score, 3)
            ranked.append(copy)
    return sorted(ranked, key=lambda item: item.get("match_score", 0), reverse=True)[:limit]


def _history_runs(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    runs = _payload_items(payload, "history") or _payload_items(payload, "runs")
    if runs:
        return runs
    compact = _load_json(DATA_DIR / "skill_workflow_runs.json", [])
    if isinstance(compact, list):
        return [item for item in compact if isinstance(item, dict)]
    return []


def _feedback(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = _payload_items(payload, "feedback")
    if items:
        return items
    return _as_list(_load_json(DATA_DIR / "expert_feedback" / "feedback_entries.json", []))


def diagnose(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    query = _text(payload)
    rules = _payload_items(payload, "rules")
    faults = _payload_items(payload, "faults")
    feedback = _feedback(payload)
    runs = _history_runs(payload)
    matched_rules = _top_matches(query, rules, limit=5)
    matched_faults = _top_matches(query, faults, limit=5)
    matched_feedback = _top_matches(query, feedback, limit=3)
    matched_runs = _top_matches(query, runs, limit=3)
    evidence = list((context.get("detect") or {}).get("evidence") or [])
    if matched_rules:
        evidence.append(f"命中相似诊断规则 {len(matched_rules)} 条")
    if matched_faults:
        evidence.append(f"命中相似历史故障 {len(matched_faults)} 条")
    if matched_runs:
        evidence.append(f"命中历史 Skill 工作流 {len(matched_runs)} 条")
    sample_count = len(matched_faults) + len(matched_runs)
    success_count = 0
    for run in matched_runs:
        result = run.get("result") if isinstance(run.get("result"), dict) else run
        capability = result.get("capability") if isinstance(result.get("capability"), dict) else {}
        if run.get("status") == "completed" and capability.get("heal_ready") is not False:
            success_count += 1
    success_rate = success_count / sample_count if sample_count else 0.0
    return {
        "status": "ok",
        "confidence": 0.78 if evidence else 0.45,
        "summary": "已完成知识库与历史工作流检索，可为根因诊断和置信度校准提供上下文。",
        "evidence": evidence,
        "fault_type": payload.get("fault_type") or (matched_rules[0].get("fault_type") if matched_rules else "KnowledgeContext"),
        "root_cause": (matched_faults[0].get("root_cause_zh") or matched_faults[0].get("root_cause")) if matched_faults else "未命中高相关历史故障，需要继续采集证据",
        "root_cause_component": payload.get("deployment") or payload.get("service") or payload.get("pod") or "",
        "knowledge_context": {
            "rules": matched_rules,
            "faults": matched_faults,
            "feedback": matched_feedback,
            "runs": matched_runs,
        },
        "history_stats": {
            "sample_count": sample_count,
            "success_rate": round(success_rate, 3),
            "sufficient_samples": sample_count >= 5,
        },
    }
