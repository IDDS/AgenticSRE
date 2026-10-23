"""Confidence calibration helpers owned by the skill runtime.

The skill runtime needs auditable confidence numbers without depending on the
legacy multi-agent orchestrator. These helpers carry over the production rules
that matter operationally: observable signals raise confidence, weak evidence
caps overconfident diagnoses, and confirmed historical diagnosis labels can
adjust the score. Recovery outcomes are kept separate from diagnosis scoring.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from confidence_statistics import clopper_pearson_lower_bound


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_text_items(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        items: List[str] = []
        for key, item in value.items():
            if isinstance(item, dict):
                text = item.get("summary") or item.get("observation") or item.get("message")
                if text:
                    items.append(str(text))
                else:
                    items.append(str(key))
            elif isinstance(item, (list, tuple, set)):
                items.extend(_as_text_items(item))
            elif item:
                items.append(str(item))
        return items
    if isinstance(value, (list, tuple, set)):
        items = []
        for item in value:
            items.extend(_as_text_items(item))
        return items
    text = str(value).strip()
    return [text] if text else []


def _unique(items: Iterable[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


_OBSERVATION_SOURCE_TOKENS = {
    "metric": ("metric", "prometheus", "指标"),
    "event": ("event", "事件"),
    "trace": ("trace", "jaeger", "链路", "追踪"),
    "log": ("log", "日志"),
    "kubectl": ("kubectl", "证据显示"),
    "probe": ("probe", "探针"),
}


def _observation_sources(text: str) -> List[str]:
    lower = str(text or "").lower()
    return [
        source
        for source, tokens in _OBSERVATION_SOURCE_TOKENS.items()
        if any(token in lower for token in tokens)
    ]


def _observed_payload_evidence(payload: Dict[str, Any]) -> Tuple[List[str], List[str], int]:
    """Collect evidence that carries explicit observation provenance.

    Alert text, hypotheses, and diagnosis summaries are intentionally excluded.
    They remain useful support context but are not independent observations.
    """
    items: List[str] = []
    sources: List[str] = []
    structured_count = 0

    observed_summary = payload.get("observed_evidence_summary")
    if observed_summary:
        items.extend(_as_text_items(observed_summary))
        sources.append("observed_summary")
        structured_count += 1

    for item in payload.get("evidence") or []:
        if isinstance(item, dict):
            if item.get("success") is False or item.get("observed") is False:
                continue
            source = str(item.get("source") or item.get("agent") or "").strip().lower()
            signal = item.get("signal") or item.get("metric") or item.get("event")
            value = item.get("value")
            summary = item.get("summary") or item.get("message") or item.get("observation")
            if not source and not signal and not item.get("observed"):
                continue
            text = summary or " ".join(str(part) for part in (source, signal, value) if part not in (None, ""))
            if text:
                items.append(str(text))
                sources.extend(_observation_sources(source or str(text)) or [source or "structured_observation"])
                structured_count += 1
        else:
            text = str(item or "").strip()
            tagged_sources = _observation_sources(text)
            if text and tagged_sources:
                items.append(text)
                sources.extend(tagged_sources)

    agent_evidence = payload.get("agent_evidence") if isinstance(payload.get("agent_evidence"), dict) else {}
    collection = agent_evidence.get("agent_evidence_collection") if isinstance(agent_evidence.get("agent_evidence_collection"), dict) else {}
    for record in collection.get("commands") or []:
        if not isinstance(record, dict) or record.get("success") is not True:
            continue
        if record.get("executed") is False:
            continue
        command = str(record.get("command") or "").strip()
        summary = str(record.get("summary") or "").strip()
        sample = str(record.get("sample") or "").strip()
        text = " | ".join(part for part in (command, summary, sample) if part)
        if text:
            items.append(text)
            sources.append(str(record.get("source") or "kubectl").strip().lower() or "kubectl")
            structured_count += 1

    return _unique(items), _unique(sources), structured_count


def calibrate_llm_fault_confidence(
    *,
    base_confidence: float,
    scenario_present: bool,
    observation_present: bool,
    endpoint_status: str = "unknown",
    metrics_status: str = "unknown",
    process_count: int = 0,
    gpu_count: int = 0,
    metrics_count: int = 0,
    waiting_requests: Optional[float] = None,
    running_requests: Optional[float] = None,
    dry_run: bool = False,
) -> Tuple[float, Dict[str, Any]]:
    """Calibrate deterministic LLM fault-lab confidence from observable signals."""
    signal_points = 0.0
    reasons: List[str] = []
    caps: List[str] = []

    if scenario_present:
        signal_points += 0.18
        reasons.append("故障场景已知")
    if observation_present:
        signal_points += 0.18
        reasons.append("存在目标侧观测结果")
    if endpoint_status in {"ok", "unavailable", "failed"}:
        signal_points += 0.16
        reasons.append("推理接口状态可观测")
    if metrics_status in {"ok", "unavailable", "failed"}:
        signal_points += 0.14
        reasons.append("vLLM 指标接口状态可观测")
    if process_count > 0:
        signal_points += 0.10
        reasons.append("发现 vLLM 进程")
    elif endpoint_status in {"unavailable", "failed"}:
        signal_points += 0.08
        reasons.append("推理接口不可用且未发现 vLLM 进程")
    if gpu_count > 0:
        signal_points += 0.08
        reasons.append("发现 GPU 状态")
    if metrics_count > 0:
        signal_points += 0.10
        reasons.append("采集到 vLLM 业务指标")
    if waiting_requests is not None and waiting_requests > 0:
        signal_points += 0.08
        reasons.append("等待队列指标支持业务侧传播")
    if running_requests is not None and running_requests > 0:
        signal_points += 0.04
        reasons.append("运行中请求指标支持业务活跃")

    calibrated = 0.30 * _clamp(base_confidence) + 0.70 * _clamp(signal_points)
    cap = 0.93
    if dry_run:
        cap = min(cap, 0.62)
        caps.append("Dry-run 未真实改变业务，置信度上限为 0.62")
    if not observation_present:
        cap = min(cap, 0.58)
        caps.append("缺少目标观测，置信度上限受限")
    if endpoint_status == "unknown" and metrics_status == "unknown":
        cap = min(cap, 0.68)
        caps.append("推理接口和指标接口均未知，置信度上限受限")

    calibrated = round(_clamp(min(calibrated, cap)), 3)
    raw = round(_clamp(base_confidence), 3)
    return calibrated, {
        "method": "llm_fault_observable_signals_v1",
        "raw_confidence": raw,
        "calibrated_confidence": calibrated,
        "delta": round(calibrated - raw, 3),
        "factors": {
            "signal_score": round(_clamp(signal_points), 3),
            "base_confidence": raw,
        },
        "signals": {
            "scenario_present": scenario_present,
            "observation_present": observation_present,
            "endpoint_status": endpoint_status,
            "metrics_status": metrics_status,
            "process_count": process_count,
            "gpu_count": gpu_count,
            "metrics_count": metrics_count,
            "waiting_requests": waiting_requests,
            "running_requests": running_requests,
        },
        "cap": round(cap, 3),
        "reasons": reasons,
        "caps": caps,
    }


def _skill_evidence_factor(
    support_items: Sequence[str],
    observed_items: Sequence[str],
    observed_sources: Sequence[str],
    structured_count: int,
) -> Tuple[float, Dict[str, Any], List[str], List[str]]:
    observed_count = len(observed_items)
    source_count = len(set(observed_sources))
    factor = min(
        1.0,
        0.20
        + min(observed_count, 3) * 0.18
        + min(source_count, 3) * 0.14
        + min(structured_count, 3) * 0.06,
    )
    reasons: List[str] = []
    caps: List[str] = []
    if observed_count >= 3:
        reasons.append("至少三条带来源的成功观测支持诊断")
    if source_count >= 2:
        reasons.append("存在至少两类观测来源支持诊断")
    if observed_count == 0:
        caps.append("没有成功观测证据，告警文本和诊断摘要不得替代独立证据")
    elif source_count <= 1:
        caps.append("独立观测来源不足，置信度上限受限")
    return factor, {
        "count": observed_count,
        "support_count": len(support_items),
        "observed_count": observed_count,
        "source_count": source_count,
        "sources": list(observed_sources)[:8],
        "structured_count": structured_count,
        "items": list(observed_items)[:8],
        "support_items": list(support_items)[:8],
    }, reasons, caps


def _skill_specificity_factor(diagnosis: Dict[str, Any]) -> Tuple[float, List[str], List[str]]:
    fault_type = str(diagnosis.get("fault_type") or "").strip()
    root_cause = str(diagnosis.get("root_cause") or "").strip()
    component = str(diagnosis.get("root_cause_component") or "").strip()
    factor = 0.35
    reasons: List[str] = []
    caps: List[str] = []
    if fault_type:
        factor += 0.20
        reasons.append("诊断给出故障类型")
    if root_cause and len(root_cause) >= 12:
        factor += 0.25
        reasons.append("诊断给出具体根因")
    if component:
        factor += 0.15
        reasons.append("诊断定位到组件或对象")
    factor = _clamp(factor)
    if factor < 0.55:
        caps.append("根因或组件定位不具体，置信度上限受限")
    return factor, reasons, caps


def _skill_history_factor(
    history_stats: Optional[Dict[str, Any]],
    *,
    confidence_level: float = 0.95,
) -> Tuple[float, Dict[str, Any], List[str], List[str]]:
    history = dict(history_stats or {})
    recovery_samples = int(_to_float(history.get("sample_count"), 0))
    recovery_success_rate = _to_float(history.get("success_rate"), 0.0) if recovery_samples else None
    diagnosis_samples = int(_to_float(history.get("diagnosis_sample_count"), 0))
    diagnosis_correct = int(_to_float(history.get("diagnosis_correct_count"), 0))
    diagnosis_accuracy = _to_float(history.get("diagnosis_accuracy"), -1.0)
    if diagnosis_samples > 0 and diagnosis_correct <= 0 and diagnosis_accuracy >= 0:
        diagnosis_correct = max(0, min(diagnosis_samples, round(diagnosis_accuracy * diagnosis_samples)))
    sufficient = diagnosis_samples >= 5 and 0 <= diagnosis_correct <= diagnosis_samples
    reasons: List[str] = []
    caps: List[str] = []
    if not sufficient:
        return 0.50, {
            "history_kind": "diagnosis_correctness",
            "diagnosis_sample_count": diagnosis_samples,
            "diagnosis_correct_count": diagnosis_correct,
            "diagnosis_accuracy": round(diagnosis_accuracy, 3) if diagnosis_accuracy >= 0 else None,
            "sufficient_samples": False,
            "used_for_diagnosis": False,
            "recovery_sample_count": recovery_samples,
            "recovery_success_rate": round(recovery_success_rate, 3) if recovery_success_rate is not None else None,
        }, reasons, caps
    accuracy = diagnosis_correct / diagnosis_samples
    lower_bound = clopper_pearson_lower_bound(
        diagnosis_correct,
        diagnosis_samples,
        confidence_level=confidence_level,
    )
    if lower_bound >= 0.8:
        reasons.append("经专家确认的历史诊断正确率下界较高")
    if lower_bound <= 0.3:
        caps.append("历史诊断正确率下界较低，置信度上限受限")
    return _clamp(lower_bound), {
        "history_kind": "diagnosis_correctness",
        "diagnosis_sample_count": diagnosis_samples,
        "diagnosis_correct_count": diagnosis_correct,
        "diagnosis_accuracy": round(accuracy, 3),
        "diagnosis_accuracy_lower_bound": round(lower_bound, 3),
        "confidence_level": round(confidence_level, 3),
        "sufficient_samples": True,
        "used_for_diagnosis": True,
        "recovery_sample_count": recovery_samples,
        "recovery_success_rate": round(recovery_success_rate, 3) if recovery_success_rate is not None else None,
    }, reasons, caps


def calibrate_skill_diagnosis_confidence(
    *,
    raw_confidence: float,
    diagnosis: Dict[str, Any],
    evidence: Optional[Iterable[str]] = None,
    payload: Optional[Dict[str, Any]] = None,
    skill_id: str = "",
    history_stats: Optional[Dict[str, Any]] = None,
    history_confidence_level: float = 0.95,
) -> Tuple[float, Dict[str, Any]]:
    """Calibrate a skill diagnosis using portable evidence-grounded rules."""
    payload = payload or {}
    if history_stats is None:
        explicit_history = payload.get("diagnosis_history_stats")
        if isinstance(explicit_history, dict):
            history_stats = explicit_history
        else:
            generic_history = payload.get("history_stats")
            if (
                isinstance(generic_history, dict)
                and generic_history.get("history_kind") == "diagnosis_correctness"
            ):
                history_stats = generic_history
    evidence_input = list(evidence or [])
    support_items = _unique(
        evidence_input
        + _as_text_items(payload.get("evidence"))
        + _as_text_items(diagnosis.get("evidence_summary"))
    )
    # Stage-produced evidence strings are support context. They may be derived
    # from the alert or diagnosis template, so they do not become observations
    # merely because their wording contains "event", "metric", or "log".
    observed_items: List[str] = []
    observed_sources: List[str] = []
    payload_observed, payload_sources, structured_count = _observed_payload_evidence(payload)
    observed_items = _unique(list(observed_items) + payload_observed)
    observed_sources = _unique(list(observed_sources) + payload_sources)
    raw = _clamp(raw_confidence)
    evidence_factor, evidence_meta, evidence_reasons, evidence_caps = _skill_evidence_factor(
        support_items,
        observed_items,
        observed_sources,
        structured_count,
    )
    specificity, specificity_reasons, specificity_caps = _skill_specificity_factor(diagnosis)
    history_factor, history_meta, history_reasons, history_caps = _skill_history_factor(
        history_stats,
        confidence_level=history_confidence_level,
    )

    calibrated = (
        0.35 * raw +
        0.35 * evidence_factor +
        0.20 * specificity +
        0.10 * history_factor
    )
    cap_value = 0.95
    if evidence_meta["observed_count"] == 0:
        cap_value = min(cap_value, 0.60)
    elif evidence_meta["source_count"] <= 1:
        cap_value = min(cap_value, 0.72)
    elif evidence_meta["observed_count"] < 3:
        cap_value = min(cap_value, 0.82)
    if specificity < 0.55:
        cap_value = min(cap_value, 0.65)
    if history_meta.get("used_for_diagnosis") and _to_float(history_meta.get("diagnosis_accuracy_lower_bound"), 1.0) <= 0.3:
        cap_value = min(cap_value, 0.65)
    if skill_id.endswith("legacy_recipe_bridge"):
        cap_value = min(cap_value, 0.88)
        evidence_caps.append("桥接 skill 依赖旧知识库，发布专用 skill 前置信度上限为 0.88")

    calibrated = round(_clamp(min(calibrated, cap_value)), 3)
    raw_rounded = round(raw, 3)
    return calibrated, {
        "method": "skill_evidence_calibrated_v2",
        "confidence_kind": "heuristic_support_score",
        "formula": "0.35*raw + 0.35*observed_evidence + 0.20*specificity + 0.10*diagnosis_history, then apply provenance/specificity/history/bridge caps",
        "raw_confidence": raw_rounded,
        "calibrated_confidence": calibrated,
        "delta": round(calibrated - raw_rounded, 3),
        "factors": {
            "evidence": round(evidence_factor, 3),
            "specificity": round(specificity, 3),
            "history": round(history_factor, 3),
        },
        "skill_id": skill_id,
        "evidence": evidence_meta,
        "history": history_meta,
        "cap": round(cap_value, 3),
        "reasons": evidence_reasons + specificity_reasons + history_reasons,
        "caps": evidence_caps + specificity_caps + history_caps,
    }
