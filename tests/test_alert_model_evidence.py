import asyncio
import copy
import json

import pytest

from agents.alert_compression import compress_alerts, propose_relations
from agents.alert_model_evidence import pair_metric_context


def fixture():
    result = compress_alerts([dict(name=name, timestamp=1000, cluster_id='c', namespace='ns',
                                  pod='p', source='prometheus') for name in ('Latency', 'Errors')])
    result['origin'] = 'live_scan'
    left, right = result['groups']
    result['candidate_pairs'] = [dict(left=left['group_id'], right=right['group_id'])]
    result['metric_evidence'] = {'groups': {}}
    for group in result['groups']:
        result['metric_evidence']['groups'][group['group_id']] = dict(
            **{k: group[k] for k in ('cluster_id', 'namespace', 'entity_kind', 'entity', 'alert_ids')},
            status='ready', provenance='live_prometheus_rule', evidence_id='m-'+group['group_id'],
            query=group['representative'], rule={'expression': group['representative']+' > 0'},
            points=[[t, v] for t, v in enumerate([1, 2, 9, 4, 5], 996)], coverage=1)
    return result, left, right


def test_actual_model_payload_has_measurements_and_no_membership_mutation():
    result, left, right = fixture()
    original = copy.deepcopy(result)
    class Model:
        async def async_json_chat(self, messages, **kwargs):
            pair = json.loads(messages[-1]['content'])[0]
            metric = pair['metric_context']
            assert metric['groups'][left['group_id']]['summary']['maximum'] == 9
            assert metric['statistics']['status'] == 'pass'
            return {'relations': []}
    asyncio.run(propose_relations(result, Model()))
    assert result['semantic']['metric_context_pairs'] == 1
    assert result['semantic']['returned_relations'] == 0
    assert result['groups'] == original['groups']
    assert result['raw_alerts'] == original['raw_alerts']


@pytest.mark.parametrize('mode', ['import', 'edited', 'cluster_id', 'namespace', 'entity_kind', 'entity', 'members', 'missing', 'nan'])
def test_unmatched_evidence_never_enters_model_as_verified(mode):
    result, left, right = fixture()
    entry = result['metric_evidence']['groups'][left['group_id']]
    if mode == 'import': result['origin'] = 'import'
    elif mode == 'edited': result['history'] = [{'action': 'split'}]
    elif mode == 'members': entry['alert_ids'] = ['foreign']
    elif mode == 'missing': entry['points'] = []
    elif mode == 'nan': entry['points'][0][1] = float('nan')
    else: entry[mode] = 'foreign'
    context = pair_metric_context(result, left, right)
    assert context['groups'][left['group_id']]['status'] == 'unknown'
    assert 'query' not in context['groups'][left['group_id']]
    assert context['statistics']['status'] == 'unknown'


def test_constant_history_does_not_become_positive_statistical_evidence():
    result, left, right = fixture()
    for entry in result['metric_evidence']['groups'].values():
        entry['points'] = [[t, 1] for t in range(996, 1001)]
    assert pair_metric_context(result, left, right)['statistics']['reason'] == 'constant series'


def test_rejected_output_is_distinguished_from_empty_output():
    result, _, _ = fixture()
    class Model:
        async def async_json_chat(self, messages, **kwargs):
            p = json.loads(messages[-1]['content'])[0]
            valid = dict(left=p['left'], right=p['right'], type='shared_root',
                         evidence_alert_ids=[s['alert_id'] for s in p['samples']])
            return {'relations': [None, {'left': []}, dict(valid, right='foreign'),
                                  dict(valid, evidence_alert_ids=['foreign']), valid, valid]}
    asyncio.run(propose_relations(result, Model()))
    assert result['semantic']['returned_relations'] == 6
    assert result['semantic']['accepted_relations'] == 1
    assert result['semantic']['rejected_relations'] == dict(invalid_relation_object=1,
        invalid_pair=1, pair_outside_batch=1, evidence_mismatch=1, duplicate_pair=1)
    assert result['relations'][0]['status'] == 'unverified'
    assert len(result['groups']) == 2
