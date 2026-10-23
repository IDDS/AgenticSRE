import copy

import numpy as np
import pytest

from agents.alert_compression import compress_alerts
from agents.alert_semantics import recall_candidates
from agents.alert_retrieval import neighbor_pools


def scan():
    records = [dict(name='Failure', source='logs', message='connection refused',
                    timestamp=1000, cluster_id='c', namespace='ns', pod=f'pod-{i}')
               for i in range(300)]
    result = compress_alerts(records)
    # Put the positive pair after the former index cutoff, in its own scope.
    ordered = {g['group_id']: g for g in result['groups']}
    for key in result['ranking'][-2:]:
        ordered[key]['namespace'] = 'tail'
    return result


@pytest.mark.parametrize('fallback', [False, True])
def test_all_groups_and_tail_pair_are_searched_without_mutating_evidence(fallback):
    result = scan()
    original = copy.deepcopy(result)
    batches = []

    def encode(texts):
        batches.append(len(texts))
        if fallback:
            raise RuntimeError('model unavailable')
        return np.ones((len(texts), 4))

    recall_candidates(result, encoder=encode, max_pairs=10000)
    info = result['retrieval']
    assert info['indexed_groups'] == info['queried_groups'] == 300
    assert not info['truncated']
    assert info['status'] == ('lexical_fallback' if fallback else 'ok')
    assert info['search_mode'] == ('bounded_lexical' if fallback else 'hnsw_and_lexical')
    assert max(batches) <= 128
    pair = set(result['ranking'][-2:])
    assert any({p['left'], p['right']} == pair for p in result['candidate_pairs'])
    assert result['raw_alerts'] == original['raw_alerts']
    assert result['groups'] == original['groups']
    by_id = {g['group_id']: g for g in result['groups']}
    assert all(by_id[p['left']]['namespace'] == by_id[p['right']]['namespace']
               for p in result['candidate_pairs'])


def test_large_index_failure_is_explicit_and_retains_lexical_tail(monkeypatch):
    import chromadb
    monkeypatch.setattr(chromadb, 'EphemeralClient', lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    result = scan()
    recall_candidates(result, encoder=lambda texts: np.ones((len(texts), 4)), max_pairs=10000)
    assert result['retrieval']['index_error'] == 'RuntimeError'
    assert result['retrieval']['status'] == 'lexical_fallback'
    pair = set(result['ranking'][-2:])
    assert any({p['left'], p['right']} == pair for p in result['candidate_pairs'])


def test_large_index_time_boundary_and_separate_candidate_truncation():
    result = scan()
    by_id = {g['group_id']: g for g in result['groups']}
    last = by_id[result['ranking'][-1]]
    last['first_seen'] = last['last_seen'] = 100000
    recall_candidates(result, encoder=lambda texts: np.ones((len(texts), 4)), max_pairs=2)
    assert result['retrieval']['indexed_groups'] == 300
    assert not result['retrieval']['truncated']
    assert result['retrieval']['candidate_pairs_truncated']
    assert len(result['candidate_pairs']) == 2
    assert all(last['group_id'] not in (p['left'], p['right']) for p in result['candidate_pairs'])


def test_ann_recovers_semantic_only_tail_and_filters_before_selection():
    groups = [dict(cluster_id='c', namespace='ns', entity='', entity_kind='pod',
                   first_seen=1000, last_seen=1000, _retrieval_window=300)
              for _ in range(300)]
    vectors = np.zeros((300, 3))
    vectors[:, 0] = 1
    vectors[-2:] = [0, 1, 0]
    # Many identical embeddings in other scopes cannot crowd out the real hit.
    for i in range(100):
        vectors[i] = [0, 1, 0]
        groups[i]['namespace'] = 'other'
    pools, info = neighbor_pools(groups, [set() for _ in groups],
                                 [set() for _ in groups], vectors, top_n=5)
    assert 298 in pools[299] and 299 in pools[298]
    assert all(j >= 100 for j in pools[299])
    assert max(map(len, pools)) <= info['neighbor_pool_limit']
    assert info['queried_groups'] == 300
