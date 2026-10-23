import copy
import time

import pytest

from agents.alert_compression import CompressionStore, compress_alerts
from agents.alert_merge_review import build_merge_review
from agents.alert_semantics import build_scag


def case(count=2):
    now = time.time()
    result = compress_alerts([dict(name=f'Metric{i}', source='prometheus', timestamp=now,
        severity='critical' if i == 0 else 'warning', cluster_id='c',
        labels=dict(namespace='ns', pod='p', alertstate='firing')) for i in range(count)])
    result.update(origin='live_scan', cluster_id='c', collection_started_at=now,
                  collection={'prometheus': 'ok', 'kubectl': 'ok'})
    entries = {}
    for i, g in enumerate(result['groups']):
        entries[g['group_id']] = dict(**{k:g[k] for k in ('cluster_id','namespace','entity_kind','entity','alert_ids')},
            status='ready', provenance='live_prometheus_rule', evidence_id=f'm-{i}', query=f'measurement_{i}',
            points=[[now-4+j, j] for j in range(5)])
    result['metric_evidence'] = {'groups': entries}
    result['relations'] = [dict(left=result['groups'][i]['group_id'], right=result['groups'][i+1]['group_id'],
                                type='shared_root', reason='test hypothesis') for i in range(count-1)]
    build_scag(result, as_of=now)
    return result


def test_review_is_integrated_but_not_automatic_or_transitive():
    result = case(3)
    original = copy.deepcopy(result)
    assert len(result['merge_review']['suggestions']) == 2
    assert all(len(s['group_ids']) == 2 and not s['causal_confirmed'] for s in result['merge_review']['suggestions'])
    assert not result['merge_review']['automatic_merge']
    build_merge_review(result)
    assert result['groups'] == original['groups']
    assert result['raw_alerts'] == original['raw_alerts']
    assert result['compression_ratio'] == 0


@pytest.mark.parametrize('mode', ['import','edited','partial','expired','future','unknown','contradicted',
    'missing_check','different_object','different_dimension','resolved','missing_metric','same_metric','conflict','outside_window'])
def test_negative_cases_do_not_offer_review(mode):
    result = case()
    groups = result['groups']; edge = result['scag']['edges'][0]
    if mode == 'import': result['origin'] = 'import'
    elif mode == 'edited': result['history'] = [{'action':'merge'}]
    elif mode == 'partial': result['collection']['kubectl'] = 'failed'
    elif mode == 'expired': result['collection_started_at'] -= 301
    elif mode == 'future': result['collection_started_at'] += 300
    elif mode == 'unknown': edge['checks']['statistics']['status'] = 'unknown'
    elif mode == 'contradicted': edge['status'] = 'contradicted'
    elif mode == 'missing_check': edge['checks'].pop('structure')
    elif mode == 'different_object': groups[1]['entity'] = 'another'
    elif mode == 'different_dimension': result['raw_alerts'][1]['labels']['device'] = 'other'
    elif mode == 'resolved': result['raw_alerts'][1]['state'] = 'resolved'
    elif mode == 'missing_metric': result['metric_evidence']['groups'] = {}
    elif mode == 'same_metric':
        for e in result['metric_evidence']['groups'].values(): e['query'] = 'same'
    elif mode == 'conflict': result['scag']['edges'].append(dict(edge, type='conflicts'))
    elif mode == 'outside_window': result['raw_alerts'][1]['timestamp'] += 301
    assert not build_merge_review(result)['suggestions']


def test_duplicate_reverse_edge_not_double_counted():
    result = case(); edge = result['scag']['edges'][0]
    result['scag']['edges'].append(dict(edge, left=edge['right'], right=edge['left']))
    assert len(build_merge_review(result)['suggestions']) == 1


def test_manual_merge_preserves_raw_critical_and_invalidates_suggestions(tmp_path):
    result = case(); store = CompressionStore(tmp_path)
    saved = store.create(result)
    suggestion = saved['merge_review']['suggestions'][0]
    dossier = store.dossier(saved['scan_id'], saved['groups'][0]['group_id'], 1)
    assert dossier['merge_review']['suggestions'][0]['group_ids'] == suggestion['group_ids']
    with pytest.raises(ValueError):
        store.edit(saved['scan_id'], revision=1, action='merge', group_ids=suggestion['group_ids'])
    merged = store.edit(saved['scan_id'], revision=1, action='merge', group_ids=suggestion['group_ids'], reason='human reviewed')
    assert merged['raw_alerts'] == saved['raw_alerts']
    assert len(merged['groups']) == 1 and merged['groups'][0]['severity'] == 'critical'
    assert merged['merge_review']['status'] == 'invalidated_by_edit'
    assert not merged['merge_review']['suggestions']
    assert merged['history'][0]['reason'] == 'human reviewed'
    with pytest.raises(ValueError):
        store.edit(saved['scan_id'], revision=1, action='merge', group_ids=suggestion['group_ids'], reason='stale')
