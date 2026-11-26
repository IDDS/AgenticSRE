"""Recovery-only API must never replay writes or credit late observations."""
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
import web_app.app as appmod


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(appmod, '_HEAL_RUNS_FILE', tmp_path / 'runs.json')
    monkeypatch.setattr(appmod, '_HEAL_ARTIFACT_DIR', tmp_path / 'runs')
    monkeypatch.setattr(appmod, '_get_config', lambda: SimpleNamespace(remediation=SimpleNamespace(business_recovery_probes=[])))
    monkeypatch.setattr(appmod, '_shell_sync', lambda *a, **k: pytest.fail('must not replay action'))
    monkeypatch.setattr(appmod, '_build_heal_suggestions', lambda *a, **k: pytest.fail('must not regenerate plan'))
    monkeypatch.setattr(appmod, '_get_fault_store', lambda: pytest.fail('late observation must not train knowledge'))
    run = {'id':'heal-recheck', 'status':'verification_pending', 'success':None,
           'action_success':True, 'dry_run':False, 'namespace':'original', 'cluster_id':'original-cluster',
           'verification_target':{'service':'api'}, 'commands':['kubectl scale deployment/api --replicas=1'],
           'results':[{'ok':True}], 'finished_at':123,
           'verification':{'status':'unknown','recovered':None,'reason':'business_probe_unavailable'}}
    appmod._save_heal_run(run)
    return TestClient(appmod.app), run


@pytest.mark.parametrize('outcome', [True, False, None])
def test_recheck_updates_only_observation(setup, monkeypatch, outcome):
    client, original = setup
    calls=[]
    monkeypatch.setattr(appmod, '_verify_heal_recovery', lambda **kw: calls.append(kw) or {'status':'verified','recovered':True})
    monkeypatch.setattr(appmod, 'verify_business_recovery', lambda *a, **kw: {
        'status':'unknown' if outcome is None else 'verified', 'recovered':outcome,
        'verification_scope':'infrastructure_and_business_http'})
    res=client.post('/api/heal/runs/heal-recheck/verify', json={'namespace':'attacker','commands':['delete'], 'url':'http://untrusted'})
    assert res.status_code == 200, res.text
    run=res.json()
    assert calls[0]['namespace']=='original' and calls[0]['cluster_id']=='original-cluster'
    assert calls[0]['suggestion']['target']=={'service':'api'}
    assert run['results']==original['results'] and run['commands']==original['commands']
    assert run['finished_at']==123 and run['verification_history'][0]['verification']==original['verification']
    assert run['success'] is outcome
    assert run['status']==('verification_pending' if outcome is None else 'succeeded' if outcome else 'failed')
    assert run['recovery_history_record']['recorded'] is False
    assert client.get('/api/heal/runs/heal-recheck').json()==run
    if outcome is not None:
        assert client.post('/api/heal/runs/heal-recheck/verify').status_code==409


@pytest.mark.parametrize('change', [{'dry_run':True}, {'action_success':False}, {'status':'succeeded'},
    {'verification_target':None}, {'verification_target':{}}, {'rollback':{'dry_run':False}}])
def test_ineligible_records_do_not_probe(setup, monkeypatch, change):
    client, run=setup
    appmod._save_heal_run({**run, **change})
    monkeypatch.setattr(appmod, '_verify_heal_recovery', lambda **kw: pytest.fail('must not probe'))
    assert client.post('/api/heal/runs/heal-recheck/verify').status_code==409


def test_missing_config_cannot_downgrade_business_requirement(setup, monkeypatch):
    client,_=setup
    monkeypatch.setattr(appmod, '_verify_heal_recovery', lambda **kw: {'status':'verified','recovered':True})
    result=client.post('/api/heal/runs/heal-recheck/verify').json()
    assert result['status']=='verification_pending'
    assert result['verification']['reason']=='business_probe_required_for_reverification'


def test_exception_keeps_pending_and_records_attempt(setup, monkeypatch):
    client,_=setup
    def fail(**kw): raise RuntimeError('collector unavailable')
    monkeypatch.setattr(appmod, '_verify_heal_recovery', fail)
    result=client.post('/api/heal/runs/heal-recheck/verify').json()
    assert result['status']=='verification_pending'
    assert result['verification']['reason']=='verification_error'
    assert len(result['verification_history'])==1


def test_concurrent_recheck_and_rollback_conflict(setup, monkeypatch):
    client,_=setup
    entered,release=threading.Event(),threading.Event()
    def probe(**kw):
        entered.set()
        assert release.wait(10)
        return {'status':'unknown','recovered':None}
    monkeypatch.setattr(appmod, '_verify_heal_recovery', probe)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(appmod._reverify_heal_run,'heal-recheck')
        try:
            assert entered.wait(5)
            assert client.post('/api/heal/runs/heal-recheck/verify').status_code==409
            assert client.post('/api/heal/runs/heal-recheck/rollback',json={'dry_run':True}).status_code==409
        finally:
            release.set()
        assert future.result()['status']=='verification_pending'


def test_failed_persistence_does_not_claim_saved_success(setup, monkeypatch):
    client,original=setup
    monkeypatch.setattr(appmod,'_verify_heal_recovery',lambda **kw:{'status':'unknown','recovered':None})
    def fail(*a, **kw): raise OSError('disk unavailable')
    monkeypatch.setattr(appmod,'_atomic_write_json',fail)
    assert client.post('/api/heal/runs/heal-recheck/verify').status_code==503
    assert json.loads(appmod._HEAL_RUNS_FILE.read_text())[0]==original


def test_damaged_ledger_is_not_overwritten(setup):
    _,run=setup
    appmod._HEAL_RUNS_FILE.write_text('broken')
    with pytest.raises(appmod.HTTPException) as caught:
        appmod._save_heal_run(run)
    assert caught.value.status_code==503
    assert appmod._HEAL_RUNS_FILE.read_text()=='broken'


def test_concurrent_saves_preserve_all_runs(setup):
    _,run=setup
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda i:appmod._save_heal_run({**run,'id':f'heal-{i}'}),range(20)))
    assert len(appmod._load_heal_runs())==21


def test_unknown_and_unsafe_ids(setup):
    client,_=setup
    assert client.post('/api/heal/runs/heal-unknown/verify').status_code==404
    assert client.post('/api/heal/runs/bad%20id/verify').status_code==400


def test_cancelled_rollback_keeps_worker_lock_and_durable_intent(setup, monkeypatch):
    import asyncio
    _,run=setup
    run['rollback_commands']=['kubectl scale deployment/api --replicas=0']
    appmod._save_heal_run(run)
    entered,release,finished=threading.Event(),threading.Event(),threading.Event()
    calls=[]
    def shell(*args):
        calls.append(args)
        entered.set()
        assert release.wait(10)
        return {'ok':True}
    monkeypatch.setattr(appmod,'_shell_sync',shell)
    original=appmod._heal_run_rollback_locked
    def worker(*args):
        try:return original(*args)
        finally:finished.set()
    monkeypatch.setattr(appmod,'_heal_run_rollback_locked',worker)
    class Request:
        async def json(self):return {'dry_run':False,'approved':True}
    async def scenario():
        task=asyncio.create_task(appmod.heal_run_rollback('heal-recheck',Request()))
        try:
            assert await asyncio.to_thread(entered.wait,5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
            assert appmod._load_heal_runs()[0]['rollback']['status']=='running'
            with pytest.raises(appmod.HTTPException) as caught:
                appmod._reverify_heal_run('heal-recheck')
            assert caught.value.status_code==409
        finally:
            release.set()
            assert await asyncio.to_thread(finished.wait,5)
    asyncio.run(scenario())
    assert len(calls)==1
    assert appmod._load_heal_runs()[0]['rollback']['status']=='completed'


def test_rollback_unknown_refuses_retry_and_recheck(setup, monkeypatch):
    client,run=setup
    run['rollback_commands']=['kubectl scale deployment/api --replicas=0']
    appmod._save_heal_run(run)
    calls=[]
    monkeypatch.setattr(appmod,'_shell_sync',lambda *a:calls.append(a) or {'ok':False})
    first=client.post('/api/heal/runs/heal-recheck/rollback',json={'dry_run':False,'approved':True})
    assert first.status_code==200 and first.json()['status']=='outcome_unknown'
    assert client.post('/api/heal/runs/heal-recheck/rollback',json={'dry_run':False,'approved':True}).status_code==409
    assert client.post('/api/heal/runs/heal-recheck/verify').status_code==409
    assert len(calls)==1


def test_preview_does_not_block_recheck_or_erase_unknown_rollback(setup, monkeypatch):
    client,run=setup
    run['rollback_commands']=['kubectl scale deployment/api --replicas=0']
    appmod._save_heal_run(run)
    monkeypatch.setattr(appmod,'_verify_heal_recovery',lambda **kw:{'status':'unknown','recovered':None})
    assert client.post('/api/heal/runs/heal-recheck/rollback',json={'dry_run':True}).status_code==200
    assert client.post('/api/heal/runs/heal-recheck/verify').status_code==200
    run=appmod._load_heal_runs()[0]
    run['rollback']={'dry_run':False,'status':'outcome_unknown'}
    appmod._save_heal_run(run)
    assert client.post('/api/heal/runs/heal-recheck/rollback',json={'dry_run':True}).status_code==200
    assert appmod._load_heal_runs()[0]['rollback']['status']=='outcome_unknown'
    assert client.post('/api/heal/runs/heal-recheck/verify').status_code==409


@pytest.mark.parametrize('change', [
    {'dry_run': True}, {'status': 'outcome_unknown', 'action_success': None},
    {'action_success': False}, {'results': []},
    {'results': [{'ok': True, 'dry_run': True}]},
    {'results': [{'ok': True, 'outcome_unknown': True}]},
    {'results': [{'ok': True, 'executed': False}]},
    {'commands': ['one', 'two']},
])
def test_real_rollback_requires_confirmed_forward_execution(setup, monkeypatch, change):
    client, run = setup
    undo = 'kubectl scale deployment/api --replicas=0'
    appmod._save_heal_run({**run, 'rollback_commands': [undo], **change})
    calls = []
    monkeypatch.setattr(appmod, '_shell_sync', lambda *args: calls.append(args) or {'ok': True})
    response = client.post('/api/heal/runs/heal-recheck/rollback', json={'dry_run': False, 'approved': True})
    assert response.status_code == 409
    assert calls == []


def test_completed_real_rollback_is_idempotent_after_preview(setup, monkeypatch):
    client, run = setup
    undo = ['kubectl scale deployment/api --replicas=0', 'kubectl scale deployment/worker --replicas=0']
    run.update(rollback_commands=undo, commands=['scale api', 'scale worker'], results=[{'ok': True}, {'ok': True}])
    appmod._save_heal_run(run)
    calls = []
    monkeypatch.setattr(appmod, '_shell_sync', lambda command, *args: calls.append(command) or {'ok': True})
    url = '/api/heal/runs/heal-recheck/rollback'
    first = client.post(url, json={'dry_run': False, 'approved': True})
    assert first.status_code == 200 and first.json()['status'] == 'completed'
    assert client.post(url, json={'dry_run': True}).status_code == 200
    assert client.post(url, json={'dry_run': False, 'approved': True}).json() == first.json()
    assert calls == list(reversed(undo))


@pytest.mark.parametrize('field,value', [('approved', 'false'), ('dry_run', None), ('dry_run', 0)])
def test_invalid_rollback_flags_do_not_dispatch(setup, monkeypatch, field, value):
    client, run = setup
    run['rollback_commands'] = ['kubectl scale deployment/api --replicas=0']
    appmod._save_heal_run(run)
    calls = []
    monkeypatch.setattr(appmod, '_shell_sync', lambda *args: calls.append(args) or {'ok': True})
    body = {'dry_run': False, 'approved': True, field: value}
    assert client.post('/api/heal/runs/heal-recheck/rollback', json=body).status_code == 400
    assert calls == []


def test_failed_probe_cannot_confirm_recovery(setup, monkeypatch):
    client, _ = setup
    monkeypatch.setattr(appmod, '_verify_heal_recovery', lambda **kw: {'status': 'verified', 'recovered': True})
    monkeypatch.setattr(appmod, 'verify_business_recovery', lambda *a, **kw: {
        'status': 'failed', 'recovered': True, 'verification_scope': 'infrastructure_and_business_http'})
    run = client.post('/api/heal/runs/heal-recheck/verify').json()
    assert run['status'] == 'verification_pending'
    assert run['success'] is None


def test_auto_heal_record_shows_late_verification_without_dispatch(setup, monkeypatch):
    client, run = setup
    claim = {'source_run_id': 'rca-one', 'heal_run_id': run['id'], 'status': 'verification_pending',
             'success': None, 'verification': run['verification']}
    appmod._atomic_write_json(appmod._HEAL_ARTIFACT_DIR / 'auto-rca-one.json', claim)
    monkeypatch.setattr(appmod, '_verify_heal_recovery', lambda **kw: {'status': 'verified', 'recovered': True})
    monkeypatch.setattr(appmod, 'verify_business_recovery', lambda *a, **kw: {
        'status': 'verified', 'recovered': True, 'verification_scope': 'infrastructure_and_business_http'})
    checked = client.post('/api/heal/runs/heal-recheck/verify').json()
    assert checked['status'] == 'succeeded'
    shown = client.get('/api/rca/rca-one/auto-heal').json()
    assert shown['status'] == checked['status'] and shown['verification'] == checked['verification']


@pytest.mark.parametrize('field', ['approved', 'risk_ack', 'dry_run', 'auto_execute'])
def test_execute_requires_boolean_flags_before_planning(setup, field):
    client, _ = setup
    response = client.post('/api/heal/execute', json={field: 'false'})
    assert response.status_code == 400
    assert field in response.json()['detail']


@pytest.mark.parametrize('saved', ['complete', 'absent', 'incomplete', 'different_target', 'different_level', 'wrong_version'])
def test_infrastructure_recheck_requires_original_matching_scope(setup, monkeypatch, saved):
    from recovery_verification import verification_contract

    client, run = setup
    contract = verification_contract(profiles=[], cluster_id=run['cluster_id'],
                                     namespace=run['namespace'], service='api')
    if saved != 'absent':
        scope = dict(contract)
        if saved == 'incomplete':
            scope['verification_contract_complete'] = False
        if saved == 'wrong_version':
            scope['verification_contract_version'] = 'unknown'
        if saved == 'different_level':
            scope['verification_level'] = 'http_headers'
        if saved == 'different_target':
            scope = verification_contract(profiles=[], cluster_id=run['cluster_id'],
                                          namespace=run['namespace'], service='other')
        run['recovery_history_scope'] = scope
    appmod._save_heal_run(run)
    monkeypatch.setattr(appmod, '_verify_heal_recovery', lambda **kw: {'status':'verified', 'recovered':True})
    for _ in range(1 if saved == 'complete' else 2):
        response = client.post('/api/heal/runs/heal-recheck/verify')
        assert response.status_code == 200
        result = response.json()
        assert result['recovery_history_record']['recorded'] is False
        if saved == 'complete':
            assert result['status'] == 'succeeded' and result['success'] is True
            assert result['verification']['verification_scope'] == 'infrastructure'
            assert result['verification']['verification_fingerprint'] == contract['verification_fingerprint']
        else:
            assert result['status'] == 'verification_pending' and result['success'] is None
