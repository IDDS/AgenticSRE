from __future__ import annotations

from typing import Any, Dict, List


KEYWORDS = (
    "error", "failed", "failure", "timeout", "unavailable", "503", "crash",
    "oom", "latency", "degraded", "异常", "失败", "超时", "不可用", "崩溃", "告警",
)


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    return context.get("payload") or {}


def _text(payload: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key in ("fault_type", "message", "alert", "summary", "root_cause", "service", "deployment", "pod", "node"):
        parts.append(str(payload.get(key, "") or ""))
    diagnosis = payload.get("diagnosis") if isinstance(payload.get("diagnosis"), dict) else {}
    parts.extend(str(v or "") for v in diagnosis.values())
    return " ".join(parts).lower()


def detect(context: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(context)
    text = _text(payload)
    matched_tokens = [token for token in KEYWORDS if token in text]
    matched = bool(matched_tokens or payload.get("fault_type") or payload.get("alert"))
    evidence = [f"检测到故障信号: {token}" for token in matched_tokens[:4]]
    if payload.get("fault_type"):
        evidence.append(f"显式故障类型: {payload.get('fault_type')}")
    return {
        "status": "matched" if matched else "skipped",
        "confidence": 0.72 if matched else 0.0,
        "summary": "检测到通用故障信号" if matched else "未检测到明确故障信号",
        "evidence": evidence,
    }
