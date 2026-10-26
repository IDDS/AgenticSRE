"""Cross-investigation history is a bounded, read-only view of recorded facts."""
import copy
import json

import pytest
from fastapi.testclient import TestClient

import web_app.app as web
from operational_ontology import build_investigation


def fact(at='2026-10-23T10:00:00Z', *, uid='worker-uid', cluster='east', namespace='shop',
         kind='Deployment', name='worker', spec=None, **raw):
    return {'source': 'kubectl structured read', 'source_agent': 'event_agent',
            'signal_type': 'resource_topology_observed', 'observation': 'Recorded resource projection',
            'timestamp': at, 'raw_ref': {'kind': kind, 'metadata': {
                'name': name, 'namespace': namespace, 'uid': uid, 'resourceVersion': 'opaque-v'},
                'cluster_id': cluster, 'observed_at': at, 'spec': spec if spec is not None else {'replicas': 1}, **raw}}


def run(id, *observations, status='completed', started_at=1, cluster='east', namespace='shop'):
    return {'id': id, 'status': status, 'started_at': started_at, 'cluster_id': cluster,
            'namespace': namespace, 'events': [], 'result': {'observations': list(observations)}}


@pytest.fixture
def history(monkeypatch):
    records = {
        'old': run('old', fact('2026-10-23T09:00:00Z', spec={'replicas': 1})),
        'source': run('source', fact(spec={'replicas': 3}), started_at=3),
    }
    monkeypatch.setitem(web._state, 'rca_runs', records)
    monkeypatch.setattr(web, '_RCA_HISTORY_LOAD_FAILED', False, raising=False)
    return TestClient(web.app), records


def binding(records, run_id='source'):
    model = build_investigation(records[run_id])
    obj = next(o for o in model['objects'] if o['kind'] == 'Deployment' and o['name'] == 'worker')
    return model, obj


def fetch(history, *, run_id='source', entity_id=None, revision=None, **params):
    client, records = history
    model, obj = binding(records, run_id)
    if revision is None:
        header = client.get(f'/api/rca/{run_id}/ontology')
        assert header.status_code == 200, header.text
        revision = header.json()['revision']
    return client.get(f'/api/rca/{run_id}/objects/{entity_id or obj["id"]}/history',
                      params={'revision': revision, **params})


def data(history, **params):
    response = fetch(history, **params)
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'private, no-store'
    return response.json()


def test_same_uid_history_as_of_source_with_separate_incarnations(history):
    _, records = history
    records.update({
        'future': run('future', fact('2026-10-23T11:00:00Z', spec={'replicas': 8})),
        'recreated': run('recreated', fact('2026-10-23T08:00:00Z', uid='previous-uid')),
        'other-cluster': run('other-cluster', fact(cluster='west'), cluster='west'),
        'other-namespace': run('other-namespace', fact(namespace='other'), namespace='other'),
        'active': run('active', fact('2026-10-23T09:30:00Z'), status='running'),
    })
    result = data(history)
    assert [r['run_id'] for r in result['items']] == ['source', 'old']
    assert result['as_of'] == '2026-10-23T10:00:00Z'
    assert result['items'][0]['comparison']['changes'] == [
        {'field': 'spec.replicas', 'before': 1, 'after': 3}]
    assert result['other_incarnations'][0]['uid'] == 'previous-uid'
    assert result['semantics']['is_causal_proof'] is False
    assert result['coverage']['retained_terminal_limit'] == 200


def test_revisions_bind_both_source_and_history_pagination(history):
    first = data(history, limit=1)
    assert first['has_more'] and len(first['items']) == 1
    assert fetch(history, revision='stale').status_code == 409
    assert fetch(history, offset=1, limit=1).status_code == 409
    second = data(history, offset=1, limit=1, history_revision=first['history_revision'])
    assert second['items'][0]['run_id'] == 'old'
    history[1]['old']['result']['observations'][0]['raw_ref']['spec']['replicas'] = 2
    assert fetch(history, offset=1, history_revision=first['history_revision']).status_code == 409


@pytest.mark.parametrize('params', [{'limit': 0}, {'limit': 101}, {'offset': -1}])
def test_query_bounds(history, params):
    assert fetch(history, **params).status_code == 422


def test_missing_source_or_object_never_broadens_lookup(history):
    client, _ = history
    assert fetch(history, entity_id='obj-missing').status_code == 404
    assert client.get('/api/rca/missing/objects/obj-x/history', params={'revision': 'rev-x'}).status_code == 404
    assert client.get('/api/rca/source/objects/obj-x/history').status_code == 422


@pytest.mark.parametrize('at', ['', None, True, float('nan'), '2026-10-23T10:00:00', 'bad-time'])
def test_missing_or_invalid_collection_time_is_not_filled_from_event_or_run_start(history, at):
    item = history[1]['source']['result']['observations'][0]
    item['raw_ref']['observed_at'] = at
    item['timestamp'] = '2026-10-23T10:00:00Z'
    assert fetch(history).status_code == 409


def test_uid_and_actual_observation_required_not_owner_reference_placeholder(history):
    history[1]['source']['result']['observations'][0]['raw_ref']['metadata']['uid'] = ''
    assert fetch(history).status_code == 409
    child = fact(kind='Pod', name='child')
    child['raw_ref']['metadata']['ownerReferences'] = [
        {'kind': 'Deployment', 'name': 'worker', 'uid': 'worker-uid', 'controller': True}]
    history[1]['source'] = run('source', child)
    assert fetch(history).status_code == 409


def test_missing_fields_are_availability_changes_not_deleted_resources(history):
    raw = history[1]['source']['result']['observations'][0]['raw_ref']
    raw['spec'] = {}
    raw['metadata']['labels'] = {'tier': 'workers'}
    comparison = data(history)['items'][0]['comparison']
    assert comparison['changes'] == []
    assert comparison['availability'] == {'added_fields': ['metadata.labels'], 'missing_fields': ['spec.replicas']}
    assert comparison['status'] == 'availability_changed'


def test_equal_timestamps_do_not_invent_a_change_order(history):
    records = history[1]
    records['tie'] = run('tie', fact('2026-10-23T09:00:00Z', spec={'replicas': 2}))
    result = data(history)
    assert result['items'][0]['comparison']['status'] == 'baseline'
    assert all(r['comparison']['status'] == 'ambiguous_time' for r in result['items'][1:])


def test_previous_state_is_context_only_and_opaque_versions_do_not_order_time(history):
    records = history[1]
    previous = fact('2026-10-23T09:30:00Z', spec={'replicas': 99}, previous=True)
    previous['raw_ref']['metadata']['resourceVersion'] = '999999999'
    records['previous'] = run('previous', previous)
    result = data(history)
    assert result['items'][0]['comparison']['changes'][0]['before'] == 1
    assert result['items'][1]['comparison']['status'] == 'context_only'


def test_failed_investigation_keeps_successful_structured_observations(history):
    history[1]['old']['status'] = 'failed'
    assert data(history)['items'][1]['run_status'] == 'failed'


def test_collection_gaps_are_retained_alongside_valid_facts(history):
    history[1]['old']['result']['collection_history'] = [{'agent': 'event_agent', 'status': 'partial'}]
    result = data(history)
    assert result['total'] == 2
    assert result['items'][1]['collection_has_gaps'] is True
    assert 'collection_incomplete' in result['gaps']


def test_legacy_review_excerpt_not_promoted_to_recorded_fact(history):
    records = history[1]
    records['legacy'] = run('legacy')
    records['legacy']['result'] = {'judge': {'review_evidence': [fact('2026-10-23T08:00:00Z')]}}
    result = data(history)
    assert result['total'] == 2
    assert 'legacy_excerpts_excluded' in result['gaps']


def test_skill_only_committed_successful_json_and_no_environment_or_messages(history):
    raw = copy.deepcopy(fact('2026-10-23T08:00:00Z')['raw_ref'])
    raw['spec']['containers'] = [{'env': [{'name': 'PRIVATE_FIXTURE', 'value': 'DO_NOT_EXPOSE'}]}]
    raw['metadata']['annotations'] = {'private': 'DO_NOT_EXPOSE'}
    raw['status'] = {'containerStatuses': [{'name': 'worker', 'ready': False, 'restartCount': 1,
        'state': {'waiting': {'reason': 'Pending', 'message': 'DO_NOT_EXPOSE'}}}]}
    command = {'tool': 'kubectl', 'command': 'get deployment/worker -o json', 'timestamp': '2026-10-23T08:00:00Z',
               'cluster_id': 'east', 'executed': True, 'success': True, 'data': json.dumps(raw)}
    history[1]['skill'] = run('skill')
    history[1]['skill']['result'] = {'agent_evidence': {'agent_evidence_collection': {'commands': [command]}}}
    result = data(history)
    assert result['total'] == 3 and result['items'][-1]['origin'] == 'committed_tool'
    assert 'DO_NOT_EXPOSE' not in json.dumps(result)
    command['outcome_unknown'] = True
    assert data(history)['total'] == 2


def test_read_only_and_unrelated_logs_do_not_invalidate_history(history, monkeypatch):
    before = copy.deepcopy(history[1])
    def forbidden(*args, **kwargs):
        raise AssertionError('History read attempted a side effect')
    monkeypatch.setattr(web, '_save_rca_history', forbidden)
    monkeypatch.setattr(web, '_ontology_live_resource', forbidden)
    first = data(history)
    assert history[1] == before
    history[1]['old']['logs'] = ['Unrelated diagnostic prose']
    assert data(history)['history_revision'] == first['history_revision']


def test_history_survives_existing_checkpoint_restart(history, monkeypatch, tmp_path):
    monkeypatch.setattr(web, '_RCA_HISTORY_FILE', tmp_path / 'history.json')
    before = data(history)
    assert web._save_rca_history()
    history[1].clear()
    web._load_rca_history()
    after = data(history)
    assert after['history_revision'] == before['history_revision']
    assert after['items'] == before['items']


def test_history_load_failure_cannot_look_like_healthy_empty_history(history, monkeypatch, tmp_path):
    path = tmp_path / 'history.json'
    path.write_text('not valid JSON')
    monkeypatch.setattr(web, '_RCA_HISTORY_FILE', path)
    web._load_rca_history()
    assert fetch(history).status_code == 503


def test_retention_window_does_not_claim_all_ram_records_are_durable(history):
    records = history[1]
    for i in range(205):
        records[f'other-{i}'] = run(f'other-{i}', fact(uid=f'other-{i}', name=f'other-{i}'), started_at=100 + i)
    result = data(history)
    assert [r['run_id'] for r in result['items']] == ['source']
    assert result['coverage']['terminal_runs_considered'] == 200
    assert result['coverage']['source_outside_retained_window'] is True


def test_container_field_gap_is_availability_not_a_state_change(history):
    records = history[1]
    records['old']['result']['observations'][0]['raw_ref']['status'] = {'containerStatuses': [
        {'name': 'worker', 'ready': True, 'restartCount': 0}]}
    records['source']['result']['observations'][0]['raw_ref']['status'] = {'containerStatuses': [
        {'name': 'worker', 'restartCount': 0}]}
    comparison = data(history)['items'][0]['comparison']
    assert 'status.containerStatuses[worker].ready' in comparison['availability']['missing_fields']
    assert all(row['field'] != 'status.containerStatuses.names' for row in comparison['changes'])


def test_malformed_container_status_keeps_identity_but_is_not_comparable(history):
    history[1]['old']['result']['observations'][0]['raw_ref']['status'] = {'containerStatuses': [
        {'name': 'worker', 'ready': 'false'}]}
    result = data(history)
    assert result['total'] == 2
    assert result['items'][1]['comparison']['status'] == 'not_comparable'
    assert 'invalid_state_projection' in result['gaps']


def test_source_summary_only_offers_history_for_direct_uid_observations(history):
    client, records = history
    model, obj = binding(records)
    response = client.get('/api/rca/source/ontology')
    assert response.status_code == 200
    assert response.json()['history_eligible_object_ids'] == [obj['id']]
    assert response.json()['revision'] == model['revision']


def test_budget_and_concurrency_fail_explicitly_without_replacing_history(history, monkeypatch):
    import threading
    import skill_runtime.ontology_history as module
    before = copy.deepcopy(history[1])
    monkeypatch.setattr(module, 'MAX_ENTRIES', 1)
    assert fetch(history).status_code == 503
    assert history[1] == before
    monkeypatch.setattr(module, 'MAX_ENTRIES', 2000)
    monkeypatch.setattr(web, '_ONTOLOGY_HISTORY_READERS', threading.BoundedSemaphore(0))
    assert fetch(history).status_code == 503


def test_oversized_source_refuses_before_full_history_projection(history):
    history[1]['source']['result']['observations'] *= 4097
    assert fetch(history).status_code == 503


def test_non_json_skill_result_is_an_explicit_coverage_gap(history):
    history[1]['old']['result'] = {'agent_evidence': {'agent_evidence_collection': {'commands': [{
        'tool': 'kubectl', 'executed': True, 'success': True,
        'data': 'kind: Deployment\nmetadata:\n  name: worker\n  uid: worker-uid\n'}]}}}
    result = data(history)
    assert result['total'] == 1
    assert 'non_json_tool_result_excluded' in result['gaps']


@pytest.mark.parametrize('value', [True, '3', -1])
def test_invalid_replica_types_cannot_become_comparable_state(history, value):
    history[1]['old']['result']['observations'][0]['raw_ref']['spec']['replicas'] = value
    result = data(history)
    assert result['items'][1]['comparison']['status'] == 'not_comparable'
    assert result['items'][0]['comparison']['status'] == 'baseline'


@pytest.mark.parametrize('case', ['nil_array', 'unreported_condition', 'duplicate_identity'])
def test_endpoint_encoding_availability_and_duplicate_identity(history, case):
    endpoint = {'addresses': ['192.0.2.4'], 'targetRef': {'kind': 'Pod', 'name': 'worker', 'uid': 'pod-uid'},
                'conditions': {}}
    old = fact('2026-10-23T09:00:00Z', kind='EndpointSlice', name='routing', uid='slice-uid',
               addressType='IPv4', endpoints=[copy.deepcopy(endpoint)])
    new = fact(kind='EndpointSlice', name='routing', uid='slice-uid', addressType='IPv4', endpoints=[copy.deepcopy(endpoint)])
    if case == 'nil_array':
        old['raw_ref']['endpoints'] = None
        new['raw_ref']['endpoints'] = []
    elif case == 'unreported_condition':
        new['raw_ref']['endpoints'][0]['conditions']['ready'] = True
    else:
        old['raw_ref']['endpoints'] *= 2
    history[1]['old']['result']['observations'].append(old)
    history[1]['source']['result']['observations'].append(new)
    model = build_investigation(history[1]['source'])
    entity_id = next(o['id'] for o in model['objects'] if o['kind'] == 'EndpointSlice')
    result = data(history, entity_id=entity_id)
    if case == 'nil_array':
        assert result['items'][0]['comparison']['status'] == 'unchanged'
    elif case == 'unreported_condition':
        assert result['items'][0]['comparison']['status'] == 'availability_changed'
        assert result['items'][0]['comparison']['changes'] == []
    else:
        assert result['items'][1]['comparison']['status'] == 'not_comparable'


def test_observation_ids_match_existing_api_non_finite_normalization(history):
    history[1]['old']['result']['observations'][0]['raw_ref']['unused'] = float('nan')
    header = history[0].get('/api/rca/old/ontology').json()
    page = history[0].get('/api/rca/old/ontology/observations', params={'revision': header['revision']}).json()
    result = data(history)
    assert result['items'][1]['observation_id'] == page['items'][0]['id']


@pytest.mark.parametrize('case', ['unknown_cluster', 'oversized_uid', 'missing_run_id', 'mismatched_run_id'])
def test_anchor_rejects_incomplete_identity_and_run_binding(history, case):
    source = history[1]['source']
    if case == 'unknown_cluster':
        source['cluster_id'] = ''  # Raw scope alone must not masquerade as the selected run scope.
    elif case == 'oversized_uid':
        source['result']['observations'][0]['raw_ref']['metadata']['uid'] = 'x' * 513
    elif case == 'missing_run_id':
        source['id'] = None
    else:
        source['id'] = 'another-run'
    assert fetch(history).status_code == 409


@pytest.mark.parametrize('field,value', [('cluster_id', ' east '), ('namespace', ' shop ')])
def test_scope_normalization_matches_existing_investigation_contract(history, field, value):
    history[1]['source'][field] = value
    result = data(history)
    assert [r['run_id'] for r in result['items']] == ['source', 'old']


@pytest.mark.parametrize('case', ['different_name', 'different_kind', 'missing_name', 'missing_kind', 'same_container'])
def test_typed_container_history_requires_same_container_identity(history, case):
    def signal(at, name, kind, count):
        return {'source': 'kubectl get pods -o json', 'source_agent': 'event_agent',
                'signal_type': 'container_terminated', 'timestamp': at,
                'observation': 'Recorded current container termination',
                'raw_ref': {'kind': 'Pod', 'name': 'worker', 'namespace': 'shop',
                            'pod_uid': 'pod-uid', 'cluster_id': 'east', 'observed_at': at,
                            'container': name, 'container_kind': kind,
                            'restart_count': count, 'exit_code': 1}}
    old = signal('2026-10-23T09:00:00Z', 'app', 'application', 5)
    name = 'sidecar' if case == 'different_name' else '' if case == 'missing_name' else 'app'
    kind = 'init' if case == 'different_kind' else '' if case == 'missing_kind' else 'application'
    current = signal('2026-10-23T10:00:00Z', name, kind, 1)
    client, records = history
    records['old']['result']['observations'] = [old]
    records['source']['result']['observations'] = [current]
    header = client.get('/api/rca/source/ontology').json()
    obj = next(o for o in build_investigation(records['source'])['objects'] if o['kind'] == 'Pod')
    response = client.get(f'/api/rca/source/objects/{obj["id"]}/history', params={'revision': header['revision']})
    assert response.status_code == 200, response.text
    result = response.json()
    comparison = result['items'][0]['comparison']
    if case == 'same_container':
        assert comparison['status'] == 'changed'
        assert comparison['previous_run_id'] == 'old'
        assert comparison['changes'] == [{'field': 'restart_count', 'before': 5, 'after': 1}]
    elif case.startswith('missing_'):
        assert comparison['status'] == 'not_comparable'
        assert 'previous_observation_id' not in comparison
    else:
        assert comparison['status'] == 'baseline'
        assert 'previous_observation_id' not in comparison
        assert not comparison.get('changes')
