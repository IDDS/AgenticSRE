"""A fluent diagnosis must not substitute for independently collected facts."""
from types import SimpleNamespace
import json

import pytest

from memory.rca_judge import RCAJudge


FACTS = {"log_agent": {"evidence_items": [{
    "source": "kubectl", "service": "dependency", "signal_type": "log",
    "observation": "FAILED changed false to true; HTTP status=503",
    "raw_ref": {"namespace": "test", "pod": "dependency"},
}]}}


class Reviewer:
    def __init__(self, **overrides):
        self.result = dict(judge_level=0, coherence_score=.9, evidence_score=.9,
                           specificity_score=.9, actionability_score=.9,
                           supported_evidence_ids=["E1"], unsupported_claims=[],
                           contradictions=[], feedback="Observed failure state explains 503")
        self.result.update(overrides)
        self.messages = None

    def json_chat(self, messages, **kwargs):
        self.messages = messages
        return self.result


def assess(llm, evidence=FACTS, reasoning="FAILED state caused 503"):
    cfg = SimpleNamespace(memory=SimpleNamespace(judge_threshold=.65, judge_llm_weight=.4))
    return RCAJudge(llm, cfg).judge(reasoning, "dependency FAILED state", .9, evidence=evidence)


def test_observations_reach_reviewer_and_concise_grounded_answer_passes():
    llm = Reviewer()
    result = assess(llm)
    assert not result["needs_review"]
    assert result["combined_score"] == .9
    assert "FAILED changed false to true" in str(llm.messages)
    assert "kubectl" in str(llm.messages)
    assert result["evidence_count"] == 1
    assert result["review_evidence"][0]["id"] == result["llm_based"]["supported_evidence_ids"][0]
    assert result["review_evidence"][0]["observation"] == FACTS["log_agent"]["evidence_items"][0]["observation"]


def test_fluency_does_not_change_evidence_score():
    assert assess(Reviewer())["combined_score"] == assess(
        Reviewer(), reasoning="conclusively high confidence strong evidence shows " * 50
    )["combined_score"]


@pytest.mark.parametrize("override", [
    {"supported_evidence_ids": []},
    {"supported_evidence_ids": ["E404"]},
    {"unsupported_claims": ["disk failure"]},
    {"contradictions": ["dependency is healthy"]},
    {"evidence_score": .2},
    {"evidence_score": float("nan")},
    {"evidence_score": "0.9"},
    {"evidence_score": True},
    {"judge_level": 3},
    {"contradictions": None},
])
def test_invalid_or_unsupported_review_cannot_pass(override):
    result = assess(Reviewer(**override))
    assert result["needs_review"]
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("facts", [{}, {"log_agent": {"summary": "root cause confirmed"}},
    {"log_agent": {"error": "tool failed", "evidence_items": FACTS["log_agent"]["evidence_items"]}}])
def test_absent_or_failed_collection_is_not_evidence(facts):
    assert assess(Reviewer(), evidence=facts)["needs_review"]


def test_missing_or_failed_reviewer_is_closed():
    class Broken:
        def json_chat(self, *args, **kwargs):
            raise RuntimeError("unavailable")
    assert assess(None)["needs_review"]
    assert assess(Broken())["needs_review"]


def test_legacy_call_without_evidence_does_not_certify_boasting():
    result = RCAJudge().judge("conclusively high confidence confirmed that " * 50,
                              "invented disk failure", .99)
    assert result["needs_review"]
