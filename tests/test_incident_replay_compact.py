import json

import pytest

from integrations.incident_replay.compact import compact_domains
from paradigms.evidence_budget import build_evidence_context


def test_later_pod_values_and_missing_metrics_survive_shared_budget():
    pods = []
    for i in range(10):
        pods.append({'pod': f'service-{i}-1234567890-abcde',
                     'metrics': {'before': {'CpuUsageRate(%)': {'n': 2, 'mean': i}},
                                 'after': {'CpuUsageRate(%)': {'n': 3, 'mean': i + 1}}},
                     'logs': {'before': {'errors': 0}, 'after': {'errors': i}},
                     'trace': {'before': {'spans': 2}, 'after': {'spans': 3}}})
    domains = compact_domains({'pods': pods, 'quality': {'excluded_metric_files': []}})
    metric = json.loads(domains['metric_agent'][domains['metric_agent'].index('{'):])
    assert metric['pods'][-1][0] == pods[-1]['pod']
    assert metric['pods'][-1][1][1] == [2, 9]
    assert metric['pods'][-1][2][1] == [3, 10]
    assert metric['pods'][-1][1][0] is None
    text, truncated = build_evidence_context({name: {'summary': value} for name, value in domains.items()})
    assert not truncated and pods[-1]['pod'] in text


def test_unknown_fields_and_nonfinite_values_are_never_silently_dropped():
    pod = {'pod': 'p', 'metrics': {'before': {}, 'after': {}}, 'logs': {}, 'trace': {}}
    with pytest.raises(ValueError):
        compact_domains({'pods': [{**pod, 'unexpected': 1}], 'quality': {}})
    pod['metrics']['after']['CpuUsageRate(%)'] = {'n': 2, 'mean': float('nan')}
    with pytest.raises(ValueError):
        compact_domains({'pods': [pod], 'quality': {}})
