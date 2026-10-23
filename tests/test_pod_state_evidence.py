import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from agents.event_agent import EventAgent
from agents.pod_state_evidence import MESSAGE_LIMIT, problem_containers
from agents.diagnosis_grounding import evidence_catalog, claim_context, apply_alert_grounding
from tools.base_tool import ToolResult


def pod(state, *, init=False, namespace='ns', phase='Pending', previous=None):
    return {'metadata': {'name':'pod-a','namespace':namespace,'uid':'uid-a','resourceVersion':'42'},
        'status': {'phase':phase, 'initContainerStatuses' if init else 'containerStatuses':[
            {'name':'bootstrap' if init else 'app','state':state,'restartCount':2,
             'lastState':previous or {}}]}}


def evidence(value):
    return EventAgent(None,None)._build_current_state_evidence_items({},problem_containers([value],'ns'))


@pytest.mark.parametrize('reason,message', [
    ('CreateContainerConfigError','configmap "required-config" not found'),
    ('InvalidImageName','Failed to apply default image tag: invalid reference format'),
    ('ErrImagePull','registry returned 401 Unauthorized'),
    ('ImagePullBackOff','Back-off pulling image "example/image:v1"'),
])
@pytest.mark.parametrize('init', [False,True])
def test_live_status_messages_reach_literal_model_catalog(reason,message,init):
    value=pod({'waiting':{'reason':reason,'message':message}},init=init)
    before=copy.deepcopy(value)
    rows=evidence(value)
    assert len(rows)==1 and rows[0]['signal_type']=='pod_waiting_state'
    ref=rows[0]['raw_ref']
    assert ref['pod_uid']=='uid-a' and ref['resource_version']=='42'
    assert ref['container_kind']==('init' if init else 'application') and not ref['previous']
    assert message in rows[0]['observation']
    catalog=json.loads(claim_context({'event_agent':{'evidence_items':rows}}))
    assert message in catalog['observations'][0]['observation']
    assert value==before


def test_failed_init_exit_and_successful_init_have_different_evidence():
    failed=pod({'terminated':{'exitCode':37,'reason':'Error','message':'bootstrap rejected configuration'}},init=True)
    rows=evidence(failed)
    assert rows[0]['signal_type']=='container_terminated'
    assert 'init container/bootstrap' in rows[0]['observation']
    assert 'exit code 37' in rows[0]['observation'] and 'rejected configuration' in rows[0]['observation']
    healthy=pod({'terminated':{'exitCode':0,'reason':'Completed'}},init=True,phase='Running')
    assert evidence(healthy)==[]


def test_historical_oom_is_not_current_mechanism_even_after_high_model_review():
    rows=evidence(pod({'running':{'startedAt':'2026-10-16T00:01:00Z'}},phase='Running',
        previous={'terminated':{'exitCode':137,'reason':'OOMKilled','finishedAt':'2026-10-16T00:00:00Z'}}))
    assert len(rows)==1 and rows[0]['signal_type']=='container_previous_termination'
    assert 'historical observation, not current failure' in rows[0]['observation']
    aid='a-'+'1'*24
    data={'agent_skill_context':{'payload':{'source':'alert_evidence_dossier','namespace':'ns',
        'entity':'pod-a','alert_count':1,'alert_samples':[{'alert_id':aid}]}},'event_agent':{'evidence_items':rows}}
    ref=next(k for k in evidence_catalog(data) if k.startswith('ev-'))
    claim={'id':'C1','kind':'mechanism','statement':'Current OOM causes this incident',
        'supports':[{'evidence_id':ref,'quote':rows[0]['observation']}]}
    report={'confidence':.99,'alert_citations':[aid],'root_entity':{'name':'pod-a','namespace':'ns'},
        'diagnosis_claims':[claim],'root_cause_claim_ids':['C1']}
    judge={'needs_review':False,'llm_based':{'claim_reviews':[{'claim_id':'C1','statement':claim['statement'],'status':'supported'}]}}
    final,quality=apply_alert_grounding(report,judge,data)
    assert quality['needs_review'] and final['root_entity'] is None


def test_current_and_previous_failure_stay_separate():
    rows=evidence(pod({'waiting':{'reason':'CrashLoopBackOff','message':'back-off restarting failed container'}},
        previous={'terminated':{'exitCode':137,'reason':'OOMKilled'}}))
    assert [r['signal_type'] for r in rows]==['pod_waiting_state','container_previous_termination']
    assert 'OOMKilled' not in rows[0]['observation']


def test_message_excerpt_is_explicit_and_full_snapshot_is_retained():
    message='x'*(MESSAGE_LIMIT+100)+'tail-detail'
    row=evidence(pod({'waiting':{'reason':'CreateContainerError','message':message}}))[0]
    assert row['raw_ref']['message']==message and row['raw_ref']['message_truncated']
    assert 'remaining text omitted' in row['observation'] and 'tail-detail' not in row['observation']


def test_cross_namespace_status_cannot_enter_requested_scope():
    assert evidence(pod({'waiting':{'reason':'ErrImagePull'}},namespace='other'))==[]


@pytest.mark.parametrize('summarize',[True,False])
def test_unavailable_or_unrequested_summary_preserves_collected_message(monkeypatch,summarize):
    value=pod({'waiting':{'reason':'CreateContainerConfigError','message':'configmap "cfg" not found'}})
    class FailedSummary:
        calls=0
        async def async_chat(self,*a,**k):
            self.calls+=1
            raise TimeoutError()
    tool=SimpleNamespace(execute=lambda **kw:ToolResult(success=True,data={'items':[value]}))
    agent=EventAgent(FailedSummary(),SimpleNamespace(get=lambda name:tool))
    agent.summarize=summarize
    monkeypatch.setattr(agent,'_fetch_workload_state',lambda *a:{})
    monkeypatch.setattr(agent,'_fetch_events',lambda *a,**k:('',''))
    monkeypatch.setattr(agent,'_fetch_node_status',lambda:'')
    monkeypatch.setattr(agent,'_fetch_network_objects',lambda *a:{})
    monkeypatch.setattr(agent,'_describe_resource',lambda *a:'')
    result=asyncio.run(agent.analyze('Investigate pod-a','ns'))
    assert result['summary_error']==('TimeoutError' if summarize else '')
    assert result['summary_status']==('unavailable' if summarize else 'not_requested')
    assert agent.llm.calls==int(summarize)
    assert 'configmap "cfg" not found' in result['evidence_items'][0]['observation']
