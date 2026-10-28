"""Synthetic Pod status fixtures; absence and timestamps are not causal labels."""
import copy
import json

import pytest

from diagnostic_strategy import tool_evidence_catalog
from skill_runtime.pod_termination_evidence import observed_terminations
from skill_runtime.service_evidence import END_MARKER, FIELDS, decode_projection, projection_command


AT = '2026-10-28T01:00:00+00:00'


def pod(uid='pod-uid', status=None):
    return {'kind':'Pod','metadata':{'name':'pod-a','namespace':'shop','uid':uid,'resourceVersion':'opaque-1'},
            'status':{'phase':'Running','containerStatuses':status or []}}


def record(resource):
    command=projection_command('Pod','shop','pod-a')
    cells=[]
    for path in FIELDS['Pod']:
        value=resource
        for part in path.split('.'):
            value=value.get(part) if isinstance(value,dict) else None
        cells.append([] if value is None else [value])
    cells.append([END_MARKER])
    data=decode_projection(command,'shop','\n'.join(json.dumps(cell) for cell in cells))
    return {'command':command,'cluster_id':'east','namespace':'shop','executed':True,'success':True,
            'timestamp':AT,'data':json.dumps(data,ensure_ascii=False)}


def extract(rows):
    catalog=tool_evidence_catalog(rows)
    return observed_terminations(rows,cluster_id='east',namespace='shop',allowed_ids=[r['id'] for r in catalog])


def test_previous_oom_is_bounded_to_pod_identity_and_keeps_time_without_causality():
    status=[{'name':'app','ready':True,'restartCount':2,'state':{'running':{'startedAt':AT}},
             'lastState':{'terminated':{'reason':'OOMKilled','exitCode':137,
                 'finishedAt':'2026-10-28T00:59:00Z','message':'private diagnostic message',
                 'containerID':'private-container-id'}}}]
    row=record(pod(status=status))
    assert 'private diagnostic message' not in row['data'] and 'private-container-id' not in row['data']
    evidence=extract([row]); item=evidence['items'][0]
    assert item['reason']=='OOMKilled' and item['exit_code']==137 and item['state']=='previous_state'
    assert item['pod_uid']=='pod-uid' and item['resource_version']=='opaque-1'
    assert item['finished_at']=='2026-10-28T00:59:00Z' and item['time_status']=='before_or_at_collection'
    assert not item['causality_proven'] and not evidence['incident_window_bound']
    assert evidence['catalog_limited_to_resolved_ids']
    assert not evidence['absence_is_refutation'] and not evidence['execution_authorized']


@pytest.mark.parametrize('finished,expected', [(None,'unavailable_or_invalid'),('not-a-time','unavailable_or_invalid'),
    ('2026-10-28T01:01:00Z','after_collection'),('2026-10-28T00:59:00Z','before_or_at_collection')])
def test_current_termination_time_is_explicit_but_not_an_incident_window(finished,expected):
    state={'reason':'OOMKilled','exitCode':137}
    if finished is not None: state['finishedAt']=finished
    row=record(pod(status=[{'name':'app','state':{'terminated':state}}]))
    item=extract([row])['items'][0]
    assert item['state']=='current_state' and item['time_status']==expected
    assert item['finished_at']==(finished if expected!='unavailable_or_invalid' else None)
    assert not item['causality_proven']


def test_uid_replacement_remains_distinct_and_uncited_records_are_excluded():
    status=[{'name':'app','lastState':{'terminated':{'reason':'OOMKilled','exitCode':137,'finishedAt':AT}}}]
    rows=[record(pod(uid='uid-one',status=status)),record(pod(uid='uid-two',status=status))]
    result=extract(rows)
    assert {r['pod_uid'] for r in result['items']}=={'uid-one','uid-two'}
    allowed=[tool_evidence_catalog(rows)[0]['id']]
    filtered=observed_terminations(rows,cluster_id='east',namespace='shop',allowed_ids=allowed)
    assert len(filtered['items'])==1 and filtered['items'][0]['pod_uid']=='uid-one'


@pytest.mark.parametrize('change',[{'success':False},{'executed':False},{'outcome_unknown':True},
    {'namespace':'other'},{'cluster_id':'other'},{'timestamp':'invalid'},
    {'command':'get pod/pod-a -n shop -o json'}])
def test_uncommitted_unscoped_or_untimed_data_cannot_enter_catalog(change):
    row=record(pod(status=[{'name':'app','lastState':{'terminated':{'reason':'OOMKilled','exitCode':137}}}]))
    assert not extract([{**row,**change}])['items']


def test_no_pod_or_no_termination_never_refutes_oom():
    evidence=extract([record(pod(status=[{'name':'app','state':{'running':{}}}]))])
    assert evidence['items']==[] and evidence['absence_is_refutation'] is False


@pytest.mark.parametrize('state',[{'terminated':{'exitCode':True}}, {'terminated':{'reason':['OOMKilled']}},
    {'terminated':{},'running':{}}, {'terminated':'invalid'}])
def test_malformed_termination_fails_closed(state):
    with pytest.raises(ValueError):
        record(pod(status=[{'name':'app','state':state}]))


@pytest.mark.parametrize('status',[{'name':None},{'name':'app','ready':'true'},
    {'name':'app','restartCount':True},{'name':'app','restartCount':-1}])
def test_malformed_container_identity_or_counters_fail_closed(status):
    with pytest.raises(ValueError):
        record(pod(status=[status]))
