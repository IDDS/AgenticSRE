import copy
import pytest
from agents.alert_compression import compress_alerts, CompressionStore
from agents.alert_context import collect_traces, trace_evidence
from agents.alert_thresholds import merge_nested_thresholds
from agents.alert_trace_merge import merge_trace_observations


def case():
    records=[];entries={}
    for i,(field,value,threshold) in enumerate([('duration_ms',350,100),('duration_ms',350,200),('duration_ms',350,300),('http_status_code',503,499)]):
        labels=dict(namespace='ns',pod='p',trace_id='a'*32,span_id='b'*16,alertname=f'A{i}',alertstate='firing')
        records.append(dict(name=f'A{i}',source='prometheus',timestamp=1000,cluster_id='c',labels=labels,
                            severity='critical' if i==2 else 'warning'))
    result=compress_alerts(records,time_window=30)
    result.update(origin='live_scan',cluster_id='c',collection_started_at=1000,collection={'prometheus':'ok','kubectl':'ok'})
    for i,g in enumerate(result['groups']):
        field,value,threshold=[('duration_ms',350,100),('duration_ms',350,200),('duration_ms',350,300),('http_status_code',503,499)][i]
        entries[g['group_id']]=dict(**{k:g[k] for k in ('cluster_id','namespace','entity_kind','entity','alert_ids')},
            status='ready',provenance='live_prometheus_rule',evidence_id=f'm-{i}',query=field,trace_field=field,
            query_basis='simple_comparison_operand',rule={'name':f'A{i}','expression':f'{field} > {threshold}'},
            points=[[t,value] for t in range(981,1001)],series_labels=dict(namespace='ns',pod='p',trace_id='a'*32,span_id='b'*16))
    result['metric_evidence']=dict(provenance='live_prometheus_rule',groups=entries,ready_groups=4)
    result['context_evidence']={'traces':dict(provenance='live_jaeger',truncated=False,observed_spans=[
        dict(trace_id='a'*32,span_id='b'*16,entity_ref='c|ns|pod|p',pod_uid='uid',start=979.65,end=980,
             duration_ms=350,http_status_code=503,error=True,provenance='live_jaeger')])}
    return result


def test_threshold_then_request_merge_preserves_measurements_and_members(tmp_path):
    result=case();raw=copy.deepcopy(result['raw_alerts'])
    assert merge_nested_thresholds(result)
    assert len(result['groups'])==2
    assert merge_trace_observations(result)
    assert len(result['groups'])==1 and result['raw_alerts']==raw
    g=result['groups'][0]
    assert g['severity']=='critical' and g['alert_count']==4
    assert result['signature_version']==4
    assert len(g['event_merge']['original_groups'])==2
    assert not g['event_merge']['causal_confirmed']
    e=result['metric_evidence']['groups'][g['group_id']]
    assert e['status']=='composite' and len(e['measurements'])==2 and 'points' not in e
    from agents.alert_model_evidence import metric_context
    context=metric_context(result,g)
    assert context['status']=='composite' and len(context['measurements'])==2
    assert {m['summary']['maximum'] for m in context['measurements']}=={350,503}
    assert not merge_trace_observations(result)
    store=CompressionStore(tmp_path);saved=store.create(result)
    dossier=store.dossier(saved['scan_id'],g['group_id'],1)
    assert len(dossier['metric_evidence']['measurements'])==2
    assert all('points' not in m for m in dossier['metric_evidence']['measurements'])
    assert dossier['event_merge']['span']['span_id']=='b'*16


def test_composite_context_rejects_rebound_or_duplicate_members():
    from agents.alert_model_evidence import metric_context
    r=case();merge_nested_thresholds(r);merge_trace_observations(r)
    g=r['groups'][0];r['metric_evidence']['groups'][g['group_id']]['measurements'][0]['alert_ids']=['foreign']
    assert metric_context(r,g)['status']=='unknown'


@pytest.mark.parametrize('mode',['import','edited','partial','truncated','foreign_backend','healthy','old_span','future_span',
    'different_span','different_trace','no_binding','wrong_measurement','mixed_values','wrong_pod','wrong_member',
    'different_dimension','resolved','early_sample','duplicate_span','untrusted_metric','missing_series_reference'])
def test_no_cross_metric_merge_without_exact_independent_binding(mode):
    r=case();g=r['groups'][-1];m=r['metric_evidence']['groups'][g['group_id']]
    traces=r['context_evidence']['traces'];span=traces['observed_spans'][0]
    if mode=='import':r['origin']='import'
    elif mode=='edited':r['history']=[{'action':'merge'}]
    elif mode=='partial':r['collection']['kubectl']='failed'
    elif mode=='truncated':traces['truncated']=True
    elif mode=='foreign_backend':traces['provenance']='client_supplied'
    elif mode=='healthy':span['error']=False
    elif mode=='old_span':span['start']=960
    elif mode=='future_span':span['end']=1001
    elif mode=='different_span':r['raw_alerts'][-1]['labels']['span_id']='c'*16
    elif mode=='different_trace':r['raw_alerts'][-1]['labels']['trace_id']='c'*32
    elif mode=='no_binding':m.pop('trace_field')
    elif mode=='wrong_measurement':m['trace_field']='duration_ms'
    elif mode=='mixed_values':m['points'][0][1]=200
    elif mode=='wrong_pod':span['entity_ref']='c|ns|pod|other'
    elif mode=='wrong_member':m['alert_ids']=['foreign']
    elif mode=='different_dimension':r['raw_alerts'][-1]['labels']['device']='other'
    elif mode=='resolved':r['raw_alerts'][-1]['state']='resolved'
    elif mode=='early_sample':m['points'][0][0]=979
    elif mode=='duplicate_span':traces['observed_spans'].append(copy.deepcopy(span))
    elif mode=='untrusted_metric':m['provenance']='client_supplied'
    elif mode=='missing_series_reference':m['series_labels'].pop('span_id')
    before=copy.deepcopy(r)
    assert not merge_trace_observations(r)
    assert r==before


def test_exact_lookup_from_live_alert_labels_and_root_span_uid_mapping():
    trace='a'*32;span='b'*16
    payload={'data':[{'traceID':trace,'processes':{'p':{'tags':[{'key':'k8s.pod.uid','value':'uid'}]}},
        'spans':[{'spanID':span,'processID':'p','startTime':1000000000,'duration':350000,
                  'tags':[{'key':'http.status_code','value':503}]}]}]}
    inventory={'pods':[dict(namespace='ns',name='p',uid='uid')]}
    def fetch(path,params=None):return {'data':[]} if path=='/api/services' else payload
    result=dict(cluster_id='c',groups=[],raw_alerts=[{'raw':{},'labels':{'trace_id':trace}}])
    observed=collect_traces('',result,inventory,fetch)
    assert observed['queried_trace_ids']==[trace]
    assert observed['observed_spans'][0]['duration_ms']==350
    assert observed['observed_spans'][0]['error'] is True
    inventory['pods'][0]['uid']='new-pod-uid'
    assert not trace_evidence(payload,'c',inventory,'live_jaeger')['observed_spans']
