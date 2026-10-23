import asyncio
import json
from types import SimpleNamespace

import pytest

from agents.metric_agent import MetricAgent
from agents.evidence import observed_state_transitions, format_evidence
from tools.base_tool import ToolResult


class Prom:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return ToolResult(success=True, data={'query': kwargs['query'], 'results': self.rows})


class Model:
    async def async_chat(self, messages, **kwargs):
        self.messages, self.options = messages, kwargs
        return ''


def test_metric_scope_and_original_samples_survive_empty_model_summary():
    prom = Prom([{'metric': {'namespace': 'shop', 'pod': 'api'}, 'values': [[100, '1'], [130, '7']]},
                 {'metric': {'namespace': 'other', 'pod': 'secret'}, 'values': [[100, '99']]}])
    model = Model()
    agent = MetricAgent(model, SimpleNamespace(get=lambda _: prom))
    result = asyncio.run(agent.analyze('HTTP 503', 'shop', ['application']))
    assert result['error'] is True and result['summary_status'] == 'unavailable'
    assert result['collection_status'] == 'ok'
    assert result['summary_error'] == 'empty_model_response'
    assert '不能据此判定正常' in result['summary']
    assert result['evidence_items']
    assert all(item['service'] == 'api' for item in result['evidence_items'])
    assert result['evidence_items'][0]['raw_ref']['samples'] == [[100., 1.], [130., 7.]]
    assert all(call['query_type'] == 'range' and 'namespace="shop"' in call['query'] for call in prom.calls)
    assert model.options['thinking'] is False
    assert 'latest' in model.messages[1]['content'] and 'secret' not in str(model.messages)


def test_namespaced_query_does_not_use_unbound_node_metrics():
    prom = Prom([])
    result = asyncio.run(MetricAgent(Model(), SimpleNamespace(get=lambda _: prom)).analyze('fault', 'shop'))
    assert result['error'] is True and not result['evidence_items']
    assert all('node_' not in call['query'] for call in prom.calls)


def test_nonfinite_and_invalid_samples_cannot_be_observations():
    series = {'values': [[1, 'NaN'], [2, '+Inf'], [3, 'bad'], [], [4, '2'], ['NaN', '4']]}
    assert MetricAgent._samples(series) == [[4., 2.]]


def test_metric_query_failure_is_explicit():
    prom = SimpleNamespace(execute=lambda **kwargs: ToolResult(success=False, error='unavailable'))
    result = asyncio.run(MetricAgent(Model(), SimpleNamespace(get=lambda _: prom)).analyze('fault', 'shop', ['application']))
    assert result['error'] is True
    assert result['collection_errors'][0]['error'] == 'unavailable'


def test_external_cancellation_is_not_swallowed():
    class Cancelled(Model):
        async def async_chat(self, *args, **kwargs):
            raise asyncio.CancelledError()
    prom = Prom([{'metric': {'namespace': 'shop'}, 'value': [1, '2']}])
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(MetricAgent(Cancelled(), SimpleNamespace(get=lambda _: prom)).analyze('fault', 'shop', ['application']))


def test_transition_chronology_uses_original_time_and_scope():
    rows = [dict(event='state_transition', pod='api', timestamp=200, field='FAILED', before=False, after=True, change_id='new'),
            dict(event='state_transition', pod='api', timestamp=100, field='FAILED', before=True, after=False, change_id='old'),
            dict(event='state_transition', pod='other', timestamp=150, field='FAILED', before=True, after=False)]
    evidence = {'log_agent': {'evidence_items': [{'signal_type': 'pod_logs_observed', 'timestamp': '999',
        'observation': '\n'.join(json.dumps(row) for row in rows), 'raw_ref': {'namespace': 'shop', 'pod': 'api'}}]}}
    events = observed_state_transitions(evidence)
    assert [row['timestamp'] for row in events] == [100, 200]
    assert events[0]['before'] is True and events[0]['after'] is False
    assert events[1]['before'] is False and events[1]['after'] is True
    assert format_evidence(evidence).startswith('[Observed application state changes')


def test_generated_timeline_cannot_swap_restoration_and_failure():
    from agents.evidence import ground_state_timeline
    original = {'root_cause': 'application state failure',
                'timeline': [{'time': '999', 'event': 'invented occurrence'}]}
    evidence = {'log_agent': {'evidence_items': [{'signal_type': 'pod_logs_observed',
        'timestamp': '999', 'raw_ref': {'namespace': 'shop', 'pod': 'api'},
        'observation': json.dumps(dict(event='state_transition', pod='api', timestamp=100,
                                      field='FAILED', before=True, after=False))}]}}
    result = ground_state_timeline(original, evidence)
    assert result['timeline'][0]['time'] == '100'
    assert result['timeline'][0]['observation']['after'] is False
    assert original['timeline'][0]['time'] == '999'
    assert result['timeline_scope'] == 'observed_application_state_changes_only'


def test_all_collected_metric_series_keep_original_evidence():
    prom = Prom([{'metric': {'namespace': 'shop', 'pod': f'p-{i}'}, 'values': [[100, '1'], [130, '7']]} for i in range(32)])
    result = asyncio.run(MetricAgent(Model(), SimpleNamespace(get=lambda _: prom)).analyze('fault', 'shop', ['application']))
    assert len(result['evidence_items']) == 128
    assert any('pod_not_ready' in row['observation'] for row in result['evidence_items'])
    assert result['observations_truncated'] is False


def test_critic_incomplete_response_requires_review():
    from orchestrator.collaboration_optimizer import CollaborationOptimizer, CollaborationPolicy
    model = SimpleNamespace(json_chat=lambda *args, **kwargs: {'needs_human_review': False})
    from configs.config_loader import AppConfig
    result = CollaborationOptimizer(AppConfig(), llm=model).critique_report('fault', {'root_cause': 'unknown'}, {}, CollaborationPolicy())
    assert result['needs_human_review'] is True
    assert result['error'] == 'critic_unavailable_or_invalid'


def test_many_metrics_do_not_crowd_out_other_agents():
    from orchestrator.rca_engine import _extract_evidence_items
    evidence = {'metric_agent': {'evidence_items': [{'signal_type': 'metric_series_observed', 'n': i} for i in range(128)]},
                'event_agent': {'evidence_items': [{'signal_type': 'deployment_scaled_to_zero'}]},
                'log_agent': {'evidence_items': [{'signal_type': 'pod_logs_observed'}]}}
    items = _extract_evidence_items(evidence)
    assert len(items) == 100
    assert [item['source_agent'] for item in items[:3]] == ['metric_agent', 'event_agent', 'log_agent']


def test_report_revision_cannot_silently_drop_root_entity():
    from configs.config_loader import AppConfig
    from orchestrator.collaboration_optimizer import CollaborationOptimizer, CollaborationPolicy
    original = {'root_cause': 'observed failure', 'root_entity': {'namespace': 'shop', 'kind': 'Pod', 'name': 'api'}, 'confidence': .8}
    model = SimpleNamespace(json_chat=lambda *args, **kwargs: {'root_cause': 'changed conclusion', 'confidence': .9})
    revised = CollaborationOptimizer(AppConfig(), llm=model).improve_report('fault', original, {}, {'needs_revision': True}, CollaborationPolicy())
    assert revised['root_entity'] == original['root_entity']
    assert revised['root_cause'] == original['root_cause']
    assert revised['revision_error'] == 'missing_root_entity'
    assert 'revision_error' not in original
