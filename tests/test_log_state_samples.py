import json

from agents.log_agent import LogAgent, select_log_samples
from types import SimpleNamespace
from orchestrator.rca_engine import _propagation_root_identity_observed


def transition(pod='leaf', **kw):
    return json.dumps(dict(event='state_transition', pod=pod, timestamp=1,
                           before=False, after=True, **kw))


def test_audit_survives_later_request_and_access_logs():
    lines = [transition()] + ['request 503'] * 80
    samples = select_log_samples(lines, 'leaf')
    assert samples[0]['line_number'] == 1
    assert samples[-1]['line_number'] == 81
    assert len(samples) == 3


def test_other_pod_and_unstructured_text_do_not_get_audit_priority():
    lines = [transition('other'), 'state_transition before false after true'] + ['request'] * 5
    assert [s['line_number'] for s in select_log_samples(lines, 'leaf')] == [6, 7]


def test_only_last_two_transitions_and_bounded_samples():
    lines = [transition(change_id=str(i)) for i in range(5)] + ['x' * 900, 'recent']
    selected = select_log_samples(lines, 'leaf')
    assert [s['line_number'] for s in selected] == [5, 4, 6, 7]
    assert all(len(s['text']) <= 600 for s in selected)


def test_full_tail_gets_one_bounded_lookback():
    calls = []
    def execute(**kwargs):
        calls.append(kwargs['command'])
        lines = ['request'] * 500 if len(calls) == 1 else [transition()] + ['request'] * 600
        return SimpleNamespace(success=True, data='\n'.join(lines))
    tool = SimpleNamespace(execute=execute)
    agent = LogAgent(None, SimpleNamespace(get=lambda name: tool))
    result = agent._fetch_pod_logs('leaf', 'ns')
    assert calls == ['logs leaf --tail=500', 'logs leaf --tail=2000']
    assert result['command'] == calls[-1]
    assert 'state_transition' in select_log_samples(result['lines'], 'leaf')[0]['text']


def test_failed_lookback_keeps_first_observations():
    calls = []
    def execute(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(success=len(calls) == 1, data='\n'.join(['request'] * 500))
    agent = LogAgent(None, SimpleNamespace(get=lambda name: SimpleNamespace(execute=execute)))
    result = agent._fetch_pod_logs('leaf', 'ns')
    assert result['count'] == 500 and result['command'].endswith('--tail=500')
    assert len(calls) == 2


def test_root_entity_requires_observed_identity_not_role_or_composite_namespace():
    entity = {'namespace': 'ns', 'kind': 'Pod', 'name': 'full-leaf'}
    evidence = {'configuration_agent': {'evidence_items': [{
        'source': 'kubernetes_manifest', 'signal_type': 'pod_declared_configuration',
        'service': 'full-leaf', 'raw_ref': {'namespace': 'ns', 'pod': 'full-leaf', 'uid': 'u1'},
    }]}}
    assert _propagation_root_identity_observed({'root_entity': entity}, evidence, 'ns')
    assert not _propagation_root_identity_observed({'root_entity': dict(entity, namespace='default|ns')}, evidence, 'ns')
    assert not _propagation_root_identity_observed({'root_entity': dict(entity, name='dependency')}, evidence, 'ns')
    assert not _propagation_root_identity_observed({'root_entity': entity}, {}, 'ns')
    assert not _propagation_root_identity_observed({}, evidence, 'ns')


def test_health_access_logs_do_not_evict_structured_request_state():
    request = json.dumps({'pod': 'leaf', 'code': 503, 'trace_id': 'a' * 32,
                          'state': {'FAILED': True, 'change_id': 'change-1'}})
    lines = [transition(change_id='change-1'), request,
             'GET /probe 503', 'GET /health 200', 'GET /health 200']
    samples = select_log_samples(lines, 'leaf')
    assert samples[1]['line_number'] == 2
    rendered = '\n'.join(s['text'] for s in samples)[:1500]
    rows = []
    for line in rendered.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    assert any(r.get('state', {}).get('change_id') == 'change-1' for r in rows)
