from agents.evidence_adjudicator import (
    CAUSAL_ADJUDICATION_RULES,
    build_evidence_adjudication_prompt,
    build_hypothesis_refinement_prompt,
)
from eval.hypothesis_ablation import AblationRunner, DIRECT_PROMPT, INITIAL_PROMPT, _matches


def test_adjudication_policy_covers_known_rca_failure_modes() -> None:
    lowered = CAUSAL_ADJUDICATION_RULES.lower()
    assert "stale" in lowered
    assert "counterfactual" in lowered
    assert "causal chain" in lowered
    assert "exact affected component" in lowered
    assert "independent observability failure" in lowered


def test_adjudication_prompt_is_truth_isolated() -> None:
    prompt = build_evidence_adjudication_prompt("service is slow", "- latency rose")
    assert "service is slow" in prompt
    assert "latency rose" in prompt
    assert "hidden truth" in prompt
    assert "ground_truth" not in prompt
    assert "expected label" not in prompt


def test_payload_validation_does_not_confuse_direct_prompt_prose_with_schema() -> None:
    AblationRunner._validate_payload(
        {"root_cause": "CPU quota throttling", "fault_type": "resource", "component": "pod"},
        DIRECT_PROMPT,
    )


def test_payload_validation_still_requires_hypothesis_array_for_initial_prompt() -> None:
    try:
        AblationRunner._validate_payload({"root_cause": "wrong schema"}, INITIAL_PROMPT)
    except ValueError as exc:
        assert "hypotheses" in str(exc)
    else:
        raise AssertionError("initial hypothesis response must require a hypotheses array")


def test_refinement_prompt_treats_previous_selection_as_fallible() -> None:
    prompt = build_hypothesis_refinement_prompt(
        "service latency",
        "- rollback restored latency",
        [{"id": "H1", "description": "generic resource pressure"}],
        [{"id": "H1", "confidence": 0.9, "status": "active"}],
        "H1",
    )
    assert "only a proposal" in prompt
    assert "replace it" in prompt
    assert "exact mechanism" in prompt
    assert "rollback restored latency" in prompt


def test_semantic_scorer_reads_explicit_causal_chain_and_contradictions() -> None:
    output = {
        "root_cause": "MongoDB unavailable",
        "component": "post-storage MongoDB",
        "causal_chain": ["zero replicas", "write timeout"],
        "contradictions_checked": ["CrashLoop alert is a stale distractor"],
    }
    groups = [["mongodb"], ["zero replica", "unavailable"], ["stale", "distractor"]]
    assert _matches(output, groups)
