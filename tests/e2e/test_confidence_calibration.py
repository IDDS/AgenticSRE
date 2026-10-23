#!/usr/bin/env python3
"""Regression tests for evidence-grounded confidence calibration."""

from __future__ import annotations

import pathlib
import sys
import math
import pytest
from types import SimpleNamespace

from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.fault_context_store import FaultContextStore
from memory.expert_feedback import ExpertFeedbackStore
from memory.rca_judge import RCAJudge
from configs.config_loader import load_config
import configs.config_loader as config_loader_module
from confidence_statistics import clopper_pearson_lower_bound
from remediation_history import build_recovery_scope
from orchestrator.confidence_calibrator import calibrate_llm_fault_confidence, calibrate_rca_confidence
from skill_runtime.confidence import (
    calibrate_llm_fault_confidence as calibrate_skill_llm_fault_confidence,
    calibrate_skill_diagnosis_confidence,
)
import web_app.app as app_module


def test_heal_rule_min_confidence_blocks_real_execution(monkeypatch) -> None:
    cfg = app_module._get_config()
    monkeypatch.setattr(cfg.remediation, "confidence_threshold", 0.7)
    recipe = {
        "fault_type": "ServiceUnavailable",
        "risk": "medium",
        "requires_approval": True,
        "min_confidence": 0.92,
    }
    diagnosis = {
        "fault_type": "ServiceUnavailable",
        "root_cause": "frontend deployment scaled to zero",
        "confidence": 0.88,
        "evidence_summary": {"metric": "frontend 503", "event": "deployment replicas=0"},
    }

    gate = app_module._build_diagnosis_gate(diagnosis, cfg, recipe)

    assert gate["ready"] is False
    assert gate["confidence_threshold"] == 0.92
    assert gate["rule_min_confidence"] == 0.92
    assert "rule_min_confidence" in gate["blockers"]


def test_heal_auto_execution_gate_requires_historical_safe_rule(monkeypatch) -> None:
    cfg = app_module._get_config()
    monkeypatch.setattr(cfg.remediation, "max_auto_risk_level", "medium")
    recipe = {
        "fault_type": "ServiceUnavailable",
        "risk": "medium",
        "safe_auto_execute": True,
    }
    diagnosis = {
        "fault_type": "ServiceUnavailable",
        "root_cause": "frontend deployment scaled to zero",
        "confidence": 0.94,
    }
    suggestions = [{
        "step": "restore",
        "command": "kubectl scale deployment/frontend -n default --replicas=1",
        "rollback_command": "kubectl scale deployment/frontend -n default --replicas=0",
        "risk": "medium",
    }]

    gate = app_module._build_auto_execution_gate(
        recipe=recipe,
        suggestions=suggestions,
        diagnosis=diagnosis,
        cfg=cfg,
        recovery_history_stats={
            "history_kind": "recovery_outcome",
            "sample_count": 20,
            "success_count": 20,
            "failure_count": 0,
            "success_rate": 1.0,
        },
    )

    assert gate["ready"] is True
    assert gate["safe_rule"] is True
    assert gate["history_success_rate"] == 1.0
    assert gate["history_success_lower_bound"] > 0.8
    assert gate["blockers"] == []


def test_heal_auto_execution_gate_rejects_small_perfect_history() -> None:
    cfg = app_module._get_config()
    recipe = {"fault_type": "ServiceUnavailable", "risk": "low", "safe_auto_execute": True}
    diagnosis = {
        "fault_type": "ServiceUnavailable",
        "root_cause": "frontend deployment scaled to zero",
        "confidence": 0.95,
    }
    suggestions = [{"step": "inspect", "command": "kubectl get deploy frontend", "risk": "low"}]

    gate = app_module._build_auto_execution_gate(
        recipe=recipe,
        suggestions=suggestions,
        diagnosis=diagnosis,
        cfg=cfg,
        recovery_history_stats={
            "history_kind": "recovery_outcome",
            "sample_count": 5,
            "success_count": 5,
            "failure_count": 0,
            "success_rate": 1.0,
        },
    )

    assert gate["ready"] is False
    assert gate["history_success_lower_bound"] == 0.549
    assert "history_success_lower_bound_low" in gate["blockers"]


def test_heal_auto_execution_gate_message_does_not_round_898_to_90_percent(monkeypatch) -> None:
    cfg = app_module._get_config()
    monkeypatch.setattr(cfg.remediation, "auto_confidence_threshold", 0.90)
    recipe = {"fault_type": "ServiceUnavailable", "risk": "low", "safe_auto_execute": True}
    diagnosis = {
        "fault_type": "ServiceUnavailable",
        "root_cause": "frontend deployment scaled to zero",
        "confidence": 0.898,
    }
    suggestions = [{"step": "inspect", "command": "kubectl get deploy frontend", "risk": "low"}]

    gate = app_module._build_auto_execution_gate(
        recipe=recipe,
        suggestions=suggestions,
        diagnosis=diagnosis,
        cfg=cfg,
        recovery_history_stats={
            "history_kind": "recovery_outcome",
            "sample_count": 20,
            "success_count": 20,
            "failure_count": 0,
            "success_rate": 1.0,
        },
    )

    confidence_message = next(
        message for message in gate["blocker_messages"] if "诊断置信度" in message
    )
    assert "confidence_below_auto_threshold" in gate["blockers"]
    assert "89.8%" in confidence_message
    assert "90.0%" in confidence_message


def test_heal_auto_execution_gate_blocks_recent_code_change() -> None:
    cfg = app_module._get_config()
    recipe = {"fault_type": "ServiceUnavailable", "safe_auto_execute": True}
    diagnosis = {
        "fault_type": "ServiceUnavailable",
        "root_cause": "frontend deployment scaled to zero",
        "confidence": 0.96,
        "recent_code_change": True,
        "confidence_explanation": {
            "history": {"sample_count": 10, "success_rate": 0.9, "sufficient_samples": True}
        },
    }
    suggestions = [{"step": "restore", "command": "kubectl scale deployment/frontend -n default --replicas=1", "risk": "low"}]

    gate = app_module._build_auto_execution_gate(recipe=recipe, suggestions=suggestions, diagnosis=diagnosis, cfg=cfg)

    assert gate["ready"] is False
    assert "recent_code_change" in gate["blockers"]


def test_heal_auto_execution_gate_blocks_without_recovery_history() -> None:
    cfg = app_module._get_config()
    gate = app_module._build_auto_execution_gate(
        recipe={"fault_type": "ServiceUnavailable", "safe_auto_execute": True},
        suggestions=[{"step": "inspect", "command": "kubectl get deploy frontend", "risk": "low"}],
        diagnosis={
            "fault_type": "ServiceUnavailable",
            "root_cause": "frontend deployment scaled to zero",
            "confidence": 0.96,
        },
        cfg=cfg,
    )

    assert gate["ready"] is False
    assert "history_outcome_counts_missing" in gate["blockers"]


def test_heal_auto_execution_gate_blocks_manual_only_and_missing_rollback() -> None:
    cfg = app_module._get_config()
    history = {
        "history_kind": "recovery_outcome",
        "sample_count": 20,
        "success_count": 20,
        "failure_count": 0,
        "success_rate": 1.0,
    }
    diagnosis = {
        "fault_type": "ServiceUnavailable",
        "root_cause": "frontend deployment scaled to zero",
        "confidence": 0.96,
    }

    manual_gate = app_module._build_auto_execution_gate(
        recipe={
            "fault_type": "ServiceUnavailable",
            "safe_auto_execute": True,
            "manual_only": True,
        },
        suggestions=[{"step": "inspect", "command": "kubectl get deploy frontend", "risk": "low"}],
        diagnosis=diagnosis,
        cfg=cfg,
        recovery_history_stats=history,
    )
    rollback_gate = app_module._build_auto_execution_gate(
        recipe={"fault_type": "ServiceUnavailable", "safe_auto_execute": True},
        suggestions=[{
            "step": "restore",
            "command": "kubectl scale deployment/frontend --replicas=1",
            "risk": "medium",
        }],
        diagnosis=diagnosis,
        cfg=cfg,
        recovery_history_stats=history,
    )

    assert "manual_recovery_only" in manual_gate["blockers"]
    assert "rollback_not_available" in rollback_gate["blockers"]


def test_rca_confidence_is_capped_without_independent_evidence() -> None:
    result = {
        "root_cause": "frontend service is unavailable because deployment/frontend has no ready endpoints",
        "fault_type": "service_disruption",
        "affected_services": ["frontend"],
        "confidence": 0.97,
    }
    calibrated = calibrate_rca_confidence(
        result,
        evidence={"event_agent": {"summary": "service/frontend has no endpoints"}},
        hypotheses=[],
        judge_result={"combined_score": 0.9, "needs_review": False},
        critique={"quality_score": 0.9, "needs_human_review": False},
    )

    assert calibrated["confidence"] <= 0.72
    assert calibrated["confidence_raw"] == 0.97
    assert calibrated["confidence_explanation"]["method"] == "evidence_calibrated_v2"
    assert any("独立证据源不足" in item for item in calibrated["confidence_explanation"]["caps"])


def test_fault_context_store_resolution_stats_aggregate_by_fault_type(tmp_path) -> None:
    cfg = SimpleNamespace(memory=SimpleNamespace(
        backend="json",
        db_path=str(tmp_path),
        rules_collection="rules",
        faults_collection="faults",
        max_similar_results=5,
    ))
    store = FaultContextStore(cfg)
    store.add_fault({"fault_type": "ServiceUnavailable", "resolution": {"status": "success", "trigger": "auto"}})
    store.add_fault({"fault_type": "ServiceUnavailable", "resolution": {"status": "success", "trigger": "button"}})
    store.add_fault({"fault_type": "ServiceUnavailable", "resolution": {"status": "failed", "trigger": "auto"}})
    store.add_fault({"fault_type": "ImagePullError", "resolution": {"status": "success", "code_deployed": True}})

    stats = store.resolution_stats(fault_type="ServiceUnavailable")

    assert stats["sample_count"] == 3
    assert stats["success_count"] == 2
    assert stats["failure_count"] == 1
    assert stats["success_rate"] == 0.667
    assert stats["auto_count"] == 2


def test_recovery_scope_fingerprint_ignores_read_only_checks_but_separates_environment() -> None:
    restore = "kubectl scale deployment/frontend -n default --replicas=1"
    first = build_recovery_scope(
        commands=["kubectl get endpoints frontend -n default", restore],
        cluster_id="cluster-a",
        namespace="default",
        target={"deployment": "frontend", "service": "frontend"},
    )
    second = build_recovery_scope(
        commands=["kubectl -n default get deployment frontend", restore],
        cluster_id="cluster-a",
        namespace="default",
        target={"deployment": "frontend", "service": "frontend"},
    )
    other_cluster = build_recovery_scope(
        commands=[restore],
        cluster_id="cluster-b",
        namespace="default",
        target={"deployment": "frontend", "service": "frontend"},
    )

    assert first["scope_complete"] is True
    assert first["action_count"] == 1
    assert first["action_fingerprint"] == second["action_fingerprint"]
    assert first["environment_fingerprint"] == second["environment_fingerprint"]
    assert first["environment_fingerprint"] != other_cluster["environment_fingerprint"]


def test_fault_context_store_scopes_recovery_history_by_action_and_environment(tmp_path) -> None:
    cfg = SimpleNamespace(memory=SimpleNamespace(
        backend="json",
        db_path=str(tmp_path),
        rules_collection="rules",
        faults_collection="faults",
        max_similar_results=5,
    ))
    store = FaultContextStore(cfg)
    target = {"deployment": "frontend", "service": "frontend"}
    commands = ["kubectl scale deployment/frontend -n default --replicas=1"]
    scope = build_recovery_scope(
        commands=commands,
        cluster_id="cluster-a",
        namespace="default",
        target=target,
    )
    for index in range(14):
        store.add_fault({
            "heal_run_id": f"heal-a-{index}",
            "fault_type": "ServiceUnavailable",
            "cluster_id": "cluster-a",
            "namespace": "default",
            "target": target,
            "resolution": {"status": "success", "trigger": "auto", "commands": commands},
        })
    store.add_fault({
        "heal_run_id": "heal-other-action",
        "fault_type": "ServiceUnavailable",
        "cluster_id": "cluster-a",
        "namespace": "default",
        "target": target,
        "resolution": {
            "status": "success",
            "trigger": "auto",
            "commands": ["kubectl rollout restart deployment/frontend -n default"],
        },
    })
    store.add_fault({
        "heal_run_id": "heal-other-cluster",
        "fault_type": "ServiceUnavailable",
        "cluster_id": "cluster-b",
        "namespace": "default",
        "target": target,
        "resolution": {"status": "failed", "trigger": "auto", "commands": commands},
    })
    store.add_fault({
        "heal_run_id": "legacy-unscoped",
        "fault_type": "ServiceUnavailable",
        "resolution": {"status": "success", "trigger": "button"},
    })

    stats = store.resolution_stats(
        fault_type="ServiceUnavailable",
        action_fingerprint=scope["action_fingerprint"],
        environment_fingerprint=scope["environment_fingerprint"],
        min_samples=14,
    )

    assert stats["scope_complete"] is True
    assert stats["sample_count"] == 14
    assert stats["success_count"] == 14
    assert stats["failure_count"] == 0
    assert stats["success_rate"] == 1.0
    assert stats["sufficient_samples"] is True
    assert stats["legacy_unscoped_count"] == 1


def test_hard_fault_remains_diagnosable_but_is_manual_only_for_recovery() -> None:
    result = app_module._build_heal_suggestions({
        "source": "regression",
        "fault_type": "NodeNotReady",
        "namespace": "default",
        "node": "worker-1",
        "message": "Node worker-1 is NotReady",
        "diagnosis": {
            "fault_type": "NodeNotReady",
            "root_cause": "worker-1 kubelet and node heartbeat are unavailable",
            "root_cause_component": "worker-1",
            "confidence": 0.96,
            "evidence_summary": {"event": "NodeNotReady", "condition": "Ready=False"},
        },
    })

    assert result["diagnosis_gate"]["ready"] is True
    assert result["suggestions"]
    assert result["recipe"]["manual_only"] is True
    assert result["capability"]["auto_execution_eligible"] is False
    assert "manual_recovery_only" in result["capability"]["auto_execution_gate"]["blockers"]


def test_explicit_auto_execution_is_rejected_before_running_without_scoped_history() -> None:
    client = TestClient(app_module.app)
    response = client.post("/api/heal/execute", json={
        "source": "regression",
        "fault_type": "ServiceUnavailable",
        "namespace": "default",
        "service": "frontend",
        "deployment": "frontend",
        "message": "ServiceUnavailable 503 no endpoints",
        "diagnosis": {
            "fault_type": "ServiceUnavailable",
            "root_cause": "frontend deployment has zero replicas and no ready endpoints",
            "root_cause_component": "frontend",
            "confidence": 0.96,
            "evidence_summary": {"event": "replicas=0", "metric": "HTTP 503"},
        },
        "commands": ["kubectl scale deployment/frontend -n default --replicas=1"],
        "rollback_commands": ["kubectl scale deployment/frontend -n default --replicas=0"],
        "dry_run": False,
        "approved": True,
        "auto_execute": True,
    })

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "auto_execution_gate" in detail
    assert detail["auto_execution_gate"]["ready"] is False
    assert "insufficient_history" in detail["auto_execution_gate"]["blockers"]


@pytest.mark.parametrize("recovery", [True, False, None, "exception", "business_unavailable"])
def test_approved_manual_execution_remains_compatible_and_records_verified_outcome(monkeypatch, tmp_path, recovery) -> None:
    cfg = app_module._get_config()
    monkeypatch.setattr(cfg.remediation, "enabled", True)
    monkeypatch.setattr(cfg.remediation, "recommend_only", False)
    monkeypatch.setattr(cfg.remediation, "require_approval", True)
    monkeypatch.setattr(app_module, "_HEAL_RUNS_FILE", tmp_path / "heal_runs.json")
    monkeypatch.setattr(app_module, "_HEAL_ARTIFACT_DIR", tmp_path / "heal_runs")
    monkeypatch.setattr(
        app_module,
        "_shell_sync",
        lambda command, timeout, cluster_id=None: {
            "command": command,
            "ok": True,
            "dry_run": False,
            "stdout": "scaled",
            "stderr": "",
        },
    )
    if recovery == "business_unavailable":
        monkeypatch.setattr(cfg.remediation, "business_recovery_probes", [{
            "cluster_id": "", "namespace": "default", "service": "frontend",
            "url": "http://127.0.0.1:1", "samples": 2, "interval_seconds": 1,
            "timeout_seconds": .1,
        }])
    def verify(**kwargs):
        if recovery == "business_unavailable":
            return {"status": "verified", "recovered": True, "checks": []}
        if recovery == "exception":
            raise RuntimeError("probe unavailable")
        return {"status": "unknown" if recovery is None else "verified",
                "recovered": recovery, "checks": [{"type": "deployment_status"}]}
    monkeypatch.setattr(app_module, "_verify_heal_recovery", verify)

    class FakeStore:
        def __init__(self):
            self.faults = []

        def resolution_stats(self, **kwargs):
            return {
                "history_kind": "recovery_outcome",
                "scope_required": True,
                "scope_complete": True,
                "action_fingerprint": kwargs.get("action_fingerprint"),
                "environment_fingerprint": kwargs.get("environment_fingerprint"),
                "sample_count": 0,
                "success_count": 0,
                "failure_count": 0,
                "success_rate": None,
            }

        def add_fault(self, fault):
            self.faults.append(fault)
            return "fault-recorded"

    store = FakeStore()
    monkeypatch.setattr(app_module, "_get_fault_store", lambda: store)
    client = TestClient(app_module.app)
    response = client.post("/api/heal/execute", json={
        "source": "regression",
        "fault_type": "ServiceUnavailable",
        "namespace": "default",
        "service": "frontend",
        "deployment": "frontend",
        "message": "ServiceUnavailable 503 no endpoints",
        "diagnosis": {
            "fault_type": "ServiceUnavailable",
            "root_cause": "frontend deployment has zero replicas and no ready endpoints",
            "root_cause_component": "frontend",
            "confidence": 0.96,
            "evidence_summary": {"event": "replicas=0", "metric": "HTTP 503"},
        },
        "commands": ["kubectl scale deployment/frontend -n default --replicas=1"],
        "rollback_commands": ["kubectl scale deployment/frontend -n default --replicas=0"],
        "dry_run": False,
        "approved": True,
    })

    assert response.status_code == 200, response.text
    run = response.json()
    assert run["verification_target"]["service"] == "frontend"
    assert run["action_success"] is True
    assert run["results"][0]["ok"] is True
    if recovery is None or recovery in ("exception", "business_unavailable"):
        assert run["status"] == "verification_pending"
        assert run["success"] is None
        assert run["verification"]["recovered"] is None
        assert run["recovery_history_record"]["recorded"] is False
        assert not store.faults
        saved = client.get(f"/api/heal/runs/{run['id']}").json()
        assert saved["status"] == "verification_pending"
        summary = client.get("/api/heal/runs").json()["summary"]
        assert summary["verification_pending"] == 1
        assert summary["succeeded"] == summary["failed"] == 0
        return
    if recovery is False:
        assert run["status"] == "failed"
        assert run["success"] is False
        assert store.faults[0]["resolution"]["status"] == "failed"
        return
    assert run["success"] is True
    assert run["recovery_history_record"]["recorded"] is True
    assert run["recovery_history_record"]["fault_id"] == "fault-recorded"
    assert len(store.faults) == 1
    assert store.faults[0]["resolution"]["status"] == "success"
    assert store.faults[0]["resolution"]["trigger"] == "button"


def test_expert_feedback_stats_use_diagnosis_labels_only(tmp_path) -> None:
    class TraceStore:
        def get_trace_by_id(self, incident_id):
            return SimpleNamespace(result={
                "fault_type": "ServiceUnavailable",
                "root_cause": f"root cause for {incident_id}",
                "reasoning_chain": "evidence-backed reasoning",
            })

    class Learner:
        def learn_supervised(self, *, agent_diagnosis, ground_truth, memory_scope=None, source_incident_id='', source_feedback_id=''):
            return {
                "status": "success",
                "rules_added": 0,
                "correct": ground_truth == "accepted diagnosis",
            }

    store = ExpertFeedbackStore(str(tmp_path / "feedback"))
    store.submit_feedback(
        "incident-1",
        "accepted diagnosis",
        context_learner=Learner(),
        trace_store=TraceStore(),
    )
    store.submit_feedback(
        "incident-2",
        "corrected diagnosis",
        context_learner=Learner(),
        trace_store=TraceStore(),
    )

    stats = store.diagnosis_stats(fault_type="ServiceUnavailable")

    assert stats["history_kind"] == "diagnosis_correctness"
    assert stats["diagnosis_sample_count"] == 2
    assert stats["diagnosis_correct_count"] == 1
    assert stats["diagnosis_accuracy"] == 0.5
    assert stats["diagnosis_accuracy_lower_bound"] < stats["diagnosis_accuracy"]


def test_rca_confidence_uses_structured_multi_agent_evidence() -> None:
    result = {
        "root_cause": "frontend deployment scaled to zero leaves service/frontend without ready endpoints",
        "fault_type": "service_disruption",
        "affected_services": ["frontend"],
        "confidence": 0.82,
    }
    evidence = {
        "metric_agent": {"summary": "frontend request rate dropped and 5xx increased"},
        "event_agent": {"summary": "deployment frontend scaled to zero"},
        "trace_agent": {"summary": "frontend-proxy calls to frontend return 503"},
        "structured_evidence": {
            "items": [
                {"severity": "critical", "observation": "frontend deployment replicas=0", "supports": ["service_disruption"]},
                {"severity": "warning", "observation": "frontend endpoints empty", "supports": ["service_disruption"]},
            ]
        },
    }
    hypotheses = [
        {"confidence": 0.9},
        {"confidence": 0.45},
    ]

    calibrated = calibrate_rca_confidence(
        result,
        evidence=evidence,
        hypotheses=hypotheses,
        judge_result={"combined_score": 0.88, "needs_review": False},
        critique={"quality_score": 0.86, "needs_human_review": False},
    )

    explanation = calibrated["confidence_explanation"]
    assert calibrated["confidence"] >= 0.75
    assert explanation["evidence"]["agent_count"] == 3
    assert explanation["evidence"]["structured_count"] == 2
    assert "至少 3 个独立证据源提供了可用证据" in explanation["reasons"]


def test_rca_confidence_supports_operator_configured_weights_and_caps() -> None:
    calibrated = calibrate_rca_confidence(
        {
            "root_cause": "frontend deployment scaled to zero leaves service/frontend without ready endpoints",
            "fault_type": "service_disruption",
            "affected_services": ["frontend"],
            "confidence": 0.9,
        },
        evidence={
            "metric_agent": {"summary": "frontend request rate dropped"},
            "event_agent": {"summary": "frontend deployment replicas=0"},
            "trace_agent": {"summary": "frontend returned 503"},
            "structured_evidence": {"items": [{"severity": "critical"}]},
        },
        hypotheses=[{"confidence": 0.8}, {"confidence": 0.4}],
        judge_result={"combined_score": 0.8, "needs_review": False},
        critique={"quality_score": 0.8, "needs_human_review": False},
        calibration_config=SimpleNamespace(
            raw_weight=4.0,
            hypothesis_weight=0.0,
            evidence_weight=0.0,
            quality_weight=0.0,
            specificity_weight=0.0,
            history_weight=0.0,
            default_cap=0.8,
        ),
    )

    explanation = calibrated["confidence_explanation"]
    assert calibrated["confidence"] == 0.8
    assert explanation["pre_cap_confidence"] == 0.9
    assert explanation["weights"]["raw"] == 1.0
    assert explanation["contributions"]["raw"] == 0.9
    assert explanation["configuration"]["normalization_applied"] is True
    assert explanation["profile"] == "configurable_v2"


def test_rca_confidence_uses_runtime_config_without_explicit_argument(monkeypatch) -> None:
    monkeypatch.setattr(
        config_loader_module,
        "_config",
        SimpleNamespace(confidence_calibration=SimpleNamespace(
            raw_weight=1.0,
            hypothesis_weight=0.0,
            evidence_weight=0.0,
            quality_weight=0.0,
            specificity_weight=0.0,
            history_weight=0.0,
            default_cap=0.77,
        )),
    )
    calibrated = calibrate_rca_confidence(
        {
            "root_cause": "frontend deployment scaled to zero leaves service/frontend without ready endpoints",
            "fault_type": "service_disruption",
            "affected_services": ["frontend"],
            "confidence": 0.9,
        },
        evidence={
            "metric_agent": {"summary": "frontend request rate dropped"},
            "event_agent": {"summary": "frontend replicas=0"},
            "trace_agent": {"summary": "frontend returned 503"},
            "structured_evidence": {"items": [{"severity": "critical"}]},
        },
    )

    explanation = calibrated["confidence_explanation"]
    assert calibrated["confidence"] == 0.77
    assert explanation["configuration"]["source"] == "runtime_config"
    assert explanation["weights"]["raw"] == 1.0


def test_rca_confidence_deduplicates_repeated_agent_evidence() -> None:
    calibrated = calibrate_rca_confidence(
        {
            "root_cause": "frontend deployment scaled to zero leaves service/frontend without ready endpoints",
            "fault_type": "service_disruption",
            "affected_services": ["frontend"],
            "confidence": 0.82,
        },
        evidence={
            "metric_agent": {"agent": "metric_agent", "summary": "request rate dropped"},
            "metric_agent_closure": {"agent": "metric_agent", "summary": "same metric checked again"},
            "event_agent": {"agent": "event_agent", "summary": "replicas=0"},
            "agent_skill_context": {"summary": "routing context only"},
            "structured_evidence": {"items": [{"severity": "critical"}]},
        },
        hypotheses=[{"confidence": 0.8}, {"confidence": 0.4}],
    )

    evidence_meta = calibrated["confidence_explanation"]["evidence"]
    assert evidence_meta["successful_agents"] == ["event_agent", "metric_agent"]
    assert evidence_meta["agent_count"] == 2


def test_rca_judge_recognizes_chinese_confidence_language() -> None:
    judge = RCAJudge(
        llm=None,
        config=SimpleNamespace(memory=SimpleNamespace(judge_threshold=0.65, judge_llm_weight=0.4)),
    )
    high = judge._rule_based_judge(
        "多源证据一致，根因已确认。直接证据表明 frontend 副本为 0，导致服务无可用 endpoint。" * 4,
        0.85,
    )
    low = judge._rule_based_judge(
        "证据不足，无法确定根因，仍需进一步验证，不能排除其他可能性。" * 4,
        0.85,
    )

    assert high["high_patterns"] >= 3
    assert low["low_patterns"] >= 3
    assert high["score"] > low["score"]


def test_config_loader_reads_confidence_calibration_section(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "confidence_calibration:\n"
        "  raw_weight: 0.25\n"
        "  evidence_weight: 0.40\n"
        "  default_cap: 0.91\n",
        encoding="utf-8",
    )

    cfg = load_config(str(path))

    assert cfg.confidence_calibration.raw_weight == 0.25
    assert cfg.confidence_calibration.evidence_weight == 0.40
    assert cfg.confidence_calibration.default_cap == 0.91


def test_rca_confidence_does_not_use_recovery_outcomes_as_diagnosis_history() -> None:
    result = {
        "root_cause": "transient infrastructure failure leaves workflow task unavailable",
        "fault_type": "service_disruption",
        "affected_services": ["workflow"],
        "confidence": 0.78,
    }
    evidence = {
        "metric_agent": {"summary": "workflow task unavailable"},
        "event_agent": {"summary": "transient infrastructure failure"},
        "trace_agent": {"summary": "retry succeeds after endpoint recovers"},
        "structured_evidence": {"items": [{"severity": "critical", "supports": ["service_disruption"]}]},
    }

    high = calibrate_rca_confidence(
        result,
        evidence=evidence,
        hypotheses=[{"confidence": 0.85}, {"confidence": 0.4}],
        judge_result={"combined_score": 0.85, "needs_review": False},
        critique={"quality_score": 0.85, "needs_human_review": False},
        history_stats={"sample_count": 12, "success_count": 10, "failure_count": 2, "success_rate": 0.833, "sufficient_samples": True},
    )
    low = calibrate_rca_confidence(
        result,
        evidence=evidence,
        hypotheses=[{"confidence": 0.85}, {"confidence": 0.4}],
        judge_result={"combined_score": 0.85, "needs_review": False},
        critique={"quality_score": 0.85, "needs_human_review": False},
        history_stats={"sample_count": 12, "success_count": 2, "failure_count": 10, "success_rate": 0.167, "sufficient_samples": True},
    )

    assert high["confidence"] == low["confidence"]
    assert high["confidence_explanation"]["history"]["used_for_diagnosis"] is False
    assert high["confidence_explanation"]["history"]["recovery_sample_count"] == 12
    assert any(
        "恢复历史不参与根因置信度" in item
        for item in high["confidence_explanation"]["reasons"]
    )


def test_confidence_docs_keep_diagnosis_and_recovery_history_separate() -> None:
    demo = (ROOT / "docs/demo/AMS_MindOps_智算故障闭环检查说明.md").read_text(encoding="utf-8")
    generic_skill = (
        ROOT / "skill_runtime/skills/generic_root_cause_diagnosis/SKILL.md"
    ).read_text(encoding="utf-8")
    aishperf_skill = (
        ROOT / "skill_runtime/skills/llm_aishperf_aiinfra_diagnosis/SKILL.md"
    ).read_text(encoding="utf-8")
    frontend = (ROOT / "web_app/static/js/app.js").read_text(encoding="utf-8")
    skill_detail = (ROOT / "skill_runtime/detail.py").read_text(encoding="utf-8")
    skill_docs = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "skill_runtime/skills").glob("*/SKILL.md")
    )

    assert "0.18 * raw_confidence" in demo
    assert "恢复成功率与根因置信度严格分离" in demo
    assert "恢复验证不反向提高根因置信度" in demo
    assert "恢复失败记录调整规则阈值和置信度权重" not in demo
    assert "反证排除和恢复验证结果" not in demo
    assert "对照恢复验证结果统计分类准确率" not in generic_skill
    assert "恢复验证结果只评价处置动作" in generic_skill
    assert "核对诊断准确性与恢复验证结果" not in aishperf_skill
    assert "恢复验证结果单独用于评价建议动作" in aishperf_skill
    for bilingual_label in (
        "校准前置信度（Raw）",
        "假设分支稳定性（Hypothesis）",
        "多源证据充分性（Evidence）",
        "诊断质量评审（Quality）",
        "根因具体性（Specificity）",
        "历史诊断正确性（History）",
    ):
        assert bilingual_label in frontend
        assert bilingual_label in demo
    assert "history: '历史恢复效果'" not in frontend
    assert "进入自愈门禁的最低诊断置信度" in skill_detail
    assert "自愈最低置信度" not in skill_detail
    for ambiguous_phrase in (
        "恢复置信度",
        "自愈最低置信度",
        "最低自愈置信度",
        "heal 置信度门槛",
    ):
        assert ambiguous_phrase not in skill_docs


def test_rca_confidence_uses_confirmed_diagnosis_accuracy_lower_bound() -> None:
    result = {
        "root_cause": "frontend deployment scaled to zero leaves service without endpoints",
        "fault_type": "service_disruption",
        "affected_services": ["frontend"],
        "confidence": 0.82,
    }
    evidence = {
        "metric_agent": {"summary": "frontend request rate dropped"},
        "event_agent": {"summary": "frontend deployment replicas=0"},
        "trace_agent": {"summary": "frontend returned 503"},
        "structured_evidence": {"items": [{"severity": "critical", "supports": ["service_disruption"]}]},
    }

    high = calibrate_rca_confidence(
        result,
        evidence=evidence,
        hypotheses=[{"confidence": 0.85}, {"confidence": 0.4}],
        history_stats={"diagnosis_sample_count": 40, "diagnosis_correct_count": 39},
    )
    low = calibrate_rca_confidence(
        result,
        evidence=evidence,
        hypotheses=[{"confidence": 0.85}, {"confidence": 0.4}],
        history_stats={"diagnosis_sample_count": 40, "diagnosis_correct_count": 10},
    )

    assert high["confidence"] > low["confidence"]
    assert high["confidence_explanation"]["history"]["used_for_diagnosis"] is True
    assert low["confidence_explanation"]["cap"] <= 0.65
    assert any("历史诊断正确率" in item for item in low["confidence_explanation"]["caps"])


def test_llm_fault_confidence_dry_run_cap_is_explicit() -> None:
    confidence, explanation = calibrate_llm_fault_confidence(
        base_confidence=0.9,
        scenario_present=True,
        observation_present=True,
        endpoint_status="ok",
        metrics_status="ok",
        process_count=1,
        gpu_count=3,
        metrics_count=5,
        waiting_requests=4,
        running_requests=1,
        dry_run=True,
    )

    assert confidence <= 0.62
    assert explanation["method"] == "llm_fault_observable_signals_v1"
    assert any("Dry-run" in item for item in explanation["caps"])


def test_skill_runtime_llm_confidence_matches_migrated_algorithm() -> None:
    confidence, explanation = calibrate_skill_llm_fault_confidence(
        base_confidence=0.9,
        scenario_present=True,
        observation_present=True,
        endpoint_status="ok",
        metrics_status="ok",
        process_count=1,
        gpu_count=3,
        metrics_count=5,
        waiting_requests=4,
        running_requests=1,
        dry_run=True,
    )

    assert confidence <= 0.62
    assert explanation["method"] == "llm_fault_observable_signals_v1"
    assert any("Dry-run" in item for item in explanation["caps"])


def test_skill_runtime_confidence_caps_weak_evidence() -> None:
    confidence, explanation = calibrate_skill_diagnosis_confidence(
        raw_confidence=0.97,
        diagnosis={
            "fault_type": "ConfigMissing",
            "root_cause": "missing configmap",
            "root_cause_component": "frontend",
        },
        evidence=["event: configmap not found"],
        skill_id="k8s.legacy_recipe_bridge",
    )

    assert confidence <= 0.72
    assert explanation["method"] == "skill_evidence_calibrated_v2"
    assert explanation["cap"] <= 0.72
    assert any("没有成功观测证据" in item for item in explanation["caps"])


def test_skill_runtime_confidence_uses_evidence_and_history() -> None:
    high, high_explanation = calibrate_skill_diagnosis_confidence(
        raw_confidence=0.82,
        diagnosis={
            "fault_type": "ServiceUnavailable",
            "root_cause": "frontend deployment scaled to zero leaves service without endpoints",
            "root_cause_component": "frontend",
            "evidence_summary": {"deployment": "replicas=0", "endpoints": "empty"},
        },
        evidence=[
            "metric: frontend 5xx increased",
            "event: deployment frontend scaled to zero",
            "trace: frontend-proxy returns 503",
        ],
        payload={"evidence": [
            {"source": "prometheus", "signal": "frontend_5xx", "value": 12},
            {"source": "k8s_event", "event": "deployment scaled to zero", "value": 0},
            {"source": "trace", "signal": "frontend-proxy", "value": 503},
        ]},
        skill_id="k8s.service_unavailable",
        history_stats={"diagnosis_sample_count": 40, "diagnosis_correct_count": 39},
    )
    low, low_explanation = calibrate_skill_diagnosis_confidence(
        raw_confidence=0.82,
        diagnosis={
            "fault_type": "ServiceUnavailable",
            "root_cause": "frontend deployment scaled to zero leaves service without endpoints",
            "root_cause_component": "frontend",
            "evidence_summary": {"deployment": "replicas=0", "endpoints": "empty"},
        },
        evidence=[
            "metric: frontend 5xx increased",
            "event: deployment frontend scaled to zero",
            "trace: frontend-proxy returns 503",
        ],
        payload={"evidence": [
            {"source": "prometheus", "signal": "frontend_5xx", "value": 12},
            {"source": "k8s_event", "event": "deployment scaled to zero", "value": 0},
            {"source": "trace", "signal": "frontend-proxy", "value": 503},
        ]},
        skill_id="k8s.service_unavailable",
        history_stats={"diagnosis_sample_count": 40, "diagnosis_correct_count": 10},
    )

    assert high >= 0.75
    assert high > low
    assert high_explanation["evidence"]["count"] >= 3
    assert low_explanation["cap"] <= 0.65
    assert any("历史诊断正确率" in item for item in low_explanation["caps"])


def test_clopper_pearson_lower_bound_is_conservative_for_sparse_history() -> None:
    assert round(clopper_pearson_lower_bound(5, 5), 3) == 0.549
    assert clopper_pearson_lower_bound(13, 13) < 0.8
    assert clopper_pearson_lower_bound(14, 14) > 0.8


def test_clopper_pearson_lower_bound_meets_nominal_coverage_on_grid() -> None:
    confidence_level = 0.95
    for trials in range(1, 31):
        bounds = [
            clopper_pearson_lower_bound(
                successes,
                trials,
                confidence_level=confidence_level,
            )
            for successes in range(trials + 1)
        ]
        for probability_index in range(101):
            probability = probability_index / 100.0
            coverage = 0.0
            for successes, lower_bound in enumerate(bounds):
                if lower_bound <= probability + 1e-12:
                    coverage += (
                        math.comb(trials, successes)
                        * probability ** successes
                        * (1.0 - probability) ** (trials - successes)
                    )
            assert coverage >= confidence_level - 1e-10, (
                trials,
                probability,
                coverage,
            )
