"""Provider refusal is failed execution, not a completed model trace."""
import time
from types import SimpleNamespace

import httpx
from openai import APIStatusError
import pytest

from orchestrator.rca_engine import _apply_model_completion_gate
from tools.llm_client import DeadlineLLMClient


def model(create):
    value=object.__new__(DeadlineLLMClient)
    value.model,value.temperature,value.max_tokens='fixture',0,100
    value.deadline,value.call_timeout_s,value.timeout_count=time.monotonic()+10,1,0
    value.incomplete_responses=[]
    value.client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return value


def payment_error():
    return APIStatusError('sensitive-provider-body-must-not-escape',
        response=httpx.Response(402,request=httpx.Request('POST','https://provider.invalid/')),
        body={'error':'sensitive-provider-body-must-not-escape'})


def test_provider_402_is_recorded_and_does_not_escape_sensitive_body():
    def create(**kwargs):
        raise payment_error()
    client=model(create)
    with pytest.raises(Exception) as failure:
        client.chat([])
    assert getattr(client,'failed_calls',[])
    assert client.failed_calls[0]['status_code']==402
    assert 'sensitive-provider-body' not in str(failure.value)


def test_later_passing_judge_cannot_hide_provider_call_failure():
    report={'root_cause':'candidate only','confidence':.99}
    judge={'needs_review':False,'combined_score':.95,'review_blockers':[]}
    final,quality,blocked=_apply_model_completion_gate(report,judge,
        SimpleNamespace(incomplete_responses=[],timeout_count=0,
            failed_calls=[{'code':'provider_payment_required','status_code':402}]))
    assert blocked and quality['needs_review']
    assert final['confidence']<=.65


@pytest.mark.parametrize('status', [401,402,403,404])
def test_terminal_provider_refusal_blocks_later_calls_without_spending_budget(status):
    calls=[]
    def create(**kwargs):
        calls.append(True)
        raise APIStatusError('sensitive-body',response=httpx.Response(status,
            request=httpx.Request('POST','https://provider.invalid/')),body={'error':'sensitive-body'})
    client=model(create)
    for _ in range(3):
        with pytest.raises(Exception) as error:
            client.chat([])
        assert 'sensitive-body' not in str(error.value)
    assert len(calls)==1 and client.failed_call_count==1
    assert client.terminal_failure['status_code']==status
    assert client.remaining_s()>0 and client.timeout_count==0


@pytest.mark.parametrize('status', [429,500,503])
def test_retryable_failure_can_recover_without_erasing_failed_execution(status):
    from tests.test_rca_completion_gate import response
    calls=[]
    def create(**kwargs):
        calls.append(True)
        if len(calls)==1:
            raise APIStatusError('sensitive-body',response=httpx.Response(status,
                request=httpx.Request('POST','https://provider.invalid/')),body={})
        return response()
    client=model(create)
    with pytest.raises(Exception):
        client.chat([])
    assert client.chat([])
    assert len(calls)==2 and getattr(client,'terminal_failure',None) is None
    assert len(client.failed_calls)==1
    _,judge,blocked=_apply_model_completion_gate({'confidence':.9},{'needs_review':False},client)
    assert blocked and judge['needs_review']


def test_call_failure_receipts_are_bounded_and_do_not_log_provider_body(caplog):
    def create(**kwargs):
        raise APIStatusError('sensitive-body',response=httpx.Response(500,
            request=httpx.Request('POST','https://provider.invalid/')),body={})
    client=model(create)
    for _ in range(20):
        with pytest.raises(Exception):
            client.chat([])
    assert client.failed_call_count==20 and len(client.failed_calls)==16
    assert 'sensitive-body' not in str(client.failed_calls)+caplog.text


def test_invalid_final_report_also_requires_review_when_transport_completed():
    final,judge,blocked=_apply_model_completion_gate({'confidence':.9},{'needs_review':False},
        SimpleNamespace(incomplete_responses=[],timeout_count=0),
        failed_stages=[{'stage':'final_report','code':'invalid_or_unavailable_report'}])
    assert blocked and judge['needs_review']
    assert final['model_response_validation']['failed_stages'][0]['stage']=='final_report'


def test_unavailable_report_preserves_request_scope_without_claiming_candidate_cause():
    from orchestrator.rca_engine import _unavailable_model_report
    report=_unavailable_model_report('Pod/demo ImagePullBackOff','audit',
        SimpleNamespace(id='H1',description='unverified DNS mechanism',confidence=.99))
    assert report['incident_target']=={'namespace':'audit','kind':'Pod','name':'demo'}
    assert 'audit/Pod/demo' in report['root_cause'] and '未完成模型核验' in report['root_cause']
    assert 'unverified DNS mechanism' not in str(report)
    assert report['root_entity'] is None and report['confidence']==0


def test_model_unavailability_is_not_mislabelled_as_time_budget_exhaustion():
    from diagnostic_strategy import InvestigationController
    control=InvestigationController(('event_agent',),max_iterations=3,confidence_threshold=.85)
    result=control.decide(iteration=1,before=[],after=[],catalog=[],novelty=control.observe([]),
        selected_agents=[],results={},plan={},budget_remaining=50,model_unavailable=True)
    assert result['decision']['reason']=='model_service_unavailable'
    assert result['decision']['action']=='stop'


@pytest.mark.parametrize('failure_mode', ['payment', 'invalid_json', 'empty_cause'])
def test_real_engine_preserves_scope_and_blocks_automation_after_report_failure(monkeypatch,tmp_path,failure_mode):
    import asyncio
    import json
    import os
    from pathlib import Path
    import orchestrator.rca_engine as engine
    from agents.hypothesis_agent import Hypothesis
    from configs.config_loader import AppConfig
    from tools.base_tool import ToolRegistry
    from tools.llm_client import LLMClient
    from tests.test_rca_completion_gate import response
    cfg=AppConfig()
    cfg.llm.api_key='offline-regression-placeholder'
    cfg.memory.enabled=cfg.memory.auto_learn=True
    cfg.memory.db_path=str(tmp_path/'memory')
    cfg.evolution.enabled=False
    cfg.pipeline.enable_collaboration_optimization=cfg.pipeline.enable_correlation=False
    cfg.pipeline.enable_graph_rca=False
    cfg.pipeline.enable_recovery=True
    cfg.pipeline.max_evidence_iterations=1
    cfg.remediation.confidence_threshold=.1
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('configs.config_loader.get_config',lambda:cfg)
    calls,learning,recovery,events=[],[],[],[]
    def create(**kwargs):
        calls.append(True)
        if failure_mode=='payment':
            raise payment_error()
        return response(text='not-json' if failure_mode=='invalid_json' else '{"root_cause":""}')
    class Transport:
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        def with_options(self,**kwargs):
            return self
    def initialize(self,config=None):
        self.model,self.temperature,self.max_tokens='fixture',0,100
        self.client=Transport()
    def generate(self,*args,**kwargs):
        self.last_generation_method='controlled_provider_fixture'
        return [Hypothesis('H1','unverified DNS mechanism',.99)]
    class Collector:
        def __init__(self,*args):
            pass
        async def analyze(self,*args,**kwargs):
            return {'summary':'controlled ImagePullBackOff observation','evidence_items':[{
                'source':'event_agent','signal_type':'ImagePullBackOff','observation':'demo image pull failed',
                'raw_ref':{'kind':'Pod','name':'demo','namespace':'audit','uid':'controlled-pod'}}]}
    class Store:
        def __init__(self,*args):
            pass
        def get_historical_context(self,*args):
            return {'rules':[],'faults':[]}
        def resolution_stats(self,**kwargs):
            return {}
    class Learner:
        def __init__(self,*args):
            pass
        def learn_from_trace(self,**kwargs):
            learning.append(True)
        def store_fault_context(self,*args):
            learning.append(True)
    class Recovery:
        def __init__(self,*args):
            pass
        async def remediate(self,*args):
            recovery.append(True)
    monkeypatch.setattr(LLMClient,'__init__',initialize)
    monkeypatch.setattr(engine.HypothesisAgent,'generate',generate)
    monkeypatch.setattr(engine.HypothesisAgent,'rerank',lambda self,hypotheses,*args:hypotheses)
    monkeypatch.setattr(engine.PlanningAgent,'generate_plan',lambda *args:{'plan':[{'agent':'event_agent'}]})
    for name in ('EventAgent','MetricAgent','LogAgent','TraceAgent','LLMInferenceAgent'):
        monkeypatch.setattr(engine,name,Collector)
    monkeypatch.setattr(engine,'FaultContextStore',Store)
    monkeypatch.setattr(engine,'ContextLearner',Learner)
    monkeypatch.setattr(engine,'RemediationAgent',Recovery)
    monkeypatch.setattr(engine.RCAJudge,'judge',lambda *args,**kwargs:{'needs_review':False,
        'combined_score':.99,'judge_level':0,'review_blockers':[],
        'hypothesis_review':{'blockers':[],'items':[],'all_candidates_reviewed':True}})
    monkeypatch.setattr(engine,'calibrate_rca_confidence',lambda report,**kwargs:report)
    monkeypatch.setattr(engine.DiagnosisClosureManager,'build_plan',lambda *args,**kwargs:SimpleNamespace(should_iterate=False))
    result=asyncio.run(engine.run_rca('Pod/demo ImagePullBackOff','audit',config=cfg,
        registry=ToolRegistry(),log_callback=events.append))
    assert result['status']=='completed',result.get('error')
    final=result['result']
    assert len(calls)==1
    assert final['confidence']==0 and final['root_entity'] is None
    assert final['incident_target']=={'namespace':'audit','kind':'Pod','name':'demo'}
    assert 'unverified DNS mechanism' not in final['root_cause']
    assert '未完成模型核验' in final['root_cause']
    assert final['diagnosis_status']=='requires_review' and result['judge']['needs_review']
    assert final['automation_gate']['allowed'] is False
    assert 'incomplete_model_execution' in final['automation_gate']['blockers']
    assert result['human_review'] and not learning and not recovery
    budget=final['model_budget']
    assert budget['remaining_s']>0 and not budget['exhausted'] and budget['call_timeouts']==0
    assert budget['failed_stages'][0]['stage']=='final_report'
    assert budget['failed_call_count']==(1 if failure_mode=='payment' else 0)
    if failure_mode=='payment':
        assert budget['terminal_failure']['status_code']==402
    assert 'sensitive-provider-body' not in json.dumps([result,events])
    assert next(p for p in result['phases'] if p['phase']==6)['status']=='degraded'
    assert next(e for e in events if isinstance(e,dict) and e.get('event')=='phase_complete' and e['phase']==6)['status']=='degraded'
    artifact=os.environ.get('AGENTICSRE_PROVIDER_TEST_ARTIFACT_DIR')
    if artifact:
        Path(artifact).mkdir(parents=True,exist_ok=True)
        Path(artifact,failure_mode+'.json').write_text(json.dumps({
            'scenario_type':'controlled_regression_fixture','not_cluster_truth':True,
            'failure_mode':failure_mode,'result':result,'events':events,
            'provider_calls':len(calls),'learning_calls':learning,'recovery_calls':recovery},ensure_ascii=False,indent=2))
