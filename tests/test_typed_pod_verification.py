"""Actual complete Pod declarations, not absence in an event summary, falsify narrow candidates."""
import copy
import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from agents.pod_state_evidence import pod_spec_checks
from agents.event_agent import EventAgent
from agents.hypothesis_agent import HypothesisAgent
from diagnostic_strategy import evidence_catalog, falsification_evidence, hypothesis_review_context, validate_hypothesis_review
from orchestrator.session import RCASession
from orchestrator.diagnosis_closure import bind_report_entity,DiagnosisClosureManager,ClosurePlan
from operational_ontology import build_investigation
from tools.base_tool import ToolResult


def pod():
    return {'metadata':{'name':'target','namespace':'shop','uid':'live-uid','resourceVersion':'11'},
        'spec':{'containers':[{'name':'app','resources':{}}],
                'volumes':[{'name':'api','projected':{'sources':[{'serviceAccountToken':{'path':'token'}}]}}]},
        'status':{'phase':'Pending'}}


def collect(value=None,agent_name='event_agent'):
    value=value if value is not None else pod()
    tool=NS(execute=lambda **kw:ToolResult(success=True,data={'items':[value]}))
    agent=EventAgent(None,NS(get=lambda _:tool))
    facts=agent._build_current_state_evidence_items({},agent._fetch_problem_pods('shop'))
    session=RCASession(namespace='shop');session.record_evidence(agent_name,{'evidence_items':facts})
    return session,evidence_catalog(session.evidence,session.observation_history)


def test_complete_spec_proves_only_two_explicit_narrow_conditions_without_new_tool_reads():
    p=pod();out=pod_spec_checks(p)
    assert out['checks']=={'zero_cpu_memory_requests':True,'no_pvc_or_ephemeral_volume':True}
    assert out['volumes']==[{'name':'api','source_types':['projected']}]
    assert 'not all resource/storage failures' in out['scope']
    calls=[]
    tool=NS(execute=lambda **kw:(calls.append(kw) or ToolResult(success=True,data={'items':[p]})))
    a=EventAgent(None,NS(get=lambda _:tool));a._fetch_problem_pods('shop')
    assert len(calls)==1 and calls[0]['output']=='json'


@pytest.mark.parametrize('path,value,expected',[
    ('containers',[{'name':'a','resources':{'requests':{'cpu':'100m'}}}],False),
    ('initContainers',[{'name':'init','resources':{'requests':{'memory':'1Gi'}}}],False),
    ('ephemeralContainers',[{'name':'debug','resources':{'requests':{'cpu':'1'}}}],False),
    ('resources',{'requests':{'cpu':'1'}},False),('overhead',{'memory':'64Mi'},False),
    ('containers',[{'name':'a','resources':{'limits':{'cpu':'1'}}}],None),
    ('containers',[],None),('containers',None,None),('initContainers',{},None),
    ('overhead',None,None),('resources',None,None),
])
def test_every_request_source_is_checked_without_naive_summing(path,value,expected):
    p=pod();p['spec'][path]=value
    assert pod_spec_checks(p)['checks']['zero_cpu_memory_requests'] is expected


@pytest.mark.parametrize('value,expected',[('0',True),('0m',True),('0Mi',True),('0e1000',True),('.0',True),
    ('1m',False),('100Mi',False),('bad',None),(True,None),(None,None),('NaN',None),('-1',None)])
def test_zero_quantity_is_bounded_and_does_not_guess_on_invalid_values(value,expected):
    p=pod();p['spec']['containers'][0]['resources']={'requests':{'cpu':value}}
    assert pod_spec_checks(p)['checks']['zero_cpu_memory_requests'] is expected


@pytest.mark.parametrize('change',[{'resize':'InProgress'},{'containerStatuses':[{'name':'app','allocatedResources':{'cpu':'1'}}]}])
def test_resize_or_allocated_requests_prevent_spec_only_resource_refutation(change):
    p=pod();p['status'].update(change)
    assert pod_spec_checks(p)['checks']['zero_cpu_memory_requests'] is None


@pytest.mark.parametrize('source,expected',[('projected',True),('secret',True),('configMap',True),
    ('downwardAPI',True),('emptyDir',True),('persistentVolumeClaim',False),('ephemeral',False),
    ('csi',None),('hostPath',None),('unknown',None)])
def test_binding_absence_is_not_all_storage_health(source,expected):
    p=pod();p['spec']['volumes']=[{'name':'storage',source:{}}]
    assert pod_spec_checks(p)['checks']['no_pvc_or_ephemeral_volume'] is expected


@pytest.mark.parametrize('value',[None,{},[None],[{'name':'v','projected':{},'persistentVolumeClaim':{}}]])
def test_malformed_volume_declarations_cannot_prove_absence(value):
    p=pod();p['spec']['volumes']=value
    assert pod_spec_checks(p)['checks']['no_pvc_or_ephemeral_volume'] is None


def test_projection_redacts_secret_env_annotations_and_projected_payloads():
    p=pod();sentinel='sensitive-content-sentinel'
    p['metadata']['annotations']={'secret':sentinel}
    p['spec']['containers'][0]['env']=[{'name':'KEY','value':sentinel}]
    p['spec']['volumes'][0]['projected']['sources'].append({'secret':{'name':sentinel}})
    assert sentinel not in json.dumps(pod_spec_checks(p))


@pytest.mark.parametrize('field',['uid','resourceVersion','namespace'])
def test_missing_identity_cannot_certify_spec_checks(field):
    p=pod();p['metadata'].pop(field)
    assert pod_spec_checks(p) is None


def test_typed_check_id_resolves_to_same_ontology_observation():
    session,catalog=collect()
    check=next(c for c in catalog if c['signal_type']=='pod_spec_checks')
    model=build_investigation({'id':'run','namespace':'shop','cluster_id':'east','result':{'observations':session.observation_history}})
    assert check['id'] in {o['id'] for o in model['observations']}
    candidates=HypothesisAgent(None).generate('Pod/target Pending')
    for h in [candidates[0],candidates[2]]:
        assert falsification_evidence(h.to_dict(),catalog,[check['id']])==[check['id']]
        ctx=hypothesis_review_context([{**h.to_dict(),'status':'rejected'}],catalog)
        review=validate_hypothesis_review(ctx,{'hypothesis_reviews':[{'id':h.id,'status':'contradicted',
            'supporting_evidence_ids':[],'contradicting_evidence_ids':[check['id']],
            'reason':'完整 Pod 声明确认没有相关声明；不是摘要未提及。'}]})
        assert review['items'][0]['assertion_verified'] and not review['blockers']


def test_other_event_or_model_only_summary_cannot_falsify_registered_test():
    _,catalog=collect();h=HypothesisAgent(None).generate('Pod/target Pending')[2]
    other=next(c for c in catalog if c['signal_type']=='pod_waiting_state')
    review=validate_hypothesis_review(hypothesis_review_context([{**h.to_dict(),'status':'rejected'}],catalog),
        {'hypothesis_reviews':[{'id':h.id,'status':'contradicted','supporting_evidence_ids':[],
            'contradicting_evidence_ids':[other['id']],'reason':'事件指向选择器，不能确定 PVC。'}]})
    assert review['blockers'] and review['items'][0]['status']=='uncertain'


def test_peer_or_recreated_incarnation_cannot_supply_target_test():
    _,catalog=collect();check=next(c for c in catalog if c['signal_type']=='pod_spec_checks')
    peer=copy.deepcopy(check);peer['entity']['uid']='replacement';peer['id']='obs-peer'
    h=HypothesisAgent(None).generate('Pod/target Pending')[2].to_dict()
    assert not falsification_evidence(h,catalog+[peer],[check['id']])
    h['service']='another-target'
    assert not falsification_evidence(h,catalog,[check['id']])


def test_closure_test_reference_keeps_exact_collector_identity():
    _,catalog=collect(agent_name='event_agent_closure')
    assert any(c.get('verification_checks',{}).get('no_pvc_or_ephemeral_volume') is True for c in catalog)


def test_proposer_cannot_reject_registered_candidate_from_warning_alone():
    _,catalog=collect();h=HypothesisAgent(None).generate('Pod/target Pending')[2]
    other=next(c for c in catalog if c['signal_type']=='pod_waiting_state')
    model=NS(json_chat=lambda *a,**k:{'hypotheses':[{'id':h.id,'confidence':0,'status':'rejected',
        'supporting_evidence_ids':[],'contradicting_evidence_ids':[other['id']]}]})
    agent=HypothesisAgent(model);agent.set_evidence_catalog(catalog)
    after=agent.rerank([h],{})[0]
    assert after.status=='active' and 'falsification_test_not_observed' in after.validation_errors


def test_proposer_cannot_confirm_against_an_observed_falsification_even_with_a_valid_other_citation():
    _,catalog=collect();h=HypothesisAgent(None).generate('Pod/target Pending')[2]
    other=next(c for c in catalog if c['signal_type']=='pod_waiting_state')
    model=NS(json_chat=lambda *a,**k:{'hypotheses':[{'id':h.id,'confidence':.99,'status':'confirmed',
        'supporting_evidence_ids':[other['id']],'contradicting_evidence_ids':[]}]})
    agent=HypothesisAgent(model);agent.set_evidence_catalog(catalog)
    after=agent.rerank([h],{})[0]
    assert after.status=='active' and 'typed_test_refutes_confirmation' in after.validation_errors


def test_reviewer_cannot_ignore_other_direct_observations_when_supporting_candidate():
    _,catalog=collect();h=HypothesisAgent(None).generate('Pod/target Pending')[2].to_dict();h['status']='confirmed'
    other=next(c for c in catalog if c['signal_type']=='pod_waiting_state')
    review=validate_hypothesis_review(hypothesis_review_context([h],catalog),{'hypothesis_reviews':[{
        'id':h['id'],'status':'supported','supporting_evidence_ids':[other['id']],
        'contradicting_evidence_ids':[],'reason':'Pending supports volume waiting'}]})
    item=review['items'][0]
    assert review['blockers'] and item['status']=='uncertain'
    assert item['host_test_evidence_ids'] and 'typed_test_contradicts_support' in item['rationale_limits']


def report():
    return {'root_entity':{'namespace':'shop','kind':'Pod','name':'target'},'root_cause':'target selector mismatch',
            'fault_type':'scheduling_failure','confidence':.8}


def test_observed_identity_enrichment_uses_uid_not_report_prose():
    session,_=collect();original=report();bound=bind_report_entity(original,session.evidence)
    assert bound['root_entity']['uid']=='live-uid' and bound['root_entity']['resource_version']=='11'
    assert 'uid' not in original['root_entity'] and bound['root_entity_validation']['observed']
    assert not bind_report_entity({'root_cause':'Pod/target selector mismatch'},session.evidence)['root_entity_validation']['observed']


def test_recreated_pod_does_not_rebind_old_report_or_survive_failed_followup():
    session,_=collect();old=bind_report_entity(report(),session.evidence)
    p=pod();p['metadata']['uid']='new-uid';p['metadata']['resourceVersion']='12'
    fresh,_=collect(p,agent_name='event_agent_closure')
    evidence={**session.evidence,**fresh.evidence}
    changed=bind_report_entity(old,evidence)
    assert not changed['root_entity_validation']['observed']
    assert changed['root_entity_validation']['status']=='pod_incarnation_changed'
    assert changed['root_entity']['uid']=='live-uid'
    assert not bind_report_entity(old,{**session.evidence,'event_agent_closure':{'error':True}})['root_entity_validation']['observed']


def test_missing_identity_rewrite_is_rejected_whole_not_merged_into_new_cause():
    session,_=collect();original=bind_report_entity(report(),session.evidence)
    model=NS(json_chat=lambda *a,**k:{'root_cause':'peer caused a new failure','fault_type':'network','confidence':.99})
    result=DiagnosisClosureManager(model).revise_report('Pod/target Pending',original,session.evidence,{},ClosurePlan())
    assert result['root_cause']==original['root_cause'] and result['root_entity']['uid']=='live-uid'
    assert result['closure_revision_validation']['original_report_retained'] is True


def test_explicit_identity_abstention_is_not_overwritten_with_original():
    session,_=collect();original=bind_report_entity(report(),session.evidence)
    model=NS(json_chat=lambda *a,**k:{'root_entity':None,'root_cause':'无法确定根因对象','fault_type':'unknown'})
    result=DiagnosisClosureManager(model).revise_report('Pod/target Pending',original,session.evidence,{},ClosurePlan())
    assert result['root_entity'] is None and not result['root_entity_validation']['observed']


@pytest.mark.parametrize('mode',['missing_field','recreated','explicit_null'])
def test_engine_binds_original_before_review_and_checks_every_closure_outcome(monkeypatch,tmp_path,mode):
    import orchestrator.rca_engine as engine
    from configs.config_loader import AppConfig
    from agents.hypothesis_agent import Hypothesis
    from tools.llm_client import LLMClient
    from tools.base_tool import ToolRegistry
    cfg=AppConfig();cfg.llm.api_key='offline-regression-placeholder'
    cfg.memory.enabled=cfg.evolution.enabled=False
    cfg.pipeline.enable_collaboration_optimization=cfg.pipeline.enable_correlation=False
    cfg.pipeline.enable_graph_rca=cfg.pipeline.enable_recovery=False;cfg.pipeline.max_evidence_iterations=1
    monkeypatch.chdir(tmp_path);monkeypatch.setattr('configs.config_loader.get_config',lambda:cfg)
    monkeypatch.setattr(engine.HypothesisAgent,'generate',lambda *a,**k:[Hypothesis('H1','target selector mismatch',.6)])
    monkeypatch.setattr(engine.PlanningAgent,'generate_plan',lambda *a,**k:{'plan':[{'agent':'event_agent'}]})
    monkeypatch.setattr(engine.DiagnosisClosureManager,'build_plan',lambda *a,**k:ClosurePlan(
        should_iterate=True,target_agents=['event_agent'],focus_queries={'event_agent':'Pod/target Pending follow-up'}))
    calls=[]
    class Collector:
        def __init__(self,*args): pass
        async def analyze(self,*args,**kwargs):
            p=pod();calls.append(p)
            p['spec']['nodeSelector']={'tier':'missing'}
            if mode=='recreated' and len(calls)>1:
                p['metadata']['uid']='replacement-uid';p['metadata']['resourceVersion']='12'
            session,_=collect(p)
            return session.evidence['event_agent']
    for cls in ('EventAgent','MetricAgent','LogAgent','TraceAgent','LLMInferenceAgent'):
        monkeypatch.setattr(engine,cls,Collector)
    def chat(self,messages,**options):
        options['_receipt']['completion_status']='complete'
        user=messages[-1]['content'];system=messages[0]['content']
        if '"current_hypotheses"' in user:
            cat=json.loads(user)['observations']
            return json.dumps({'hypotheses':[{'id':'H1','confidence':.95,'status':'confirmed',
                'supporting_evidence_ids':[cat[0]['id']],'contradicting_evidence_ids':[]}]})
        if 'independent RCA evidence reviewer' in system:
            data=json.loads(user[user.index('{"hypothesis_review_request":'):])
            ref=data['hypothesis_review_request']['observations'][0]['id']
            return json.dumps({'judge_level':0,'coherence_score':.9,'evidence_score':.9,'specificity_score':.9,
                'actionability_score':.9,'supported_evidence_ids':['E1'],'unsupported_claims':[],'contradictions':[],
                'hypothesis_reviews':[{'id':'H1','status':'supported','supporting_evidence_ids':[ref],
                    'contradicting_evidence_ids':[],'reason':'fixture validates identity flow only'}]})
        if 'SRE RCA closure agent' in system:
            assert '"root_entity"' in user
            revised={'root_cause':'untrusted replacement conclusion','fault_type':'scheduling_failure','confidence':.99}
            if mode=='explicit_null':revised['root_entity']=None
            return json.dumps(revised)
        return json.dumps(report())
    monkeypatch.setattr(LLMClient,'chat',chat)
    result=asyncio.run(engine.run_rca('Pod/target Pending','shop',config=cfg,registry=ToolRegistry()))
    assert result['status']=='completed' and len(calls)==2
    final=result['result']
    if mode=='missing_field':
        assert final['root_entity']['uid']=='live-uid' and final['root_entity_validation']['observed']
        assert final['root_cause']==report()['root_cause'] and final['automation_gate']['allowed']
        assert final['closure_revision_validation']['original_report_retained']
    else:
        assert final.get('root_entity') is None
        assert not final['automation_gate']['allowed'] and final['diagnosis_status']=='requires_review'
        assert 'root_entity_not_uid_bound' in final['automation_gate']['blockers']
