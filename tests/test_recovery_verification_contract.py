"""Recovery history must not borrow successes from another verification contract."""
import copy
import json
from types import SimpleNamespace

import pytest

import recovery_verification as probes
import web_app.app as web
from memory.fault_context_store import FaultContextStore
from remediation_history import build_recovery_scope

COMMANDS = ['kubectl scale deployment/api -n test --replicas=1']
TARGET = {'service': 'api', 'deployment': 'api'}


def profile(**changes):
    return {'cluster_id': 'east', 'namespace': 'test', 'service': 'api',
            'url': 'http://api.test.svc.cluster.local/readiness', 'samples': 2,
            'json_assertions': [{'path': ['status'], 'op': 'equals', 'value': 1}], **changes}


def contract(profiles):
    return probes.verification_contract(profiles=profiles, cluster_id='east', namespace='test', service='api')


@pytest.fixture
def history(tmp_path, monkeypatch):
    cfg = SimpleNamespace(memory=SimpleNamespace(backend='json', db_path=str(tmp_path),
        rules_collection='rules', faults_collection='faults', max_similar_results=5),
        remediation=SimpleNamespace(business_recovery_probes=[profile()], auto_history_min_samples=14))
    store = FaultContextStore(cfg)
    monkeypatch.setattr(web, '_get_fault_store', lambda: store)
    monkeypatch.setattr(web, '_get_config', lambda: cfg)
    return cfg, store


def query(history):
    cfg, _ = history
    return web._scoped_recovery_history_stats(body={'cluster_id': 'east'},
        diagnosis={'fault_type': 'ServiceUnavailable'}, fault_type='ServiceUnavailable',
        target=TARGET, commands=COMMANDS, namespace='test', cfg=cfg)


@pytest.mark.parametrize('mode', ['failure', 'timeout', 'transport', 'signal', 'inner_timeout',
                                  'launch_error', 'unexpected_error', 'untyped', 'ontology_unknown',
                                  'not_dispatched', 'dry_run'])
def test_execute_failure_accounting_uses_terminal_dispatch_evidence(history, monkeypatch, mode):
    """Exercise the endpoint, native writer and persisted statistics, without real actions."""
    import asyncio
    import subprocess
    cfg, store = history
    cfg.kubernetes = SimpleNamespace(namespace='test', use_ssh=mode == 'transport',
        ssh_jump_host='jump' if mode == 'transport' else '', ssh_target='target', target_host='target')
    for key, value in dict(enabled=True, recommend_only=False, require_approval=True,
                           dry_run=True, max_steps=5).items():
        setattr(cfg.remediation, key, value)
    monkeypatch.setattr(web, '_apply_cluster_to_cmd', lambda cmd, cluster_id: cmd)
    suggestion = {'target': TARGET, 'namespace': 'test', 'fault_type': 'ServiceUnavailable',
                  'diagnosis_gate': {'ready': True}, 'diagnosis_used': {'confidence': .99}}
    monkeypatch.setattr(web, '_build_heal_suggestions', lambda body: suggestion)
    monkeypatch.setattr(web, '_save_heal_run', lambda run: None)
    probe_calls = []
    def no_probe(*args, **kwargs):
        probe_calls.append(True)
        return {'status': 'skipped', 'reason': 'execution_failed', 'recovered': False}
    monkeypatch.setattr(web, '_verify_heal_recovery', no_probe)
    monkeypatch.setattr(web, 'verify_business_recovery', no_probe)
    def shell(*args, **kwargs):
        if mode == 'timeout':
            raise subprocess.TimeoutExpired('controlled', 1)
        if mode == 'launch_error':
            raise OSError('controlled launch failure')
        if mode == 'unexpected_error':
            raise RuntimeError('controlled waiting failure')
        rc = {'transport': 255, 'signal': -9, 'inner_timeout': 124}.get(mode, 1)
        return SimpleNamespace(returncode=rc, stdout='', stderr='controlled')
    monkeypatch.setattr(web.subprocess, 'run', shell)
    if mode == 'untyped':
        monkeypatch.setattr(web, '_shell_sync', lambda *args: {'ok': False, 'returncode': 1})
    ontology = {'target': {'cluster_id': 'east'}} if mode in {'ontology_unknown', 'not_dispatched'} else None
    monkeypatch.setattr(web, '_bind_ontology_heal_input', lambda *args, **kwargs: ontology)
    monkeypatch.setattr(web, '_verify_ontology_action', lambda *args: {})
    monkeypatch.setattr(web, '_load_heal_runs', lambda: [{'id': 'proposal', 'dry_run': True,
        'commands': COMMANDS, 'rollback_commands': [], 'ontology_action_context': ontology}])
    monkeypatch.setattr(web, '_execute_ontology_command', lambda *args: {
        'ok': False, 'executed': mode != 'not_dispatched',
        'outcome_unknown': mode == 'ontology_unknown', 'execution_failure_confirmed': True})
    class Request:
        async def json(self):
            return {'cluster_id': 'east', 'namespace': 'test', 'commands': COMMANDS,
                    'dry_run': mode == 'dry_run', 'approved': True, 'ontology_proposal_id': 'proposal'}
    if mode == 'failure':
        for i in range(14):
            add(store, i, policy=contract([profile()]))
    runs = [asyncio.run(web.heal_execute(Request())) for _ in range(5 if mode == 'failure' else 1)]
    # Reload actual JSON storage: no mock aggregate can hide a missing failure denominator.
    monkeypatch.setattr(web, '_get_fault_store', lambda: FaultContextStore(cfg))
    stats, _ = query(history)
    run = runs[-1]
    if mode == 'failure':
        assert (stats['sample_count'], stats['success_count'], stats['failure_count']) == (19, 14, 5)
        assert run['status'] == 'failed' and run['recovery_history_record']['recorded'] is True
        assert run['verification']['reason'] == 'action_execution_failed'
        assert run['verification']['verification_scope'] == 'action_execution'
        assert 'business_validation' not in run['verification']
        gate = web._build_auto_execution_gate(recipe={'safe_auto_execute': True}, suggestions=[{'risk': 'low'}],
            diagnosis={'confidence': .99}, cfg=cfg, recovery_history_stats=stats)
        assert gate['ready'] is False
    else:
        assert stats['sample_count'] == 0 and not run['recovery_history_record']['recorded']
        assert run['verification']['recovered'] is None
        assert run['status'] == ('dry_run' if mode == 'dry_run' else
                                 'blocked' if mode in {'launch_error', 'not_dispatched'} else 'outcome_unknown')
    assert not probe_calls, 'unsuccessful or undispatched actions must not run recovery probes'


def add(store, index, *, policy=None, status='success', verification_status='verified', **changes):
    scope = build_recovery_scope(commands=COMMANDS, cluster_id='east', namespace='test', target=TARGET)
    resolution = {**scope, 'status': status, 'verification_status': verification_status, 'commands': COMMANDS}
    if policy:
        resolution.update({k: policy[k] for k in ('verification_fingerprint', 'verification_contract_version', 'verification_level')})
    resolution.update(changes)
    store.add_fault({'heal_run_id': f'heal-{index}', 'fault_type': 'ServiceUnavailable',
                     'cluster_id': 'east', 'namespace': 'test', 'target': TARGET, 'resolution': resolution})


def test_legacy_successes_cannot_support_current_json_contract(history):
    cfg, store = history
    for i in range(14):
        add(store, i)
    stats, scope = query(history)
    assert stats['sample_count'] == 0
    assert stats['verification_missing_count'] == 14
    assert stats['verification_required'] is True
    assert scope['verification_fingerprint']


def test_contract_defaults_and_assertion_order_are_stable_and_private():
    first = profile()
    explicit = profile(interval_seconds=2.0, timeout_seconds=3.0, expected_status=200,
                       max_header_latency_ms=1000.0, max_response_latency_ms=1000.0, max_body_bytes=65536,
                       json_assertions=[{'op': 'equals', 'value': 1.0, 'path': ['status']}])
    before = copy.deepcopy(first)
    a, b = contract([first]), contract([explicit])
    assert a['verification_fingerprint'] == b['verification_fingerprint']
    assert a['verification_level'] == 'json_assertions'
    text = json.dumps(a)
    assert first['url'] not in text and 'json_assertions' not in a
    assert first == before
    assert contract([])['verification_fingerprint'] != a['verification_fingerprint']
    header = profile(); header.pop('json_assertions')
    assert contract([header])['verification_fingerprint'] != a['verification_fingerprint']


@pytest.mark.parametrize('change', [
    {'url': 'http://api.test.svc.cluster.local/other'}, {'samples': 3}, {'interval_seconds': 3},
    {'timeout_seconds': 1}, {'max_header_latency_ms': 500}, {'max_response_latency_ms': 500},
    {'max_body_bytes': 1024}, {'expected_status': 201},
    {'json_assertions': [{'path': ['status'], 'op': 'equals', 'value': True}]},
    {'json_assertions': [{'path': ['other'], 'op': 'equals', 'value': 1}]},
])
def test_acceptance_changes_split_history(change):
    assert contract([profile()])['verification_fingerprint'] != contract([profile(**change)])['verification_fingerprint']


@pytest.mark.parametrize('profiles', [None, {}, [profile(), profile()], [profile(service='other')],
    [profile(json_assertions=[])], [profile(timeout_seconds=10 ** 1000)]])
def test_invalid_or_unmatched_contract_is_incomplete(profiles):
    result = contract(profiles)
    assert result['verification_contract_complete'] is False
    assert not result['verification_fingerprint']


def test_only_same_verified_contract_counts_and_survives_json_restart(history):
    cfg, store = history
    current = contract([profile()])
    header = profile(); header.pop('json_assertions')
    other = contract([header])
    add(store, 1, policy=current)
    add(store, 2, policy=current, status='failed')
    add(store, 3, policy=other)
    add(store, 4)
    add(store, 5, policy=current, verification_status='unknown')
    add(store, 6, policy=current, status='pending')
    for candidate in [store, FaultContextStore(cfg)]:
        stats = candidate.resolution_stats(fault_type='ServiceUnavailable',
            action_fingerprint=build_recovery_scope(commands=COMMANDS, cluster_id='east', namespace='test', target=TARGET)['action_fingerprint'],
            environment_fingerprint=build_recovery_scope(commands=COMMANDS, cluster_id='east', namespace='test', target=TARGET)['environment_fingerprint'],
            verification_fingerprint=current['verification_fingerprint'], require_verification_contract=True)
        assert (stats['sample_count'], stats['success_count'], stats['failure_count']) == (2, 1, 1)
        assert stats['verification_missing_count'] == 1
        assert stats['verification_mismatch_count'] == 1
        assert stats['verification_unverified_count'] == 1
        assert stats['unclassified_outcome_count'] == 1
        assert stats['verification_scope_complete'] is True
    stored = store.list_faults(limit=100)
    assert stored[0]['verification_fingerprint'] == current['verification_fingerprint']


def test_required_missing_contract_cannot_fall_back_to_broad_statistics(history):
    _, store = history
    add(store, 1)
    stats = store.resolution_stats(fault_type='ServiceUnavailable', require_verification_contract=True)
    assert stats['sample_count'] == 0 and stats['scope_complete'] is False


def test_auto_gate_rejects_legacy_aggregates_with_good_counts(history):
    cfg, _ = history
    gate = web._build_auto_execution_gate(recipe={'safe_auto_execute': True}, suggestions=[{'risk': 'low'}],
        diagnosis={'confidence': .99}, cfg=cfg,
        recovery_history_stats={'scope_required': True, 'scope_complete': True, 'sample_count': 30,
                                'success_count': 30, 'failure_count': 0, 'success_rate': 1.0})
    assert gate['ready'] is False
    assert 'verification_contract_missing' in gate['blockers']


def test_matching_history_can_support_gate_but_config_change_removes_it(history):
    cfg, store = history
    policy = contract([profile()])
    for i in range(14):
        add(store, i, policy=policy)
    stats, _ = query(history)
    gate = web._build_auto_execution_gate(recipe={'safe_auto_execute': True}, suggestions=[{'risk': 'low'}],
        diagnosis={'confidence': .99}, cfg=cfg, recovery_history_stats=stats)
    assert gate['ready'] is True
    cfg.remediation.business_recovery_probes[0]['json_assertions'][0]['value'] = 0
    changed, _ = query(history)
    assert changed['sample_count'] == 0 and changed['verification_mismatch_count'] == 14


def test_actual_infrastructure_verification_carries_contract():
    result = probes.verify_business_recovery({'status': 'verified', 'recovered': True},
        profiles=[], cluster_id='east', namespace='test', service='api')
    policy = contract([])
    assert result['verification_fingerprint'] == policy['verification_fingerprint']
    assert result['verification_level'] == 'infrastructure'


def test_writer_refuses_stale_contract_without_adding_fault(history):
    _, store = history
    old = contract([])
    new = contract([profile()])
    scope = {**build_recovery_scope(commands=COMMANDS, cluster_id='east', namespace='test', target=TARGET), **old}
    run = {'id': 'heal-stale', 'dry_run': False, 'verification': {
        'status': 'verified', 'recovered': True, **new}}
    result = web._record_verified_recovery_outcome(run=run, body={}, suggestion={}, recovery_scope=scope)
    assert result['recorded'] is False and result['reason'] == 'verification_contract_mismatch'
    assert not store.list_faults()


def test_flattened_metadata_backend_retains_contract_and_filters_outcomes(history):
    _, store = history
    class MetadataCollection:
        def __init__(self):
            self.rows = {}
        def upsert(self, *, ids, documents, metadatas):
            self.rows[ids[0]] = (documents[0], metadatas[0])
        def get(self, **kwargs):
            return {'ids': list(self.rows), 'documents': [r[0] for r in self.rows.values()],
                    'metadatas': [r[1] for r in self.rows.values()]}
    store._backend = 'chromadb'
    store._faults_col = MetadataCollection()
    policy = contract([profile()])
    add(store, 1, policy=policy)
    add(store, 2)
    scope = build_recovery_scope(commands=COMMANDS, cluster_id='east', namespace='test', target=TARGET)
    stats = store.resolution_stats(fault_type='ServiceUnavailable',
        action_fingerprint=scope['action_fingerprint'], environment_fingerprint=scope['environment_fingerprint'],
        verification_fingerprint=policy['verification_fingerprint'], require_verification_contract=True)
    assert stats['sample_count'] == 1 and stats['verification_missing_count'] == 1
    assert store.list_faults()[0]['verification_fingerprint'] == policy['verification_fingerprint']


@pytest.mark.parametrize('nested', ['', 'different'])
def test_conflicting_nested_and_flat_contracts_are_excluded(history, monkeypatch, nested):
    _, store = history
    policy = contract([profile()])
    add(store, 1, policy=policy)
    rows = copy.deepcopy(store.list_faults())
    rows[0]['resolution']['verification_fingerprint'] = nested
    monkeypatch.setattr(store, 'list_faults', lambda **kwargs: rows)
    stats, _ = query(history)
    assert stats['sample_count'] == 0 and stats['verification_conflict_count'] == 1


@pytest.mark.parametrize('field', ['action_fingerprint', 'environment_fingerprint'])
def test_gate_requires_action_and_environment_even_with_valid_contract(history, field):
    cfg, _ = history
    stats, _ = query(history)
    stats.update(sample_count=14, success_count=14, failure_count=0, success_rate=1.0)
    stats.pop(field)
    gate = web._build_auto_execution_gate(recipe={'safe_auto_execute': True}, suggestions=[{'risk': 'low'}],
        diagnosis={'confidence': .99}, cfg=cfg, recovery_history_stats=stats)
    assert gate['ready'] is False and 'recovery_scope_incomplete' in gate['blockers']


def test_number_fingerprints_do_not_depend_on_ambient_decimal_precision():
    from decimal import localcontext
    a = profile(json_assertions=[{'path': ['status'], 'op': 'equals', 'value': 1.1234}])
    b = profile(json_assertions=[{'path': ['status'], 'op': 'equals', 'value': 1.1256}])
    expected = contract([a])['verification_fingerprint']
    with localcontext() as context:
        context.prec = 2
        assert contract([a])['verification_fingerprint'] == expected
        assert contract([a])['verification_fingerprint'] != contract([b])['verification_fingerprint']


@pytest.mark.parametrize('outcome', [True, False, None])
def test_native_writer_persists_matching_contract_and_skips_unknown(history, outcome):
    cfg, store = history
    cfg.remediation.business_recovery_probes = []
    scope = web._recovery_scope_for_plan(body={'cluster_id': 'east'}, target=TARGET,
                                        commands=COMMANDS, namespace='test', cfg=cfg)
    verification = probes.verify_business_recovery({'status': 'unknown' if outcome is None else 'verified', 'recovered': outcome},
        profiles=[], cluster_id='east', namespace='test', service='api')
    run = {'id': 'heal-writer', 'dry_run': False, 'verification': verification,
           'cluster_id': 'east', 'namespace': 'test', 'commands': COMMANDS, 'fault_type': 'ServiceUnavailable'}
    result = web._record_verified_recovery_outcome(run=run, body={}, suggestion={'target': TARGET}, recovery_scope=scope)
    if outcome is None:
        assert result['recorded'] is False and not store.list_faults()
    else:
        assert result['recorded'] is True
        saved = store.list_faults()[0]
        assert saved['verification_fingerprint'] == scope['verification_fingerprint']
        stats, _ = query(history)
        assert stats['sample_count'] == 1 and stats['success_count'] == int(outcome)


@pytest.mark.parametrize('anchor', ['scope', 'previous_only'])
@pytest.mark.parametrize('infrastructure_failure', [False, True])
def test_reverification_cannot_silently_adopt_weaker_contract_on_repeated_checks(history, monkeypatch, tmp_path, anchor, infrastructure_failure):
    from fastapi.testclient import TestClient
    cfg, _ = history
    old = contract([profile()])
    header = profile(); header.pop('json_assertions')
    cfg.remediation.business_recovery_probes = [header]
    current = contract([header])
    monkeypatch.setattr(web, '_HEAL_RUNS_FILE', tmp_path / 'heal-runs.json')
    monkeypatch.setattr(web, '_HEAL_ARTIFACT_DIR', tmp_path / 'heal-runs')
    monkeypatch.setattr(web, '_verify_heal_recovery', lambda **kwargs: {'status': 'verified', 'recovered': True})
    monkeypatch.setattr(web, 'verify_business_recovery', lambda *args, **kwargs: {
        **current, 'status': 'verified', 'recovered': not infrastructure_failure,
        'reason': 'infrastructure_not_ready' if infrastructure_failure else 'business_probe_passed',
        'verification_scope': 'infrastructure' if infrastructure_failure else 'infrastructure_and_business_http',
        'business_validation': 'http_headers'})
    monkeypatch.setattr(web, '_shell_sync', lambda *args, **kwargs: pytest.fail('must not replay an action'))
    monkeypatch.setattr(web, '_get_fault_store', lambda: pytest.fail('late observations must not train recovery history'))
    record = {'id': 'heal-contract', 'status': 'verification_pending', 'success': None, 'dry_run': False,
              'action_success': True, 'cluster_id': 'east', 'namespace': 'test', 'verification_target': TARGET,
              'commands': COMMANDS, 'finished_at': 123,
              'verification': {**old, 'status': 'unknown', 'recovered': None}}
    if anchor == 'scope':
        record['recovery_history_scope'] = old
    web._save_heal_run(record)
    client = TestClient(web.app)
    for _ in range(1 if infrastructure_failure else 2):
        response = client.post('/api/heal/runs/heal-contract/verify', json={'verification_fingerprint': current['verification_fingerprint']})
        assert response.status_code == 200, response.text
        result = response.json()
        if infrastructure_failure:
            assert result['success'] is False and result['status'] == 'failed'
            assert result['verification']['reason'] == 'infrastructure_not_ready'
        else:
            assert result['success'] is None and result['status'] == 'verification_pending'
            assert result['verification']['reason'] == 'verification_contract_changed'
        assert result['verification_required_fingerprint'] == old['verification_fingerprint']
        assert result['finished_at'] == 123 and result['commands'] == COMMANDS
        assert result['recovery_history_record']['recorded'] is False


def test_history_backend_failure_is_explicit_and_cannot_use_legacy_counts(history, monkeypatch):
    _, store = history
    def fail(**kwargs):
        raise OSError('unavailable')
    monkeypatch.setattr(store, 'resolution_stats', fail)
    stats, _ = query(history)
    assert stats['sample_count'] == 0 and stats['verification_contract_unavailable'] is True
    assert stats['verification_scope_complete'] is False


@pytest.mark.parametrize('field,value', [('verification_fingerprint', 'other'), ('action_fingerprint', 'other'), ('verification_required', False)])
def test_backend_metadata_must_bind_the_requested_scope(history, monkeypatch, field, value):
    _, store = history
    stats, _ = query(history)
    stats.update(sample_count=30, success_count=30, failure_count=0)
    stats[field] = value
    monkeypatch.setattr(store, 'resolution_stats', lambda **kwargs: stats)
    result, _ = query(history)
    assert result['sample_count'] == 0 and result['verification_contract_unavailable'] is True


@pytest.mark.parametrize('phase', ['infrastructure', 'business'])
def test_reverification_exception_cannot_erase_original_contract(history, monkeypatch, tmp_path, phase):
    from fastapi.testclient import TestClient
    cfg, _ = history
    required = contract([profile()])
    header = profile(); header.pop('json_assertions')
    cfg.remediation.business_recovery_probes = [header]
    current = contract([header])
    monkeypatch.setattr(web, '_HEAL_RUNS_FILE', tmp_path / 'exception-runs.json')
    monkeypatch.setattr(web, '_HEAL_ARTIFACT_DIR', tmp_path / 'exception-runs')
    attempts = {'round': 0}
    def infrastructure(**kwargs):
        attempts['round'] += 1
        if phase == 'infrastructure' and attempts['round'] == 1:
            raise RuntimeError('unavailable')
        return {'status': 'verified', 'recovered': True}
    def business(*args, **kwargs):
        if phase == 'business' and attempts['round'] == 1:
            raise RuntimeError('unavailable')
        return {**current, 'status': 'verified', 'recovered': True,
                'verification_scope': 'infrastructure_and_business_http', 'business_validation': 'http_headers'}
    monkeypatch.setattr(web, '_verify_heal_recovery', infrastructure)
    monkeypatch.setattr(web, 'verify_business_recovery', business)
    monkeypatch.setattr(web, '_get_fault_store', lambda: pytest.fail('no learning from rechecks'))
    web._save_heal_run({'id': 'heal-anchor-error', 'status': 'verification_pending', 'success': None,
        'dry_run': False, 'action_success': True, 'namespace': 'test', 'cluster_id': 'east',
        'verification_target': TARGET, 'commands': COMMANDS,
        'verification': {**required, 'status': 'unknown', 'recovered': None}})
    client = TestClient(web.app)
    first = client.post('/api/heal/runs/heal-anchor-error/verify').json()
    assert first['verification']['reason'] == 'verification_error'
    assert first['verification_required_fingerprint'] == required['verification_fingerprint']
    second = client.post('/api/heal/runs/heal-anchor-error/verify').json()
    assert second['status'] == 'verification_pending' and second['success'] is None
    assert second['verification']['reason'] == 'verification_contract_changed'


def test_real_json_success_is_written_reloaded_and_counted_under_exact_contract(history, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    cfg, store = history
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            body = b'{"status":1}'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    url = f'http://127.0.0.1:{server.server_port}'
    cfg.remediation.business_recovery_probes = [profile(url=url, interval_seconds=1)]
    scope = web._recovery_scope_for_plan(body={'cluster_id': 'east'}, target=TARGET,
        commands=COMMANDS, namespace='test', cfg=cfg)
    try:
        observed = probes.verify_business_recovery({'status': 'verified', 'recovered': True},
            profiles=cfg.remediation.business_recovery_probes, cluster_id='east', namespace='test', service='api')
    finally:
        server.shutdown(); server.server_close(); worker.join()
    assert observed['recovered'] is True and observed['business_validation'] == 'json_assertions'
    result = web._record_verified_recovery_outcome(run={
        'id': 'heal-real-json-contract', 'dry_run': False, 'action_success': True,
        'verification': observed, 'commands': COMMANDS, 'cluster_id': 'east', 'namespace': 'test',
        'fault_type': 'ServiceUnavailable'}, body={}, suggestion={'target': TARGET}, recovery_scope=scope)
    assert result['recorded'] is True
    restarted = FaultContextStore(cfg)
    monkeypatch.setattr(web, '_get_fault_store', lambda: restarted)
    stats, _ = query(history)
    assert stats['sample_count'] == 1 and stats['success_count'] == 1 and stats['failure_count'] == 0
    assert stats['verification_fingerprint'] == observed['verification_fingerprint']
    assert url not in json.dumps(restarted.list_faults())
    gate = web._build_auto_execution_gate(recipe={'safe_auto_execute': True}, suggestions=[{'risk': 'low'}],
        diagnosis={'confidence': .99}, cfg=cfg, recovery_history_stats=stats)
    assert gate['ready'] is False and 'insufficient_history' in gate['blockers']
