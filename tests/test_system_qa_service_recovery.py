"""Regressions from remote, truth-free system acceptance on 2026-10-15."""
import json
from pathlib import Path

import pytest
from skill_runtime.executor import SkillExecutor
from web_app import app as dashboard

SKILLS = Path(__file__).resolve().parents[1] / 'skill_runtime' / 'skills'

def execute(**changes):
    observation={'command':'get endpoints api -n qa','sample':'NAME ENDPOINTS AGE\napi <none> 2m',
                 'namespace':'qa','cluster_id':'default','executed':True,'success':True}
    observation.update(changes)
    return SkillExecutor.from_dir(SKILLS).run('k8s.service_unavailable',
        {'namespace':'qa','cluster_id':'default','service':'api','deployment':'api',
         'message':'业务探针无法连接服务',
         'agent_evidence':{'agent_evidence_collection':{'commands':[observation]}}},
        stages=['detect','diagnose']).to_dict()

def test_service_skill_accepts_scoped_current_evidence_without_fault_label():
    run=execute()
    assert run['stages']['detect']['status']=='matched'
    diagnosis=run['stages']['diagnose']['output']
    assert diagnosis['fault_type']=='ServiceUnavailable'
    assert 'endpoints' in diagnosis['root_cause']
    assert any('endpoints' in e for e in run['stages']['diagnose']['evidence'])

@pytest.mark.parametrize('change',[
    {'sample':'NAME ENDPOINTS AGE\napi 10.0.0.1:80 2m'},
    {'sample':'NAME ENDPOINTS AGE\nother <none> 2m'},
    {'namespace':'other'}, {'cluster_id':'other'}, {'executed':False}, {'success':False},
    {'command':'get endpoints other -n qa'},
    {'command':'get events -n qa'},
])
def test_unrelated_healthy_or_failed_observation_does_not_confirm_service_fault(change):
    run=execute(**change)
    assert run['stages']['detect']['status']=='skipped'
    assert run['stages']['diagnose']['status']=='skipped'

def test_current_json_endpoint_evidence_is_supported():
    run=execute(command='get endpoints/api -o json',
        sample=json.dumps({'metadata':{'name':'api','namespace':'qa'},'subsets':[]}))
    assert run['stages']['detect']['status']=='matched'

@pytest.mark.parametrize('fault_type,expected',[
    ('service_disruption','ServiceUnavailable'),('service_unavailable','ServiceUnavailable'),
    ('image_pull_error','ImagePullError'),
])
def test_explicit_diagnosis_category_is_not_overridden_by_incidental_cpu_evidence(fault_type,expected):
    result=dashboard._build_heal_suggestions({'namespace':'qa','service':'api','deployment':'api',
        'diagnosis':{'fault_type':fault_type,'root_cause':'目标服务无就绪后端','confidence':0.9,
                     'evidence_summary':{'metrics':'node_cpu usage is normal'}}})
    assert result['fault_type']==expected
    assert all('set resources' not in s['command'] for s in result['suggestions'])

@pytest.mark.parametrize('result,expected',[
    ({'duration_s':119.09,'result':{'result':{'root_cause':'observed'}}},119.1),
    ({'duration_s':0,'root_cause':'observed'},0),
    ({'result':{'duration_s':22.36,'root_cause':'observed'}},22.4),
])
def test_completed_report_uses_stored_duration_at_any_read_time(monkeypatch,result,expected):
    run={'id':'qa','status':'completed','started_at':100,'result':result}
    for now in [1000,2000]:
        monkeypatch.setattr(dashboard.time,'time',lambda:now)
        assert dashboard._build_report(run)['basic_info']['耗时(秒)']==expected

def test_completed_report_without_duration_never_uses_current_time(monkeypatch):
    monkeypatch.setattr(dashboard.time,'time',lambda:1000)
    run={'id':'qa','status':'completed','started_at':100,'result':{}}
    assert dashboard._build_report(run)['basic_info']['耗时(秒)']=='-'
    run['finished_at']=130
    assert dashboard._build_report(run)['basic_info']['耗时(秒)']==30

def test_running_report_can_show_elapsed_time(monkeypatch):
    monkeypatch.setattr(dashboard.time,'time',lambda:130)
    assert dashboard._build_report({'status':'running','started_at':100})['basic_info']['耗时(秒)']==30
