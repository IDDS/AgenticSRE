from orchestrator.diagnosis_closure import DiagnosisClosureManager


def test_confirmed_top1_does_not_expand_dormant_evidence_branches() -> None:
    manager = DiagnosisClosureManager()
    plan = manager.build_plan(
        rca_result={"root_cause": "KV Cache pressure caused request queueing", "confidence": 0.62},
        judge_result={"combined_score": 0.58, "needs_review": True},
        critique={
            "quality_score": 0.65,
            "needs_revision": True,
            "missing_evidence": [],
        },
        evidence={
            "metric_agent": {"summary": "KV Cache 95%"},
            "llm_inference_agent": {"summary": "waiting requests 44"},
        },
        targeted_evidence_complete=True,
    )

    assert plan.should_iterate is False
    assert "Top1 hypothesis" in plan.reason


def test_unconfirmed_run_keeps_normal_closure_behavior() -> None:
    manager = DiagnosisClosureManager()
    plan = manager.build_plan(
        rca_result={"root_cause": "unknown", "confidence": 0.4},
        judge_result={"combined_score": 0.4, "needs_review": True},
        critique={"quality_score": 0.5, "needs_revision": True, "missing_evidence": []},
        evidence={"metric_agent": {"summary": "insufficient"}},
        targeted_evidence_complete=False,
    )

    assert plan.should_iterate is True
