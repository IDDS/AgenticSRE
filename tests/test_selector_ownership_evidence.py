"""Synthetic UID-chain fixtures; a label mismatch is not causal proof."""
import copy
import json
import time

import pytest

from orchestrator.selector_ownership_evidence import collect_selector_ownership,merge_cited_catalog
from skill_runtime.service_evidence import END_MARKER,FIELDS,decode_projection,projection_command,projection_request,observed_entity_catalog


ROOT={'cluster_id':'east','namespace':'shop','kind':'Deployment','name':'engine','uid':'dep-uid','resource_version':'dep-rv'}
SCOPE={'status':'collected','candidate_workloads':['engine']}
HYP=[{'id':'H2','status':'active','proposed_status':'rejected','validation_errors':['unverified_rejection'],
      'description':'Service selector 与 Pod 标签不匹配'}]


def obj(kind,name,uid,rv,**fields):
    return {'kind':kind,'projection':'service-evidence/v1',
        'metadata':{'namespace':'shop','name':name,'uid':uid,'resourceVersion':rv},**fields}


def fixture():
    service=obj('Service','gateway','svc-uid','svc-rv',spec={'selector':{'component':'backend'}})
    deployment=obj('Deployment','engine','dep-uid','dep-rv',spec={'selector':{'matchLabels':{'workload':'engine'},'matchExpressions':[]}})
    replica=obj('ReplicaSet','engine-rs','rs-uid','rs-rv')
    replica['metadata']['ownerReferences']=[{'kind':'Deployment','name':'engine','uid':'dep-uid','controller':True}]
    pod=obj('Pod','engine-pod','pod-uid','pod-rv')
    pod['metadata'].update(labels={'workload':'engine','component':'wrong'},ownerReferences=[
        {'kind':'ReplicaSet','name':'engine-rs','uid':'rs-uid','controller':True}])
    initial={'command':projection_command('Service','shop','gateway'),'cluster_id':'east','namespace':'shop',
        'success':True,'executed':True,'timestamp':'2026-10-28T01:00:00Z','data':json.dumps(service)}
    return service,deployment,replica,pod,initial


def invoke(service=None,deployment=None,replica=None,pod=None,initial=None,*,runner=None,**changes):
    values=fixture();service=service or values[0];deployment=deployment or values[1]
    replica=replica or values[2];pod=pod or values[3];initial=initial or values[4]
    calls=[]
    def read(command):
        calls.append(command)
        kind,name=projection_request(command,'shop')
        data={'Service':service,'DeploymentSelector':deployment,'ReplicaSet':replica,
              'Pod':{'kind':'List','projection':'service-evidence/v1','inventory_complete':True,'items':[pod]}}[kind]
        return {'command':command,'success':True,'executed':True,'data':json.dumps(data)}
    kwargs=dict(commands=[initial],hypotheses=HYP,root_entity=ROOT,service_scope=SCOPE,
        service='gateway',cluster_id='east',namespace='shop',run_read=runner or read)
    kwargs.update(changes)
    result,reads=collect_selector_ownership(**kwargs)
    return result,reads,calls


def test_mismatch_requires_exact_pod_replicaset_deployment_uid_chain():
    result,reads,calls=invoke()
    assert result['attempted'] and result['stop_reason']=='selector_observations_collected'
    assert len(reads)==len(calls)==6
    item=result['items'][0]
    assert item['pod_uid']=='pod-uid' and item['replicaset_uid']=='rs-uid' and item['mismatched_keys']==['component']
    assert len(item['observation_ids'])==4 and not item['causality_proven']
    assert not result['absence_is_refutation'] and not result['incident_window_bound'] and not result['execution_authorized']
    assert '-l workload=engine' in calls[2] and all('get pods -n shop --chunk' not in c for c in calls)


def test_matching_label_or_unverified_controller_does_not_invent_a_mismatch():
    service,deployment,replica,pod,initial=fixture()
    pod['metadata']['labels']['component']='backend'
    result,_,_=invoke(service,deployment,replica,pod,initial)
    assert result['items']==[] and result['stop_reason']=='selector_observations_collected'
    pod['metadata']['labels']['component']='wrong'
    replica['metadata']['ownerReferences'][0]['uid']='other-deployment'
    result,_,_=invoke(service,deployment,replica,pod,initial)
    assert result['items']==[] and not result['absence_is_refutation']


def test_no_pods_cannot_refute_a_selector_or_oom_hypothesis():
    service,deployment,replica,pod,initial=fixture()
    def read(command):
        kind,_=projection_request(command,'shop')
        data={'Service':service,'DeploymentSelector':deployment,
              'Pod':{'kind':'List','projection':'service-evidence/v1','inventory_complete':True,'items':[]}}[kind]
        return {'command':command,'success':True,'executed':True,'data':json.dumps(data)}
    result,reads,_=invoke(runner=read)
    assert result['items']==[] and len(reads)==5 and not result['absence_is_refutation']


def test_chinese_selector_question_uses_the_same_bounded_path():
    hypothesis=[{**HYP[0],'description':'服务选择器与后端 Pod 标签不匹配'}]
    result,_,_=invoke(hypotheses=hypothesis)
    assert result['attempted'] and result['items'][0]['mismatched_keys']==['component']


@pytest.mark.parametrize('kind', ['service_uid','deployment_rv','replicaset_uid','complex_selector','external_service','pod_outside_selector'])
def test_changed_identity_or_unsupported_selector_stops_and_keeps_gap(kind):
    service,deployment,replica,pod,initial=fixture()
    if kind=='service_uid': service['metadata']['uid']='replacement'
    if kind=='deployment_rv': deployment['metadata']['resourceVersion']='changed'
    if kind=='replicaset_uid': replica['metadata']['uid']='replacement'
    if kind=='complex_selector': deployment['spec']['selector']['matchExpressions']=[{'key':'tier','operator':'Exists','values':[]}]
    if kind=='external_service': service['spec']['type']='ExternalName'
    if kind=='pod_outside_selector': pod['metadata']['labels']['workload']='other'
    result,_,_=invoke(service,deployment,replica,pod,initial)
    assert result['items']==[] and result['stop_reason']!='selector_observations_collected'
    assert not result['execution_authorized']


@pytest.mark.parametrize('where',['pod','replicaset'])
def test_multiple_controller_claims_never_form_a_verified_owner_chain(where):
    service,deployment,replica,pod,initial=fixture()
    target=pod if where=='pod' else replica
    target['metadata']['ownerReferences'].append({'kind':'Job','name':'other','uid':'other-uid','controller':True})
    result,_,_=invoke(service,deployment,replica,pod,initial)
    assert result['items']==[] and not result['absence_is_refutation']


def test_missing_scope_or_malformed_selector_never_starts_namespace_inventory():
    result,reads,calls=invoke(service_scope={'status':'incomplete','candidate_workloads':['engine']})
    assert not result['attempted'] and not reads and not calls
    service,deployment,replica,pod,initial=fixture()
    deployment['spec']['selector']['matchLabels']={}
    result,_,calls=invoke(service,deployment,replica,pod,initial)
    assert result['stop_reason']=='selector_deployment_unsupported' and len(calls)==2


def test_timeout_has_no_late_result_or_retry():
    calls=[]
    def read(command):
        calls.append(command);time.sleep(.15)
        return {'command':command,'success':True,'executed':True,'data':'{}'}
    result,reads,_=invoke(runner=read,per_read_timeout_s=.01,total_budget_s=.02)
    assert result['stop_reason']=='selector_read_timeout' and not reads and len(calls)==1


def test_deployment_selector_projection_is_fixed_and_legacy_command_remains_recognized():
    command=projection_command('DeploymentSelector','shop','engine')
    assert projection_request(command,'shop')==('DeploymentSelector','engine')
    legacy=projection_command('Deployment','shop','engine')
    assert projection_request(legacy,'shop')==('Deployment','engine')
    row=fixture()[1];cells=[]
    for path in FIELDS['DeploymentSelector']:
        value=row
        for part in path.split('.'): value=value.get(part) if isinstance(value,dict) else None
        cells.append([] if value is None else [value])
    cells.append([END_MARKER])
    decoded=decode_projection(command,'shop','\n'.join(json.dumps(cell) for cell in cells))
    assert decoded['kind']=='Deployment' and decoded['metadata']['uid']=='dep-uid'
    assert decoded['spec']['selector']['matchLabels']=={'workload':'engine'}
    recorded={'command':command,'cluster_id':'east','namespace':'shop','executed':True,'success':True,'data':json.dumps(decoded)}
    assert observed_entity_catalog([recorded],'east','shop')[0]['uid']=='dep-uid'


def test_crowded_inventory_cannot_evict_owner_chain_or_prior_assertion_citations():
    existing=[{'id':f'old-{i}'} for i in range(24)]
    fresh=[{'id':f'new-{i}'} for i in range(4)]
    hypotheses=[{'supporting_evidence_ids':['old-23'],'contradicting_evidence_ids':[]}]
    items=[{'observation_ids':[r['id'] for r in fresh]}]
    rows,complete=merge_cited_catalog(existing,fresh,hypotheses,items)
    assert complete and len(rows)==24 and {'old-23',*(r['id'] for r in fresh)} <= {r['id'] for r in rows}
    rows,complete=merge_cited_catalog(existing,fresh,hypotheses,[{'observation_ids':['invented']}])
    assert not complete and 'old-23' in {r['id'] for r in rows}


@pytest.mark.parametrize('selector', [
    {'matchLabels': []},
    {'matchLabels': {'workload': 'engine'}, 'matchExpressions': 0},
    {'matchLabels': {'workload': 'engine'}, 'matchExpressions': [
        {'key': 'tier', 'operator': 'Exists', 'values': False}]},
])
def test_falsey_malformed_selector_fields_are_not_treated_as_absent(selector):
    row = fixture()[1]
    row['spec']['selector'] = selector
    cells = []
    for path in FIELDS['DeploymentSelector']:
        value = row
        for part in path.split('.'):
            value = value.get(part) if isinstance(value, dict) else None
        cells.append([] if value is None else [value])
    cells.append([END_MARKER])
    with pytest.raises(ValueError):
        decode_projection(projection_command('DeploymentSelector', 'shop', 'engine'), 'shop',
                          '\n'.join(json.dumps(cell) for cell in cells))
