import json
import shlex
import pytest
from agents.alert_context import namespace_trace_query


def test_exact_trace_proxy_discovers_prefix_and_preserves_response():
    trace_id='a'*32
    calls=[]
    expected={'data':[{'traceID':trace_id,'spans':[]}]}
    def shell(command, timeout):
        args=shlex.split(command);calls.append(args[3])
        assert args[:3]==['kubectl','get','--raw']
        return {'ok':True,'stdout':json.dumps(expected) if '/jaeger/ui/' in args[3] else '<html>not the query API</html>'}
    prefixes={}
    assert namespace_trace_query('shop','/api/traces/'+trace_id,None,shell,prefixes)==expected
    assert prefixes=={'shop':'/jaeger/ui'} and len(calls)==2
    namespace_trace_query('shop','/api/traces/'+trace_id,None,shell,prefixes)
    assert len(calls)==3


@pytest.mark.parametrize('path',['/api/traces/../config','/api/traces/a?x=1','/api/traces/'+'a'*33,'/api/traces/'+'z'*32,'/api/admin','/api/traces/'+ 'a'*32+'/extra'])
def test_proxy_rejects_paths_before_any_remote_command(path):
    def shell(*args):raise AssertionError('must not execute')
    with pytest.raises(ValueError):namespace_trace_query('shop',path,None,shell,{})


def test_proxy_keeps_namespace_and_query_as_quoted_arguments():
    calls=[]
    def shell(command, timeout):
        calls.append(shlex.split(command))
        return {'ok':True,'stdout':'{"data":[]}'}
    namespace_trace_query('shop','/api/traces',{'service':'a; echo secret','limit':5},shell,{})
    assert len(calls[0])==5
    assert 'service=a%3B+echo+secret' in calls[0][3]
