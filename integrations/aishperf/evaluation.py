from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Tuple


AIOPS_EVAL_METRIC_DEFINITIONS: Dict[str, Dict[str, str]] = {
    "case_total": {
        "description": "Total number of AIOps diagnosis cases selected for the run.",
        "unit": "count",
        "direction": "higher_is_better",
    },
    "diagnosis_pass": {
        "description": "Number of cases where the agent diagnosis matches the expected root cause.",
        "unit": "count",
        "direction": "higher_is_better",
    },
    "diagnosis_pass_rate": {
        "description": "Ratio of evaluated cases where the agent diagnosis matches the expected root cause.",
        "unit": "ratio",
        "direction": "higher_is_better",
    },
    "evaluated_sessions": {
        "description": "Number of sessions that receive evaluator scores.",
        "unit": "count",
        "direction": "higher_is_better",
    },
    "evaluation_coverage": {
        "description": "Ratio of generated sessions that receive evaluator scores.",
        "unit": "ratio",
        "direction": "higher_is_better",
    },
    "avg_turns": {
        "description": "Average number of agent turns per diagnosis session.",
        "unit": "turns",
        "direction": "lower_is_better",
    },
    "avg_latency_s": {
        "description": "Average end-to-end diagnosis latency per evaluated session.",
        "unit": "s",
        "direction": "lower_is_better",
    },
    "fault_injection_success_rate": {
        "description": "Ratio of selected fault injections that are applied successfully.",
        "unit": "ratio",
        "direction": "higher_is_better",
    },
    "recovery_success_rate": {
        "description": "Ratio of selected fault injections that are recovered successfully after evaluation.",
        "unit": "ratio",
        "direction": "higher_is_better",
    },
    "error_rate": {
        "description": "Ratio of cases that fail during fault injection, agent execution, scoring, or recovery.",
        "unit": "ratio",
        "direction": "lower_is_better",
    },
}


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_success_status(value: Any) -> bool:
    text = str(value or "").lower()
    return text in {"ok", "success", "executed", "dry_run", "diagnosed", "suggested", "completed", "passed"}


def _expected_fault_type(scenario: Dict[str, Any], body: Dict[str, Any]) -> str:
    return str(
        scenario.get("fault_type")
        or _as_dict(body.get("result")).get("fault_type")
        or body.get("fault_type")
        or ""
    ).lower()


def _diagnosed_fault_type(diagnosis: Dict[str, Any]) -> str:
    return str(diagnosis.get("fault_type") or diagnosis.get("k8s_fault_type") or "").lower()


def _diagnosis_matches(scenario: Dict[str, Any], diagnosis: Dict[str, Any], body: Dict[str, Any]) -> bool:
    diagnosed = _diagnosed_fault_type(diagnosis)
    expected = _expected_fault_type(scenario, body)
    if expected and diagnosed and (expected in diagnosed or diagnosed in expected):
        return True
    confidence = _float(diagnosis.get("confidence"))
    ready = bool(diagnosis.get("diagnosis_ready"))
    return ready and confidence >= 0.75


def _fault_injection_success(result: Dict[str, Any]) -> Tuple[bool, str]:
    status = str(result.get("status") or "").lower()
    if status == "dry_run":
        return False, "dry-run 不计入真实注入成功率"
    if status in {"executed", "ok", "success"}:
        return True, "故障动作返回成功状态"
    effect = _as_dict(result.get("effect_summary"))
    if effect.get("ok") and not effect.get("dry_run"):
        return True, "故障效果摘要显示真实执行成功"
    return False, "未发现真实注入成功证据"


def _recovery_success(result: Dict[str, Any], heal: Dict[str, Any]) -> Tuple[bool, str]:
    recovery = _as_dict(result.get("recovery_verification"))
    if recovery.get("recovered") is True:
        return True, "恢复验证显示已恢复"
    if str(result.get("action") or "").lower() == "cleanup" and _is_success_status(result.get("status")):
        return True, "恢复动作返回成功状态"
    suggestions = _as_list(heal.get("suggestions"))
    blocked = _as_list(heal.get("blocked"))
    if suggestions and not blocked:
        return False, "已生成自愈建议，但未验证真实恢复"
    return False, "未发现恢复成功证据"


def build_aiops_eval_summary(
    *,
    scenario: Dict[str, Any],
    body: Dict[str, Any],
    source: str = "agenticsre.llm_faultlab",
) -> Dict[str, Any]:
    """Build a read-only AISHPerf-style evaluation summary for one run.

    This does not execute any fault action. It converts existing AgenticSRE
    experiment, diagnosis, and healing artifacts into AISHPerf aiops-eval style
    metrics so the UI can show a repeatable scoring view.
    """

    result = _as_dict(body.get("result"))
    diagnosis = _as_dict(body.get("diagnosis"))
    heal = _as_dict(body.get("heal"))
    trace = _as_list(body.get("trace"))
    started_at = _float(body.get("started_at") or result.get("started_at") or result.get("created_at") or 0.0)
    ended_at = _float(body.get("ended_at") or result.get("ended_at") or result.get("updated_at") or time.time())
    elapsed = max(0.0, ended_at - started_at) if started_at else 0.0

    injection_ok, injection_reason = _fault_injection_success(result)
    recovery_ok, recovery_reason = _recovery_success(result, heal)
    diagnosis_ok = _diagnosis_matches(scenario, diagnosis, body)
    diagnosis_reason = (
        "诊断故障类型与场景期望匹配，或诊断门禁与置信度达到阈值"
        if diagnosis_ok else
        "诊断结果尚未达到可判定通过条件"
    )
    evaluated = 1 if diagnosis else 0
    error = 1 if result.get("error") or diagnosis.get("error") or heal.get("error") else 0

    metrics = {
        "case_total": 1,
        "diagnosis_pass": 1 if diagnosis_ok else 0,
        "diagnosis_pass_rate": 1.0 if diagnosis_ok else 0.0,
        "evaluated_sessions": evaluated,
        "evaluation_coverage": 1.0 if evaluated else 0.0,
        "avg_turns": max(1, len(trace) or len(_as_list(body.get("skill_runs"))) or (1 if diagnosis else 0)),
        "avg_latency_s": round(elapsed, 3),
        "fault_injection_success_rate": 1.0 if injection_ok else 0.0,
        "recovery_success_rate": 1.0 if recovery_ok else 0.0,
        "error_rate": 1.0 if error else 0.0,
    }
    confidence = _float(diagnosis.get("confidence"))
    status = "passed" if diagnosis_ok and (result.get("status") == "dry_run" or injection_ok) and not error else "failed"
    run_id = str(body.get("run_id") or result.get("run_id") or f"agenticsre-aiops-{uuid.uuid4().hex[:10]}")
    result_json = {
        "run_id": run_id,
        "suite_id": "agenticsre-aiops-eval",
        "entity_id": str(body.get("cluster_id") or body.get("host_id") or "agenticsre-ai-cluster"),
        "status": status,
        "metrics": metrics,
    }
    return {
        "status": "ok",
        "source": source,
        "run_id": run_id,
        "scenario": {
            "id": scenario.get("id"),
            "name": scenario.get("name"),
            "fault_type": scenario.get("fault_type"),
            "layer": scenario.get("layer"),
        },
        "metrics": metrics,
        "metric_definitions": AIOPS_EVAL_METRIC_DEFINITIONS,
        "result_json": result_json,
        "diagnosis": {
            "passed": diagnosis_ok,
            "confidence": round(confidence, 3),
            "reason": diagnosis_reason,
            "root_cause": diagnosis.get("root_cause") or "",
        },
        "fault_injection": {
            "passed": injection_ok,
            "reason": injection_reason,
        },
        "recovery": {
            "passed": recovery_ok,
            "reason": recovery_reason,
        },
        "artifacts": {
            "result_json": "兼容 AISHPerf result.json 的最小字段，可用于后续归档或报告生成。",
            "metric_definitions": "指标定义来自 AISHPerf aiops-eval metrics/spec.json。",
        },
        "safety": {
            "read_only": True,
            "auto_execute": False,
            "note": "该评测摘要只复用已有实验、诊断和自愈结果，不会触发新的故障注入或恢复动作。",
        },
    }
