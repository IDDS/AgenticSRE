"""Deterministic gates for skill-based healing."""

from __future__ import annotations

from typing import Any, Dict, List

from .schemas import SkillMetadata, StageResult, merge_evidence


def build_policy(
    skill: SkillMetadata,
    detect: StageResult | None,
    diagnose: StageResult | None,
    heal: StageResult | None,
) -> Dict[str, Any]:
    confidence_source = diagnose or heal or StageResult("diagnose")
    confidence = max(float(confidence_source.confidence or 0.0), 0.0)
    evidence = merge_evidence(detect, diagnose, heal)
    blockers: List[str] = []
    messages: List[str] = []
    supports_heal = "heal" in skill.stages
    requires_own_diagnosis = "diagnose" in skill.stages

    if not supports_heal:
        blockers.append("heal_stage_not_declared")
        messages.append("该 Skill 未声明 heal 阶段，不提供自愈能力。")
    if supports_heal and requires_own_diagnosis and (not diagnose or diagnose.status not in {"ok", "matched"}):
        blockers.append("missing_diagnosis")
        messages.append("skill 未产生有效诊断结果。")
    if supports_heal and confidence < skill.min_confidence_for_heal:
        blockers.append("low_confidence_for_heal")
        messages.append(f"诊断置信度 {confidence:.0%} 低于自愈阈值 {skill.min_confidence_for_heal:.0%}。")
    if supports_heal and len(evidence) < skill.min_evidence_sources:
        blockers.append("insufficient_evidence")
        messages.append(f"证据数 {len(evidence)} 低于 skill 要求的 {skill.min_evidence_sources}。")
    if heal and heal.status == "blocked":
        blockers.append("heal_blocked")
        messages.append(heal.summary or "skill 自愈动作被阻断。")
    mutating_permission = any(
        p.endswith(".write")
        or ".write." in p
        or p.endswith(".admin")
        or p.endswith(".restart")
        or ".restart." in p
        for p in skill.permissions
    )
    if mutating_permission:
        if skill.requires_approval:
            messages.append("skill 包含写/重启权限，真实执行必须审批。")

    auto_blockers: List[str] = []
    auto_messages: List[str] = []
    if not supports_heal:
        auto_blockers.append("heal_stage_not_declared")
        auto_messages.append("该 Skill 不提供自愈能力。")
    else:
        if confidence < skill.min_confidence_for_auto_heal:
            auto_blockers.append("low_confidence_for_auto_heal")
            auto_messages.append(f"诊断置信度 {confidence:.0%} 低于全自动阈值 {skill.min_confidence_for_auto_heal:.0%}。")
        if not skill.safe_auto_execute:
            auto_blockers.append("skill_not_marked_safe")
            auto_messages.append("skill 未显式标记 safe_auto_execute。")
        if skill.requires_approval:
            auto_blockers.append("approval_required")
            auto_messages.append("skill 策略要求人工审批。")
        if blockers:
            auto_blockers.append("heal_gate_not_ready")
            auto_messages.append("自愈门禁未通过。")

    return {
        "supports_heal": supports_heal,
        "heal_ready": not blockers,
        "heal_blockers": blockers,
        "heal_blocker_messages": messages,
        "auto_heal_ready": not auto_blockers,
        "auto_heal_blockers": auto_blockers,
        "auto_heal_blocker_messages": auto_messages,
        "confidence": round(confidence, 3),
        "evidence_count": len(evidence),
        "requires_approval": skill.requires_approval,
        "safe_auto_execute": skill.safe_auto_execute,
        "risk": skill.risk,
        "min_confidence_for_heal": skill.min_confidence_for_heal,
        "min_confidence_for_auto_heal": skill.min_confidence_for_auto_heal,
    }
