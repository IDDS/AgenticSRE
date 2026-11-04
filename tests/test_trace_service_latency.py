"""Service latency must not be substituted with the longest span in a trace."""
import asyncio
from types import SimpleNamespace

from agents.trace_agent import TraceAgent
from tools.observability import JaegerTool


def jaeger(trace):
    tool = JaegerTool('http://jaeger.invalid')
    tool.session.get = lambda *args, **kwargs: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {'data': [trace]})
    return tool


def trace():
    return {'traceID': 'a' * 32,
            'processes': {'frontend': {'serviceName': 'frontend'}, 'rate': {'serviceName': 'rate'}},
            'spans': [{'processID': 'frontend', 'duration': 100000},
                      {'processID': 'rate', 'duration': 5000},
                      {'processID': 'rate', 'duration': 3000}]}


def test_jaeger_reports_distinct_trace_and_selected_service_span_duration():
    result = jaeger(trace()).execute(service='rate')
    assert result.success
    summary = result.data['traces'][0]
    assert summary['total_duration_us'] == 100000
    assert summary['service_max_span_duration_us'] == 5000


def test_trace_agent_calculates_percentiles_from_selected_service_spans():
    class Model:
        async def async_chat(self, *args, **kwargs):
            return 'reviewed summary'
    tool = jaeger(trace())
    agent = TraceAgent(Model(), SimpleNamespace(get=lambda name: tool if name == 'jaeger' else None))
    result = asyncio.run(agent.analyze('latency', service='rate'))
    assert result['latency_results']['rate']['p95_us'] == 5000
    assert result['latency_results']['rate']['count'] == 1


def test_missing_service_span_does_not_become_a_zero_or_trace_duration():
    result = jaeger(trace()).execute(service='unrelated')
    assert result.success
    assert result.data['traces'][0]['service_max_span_duration_us'] is None
    class Model:
        async def async_chat(self, *args, **kwargs):
            raise AssertionError('no usable latency evidence')
    tool = jaeger(trace())
    agent = TraceAgent(Model(), SimpleNamespace(get=lambda name: tool if name == 'jaeger' else None))
    result = asyncio.run(agent.analyze('latency', service='unrelated'))
    assert result['error'] and not result.get('latency_results')
    assert result['services_analyzed'] == 0 and result['services_fetched'] == 1
