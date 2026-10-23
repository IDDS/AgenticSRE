import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agents.alert_compression import compress_alerts, propose_relations
from agents.alert_semantics import recall_candidates


def case(n=45):
    result = compress_alerts([dict(name='Failure', source='logs', message='connection refused',
        timestamp=1000, cluster_id='c', namespace='ns', pod=f'p-{i}') for i in range(n+1)])
    ids = [g['group_id'] for g in result['groups']]
    result['candidate_pairs'] = [dict(left=ids[0], right=key) for key in ids[1:]]
    return result


def relation(pair):
    return dict(left=pair['left'], right=pair['right'], type='shared_root', reason='candidate only',
                evidence_alert_ids=[s['alert_id'] for s in pair['samples']])


@pytest.mark.parametrize('failed_batch', [None, 1])
def test_all_batches_tail_and_failure_are_accounted_for(failed_batch):
    result = case()
    original = copy.deepcopy(result)
    inputs = []
    class Model:
        async def async_json_chat(self, messages, **kwargs):
            batch = json.loads(messages[-1]['content'])
            inputs.append(batch)
            if len(inputs)-1 == failed_batch:
                raise TimeoutError('fixture')
            return {'relations': [relation(pair) for pair in batch]}
    asyncio.run(propose_relations(result, Model()))
    report = result['semantic']
    assert list(map(len, inputs)) == [20, 20, 5]
    assert report['model_calls'] == 3
    assert report['completed_pairs'] == (45 if failed_batch is None else 25)
    assert report['failed_pairs'] == (0 if failed_batch is None else 20)
    assert report['coverage_complete'] == (failed_batch is None)
    assert report['status'] == ('ok' if failed_batch is None else 'partial')
    assert len(result['relations']) == report['completed_pairs']
    assert result['relations'][-1]['right'] == result['candidate_pairs'][-1]['right']
    assert result['groups'] == original['groups'] and result['raw_alerts'] == original['raw_alerts']
    assert all(r['status'] == 'unverified' for r in result['relations'])


def test_cross_batch_evidence_is_rejected_and_truncation_not_hidden():
    result = case(21)
    result['retrieval'] = {'candidate_pairs_truncated': True}
    first = []
    class Model:
        async def async_json_chat(self, messages, **kwargs):
            batch = json.loads(messages[-1]['content'])
            if not first:
                first.append(relation(batch[0]))
                return {'relations': []}
            return {'relations': [first[0], relation(batch[0])]}
    asyncio.run(propose_relations(result, Model()))
    assert len(result['relations']) == 1
    assert result['relations'][0]['right'] == result['candidate_pairs'][-1]['right']
    assert result['semantic']['completed_pairs'] == 21
    assert result['semantic']['coverage_complete'] is False


def test_default_retrieval_no_longer_drops_pairs_after_200():
    result = case(80)
    recall_candidates(result, encoder=lambda texts: [[1, 0] for _ in texts])
    assert len(result['candidate_pairs']) > 200
    assert len(result['candidate_pairs']) == result['retrieval']['candidate_pairs_total']
    assert not result['retrieval']['candidate_pairs_truncated']


def test_cancellation_propagates_instead_of_starting_more_calls():
    result = case()
    model = SimpleNamespace(async_json_chat=AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(propose_relations(result, model))
    assert model.async_json_chat.call_count == 1


def test_context_preserves_all_candidates_and_recalls_tail_trace():
    from agents.alert_context import recall_context
    from agents.alert_semantics import entity_ref
    result = case(300)
    original = copy.deepcopy(result['candidate_pairs'])
    recall_context(result)
    assert result['candidate_pairs'] == original
    by_id = {g['group_id']: g for g in result['groups']}
    a, b = [by_id[key] for key in result['ranking'][-2:]]
    ref = lambda g: entity_ref(g['cluster_id'], g['namespace'], g['entity_kind'], g['entity'])
    result['context_evidence'] = {'traces': {'edges': [dict(source=ref(a), target=ref(b),
        at=1000, trace_id='trace-tail', span_id='span-tail', parent_span_id='parent', provenance='fixture')]}}
    recall_context(result)
    assert result['context_candidate_pairs'] == 1
    assert len(result['candidate_pairs']) == len(original)+1
    assert any(p.get('retrieval', {}).get('observed_context') == 'observed_call'
               and {p['left'], p['right']} == {a['group_id'], b['group_id']} for p in result['candidate_pairs'])
