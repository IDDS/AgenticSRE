import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from agents.alert_compression import CompressionStore, compress_alerts, make_group, regroup_stats
from agents.alert_dossier_prompt import dossier_incident, initial_incident
from agents.hypothesis_agent import HypothesisAgent
from agents.planning_agent import PlanningAgent


def payload():
    return dict(source='alert_evidence_dossier', scan_id='scan-'+'a'*32, revision=1, group_id='g-a',
        cluster_id='c', namespace='shop', entity_kind='pod', entity='api', alert_count=2,
        association_candidates=[{'audit': 'x'*20000}],
        alert_samples=[dict(alert_id='a-warning', name='Error', severity='warning', timestamp=1,
                            message='request failed'),
                       dict(alert_id='a-critical', name='Latency300', severity='critical', timestamp=2,
                            message='response duration_ms=9000')],
        log_numeric_summary={'message.duration_ms': {'min': 20, 'max': 9000, 'unit': 'ms',
            'min_alert_id': 'a-warning', 'max_alert_id': 'a-critical'}},
        root_candidates={'status': 'candidates_ready', 'valid_until': 100,
            'candidates': [{'entity_ref': 'c|shop|pod|dependency', 'paths': [['dependency', 'api']]}]})


class Capture:
    def __init__(self):
        self.calls = []
    def json_chat(self, messages, **kwargs):
        self.calls.append(messages)
        return {'hypotheses': [{'id': 'H1', 'description': '待验证', 'confidence': .3}], 'plan': []}


def test_real_hypothesis_and_planner_receive_complete_priority_facts():
    dossier = payload()
    before = copy.deepcopy(dossier)
    incident = dossier_incident('Investigate', dossier, as_of=50)
    assert len(incident) <= 2000
    parsed = json.loads(incident)
    assert parsed['observations'][0]['alert_id'] == 'a-critical'
    assert parsed['omitted']['alert_samples'] == parsed['omitted']['root_candidates'] == 0
    assert parsed['numeric_summaries'][0]['values']['max'] == 9000
    assert parsed['root_candidates'][0]['causal_confirmed'] is False
    assert dossier == before
    llm = Capture()
    HypothesisAgent(llm).generate(incident)
    PlanningAgent(llm, SimpleNamespace(list_tools=lambda: [])).generate_plan([], incident)
    assert len(llm.calls) == 2
    for call in llm.calls:
        text = call[1]['content']
        assert incident in text
        assert all(value in text for value in ('a-critical', 'a-warning', '9000', 'c|shop|pod|dependency'))
        assert 'x'*100 not in text


def test_generic_initial_context_preserves_existing_format():
    body = {'source': 'other', 'a': 'x'*9000}
    assert initial_incident('q', body) == ('q\n\n当前事件结构化观测上下文（仅含运行期证据，不含注入真值）：\n'
                                          + json.dumps(body, ensure_ascii=False)[:8000])


def test_rca_entry_uses_budgeted_handoff_before_tool_initialization(monkeypatch):
    from configs.config_loader import AppConfig
    from orchestrator.rca_engine import run_rca
    import agents.alert_dossier_prompt as module
    seen = []
    class Stop(Exception):
        pass
    def capture(query, evidence):
        seen.append((query, evidence))
        raise Stop()
    monkeypatch.setattr(module, 'initial_incident', capture)
    data = payload()
    with pytest.raises(Stop):
        asyncio.run(run_rca('investigate', namespace='shop', config=AppConfig(), initial_evidence={'payload': data}))
    assert seen == [('investigate', data)]


@pytest.mark.parametrize('size', [700, 1000, 2000])
def test_budget_never_cuts_json_and_reports_omissions(size):
    data = payload()
    data['alert_samples'] *= 40
    text = dossier_incident('q'*10000, data, limit=size, as_of=50)
    parsed = json.loads(text)
    assert len(text) <= size and parsed['omitted']['alert_samples'] > 0
    assert parsed['omitted']['alert_samples'] + len(parsed['observations']) == 80
    assert parsed['omitted']['audit_details'] == 'available_in_full_dossier'


def test_expired_candidate_is_not_revived_and_unknown_counts_are_explicit():
    result = json.loads(dossier_incident('q', payload(), as_of=101))
    assert result['root_candidates'] == []
    assert 'dependency' not in json.dumps(result)


def test_long_rule_and_window_audit_do_not_displace_composite_measurements():
    data = payload()
    data['root_candidates'] = {}
    data['metric_evidence'] = {'status': 'composite', 'measurements': [
        {'evidence_id': 'm-'+field, 'rule': {'name': field, 'expression': 'x'*10000},
         'request_window': {'field': field, 'operation': 'x'*10000},
         'summary': {'samples': 31, 'minimum': low, 'maximum': high,
                     'first': [1000, low], 'last': [1030, high], 'constant': False}}
        for field, low, high in [('mean_duration_ms', 20, 9000), ('error_ratio', 0, 1)]]}
    result = json.loads(dossier_incident('q', data))
    assert result['omitted']['measurements'] == 0 and len(result['measurements']) == 2
    assert {m['quantity']: m['summary']['maximum'] for m in result['measurements']} == {
        'mean_duration_ms': 9000, 'error_ratio': 1}
    assert 'x'*100 not in json.dumps(result)


def test_scope_cannot_be_silently_shortened_to_another_resource():
    data = payload()
    data['entity'] = 'x'*10000
    result = json.loads(dossier_incident('q', data))
    assert result['status'] == 'scope_exceeds_budget' and result['ref']['group_id'] == 'g-a'


@pytest.mark.parametrize('invalid', [0, 699, True, '2000'])
def test_invalid_budget_rejected(invalid):
    with pytest.raises(ValueError):
        dossier_incident('q', payload(), limit=invalid)


def test_measurement_extrema_survive_point_removal_in_compact_dossier(tmp_path):
    result = compress_alerts([dict(name='Latency', source='prometheus', timestamp=1000,
        cluster_id='c', namespace='shop', pod='api', severity='critical')])
    group = result['groups'][0]
    result.update(origin='live_scan', metric_evidence={'groups': {group['group_id']: {
        **{k: group[k] for k in ('cluster_id', 'namespace', 'entity_kind', 'entity', 'alert_ids')},
        'status': 'ready', 'provenance': 'live_prometheus_rule', 'evidence_id': 'm-one',
        'points': [[995+i, v] for i, v in enumerate([20, 20, 9000, 20, 20, 20])],
        'rule': {'name': 'Latency', 'expression': 'latency_ms > 300'}}}})
    store = CompressionStore(tmp_path)
    scan = store.create(result)
    compact = store.dossier(scan['scan_id'], group['group_id'], 1)
    assert 'points' not in compact['metric_evidence']
    assert compact['metric_evidence']['summary']['maximum'] == 9000
    assert store.get(scan['scan_id']) == scan
    prompt = json.loads(dossier_incident('q', compact))
    assert prompt['measurements'][0]['summary']['maximum'] == 9000


@pytest.mark.parametrize('compact', [False, True])
def test_cross_metric_group_keeps_interior_error_type_not_only_latency(tmp_path, compact):
    records = [dict(name=name, source='prometheus', timestamp=1000+i,
        cluster_id='c', namespace='shop', pod='api', severity='critical' if name == 'Latency300' else 'warning')
        for i, name in enumerate(('Latency100', 'RequestError', 'Latency200', 'Latency300'))]
    result = compress_alerts(records)
    result['groups'] = [make_group(result['raw_alerts'], list(range(4)), 'verified_request_window')]
    regroup_stats(result)
    store = CompressionStore(tmp_path)
    scan = store.create(result)
    dossier = store.dossier(scan['scan_id'], scan['groups'][0]['group_id'], 1, compact=compact)
    assert {s['name'] for s in dossier['alert_samples']} == {r['name'] for r in records}
    original_error = result['raw_alerts'][1]['alert_id']
    context = json.loads(dossier_incident('investigate', dossier))
    assert any(s['alert_id'] == original_error and s['name'] == 'RequestError' for s in context['observations'])
    assert context['omitted']['alert_samples'] == 0
    assert store.get(scan['scan_id']) == scan
