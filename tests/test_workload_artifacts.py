import asyncio
import json
from types import SimpleNamespace
from agents.workload_artifacts import collect_workload_artifacts
from agents.evidence import format_evidence
from tools.base_tool import ToolResult


class Tool:
    def __init__(self, wrong_scope=False, credential=False):
        self.calls=[]
        self.wrong_scope, self.credential=wrong_scope, credential
    def execute(self, command, namespace):
        self.calls.append(command)
        kind,name=command.split()[1:3]
        data={'metadata':{'namespace':'other' if self.wrong_scope else namespace,'name':name,'uid':'u','resourceVersion':'7'}}
        if kind=='pod':
            data['spec']={'containers':[{'name':'web','env':[{'name':'PASSWORD','value':'do-not-copy'}],
                'volumeMounts':[{'name':'source'},{'name':'private'}]}],
                'volumes':[{'name':'source','configMap':{'name':'code'}},
                    {'name':'unused','configMap':{'name':'unrelated'}},
                    {'name':'private','secret':{'secretName':'credentials'}}]}
        else:
            data['data']={'app.py': "FAILED = False\ncode = 503 if FAILED else 200\n" + ("API_KEY='do-not-copy'" if self.credential else ''),
                          'settings.env':'PASSWORD=do-not-copy'}
        return ToolResult(success=True,data=json.dumps(data))


def test_only_mounted_target_code_is_read_and_environment_values_are_omitted():
    tool=Tool()
    result=asyncio.run(collect_workload_artifacts(SimpleNamespace(get=lambda _:tool),'ns','leaf'))
    assert tool.calls==['get pod leaf -o json','get configmap code -o json']
    assert 'do-not-copy' not in json.dumps(result)
    source=[e for e in result['evidence_items'] if e['signal_type']=='declared_source_excerpt']
    assert source and '503 if FAILED' in source[0]['observation']
    assert source[0]['raw_ref']['resource_version']=='7'
    assert result['partial'] and '运行态变量' in result['summary']


def test_wrong_namespace_and_invalid_name_never_expand_scope():
    tool=Tool(wrong_scope=True)
    result=asyncio.run(collect_workload_artifacts(SimpleNamespace(get=lambda _:tool),'ns','leaf'))
    assert result['error'] and len(tool.calls)==1
    tool.calls.clear()
    result=asyncio.run(collect_workload_artifacts(SimpleNamespace(get=lambda _:tool),'ns','leaf; id'))
    assert result['error'] and not tool.calls


def test_source_with_embedded_credentials_is_not_delivered():
    tool=Tool(credential=True)
    result=asyncio.run(collect_workload_artifacts(SimpleNamespace(get=lambda _:tool),'ns','leaf'))
    assert not any(e['signal_type']=='declared_source_excerpt' for e in result['evidence_items'])
    assert 'do-not-copy' not in json.dumps(result)


def test_many_spans_do_not_hide_an_independent_source():
    data={'trace':{'evidence_items':[{'signal_type':'span','observation':str(n)+'x'*600} for n in range(10)]},
          'logs':{'evidence_items':[{'signal_type':'actual_error','observation':'target HTTP 503'}]},
          'code':{'evidence_items':[{'signal_type':'declared_source','observation':'source line 42'}]}}
    result=format_evidence(data,limit=1500)
    assert 'actual_error' in result and 'source line 42' in result
