"""Evidence acquisition must survive unavailable summaries and reject wrong scope."""
import asyncio
from types import SimpleNamespace
import pytest
from agents.log_agent import LogAgent
from agents.trace_agent import TraceAgent
from agents.metric_agent import MetricAgent
from agents.alert_root_candidates import propagation_targets
from tools.base_tool import ToolResult
from tools.llm_client import LLMClient
from skill_runtime.skills.k8s_service_unavailable.skill import detect, diagnose


class Tool:
    def __init__(self, data):
        self.data, self.calls = data, []
    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return ToolResult(success=True, data=self.data)


class Summary:
    def __init__(self, failure=False):
        self.calls, self.failure = [], failure
    async def async_chat(self, *args, **kwargs):
        self.calls.append(kwargs)
        if self.failure:
            raise TimeoutError('unavailable')
        return ''


def registry(**tools):
    return SimpleNamespace(get=lambda name: tools.get(name))


@pytest.mark.parametrize('agent', [LogAgent, TraceAgent, MetricAgent])
def test_no_observations_is_failed_acquisition_not_healthy(agent):
    llm = Summary()
    result = asyncio.run(agent(llm, registry()).analyze('503', namespace='ns'))
    assert result['error'] and not result['evidence_items']
    assert not llm.calls


@pytest.mark.parametrize('failure', [False, True])
def test_pod_logs_survive_empty_or_failed_model_summary(failure):
    tool, llm = Tool('GET /probe 503\napplication failure'), Summary(failure)
    result = asyncio.run(LogAgent(llm, registry(kubectl=tool)).analyze('503', 'ns', pod_name='leaf'))
    assert tool.calls == [{'command': 'logs leaf --tail=500', 'namespace': 'ns'}]
    assert result['evidence_items'][0]['service'] == 'leaf'
    assert 'application failure' in result['evidence_items'][0]['observation']
    assert result['summary'] and llm.calls[0]['thinking'] is False


def test_shell_text_is_not_a_pod_lookup():
    tool = Tool('bad')
    assert not LogAgent(Summary(), registry(kubectl=tool))._fetch_pod_logs('leaf; id', 'ns')
    assert not tool.calls


@pytest.mark.parametrize('failure', [False, True])
def test_exact_trace_preserves_error_and_parent_proof(failure):
    tid = 'a' * 32
    trace = {'traceID': tid, 'processes': {'p': {'serviceName': 'leaf', 'tags': [
        {'key': 'k8s.namespace.name', 'value': 'ns'}, {'key': 'k8s.pod.uid', 'value': 'uid'}]}},
        'spans': [{'spanID': 's', 'processID': 'p', 'operationName': 'GET', 'startTime': 1,
                   'tags': [{'key': 'error', 'value': True}],
                   'references': [{'refType': 'CHILD_OF', 'spanID': 'parent'}]}]}
    tool = Tool({'data': [trace]})
    result = asyncio.run(TraceAgent(Summary(failure), registry(jaeger=tool)).analyze('503', namespace='ns', trace_ids=[tid, tid]))
    assert tool.calls == [{'trace_id': tid}]
    assert len(result['evidence_items']) == 1
    raw = result['evidence_items'][0]['raw_ref']
    assert raw['error'] is True and raw['parent_links'][0]['spanID'] == 'parent'
    assert raw['pod_uid'] == 'uid' and result['summary']
    rejected = asyncio.run(TraceAgent(Summary(), registry(jaeger=tool)).analyze('503', namespace='other', trace_ids=[tid]))
    assert rejected['error'] and not rejected['evidence_items']


def test_lookup_hints_reject_bad_scope_expiry_and_malformed_refs():
    import time
    payload = {'source': 'alert_evidence_dossier', 'namespace': 'ns', 'cluster_id': 'c',
        'root_candidates': {'status': 'candidates_ready', 'valid_until': time.time()+60,
            'candidates': [{'entity_ref': 'c|ns|pod|leaf', 'supporting_evidence': [None,
                {'trace_refs': [None, {'trace_id': 'b'*32}]}]}]}}
    assert propagation_targets(payload, 'ns') == {'pod_name': 'leaf', 'trace_ids': ['b'*32]}
    assert propagation_targets(payload, 'other') == {}
    payload['root_candidates']['valid_until'] = 1
    assert propagation_targets(payload, 'ns') == {}


def test_async_summary_thinking_option_is_forwarded_without_changing_default():
    client = object.__new__(LLMClient)
    calls = []
    client.chat = lambda *args, **kwargs: calls.append(kwargs) or 'ok'
    assert asyncio.run(client.async_chat([], thinking=False)) == 'ok'
    asyncio.run(client.async_chat([]))
    assert calls == [{'thinking': False}, {}]


def test_http_503_alone_does_not_prove_empty_endpoints():
    context = {'payload': {'message': 'HTTP 503', 'pod': 'leaf'}}
    context['detect'] = detect(context)
    result = diagnose(context)
    assert result['requires_more_evidence'] and result['confidence'] < 0.5
    assert 'endpoints 为空' not in result['root_cause']
    assert result['root_cause_component'] == 'leaf'


def test_plain_summary_does_not_force_json_provider_mode():
    from unittest.mock import Mock
    client = object.__new__(LLMClient)
    client.model, client.temperature, client.max_tokens = 'deepseek-v4-pro', .1, 100
    create = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{}'))], usage=None))
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    client.chat([{'role': 'user', 'content': 'Summarize HTTP 503'}], thinking=False)
    assert create.call_args.kwargs['extra_body']['thinking']['type'] == 'disabled'
    assert 'response_format' not in create.call_args.kwargs
    client.json_chat([{'role': 'system', 'content': 'Classify'}], thinking=False)
    assert create.call_args.kwargs['response_format'] == {'type': 'json_object'}


def test_metric_empty_summary_is_not_successful_diagnosis():
    tool = Tool({'results': [{'metric': {'pod': 'p'}, 'value': [100, '1']}]})
    result = asyncio.run(MetricAgent(Summary(), registry(prometheus=tool)).analyze('503', categories=['workload']))
    assert result['metrics_fetched'] > 0
    assert result['error'] and result['summary']
