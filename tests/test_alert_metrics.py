import copy
import pytest
from agents.alert_compression import compress_alerts, CompressionStore
from agents.alert_metrics import collect_metric_evidence, measurement_query
from agents.alert_semantics import statistical_check


def fixture():
    labels=[dict(alertname='HighLoad',namespace='shop',pod=p,severity='warning') for p in ('a','b')]
    result=compress_alerts([dict(name='HighLoad',source='prometheus',cluster_id='c',timestamp=1000,labels=x) for x in labels],time_window=30)
    result.update(origin='live_scan',cluster_id='c',collection_started_at=1000)
    rule=dict(type='alerting',health='ok',name='HighLoad',query='probe_load > 2',labels={'severity':'warning'},
              annotations={},alerts=[dict(state='firing',labels=x) for x in labels])
    rules={'groups':[dict(name='test',file='/rules.yaml',interval=5,rules=[rule])]}
    values=[1,2,8,3,7,4,9]
    series=[dict(metric=dict(namespace='shop',pod=p),values=[[970+i*5,str(v)] for i,v in enumerate(values)]) for p in ('a','b')]
    calls=[]
    def fetch(path, params):
        calls.append((path,params))
        return dict(status='success',data=rules if path.endswith('rules') else dict(resultType='matrix',result=series))
    return result,rules,series,fetch,calls


def test_live_rule_series_feed_statistics_without_changing_alert_membership(tmp_path):
    result,_,_,fetch,calls=fixture();before=copy.deepcopy(result)
    result['metric_evidence']=collect_metric_evidence('',result,fetch)
    assert result['metric_evidence']['status']=='ready'
    assert result['raw_alerts']==before['raw_alerts'] and result['groups']==before['groups']
    check=statistical_check(result,*result['groups'])
    assert check['status']=='pass' and check['aligned_samples']==7 and check['pearson']==1
    assert all(check['metric_evidence_ids'])
    assert all('ALERTS' not in c[1].get('query','') for c in calls)
    store=CompressionStore(tmp_path);scan=store.create(result)
    dossier=store.dossier(scan['scan_id'],scan['groups'][0]['group_id'],1,compact=False)
    assert dossier['metric_evidence']['raw_values']


@pytest.mark.parametrize('mode',['no_rule','ambiguous_rule','inactive','wrong_alert_entity','unsupported_query','ambiguous_series','wrong_scope','missing_series','short_history','nonfinite','off_grid','conflict'])
def test_unsafe_or_incomplete_metric_evidence_is_not_used(mode):
    result,rules,series,fetch,_=fixture();rule=rules['groups'][0]['rules'][0]
    if mode=='no_rule':rules['groups'][0]['rules']=[]
    if mode=='ambiguous_rule':rules['groups'][0]['rules'].append(copy.deepcopy(rule))
    if mode=='inactive':
        for a in rule['alerts']:a['state']='pending'
    if mode=='wrong_alert_entity':
        for a in rule['alerts']:a['labels']['pod']='other'
    if mode=='unsupported_query':rule['query']='sum(rate(probe_load[5m])) > 2'
    if mode=='ambiguous_series':series.extend(copy.deepcopy(series))
    if mode=='wrong_scope':
        for s in series:s['metric']['namespace']='other'
    if mode=='missing_series':series.clear()
    if mode=='short_history':
        for s in series:s['values']=s['values'][:3]
    if mode=='nonfinite':
        for s in series:s['values'][0][1]='NaN'
    if mode=='off_grid':
        for s in series:s['values'][0][0]+=.5
    if mode=='conflict':
        for s in series:s['values'].append([970,'999'])
    result['metric_evidence']=collect_metric_evidence('',result,fetch)
    assert result['metric_evidence']['ready_groups']==0
    assert statistical_check(result,*result['groups'])['status']=='unknown'


def test_imports_never_query_live_backend_or_use_forged_live_metric_evidence():
    result,_,_,fetch,calls=fixture()
    result['metric_evidence']=collect_metric_evidence('',result,fetch)
    result['origin']='import';calls.clear()
    assert collect_metric_evidence('',result,fetch)['status']=='skipped' and not calls
    assert statistical_check(result,*result['groups'])['status']=='unknown'


def test_backend_errors_and_partial_warnings_are_explicit():
    result,*_=fixture()
    def fetch(*args):return dict(status='success',warnings=['partial data'],data={})
    assert collect_metric_evidence('',result,fetch)['status']=='unavailable'


def test_explicit_rule_annotation_and_alert_state_guard():
    result,rules,_,fetch,_=fixture();rule=rules['groups'][0]['rules'][0]
    rule.update(query='sum(probe_load) > 2',annotations={'agenticsre_metric_query':'probe_load'})
    assert collect_metric_evidence('',result,fetch)['ready_groups']==2
    rule['annotations']['agenticsre_metric_query']='ALERTS{alertstate="firing"}'
    assert measurement_query(rule,result['groups'][0])[0] is None
    rule['annotations']={};rule['query']='ALERTS > 0'
    assert measurement_query(rule,result['groups'][0])[0] is None


def test_expired_collection_stops_backend_calls_and_reports_each_group(monkeypatch):
    import agents.alert_metrics as module
    result,_,_,fetch,calls=fixture()
    clock=[0.0]
    monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
    def expired(path,params):
        data=fetch(path,params);clock[0]=26.0;return data
    report=collect_metric_evidence('',result,expired)
    assert len(calls)==1
    assert report['ready_groups']==0
    assert all(e['error']=='TimeoutError' for e in report['groups'].values())


@pytest.mark.parametrize('key',['','${LLM_API_KEY}','change-me'])
def test_metric_tools_do_not_require_model_credentials(monkeypatch,key):
    import tools
    from configs.config_loader import get_config
    cfg=copy.deepcopy(get_config());cfg.llm.api_key=key
    def forbidden(*args):raise AssertionError('model client must not be initialized')
    monkeypatch.setattr(tools,'LLMClient',forbidden)
    registry=tools.build_tool_registry(cfg,isolated=True)
    assert registry.get('prometheus').llm is None


def test_configured_model_is_preserved_for_tools(monkeypatch):
    import tools
    from configs.config_loader import get_config
    cfg=copy.deepcopy(get_config());cfg.llm.api_key='test-configured-key'
    client=object();monkeypatch.setattr(tools,'LLMClient',lambda config:client)
    assert tools.build_tool_registry(cfg,isolated=True).get('prometheus').llm is client


def test_prometheus_millisecond_grid_does_not_reject_valid_history():
    result,_,series,fetch,_=fixture()
    result['collection_started_at']=1000.3337433
    def rounded(path,params):
        if path.endswith('query_range'):
            for s in series:
                s['values']=[[round(params['start']+i*params['step'],3),str(i)] for i in range(7)]
        return fetch(path,params)
    report=collect_metric_evidence('',result,rounded)
    assert report['ready_groups']==2
    assert all(e['end']==1000.333 for e in report['groups'].values())


def test_selector_history_uses_preserved_condition_and_exporter_labels():
    result,rules,series,fetch,_=fixture()
    for row in result['raw_alerts']:
        row['labels'].update(condition='true',instance='exporter-a')
    for alert in rules['groups'][0]['rules'][0]['alerts']:
        alert['labels'].update(condition='true',instance='exporter-a')
    originals=copy.deepcopy(series);series.clear()
    for sample in originals:
        for condition,instance in [('true','exporter-a'),('false','exporter-a'),('unknown','exporter-a'),('true','exporter-b')]:
            item=copy.deepcopy(sample);item['metric'].update(condition=condition,instance=instance)
            series.append(item)
    report=collect_metric_evidence('',result,fetch)
    assert report['ready_groups']==2
    assert all(e['series_labels']['condition']=='true' and e['series_labels']['instance']=='exporter-a' for e in report['groups'].values())
    series[:]=[s for s in series if s['metric']['condition']!='true']
    assert collect_metric_evidence('',result,fetch)['ready_groups']==0


def test_rule_overridden_labels_cannot_disambiguate_measurements():
    result,rules,series,fetch,_=fixture()
    rule=rules['groups'][0]['rules'][0]
    rule['labels']['condition']='true'
    for row in result['raw_alerts']:row['labels']['condition']='true'
    for alert in rule['alerts']:alert['labels']['condition']='true'
    originals=copy.deepcopy(series);series.clear()
    for sample in originals:
        for condition in ('true','false'):
            item=copy.deepcopy(sample);item['metric']['condition']=condition;series.append(item)
    assert collect_metric_evidence('',result,fetch)['ready_groups']==0


@pytest.mark.parametrize('index',range(4))
def test_reviewed_complex_expression_preserves_labels_and_provenance(index):
    from agents.alert_metric_adapters import REVIEWED_ADAPTERS
    result,rules,series,fetch,_=fixture()
    adapter=REVIEWED_ADAPTERS[index]
    rule=rules['groups'][0]['rules'][0];rule['query']=adapter['expression']
    report=collect_metric_evidence('',result,fetch)
    assert report['ready_groups']==2
    for entry in report['groups'].values():
        assert entry['query_basis']=='reviewed_expression'
        assert entry['measurement_adapter']['adapter_id']==adapter['adapter_id']
        assert entry['measurement_adapter']['meaning']
    # A same-name rule with changed semantics must not inherit the adapter.
    rule['query']='('+rule['query']+') or vector(1)'
    assert collect_metric_evidence('',result,fetch)['ready_groups']==0


def test_complex_expression_annotation_takes_precedence():
    from agents.alert_metric_adapters import REVIEWED_ADAPTERS
    result,rules,_,_,_=fixture();rule=rules['groups'][0]['rules'][0]
    rule.update(query=REVIEWED_ADAPTERS[0]['expression'],annotations={'agenticsre_metric_query':'custom_measurement'})
    assert measurement_query(rule,result['groups'][0])==('custom_measurement','rule_annotation')


def test_constant_heartbeat_is_explicitly_inapplicable_without_dropping_alert():
    labels={'alertname':'Watchdog','severity':'none'}
    result=compress_alerts([dict(name='Watchdog',source='prometheus',cluster_id='c',timestamp=1000,labels=labels)])
    result.update(origin='live_scan',cluster_id='c',collection_started_at=1000)
    before=copy.deepcopy(result)
    rule=dict(type='alerting',health='ok',name='Watchdog',query='vector(1)',labels={'severity':'none'},
              alerts=[dict(state='firing',labels=labels)])
    def fetch(path,params):
        assert path.endswith('rules'), 'heartbeat must not query a measurement history'
        return dict(status='success',data={'groups':[dict(name='general',rules=[rule])]})
    report=collect_metric_evidence('',result,fetch)
    assert result==before
    assert next(iter(report['groups'].values()))['status']=='not_applicable'
    rule['query']='vector(0)'
    assert next(iter(collect_metric_evidence('',result,fetch)['groups'].values()))['status']=='unknown'
