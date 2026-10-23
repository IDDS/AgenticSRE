"""Evidence-grounded confidence calibration for RCA outputs."""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from confidence_statistics import clopper_pearson_lower_bound


_SEVERITY_WEIGHT = {
    "critical": 1.0,
    "error": 0.9,
    "warning": 0.65,
    "warn": 0.65,
    "info": 0.35,
}

_VAGUE_PATTERNS = [
    r"\bunknown\b",
    r"\bgeneral investigation\b",
    r"\bmanual investigation\b",
    r"\binsufficient evidence\b",
    r"\bcannot determine\b",
    r"\bunclear\b",
    "无法确定",
    "证据不足",
    "不明确",
]

_NON_INDEPENDENT_EVIDENCE_KEYS = {
    "structured_evidence",
    "correlation",
    "graph_rca",
    "remediation",
    "agent_skill_context",
    "agent_workflow",
}

_DEFAULT_CALIBRATION_CONFIG = {
    "raw_weight": 0.18,
    "hypothesis_weight": 0.18,
    "evidence_weight": 0.27,
    "quality_weight": 0.17,
    "specificity_weight": 0.10,
    "history_weight": 0.10,
    "hypothesis_top_weight": 0.75,
    "evidence_coverage_weight": 0.45,
    "evidence_signal_weight": 0.35,
    "evidence_alignment_weight": 0.20,
    "quality_judge_weight": 0.65,
    "evidence_source_target": 4,
    "evidence_signal_target": 3.0,
    "evidence_alignment_target": 5,
    "min_evidence_sources_without_cap": 2,
    "min_sources_without_structured_evidence": 3,
    "min_root_cause_chars": 30,
    "specificity_cap_threshold": 0.55,
    "min_history_samples": 5,
    "history_confidence_level": 0.95,
    "low_history_diagnosis_accuracy": 0.30,
    "default_cap": 0.95,
    "single_evidence_cap": 0.72,
    "missing_structured_evidence_cap": 0.78,
    "contradictory_evidence_cap": 0.70,
    "review_required_cap": 0.75,
    "low_specificity_cap": 0.65,
    "low_history_diagnosis_accuracy_cap": 0.65,
}


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _config_value(config: Any, name: str, default: Any) -> Any:
    if isinstance(config, dict):
        return config.get(name, default)
    return getattr(config, name, default) if config is not None else default


def _positive_int(value: Any, default: int) -> int:
    try:
        return max(1, int(float(value)))
    except (TypeError, ValueError):
        return default


def _normalized_weights(config: Any, names: Sequence[str]) -> Tuple[Dict[str, float], bool]:
    values = {
        name.removesuffix("_weight"): max(
            0.0,
            _to_float(_config_value(config, name, _DEFAULT_CALIBRATION_CONFIG[name]), _DEFAULT_CALIBRATION_CONFIG[name]),
        )
        for name in names
    }
    total = sum(values.values())
    fallback_applied = total <= 0
    if total <= 0:
        values = {
            name.removesuffix("_weight"): _DEFAULT_CALIBRATION_CONFIG[name]
            for name in names
        }
        total = sum(values.values())
    normalized = {name: value / total for name, value in values.items()}
    return normalized, fallback_applied or abs(total - 1.0) > 1e-9


def _resolve_calibration_config(config: Any = None) -> Dict[str, Any]:
    source = "explicit"
    if config is None:
        try:
            from configs.config_loader import get_config
            config = getattr(get_config(), "confidence_calibration", None)
            source = "runtime_config"
        except Exception:
            source = "defaults"
    elif hasattr(config, "confidence_calibration"):
        config = getattr(config, "confidence_calibration")

    factor_weights, factor_normalized = _normalized_weights(config, (
        "raw_weight", "hypothesis_weight", "evidence_weight",
        "quality_weight", "specificity_weight", "history_weight",
    ))
    evidence_weights, evidence_normalized = _normalized_weights(config, (
        "evidence_coverage_weight", "evidence_signal_weight", "evidence_alignment_weight",
    ))
    evidence_weights = {
        name.removeprefix("evidence_"): value
        for name, value in evidence_weights.items()
    }
    hypothesis_top_weight = _clamp(_to_float(
        _config_value(config, "hypothesis_top_weight", _DEFAULT_CALIBRATION_CONFIG["hypothesis_top_weight"]),
        _DEFAULT_CALIBRATION_CONFIG["hypothesis_top_weight"],
    ))
    quality_judge_weight = _clamp(_to_float(
        _config_value(config, "quality_judge_weight", _DEFAULT_CALIBRATION_CONFIG["quality_judge_weight"]),
        _DEFAULT_CALIBRATION_CONFIG["quality_judge_weight"],
    ))

    def bounded(name: str) -> float:
        return _clamp(_to_float(
            _config_value(config, name, _DEFAULT_CALIBRATION_CONFIG[name]),
            _DEFAULT_CALIBRATION_CONFIG[name],
        ))

    def bounded_with_legacy(name: str, legacy_name: str) -> float:
        configured = _config_value(config, name, None)
        if configured is None:
            configured = _config_value(
                config,
                legacy_name,
                _DEFAULT_CALIBRATION_CONFIG[name],
            )
        return _clamp(_to_float(configured, _DEFAULT_CALIBRATION_CONFIG[name]))

    return {
        "source": source,
        "weights": factor_weights,
        "hypothesis_top_weight": hypothesis_top_weight,
        "evidence_weights": evidence_weights,
        "quality_judge_weight": quality_judge_weight,
        "evidence_source_target": _positive_int(
            _config_value(config, "evidence_source_target", _DEFAULT_CALIBRATION_CONFIG["evidence_source_target"]),
            _DEFAULT_CALIBRATION_CONFIG["evidence_source_target"],
        ),
        "evidence_signal_target": max(0.1, _to_float(
            _config_value(config, "evidence_signal_target", _DEFAULT_CALIBRATION_CONFIG["evidence_signal_target"]),
            _DEFAULT_CALIBRATION_CONFIG["evidence_signal_target"],
        )),
        "evidence_alignment_target": _positive_int(
            _config_value(config, "evidence_alignment_target", _DEFAULT_CALIBRATION_CONFIG["evidence_alignment_target"]),
            _DEFAULT_CALIBRATION_CONFIG["evidence_alignment_target"],
        ),
        "min_evidence_sources_without_cap": _positive_int(
            _config_value(config, "min_evidence_sources_without_cap", _DEFAULT_CALIBRATION_CONFIG["min_evidence_sources_without_cap"]),
            _DEFAULT_CALIBRATION_CONFIG["min_evidence_sources_without_cap"],
        ),
        "min_sources_without_structured_evidence": _positive_int(
            _config_value(config, "min_sources_without_structured_evidence", _DEFAULT_CALIBRATION_CONFIG["min_sources_without_structured_evidence"]),
            _DEFAULT_CALIBRATION_CONFIG["min_sources_without_structured_evidence"],
        ),
        "min_root_cause_chars": _positive_int(
            _config_value(config, "min_root_cause_chars", _DEFAULT_CALIBRATION_CONFIG["min_root_cause_chars"]),
            _DEFAULT_CALIBRATION_CONFIG["min_root_cause_chars"],
        ),
        "specificity_cap_threshold": bounded("specificity_cap_threshold"),
        "min_history_samples": _positive_int(
            _config_value(config, "min_history_samples", _DEFAULT_CALIBRATION_CONFIG["min_history_samples"]),
            _DEFAULT_CALIBRATION_CONFIG["min_history_samples"],
        ),
        "history_confidence_level": bounded("history_confidence_level"),
        "low_history_diagnosis_accuracy": bounded_with_legacy(
            "low_history_diagnosis_accuracy",
            "low_history_success_rate",
        ),
        "caps": {
            "default": bounded("default_cap"),
            "single_evidence": bounded("single_evidence_cap"),
            "missing_structured_evidence": bounded("missing_structured_evidence_cap"),
            "contradictory_evidence": bounded("contradictory_evidence_cap"),
            "review_required": bounded("review_required_cap"),
            "low_specificity": bounded("low_specificity_cap"),
            "low_history_diagnosis_accuracy": bounded_with_legacy(
                "low_history_diagnosis_accuracy_cap",
                "low_history_success_cap",
            ),
        },
        "normalization_applied": factor_normalized or evidence_normalized,
    }


def _successful_evidence_agents(evidence: Dict[str, Any]) -> List[str]:
    agents: List[str] = []
    for name, result in (evidence or {}).items():
        if name in _NON_INDEPENDENT_EVIDENCE_KEYS:
            continue
        if isinstance(result, dict) and result.get("error"):
            continue
        if isinstance(result, dict) and (result.get("summary") or result.get("evidence_items")):
            canonical = _safe_text(result.get("agent") or name).strip() or name
            canonical = re.sub(r"_closure(?:_\d+)?$", "", canonical)
            canonical = re.sub(r"^skill_\d+_", "skill:", canonical)
            if canonical not in _NON_INDEPENDENT_EVIDENCE_KEYS:
                agents.append(canonical)
    return sorted(set(agents))


def _structured_items(evidence: Dict[str, Any]) -> List[Dict[str, Any]]:
    structured = (evidence or {}).get("structured_evidence")
    if isinstance(structured, dict) and isinstance(structured.get("items"), list):
        return [x for x in structured.get("items") or [] if isinstance(x, dict)]
    items: List[Dict[str, Any]] = []
    for result in (evidence or {}).values():
        if isinstance(result, dict):
            items.extend(x for x in (result.get("evidence_items") or []) if isinstance(x, dict))
    return items[:100]


def _evidence_text(evidence: Dict[str, Any], limit: int = 6000) -> str:
    parts: List[str] = []
    for name, result in (evidence or {}).items():
        if name == "structured_evidence" and isinstance(result, dict):
            for item in (result.get("items") or [])[:20]:
                if isinstance(item, dict):
                    parts.append(str(
                        item.get("observation") or item.get("message") or
                        item.get("signal") or item
                    ))
            continue
        if name in _NON_INDEPENDENT_EVIDENCE_KEYS:
            continue
        if isinstance(result, dict):
            parts.append(f"{name}: {result.get('summary', '')}")
            for item in (result.get("evidence_items") or [])[:10]:
                parts.append(str(item))
        else:
            parts.append(f"{name}: {result}")
    return "\n".join(parts)[:limit].lower()


def _hypothesis_factor(
    hypotheses: Sequence[Any],
    result: Dict[str, Any],
    profile: Dict[str, Any],
) -> Tuple[float, Dict[str, Any], List[str]]:
    reasons: List[str] = []
    if not hypotheses:
        return _clamp(_to_float(result.get("confidence"), 0.5)), {
            "top": 0.0,
            "margin": 0.0,
            "count": 0,
        }, ["未提供假设排序，使用诊断原始置信度作为先验"]

    confidences = sorted([
        _to_float(getattr(h, "confidence", None) if not isinstance(h, dict) else h.get("confidence"), 0.0)
        for h in hypotheses
    ], reverse=True)
    top = confidences[0] if confidences else 0.0
    second = confidences[1] if len(confidences) > 1 else 0.0
    margin = max(0.0, top - second)
    top_weight = profile["hypothesis_top_weight"]
    margin_weight = 1.0 - top_weight
    factor = _clamp(top_weight * top + margin_weight * min(1.0, margin * 2.0))
    if margin >= 0.25:
        reasons.append("最高假设与次高假设差距较大，根因排序较稳定")
    elif len(confidences) > 1:
        reasons.append("多个假设分数接近，降低诊断确定性")
    return factor, {
        "top": round(top, 3),
        "margin": round(margin, 3),
        "count": len(confidences),
        "top_weight": round(top_weight, 4),
        "margin_weight": round(margin_weight, 4),
    }, reasons


def _evidence_factor(
    evidence: Dict[str, Any],
    result: Dict[str, Any],
    profile: Dict[str, Any],
) -> Tuple[float, Dict[str, Any], List[str], List[str]]:
    reasons: List[str] = []
    limitations: List[str] = []
    agents = _successful_evidence_agents(evidence)
    items = _structured_items(evidence)
    coverage_score = min(1.0, len(agents) / profile["evidence_source_target"])

    severity_points = 0.0
    support_count = 0
    contradict_count = 0
    for item in items:
        severity_points += _SEVERITY_WEIGHT.get(str(item.get("severity") or "info").lower(), 0.35)
        support_count += len(item.get("supports") or [])
        contradict_count += len(item.get("contradicts") or [])
    signal_score = min(1.0, severity_points / profile["evidence_signal_target"])

    text = _evidence_text(evidence)
    root = _safe_text(result.get("root_cause")).lower()
    fault_type = _safe_text(result.get("fault_type")).lower()
    affected = " ".join(_safe_text(x).lower() for x in (result.get("affected_services") or []))
    keyword_hits = 0
    tokens = list(dict.fromkeys(
        re.findall(r"[a-zA-Z0-9_\-/]{4,}|[\u4e00-\u9fff]{2,}", f"{root} {fault_type} {affected}")[:30]
    ))
    for token in tokens:
        if token and token in text:
            keyword_hits += 1
    alignment_score = min(1.0, keyword_hits / profile["evidence_alignment_target"])

    weights = profile["evidence_weights"]
    factor = _clamp(
        weights["coverage"] * coverage_score +
        weights["signal"] * signal_score +
        weights["alignment"] * alignment_score
    )
    if len(agents) >= profile["min_sources_without_structured_evidence"]:
        reasons.append(f"至少 {profile['min_sources_without_structured_evidence']} 个独立证据源提供了可用证据")
    if len(agents) < profile["min_evidence_sources_without_cap"]:
        limitations.append("独立证据源数不足")
    if items:
        reasons.append(f"结构化证据 {len(items)} 条参与校准")
    else:
        limitations.append("缺少结构化证据")
    if contradict_count:
        limitations.append("存在与结论冲突的证据")

    return factor, {
        "successful_agents": agents,
        "agent_count": len(agents),
        "structured_count": len(items),
        "support_count": support_count,
        "contradict_count": contradict_count,
        "coverage_score": round(coverage_score, 3),
        "signal_score": round(signal_score, 3),
        "alignment_score": round(alignment_score, 3),
        "keyword_hits": keyword_hits,
        "weights": {name: round(value, 4) for name, value in weights.items()},
        "source_target": profile["evidence_source_target"],
        "signal_target": profile["evidence_signal_target"],
        "alignment_target": profile["evidence_alignment_target"],
    }, reasons, limitations


def _specificity_factor(
    result: Dict[str, Any],
    profile: Dict[str, Any],
) -> Tuple[float, Dict[str, Any], List[str], List[str]]:
    root = _safe_text(result.get("root_cause")).strip()
    fault_type = _safe_text(result.get("fault_type")).strip()
    services = result.get("affected_services") or []
    text = f"{root} {fault_type}".lower()
    reasons: List[str] = []
    limitations: List[str] = []
    score = 0.25
    root_long_enough = len(root) >= profile["min_root_cause_chars"]
    if root_long_enough:
        score += 0.25
    else:
        limitations.append("根因描述过短")
    fault_type_present = bool(fault_type and fault_type.lower() != "unknown")
    if fault_type_present:
        score += 0.2
    else:
        limitations.append("故障类型缺失或 unknown")
    services_present = bool(services)
    if services_present:
        score += 0.15
    else:
        limitations.append("影响对象未定位")
    vague = any(re.search(p, text) for p in _VAGUE_PATTERNS)
    if not vague:
        score += 0.15
    else:
        limitations.append("诊断文本包含不确定表述")
    if score >= 0.75:
        reasons.append("根因、故障类型和影响对象较具体")
    return _clamp(score), {
        "root_cause_chars": len(root),
        "min_root_cause_chars": profile["min_root_cause_chars"],
        "root_cause_long_enough": root_long_enough,
        "fault_type_present": fault_type_present,
        "affected_services_present": services_present,
        "contains_vague_language": vague,
    }, reasons, limitations


def _history_factor(
    history_stats: Optional[Dict[str, Any]],
    profile: Dict[str, Any],
) -> Tuple[float, Dict[str, Any], List[str], List[str]]:
    reasons: List[str] = []
    limitations: List[str] = []
    stats = dict(history_stats or {})
    recovery_sample_count = int(_to_float(stats.get("sample_count"), 0))
    recovery_success_rate_raw = stats.get("success_rate")
    recovery_success_rate = _to_float(recovery_success_rate_raw, -1.0)
    diagnosis_sample_count = int(_to_float(stats.get("diagnosis_sample_count"), 0))
    diagnosis_correct_count = int(_to_float(stats.get("diagnosis_correct_count"), 0))
    diagnosis_accuracy = _to_float(stats.get("diagnosis_accuracy"), -1.0)
    if diagnosis_sample_count > 0 and diagnosis_correct_count <= 0 and diagnosis_accuracy >= 0:
        diagnosis_correct_count = max(
            0,
            min(diagnosis_sample_count, round(diagnosis_accuracy * diagnosis_sample_count)),
        )
    if diagnosis_sample_count <= 0:
        return 0.5, {
            "history_kind": "diagnosis_correctness",
            "diagnosis_sample_count": 0,
            "diagnosis_correct_count": 0,
            "diagnosis_accuracy": None,
            "diagnosis_accuracy_lower_bound": None,
            "sufficient_samples": False,
            "used_for_diagnosis": False,
            "required_sample_count": profile["min_history_samples"],
            "recovery_sample_count": recovery_sample_count,
            "recovery_success_rate": round(recovery_success_rate, 3) if recovery_success_rate >= 0 else None,
        }, ["缺少经确认的历史诊断标签，恢复历史不参与根因置信度"], []

    sufficient = diagnosis_sample_count >= profile["min_history_samples"]
    accuracy = diagnosis_correct_count / max(diagnosis_sample_count, 1)
    lower_bound = clopper_pearson_lower_bound(
        diagnosis_correct_count,
        diagnosis_sample_count,
        confidence_level=profile["history_confidence_level"],
    )
    factor = _clamp(lower_bound)
    if not sufficient:
        factor = 0.5
        reasons.append("历史诊断标签不足，按中性因子处理")
    elif lower_bound >= 0.8:
        reasons.append("历史诊断正确率置信下界较高")
    elif lower_bound <= profile["low_history_diagnosis_accuracy"]:
        limitations.append("历史诊断正确率置信下界较低")
    else:
        reasons.append("按经确认的历史诊断正确率置信下界校准")
    return factor, {
        "history_kind": "diagnosis_correctness",
        "diagnosis_sample_count": diagnosis_sample_count,
        "diagnosis_correct_count": diagnosis_correct_count,
        "diagnosis_accuracy": round(accuracy, 3),
        "diagnosis_accuracy_lower_bound": round(lower_bound, 3),
        "confidence_level": round(profile["history_confidence_level"], 3),
        "sufficient_samples": sufficient,
        "used_for_diagnosis": sufficient,
        "required_sample_count": profile["min_history_samples"],
        "recovery_sample_count": recovery_sample_count,
        "recovery_success_rate": round(recovery_success_rate, 3) if recovery_success_rate >= 0 else None,
    }, reasons, limitations


def calibrate_rca_confidence(
    result: Dict[str, Any],
    *,
    evidence: Optional[Dict[str, Any]] = None,
    hypotheses: Optional[Sequence[Any]] = None,
    judge_result: Optional[Dict[str, Any]] = None,
    critique: Optional[Dict[str, Any]] = None,
    history_stats: Optional[Dict[str, Any]] = None,
    calibration_config: Any = None,
) -> Dict[str, Any]:
    """Return RCA result with calibrated confidence and an auditable explanation."""
    profile = _resolve_calibration_config(calibration_config)
    adjusted = dict(result or {})
    raw_conf = _clamp(_to_float(adjusted.get("confidence"), 0.0))
    hyp_factor, hyp_meta, hyp_reasons = _hypothesis_factor(hypotheses or [], adjusted, profile)
    evidence_factor, evidence_meta, evidence_reasons, evidence_limitations = _evidence_factor(
        evidence or {}, adjusted, profile
    )
    specificity, specificity_meta, specificity_reasons, specificity_limitations = _specificity_factor(
        adjusted, profile
    )
    history_factor, history_meta, history_reasons, history_limitations = _history_factor(
        history_stats, profile
    )
    judge_score = _clamp(_to_float((judge_result or {}).get("combined_score"), raw_conf))
    critic_score = _clamp(_to_float((critique or {}).get("quality_score"), judge_score))
    quality_judge_weight = profile["quality_judge_weight"]
    quality_factor = _clamp(
        quality_judge_weight * judge_score +
        (1.0 - quality_judge_weight) * critic_score
    )

    factor_values = {
        "raw": raw_conf,
        "hypothesis": hyp_factor,
        "evidence": evidence_factor,
        "quality": quality_factor,
        "specificity": specificity,
        "history": history_factor,
    }
    contributions = {
        name: profile["weights"][name] * value
        for name, value in factor_values.items()
    }
    pre_cap_confidence = sum(contributions.values())

    cap_value = profile["caps"]["default"]
    cap_triggers: List[Dict[str, Any]] = []
    caps: List[str] = []

    def apply_cap(code: str, value: float, reason: str) -> None:
        nonlocal cap_value
        cap_triggers.append({"code": code, "cap": round(value, 3), "reason": reason})
        caps.append(reason)
        cap_value = min(cap_value, value)

    if evidence_meta["agent_count"] < profile["min_evidence_sources_without_cap"]:
        apply_cap(
            "insufficient_evidence_sources",
            profile["caps"]["single_evidence"],
            f"独立证据源不足（少于 {profile['min_evidence_sources_without_cap']} 个），置信度上限受限",
        )
    if (
        evidence_meta["structured_count"] == 0 and
        evidence_meta["agent_count"] < profile["min_sources_without_structured_evidence"]
    ):
        apply_cap(
            "missing_structured_evidence",
            profile["caps"]["missing_structured_evidence"],
            "缺少结构化证据且证据源不足，置信度上限受限",
        )
    if evidence_meta["contradict_count"] > 0:
        apply_cap(
            "contradictory_evidence",
            profile["caps"]["contradictory_evidence"],
            "存在与结论冲突的证据，置信度上限受限",
        )
    if (judge_result or {}).get("needs_review") or (critique or {}).get("needs_human_review"):
        review_sources = []
        if (judge_result or {}).get("needs_review"):
            review_sources.append("Judge")
        if (critique or {}).get("needs_human_review"):
            review_sources.append("Critic")
        apply_cap(
            "human_review_required",
            profile["caps"]["review_required"],
            f"{'/'.join(review_sources)} 要求人工复核，置信度上限受限",
        )
    if specificity < profile["specificity_cap_threshold"]:
        apply_cap(
            "low_specificity",
            profile["caps"]["low_specificity"],
            "根因或影响对象定位不具体，置信度上限受限",
        )
    if (
        history_meta.get("used_for_diagnosis") and
        _to_float(history_meta.get("diagnosis_accuracy_lower_bound"), 1.0) <= profile["low_history_diagnosis_accuracy"]
    ):
        apply_cap(
            "low_history_diagnosis_accuracy",
            profile["caps"]["low_history_diagnosis_accuracy"],
            "历史诊断正确率置信下界较低，置信度上限受限",
        )

    calibrated = round(_clamp(min(pre_cap_confidence, cap_value)), 3)
    formula = " + ".join(
        f"{profile['weights'][name]:.4f}*{name}"
        for name in ("raw", "hypothesis", "evidence", "quality", "specificity", "history")
    ) + ", then apply configured evidence/quality/history caps"
    adjusted.setdefault("confidence_raw", round(raw_conf, 3))
    adjusted["confidence"] = calibrated
    adjusted["confidence_explanation"] = {
        "method": "evidence_calibrated_v2",
        "confidence_kind": "heuristic_support_score",
        "profile": "configurable_v2",
        "formula": formula,
        "raw_confidence": round(raw_conf, 3),
        "pre_cap_confidence": round(pre_cap_confidence, 3),
        "calibrated_confidence": calibrated,
        "delta": round(calibrated - raw_conf, 3),
        "factors": {name: round(value, 3) for name, value in factor_values.items()},
        "weights": {name: round(value, 4) for name, value in profile["weights"].items()},
        "contributions": {name: round(value, 4) for name, value in contributions.items()},
        "hypothesis": hyp_meta,
        "evidence": evidence_meta,
        "quality": {
            "judge_score": round(judge_score, 3),
            "critic_score": round(critic_score, 3),
            "judge_weight": round(quality_judge_weight, 4),
            "critic_weight": round(1.0 - quality_judge_weight, 4),
        },
        "specificity": specificity_meta,
        "history": history_meta,
        "cap": round(cap_value, 3),
        "cap_triggers": cap_triggers,
        "reasons": hyp_reasons + evidence_reasons + specificity_reasons + history_reasons,
        "limitations": evidence_limitations + specificity_limitations + history_limitations,
        "caps": caps,
        "configuration": {
            "source": profile["source"],
            "normalization_applied": profile["normalization_applied"],
            "hypothesis_top_weight": round(profile["hypothesis_top_weight"], 4),
            "evidence_weights": {
                name: round(value, 4) for name, value in profile["evidence_weights"].items()
            },
            "quality_judge_weight": round(profile["quality_judge_weight"], 4),
            "thresholds": {
                "min_evidence_sources_without_cap": profile["min_evidence_sources_without_cap"],
                "min_sources_without_structured_evidence": profile["min_sources_without_structured_evidence"],
                "specificity_cap_threshold": round(profile["specificity_cap_threshold"], 3),
                "min_history_samples": profile["min_history_samples"],
                "low_history_diagnosis_accuracy": round(profile["low_history_diagnosis_accuracy"], 3),
                "history_confidence_level": round(profile["history_confidence_level"], 3),
            },
            "caps": {name: round(value, 3) for name, value in profile["caps"].items()},
        },
    }
    return adjusted


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
    if endpoint_status in {"ok", "failed", "unavailable", "degraded"}:
        signal_points += 0.16
        reasons.append("推理接口状态可观测")
    if metrics_status in {"ok", "failed", "unavailable", "degraded"}:
        signal_points += 0.14
        reasons.append("vLLM 指标接口状态可观测")
    if process_count > 0:
        signal_points += 0.10
        reasons.append("发现 vLLM 进程")
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
    return calibrated, {
        "method": "llm_fault_observable_signals_v1",
        "raw_confidence": round(_clamp(base_confidence), 3),
        "calibrated_confidence": calibrated,
        "delta": round(calibrated - _clamp(base_confidence), 3),
        "factors": {
            "signal_score": round(_clamp(signal_points), 3),
            "base_confidence": round(_clamp(base_confidence), 3),
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
