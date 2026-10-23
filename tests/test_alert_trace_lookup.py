from agents.alert_context import collect_traces


def test_explicit_trace_is_fetched_even_when_absent_from_recent_service_results():
    trace_id='a'*32
    calls=[]
    def fetch(path, params=None):
        calls.append(path)
        if path=='/api/services':return {'data':[]}
        assert path=='/api/traces/'+trace_id
        return {'data':[{'traceID':trace_id,'spans':[],'processes':{}}]}
    result={'cluster_id':'c','groups':[],'raw_alerts':[{'raw':{'trace_id':trace_id.upper()}}]}
    collected=collect_traces('',result,{},fetch)
    assert collected['queried_trace_ids']==[trace_id]
    assert collected['missing_trace_ids']==[]
    assert collected['status']=='no_mapped_calls'
    assert calls==['/api/services','/api/traces/'+trace_id]


def test_missing_exact_trace_is_explicit_and_never_substituted_by_another_id():
    trace_id='a'*32
    def fetch(path,params=None):
        return {'data':[]} if path=='/api/services' else {'data':[{'traceID':'b'*32}]}
    collected=collect_traces('',{'cluster_id':'c','groups':[],'raw_alerts':[{'raw':{'traceID':trace_id}}]}, {}, fetch)
    assert collected['missing_trace_ids']==[trace_id]
    assert collected['edges']==[]


def test_trace_lookup_rejects_paths_and_reports_collection_limit():
    rows=[{'raw':{'trace_id':f'{i:032x}'}} for i in range(1,22)] + [{'raw':{'trace_id':'../../config'}}]
    calls=[]
    def fetch(path,params=None):
        calls.append(path)
        return {'data':[]}
    collected=collect_traces('',{'cluster_id':'c','groups':[],'raw_alerts':rows}, {}, fetch)
    assert collected['requested_trace_ids']==21
    assert collected['invalid_trace_ids']==1
    assert collected['truncated'] and collected['exact_trace_lookup_truncated']
    assert len(collected['queried_trace_ids'])==20
    assert all('..' not in path for path in calls)
