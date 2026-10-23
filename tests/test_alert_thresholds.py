import copy
import pytest
from agents.alert_compression import compress_alerts, CompressionStore
from agents.alert_metrics import collect_metric_evidence
from agents.alert_thresholds import merge_nested_thresholds


def fixture():
    records=[];rules=[]
    for n,sev in [(100,'warning'),(200,'warning'),(300,'critical')]:
        labels=dict(alertname='Latency'+str(n),namespace='shop',instance='a',severity=sev,alertstate='firing')
        records.append(dict(name=labels['alertname'],source='prometheus',cluster_id='c',timestamp=1000,labels=labels,severity=sev))
        rules.append(dict(name=labels['alertname'],type='alerting',health='ok',query=f'latency_ms > {n}',labels={'severity':sev},
                          alerts=[dict(state='firing',labels={k:v for k,v in labels.items() if k!='alertstate'})]))
    result=compress_alerts(records,time_window=30);result.update(origin='live_scan',cluster_id='c',collection_started_at=1000)
    def fetch(path,params):
        if path.endswith('rules'):data={'groups':[dict(name='test',interval=5,rules=rules)]}
        else:data=dict(resultType='matrix',result=[dict(metric={'namespace':'shop','instance':'a'},values=[[970+5*i,str(350+i)] for i in range(7)])])
        return dict(status='success',data=data)
    result['metric_evidence']=collect_metric_evidence('',result,fetch)
    return result


def test_nested_thresholds_preserve_raw_critical_rules_and_dossier(tmp_path):
    result=fixture();before=copy.deepcopy(result['raw_alerts'])
    assert merge_nested_thresholds(result)
    assert result['raw_alerts']==before and result['compressed_groups']==1
    g=result['groups'][0];assert g['severity']=='critical' and g['representative']=='Latency300'
    assert sorted(g['alert_indices'])==[0,1,2]
    assert len(g['threshold_merge']['rules'])==3
    assert result['signature_version']==3
    assert result['metric_evidence']['ready_groups']==1
    assert len(result['metric_evidence']['groups'][g['group_id']]['points'])==7
    assert not merge_nested_thresholds(result), 'second merge must be idempotent'
    store=CompressionStore(tmp_path);saved=store.create(result)
    dossier=store.dossier(saved['scan_id'],g['group_id'],1)
    assert len(dossier['threshold_merge']['rules'])==3
    assert len(dossier['metric_evidence']['rule_references'])==3


@pytest.mark.parametrize('mode',['import','untrusted','not_ready','annotation','different_operator','different_selector','different_series','different_points','outside_window','resolved','different_dimension','same_threshold'])
def test_no_merging_without_all_subsumption_conditions(mode):
    result=fixture()
    if mode=='import':result['origin']='import'
    if mode=='untrusted':result['metric_evidence']['provenance']='client_supplied'
    for j,g in enumerate(result['groups']):
        e=result['metric_evidence']['groups'][g['group_id']]
        if mode=='not_ready':e['status']='unknown'
        if mode=='annotation':e['query_basis']='rule_annotation'
        if mode=='different_operator':e['rule']['expression']=f'latency_ms {(">",">=","<")[j]} {100+j*100}'
        if mode=='different_selector':e['rule']['expression']=f'latency_{j} > {100+j*100}'
        if mode=='different_series':e['series_labels']={**e['series_labels'],'sensor':str(j)}
        if mode=='different_points':e['points'][0]=(970,350+j)
        if mode=='outside_window':
            g['first_seen']=g['last_seen']=1000+j*31
            result['raw_alerts'][g['alert_indices'][0]]['timestamp']=1000+j*31
        if mode=='resolved':result['raw_alerts'][g['alert_indices'][0]]['state']='resolved'
        if mode=='different_dimension':result['raw_alerts'][g['alert_indices'][0]]['labels']['sensor']=str(j)
        if mode=='same_threshold':e['rule']['expression']='latency_ms > 100'
    before=copy.deepcopy(result)
    assert not merge_nested_thresholds(result)
    assert result==before
