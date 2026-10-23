"""Falsification, provenance and bounded investigation are behavioral contracts."""
import asyncio
import copy
import json

import pytest

from agents.hypothesis_agent import Hypothesis, HypothesisAgent
from diagnostic_strategy import (InvestigationController, evidence_catalog, semantic_fingerprint,
                                 strategy_view, tool_evidence_catalog)
from operational_ontology import build_investigation, observation_id
from orchestrator.session import RCASession


def fact(value="Pending", uid="pod-1", **raw):
    return {"signal_type":"pod_waiting_state","source":"kubectl get pods -o json", "observation":value,
        "timestamp":"2026-10-20T01:00:00Z","raw_ref":{"kind":"Pod","name":"worker","namespace":"shop",
            "uid":uid,"observed_at":"2026-10-20T01:00:00Z","resource_version":"100",**raw}}


def saved_catalog(*facts, error=False):
    session=RCASession(namespace="shop")
    payload={"evidence_items":list(facts),"error":error}
    session.record_evidence("event_agent",payload)
    return session,evidence_catalog({"event_agent":payload},session.observation_history)


def assessed(id="H1", **extra):
    return {"id":id,"description":"Pod 调度约束不满足的待验证机制","confidence":.95,"status":"confirmed",
            "supporting_evidence_ids":["obs-1"],"contradicting_evidence_ids":[],"validation_errors":[],**extra}


def controller(agents=("event_agent","log_agent"),limit=3):
    return InvestigationController(agents,max_iterations=limit,confidence_threshold=.85)


def decide(control, hypotheses, *, catalog=None, iteration=1, selected=("event_agent",), results=None, novelty=None):
    catalog=catalog if catalog is not None else [{"id":"obs-1","semantic_fingerprint":"state-one"}]
    return control.decide(iteration=iteration,before=[],after=hypotheses,catalog=catalog,
        novelty=novelty if novelty is not None else control.observe(catalog), selected_agents=selected,
        results=results if results is not None else {a:{} for a in selected},plan={"plan":[]},budget_remaining=30)


def test_pending_generates_distinct_testable_mechanisms_and_marks_seed_origin():
    hypotheses=HypothesisAgent(None).generate("Pod/worker Pending FailedScheduling")
    assert len(hypotheses)==3
    assert all(h.source=="deterministic_seed" and h.status=="active" and h.supports_if and h.refutes_if for h in hypotheses)
    assert len({h.description for h in hypotheses})==3
    assert not any(h.supporting_evidence_ids for h in hypotheses)


def test_catalog_ids_are_the_same_records_as_ontology_not_new_summary_evidence():
    session,catalog=saved_catalog(fact())
    model=build_investigation({"id":"run-1","namespace":"shop","cluster_id":"east",
        "result":{"observations":session.observation_history}})
    assert catalog[0]["id"]==model["observations"][0]["id"]==observation_id(session.observation_history[0])
    assert evidence_catalog({"log_agent":{"summary":"model says configuration wrong"}},[])==[]


@pytest.mark.parametrize("extra",[{"error":"timeout"},{"collection_errors":["metrics unavailable"]},
                                  {"success":False},{"outcome_unknown":True}])
def test_failed_or_partial_collection_is_not_eligible_for_positive_stopping(extra):
    session,catalog=saved_catalog(fact())
    payload={"event_agent":{"evidence_items":[fact()],**extra}}
    assert evidence_catalog(payload,session.observation_history)==[]


@pytest.mark.parametrize("raw",[{"previous":True},{"stale":True}])
def test_historical_state_does_not_satisfy_current_hypothesis(raw):
    _,catalog=saved_catalog(fact(**raw))
    assert catalog==[]


def test_catalog_balances_agents_and_preserves_real_scope_filter():
    session=RCASession()
    many=[fact(str(i)) for i in range(30)]
    session.record_evidence("metric_agent",{"evidence_items":many})
    session.record_evidence("event_agent",{"evidence_items":[fact("scheduler refuses target")]})
    rows=evidence_catalog(session.evidence,session.observation_history,limit=4)
    assert rows[1]["agent"]=="event_agent"
    # An upstream scope filter cannot introduce an identity absent from history.
    scoped={"event_agent":{"evidence_items":[fact("not actually collected",uid="other-pod")]}}
    assert evidence_catalog(scoped,session.observation_history)==[]


def test_timestamp_and_resource_version_churn_is_not_new_semantic_information():
    original=fact()
    other=copy.deepcopy(original)
    other.update(timestamp="2026-10-20T02:00:00Z",collected_at=999,collection_attempt=2)
    other["raw_ref"].update(observed_at="2026-10-20T02:00:00Z",resource_version="101")
    assert semantic_fingerprint(original)==semantic_fingerprint(other)
    assert semantic_fingerprint(original)!=semantic_fingerprint(fact("Running"))
    assert semantic_fingerprint(original)!=semantic_fingerprint(fact(uid="replacement"))


def test_repeated_facts_are_not_recounted_as_new_when_their_record_ids_change():
    control=controller()
    assert control.observe([{"id":"obs-old","semantic_fingerprint":"same"}])["novel_observation_ids"]==["obs-old"]
    observed=control.observe([{"id":"obs-new","semantic_fingerprint":"same"}])
    assert observed["novel_observation_ids"]==[] and observed["repeated_observation_ids"]==["obs-new"]


class Model:
    def __init__(self,response):
        self.response=response
    def json_chat(self,*args,**kwargs):
        return self.response


def test_generation_owns_unique_ids_and_bounds_malformed_model_candidates():
    items=[None,"malformed",{"description":False}]+[
        {"id":None if i%2 else {"bad":"id"},"description":f"checkout 机制 {i}",
         "confidence":float("nan"),"investigation_plan":"not an array"} for i in range(9)]
    result=HypothesisAgent(Model({"hypotheses":items})).generate("Investigate checkout errors")
    assert [h.id for h in result]==["H1","H2","H3","H4","H5"]
    assert all(h.confidence==.5 and h.investigation_plan==[] and h.source=="llm" for h in result)


def update_hypothesis(update, *, new_branches=None):
    _,catalog=saved_catalog(fact())
    value=copy.deepcopy(update)
    for field in ("supporting_evidence_ids","contradicting_evidence_ids"):
        value[field]=[catalog[0]["id"] if x=="observed" else x for x in value.get(field,[])]
    agent=HypothesisAgent(Model({"hypotheses":[value],"new_hypotheses":new_branches or []}))
    agent.set_evidence_catalog(catalog)
    result=agent.rerank([Hypothesis("H1","Pod 调度约束待验证",.6)],{})
    return result[0],agent,catalog


@pytest.mark.parametrize("refs",[[],["invented-reference"]])
def test_uncited_or_forged_updates_cannot_confirm_or_raise_confidence(refs):
    h,_,_=update_hypothesis({"id":"H1","confidence":.99,"status":"confirmed","supporting_evidence_ids":refs})
    assert h.confidence==.6 and h.status=="active" and h.validation_errors


def test_real_reference_can_change_candidate_but_is_not_independent_confirmation():
    h,_,catalog=update_hypothesis({"id":"H1","confidence":.9,"status":"confirmed","supporting_evidence_ids":["observed"]})
    assert h.confidence==.9 and h.supporting_evidence_ids==[catalog[0]["id"]]
    record=decide(controller(),[h.to_dict()],catalog=catalog)
    assert record["decision"]["reason"]=="cited_hypothesis_ready_for_independent_review"
    assert record["decision"]["independent_review_required"] and record["decision"]["execution_authorized"] is False


def test_same_fact_on_both_sides_is_a_validation_gap_not_confirmation():
    h,_,_=update_hypothesis({"id":"H1","confidence":.99,"status":"confirmed",
        "supporting_evidence_ids":["observed"],"contradicting_evidence_ids":["observed"]})
    assert h.confidence==.6 and "same_observation_both_supports_and_contradicts" in h.validation_errors


@pytest.mark.parametrize("score",[float("nan"),float("inf"),True,-1,2,None,"0.99"])
def test_invalid_confidence_does_not_create_a_confirmed_candidate(score):
    h,_,_=update_hypothesis({"id":"H1","confidence":score,"status":"confirmed","supporting_evidence_ids":["observed"]})
    assert h.confidence==.6 and h.status=="active" and "invalid_confidence_value" in h.validation_errors


def test_duplicate_updates_do_not_let_last_high_confidence_claim_win():
    _,catalog=saved_catalog(fact())
    agent=HypothesisAgent(Model({"hypotheses":[{"id":"H1","confidence":.1},{"id":"H1","confidence":.99,
        "status":"confirmed","supporting_evidence_ids":[catalog[0]["id"]]}]}))
    agent.set_evidence_catalog(catalog)
    h=agent.rerank([Hypothesis("H1","待验证机制",.6)],{})[0]
    assert h.confidence==.6 and h.validation_errors==["duplicate_hypothesis_update"]


def test_new_branch_requires_a_real_observed_object_and_reference():
    _,catalog=saved_catalog(fact())
    new={"description":"节点选择器不匹配导致当前 Pod 无法调度","service":"worker","confidence":.9,
         "supporting_evidence_ids":[catalog[0]["id"]]}
    agent=HypothesisAgent(Model({"hypotheses":[],"new_hypotheses":[new]})); agent.set_evidence_catalog(catalog)
    result=agent.rerank([Hypothesis("H1","资源不足待验证",.6)],{})
    assert len(result)==2 and result[0].source=="evidence_grounded_branch"
    assert agent.branch_decisions[0]["accepted"] is True
    new["service"]="unobserved-peer"
    result=agent.rerank([Hypothesis("H1","资源不足待验证",.6)],{})
    assert len(result)==1 and "branch_object_not_observed" in agent.branch_decisions[0]["reasons"]


def test_high_prior_without_resolved_evidence_cannot_stop_investigation():
    record=decide(controller(),[assessed(supporting_evidence_ids=[])])
    assert record["decision"]["action"]=="continue" and record["decision"]["next_agents"]==["log_agent"]


def test_counterevidence_blocks_early_stopping_and_requests_other_source():
    record=decide(controller(),[assessed(contradicting_evidence_ids=["obs-other"])])
    assert record["decision"]["action"]=="continue"


def test_failures_are_not_counterevidence_and_retry_is_bounded():
    control=controller(agents=("event_agent",))
    first=decide(control,[assessed(supporting_evidence_ids=[])],catalog=[],results={"event_agent":{"error":"timeout"}})
    second=decide(control,[assessed(supporting_evidence_ids=[])],catalog=[],iteration=2,results={"event_agent":{"error":"timeout"}})
    assert first["decision"]["next_agents"]==["event_agent"]
    assert second["decision"]["action"]=="stop" and second["collection_failures"]==["event_agent"]
    assert second["semantics"]["failure_is_counterevidence"] is False


def test_no_new_data_switches_sources_then_stops_instead_of_self_amplifying():
    control=controller()
    first=decide(control,[assessed(supporting_evidence_ids=[])])
    second=decide(control,[assessed(supporting_evidence_ids=[])],iteration=2,selected=("log_agent",))
    assert first["decision"]["reason"]=="switch_information_source"
    assert second["novelty"]["novel_observation_ids"]==[] and second["decision"]["action"]=="stop"


def test_all_rejected_hypotheses_leave_no_top_candidate():
    session=RCASession(hypotheses=[Hypothesis("H1","已排除候选",.99,status="rejected")])
    assert session.top_hypothesis() is None


def test_generation_and_round_records_are_deep_snapshots():
    control=controller()
    hypotheses=[assessed()]
    generation=control.record_generation(hypotheses)
    record=decide(control,hypotheses)
    hypotheses[0]["description"]="later change"
    assert generation["items"][0]["description"]!="later change" and record["after"][0]["description"]!="later change"


def test_legacy_and_partial_history_are_explicit_and_get_does_not_start_diagnosis(monkeypatch):
    import web_app.app as app
    old={"id":"old","status":"completed","result":{"root_cause":"old conclusion"}}
    partial={"id":"partial","status":"failed","events":[{"event":"hypothesis_generation_record","items":[assessed()]}],
        "result":{"diagnostic_strategy":{}}}
    monkeypatch.setitem(app._state,"rca_runs",{"old":old,"partial":partial})
    original=copy.deepcopy([old,partial])
    assert asyncio.run(app.rca_diagnostic_strategy("old"))["recording_status"]=="legacy_or_not_yet_recorded"
    assert asyncio.run(app.rca_diagnostic_strategy("partial"))["recording_status"]=="recorded"
    assert app._build_report(partial)["diagnostic_strategy"]==strategy_view(partial)
    assert [old,partial]==original


def test_skill_assessment_reuses_actual_committed_tool_ids_without_extra_calls():
    from tests.test_skill_owned_diagnosis import Model as OriginalModel, Registry, request, SKILLS, waiting_status
    from orchestrator.skill_agent import SkillAgent
    class EvidenceModel(OriginalModel):
        def json_chat(self,messages,**options):
            output=super().json_chat(messages,**options)
            if "skill_diagnose_stages" in messages[-1]["content"]:
                data=json.loads(messages[-1]["content"])
                assert len(data["hypothesis_evidence_catalog"])==1
                output["hypothesis_assessments"]=[{"id":"H1","confidence":.9,"status":"confirmed",
                    "supporting_evidence_ids":[data["hypothesis_evidence_catalog"][0]["id"]],"contradicting_evidence_ids":[]}]
            return output
    model=EvidenceModel();calls=[]
    def runner(command,namespace,payload):
        calls.append(command)
        return {"ok":True,"stdout":"demo\naudit\nlive-pod-uid\n100\nnode-a\n"+json.dumps(waiting_status())}
    result=SkillAgent(registry=Registry(),skills_dir=SKILLS,llm_client=model,kubectl_runner=runner).run(request()).to_dict()
    strategy=result["diagnostic_strategy"]
    assert len(model.calls)==2 and len(calls)==1
    assert strategy["adaptive_skill_replanning_enabled"] is False
    ref=strategy["rounds"][0]["after"][0]["supporting_evidence_ids"][0]
    observed=build_investigation({"id":"skill-run","status":"completed","cluster_id":"default","namespace":"audit","result":result})
    assert ref in {o["id"] for o in observed["observations"]}


@pytest.mark.parametrize("reject_all", [False, True])
def test_engine_actually_changes_sources_and_keeps_earlier_observations(monkeypatch, tmp_path, reject_all):
    import orchestrator.rca_engine as engine
    from configs.config_loader import AppConfig
    from tools.base_tool import ToolRegistry
    from tools.llm_client import LLMClient

    cfg=AppConfig()
    cfg.llm.api_key="offline-regression-placeholder"
    cfg.memory.enabled=cfg.memory.auto_learn=cfg.evolution.enabled=False
    cfg.pipeline.enable_collaboration_optimization=cfg.pipeline.enable_correlation=False
    cfg.pipeline.enable_graph_rca=cfg.pipeline.enable_recovery=False
    cfg.pipeline.max_evidence_iterations=3
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("configs.config_loader.get_config",lambda:cfg)
    plans,actual,catalogues=[],[],[]

    def generate(self,*args,**kwargs):
        self.last_generation_method="test_fixture"
        return [Hypothesis("H1","checkout 错误由依赖连接失败引起",.6,source="test_fixture"),
                Hypothesis("H2","checkout 错误由应用配置不匹配引起",.5,source="test_fixture")]

    def plan(self,hypotheses,incident,iteration):
        plans.append((copy.deepcopy(hypotheses),incident))
        # The model repeats the same choice; the host must really switch it.
        return {"plan":[{"agent":"event_agent","target_hypothesis":"H1"}]}

    def collector(name):
        class Collector:
            def __init__(self,*args):
                pass
            async def analyze(self,*args,**kwargs):
                actual.append(name)
                return {"summary":"observed checkout error","evidence_items":[{
                    "source":name,"signal_type":"application_error","observation":name+" checkout error",
                    "raw_ref":{"kind":"Service","name":"checkout","namespace":"shop","uid":"svc-1"}}]}
        return Collector

    def chat(self,messages,**options):
        options["_receipt"]["completion_status"]="complete"
        user=messages[-1]["content"]
        if '"current_hypotheses"' in user:
            data=json.loads(user)
            catalogue=data["observations"]
            assert "supporting_evidence_ids" in data["output_schema"]["hypotheses"][0]
            catalogues.append(catalogue)
            refs=[c["id"] for c in catalogue]
            return json.dumps({"hypotheses":[{"id":i,"confidence":.6,"status":"rejected" if reject_all else "active",
                "supporting_evidence_ids":[] if reject_all else refs,
                "contradicting_evidence_ids":refs if reject_all else [],
                "unresolved_questions":["需要另一来源验证机制"],"next_agents":["log_agent"]} for i in ["H1","H2"]]})
        return json.dumps({"root_cause":"shop/checkout 错误机制待核验","confidence":.6,
            "fault_type":"unknown","reasoning_chain":"实际观测不足以排除竞争机制"})

    monkeypatch.setattr(engine.HypothesisAgent,"generate",generate)
    monkeypatch.setattr(engine.PlanningAgent,"generate_plan",plan)
    monkeypatch.setattr(LLMClient,"chat",chat)
    for cls,name in [("MetricAgent","metric_agent"),("LogAgent","log_agent"),
                     ("TraceAgent","trace_agent"),("EventAgent","event_agent"),("LLMInferenceAgent","llm_inference_agent")]:
        monkeypatch.setattr(engine,cls,collector(name))
    result=asyncio.run(engine.run_rca("Investigate checkout errors","shop",config=cfg,registry=ToolRegistry()))
    assert result["status"]=="completed"
    rounds=result["diagnostic_strategy"]["rounds"]
    assert len(rounds)==3 and len(catalogues)==3
    assert rounds[0]["selected_agents"]==["event_agent"]
    assert set(rounds[1]["selected_agents"])=={"metric_agent","log_agent"}
    assert rounds[2]["selected_agents"]==["trace_agent"]
    assert actual[:4]==[a for r in rounds for a in r["selected_agents"]]
    # The pre-existing independent closure may make a separate final check.
    assert set(actual)=={"event_agent","metric_agent","log_agent","trace_agent"}
    # Evidence from the first source remains citable when another source arrives.
    assert {c["id"] for c in catalogues[0]} <= {c["id"] for c in catalogues[1]}
    assert rounds[-1]["decision"]["reason"]=="iteration_limit_reached"
    assert all(r["decision"]["execution_authorized"] is False for r in rounds)
    assert len(plans[0][0])==2
    if not reject_all:
        assert "resolve_investigation_gaps" in plans[1][1] and "需要另一来源验证机制" in plans[1][1]
    assert all(len(r["selected_agents"])<=2 for r in rounds)
