"""Scoped collection regressions; fixtures are not live fault injection evidence."""
import copy
import importlib.util
import json
import re
import shlex
from pathlib import Path

import pytest


def resource(kind, name, **fields):
    return {'kind': kind, 'metadata': {'name': name, 'namespace': 'shop',
        'uid': name + '-uid', 'resourceVersion': 'opaque-a'}, **fields}


def objects():
    return [resource('Service', 'gateway', spec={'selector': {'backend': 'engine'}}),
        resource('Endpoints', 'gateway', subsets=[]),
        resource('Deployment', 'engine', spec={'replicas': 0,
            'template': {'metadata': {'labels': {'backend': 'engine'}}}}, status={}),
        resource('Deployment', 'gateway', spec={'replicas': 0,
            'template': {'metadata': {'labels': {'backend': 'unrelated'}}}}, status={})]


class Cluster:
    def __init__(self, rows=None):
        self.rows = copy.deepcopy(rows if rows is not None else objects())
        self.commands = []

    def execute(self, tool, *, command, namespace):
        assert tool == 'kubectl' and namespace == 'shop'
        self.commands.append(command)
        args = shlex.split(command)
        kinds = {'services': 'Service', 'service': 'Service', 'endpoints': 'Endpoints',
                 'deployments': 'Deployment', 'deployment': 'Deployment',
                 'pods': 'Pod', 'pod': 'Pod', 'replicaset': 'ReplicaSet'}
        target = args[1].split('/')
        kind = kinds.get(target[0])
        name = target[1] if len(target) == 2 else None
        rows = [r for r in self.rows if r['kind'] == kind and (not name or r['metadata']['name'] == name)]
        if '-l' in args:
            labels = dict(p.split('=', 1) for p in args[args.index('-l')+1].split(','))
            rows = [r for r in rows if all(r['metadata'].get('labels', {}).get(k) == v for k, v in labels.items())]
        if name and not rows:
            return {'executed': True, 'success': False, 'error': 'NotFound', 'data': ''}
        fmt = args[args.index('-o')+1] if '-o' in args else ''
        if fmt.startswith('jsonpath-as-json='):
            expr = fmt.split('=', 1)[1]
            fields = re.findall(r'\{(\.[a-zA-Z.]+)\}', expr)
            cells = []
            for row in rows:
                for field in fields:
                    current = row
                    for part in field[1:].split('.'):
                        current = current.get(part) if isinstance(current, dict) else None
                    cells.append([] if current is None else [current])
            if '{"service-evidence/v1:end"}' in expr:
                cells.append(['service-evidence/v1:end'])
            body = '\n'.join(json.dumps(cell) for cell in cells)
        else:
            body = json.dumps(rows[0] if name and rows else {'kind': 'List', 'items': rows})
        return {'executed': True, 'success': True, 'data': body, 'sample': body}


def collect(cluster, commands=None):
    path = Path(__file__).parents[1] / 'skill_runtime/skills/generic_evidence_collection/skill.py'
    spec = importlib.util.spec_from_file_location('scoped_collector', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.diagnose({'payload': {'namespace': 'shop', 'cluster_id': 'east',
        'service': 'gateway', 'message': 'HTTP 502', 'collect_service_topology': True,
        'investigation_commands': ['kubectl get pods -l <selector> -o json'] if commands is None else commands}, 'tools': cluster})


def test_zero_pods_discovers_actual_template_and_reads_exact_deployment():
    cluster = Cluster()
    result = collect(cluster)
    assert any(shlex.split(c)[:2] == ['get', 'deployment/engine'] for c in cluster.commands)
    assert not any('deployment/gateway' in c for c in cluster.commands)
    assert result['service_scope']['candidate_workloads'] == ['engine']
    assert result['service_scope']['status'] == 'collected'
    assert result['service_scope']['is_causal_proof'] is False
    assert len(cluster.commands) <= 8


@pytest.mark.parametrize('prefix', [[], ['kubectl get pods -l <selector> -o json']])
def test_valid_model_checks_are_retained_within_the_shared_call_budget(prefix):
    cluster = Cluster()
    collect(cluster, prefix + ['kubectl get events -n shop', 'kubectl logs deployment/engine --tail=40'])
    assert 'get events -n shop' in cluster.commands
    assert 'logs deployment/engine --tail=40' in cluster.commands
    assert len(cluster.commands) == 8


def test_multiple_matching_templates_remain_ambiguous():
    rows = objects()
    rows.append(resource('Deployment', 'engine-two', spec={'replicas': 0,
        'template': {'metadata': {'labels': {'backend': 'engine'}}}}))
    result = collect(Cluster(rows))
    assert result['service_scope']['status'] == 'ambiguous'
    assert result['service_scope']['candidate_workloads'] == ['engine', 'engine-two']


@pytest.mark.parametrize('commands', [None, []])
@pytest.mark.parametrize('spec', [{}, {'selector': {}}, {'type': 'ExternalName'}])
def test_selectorless_or_external_service_does_not_scan_namespace(spec, commands):
    rows = objects()
    rows[0]['spec'] = spec
    cluster = Cluster(rows)
    result = collect(cluster, commands)
    assert result['service_scope']['status'] == 'unsupported_selector'
    assert not any(shlex.split(c)[1] in {'pods', 'deployments'} for c in cluster.commands)


def test_service_uid_or_selector_drift_is_an_explicit_gap():
    class Drift(Cluster):
        def execute(self, *a, **kw):
            if sum('service/gateway' in c for c in self.commands):
                self.rows[0]['metadata']['uid'] = 'replacement'
            return super().execute(*a, **kw)
    result = collect(Drift())
    assert result['service_scope']['status'] == 'scope_changed'


def test_failed_inventory_never_means_no_workload():
    class Forbidden(Cluster):
        def execute(self, *a, **kw):
            if shlex.split(kw['command'])[1] == 'deployments':
                return {'executed': True, 'success': False, 'error': 'Forbidden'}
            return super().execute(*a, **kw)
    result = collect(Forbidden())
    assert result['service_scope']['status'] == 'incomplete'
    assert 'deployment_inventory_unavailable' in result['service_scope']['gaps']


def test_unavailable_service_keeps_legacy_checks_but_cannot_bind_identity():
    class Unavailable(Cluster):
        def execute(self, *a, **kw):
            if 'service/gateway' in kw['command']:
                self.commands.append(kw['command'])
                return {'executed': True, 'success': False, 'error': 'Forbidden'}
            return super().execute(*a, **kw)
    cluster = Unavailable()
    result = collect(cluster, [])
    assert result['service_scope']['status'] == 'incomplete'
    assert 'service_unavailable' in result['service_scope']['gaps']
    assert any(c.startswith('get pods ') for c in cluster.commands)
    from skill_runtime.service_evidence import bind_root_entity
    assert bind_root_entity({}, [], result['service_scope'])[0] is None


def projected(kind='Deployment', name='engine', row=None):
    from skill_runtime.service_evidence import decode_projection, projection_command
    cluster = Cluster([row or next(r for r in objects() if r['kind'] == kind)])
    command = projection_command(kind, 'shop', name)
    wire = cluster.execute('kubectl', command=command, namespace='shop')['data']
    return command, wire, decode_projection(command, 'shop', wire)


def test_projection_preserves_missing_fields_without_assuming_ready_zero():
    command, wire, row = projected()
    assert row['spec']['replicas'] == 0
    assert 'readyReplicas' not in row.get('status', {})
    assert row['metadata']['uid'] == 'engine-uid'
    from skill_runtime.kubectl_safety import readonly_kubectl_args
    assert readonly_kubectl_args(command, 'shop')[0] == 'get'


@pytest.mark.parametrize('mutation', ['truncated', 'wrong_namespace', 'negative', 'bool', 'missing_uid'])
def test_malformed_projection_is_rejected(mutation):
    from skill_runtime.service_evidence import decode_projection
    row = objects()[2]
    if mutation == 'wrong_namespace': row['metadata']['namespace'] = 'peer'
    if mutation == 'missing_uid': row['metadata'].pop('uid')
    if mutation == 'negative': row['spec']['replicas'] = -1
    if mutation == 'bool': row['spec']['replicas'] = False
    # Build the wire directly; projected() intentionally validates its result.
    from skill_runtime.service_evidence import projection_command
    command = projection_command('Deployment', 'shop', 'engine')
    wire = Cluster([row]).execute('kubectl', command=command, namespace='shop')['data']
    if mutation == 'truncated': wire = wire[:-1]
    with pytest.raises(ValueError): decode_projection(command, 'shop', wire)


def test_projection_excludes_sensitive_specs_and_annotations():
    row = objects()[2]
    row['metadata']['annotations'] = {'private': 'not-for-evidence'}
    row['spec']['template']['spec'] = {'containers': [{'env': [{'name': 'PASSWORD', 'value': 'not-for-evidence'}]}]}
    _, wire, result = projected(row=row)
    assert 'not-for-evidence' not in wire + json.dumps(result)


def test_inventory_over_limit_and_partial_rows_fail_closed():
    from skill_runtime.service_evidence import decode_projection, projection_command, MAX_ROWS
    command = projection_command('Deployment', 'shop')
    _, wire, _ = projected()
    with pytest.raises(ValueError): decode_projection(command, 'shop', wire * (MAX_ROWS+1))
    with pytest.raises(ValueError): decode_projection(command, 'shop', wire + '[]')


def test_large_inventory_sample_remains_valid_json_and_marks_omission():
    from skill_runtime.service_evidence import evidence_sample, PROJECTION
    data = json.dumps({'kind': 'List', 'projection': PROJECTION, 'items': objects()*40})
    sample = json.loads(evidence_sample(data, 1500))
    assert sample['items_omitted'] and sample['observed_item_count'] == 160
    assert 'items' not in sample


def test_truncation_at_a_complete_inventory_row_is_not_a_complete_inventory():
    from skill_runtime.service_evidence import decode_projection, projection_command
    command = projection_command('Deployment', 'shop')
    cluster = Cluster()
    wire = cluster.execute('kubectl', command=command, namespace='shop')['data']
    # Drop the second row and footer: the remaining cells are exactly one row.
    cells = [json.loads(line) for line in wire.splitlines()]
    prefix = '\n'.join(json.dumps(c) for c in cells[:9])
    with pytest.raises(ValueError, match='end_marker'): decode_projection(command, 'shop', prefix)


def test_large_healthy_endpoint_sample_does_not_trigger_empty_endpoint_skill():
    from skill_runtime.service_evidence import evidence_sample
    from skill_runtime.executor import SkillExecutor
    row = resource('Endpoints', 'gateway', subsets=[{'addresses': [{'ip': '192.0.2.1'}]*800}])
    command, _, data = projected('Endpoints', 'gateway', row)
    sample = evidence_sample(json.dumps(data), 1200)
    assert json.loads(sample)['ready_address_count'] == 800
    assert json.loads(sample)['subsets'][0]['addresses']
    runtime = SkillExecutor.from_dir(Path(__file__).parents[1] / 'skill_runtime/skills')
    payload = {'namespace': 'shop', 'cluster_id': 'east', 'service': 'gateway', 'message': 'Check service',
        'agent_evidence': {'agent_evidence_collection': {'commands': [{
            'command': command, 'namespace': 'shop', 'cluster_id': 'east', 'success': True,
            'executed': True, 'sample': sample, 'data': json.dumps(data)}]}}}
    output = runtime.run('k8s.service_unavailable', payload, stages=['detect', 'diagnose'], dry_run=True).to_dict()
    assert output['stages']['detect']['status'] == 'skipped'


def test_ready_endpoint_addresses_survive_ontology_projection():
    from operational_ontology import committed_tool_observations
    row = resource('Endpoints', 'gateway', subsets=[{'addresses': [{'ip': '192.0.2.1'}]}])
    command, wire, result = projected('Endpoints', 'gateway', row)
    observations = committed_tool_observations({'command': command, 'success': True, 'executed': True,
        'data': json.dumps(result), 'cluster_id': 'east'})
    assert observations[0]['raw_ref']['subsets'][0]['addresses'][0]['ip'] == '192.0.2.1'


def test_healthy_pod_uses_observed_selector_and_uid_owner_chain():
    rows = objects()
    rows[1]['subsets'] = [{'addresses': [{'ip': '192.0.2.1'}]}]
    rows[2]['spec']['replicas'] = 1
    rows[2]['status'] = {'readyReplicas': 1, 'availableReplicas': 1}
    rs = resource('ReplicaSet', 'engine-r7')
    rs['metadata']['ownerReferences'] = [{'kind': 'Deployment', 'name': 'engine', 'uid': 'engine-uid', 'controller': True}]
    pod = resource('Pod', 'engine-r7-x', status={'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}]})
    pod['metadata'].update(labels={'backend': 'engine'}, ownerReferences=[{
        'kind': 'ReplicaSet', 'name': 'engine-r7', 'uid': 'engine-r7-uid', 'controller': True}])
    cluster = Cluster(rows + [pod, rs])
    result = collect(cluster)
    assert result['service_scope']['selected_pod_count'] == 1
    assert result['service_scope']['status'] == 'collected'
    assert any('replicaset/engine-r7' in c for c in cluster.commands)
    assert any(shlex.split(c)[shlex.split(c).index('-l')+1] == 'backend=engine'
               for c in cluster.commands if '-l' in shlex.split(c))
    # No model commands: retain targeted application logs and UID-scoped
    # events instead of replacing them with another namespace inventory.
    cluster = Cluster(rows + [pod, rs])
    collect(cluster, [])
    assert any(c.startswith('logs pod/engine-r7-x ') for c in cluster.commands)
    assert any('--field-selector=involvedObject.uid=engine-uid' in c for c in cluster.commands)
    assert len(cluster.commands) == 9
    cluster = Cluster(rows + [pod, rs])
    collect(cluster, ['kubectl get events -n shop', 'kubectl logs pod/engine-r7-x --tail=40'])
    assert any(c.startswith('logs pod/engine-r7-x ') for c in cluster.commands)
    assert len(cluster.commands) == 9


def entity_call():
    command, _, result = projected()
    return {'command': command, 'namespace': 'shop', 'cluster_id': 'east',
            'success': True, 'executed': True, 'data': json.dumps(result)}


def test_root_identity_requires_full_catalog_match_and_keeps_review_required():
    from skill_runtime.service_evidence import observed_entity_catalog, bind_root_entity
    catalog = observed_entity_catalog([entity_call()], 'east', 'shop')
    bound, validation = bind_root_entity(catalog[0], catalog, {'status': 'collected'})
    assert bound['uid'] == 'engine-uid'
    assert validation == {'status': 'identity_bound', 'is_causal_proof': False, 'independent_review_required': True}
    for key in ('uid', 'resource_version', 'cluster_id', 'namespace', 'name'):
        spoof = dict(catalog[0], **{key: 'invented'})
        assert bind_root_entity(spoof, catalog)[0] is None
    assert bind_root_entity(None, catalog)[0] is None
    assert bind_root_entity({'name': 'engine'}, catalog)[0] is None
    assert bind_root_entity(catalog[0], catalog, {'status': 'ambiguous'})[0] is None


@pytest.mark.parametrize('field,value', [('executed', False), ('success', False), ('outcome_unknown', True),
    ('namespace', 'peer'), ('cluster_id', 'west')])
def test_failed_unknown_or_cross_scope_calls_cannot_bind(field, value):
    from skill_runtime.service_evidence import observed_entity_catalog
    call = entity_call()
    call[field] = value
    assert observed_entity_catalog([call], 'east', 'shop') == []


def test_conflicting_uid_or_resource_version_cannot_be_resolved_by_order():
    from skill_runtime.service_evidence import observed_entity_catalog, bind_root_entity
    original = entity_call()
    for key in ('uid', 'resourceVersion'):
        other = copy.deepcopy(original)
        data = json.loads(other['data']); data['metadata'][key] = 'another'
        other['data'] = json.dumps(data)
        for records in ([original, other], [other, original]):
            catalog = observed_entity_catalog(records, 'east', 'shop')
            assert bind_root_entity(catalog[0], catalog)[1]['reason'] == 'conflicting_entity_observations'


def test_host_canonicalizes_before_truncation_and_rejects_unknown_output():
    from orchestrator.skill_agent import SkillAgent
    command, wire, row = projected()
    agent = SkillAgent(registry=None, skills_dir=Path(__file__).parents[1] / 'skill_runtime/skills',
        kubectl_runner=lambda *a: {'success': True, 'stdout': wire})
    captured = agent._run_kubectl(command, 'shop', kubectl_available=True, payload={'cluster_id': 'east'})
    assert captured['success'] and json.loads(captured['data'])['metadata'] == row['metadata']
    agent.kubectl_runner = lambda *a: {'success': True, 'stdout': wire[:-1]}
    captured = agent._run_kubectl(command, 'shop', kubectl_available=True)
    assert not captured['success'] and captured['error'] == 'invalid_service_projection' and not captured['data']
    agent.kubectl_runner = lambda *a: {'success': True, 'outcome_unknown': True, 'stdout': wire}
    assert not agent._run_kubectl(command, 'shop', kubectl_available=True)['success']


def test_reasoning_and_result_preserve_explicit_entity_selection():
    from orchestrator.skill_agent import SkillAgent, SkillAgentResult
    from skill_runtime.service_evidence import observed_entity_catalog
    agent = SkillAgent(registry=None, skills_dir=Path(__file__).parents[1] / 'skill_runtime/skills')
    entity = observed_entity_catalog([entity_call()], 'east', 'shop')[0]
    normalized = agent._normalize_reasoning_result({'root_cause': 'engine replicas 0', 'root_entity': entity})
    assert normalized['root_entity'] == entity
    result = SkillAgentResult('completed', 'query', 'shop', 0, {'diagnosis': normalized}, {}, {})
    assert result.to_dict()['root_entity'] == entity
    assert agent._normalize_reasoning_result({'root_cause': 'Deployment/engine uid invented'}).get('root_entity') is None


def test_final_reasoning_binds_identity_but_cannot_authorize_action():
    from orchestrator.skill_agent import SkillAgent, SkillAgentResult, SkillAgentRequest
    from skill_runtime.service_evidence import observed_entity_catalog
    from operational_ontology import build_investigation, action_context
    call = entity_call()
    entity = observed_entity_catalog([call], 'east', 'shop')[0]
    agent = SkillAgent(registry=None, skills_dir=Path(__file__).parents[1] / 'skill_runtime/skills')
    prompts = []
    def reasoning(phase, messages, **kwargs):
        prompts.append(json.loads(messages[1]['content']))
        return {'root_cause': 'Deployment/engine 的期望副本为 0，具体触发机制待调查',
                'confidence': 0.8, 'root_entity': entity, 'evidence': ['实际 Deployment 只读结果']}
    agent._call_llm_json = reasoning
    evidence = {'agent_evidence_collection': {'commands': [call], 'service_scope': {'status': 'collected'}}}
    result = {'diagnosis': {'root_cause': '待诊断', 'confidence': 0.6}, 'skill_runtime': {'runs': []}}
    agent.reason_about_result(SkillAgentRequest('HTTP 502', 'shop', {'payload': {'cluster_id': 'east'}}),
        hypotheses=[], verification_plan={}, evidence=evidence, skill_result=result)
    assert result['diagnosis']['root_entity']['uid'] == 'engine-uid'
    assert prompts[0]['observed_entity_catalog'][0]['uid'] == 'engine-uid'
    exported = SkillAgentResult('completed', 'HTTP 502', 'shop', 0, result, evidence, {}).to_dict()
    view = build_investigation({'id': 'test', 'status': 'completed', 'cluster_id': 'east',
        'namespace': 'shop', 'result': exported})
    gate = action_context(view)
    assert gate['target']['uid'] == 'engine-uid'
    assert not gate['recorded_conditions_met']
    assert 'independent_review_required' in gate['blockers']


@pytest.mark.parametrize('cluster_key', ['cluster_id', 'target_cluster_id'])
def test_agent_chain_commits_scoped_trace_and_binds_explicit_root(cluster_key):
    from orchestrator.skill_agent import SkillAgent, SkillAgentRequest
    class Model:
        def json_chat(self, messages, **kwargs):
            content = messages[-1]['content']
            if '"observed_entity_catalog"' in content:
                data = json.loads(content)
                entity = next(e for e in data['observed_entity_catalog'] if e['kind'] == 'Deployment')
                return {'root_cause': 'Deployment/engine 期望副本为 0，具体触发机制待调查',
                        'root_entity': entity, 'confidence': 0.8, 'reasoning_chain': '实际命名对象读取'}
            return {'hypotheses': [{'id': 'H1', 'description': 'Service 后端不可用', 'confidence': 0.7,
                'fault_type': 'service_unavailable', 'expected_evidence': ['检查实际端点']}],
                'skill_calls': [{'skill_id': 'k8s.service_unavailable', 'stages': ['detect', 'diagnose'],
                    'commands': ['kubectl get pods -l <selector> -o json']}]}
    cluster = Cluster()
    agent = SkillAgent(registry=None, skills_dir=Path(__file__).parents[1] / 'skill_runtime/skills',
        llm_client=Model(), kubectl_runner=lambda command, ns, payload:
            cluster.execute('kubectl', command=command, namespace=ns))
    result = agent.run(SkillAgentRequest('gateway GET /api/items HTTP 502', 'shop',
        {'domain': 'k8s', 'stages': ['detect', 'diagnose'], 'payload': {
            cluster_key: 'east', 'namespace': 'shop', 'service': 'gateway'}}))
    exported = result.to_dict()
    assert result.status == 'completed'
    assert exported['root_entity']['uid'] == 'engine-uid'
    records = exported['agent_evidence']['agent_evidence_collection']['commands']
    assert all(c['cluster_id'] == 'east' for c in records)
    assert all(c['timestamp'] for c in records)
    assert exported['agent_evidence']['agent_evidence_collection']['service_scope']['status'] == 'collected'
    assert any(c['command'].startswith('get deployment/engine ') for c in records)
