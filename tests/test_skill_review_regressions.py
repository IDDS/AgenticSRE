"""Behavior regressions from the September system review; entirely offline."""
from pathlib import Path
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from skill_runtime.executor import SkillExecutor
from skill_runtime.evolution import SkillDraftGenerator, SkillDraftVerifier
from eval.hypothesis_ablation import _matches


@pytest.fixture(scope="module")
def runtime():
    return SkillExecutor.from_dir(ROOT / "skill_runtime/skills")


@pytest.mark.parametrize("dbe,sbe,matched", [(0, 12, True), (2, 0, True), (2, 3, True), (0, 0, False)])
def test_ecc_counters_remain_independent(runtime, dbe, sbe, matched):
    run = runtime.run("llm.gpu_hardware_health", {"metrics": {
        "DCGM_FI_DEV_ECC_DBE_VOL_TOTAL": dbe,
        "DCGM_FI_DEV_ECC_SBE_VOL_TOTAL": sbe}}, stages=["detect", "diagnose"])
    assert (run.stages["detect"].status == "matched") is matched
    if not matched:
        assert run.stages["diagnose"].status == "skipped"
        run = runtime.run("llm.gpu_hardware_health", {"metrics": {
            "DCGM_FI_DEV_ECC_DBE_VOL_TOTAL": dbe, "DCGM_FI_DEV_ECC_SBE_VOL_TOTAL": sbe}}, stages=["diagnose"])
    observed = run.stages["diagnose"].output["observed"]
    assert observed["ecc_errors"] == dbe + sbe
    assert observed["ecc_dbe_errors"] == dbe
    assert observed["ecc_sbe_errors"] == sbe


@pytest.mark.parametrize("field", ["kv_cache_usage", "gpu_memory_usage"])
@pytest.mark.parametrize("value,pressure", [(0.2, False), (20, False), (0.9, True), (90, True)])
def test_memory_threshold_uses_normalized_ratio(runtime, field, value, pressure):
    run = runtime.run("llm.business_slo", {"metrics": {field: value}}, stages=["diagnose"])
    result = run.stages["diagnose"].output
    assert (result["fault_type"] == "LLM::kv_cache_or_gpu_memory_pressure") is pressure
    assert result["slo_snapshot"][field] == (value / 100 if value > 1 else value)


@pytest.mark.parametrize("mode,expected", [("missing", "skipped"), ("failure", "error"), ("partial", "error"), ("success", "ok")])
def test_prometheus_requires_actual_success(runtime, mode, expected):
    calls = []
    def runner(tool, args, payload):
        calls.append(args["query"])
        success = mode == "success" or (mode == "partial" and len(calls) == 1)
        return {"executed": True, "success": success, "error": "" if success else "timeout"}
    run = runtime.run("tool.prometheus_query", {"queries": ["sum(up)", "max(up)"]},
                      stages=["diagnose"], tool_runner=None if mode == "missing" else runner)
    diagnosis = run.stages["diagnose"]
    assert diagnosis.status == expected
    assert diagnosis.confidence == (0.86 if mode == "success" else 0.0)
    assert len(diagnosis.evidence) == {"missing": 0, "failure": 0, "partial": 1, "success": 2}[mode]
    if mode != "success":
        assert diagnosis.summary != "已执行 Prometheus 查询。"


def approved_evidence(draft=None):
    return {"draft_sha256": SkillDraftVerifier().release_gate(draft, {})["draft_sha256"] if draft else "",
            **dict.fromkeys(["unit_tests_passed", "dry_run_passed", "forward_tests_passed",
                            "rollback_verified", "human_approved"], True), "publish_target": "review-only"}


@pytest.mark.parametrize("source", [
    "def detect(\ndef diagnose(\ndef heal(\ndef verify(",
    "# def detect def diagnose def heal def verify\npass",
    "def outer():\n    def detect(ctx): pass\n    def diagnose(ctx): pass\n    def heal(ctx): pass\n    def verify(ctx): pass",
])
def test_draft_requires_compilable_top_level_functions(source):
    draft = SkillDraftGenerator().draft({"fault_type": "test"})
    draft.skill_py = source
    verifier = SkillDraftVerifier()
    assert not verifier.verify_draft(draft).review_ready
    assert not verifier.release_gate(draft, approved_evidence())["publish_ready"]


@pytest.mark.parametrize("value", ["false", "true", 1, [True], {"passed": True}, False, None])
def test_release_gate_requires_boolean_true(value):
    draft = SkillDraftGenerator().draft({"fault_type": "test"})
    verifier = SkillDraftVerifier()
    for key in ("unit_tests_passed", "dry_run_passed", "forward_tests_passed", "rollback_verified", "human_approved"):
        evidence = approved_evidence(draft)
        evidence[key] = value
        assert not verifier.release_gate(draft, evidence)["publish_ready"]


def test_release_gate_valid_draft_remains_manual_only():
    draft = SkillDraftGenerator().draft({"fault_type": "test"})
    result = SkillDraftVerifier().release_gate(draft, approved_evidence(draft))
    assert result["publish_ready"]
    assert result["publish_plan"]["mode"] == "manual_reviewed_copy"
    assert result["publish_plan"]["auto_enable"] is False


def holdout_groups():
    data = yaml.safe_load((ROOT / "eval/evolution_holdout_cases.yaml").read_text())
    return data["cases"][0]["ground_truth"]["match_groups"]


def test_rejected_cause_cannot_rescue_wrong_conclusion():
    assert not _matches({"root_cause": "Database disk is full", "rejected_distractors": [
        "Envoy sidecar CPU quota throttled is unrelated; stale GC alert is also unrelated"]}, holdout_groups())


def test_selected_component_does_not_rescue_rejected_mechanism():
    assert not _matches({"root_cause": "Envoy disk is full", "rejected_distractors": [
        "CPU quota throttled is unrelated; stale GC alert"]}, holdout_groups())


def test_correct_cause_and_explicit_distractor_both_score():
    answer = {"root_cause": "Envoy sidecar CPU quota throttled", "rejected_distractors": ["stale GC alert"]}
    assert _matches(answer, holdout_groups())
    del answer["rejected_distractors"]
    assert not _matches(answer, holdout_groups())


def test_old_cause_only_rubric_and_causal_chain_remain_supported():
    assert _matches({"root_cause": "MongoDB unavailable", "causal_chain": ["zero replicas"]},
                    [["mongodb"], ["zero replicas"]])


def test_reachability_validator_accepts_scoped_rubrics():
    from eval.paradigm_real_injection_replay import _validate_ground_truth_reachability
    _validate_ground_truth_reachability({"incident": "Envoy CPU quota throttled; stale GC alert",
                                       "ground_truth": {"match_groups": holdout_groups()}})
