"""Guarded coordinator behavior; these tests do not represent live faults."""
import asyncio
import copy
import json
import time
from threading import Event
from types import SimpleNamespace

import pytest
import web_app.app as web


@pytest.fixture
def scenario(monkeypatch, tmp_path):
    run = {'id': 'rca-one', 'status': 'completed', 'started_at': time.time(), 'guarded_auto_recovery': True}
    cfg = SimpleNamespace(remediation=SimpleNamespace(auto_loop_enabled=True, auto_scale_targets=[{
        'cluster_id': 'east', 'namespace': 'shop', 'deployment': 'engine', 'service': 'gateway', 'replicas': 1}]))
    target = {'id': 'd1', 'kind': 'Deployment', 'name': 'engine', 'namespace': 'shop',
              'cluster_id': 'east', 'uid': 'uid-a'}
    service = {'id': 's1', 'kind': 'Service', 'name': 'gateway', 'identity_status': 'uid_bound', 'uid': 'svc-a'}
    model = {'revision': 'rev1', 'objects': [target, service], 'gaps': [],
             'observations': [{'entity_id': 's1', 'resource_version': 'svc-rv', 'temporality': 'current'}], 'relations': [
        {'type': 'matches_workload_template', 'source_id': 's1', 'target_id': 'd1'}]}
    action = {'source': 'kb', 'executable': True, 'command': 'kubectl scale deployment/engine -n shop --replicas=1',
              'rollback_command': 'kubectl scale deployment/engine -n shop --replicas=0'}
    calls = []
    original_execute = web.heal_execute
    monkeypatch.setattr(web, '_get_config', lambda: cfg)
    monkeypatch.setattr(web, '_HEAL_ARTIFACT_DIR', tmp_path)
    monkeypatch.setitem(web._state, 'rca_runs', {'rca-one': run})
    monkeypatch.setattr(web, 'build_investigation', lambda run: copy.deepcopy(model))
    monkeypatch.setattr(web, 'action_context', lambda model: {'recorded_conditions_met': True, 'target': target})
    monkeypatch.setattr(web, '_bind_ontology_heal_input', lambda body, execution=False, guarded_skill=False: None)
    monkeypatch.setattr(web, '_build_heal_suggestions', lambda body: {'suggestions': [action]})
    monkeypatch.setattr(web, '_verify_ontology_action', lambda ctx, commands: {'spec': {'replicas': 0,
        'template': {'metadata': {'labels': {'app': 'engine'}}}}})
    monkeypatch.setattr(web, '_ontology_live_resource', lambda target: {'ok': True, 'json': {
        'metadata': {'uid': 'svc-a', 'resourceVersion': 'svc-rv'}, 'spec': {'selector': {'app': 'engine'}}}})
    async def execute(request):
        body = await request.json()
        calls.append(body)
        if body['dry_run']:
            return {'id': 'preview-1', 'status': 'dry_run'}
        return {'id': 'heal-1', 'status': 'succeeded', 'success': True,
                'verification': {'status': 'verified', 'recovered': True}}
    monkeypatch.setattr(web, 'heal_execute', execute)
    return SimpleNamespace(run=run, cfg=cfg, target=target, model=model, action=action, calls=calls,
                           tmp=tmp_path, original_execute=original_execute)


def test_coordinator_selects_recipe_previews_then_uses_existing_auto_gate_once(scenario):
    first = asyncio.run(web._guarded_auto_recovery('rca-one'))
    second = asyncio.run(web._guarded_auto_recovery('rca-one'))
    assert first == second and first['status'] == 'succeeded'
    assert len(scenario.calls) == 2
    preview, real = scenario.calls
    assert preview['dry_run'] is True and real['dry_run'] is False and real['auto_execute'] is True
    assert real['ontology_proposal_id'] == 'preview-1' and 'approved' not in real
    assert preview['commands'] == real['commands'] == [scenario.action['command']]


@pytest.mark.parametrize('change', ['disabled', 'old', 'unguarded', 'ambiguous', 'gap', 'policy', 'replicas', 'no_action', 'service_changed'])
def test_missing_safety_prerequisites_do_not_dispatch(scenario, monkeypatch, change):
    if change == 'disabled': scenario.cfg.remediation.auto_loop_enabled = False
    if change == 'old': scenario.run['started_at'] -= 601
    if change == 'unguarded': scenario.run['guarded_auto_recovery'] = False
    if change == 'ambiguous': scenario.model['relations'].clear()
    if change == 'gap': scenario.model['gaps'].append('collection_incomplete')
    if change == 'policy': scenario.cfg.remediation.auto_scale_targets.clear()
    if change == 'replicas': monkeypatch.setattr(web, '_verify_ontology_action', lambda *args: {'spec': {'replicas': 1,
        'template': {'metadata': {'labels': {'app': 'engine'}}}}})
    if change == 'no_action': scenario.action['source'] = 'diagnosis_dynamic'
    if change == 'service_changed': monkeypatch.setattr(web, '_ontology_live_resource', lambda *args: {'ok': True,
        'json': {'metadata': {'uid': 'different', 'resourceVersion': 'svc-rv'}, 'spec': {'selector': {'app': 'engine'}}}})
    result = asyncio.run(web._guarded_auto_recovery('rca-one'))
    assert result['status'] in ('blocked', 'disabled')
    assert scenario.calls == []


def test_unknown_dispatch_is_not_retried(scenario, monkeypatch):
    async def execute(request):
        body = await request.json()
        scenario.calls.append(body)
        if body['dry_run']: return {'id': 'preview-1', 'status': 'dry_run'}
        raise TimeoutError('outcome unknown')
    monkeypatch.setattr(web, 'heal_execute', execute)
    first = asyncio.run(web._guarded_auto_recovery('rca-one'))
    second = asyncio.run(web._guarded_auto_recovery('rca-one'))
    assert first['status'] == second['status'] == 'outcome_unknown'
    assert len(scenario.calls) == 2


def test_gate_rejection_records_blockers_and_never_retries(scenario, monkeypatch):
    async def execute(request):
        body = await request.json()
        scenario.calls.append(body)
        if body['dry_run']: return {'id': 'preview-1', 'status': 'dry_run'}
        raise web.HTTPException(400, {'auto_execution_gate': {'blockers': ['insufficient_history']}})
    monkeypatch.setattr(web, 'heal_execute', execute)
    first = asyncio.run(web._guarded_auto_recovery('rca-one'))
    second = asyncio.run(web._guarded_auto_recovery('rca-one'))
    assert first['status'] == second['status'] == 'blocked'
    assert first['blockers'] == ['insufficient_history']
    assert len(scenario.calls) == 2


def test_interrupted_claim_never_dispatches_after_restart(scenario):
    (scenario.tmp/'auto-rca-one.json').write_text(json.dumps({'source_run_id': 'rca-one', 'status': 'dispatching'}))
    result = asyncio.run(web._guarded_auto_recovery('rca-one'))
    assert result['status'] == 'outcome_unknown'
    assert scenario.calls == []


def test_status_read_remains_available_after_disable_and_expiry(scenario):
    (scenario.tmp/'auto-rca-one.json').write_text(json.dumps({'source_run_id': 'rca-one', 'status': 'blocked'}))
    scenario.cfg.remediation.auto_loop_enabled = False
    scenario.run['started_at'] -= 1000
    result = asyncio.run(web.rca_guarded_auto_heal_status('rca-one'))
    assert result['status'] == 'blocked'
    assert scenario.calls == []


def test_service_changed_during_preview_prevents_dispatch(scenario, monkeypatch):
    reads = []
    def read_service(target):
        reads.append(target)
        return {'ok': True, 'json': {'metadata': {'uid': 'svc-a',
            'resourceVersion': 'svc-rv' if len(reads) == 1 else 'changed'},
            'spec': {'selector': {'app': 'engine'}}}}
    monkeypatch.setattr(web, '_ontology_live_resource', read_service)
    result = asyncio.run(web._guarded_auto_recovery('rca-one'))
    assert result['reason'] == 'service_binding_or_diagnosis_changed_after_preview'
    assert len(scenario.calls) == 1 and scenario.calls[0]['dry_run'] is True


@pytest.mark.parametrize('change', ['disabled', 'modified'])
def test_revoked_auto_policy_during_preview_prevents_dispatch(scenario, monkeypatch, change):
    async def preview(request):
        body = await request.json()
        scenario.calls.append(body)
        if change == 'disabled': scenario.cfg.remediation.auto_loop_enabled = False
        else: scenario.cfg.remediation.auto_scale_targets[0]['replicas'] = 2
        return {'id': 'preview-1', 'status': 'dry_run'}
    monkeypatch.setattr(web, 'heal_execute', preview)
    result = asyncio.run(web._guarded_auto_recovery('rca-one'))
    assert result['status'] == 'blocked'
    assert len(scenario.calls) == 1 and scenario.calls[0]['dry_run'] is True


def test_existing_auto_history_gate_rejects_before_dispatch(monkeypatch):
    cfg = SimpleNamespace(remediation=SimpleNamespace(enabled=True, recommend_only=False,
        require_approval=False, max_steps=1, auto_history_min_samples=14))
    context = {'blockers': [], 'target': {'kind': 'Deployment', 'name': 'engine',
        'namespace': 'shop', 'cluster_id': 'east', 'uid': 'uid-a'}}
    command = 'kubectl scale deployment/engine -n shop --replicas=1'
    rollback = 'kubectl scale deployment/engine -n shop --replicas=0'
    body = {'namespace': 'shop', 'commands': [command], 'rollback_commands': [rollback], 'dry_run': False,
            'auto_execute': True, 'ontology_proposal_id': 'preview-1'}
    monkeypatch.setattr(web, '_get_config', lambda: cfg)
    monkeypatch.setattr(web, '_bind_ontology_heal_input', lambda *args, **kwargs: context)
    monkeypatch.setattr(web, '_validate_heal_command', lambda command: None)
    monkeypatch.setattr(web, '_recovery_scope_for_plan', lambda **kwargs: {})
    monkeypatch.setattr(web, '_load_heal_runs', lambda: [{'id': 'preview-1', 'dry_run': True,
        'commands': [command], 'rollback_commands': [rollback], 'ontology_action_context': context}])
    monkeypatch.setattr(web, '_build_heal_suggestions', lambda body: {
        'diagnosis_gate': {'ready': True}, 'diagnosis_used': {'confidence': 1.0},
        'recipe': {'safe_auto_execute': True}, 'target': {},
        'suggestions': [{'command': command, 'risk': 'medium', 'rollback_command': rollback}]})
    monkeypatch.setattr(web, '_scoped_recovery_history_stats', lambda **kwargs: ({
        'sample_count': 0, 'success_count': 0, 'failure_count': 0}, {}))
    dispatched = []
    monkeypatch.setattr(web, '_execute_ontology_command', lambda *args: dispatched.append(args))
    with pytest.raises(web.HTTPException) as exc:
        asyncio.run(web.heal_execute(web._InternalHealRequest(body)))
    assert exc.value.status_code == 400
    assert 'insufficient_history' in exc.value.detail['auto_execution_gate']['blockers']
    assert dispatched == []


def test_coordinator_reaches_actual_heal_gate_and_verification_with_controlled_dependencies(scenario, monkeypatch):
    from remediation_history import build_recovery_scope
    from recovery_verification import verification_contract
    cfg = scenario.cfg
    cfg.kubernetes = SimpleNamespace(namespace='shop')
    cfg.remediation.enabled = True
    cfg.remediation.recommend_only = False
    cfg.remediation.require_approval = False
    cfg.remediation.max_steps = 1
    cfg.remediation.business_recovery_probes = []
    context = {'recorded_conditions_met': True, 'blockers': [], 'target': scenario.target}
    action = {**scenario.action, 'risk': 'medium'}
    scope = {**build_recovery_scope(commands=[action['command']], cluster_id='east', namespace='shop',
                                    target={'service': 'gateway', 'deployment': 'engine'}),
             **verification_contract(profiles=[], cluster_id='east', namespace='shop', service='gateway')}
    stats = {**scope, 'scope_required': True, 'verification_required': True, 'verification_scope_complete': True,
             'sample_count': 20, 'success_count': 20, 'failure_count': 0, 'success_rate': 1.0}
    records, dispatched = [], []
    def bind(body, execution=False, guarded_skill=False):
        body.update(cluster_id='east', namespace='shop')
        return context
    monkeypatch.setattr(web, '_bind_ontology_heal_input', bind)
    monkeypatch.setattr(web, '_build_heal_suggestions', lambda body: {'namespace': 'shop',
        'diagnosis_gate': {'ready': True}, 'diagnosis_used': {'confidence': .99, 'fault_type': 'ServiceUnavailable'},
        'fault_type': 'ServiceUnavailable', 'recipe': {'safe_auto_execute': True},
        'target': {'service': 'gateway', 'deployment': 'engine'}, 'suggestions': [action]})
    monkeypatch.setattr(web, '_recovery_scope_for_plan', lambda **kwargs: scope)
    monkeypatch.setattr(web, '_scoped_recovery_history_stats', lambda **kwargs: (stats, scope))
    monkeypatch.setattr(web, '_load_heal_runs', lambda: list(records))
    monkeypatch.setattr(web, '_save_heal_run', lambda record: records.insert(0, copy.deepcopy(record)))
    monkeypatch.setattr(web, '_execute_ontology_command', lambda *args: dispatched.append(args) or {'ok': True, 'executed': True})
    monkeypatch.setattr(web, '_verify_heal_recovery', lambda **kwargs: {'status': 'verified', 'recovered': True})
    monkeypatch.setattr(web, 'verify_business_recovery', lambda verification, **kwargs: verification)
    monkeypatch.setattr(web, '_record_verified_recovery_outcome', lambda **kwargs: {'recorded': True})
    monkeypatch.setattr(web, 'heal_execute', scenario.original_execute)
    result = asyncio.run(web._guarded_auto_recovery('rca-one'))
    assert result['status'] == 'succeeded' and result['verification']['recovered'] is True
    assert len(dispatched) == 1
    assert any(r.get('auto_execution_gate', {}).get('ready') is True for r in records)


def test_completed_multi_agent_run_schedules_only_after_durable_save(monkeypatch):
    cfg = SimpleNamespace(remediation=SimpleNamespace(auto_loop_enabled=True),
                          pipeline=SimpleNamespace(enable_recovery=True),
                          runtime=SimpleNamespace(enable_self_healing=True), llm=object())
    seen, saved, done = [], [], Event()
    class Pipeline:
        def __init__(self, config):
            seen.append((config.pipeline.enable_recovery, config.runtime.enable_self_healing))
        async def run(self, *args, **kwargs):
            return SimpleNamespace(status='completed', to_dict=lambda: {'status': 'completed'})
    def save(*, required_run_id=None):
        saved.append(required_run_id)
        return True
    def schedule(run_id):
        assert saved[-1] == run_id
        assert web._state['rca_runs'][run_id]['status'] == 'completed'
        seen.append(run_id)
        done.set()
    monkeypatch.setattr(web, '_get_config', lambda: cfg)
    monkeypatch.setattr(web, '_resolve_rca_cluster_scope', lambda *args: ('', None))
    monkeypatch.setattr(web, '_llm_configured', lambda *args: True)
    monkeypatch.setattr(web, '_save_rca_history', save)
    monkeypatch.setattr(web, '_schedule_guarded_auto_recovery', schedule)
    monkeypatch.setattr(web, 'Pipeline', Pipeline)
    monkeypatch.setitem(web._state, 'rca_runs', {})
    monkeypatch.setitem(web._state, 'rca_dedup', {})
    class Request:
        async def json(self): return {'query': 'investigate', 'namespace': 'shop'}
    result = asyncio.run(web.rca_run(Request()))
    assert done.wait(5)
    assert seen == [(False, False), result['run_id']]
    assert cfg.pipeline.enable_recovery is True
    assert cfg.runtime.enable_self_healing is True


def test_skill_run_uses_readonly_registry_in_guarded_mode(monkeypatch):
    cfg = SimpleNamespace(remediation=SimpleNamespace(auto_loop_enabled=True),
                          runtime=SimpleNamespace(enable_self_healing=True), llm=SimpleNamespace(timeout=90))
    seen, done = [], Event()
    class Agent:
        def __init__(self, **kwargs): pass
        def run(self, request):
            seen.append((request.skill_workflow['dry_run'], request.skill_workflow['stages']))
            return SimpleNamespace(status='completed', skill_runtime_result={}, agent_skill_bridge={},
                                   to_dict=lambda: {'status': 'completed'})
    def registry(config, **kwargs):
        seen.append(config.runtime.enable_self_healing)
        return None
    monkeypatch.setattr(web, '_get_config', lambda: cfg)
    monkeypatch.setattr(web, '_cluster_scoped_config', lambda *args: cfg)
    monkeypatch.setattr(web, '_resolve_rca_cluster_scope', lambda *args: ('east', None))
    monkeypatch.setattr(web, '_llm_configured', lambda *args: True)
    monkeypatch.setattr(web, '_save_rca_history', lambda **kwargs: True)
    monkeypatch.setattr(web, '_schedule_guarded_auto_recovery', lambda run_id: done.set())
    monkeypatch.setattr(web, '_finalize_ai_event_agent_skill_run', lambda **kwargs: None)
    monkeypatch.setattr(web, 'build_tool_registry', registry)
    monkeypatch.setattr(web, 'LLMClient', lambda config: None)
    monkeypatch.setattr(web, 'SkillAgent', Agent)
    monkeypatch.setitem(web._state, 'rca_runs', {})
    monkeypatch.setitem(web._state, 'rca_dedup', {})
    class Request:
        async def json(self): return {'query': 'investigate', 'namespace': 'shop',
            'skill_workflow': {'payload': {}, 'dry_run': False, 'stages': ['detect', 'diagnose', 'heal']}}
    asyncio.run(web.rca_run(Request()))
    assert done.wait(5)
    assert seen == [False, (True, ['detect', 'diagnose'])] and cfg.runtime.enable_self_healing is True
