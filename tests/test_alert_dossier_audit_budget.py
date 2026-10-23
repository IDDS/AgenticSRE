"""Keep statistical qualification intact while bounding repeated audit rows."""
import json

import pytest

from agents.alert_compression import CompressionStore, compress_alerts
from agents.alert_semantics import build_scag


@pytest.mark.parametrize('count,shape', [(3, 'positive'), (31, 'positive'),
                                        (31, 'negative'), (31, 'constant')])
def test_compact_statistical_audit_is_explicit_and_retrievable(tmp_path, count, shape):
    rows = []
    for pod in ('api', 'db'):
        for index in range(count):
            value = 1 if shape == 'constant' else index+1
            if pod == 'db' and shape == 'negative':
                value = -value
            rows.append(dict(name='Latency', source='prometheus', timestamp=1000+index,
                cluster_id='qa', namespace='shop', pod=pod, severity='warning',
                message='latency', metric_name='latency', metric_value=value, unit='ms'))
    result = compress_alerts(rows)
    left, right = result['groups']
    result['candidate_pairs'] = [{'left': left['group_id'], 'right': right['group_id']}]
    build_scag(result)
    store = CompressionStore(tmp_path)
    scan = store.create(result)
    before = store._path(scan['scan_id']).read_bytes()
    ref = scan['scan_id'], left['group_id'], 1
    compact, full = store.dossier(*ref), store.dossier(*ref, compact=False)
    edge = compact['association_candidates'][0]
    original = full['association_candidates'][0]
    stats = edge['checks']['statistics']
    original_stats = original['checks']['statistics']
    for key in ('status', 'type', 'causal_confirmed', 'context'):
        assert edge[key] == original[key]
    assert edge['causal_confirmed'] is False
    assert {k: v for k, v in stats.items() if not k.startswith('matched_evidence')} == {
        k: v for k, v in original_stats.items() if not k.startswith('matched_evidence')}
    assert len(stats['matched_evidence']) == min(count, 3)
    assert len(original_stats['matched_evidence']) == count
    if count > 3:
        assert stats['matched_evidence_total'] == count and stats['matched_evidence_truncated'] is True
        pointer = stats['matched_evidence_ref']
        assert pointer['revision'] == scan['revision'] and pointer['scan_id'] == scan['scan_id']
        stored_edge = next(e for e in store.get(pointer['scan_id'])['scag']['edges']
                           if all(e[k] == v for k, v in pointer['edge'].items()))
        assert stored_edge['checks']['statistics']['matched_evidence'] == original_stats['matched_evidence']
        assert stats['matched_evidence'] == [original_stats['matched_evidence'][i] for i in (0, count//2, count-1)]
        assert len(json.dumps(stats)) < len(json.dumps(original_stats)) * .4
    else:
        assert 'matched_evidence_truncated' not in stats
    # Existing compact formatting omits empty labels and a false truncation
    # flag. Compare observed facts, not those optional presentation fields.
    assert compact['alert_samples'] == [{k: v for k, v in sample.items()
        if not (k == 'labels' and not v or k == 'message_truncated' and v is False)}
        for sample in full['alert_samples']]
    assert compact['metric_summary'] == full['metric_summary']
    assert store._path(scan['scan_id']).read_bytes() == before
    assert [a['raw'] for a in store.get(scan['scan_id'])['raw_alerts']] == rows
