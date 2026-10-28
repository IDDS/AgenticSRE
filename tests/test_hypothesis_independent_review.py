"""Proposer assertions and independent, fallible judgments stay separate."""
import asyncio
import copy
import json
from types import SimpleNamespace as NS

import pytest

from diagnostic_strategy import hypothesis_review_context, validate_hypothesis_review, strategy_view
from memory.rca_judge import RCAJudge
from orchestrator.diagnosis_closure import DiagnosisClosureManager


def context(status="confirmed"):
    return hypothesis_review_context([{"id":"H1","description":"checkout 依赖连接失败导致错误",
        "status":status,"confidence":.95,"validation_errors":[]}],
        [{"id":"obs-real","observation":"dependency connection refused","entity":{"name":"checkout","uid":"svc-1"}}])


def row(**changes):
    return {"id":"H1","status":"supported","supporting_evidence_ids":["obs-real"],
        "contradicting_evidence_ids":[],"reason":"当前依赖错误直接支持机制", "missing_evidence":[],"next_agents":[],**changes}


def validate(rows,ctx=None):
    return validate_hypothesis_review(ctx or context(),{"hypothesis_reviews":rows})


def test_review_does_not_mutate_or_relabel_proposer_and_is_not_execution_authority():
    original=context();before=copy.deepcopy(original)
    result=validate([row()],original)
    assert result["all_candidates_reviewed"] and result["items"][0]["assertion_verified"]
    assert result["semantics"]["execution_authorized"] is False and result["semantics"]["entailment_is_model_judgment"]
    assert original==before and result["items"][0]["proposer_status"]=="confirmed"


@pytest.mark.parametrize("changes",[
    {"supporting_evidence_ids":[]},{"supporting_evidence_ids":["invented"]},
    {"supporting_evidence_ids":[{}]},{"supporting_evidence_ids":"obs-real"},
    {"supporting_evidence_ids":["obs-real"]*9},{"contradicting_evidence_ids":["obs-real"]},
    {"status":"confirmed"},{"status":None},{"status":{}},{"reason":""},
])
def test_invalid_or_uncontested_claims_cannot_certify_assertion(changes):
    result=validate([row(**changes)])
    assert result["blockers"] and not result["items"][0]["assertion_verified"]


@pytest.mark.parametrize("rows",[[],[row(),row()],[row(),row(id="invented")],[row(),"bad"]])
def test_missing_duplicate_or_unknown_review_is_not_last_write_wins(rows):
    result=validate(rows)
    assert result["blockers"] and not result["all_candidates_reviewed"]


def test_uncertainty_without_refs_is_valid_but_does_not_verify_confirmed_candidate():
    result=validate([row(status="uncertain",supporting_evidence_ids=[],reason="缺少直接机制证据",
        missing_evidence=["当前错误日志"],next_agents=["log_agent","kubectl","delete","event_agent"])])
    item=result["items"][0]
    assert not item["validation_errors"] and item["status"]=="uncertain" and result["blockers"]
    assert item["next_agents"]==["log_agent","event_agent"]


def test_rejected_candidate_needs_direct_counterevidence_not_absence_of_mention():
    assert validate([row(status="uncertain",supporting_evidence_ids=[])],context("rejected"))["blockers"]
    result=validate([row(status="contradicted",supporting_evidence_ids=[],contradicting_evidence_ids=["obs-real"])],context("rejected"))
    assert not result["blockers"] and result["items"][0]["assertion_verified"]


@pytest.mark.parametrize("reason",[
    "调度事件未提及卷问题，间接反驳卷机制。",
    "观测中无 PVC 引用信息，因此不支持卷机制。",
    "未发现相关错误，排除存储故障。",
    "No mention of PVC state; indirect counterevidence.",
    "Absence of storage evidence disproves this mechanism.",
    "The event does not support the PVC explanation.",
])
def test_real_review_absence_rationales_are_preserved_but_not_certified(reason):
    result=validate([row(status="contradicted",supporting_evidence_ids=[],
        contradicting_evidence_ids=["obs-real"],reason=reason)],context("rejected"))
    item=result["items"][0]
    assert result["all_candidates_reviewed"] and result["blockers"]
    assert item["reviewer_status"]=="contradicted" and item["status"]=="uncertain"
    assert item["reason"]==reason and item["rationale_limits"]
    assert not item["assertion_verified"]


def test_active_uncertainty_is_not_silently_certified_and_does_not_assert_a_cause():
    result=validate([row(status="uncertain",supporting_evidence_ids=[])],context("active"))
    assert not result["blockers"] and not result["items"][0]["assertion_verified"]


def test_proposer_validation_errors_cannot_be_repaired_by_overwriting_audit_state():
    ctx=context();ctx["hypotheses"][0]["validation_errors"]=["unresolved_evidence_reference"]
    assert validate([row()],ctx)["blockers"]


def test_unverified_rejection_stays_active_and_independent_review_still_blocks():
    from agents.hypothesis_agent import Hypothesis, HypothesisAgent
    hyp = Hypothesis('H1', '后端不可用可能导致请求失败', .6)
    agent = HypothesisAgent(None)
    agent.set_evidence_catalog([{'id':'obs-real','entity':{'name':'checkout','uid':'svc-1'}}])
    agent._grounded_updates([hyp], {'hypotheses':[{'id':'H1','status':'rejected','confidence':0,
        'supporting_evidence_ids':[],'contradicting_evidence_ids':['obs-real'],
        'reasoning':'没有 Pod 因而排除后端不可用','unresolved_questions':[]}]}, False, 5)
    assert hyp.status == 'active' and hyp.proposed_status == 'rejected' and hyp.confidence == .6
    assert 'unverified_rejection' in hyp.validation_errors and hyp.unresolved_questions
    assert hyp.contradicting_evidence_ids == ['obs-real'] and hyp.update_reason
    review = validate_hypothesis_review(hypothesis_review_context([hyp.to_dict()], agent.evidence_catalog),
        {'hypothesis_reviews':[row(status='contradicted', supporting_evidence_ids=[],
                                   contradicting_evidence_ids=['obs-real'])]})
    assert review['blockers'] and review['items'][0]['assertion_requires_review']
    assert review['items'][0]['proposer_status'] == 'active'
    assert review['items'][0]['proposed_status'] == 'rejected'
    assert not review['items'][0]['assertion_verified']


@pytest.mark.parametrize('update', [None, [], [{'id':'H1'}, {'id':'H1'}],
    [{'id':'H1','status':'active','confidence':.5,'supporting_evidence_ids':['obs-real'],
      'contradicting_evidence_ids':[],'unresolved_questions':[]}],
    [{'id':'H1','status':'rejected','confidence':float('nan'),'supporting_evidence_ids':[],
      'contradicting_evidence_ids':['obs-real']}],
    [{'id':'H1','status':'confirmed','confidence':.9,'supporting_evidence_ids':[],
      'contradicting_evidence_ids':['obs-real']}]])
def test_unverified_rejection_cannot_be_cleared_by_a_later_incomplete_update(update):
    from agents.hypothesis_agent import Hypothesis, HypothesisAgent
    hyp = Hypothesis('H1', '仍需反证的候选', .6, proposed_status='rejected',
                     validation_errors=['unverified_rejection'], unresolved_questions=['保留原缺口'],
                     contradicting_evidence_ids=['obs-original'], update_reason='原始排除理由')
    agent=HypothesisAgent(None); agent.set_evidence_catalog([{'id':'obs-real'}])
    agent._grounded_updates([hyp], {'hypotheses':update}, False, 5)
    assert hyp.status == 'active' and hyp.proposed_status == 'rejected'
    assert 'unverified_rejection' in hyp.validation_errors and '保留原缺口' in hyp.unresolved_questions
    assert hyp.contradicting_evidence_ids == ['obs-original'] and hyp.update_reason == '原始排除理由'


def test_pending_rejection_can_resolve_only_with_registered_direct_test():
    from agents.hypothesis_agent import Hypothesis, HypothesisAgent
    hyp=Hypothesis('H1','卷声明候选',.6,service='target',verification_test='no_pvc_or_ephemeral_volume',
        proposed_status='rejected',validation_errors=['unverified_rejection'],unresolved_questions=['需检查卷声明'])
    agent=HypothesisAgent(None)
    agent.set_evidence_catalog([{'id':'obs-test','entity':{'kind':'Pod','name':'target','namespace':'shop','uid':'uid'},
                                 'verification_checks':{'no_pvc_or_ephemeral_volume':True}}])
    agent._grounded_updates([hyp],{'hypotheses':[{'id':'H1','status':'rejected','confidence':.1,
        'supporting_evidence_ids':[],'contradicting_evidence_ids':['obs-test'],'unresolved_questions':[]}]},False,5)
    assert hyp.status=='rejected' and hyp.proposed_status=='rejected'
    assert not hyp.validation_errors and not hyp.unresolved_questions


def test_existing_judge_call_carries_observations_and_missing_review_blocks_high_scores():
    class Model:
        calls=0
        def json_chat(self,messages,**options):
            self.calls+=1
            assert "hypothesis_review_request" in messages[-1]["content"]
            assert "obs-real" in messages[-1]["content"]
            return {"judge_level":0,"coherence_score":.99,"evidence_score":.99,
                "specificity_score":.99,"actionability_score":.99,"supported_evidence_ids":["E1"],
                "unsupported_claims":[],"contradictions":[]}
    model=Model()
    judge=RCAJudge(model,NS(memory=NS(judge_threshold=.65)))
    quality=judge.judge("observed dependency error","dependency error",.99,
        evidence={"log_agent":{"evidence_items":[{"source":"logs","observation":"dependency connection refused"}]}},
        hypothesis_context=context())
    assert model.calls==1 and quality["needs_review"] and quality["combined_score"]==0
    assert "asserted_hypothesis_not_independently_verified" in quality["review_blockers"]


def test_independent_gap_overrides_proposer_early_stop_and_drives_bounded_focus():
    review=validate([row(status="uncertain",supporting_evidence_ids=[],reason="需当前日志核对",
        missing_evidence=["实例 UID 对应的依赖错误"],next_agents=["log_agent"])])
    plan=DiagnosisClosureManager().build_plan({"root_cause":"checkout error","confidence":.95},
        {"combined_score":0,"needs_review":True,"hypothesis_review":review},{},{},targeted_evidence_complete=True)
    assert plan.should_iterate and plan.max_rounds==1 and plan.target_agents==["log_agent"]
    assert "实例 UID" in plan.focus_queries["log_agent"] and "H1" in plan.focus_queries["log_agent"]
    assert plan.failure_modes==["hypothesis_review_disagreement"]


def test_failed_partial_history_preserves_review_event_without_inventing_old_verdict():
    event={"event":"hypothesis_independent_review","stage":"initial_quality_review",**validate([row()])}
    current=strategy_view({"id":"run","status":"failed","events":[event]})
    assert current["recording_status"]=="recorded" and current["independent_reviews"]==[event]
    assert strategy_view({"id":"old","status":"completed"})["independent_reviews"]==[]


def test_real_engine_rechecks_asserted_hypothesis_after_one_focused_closure(monkeypatch,tmp_path):
    import orchestrator.rca_engine as engine
    from configs.config_loader import AppConfig
    from agents.hypothesis_agent import Hypothesis
    from tools.base_tool import ToolRegistry
    from tools.llm_client import LLMClient
    cfg=AppConfig();cfg.llm.api_key="offline-regression-placeholder"
    cfg.memory.enabled=cfg.evolution.enabled=False
    cfg.pipeline.enable_collaboration_optimization=cfg.pipeline.enable_correlation=False
    cfg.pipeline.enable_graph_rca=False;cfg.pipeline.enable_recovery=True;cfg.pipeline.max_evidence_iterations=1
    monkeypatch.chdir(tmp_path);monkeypatch.setattr("configs.config_loader.get_config",lambda:cfg)
    actual,queries,reviews=[],[],[]
    def generate(self,*args,**kwargs):
        self.last_generation_method="fixture"
        return [Hypothesis("H1","checkout 依赖连接失败导致错误",.6)]
    monkeypatch.setattr(engine.HypothesisAgent,"generate",generate)
    monkeypatch.setattr(engine.PlanningAgent,"generate_plan",lambda *args:{"plan":[{"agent":"event_agent"}]})
    def collector(name):
        class Collector:
            def __init__(self,*args): pass
            async def analyze(self,query,*args,**kwargs):
                actual.append(name);queries.append(query)
                return {"summary":"dependency connection refused","evidence_items":[{
                    "source":name,"signal_type":"application_error","observation":"dependency connection refused",
                    "raw_ref":{"kind":"Service","name":"checkout","namespace":"shop","uid":"svc-1"}}]}
        return Collector
    for cls,name in [("EventAgent","event_agent"),("LogAgent","log_agent"),("MetricAgent","metric_agent"),
                     ("TraceAgent","trace_agent"),("LLMInferenceAgent","llm_inference_agent")]:
        monkeypatch.setattr(engine,cls,collector(name))
    def chat(self,messages,**options):
        options["_receipt"]["completion_status"]="complete"
        user=messages[-1]["content"]
        if '"current_hypotheses"' in user:
            cat=json.loads(user)["observations"]
            return json.dumps({"hypotheses":[{"id":"H1","confidence":.98,"status":"confirmed",
                "supporting_evidence_ids":[cat[0]["id"]],"contradicting_evidence_ids":[]}]})
        if "independent RCA evidence reviewer" in messages[0]["content"]:
            reviews.append(user)
            return json.dumps({"judge_level":0,"coherence_score":.99,"evidence_score":.99,"specificity_score":.99,
                "actionability_score":.99,"supported_evidence_ids":["E1"],"unsupported_claims":[],"contradictions":[],
                "hypothesis_reviews":[row(status="uncertain",supporting_evidence_ids=[],reason="仍需直接日志验证",
                    next_agents=["log_agent"],missing_evidence=["依赖日志与目标 UID 的关联"])]})
        return json.dumps({"root_cause":"shop/checkout 依赖连接失败导致错误","confidence":.99,
            "fault_type":"dependency_failure","reasoning_chain":"观测到连接错误，需核验机制"})
    monkeypatch.setattr(LLMClient,"chat",chat)
    def forbidden(*args,**kwargs): raise AssertionError("unverified hypothesis must not recover")
    monkeypatch.setattr(engine,"RemediationAgent",forbidden)
    result=asyncio.run(engine.run_rca("Investigate checkout errors","shop",config=cfg,registry=ToolRegistry()))
    assert result["status"]=="completed"
    history=result["diagnostic_strategy"]["independent_reviews"]
    assert len(history)==2 and len(reviews)==2 and actual==["event_agent","log_agent"]
    assert "依赖日志与目标 UID" in queries[1]
    assert result["collaboration"]["closure"]["plan"]["max_rounds"]==1
    assert result["result"]["automation_gate"]["allowed"] is False
    assert result["result"]["diagnosis_status"]=="requires_review" and result["judge"]["needs_review"]
    assert result["hypotheses"][0]["status"]=="confirmed" # audit, not overwritten to hide disagreement
