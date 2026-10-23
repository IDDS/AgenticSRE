import copy
import pytest
from agents.alert_compression import compress_alerts, CompressionStore
from agents.alert_semantics import build_scag
from agents.alert_root_candidates import build_root_candidates
from test_alert_propagation import fixture


def case():
    observed, _, _, _, _ = fixture()
    rows = [dict(name=pod, pod=pod, cluster_id='c', namespace='ns', source='http_probe',
                 timestamp=990+i+offset, metric_name='request_failed', unit='boolean', metric_value=value)
            for i,value in enumerate((0,0,1,0,1,1,0,1,0,0,1,0))
            for pod,offset in (('dependency',0),('caller',.05))]
    result=compress_alerts(rows)
    result.update(origin='live_scan',collection={'prometheus':'ok','kubectl':'ok'},collection_started_at=1002,
                  context_evidence=observed['context_evidence'])
    for e in result['context_evidence']['traces']['edges']:e['provenance']='live_jaeger'
    groups={g['entity']:g for g in result['groups']}
    result['candidate_pairs']=[dict(left=groups['caller']['group_id'],right=groups['dependency']['group_id'])]
    build_scag(result,as_of=1002)
    return result


def test_observed_direction_generates_candidates_without_model_truth():
    result=case()
    report=result['root_candidates']
    assert report['status']=='candidates_ready'
    assert len(report['candidates'])==1
    candidate=report['candidates'][0]
    assert candidate['entity_ref']=='c|ns|pod|dependency'
    assert candidate['paths']==[['c|ns|pod|dependency','c|ns|pod|caller']]
    assert candidate['confidence'] is None and not candidate['causal_confirmed']
    assert len(candidate['next_checks'])==3
    assert report['confidence_basis']=='not_calibrated' and not report['automatic_action']
    assert len(result['groups'])==2 and len(result['raw_alerts'])==24


@pytest.mark.parametrize('mode',['import','edited','partial','expired','future','statistics','trace','truncated','conflict','stale_alerts'])
def test_incomplete_or_conflicting_evidence_abstains(mode):
    result=case()
    if mode=='import':result['origin']='import'
    if mode=='edited':result['history']=[{}]
    if mode=='partial':result['collection']['kubectl']='failed'
    if mode=='expired':result['collection_started_at']=1
    if mode=='future':result['collection_started_at']=1003
    if mode=='statistics':
        for row in result['raw_alerts']:row['raw'].pop('metric_value',None)
    if mode=='trace':
        for e in result['context_evidence']['traces']['edges']:e['provenance']='client_supplied'
    if mode=='truncated':result['context_evidence']['traces']['truncated']=True
    if mode=='conflict':result['scag']['edges'][0]['type']='conflicts'
    if mode=='stale_alerts':
        for g in result['groups']:g['first_seen']-=1000
    report=build_root_candidates(result,1002)
    assert not report['candidates'] and report['status'] in ('abstained','expired')


def test_edit_invalidates_and_dossier_rechecks_expiry(tmp_path):
    result=case();store=CompressionStore(tmp_path);saved=store.create(result)
    key=saved['groups'][0]['group_id']
    # Reopening an old stored result must not revive old root recommendations.
    assert store.dossier(saved['scan_id'],key,1)['root_candidates']['status']=='expired'
    updated=store.edit(saved['scan_id'],revision=1,action='merge',
        group_ids=[g['group_id'] for g in saved['groups']],reason='reviewed')
    assert updated['root_candidates']['status']=='invalidated_by_edit'
    assert not updated['root_candidates']['candidates']
    assert updated['raw_alerts']==result['raw_alerts']


def test_candidate_evidence_is_not_modified_by_repeat_evaluation():
    result=case();before=copy.deepcopy(result)
    build_root_candidates(result,1002)
    assert result==before


def test_hypothesis_prompt_keeps_candidate_before_large_dossier(monkeypatch):
    from agents.alert_root_candidates import diagnosis_handoff
    from agents.hypothesis_agent import HypothesisAgent
    result=case()
    payload=dict(source='alert_evidence_dossier',raw_details='x'*9000,root_candidates=result['root_candidates'])
    handoff=diagnosis_handoff(payload,1002)
    calls=[]
    class Model:
        def json_chat(self,messages,**kwargs):
            calls.append(messages)
            return {'hypotheses':[{'id':'H1','description':'待验证','confidence':.3}]}
    HypothesisAgent(Model()).generate(handoff+'x'*9000)
    assert 'c|ns|pod|dependency' in calls[0][1]['content']
    assert '不是根因结论' in calls[0][1]['content']
    assert diagnosis_handoff(payload,2000)==''
    payload['source']='client_supplied'
    assert diagnosis_handoff(payload,1002)==''


@pytest.mark.parametrize('unique_count',[2,21])
def test_duplicate_trace_responses_do_not_consume_unique_trace_limit(unique_count):
    from agents.alert_context import trace_evidence
    _,_,_,payload,inventory=fixture()
    first=payload['data'][0]
    traces=[copy.deepcopy(first) for _ in range(25)]
    for i in range(1,unique_count):
        trace=copy.deepcopy(first);trace['traceID']=f't-{i}'
        trace['spans'][1]['references'][0]['traceID']=trace['traceID']
        traces.append(trace)
    mapped=trace_evidence({'data':traces},'c',inventory,'live_jaeger')
    assert len(mapped['edges'])==min(unique_count,20)
    assert mapped['truncated']==(unique_count>20)


@pytest.mark.parametrize('value',[None,[],{'status':'candidates_ready','valid_until':1302,'candidates':None},
    {'status':'candidates_ready','valid_until':1302,'candidates':[None,{},'bad']}])
def test_malformed_optional_handoff_does_not_break_diagnosis(value):
    from agents.alert_root_candidates import diagnosis_handoff
    assert diagnosis_handoff(dict(source='alert_evidence_dossier',root_candidates=value),1002)==''


@pytest.mark.parametrize('reverse',[False,True])
def test_collection_order_is_not_fault_propagation_order(reverse):
    original=case()
    records=[dict(name='Error',source='prometheus',pod=pod,namespace='ns',cluster_id='c',
                  timestamp=1002+offset,state='firing')
             for pod,offset in (('dependency',.01 if reverse else 0),('caller',0 if reverse else .01))]
    result=compress_alerts(records)
    result.update(origin='live_scan',collection={'prometheus':'ok','kubectl':'ok'},collection_started_at=1003,
                  context_evidence=original['context_evidence'])
    metrics={}
    for g in result['groups']:
        old=next(o for o in original['groups'] if o['entity']==g['entity'])
        metrics[g['group_id']]=dict(**{k:g[k] for k in ('cluster_id','namespace','entity_kind','entity')},
            status='ready',provenance='live_prometheus_rule',evidence_id='m-'+g['entity'],
            points=[[original['raw_alerts'][i]['timestamp'],original['raw_alerts'][i]['raw']['metric_value']] for i in old['alert_indices']])
    result['metric_evidence']={'groups':metrics}
    result['candidate_pairs']=[dict(left=result['groups'][0]['group_id'],right=result['groups'][1]['group_id'])]
    build_scag(result,as_of=1003)
    assert result['root_candidates']['candidates'][0]['entity_ref']=='c|ns|pod|dependency'
    assert 'same_trace_completion_order' in result['root_candidates']['assessments'][0]['time_basis']
