"""Unresolved discriminators must not be hidden by proposer confidence."""
import copy
import json
import os
from pathlib import Path

import pytest

from diagnostic_strategy import InvestigationController, apply_investigation_readiness_gate


def candidate(id='H1', **changes):
    return {'id': id, 'description': '依赖服务连接失败机制待验证', 'confidence': .95,
            'status': 'confirmed', 'supporting_evidence_ids': ['obs-1'],
            'contradicting_evidence_ids': [], 'validation_errors': [],
            'unresolved_questions': [], 'next_agents': [], **changes}


def decide(hypotheses, *, iteration=1, budget=60, limit=3, agents=None):
    control = InvestigationController(agents if agents is not None else ('event_agent', 'log_agent', 'trace_agent'),
        max_iterations=limit, confidence_threshold=.85)
    catalog = [{'id': 'obs-1', 'agent': 'event_agent', 'semantic_fingerprint': 'current-error'}]
    return control.decide(iteration=iteration, before=[], after=hypotheses,
        catalog=catalog, novelty=control.observe(catalog), selected_agents=['event_agent'],
        results={'event_agent': {}}, plan={'plan': []}, budget_remaining=budget)


def test_confirmed_candidate_with_open_question_still_collects_requested_source():
    record = decide([candidate(unresolved_questions=['需要目标应用日志区分超时与拒绝连接'],
                               next_agents=['log_agent'])])
    assert record['decision']['action'] == 'continue'
    assert 'log_agent' in record['decision']['next_agents']


def test_two_supported_competing_mechanisms_are_not_resolved_by_model_score_alone():
    record = decide([candidate(next_agents=['trace_agent']),
                     candidate('H2', description='应用配置导致连接失败机制待验证',
                               confidence=.92, status='active', next_agents=['log_agent'])])
    assert record['decision']['action'] == 'continue'
    assert record['decision']['reason'] != 'cited_hypothesis_ready_for_independent_review'


def test_fully_cited_resolved_leader_can_still_proceed_to_review():
    record = decide([candidate()])
    assert record['decision']['reason'] == 'cited_hypothesis_ready_for_independent_review'
    assert record['readiness']['ready_for_independent_review'] is True
    assert record['decision']['execution_authorized'] is False


@pytest.mark.parametrize('changes', [
    {'confidence': .8}, {'status': 'rejected'}, {'supporting_evidence_ids': []},
    {'supporting_evidence_ids': ['invented']}, {'contradicting_evidence_ids': ['obs-1']},
    {'validation_errors': ['unresolved_reference']}, {'id': 'H1'},
])
def test_weak_rejected_uncited_or_invalid_alternative_does_not_block_early_review(changes):
    alternative=candidate('H2',confidence=.92)
    alternative.update(changes)
    record = decide([candidate(),alternative])
    assert record['readiness']['close_alternative_ids'] == []
    assert record['decision']['reason'] == 'cited_hypothesis_ready_for_independent_review'


@pytest.mark.parametrize('margin,blocked', [(.099, True), (.1, True), (.101, False)])
def test_score_margin_is_a_bounded_explicit_heuristic(margin, blocked):
    record = decide([candidate(), candidate('H2', confidence=.95-margin)])
    assert bool(record['readiness']['close_alternative_ids']) is blocked
    assert record['readiness']['alternative_score_margin'] == .1
    assert record['readiness']['score_comparison_is_heuristic'] is True


@pytest.mark.parametrize('kwargs,reason', [({'budget': 0}, 'model_budget_exhausted'),
    ({'iteration': 3}, 'iteration_limit_reached'),
    ({'agents': ['event_agent']}, 'unresolved_gaps_no_eligible_sources')])
def test_unresolved_questions_do_not_override_budgets_or_spin_without_sources(kwargs, reason):
    record = decide([candidate(unresolved_questions=['机制尚需判别'])], **kwargs)
    assert record['decision']['action'] == 'stop'
    assert record['decision']['reason'] == reason
    assert record['readiness']['ready_for_independent_review'] is False


def test_empty_question_strings_cannot_manufacture_an_open_question():
    record = decide([candidate(unresolved_questions=[' ', '', None, False])])
    assert record['readiness']['open_questions'] == []
    assert record['readiness']['ready_for_independent_review'] is True


def test_failed_requested_source_stays_a_gap_after_retry_allowance_is_exhausted():
    control=InvestigationController(('log_agent',),max_iterations=4,confidence_threshold=.85)
    catalog=[{'id':'obs-1','agent':'event_agent','semantic_fingerprint':'current-error'}]
    for iteration in (1,2):
        record=control.decide(iteration=iteration,before=[],
            after=[candidate(next_agents=['log_agent'])],catalog=catalog,
            novelty=control.observe(catalog),selected_agents=['log_agent'],
            results={'log_agent':{'error':True}},plan={},budget_remaining=60)
        assert 'requested_source_retry_pending' in record['readiness']['blockers']
    assert record['readiness']['requested_source_retries'] == []
    assert record['readiness']['requested_failed_sources'] == ['log_agent']
    assert record['decision']['reason'] == 'unresolved_gaps_no_eligible_sources'
    assert record['source_states']['log_agent']['attempts'] == 2


def test_readiness_and_planner_feedback_are_detached_snapshots_with_gaps_first():
    control=InvestigationController(('event_agent','log_agent'),max_iterations=3,confidence_threshold=.85)
    catalog=[{'id':'obs-1','agent':'event_agent','semantic_fingerprint':'current-error'}]
    hypotheses=[candidate(unresolved_questions=['需要日志判别'],next_agents=['log_agent'])]
    result=control.decide(iteration=1,before=[],after=hypotheses,catalog=catalog,
        novelty=control.observe(catalog),selected_agents=['event_agent'],results={'event_agent':{}},plan={},budget_remaining=60)
    feedback=control.feedback()
    assert list(feedback)[0]=='investigation_gaps'
    assert '需要日志判别' in json.dumps(feedback,ensure_ascii=False)[:1000]
    feedback['investigation_gaps']['open_questions'].clear()
    result['readiness']['open_questions'].clear()
    hypotheses[0]['unresolved_questions'].clear()
    assert control.rounds[0]['readiness']['open_questions']==['需要日志判别']


def test_long_questions_do_not_hide_prior_collection_failures_from_planner():
    control=InvestigationController(('event_agent','log_agent'),max_iterations=3,confidence_threshold=.85)
    catalog=[{'id':'obs-1','agent':'event_agent','semantic_fingerprint':'current-error'}]
    record=control.decide(iteration=1,before=[],after=[candidate(
        unresolved_questions=['需要日志判别'+('x'*300)]*6,next_agents=['log_agent'])],
        catalog=catalog,novelty=control.observe(catalog),selected_agents=['log_agent'],
        results={'log_agent':{'error':True}},plan={},budget_remaining=60)
    feedback=json.dumps(control.feedback(),ensure_ascii=False)[:1000]
    assert 'unresolved_collection_failures' in feedback and 'log_agent' in feedback
    assert '需要日志判别' in feedback
    assert len(record['readiness']['open_questions'])==6


def test_remaining_gap_blocks_a_later_passing_judge_without_mutating_originals():
    record=decide([candidate(unresolved_questions=['缺少区分依据'])],iteration=3)
    report={'root_cause':'observed errors','confidence':.99}
    judge={'needs_review':False,'combined_score':.95,'review_blockers':[]}
    before=copy.deepcopy((report,judge))
    final,quality=apply_investigation_readiness_gate(report,judge,[record])
    assert (report,judge)==before
    assert quality['needs_review'] and 'unresolved_investigation_gaps' in quality['review_blockers']
    assert final['confidence']==.65 and final['investigation_gaps']['resolution_recorded'] is False


def test_resolved_or_legacy_round_does_not_invent_a_gap():
    for rounds in ([],[{'decision':{'action':'stop'}}],[decide([candidate()])]):
        report,judge={'confidence':.9},{'needs_review':False}
        final,quality=apply_investigation_readiness_gate(report,judge,rounds)
        assert final is report and quality is judge


@pytest.mark.parametrize('field', ['unresolved_questions', 'next_agents'])
def test_missing_model_field_does_not_clear_previous_investigation_gap(field):
    from agents.hypothesis_agent import Hypothesis,HypothesisAgent
    hypothesis=Hypothesis('H1','连接失败机制待验证',.6,
        unresolved_questions=['需要日志区分机制'],next_agents=['log_agent'])
    agent=HypothesisAgent(None)
    agent.set_evidence_catalog([{'id':'obs-1'}])
    update={'id':'H1','confidence':.95,'status':'confirmed',
        'supporting_evidence_ids':['obs-1'],'contradicting_evidence_ids':[],
        'unresolved_questions':[],'next_agents':[]}
    del update[field]
    result=agent._grounded_updates([hypothesis],{'hypotheses':[update]},False,5)
    assert getattr(result[0],field)==(['需要日志区分机制'] if field=='unresolved_questions' else ['log_agent'])


@pytest.mark.parametrize('changes', [
    {'unresolved_questions':None},{'unresolved_questions':[None]},
    {'next_agents':None},{'next_agents':[False]},
    {'supporting_evidence_ids':['invented']},{'confidence':float('nan')},
    {'status':'invalid'},
])
def test_invalid_model_update_cannot_clear_open_questions_or_pending_sources(changes):
    from agents.hypothesis_agent import Hypothesis,HypothesisAgent
    hypothesis=Hypothesis('H1','连接失败机制待验证',.6,
        unresolved_questions=['需要日志区分机制'],next_agents=['log_agent'])
    agent=HypothesisAgent(None)
    agent.set_evidence_catalog([{'id':'obs-1'}])
    update={'id':'H1','confidence':.95,'status':'confirmed',
        'supporting_evidence_ids':['obs-1'],'contradicting_evidence_ids':[],
        'unresolved_questions':[],'next_agents':[],**changes}
    result=agent._grounded_updates([hypothesis],{'hypotheses':[update]},False,5)
    assert result[0].unresolved_questions==['需要日志区分机制']
    assert result[0].next_agents==['log_agent']
    assert result[0].validation_errors


def test_valid_assessment_can_explicitly_clear_previous_gap():
    from agents.hypothesis_agent import Hypothesis,HypothesisAgent
    hypothesis=Hypothesis('H1','连接失败机制待验证',.6,
        unresolved_questions=['需要日志区分机制'],next_agents=['log_agent'])
    agent=HypothesisAgent(None)
    agent.set_evidence_catalog([{'id':'obs-1'}])
    result=agent._grounded_updates([hypothesis],{'hypotheses':[{
        'id':'H1','confidence':.95,'status':'confirmed',
        'supporting_evidence_ids':['obs-1'],'contradicting_evidence_ids':[],
        'unresolved_questions':[],'next_agents':[]}]},False,5)
    assert not result[0].unresolved_questions and not result[0].next_agents
    assert not result[0].validation_errors and result[0].status=='confirmed'


@pytest.mark.parametrize('resolved', [True, False])
def test_real_engine_collects_discriminator_and_keeps_unresolved_gate(monkeypatch,tmp_path,resolved):
    import asyncio
    from types import SimpleNamespace
    import orchestrator.rca_engine as engine
    from agents.hypothesis_agent import Hypothesis
    from configs.config_loader import AppConfig
    from tools.base_tool import ToolRegistry
    from tools.llm_client import LLMClient
    cfg=AppConfig()
    cfg.llm.api_key='offline-regression-placeholder'
    cfg.memory.enabled=cfg.memory.auto_learn=True
    cfg.memory.db_path=str(tmp_path/'memory')
    cfg.kubernetes.cluster_id='controlled-cluster'
    cfg.evolution.enabled=False
    cfg.pipeline.enable_collaboration_optimization=cfg.pipeline.enable_correlation=False
    cfg.pipeline.enable_graph_rca=False
    cfg.pipeline.enable_recovery=True
    cfg.pipeline.max_evidence_iterations=2
    cfg.remediation.confidence_threshold=.1
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('configs.config_loader.get_config',lambda:cfg)
    actual,plans,learning,recovery=[],[],[],[]
    def generate(self,*args,**kwargs):
        self.last_generation_method='controlled_readiness_fixture'
        return [Hypothesis('H1','checkout 依赖连接失败机制待核验',.6),
                Hypothesis('H2','checkout 配置不匹配机制待核验',.5)]
    def plan(self,hypotheses,incident,iteration):
        plans.append(incident)
        return {'plan':[{'agent':'event_agent'}]}
    def collector(name):
        class Collector:
            def __init__(self,*args):
                pass
            async def analyze(self,*args,**kwargs):
                actual.append(name)
                return {'summary':'controlled observation','evidence_items':[{
                    'source':name,'signal_type':'application_error','observation':name+' checkout error',
                    'raw_ref':{'kind':'Service','name':'checkout','namespace':'shop','uid':'controlled-service'}}]}
        return Collector
    def chat(self,messages,**options):
        options['_receipt']['completion_status']='complete'
        if '"current_hypotheses"' in messages[-1]['content']:
            data=json.loads(messages[-1]['content'])
            has_log=any(item['agent']=='log_agent' for item in data['observations'])
            cleared=has_log and resolved
            refs=[row['id'] for row in data['observations']]
            return json.dumps({'hypotheses':[
                {'id':'H1','status':'confirmed','confidence':.97,'supporting_evidence_ids':refs,
                 'contradicting_evidence_ids':[],'unresolved_questions':[] if cleared else ['需要日志区分两种机制'],
                 'next_agents':[] if cleared else ['log_agent']},
                {'id':'H2','status':'rejected' if cleared else 'active','confidence':.0 if cleared else .94,
                 'supporting_evidence_ids':[] if cleared else refs,'contradicting_evidence_ids':refs if cleared else [],
                 'unresolved_questions':[],'next_agents':[] if cleared else ['trace_agent']}]})
        return json.dumps({'root_cause':'controlled fixture: checkout 机制待核验','confidence':.98,
            'fault_type':'application','reasoning_chain':'controlled model, not cluster truth'})
    class Store:
        supports_memory_scope=True
        def __init__(self,*args):
            pass
        def get_historical_context(self,*args):
            return {'rules':[],'faults':[]}
        def query_similar_rules(self,*args,**kwargs):
            return []
        def query_similar_faults(self,*args,**kwargs):
            return []
        def resolution_stats(self,**kwargs):
            return {}
    class Learner:
        def __init__(self,*args):
            pass
        def learn_from_trace(self,**kwargs):
            learning.append('rules');return {'rules_added':0}
        def store_fault_context(self,*args,**kwargs):
            learning.append('fault')
    class Recovery:
        def __init__(self,*args):
            pass
        async def remediate(self,*args):
            recovery.append(True);return {'status':'controlled_recommendation_only'}
    def passing_judge(self,**kwargs):
        return {'needs_review':False,'combined_score':.99,'judge_level':0,'review_blockers':[],
                'hypothesis_review':{'blockers':[],'items':[],'all_candidates_reviewed':True}}
    monkeypatch.setattr(engine.HypothesisAgent,'generate',generate)
    monkeypatch.setattr(engine.PlanningAgent,'generate_plan',plan)
    monkeypatch.setattr(LLMClient,'chat',chat)
    monkeypatch.setattr(engine,'FaultContextStore',Store)
    monkeypatch.setattr(engine,'ContextLearner',Learner)
    monkeypatch.setattr(engine,'RemediationAgent',Recovery)
    monkeypatch.setattr(engine.RCAJudge,'judge',passing_judge)
    monkeypatch.setattr(engine,'calibrate_rca_confidence',lambda report,**kwargs:report)
    monkeypatch.setattr(engine.DiagnosisClosureManager,'build_plan',lambda self,**kwargs:SimpleNamespace(should_iterate=False))
    for cls,name in [('EventAgent','event_agent'),('MetricAgent','metric_agent'),('LogAgent','log_agent'),
                     ('TraceAgent','trace_agent'),('LLMInferenceAgent','llm_inference_agent')]:
        monkeypatch.setattr(engine,cls,collector(name))
    result=asyncio.run(engine.run_rca('Investigate checkout errors','shop',config=cfg,registry=ToolRegistry()))
    assert result['status']=='completed',result.get('error')
    rounds=result['diagnostic_strategy']['rounds']
    assert len(rounds)==2
    assert rounds[0]['decision']['reason']=='resolve_investigation_gaps'
    assert rounds[0]['readiness']['close_alternative_ids']==['H2']
    assert rounds[1]['selected_agents']==['log_agent','trace_agent']
    assert actual==['event_agent','log_agent','trace_agent']
    assert '需要日志区分两种机制' in plans[1]
    final=result['result']
    if resolved:
        assert rounds[-1]['decision']['reason']=='cited_hypothesis_ready_for_independent_review'
        assert final['automation_gate']['allowed'] is True
        assert learning==['rules','fault'] and recovery==[True]
        assert result['human_review'] is None
    else:
        assert rounds[-1]['decision']['reason']=='iteration_limit_reached'
        assert final['diagnosis_status']=='requires_review'
        assert final['automation_gate']['allowed'] is False
        assert 'unresolved_investigation_gaps' in final['automation_gate']['blockers']
        assert result['judge']['needs_review'] and final['confidence']<=.65
        assert not learning and not recovery and result['human_review']
    artifact=os.environ.get('AGENTICSRE_READINESS_TEST_ARTIFACT_DIR')
    if artifact:
        Path(artifact).mkdir(parents=True,exist_ok=True)
        Path(artifact,'resolved.json' if resolved else 'unresolved.json').write_text(json.dumps({
            'scenario_type':'controlled_regression_fixture','not_cluster_truth':True,'resolved':resolved,
            'strategy':result['diagnostic_strategy'],'actual_agents':actual,'planner_inputs':plans,
            'final':final,'judge_needs_review':result['judge']['needs_review'],
            'learning_calls':learning,'recovery_calls':recovery},ensure_ascii=False,indent=2))
