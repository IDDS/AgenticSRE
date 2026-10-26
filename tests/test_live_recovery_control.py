"""Control invariants; run remotely before an actual testbed experiment."""
import copy
import json
import pytest
from scripts.live_recovery_control import scale_patch, snapshot
from scripts.validate_live_business_recovery import no_truth, try_approved_recovery


def deployment():
    return {'kind': 'Deployment', 'metadata': {'name': 'station', 'namespace': 'testbed',
        'uid': 'incarnation-one', 'resourceVersion': '17', 'generation': 2},
        'spec': {'replicas': 1, 'template': {'spec': {'containers': [{'name': 'api', 'image': 'fixed-image'}]}}},
        'status': {'readyReplicas': 1, 'observedGeneration': 2}}


@pytest.mark.parametrize('field,value', [('uid', 'replacement'), ('namespace', 'peer'), ('name', 'peer'),
                                        ('resourceVersion', ''), ('deletionTimestamp', 'now')])
def test_recovery_refuses_changed_object_identity(field, value):
    original = deployment()
    saved = snapshot(original, namespace='testbed', deployment='station')
    current = copy.deepcopy(original)
    current['spec']['replicas'] = 0
    current['metadata'][field] = value
    with pytest.raises(ValueError):
        scale_patch(saved, current, restore=True)


def test_recovery_never_overwrites_an_unrelated_edit_or_scale():
    original = deployment()
    saved = snapshot(original, namespace='testbed', deployment='station')
    mutated = copy.deepcopy(original)
    mutated['spec']['replicas'] = 0
    mutated['spec']['template']['spec']['containers'][0]['image'] = 'new-image'
    with pytest.raises(ValueError, match='concurrent_deployment_edit'):
        scale_patch(saved, mutated, restore=True)
    original['spec']['replicas'] = 3
    with pytest.raises(ValueError, match='concurrent_scale_change'):
        scale_patch(saved, original, restore=True)


def test_recovery_is_idempotent_and_patch_contains_atomic_identity_guards():
    original = deployment()
    saved = snapshot(original, namespace='testbed', deployment='station')
    assert scale_patch(saved, original, restore=True) is None
    patch = scale_patch(saved, original, restore=False)
    assert patch[:3] == [
        {'op': 'test', 'path': '/metadata/uid', 'value': 'incarnation-one'},
        {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '17'},
        {'op': 'test', 'path': '/spec/replicas', 'value': 1}]
    assert patch[-1]['value'] == 0
    original['spec']['replicas'] = 0
    original['metadata']['resourceVersion'] = '18'
    restored = scale_patch(saved, original, restore=True)
    assert restored[1]['value'] == '18' and restored[-1]['value'] == 1
    assert 'template' not in saved and 'spec' not in saved


@pytest.mark.parametrize('case', ['production', 'not_ready', 'stale', 'multiple', 'boolean'])
def test_injection_requires_a_stable_single_replica_testbed(case):
    original = deployment()
    namespace = 'testbed'
    if case == 'production':
        namespace = original['metadata']['namespace'] = 'production'
    elif case == 'not_ready':
        original['status']['readyReplicas'] = 0
    elif case == 'stale':
        original['status']['observedGeneration'] = 1
    elif case == 'multiple':
        original['spec']['replicas'] = 3
    else:
        original['spec']['replicas'] = True
    with pytest.raises(ValueError):
        snapshot(original, namespace=namespace, deployment='station')


@pytest.mark.parametrize('answer', ['fault_type', 'root_cause', 'diagnosis', 'commands', 'initial_evidence'])
def test_diagnosis_request_rejects_answers_even_when_nested(answer):
    no_truth({'query': 'Check service station', 'payload': {'service': 'station'}})
    with pytest.raises(ValueError, match='diagnosis_request_contains_answer'):
        no_truth({'payload': [{'nested': {answer: 'hidden-answer'}}]})


def test_recovery_does_not_retry_a_second_model_after_an_unknown_action(monkeypatch, tmp_path):
    import scripts.validate_live_business_recovery as runner
    target = {'kind': 'Deployment', 'uid': 'incarnation-one', 'name': 'station',
              'namespace': 'train-ticket', 'cluster_id': 'default'}
    model = {'revision': 'recorded-revision', 'action_context': {
        'recorded_conditions_met': True, 'target': target}}
    records = [{'mode': mode, 'run_id': mode, 'ontology': model} for mode in ['multi', 'skill']]
    called = []
    def api(base, path, body=None, **kwargs):
        called.append(copy.deepcopy(body))
        if len(called) == 1:
            return {'id': 'proposal'}
        raise TimeoutError('controlled lost acknowledgement')
    monkeypatch.setattr(runner, 'request', api)
    result = try_approved_recovery('http://127.0.0.1', records, {'target': {
        'uid': 'incarnation-one', 'deployment': 'station'}}, tmp_path)
    assert len(called) == 2 and len(result) == 1
    assert result[0]['executed'] is None
    assert called[0]['dry_run'] is True and called[1]['dry_run'] is False


@pytest.mark.parametrize('response', ['timeout', 'nonzero'])
def test_restore_confirms_lost_acknowledgement_without_replaying(monkeypatch, response):
    import subprocess
    from types import SimpleNamespace
    import scripts.live_recovery_control as control
    original = deployment()
    saved = snapshot(original, namespace='testbed', deployment='station')
    current = copy.deepcopy(original)
    current['spec']['replicas'] = 0
    monkeypatch.setattr(control, 'read_deployment', lambda _: copy.deepcopy(current))
    calls = []
    def patch(*args, **kwargs):
        calls.append(args)
        current['spec']['replicas'] = 1
        current['metadata']['resourceVersion'] = '18'
        if response == 'timeout':
            raise subprocess.TimeoutExpired('controlled', 15)
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(control.subprocess, 'run', patch)
    assert control.scale(saved, restore=True) == 'observed_restored'
    assert len(calls) == 1


@pytest.mark.parametrize('condition', ['transient', 'identity_changed', 'already_complete'])
def test_watchdog_retries_transient_failure_but_preserves_safety_refusals(monkeypatch, tmp_path, condition):
    import json
    import scripts.live_recovery_control as control
    (tmp_path/'contract.json').write_text(json.dumps({'deadline_unix': 0, 'target': {}}))
    calls = []
    clock = {'now': 1}
    monkeypatch.setattr(control.time, 'time', lambda: clock['now'])
    monkeypatch.setattr(control.time, 'monotonic', lambda: clock['now'])
    monkeypatch.setattr(control.time, 'sleep', lambda delta: clock.update(now=clock['now']+delta))
    def restore(*args, **kwargs):
        calls.append(True)
        if condition == 'identity_changed': raise ValueError('deployment_identity_changed')
        if len(calls) == 1: raise RuntimeError('temporary_read_failure')
        return 'observed_restored'
    monkeypatch.setattr(control, 'scale', restore)
    if condition == 'already_complete':
        (tmp_path/'contract.json').write_text(json.dumps({'deadline_unix': 10, 'target': {}}))
        (tmp_path/'cleanup-complete.json').write_text('{}')
    control.watchdog(tmp_path)
    if condition == 'already_complete':
        assert not calls and not (tmp_path/'watchdog-result.json').exists()
    else:
        record = json.loads((tmp_path/'watchdog-result.json').read_text())
        assert record['outcome'] == ('restore_refused' if condition == 'identity_changed' else 'observed_restored')
        assert len(calls) == (1 if condition == 'identity_changed' else 2)


def test_unknown_injection_is_fenced_before_cleanup_and_late_dispatch_is_refused(monkeypatch):
    import json
    import subprocess
    from types import SimpleNamespace
    import scripts.live_recovery_control as control
    live = deployment()
    live['metadata']['annotations'] = {'existing-owner': 'preserve'}
    saved = snapshot(live, namespace='testbed', deployment='station')
    pending = []
    monkeypatch.setattr(control, 'read_deployment', lambda _: copy.deepcopy(live))
    def kubectl(args, **kwargs):
        patch = json.loads(args[-1])
        if patch[-1].get('value') == 0:
            pending.append(patch)
            raise subprocess.TimeoutExpired('controlled delayed injection', 15)
        if patch[-1]['op'] == 'add':
            live['metadata']['annotations'] = patch[-1]['value']
        else:
            assert patch[-1]['op'] == 'remove'
            live['metadata']['annotations'].pop(control.FENCE_ANNOTATION)
        live['metadata']['resourceVersion'] = str(int(live['metadata']['resourceVersion']) + 1)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(control.subprocess, 'run', kubectl)
    with pytest.raises(RuntimeError, match='scale_outcome_unknown'):
        control.scale(saved, restore=False)
    assert live['spec']['replicas'] == 1 and len(pending) == 1
    assert control.scale(saved, restore=True, fence_unknown=True) == 'fenced_restored'
    assert pending[0][1]['value'] != live['metadata']['resourceVersion']
    assert control.scale(saved, restore=True, fence_unknown=True) == 'already_restored'
    with pytest.raises(ValueError, match='experiment_already_fenced'):
        control.scale(saved, restore=False)
    assert control.release_fence(saved) is True
    assert live['metadata']['annotations'] == {'existing-owner': 'preserve'}
    # Removing our annotation is another version change, so the old in-flight
    # injection still cannot satisfy its API-side resourceVersion precondition.
    assert pending[0][1]['value'] != live['metadata']['resourceVersion']


def test_execute_opt_in_is_checked_before_any_cluster_access(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import scripts.validate_live_business_recovery as runner
    monkeypatch.delenv('AGENTICSRE_RUN_LIVE_RECOVERY', raising=False)
    monkeypatch.setattr(runner, 'get_resource', lambda *args: pytest.fail('must not touch cluster'))
    with pytest.raises(RuntimeError, match='explicit_live_test_opt_in_required'):
        runner.run(SimpleNamespace(execute=True, output=tmp_path/'case'))
    assert not (tmp_path/'case').exists()


@pytest.mark.parametrize('execute,warmup_failures', [(False, 0), (True, 0), (True, 2)])
def test_main_readonly_default_and_failed_dispatch_finally_restore(monkeypatch, tmp_path, execute, warmup_failures):
    from types import SimpleNamespace
    import scripts.validate_live_business_recovery as runner
    original = deployment()
    original['metadata'].update(name=runner.SERVICE, namespace=runner.NAMESPACE)
    objects = {
        'deployment': original,
        'service': {'metadata': {'uid': 'service-one'}, 'spec': {'selector': {'app': 'station'}}},
        'pods': {'items': [{'metadata': {'uid': 'pod-one', 'labels': {'app': 'station'},
            'ownerReferences': [{'controller': True, 'kind': 'ReplicaSet', 'name': 'rs', 'uid': 'rs-one'}]},
            'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}}]},
        'replicaset': {'metadata': {'uid': 'rs-one', 'ownerReferences': [
            {'controller': True, 'kind': 'Deployment', 'uid': original['metadata']['uid']}] }},
        'endpoints': {'subsets': [{'addresses': [{'targetRef': {'uid': 'pod-one'}}]}]},
        'hpa': {'items': []}, 'networkchaos': {'items': []}}
    monkeypatch.setattr(runner, 'get_resource', lambda kind, name=None: copy.deepcopy(objects[kind]))
    monkeypatch.setattr(runner, 'read_deployment', lambda _: copy.deepcopy(original))
    monkeypatch.setattr(runner.time, 'sleep', lambda _: None)
    warmup_count = {'samples': 0}
    def sample(*args):
        if args[1] == 'recovery_warmup': warmup_count['samples'] += 1
        healthy = args[1] != 'recovery_warmup' or warmup_count['samples'] > warmup_failures
        return {'at': '2026-10-26T00:00:00+00:00', 'phase': args[1], 'elapsed_ms': 1,
                'business_success': healthy, 'control_success': True}
    monkeypatch.setattr(runner, 'business_sample', sample)
    monkeypatch.setattr(runner, 'diagnose', lambda *args: pytest.fail('no diagnosis on this path'))
    commands = []
    def scale(saved, *, restore, **kwargs):
        commands.append((restore, kwargs))
        if not restore: raise RuntimeError('controlled_before_dispatch_failure')
        return 'fenced_restored'
    monkeypatch.setattr(runner, 'scale', scale)
    monkeypatch.setattr(runner, 'release_fence', lambda _: True)
    processes = []
    class Process:
        def __init__(self, command, **kwargs):
            self.terminated = False
            processes.append(self)
            if '--watchdog' in command:
                (tmp_path/'case'/'watchdog-ready.json').write_text('{}')
            else:
                kwargs['stdout'].write('Forwarding from 127.0.0.1:18226')
                kwargs['stdout'].flush()
        def poll(self): return None
        def wait(self, **kwargs): return 0
        def terminate(self): self.terminated = True
    monkeypatch.setattr(runner.subprocess, 'Popen', Process)
    monkeypatch.setenv('AGENTICSRE_RUN_LIVE_RECOVERY', '1')
    result = runner.run(SimpleNamespace(execute=execute, healthy_diagnosis=False,
        output=tmp_path/'case', business='http://127.0.0.1', api='http://127.0.0.1'))
    assert processes[0].terminated
    if execute:
        assert result['watchdog_armed'] is True and result['fault_dispatched'] is None
        assert result['injection_outcome'] == 'unknown'
        assert commands == [(False, {}), (True, {'fence_unknown': True})]
        assert result['business_recovery_confirmed'] is True
        readiness = json.loads((tmp_path/'case'/'business-readiness.json').read_text())
        assert readiness['warmup_failures'] == warmup_failures
        assert readiness['warmup_samples'] == warmup_failures + 1
    else:
        assert not commands and len(processes) == 1
        assert result == {'status': 'preflight_passed', 'fault_injected': False}


def test_delayed_injection_cannot_resume_after_unknown_watchdog_restore(monkeypatch):
    """Watchdog restores observed zero while the original caller awaits an acknowledgement."""
    from types import SimpleNamespace
    import scripts.live_recovery_control as control
    live = deployment()
    saved = snapshot(live, namespace='testbed', deployment='station')
    attempts = []
    monkeypatch.setattr(control, 'read_deployment', lambda _: copy.deepcopy(live))
    def kubectl(args, **kwargs):
        patch = json.loads(args[-1])
        setting_zero = patch[-1].get('value') == 0
        if setting_zero:
            attempts.append(True)
            live['spec']['replicas'] = 0
            live['metadata']['resourceVersion'] = str(int(live['metadata']['resourceVersion']) + 1)
            if len(attempts) == 1:
                # The watchdog completes before the suspended injection caller
                # observes its nonzero/unknown transport result.
                control.scale(saved, restore=True, fence_unknown=True)
                return SimpleNamespace(returncode=1)
        else:
            for op in patch:
                if op['op'] == 'replace' and op['path'] == '/spec/replicas':
                    live['spec']['replicas'] = op['value']
                if op['op'] == 'add' and op['path'] == '/metadata/annotations':
                    live['metadata']['annotations'] = op['value']
            live['metadata']['resourceVersion'] = str(int(live['metadata']['resourceVersion']) + 1)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(control.subprocess, 'run', kubectl)
    with pytest.raises(ValueError, match='experiment_already_fenced'):
        control.scale(saved, restore=False)
    assert len(attempts) == 1 and live['spec']['replicas'] == 1


def test_injection_deadline_is_checked_again_before_dispatch(monkeypatch):
    import scripts.live_recovery_control as control
    live = deployment()
    saved = snapshot(live, namespace='testbed', deployment='station')
    saved['injection_deadline_unix'] = 10
    monkeypatch.setattr(control.time, 'time', lambda: 11)
    with pytest.raises(ValueError, match='injection_deadline_passed'):
        scale_patch(saved, live, restore=False)
