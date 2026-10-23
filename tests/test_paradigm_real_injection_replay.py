import copy

from eval.paradigm_real_injection_replay import (
    _aggregate,
    _backfill_contract_sample,
    _evidence_contract_status,
    _route_evidence,
    _rescore_sample,
    _score_artifact,
    _validate_evidence_contract,
    _validate_ground_truth_reachability,
    _apply_execution_profile,
)
from configs.config_loader import get_config
from paradigms.plan_and_execute import PlanAndExecuteParadigm


def _sample(case_id, paradigm, correct, latency, calls):
    return {
        "case_id": case_id,
        "paradigm": paradigm,
        "repeat": 1,
        "mechanism_family": "network_request",
        "valid": True,
        "correct": correct,
        "latency_s": latency,
        "actual_model_calls": calls,
        "result": {
            "metrics": {"llm_calls": calls + 4, "agent_calls": 4},
        },
    }


def test_route_evidence_never_uses_ground_truth_and_preserves_observations():
    case = {
        "incident": "A target request fails.",
        "evidence": [
            "Fault-window latency is 500 ms.",
            "One request returns errno EACCES.",
            "Recovery status is success.",
        ],
        "ground_truth": {"label": "SECRET LABEL", "match_groups": [["secret"]]},
    }
    routed = _route_evidence(case)
    combined = "\n".join(routed.values())
    assert "SECRET LABEL" not in combined
    for observation in case["evidence"]:
        assert observation in combined


def test_aggregate_uses_each_case_repeat_once_for_pairwise_statistics():
    samples = [
        _sample("c1", "chain", True, 10.0, 1),
        _sample("c1", "debate", False, 20.0, 4),
        _sample("c2", "chain", False, 12.0, 1),
        _sample("c2", "debate", True, 22.0, 4),
    ]
    result = _aggregate(samples, ["chain", "debate"])
    paired = result["pairwise"]["chain__vs__debate"]
    assert paired["paired_n"] == 2
    assert paired["first_only"] == 1
    assert paired["second_only"] == 1
    assert result["by_paradigm"]["chain"]["avg_actual_model_calls"] == 1
    assert result["by_paradigm"]["debate"]["avg_actual_model_calls"] == 4


def test_plan_and_execute_uses_planned_domain_steps_and_falls_back_safely():
    focused = PlanAndExecuteParadigm._planned_agent_queries(
        {
            "plan": [
                {"agent": "metric_agent", "action": "check CPU", "expected_evidence": "throttling"},
                {"agent": "log_agent", "action": "inspect errors", "command": "logs"},
                {"agent": "kubectl", "action": "not a domain agent"},
            ]
        },
        "incident",
    )
    assert set(focused) == {"metric_agent", "log_agent"}
    assert "throttling" in focused["metric_agent"]
    fallback = PlanAndExecuteParadigm._planned_agent_queries({}, "incident")
    assert set(fallback) == PlanAndExecuteParadigm.DOMAIN_AGENTS


def test_evidence_contract_marks_only_required_types_as_incomplete():
    case = {
        "id": "case",
        "evidence_contract": {
            "required": ["metric", "trace"],
            "supporting": ["event"],
            "inapplicable": ["log"],
        },
    }
    _validate_evidence_contract(case)
    status = _evidence_contract_status(
        case,
        {
            "metric_agent": [{"available": True}],
            "event_agent": [{"available": True}],
        },
    )
    assert status["complete"] is False
    assert status["missing"] == ["trace"]
    assert status["supporting_collected"] == ["event"]


def test_contract_metrics_keep_incomplete_in_strict_denominator():
    samples = [
        {**_sample("c1", "plan_and_execute", True, 10.0, 1), "evidence_complete": True, "diagnosis_correct": True},
        {**_sample("c2", "plan_and_execute", False, 12.0, 1), "evidence_complete": False, "diagnosis_correct": True},
    ]
    result = _aggregate(samples, ["plan_and_execute"])
    metrics = result["by_paradigm"]["plan_and_execute"]
    assert metrics["evidence_completeness_rate"] == 0.5
    assert metrics["strict_end_to_end_accuracy"] == 0.5
    assert metrics["conditional_accuracy"] == 1.0


def test_legacy_react_backfill_credits_only_agents_present_in_output():
    case = {
        "id": "c1",
        "evidence_contract": {
            "required": ["metric", "trace"],
            "supporting": [],
            "inapplicable": ["log", "event"],
        },
    }
    sample = _sample("c1", "react", True, 10.0, 2)
    sample["result"]["evidence_summary"] = {
        "metric_agent": "CPU rose",
        "log_agent": "No errors",
    }
    sample["routed_evidence"] = {
        "metric_agent": "- CPU rose",
        "log_agent": "- No errors",
        "trace_agent": "- target container",
        "event_agent": "- recovered",
    }
    enriched = _backfill_contract_sample(sample, case)
    assert enriched["evidence_complete"] is False
    assert enriched["evidence_collected"] == ["log", "metric"]
    assert enriched["evidence_missing"] == ["trace"]
    assert enriched["correct"] is False


def test_accuracy_profile_enables_precision_verifier_without_changing_evidence_gate():
    cfg = copy.deepcopy(get_config())
    _apply_execution_profile(type("Pool", (), {"cfg": cfg})(), "plan_and_execute", "plan_accuracy_contract_v2")
    assert cfg.pipeline.max_evidence_iterations == 1
    assert cfg.pipeline.enable_correlation is False
    assert cfg.pipeline.enable_precision_verifier is True

    cfg_v3 = copy.deepcopy(get_config())
    _apply_execution_profile(
        type("Pool", (), {"cfg": cfg_v3})(),
        "plan_and_execute",
        "plan_accuracy_contract_v3",
    )
    assert cfg_v3.pipeline.enable_evidence_coverage_gate is True


def test_scoring_groups_must_be_reachable_from_model_visible_input():
    reachable = {
        "id": "reachable",
        "incident": "One target request fails.",
        "evidence": ["target failures=4", "Recovery status is success."],
        "ground_truth": {"match_groups": [["target", "request"], ["failures=4", "four"]]},
    }
    _validate_ground_truth_reachability(reachable)

    unreachable = {
        "id": "unreachable",
        "incident": "One target request fails.",
        "evidence": ["target failures=4"],
        "ground_truth": {"match_groups": [["hidden injector name"]]},
    }
    try:
        _validate_ground_truth_reachability(unreachable)
    except ValueError as exc:
        assert "unreachable" in str(exc)
    else:
        raise AssertionError("unreachable scoring group was accepted")


def test_rescore_does_not_change_model_output_and_keeps_incomplete_strictly_wrong():
    sample = _sample("c1", "chain", False, 1.0, 1)
    sample["result"]["root_cause"] = "target failures=4 caused the incident"
    sample["evidence_complete"] = False
    case = {
        "ground_truth": {
            "label": "target failure",
            "match_groups": [["target"], ["failures=4"]],
        }
    }
    rescored = _rescore_sample(sample, case)
    assert rescored["result"] == sample["result"]
    assert rescored["diagnosis_correct"] is True
    assert rescored["correct"] is False


def test_full_rca_scoring_includes_scope_and_nested_decisive_evidence():
    artifact = {
        "root_cause": "A one-shot negative return occurred.",
        "affected_services": ["stable_operation binary function, call 3"],
        "evidence_summary": {
            "decisive_evidence": ["Call 3 is marked injected=1 and returned -77."],
        },
    }
    groups = [
        ["binary function", "stable_operation"],
        ["call 3", "-77"],
        ["one-shot", "injected=1"],
    ]
    assert _score_artifact(artifact, groups) is True
