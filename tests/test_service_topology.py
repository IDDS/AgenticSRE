"""Observed Service bindings must replace name guesses without claiming causation."""
import copy
import json
import asyncio
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents.event_agent import EventAgent
from operational_ontology import build_investigation, ontology_schema, query_investigation
from tools.base_tool import ToolResult
from skill_runtime.service_evidence import projection_command


AT = '2026-10-23T10:00:00Z'


def resource(kind, name, *, uid=None, namespace='shop', labels=None, **fields):
    return {'kind': kind, 'metadata': {'name': name, 'namespace': namespace,
            'uid': uid or name + '-uid', 'resourceVersion': 'v-a',
            'labels': labels or {}}, **fields}


def owner(row):
    return {'kind': row['kind'], 'name': row['metadata']['name'],
            'uid': row['metadata']['uid'], 'controller': True}


def resources():
    deployment = resource('Deployment', 'engine', spec={'replicas': 1,
        'template': {'metadata': {'labels': {'backend': 'a'}}}}, status={'readyReplicas': 1})
    rs = resource('ReplicaSet', 'engine-r7')
    rs['metadata']['ownerReferences'] = [owner(deployment)]
    pod = resource('Pod', 'engine-r7-x', labels={'backend': 'a'},
                   status={'phase': 'Running', 'podIP': '192.0.2.10'})
    pod['metadata']['ownerReferences'] = [owner(rs)]
    service = resource('Service', 'gateway', spec={'selector': {'backend': 'a'}, 'ports': [{'port': 80}]})
    endpoint = resource('EndpointSlice', 'gateway-slice',
        labels={'kubernetes.io/service-name': 'gateway'}, addressType='IPv4',
        endpoints=[{'addresses': ['192.0.2.10'], 'conditions': {'ready': True},
                    'targetRef': {'kind': 'Pod', 'name': pod['metadata']['name'],
                                  'namespace': 'shop', 'uid': pod['metadata']['uid']}}])
    endpoint['metadata']['ownerReferences'] = [owner(service)]
    unrelated = resource('Deployment', 'gateway', spec={'replicas': 0,
        'template': {'metadata': {'labels': {'backend': 'other'}}}})
    return [service, deployment, rs, pod, endpoint, unrelated]


def observation(row, cluster='east'):
    return {'signal_type': 'resource_topology_observed', 'source_agent': 'event_agent',
        'source': 'kubectl structured read', 'timestamp': AT,
        'observation': row['kind'] + '/' + row['metadata']['name'] + ' topology projection',
        'raw_ref': {**copy.deepcopy(row), 'cluster_id': cluster, 'observed_at': AT}}


def model(rows=None, extra=()):
    return build_investigation({'id': 'topology-run', 'cluster_id': 'east', 'namespace': 'shop',
        'result': {'observations': [observation(r) for r in (rows if rows is not None else resources())] + list(extra)},
        'events': []})


def edges(view, kind):
    objects = {r['id']: r for r in view['objects']}
    return [(objects[e['source_id']]['name'], objects[e['target_id']]['name'], e)
            for e in view['relations'] if e['type'] == kind]


def test_schema_and_query_expose_typed_service_relations():
    schema = ontology_schema()
    assert 'EndpointSlice' in schema['object_types']
    assert {'selects_pod', 'matches_workload_template', 'has_endpoint_slice',
            'endpoint_targets_pod', 'controlled_by'} <= set(schema['relation_types'])
    view = model()
    service = next(r for r in view['objects'] if r['kind'] == 'Service')
    page = query_investigation(view, 'relations', entity_id=service['id'])
    assert any(e['type'] == 'selects_pod' for e in page['items'])


def test_actual_selector_and_endpoint_uid_bind_differently_named_backend():
    view = model()
    assert [(a, b) for a, b, _ in edges(view, 'selects_pod')] == [('gateway', 'engine-r7-x')]
    assert [(a, b) for a, b, _ in edges(view, 'matches_workload_template')] == [('gateway', 'engine')]
    assert [(a, b) for a, b, _ in edges(view, 'has_endpoint_slice')] == [('gateway', 'gateway-slice')]
    assert [(a, b) for a, b, _ in edges(view, 'endpoint_targets_pod')] == [('gateway-slice', 'engine-r7-x')]
    assert { (a, b) for a, b, _ in edges(view, 'controlled_by')} == {
        ('engine-r7-x', 'engine-r7'), ('engine-r7', 'engine'), ('gateway-slice', 'gateway')}
    ids = {o['id'] for o in view['observations']}
    for _, _, edge in edges(view, 'selects_pod') + edges(view, 'has_endpoint_slice'):
        assert len(edge['evidence_ids']) >= 2 and set(edge['evidence_ids']) <= ids
        assert edge['is_causal_proof'] is False


def test_selector_can_match_multiple_workloads_without_inventing_one_owner():
    rows = resources()
    rows.append(resource('Deployment', 'another-engine', spec={
        'template': {'metadata': {'labels': {'backend': 'a', 'tier': 'other'}}}}))
    rows.append(resource('Pod', 'another-pod', labels={'backend': 'a'}))
    view = model(rows)
    assert {b for _, b, _ in edges(view, 'matches_workload_template')} == {'engine', 'another-engine'}
    assert {b for _, b, _ in edges(view, 'selects_pod')} == {'engine-r7-x', 'another-pod'}


@pytest.mark.parametrize('spec', [{}, {'selector': {}}, {'type': 'ExternalName', 'externalName': 'example.invalid'}])
def test_selectorless_service_is_not_a_selector_mismatch(spec):
    rows = resources()
    rows[0]['spec'] = spec
    view = model(rows)
    assert not edges(view, 'selects_pod') and not edges(view, 'matches_workload_template')
    assert edges(view, 'has_endpoint_slice')  # An explicit owner is still observable.
    assert not any('selector_mismatch' in str(gap) for gap in view['gaps'])


def test_old_endpoint_target_uid_cannot_bind_a_same_name_rebuilt_pod():
    rows = resources()
    rows[3]['metadata']['uid'] = 'rebuilt-pod-uid'
    view = model(rows)
    assert not edges(view, 'endpoint_targets_pod')
    assert 'endpoint_target_unresolved' in view['gaps']


def test_old_service_owner_uid_cannot_bind_a_rebuilt_service():
    rows = resources()
    rows[0]['metadata']['uid'] = 'rebuilt-service-uid'
    view = model(rows)
    assert not edges(view, 'has_endpoint_slice')
    assert 'endpoint_slice_service_owner_mismatch' in view['gaps']


def test_cross_namespace_and_cluster_candidates_do_not_create_service_edges():
    rows = resources()[:1]
    foreign = resource('Pod', 'foreign', namespace='elsewhere', labels={'backend': 'a'})
    view = model(rows + [foreign], [observation(resource('Pod', 'other-cluster', labels={'backend': 'a'}), 'west')])
    assert not edges(view, 'selects_pod')


def test_missing_uid_and_conflicting_observations_fail_closed_for_new_links():
    rows = resources()
    rows[3]['metadata']['uid'] = ''
    view = model(rows)
    assert not edges(view, 'selects_pod') and not edges(view, 'endpoint_targets_pod')
    rows = resources()
    changed = copy.deepcopy(rows[0])
    changed['metadata']['resourceVersion'] = 'opaque-next'
    changed['spec']['selector'] = {'backend': 'other'}
    view = model(rows, [observation(changed)])
    assert not edges(view, 'selects_pod')
    assert 'conflicting_topology_observations' in view['gaps']


def test_selector_only_committed_projection_does_not_conflict_with_full_topology():
    service, deployment = resources()[:2]
    selector = copy.deepcopy(deployment)
    selector['spec'] = {'selector': {'matchLabels': {'backend': 'a'}}}
    calls = [{'tool': 'kubectl', 'command': projection_command(kind, 'shop', row['metadata']['name']),
              'timestamp': AT, 'cluster_id': 'east', 'executed': True, 'success': True,
              'data': row} for kind, row in [('Service', service), ('Deployment', deployment),
                                              ('DeploymentSelector', selector)]]
    view = build_investigation({'id': 'selector-topology', 'cluster_id': 'east', 'namespace': 'shop',
        'events': [], 'result': {'agent_evidence': {'agent_evidence_collection': {'commands': calls}}}})
    assert len(view['observations']) == 3
    assert [(a, b) for a, b, _ in edges(view, 'matches_workload_template')] == [('gateway', 'engine')]
    assert 'conflicting_topology_observations' not in view['gaps']


@pytest.mark.parametrize('ready,terminating,effective', [(True, False, True), (False, False, False), (None, False, True), (True, True, True)])
def test_endpoint_conditions_are_recorded_without_asserting_business_health(ready, terminating, effective):
    rows = resources()
    rows[0]['spec']['publishNotReadyAddresses'] = True
    rows[4]['endpoints'][0]['conditions'] = {'ready': ready, 'terminating': terminating}
    edge = edges(model(rows), 'endpoint_targets_pod')[0][2]
    assert edge['endpoint_conditions']['ready'] is ready
    assert edge['endpoint_conditions']['effective_ready'] is effective
    assert edge['endpoint_conditions']['terminating'] is terminating
    assert edge['is_causal_proof'] is False
    assert edge['readiness_is_health_proof'] is False


def test_committed_skill_projection_keeps_safe_routing_fields_and_redacts_unrelated_data():
    rows = resources()
    rows[3]['spec'] = {'containers': [{'env': [{'name': 'PRIVATE_FIELD', 'value': 'controlled-private-value'}]}]}
    rows[3]['metadata']['annotations'] = {'private-field': 'controlled-private-value'}
    run = {'id': 'skill-topology', 'cluster_id': 'east', 'namespace': 'shop', 'events': [], 'result': {
        'agent_evidence': {'agent_evidence_collection': {'commands': [{
            'tool': 'kubectl', 'command': 'get svc,pods,replicasets,deployments,endpointslices -o json',
            'timestamp': AT, 'cluster_id': 'east', 'executed': True, 'success': True,
            'data': {'kind': 'List', 'items': rows}}]}}}}
    view = build_investigation(run)
    assert edges(view, 'selects_pod') and edges(view, 'endpoint_targets_pod')
    assert 'controlled-private-value' not in json.dumps(view)
    run['result']['agent_evidence']['agent_evidence_collection']['commands'][0]['outcome_unknown'] = True
    assert not build_investigation(run)['relations']


def collected_state(*, fail_slices=False, mutate=None, return_agent=False):
    rows = resources()
    if mutate:
        mutate(rows)
    calls = []
    def execute(command, namespace='', **kwargs):
        calls.append(command)
        if command == 'get deployment,service,endpoints -o json':
            return ToolResult(True, {'items': [r for r in rows if not isinstance(r, dict)
                or r['kind'] in {'Service', 'Deployment'}]})
        if command == 'get pods,replicasets -o json':
            return ToolResult(True, {'items': [r for r in rows if isinstance(r, dict)
                and r['kind'] in {'Pod', 'ReplicaSet'}]})
        if command == 'get endpointslices.discovery.k8s.io -o json':
            return ToolResult(False, error='controlled-private-backend-detail') if fail_slices else ToolResult(
                True, {'items': [r for r in rows if isinstance(r, dict) and r['kind'] == 'EndpointSlice']})
        return ToolResult(False, error='unexpected fixture request')
    agent = EventAgent(None, SimpleNamespace(get=lambda name: SimpleNamespace(execute=execute) if name == 'kubectl' else None))
    if return_agent:
        return agent, calls
    state = agent._fetch_workload_state('shop')
    return state, agent._build_current_state_evidence_items(state, []), calls


def test_event_collector_retains_healthy_pods_and_service_identity_for_ontology():
    state, facts, calls = collected_state()
    service = next(r for r in state['topology_resources'] if r['kind'] == 'Service')
    assert service['metadata']['uid'] == 'gateway-uid'
    assert len(calls) == 3
    view = build_investigation({'id': 'event-topology', 'cluster_id': 'east', 'namespace': 'shop',
                                'events': [], 'result': {'observations': facts}})
    assert edges(view, 'selects_pod') and edges(view, 'endpoint_targets_pod')
    assert any(f['raw_ref'].get('kind') == 'Pod' for f in facts)


def test_failed_slice_collection_remains_a_gap_and_never_means_zero_endpoints():
    state, facts, _ = collected_state(fail_slices=True)
    assert state['topology_collection']['endpoint_slices']['status'] == 'failed'
    assert 'controlled-private-backend-detail' not in json.dumps(state)
    assert not any(f['signal_type'] == 'service_endpoints_empty' for f in facts)
    assert not edges(build_investigation({'id': 'partial', 'cluster_id': 'east', 'namespace': 'shop',
        'events': [], 'result': {'observations': facts}}), 'has_endpoint_slice')


@pytest.mark.parametrize('case,source', [
    ('healthy', None), ('invalid_resource', 'workload_resources'),
    ('invalid_selector', 'workload_resources'), ('invalid_endpoint', 'endpoint_slices'),
    ('invalid_pod', 'pods_and_controllers'),
    ('invalid_resource_row', 'workload_resources'),
])
def test_incomplete_topology_propagates_to_session_and_strategy(monkeypatch, case, source):
    from diagnostic_strategy import evidence_catalog
    from orchestrator.session import RCASession

    def mutate(rows):
        if case == 'invalid_resource':
            rows.append({'kind': 'Deployment', 'metadata': {}})
        elif case == 'invalid_selector':
            rows[0]['spec']['selector'] = {'backend': ['a']}
        elif case == 'invalid_endpoint':
            rows[4]['endpoints'][0]['conditions']['ready'] = 'invalid'
        elif case == 'invalid_pod':
            rows[3]['metadata']['labels'] = ['invalid']
        elif case == 'invalid_resource_row':
            # Preserve an invalid row returned by the first raw list response.
            rows.append(None)

    agent, _ = collected_state(mutate=mutate, return_agent=True)
    agent.summarize = False
    monkeypatch.setattr(agent, '_fetch_live_pods', lambda ns: [])
    monkeypatch.setattr(agent, '_fetch_events', lambda *args, **kwargs: ('', ''))
    monkeypatch.setattr(agent, '_fetch_node_status', lambda: '')
    monkeypatch.setattr(agent, '_fetch_problem_pods', lambda ns: [])
    monkeypatch.setattr(agent, '_fetch_network_objects', lambda *args: {})
    payload = asyncio.run(agent.analyze('read current workload', 'shop'))
    collected = payload['workload_state']
    session = RCASession(namespace='shop')
    session.record_evidence('event_agent', payload)
    assert session.observation_history, 'Partial collection must preserve successfully observed facts'
    catalog = evidence_catalog(session.evidence, session.observation_history)
    if source:
        assert collected['topology_collection'][source]['status'] == 'partial'
        assert any(e['source'] == source for e in payload['collection_errors'])
        assert session.collection_history[-1]['status'] == 'partial'
        assert catalog == [], 'Partial collection cannot provide positive evidence for early stopping'
        view = build_investigation({'id': 'partial-propagation', 'cluster_id': 'east', 'namespace': 'shop',
            'events': [], 'result': {'observations': session.observation_history,
                                   'collection_history': session.collection_history}})
        assert 'collection_incomplete' in view['gaps']
    else:
        assert payload['collection_errors'] == []
        assert session.collection_history[-1]['status'] == 'collected'
        assert catalog


def test_manual_slice_label_is_not_promoted_to_service_instance_ownership():
    rows = resources()
    rows[0]['spec'] = {}
    rows[4]['metadata']['ownerReferences'] = []
    view = model(rows)
    assert not edges(view, 'has_endpoint_slice')
    edge = edges(view, 'endpoint_slice_label_association')[0][2]
    assert edge['service_instance_verified'] is False
    assert edges(view, 'endpoint_targets_pod')


@pytest.mark.parametrize('field,value', [('ready', 'false'), ('ready', 0), ('terminating', 1)])
def test_invalid_condition_types_cannot_create_ready_endpoint_links(field, value):
    rows = resources()
    rows[4]['endpoints'][0]['conditions'] = {field: value}
    view = model(rows)
    assert not edges(view, 'endpoint_targets_pod')
    assert 'topology_projection_incomplete' in view['gaps']


@pytest.mark.parametrize('mutate', [
    lambda rows: rows[4]['endpoints'][0]['targetRef'].update(uid=[]),
    lambda rows: rows[4]['endpoints'][0].update(conditions=[]),
    lambda rows: rows[4]['metadata']['ownerReferences'][0].update(uid=[]),
    lambda rows: rows[0]['spec'].update(selector={'backend': ['a']}),
])
def test_malformed_topology_does_not_raise_or_generate_verified_slice_links(mutate):
    rows = resources()
    mutate(rows)
    view = model(rows)
    assert not edges(view, 'has_endpoint_slice')
    assert 'topology_projection_incomplete' in view['gaps']


def test_duplicate_records_stay_deterministic_and_historical_rows_do_not_join():
    rows = resources()
    record = observation(rows[3])
    record['raw_ref']['previous'] = True
    view = model([rows[0]], [record])
    assert not edges(view, 'selects_pod')
    view = model(rows, [observation(rows[0]), observation(rows[4])])
    assert len(edges(view, 'has_endpoint_slice')) == 1
    assert view == model(rows, [observation(rows[0]), observation(rows[4])])


def test_oversize_slice_projection_remains_incomplete_not_an_empty_ready_set():
    rows = resources()
    rows[4]['endpoints'] *= 1001
    view = model(rows)
    assert not edges(view, 'has_endpoint_slice')
    assert 'topology_projection_incomplete' in view['gaps']


def test_service_relations_are_exposed_by_the_existing_revision_bound_api(monkeypatch):
    import web_app.app as web
    record = {'id': 'topology-api', 'cluster_id': 'east', 'namespace': 'shop',
              'status': 'completed', 'events': [],
              'result': {'observations': [observation(r) for r in resources()]}}
    monkeypatch.setitem(web._state, 'rca_runs', {record['id']: record})
    summary = asyncio.run(web.rca_ontology(record['id']))
    page = asyncio.run(web.rca_ontology_collection(record['id'], 'relations',
        revision=summary['revision'], offset=0, limit=100))
    assert page['total'] == summary['counts']['relations']
    assert {'selects_pod', 'has_endpoint_slice', 'endpoint_targets_pod'} <= {r['type'] for r in page['items']}
    assert web._build_report(record)['ontology']['revision'] == summary['revision']
    with pytest.raises(web.HTTPException) as error:
        asyncio.run(web.rca_ontology_collection(record['id'], 'relations',
            revision='stale-revision', offset=0, limit=100))
    assert error.value.status_code == 409


def test_nil_endpoint_array_is_an_explicit_empty_slice_not_a_projection_failure():
    rows = resources()
    rows[4]['endpoints'] = None  # Real Kubernetes responses can encode a nil Go slice as null.
    view = model(rows)
    assert edges(view, 'has_endpoint_slice')
    assert not edges(view, 'endpoint_targets_pod')
    assert 'topology_projection_incomplete' not in view['gaps']
    del rows[4]['endpoints']
    missing = model(rows)
    assert not edges(missing, 'has_endpoint_slice')
    assert 'topology_projection_incomplete' in missing['gaps']


def test_nil_endpoint_array_survives_committed_skill_projection():
    rows = resources()
    rows[4]['endpoints'] = None
    record = {'id': 'nil-slice-skill', 'cluster_id': 'east', 'namespace': 'shop', 'events': [],
        'result': {'agent_evidence': {'agent_evidence_collection': {'commands': [{
            'tool': 'kubectl', 'command': 'get svc,endpointslices -o json',
            'cluster_id': 'east', 'timestamp': AT, 'success': True, 'executed': True,
            'data': {'kind': 'List', 'items': rows}}]}}}}
    view = build_investigation(record)
    assert edges(view, 'has_endpoint_slice')
    assert 'topology_projection_incomplete' not in view['gaps']


def test_shared_projection_module_is_included_by_the_application_docker_copy_rules():
    import operational_ontology
    root = Path(__file__).resolve().parents[1]
    module = sys.modules[operational_ontology.project_resource.__module__]
    relative = Path(module.__file__).resolve().relative_to(root).as_posix()
    sources = [source for line in (root / 'Dockerfile').read_text().splitlines()
               if line.startswith('COPY ') for source in shlex.split(line)[1:-1]]
    assert any(relative == source or source.endswith('/') and relative.startswith(source)
               for source in sources), 'Shared runtime projection is absent from application image COPY rules'
