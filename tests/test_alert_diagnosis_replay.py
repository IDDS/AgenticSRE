"""Adversarial checks for the intake evaluator, not production RCA tests."""
import copy

import pytest

from eval.alert_diagnosis_replay import labels_for, score_answer, summarize


def example():
    labels = {'expected': {'pod-a': {'symptoms': ['error', 'latency'], 'severity': 'critical'}},
              'support': {'a-error': {'entity': 'pod-a', 'kind': 'error'},
                          'a-slow': {'entity': 'pod-a', 'kind': 'latency'},
                          'a-foreign': {'entity': 'pod-b', 'kind': 'error'}}}
    answer = {'observations': [{'entity': 'pod-a', 'highest_severity': 'critical',
                 'symptoms': [{'kind': 'error', 'evidence_alert_ids': ['a-error']},
                              {'kind': 'latency', 'evidence_alert_ids': ['a-slow']}]}],
              'root_cause_confirmed': False, 'confirmed_causal_edges': [], 'mechanism_status': 'unverified'}
    return answer, labels


def test_exact_fact_and_support_pass():
    answer, labels = example()
    assert all(score_answer(answer, labels).values())


@pytest.mark.parametrize('critical', [False, True])
def test_rolling_window_severity_tracks_the_firing_rule(critical):
    rows = [dict(source='prometheus', state='firing', namespace='n', timestamp=1,
                 entity='n-a', name=name, alert_id=name, severity='warning')
            for name in ('ProbeError', 'ProbeLatency100')]
    if critical:
        rows.append(dict(rows[0], name='ProbeLatency300', alert_id='critical', severity='critical'))
    scan = dict(scan_scope='n', created_at=2, raw_alerts=rows)
    labels = labels_for(scan, 'disjoint-requests-same-pod')
    assert labels['expected']['n-a']['severity'] == ('critical' if critical else 'warning')
    rows[0]['severity'] = 'critical'
    with pytest.raises(AssertionError):
        labels_for(scan, 'disjoint-requests-same-pod')


@pytest.mark.parametrize('citation', ['a-slow', 'a-foreign', 'unknown', 'g-group'])
def test_ids_must_support_same_pod_and_symptom(citation):
    answer, labels = example()
    answer['observations'][0]['symptoms'][0]['evidence_alert_ids'] = [citation]
    result = score_answer(answer, labels)
    assert result['exact_facts'] and not result['supported_citations']


@pytest.mark.parametrize('mutation', ['omit', 'extra', 'severity', 'symptom', 'duplicate_pod',
                                     'duplicate_symptom', 'empty_citation', 'malformed_citation'])
def test_failures_cannot_be_hidden_by_correct_root_word(mutation):
    answer, labels = example()
    row = answer['observations'][0]
    if mutation == 'omit':
        answer['observations'] = []
    elif mutation == 'extra':
        extra = copy.deepcopy(row)
        extra['entity'] = 'pod-extra'
        answer['observations'].append(extra)
    elif mutation == 'severity':
        row['highest_severity'] = 'warning'
    elif mutation == 'symptom':
        row['symptoms'].pop()
    elif mutation == 'duplicate_pod':
        answer['observations'].append(copy.deepcopy(row))
    elif mutation == 'duplicate_symptom':
        row['symptoms'].append(copy.deepcopy(row['symptoms'][0]))
    elif mutation == 'empty_citation':
        row['symptoms'][0]['evidence_alert_ids'] = []
    else:
        row['symptoms'][0]['evidence_alert_ids'] = [{}]
    assert not all(score_answer(answer, labels).values())


@pytest.mark.parametrize('key,value', [('root_cause_confirmed', True), ('root_cause_confirmed', 0),
    ('confirmed_causal_edges', [{'from': 'a', 'to': 'b'}]), ('mechanism_status', 'confirmed')])
def test_unjustified_causality_fails(key, value):
    answer, labels = example()
    answer[key] = value
    assert not score_answer(answer, labels)['causal_restraint']


def test_empty_snapshot_requires_no_fabricated_pod():
    answer, _ = example()
    labels = {'expected': {}, 'support': {}}
    assert not score_answer(answer, labels)['exact_facts']
    answer['observations'] = []
    assert all(score_answer(answer, labels).values())
    answer.pop('root_cause_confirmed')
    assert not all(score_answer(answer, labels).values())


@pytest.mark.parametrize('mode', ['missing', 'duplicate', 'wrong_order', 'failed', 'unknown_usage'])
def test_incomplete_or_failed_calls_never_pass_or_report_free_tokens(mode):
    plan = [{'case': 'case-00', 'arm': arm} for arm in ('raw', 'dossier')]
    contract = {'call_plan': plan, 'scope': 'test'}
    calls = [dict(task, passed=True, seconds=1, provider={'usage': {
        'prompt_tokens': 10, 'completion_tokens': 3, 'total_tokens': 13}}) for task in plan]
    if mode == 'missing':
        calls.pop()
    elif mode == 'duplicate':
        calls[1] = copy.deepcopy(calls[0])
    elif mode == 'wrong_order':
        calls.reverse()
    elif mode == 'failed':
        calls[1]['passed'] = False
    else:
        calls[1]['provider']['usage']['prompt_tokens'] = None
    result = summarize(contract, calls)
    if mode != 'unknown_usage':
        assert not result['factual_gate_passed']
    if mode in ('missing', 'duplicate', 'wrong_order', 'unknown_usage'):
        assert result['arms']['dossier']['actual_input_tokens'] is None
    assert result['production_root_accuracy'] is None and not result['end_to_end_rca_tested']
