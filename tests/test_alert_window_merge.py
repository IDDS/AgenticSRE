import copy
import json

import pytest

from agents.alert_compression import compress_alerts, CompressionStore
from agents.alert_model_evidence import metric_context
from agents.alert_thresholds import merge_nested_thresholds
from agents.alert_window_merge import collect_window_proofs, merge_window_observations


def case():
    records=[]
    for i in range(4):
        records.append(dict(name=f'Window{i}',source='prometheus',timestamp=1000,cluster_id='c',
            labels=dict(namespace='ns',pod='p',alertname=f'Window{i}',alertstate='firing'),
            severity='critical' if i==2 else 'warning'))
    result=compress_alerts(records,time_window=30)
    result.update(origin='live_scan',cluster_id='c',collection_started_at=1000,
                  collection={'prometheus':'ok','kubectl':'ok'})
    entries={}
    for i,g in enumerate(result['groups']):
        field='mean_duration_ms' if i<3 else 'error_ratio'
        value=350 if i<3 else 1
        metric='duration' if i<3 else 'errors'
        threshold=(100,200,300,.5)[i]
        entries[g['group_id']]=dict(**{k:g[k] for k in ('cluster_id','namespace','entity_kind','entity','alert_ids')},
            status='ready',provenance='live_prometheus_rule',evidence_id=f'm-{i}',query=metric,
            query_basis='simple_comparison_operand',rule={'name':g['representative'],'expression':f'{metric} > {threshold}'},
            points=[[t,value] for t in range(981,1001)],series_labels=dict(namespace='ns',pod='p'),
            request_window=dict(field=field,seconds='10',count_metric='window_count',end_metric='window_end',operation='request'))
    result['metric_evidence']=dict(provenance='live_prometheus_rule',groups=entries,ready_groups=4)
    assert merge_nested_thresholds(result)
    inventory={'pods':[dict(namespace='ns',name='p',uid='uid')]}
    traces=[]
    for i,end in enumerate((995,997,998)):
        traces.append(dict(traceID=f'{i+1:032x}',processes={'p':{'serviceName':'p','tags':[{'key':'k8s.pod.uid','value':'uid'}]}},
            spans=[dict(spanID=f'{i+1:016x}',processID='p',operationName='request',startTime=int((end-.35)*1e6),duration=350000,
                        tags=[{'key':'http.status_code','value':503},{'key':'span.kind','value':'server'}])]))
    fixture=dict(traces=traces,count=3,stop=999,service='p',mean=350,ratio=1,queries=[])
    def fetch(backend,path,params):
        if backend=='jaeger':
            assert params['service']==fixture['service'] and params['operation']=='request' and params['limit']==201
            assert json.loads(params['tags']) == {'k8s.pod.uid': 'uid'}
            return {'data':fixture['traces']}
        fixture['queries'].append(params['query'])
        assert params['query'].count('label_replace(') in (4, 8)
        values = dict(mean_duration_ms=fixture['mean'],error_ratio=fixture['ratio'],
                      count_metric=fixture['count'],end_metric=fixture['stop'])
        if params['query'].count('label_replace(') == 8:
            values.update({'source_time_'+field:fixture.get('sample_times', {}).get(field, 999) for field in list(values)})
        series = [dict(metric={'namespace':'ns','pod':'p','__agenticsre_window_field':field},
                       value=[params['time'],str(value)]) for field,value in values.items()]
        if fixture.get('bad_snapshot') == 'missing': series.pop()
        if fixture.get('bad_snapshot') == 'duplicate': series[-1] = copy.deepcopy(series[0])
        if fixture.get('bad_snapshot') == 'foreign': series[-1]['metric']['pod'] = 'foreign'
        return {'status':'success','data':{'resultType':'vector','result':series}}
    return result,inventory,fixture,fetch


def test_joint_window_reconstruction_merge_and_dossier(tmp_path):
    result,inventory,fixture,fetch=case()
    original=copy.deepcopy(result['raw_alerts'])
    evidence=collect_window_proofs(result,'','',inventory,fetch)
    assert len(evidence['proofs'])==1 and not evidence['rejected']
    assert evidence['proofs'][0]['request_count']==3
    assert merge_window_observations(result,evidence)
    assert len(result['groups'])==1 and result['raw_alerts']==original
    group=result['groups'][0]
    assert group['severity']=='critical' and group['alert_count']==4
    assert not group['event_merge']['causal_confirmed']
    assert metric_context(result,group)['status']=='composite'
    assert result['signature_version']==5
    assert not merge_window_observations(result,evidence)
    store=CompressionStore(tmp_path);saved=store.create(result)
    dossier=store.dossier(saved['scan_id'],group['group_id'],1)
    assert dossier['event_merge']['window']['request_count']==3
    assert len(dossier['metric_evidence']['measurements'])==2
    result['window_evidence']=evidence
    saved=store.create(result)
    edited=store.edit(saved['scan_id'],revision=1,action='split',group_ids=[group['group_id']],
                      alert_ids=[group['alert_ids'][0]],reason='reviewed independent observation')
    assert edited['window_evidence']=={'status':'invalidated_by_edit','proofs':[]}
    assert edited['window_compression']['status']=='invalidated_by_edit'


@pytest.mark.parametrize('mode', ['import','edited','partial','missing_trace','incomplete_count','duplicate_trace',
    'truncated','different_uid','disjoint_requests','duration_mismatch','ratio_mismatch','stale_window',
    'stale_anchor','different_dimensions','bad_companion','wrong_members','unsupported_threshold','mixed_slow_cohort',
    'unmapped_partition','different_service','non_server_span'])
def test_window_proof_rejects_missing_or_conflicting_evidence(mode):
    result,inventory,fixture,fetch=case()
    entries=list(result['metric_evidence']['groups'].values())
    delay=next(m for m in entries if m['request_window']['field']=='mean_duration_ms')
    error=next(m for m in entries if m['request_window']['field']=='error_ratio')
    if mode=='import':result['origin']='import'
    elif mode=='edited':result['history']=[{'action':'merge'}]
    elif mode=='partial':result['collection']['kubectl']='failed'
    elif mode=='missing_trace':fixture['traces']=[]
    elif mode=='incomplete_count':fixture['count']=4
    elif mode=='duplicate_trace':fixture['traces'].append(copy.deepcopy(fixture['traces'][0]));fixture['count']=4
    elif mode=='truncated':fixture['traces']*=67
    elif mode=='different_uid':inventory['pods'][0]['uid']='replacement'
    elif mode=='disjoint_requests':fixture['traces'][0]['spans'][0]['tags'][0]['value']=200
    elif mode=='duration_mismatch':fixture['mean']=351
    elif mode=='ratio_mismatch':fixture['ratio']=.9
    elif mode=='stale_window':fixture['stop']=990
    elif mode=='stale_anchor':
        for entry in entries:entry['points'][-1][0]=990
    elif mode=='different_dimensions':error['series_labels']['route']='other'
    elif mode=='bad_companion':
        for entry in entries:entry['request_window']['count_metric']='bad{unsafe}'
    elif mode=='wrong_members':error['alert_ids']=['foreign']
    elif mode=='unsupported_threshold':delay['rule_references'][0]['rule']['expression']='duration < 100'
    elif mode=='mixed_slow_cohort':fixture['traces'][0]['spans'][0]['duration']=1000
    elif mode=='unmapped_partition':
        for entry in entries:entry['series_labels']['route']='checkout'
    elif mode=='different_service':fixture['traces'][0]['processes']['p']['serviceName']='other'
    elif mode=='non_server_span':fixture['traces'][0]['spans'][0]['tags'][1]['value']='client'
    original=copy.deepcopy(result)
    evidence=collect_window_proofs(result,'','',inventory,fetch)
    assert not evidence['proofs']
    assert not merge_window_observations(result,evidence)
    assert result==original
    if mode == 'stale_window':
        assert evidence['rejection_details'][0]['window_age_seconds'] == 10
        assert evidence['rejection_details'][0]['request_count'] == 3


@pytest.mark.parametrize('mode', ['valid', 'foreign_pod', 'missing_uid', 'ambiguous_pod',
                                  'invalid_service', 'mismatched_service_contract'])
def test_shared_trace_service_preserves_pod_boundaries(mode):
    result, inventory, fixture, fetch = case()
    fixture['service'] = 'checkout'
    entries = list(result['metric_evidence']['groups'].values())
    for entry in entries:
        entry['request_window']['service'] = 'checkout'
    for trace in fixture['traces']:
        trace['processes']['p']['serviceName'] = 'checkout'
    foreign = copy.deepcopy(fixture['traces'][0])
    foreign['traceID'] = 'f' * 32
    foreign['processes']['p']['tags'][0]['value'] = 'other-uid'
    inventory['pods'].append(dict(namespace='ns', name='other', uid='other-uid'))
    # Even if the backend ignores UID search tags, foreign requests cannot fill coverage.
    fixture['traces'].append(foreign)
    if mode == 'foreign_pod':
        fixture['traces'].pop(0)
    elif mode == 'missing_uid':
        inventory['pods'][0].pop('uid')
    elif mode == 'ambiguous_pod':
        inventory['pods'].append(dict(namespace='ns', name='p', uid='replacement'))
    elif mode == 'invalid_service':
        for entry in entries: entry['request_window']['service'] = ''
    elif mode == 'mismatched_service_contract':
        entries[0]['request_window']['service'] = 'different'
    evidence = collect_window_proofs(result, '', '', inventory, fetch)
    if mode == 'valid':
        assert len(evidence['proofs']) == 1
        proof = evidence['proofs'][0]
        assert proof['trace_service'] == 'checkout' and proof['pod_uid'] == 'uid'
        assert len(proof['spans']) == 3 and all(s['pod_uid'] == 'uid' for s in proof['spans'])
        assert merge_window_observations(result, evidence)
        assert len(result['groups']) == 1
    else:
        assert not evidence['proofs']
        assert not merge_window_observations(result, evidence)


def test_atomic_window_snapshot_does_not_mix_scrape_generations():
    result, inventory, fixture, fetch = case()
    # An earlier range query can see a previous scrape at the same evaluation
    # time; the atomic proof must use its own four-value snapshot, not mix them.
    delay = next(m for m in result['metric_evidence']['groups'].values()
                 if m['request_window']['field'] == 'mean_duration_ms')
    delay['points'][-1][1] = 349
    before = copy.deepcopy(result)
    evidence = collect_window_proofs(result, '', '', inventory, fetch)
    assert len(evidence['proofs']) == 1 and len(fixture['queries']) == 1
    snapshot = evidence['proofs'][0]['measurement_snapshot']
    assert snapshot['values']['mean_duration_ms'] == 350
    assert result == before  # Historical range samples are never rewritten.
    assert merge_window_observations(result, evidence)
    assert result['groups'][0]['event_merge']['window']['measurement_snapshot'] == snapshot


@pytest.mark.parametrize('mode', ['missing','duplicate','foreign'])
def test_atomic_window_snapshot_requires_all_four_scoped_values(mode):
    result, inventory, fixture, fetch = case()
    fixture['bad_snapshot'] = mode
    evidence = collect_window_proofs(result, '', '', inventory, fetch)
    assert not evidence['proofs'] and evidence['rejected']


@pytest.mark.parametrize('mode,reason', [
    ('mixed', 'window samples are not synchronized'),
    ('stale', 'window source samples are stale'),
    ('future', 'window source sample is in the future'),
])
def test_window_requires_fresh_synchronized_source_samples(mode, reason):
    result, inventory, fixture, fetch = case()
    fields = ('mean_duration_ms','error_ratio','count_metric','end_metric')
    fixture['sample_times'] = dict.fromkeys(fields, 999)
    if mode == 'mixed': fixture['sample_times']['mean_duration_ms'] = 998.5
    if mode == 'stale': fixture['sample_times'] = dict.fromkeys(fields, 997.99)
    if mode == 'future': fixture['sample_times'] = dict.fromkeys(fields, 1000.01)
    evidence = collect_window_proofs(result, '', '', inventory, fetch)
    assert not evidence['proofs']
    assert evidence['rejected'] == {reason: 1}
    assert evidence['rejection_details'][0]['source_timestamps'] == fixture['sample_times']


@pytest.mark.parametrize('stop,count,reason', [
    (997.99, 3, 'request window is stale'),
    (999, 1, 'insufficient requests for window reconstruction'),
    (999, 2.5, 'window request count is not an integer'),
    (999, 201, 'window request count exceeds coverage limit'),
])
def test_window_rejection_identifies_exact_time_or_count_boundary(stop, count, reason):
    result, inventory, fixture, fetch = case()
    fixture.update(stop=stop, count=count)
    evidence = collect_window_proofs(result, '', '', inventory, fetch)
    assert not evidence['proofs'] and evidence['rejected'] == {reason: 1}
    detail = evidence['rejection_details'][0]
    assert detail['window_end'] == stop and detail['request_count'] == count
    assert detail['allowed_window_end'] == [998, 1000]
    assert detail['allowed_request_count'] == [2, 200]


def test_window_proof_retains_actual_source_timestamps():
    result, inventory, fixture, fetch = case()
    evidence = collect_window_proofs(result, '', '', inventory, fetch)
    snapshot = evidence['proofs'][0]['measurement_snapshot']
    assert snapshot['source_timestamps'] == dict.fromkeys(snapshot['values'], 999)
    assert len(fixture['queries']) == 1
    assert fixture['queries'][0].count('timestamp(') == 4


@pytest.mark.parametrize('outcome', ['complete', 'stale', 'still_future'])
def test_inflight_scrape_selects_only_one_fresh_previous_snapshot(outcome):
    result, inventory, fixture, fetch = case()
    fields = ('mean_duration_ms','error_ratio','count_metric','end_metric')
    times = []
    def timed_fetch(backend, path, params):
        if backend == 'prometheus':
            times.append(params['time'])
            if len(times) == 1:
                fixture.update(stop=1000.01, sample_times=dict.fromkeys(fields, 999.99))
            else:
                fixture.update(stop=999.02, sample_times=dict.fromkeys(fields, 999))
                if outcome == 'stale':
                    # Fresh relative to the retry but stale relative to the scan.
                    fixture['sample_times'] = dict.fromkeys(fields, 997.995)
                if outcome == 'still_future': fixture['stop'] = 1000.02
        return fetch(backend, path, params)
    evidence = collect_window_proofs(result, '', '', inventory, timed_fetch)
    assert times == [1000, 999.989]
    if outcome == 'complete':
        assert len(evidence['proofs']) == 1
        proof = evidence['proofs'][0]
        snapshot = proof['measurement_snapshot']
        assert snapshot['selection'] == 'previous_completed_scrape'
        assert snapshot['excluded_inflight_snapshot']['values']['end_metric'] == 1000.01
        assert proof['end'] <= proof['evaluation_at'] <= snapshot['initial_evaluation_at']
        assert min(snapshot['source_timestamps'].values()) >= snapshot['initial_evaluation_at']-2
    else:
        reason = 'window source samples are stale' if outcome == 'stale' else 'request window ends in the future'
        assert not evidence['proofs'] and evidence['rejected'] == {reason: 1}
