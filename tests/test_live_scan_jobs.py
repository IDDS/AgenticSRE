import asyncio
import copy
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from agents.alert_compression import CompressionStore, compress_alerts
from agents.alert_jobs import AlertJobs
from agents.alert_lifecycle import AlertLifecycle
import web_app.app as dashboard


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard, 'ROOT_DIR', tmp_path)
    monkeypatch.setattr(dashboard, '_resolve_general_cluster_id', lambda c: c or 'c')
    monkeypatch.setattr(dashboard, '_cluster_scoped_config', lambda _: SimpleNamespace(
        llm=SimpleNamespace(), alert=SimpleNamespace(time_window=300)))
    monkeypatch.setattr(dashboard, '_llm_configured', lambda _: False)
    async def idle(): pass
    monkeypatch.setattr(dashboard, '_ensure_alert_job_worker', idle)
    return TestClient(dashboard.app)


def test_live_scan_is_accepted_before_collection_and_submission_is_idempotent(setup, monkeypatch):
    async def unexpected(*a, **kw):
        pytest.fail('HTTP submission must not collect or call a model')
    monkeypatch.setattr(dashboard, '_prepare_live_alert_scan', unexpected, raising=False)
    body = {'cluster_id': 'c', 'job_id': 'job-' + '1' * 32}
    first = setup.post('/api/alerts/scan-jobs', json=body)
    assert first.status_code == 202
    assert first.json()['status'] == 'queued'
    assert setup.post('/api/alerts/scan-jobs', json=body).json()['job_id'] == body['job_id']
    store = dashboard._alert_jobs()
    assert store.claim()[1]['source'] == 'live_scan'
    assert store.claim() is None
    assert setup.post('/api/alerts/scan-jobs', json={**body, 'cluster_id':'other'}).status_code == 409


def test_import_cannot_claim_live_scan_provenance(setup):
    response = setup.post('/api/alerts/compression-jobs', json={
        'alerts': [], 'source':'live_scan', 'cluster_id':'c'})
    assert response.status_code == 202
    assert dashboard._alert_jobs().claim()[1]['source'] == 'import'


def test_live_job_persists_lifecycle_once_and_reuses_prepared_snapshot(setup, monkeypatch, tmp_path):
    result = compress_alerts([{'name':'PodFailure', 'message':'crash', 'cluster_id':'c',
                              'namespace':'ns', 'pod':'p', 'timestamp': time.time(), 'source':'prometheus'}])
    result.update(origin='live_scan', scan_scope='ns', collection_started_at=time.time(),
                  collection={'prometheus':'ok', 'kubectl':'ok'}, cluster_id='c', candidate_pairs=[])
    calls = []
    async def collect(namespace, cluster, *, for_job):
        calls.append((namespace, cluster, for_job))
        return copy.deepcopy(result)
    monkeypatch.setattr(dashboard, '_prepare_live_alert_scan', collect, raising=False)
    store = dashboard._alert_jobs()
    payload = {'source':'live_scan', 'cluster_id':'c', 'namespace':'ns', 'semantic':False}
    job = store.create(payload)
    key, payload = store.claim()
    output = dashboard._compression_store()
    async def process():
        await store.process(key, payload, dashboard._prepare_alert_job, lambda _: (None, 1),
                            output, dashboard._finalize_alert_job)
    asyncio.run(process())
    completed = store.get(key)
    assert completed['status'] == 'completed'
    saved = output.get(completed['scan_id'])
    assert saved['origin'] == 'live_scan' and saved['collection'] == result['collection']
    lifecycle = AlertLifecycle(tmp_path/'data/alert_compression/lifecycle.sqlite3')
    before = lifecycle.list('c', 'ns')
    assert len(before) == 1
    # Crash after writing output but before committing job completion.
    with store.connect() as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (key,))
    asyncio.run(process())
    assert lifecycle.list('c', 'ns') == before
    assert calls == [('ns', 'c', True)]
    assert store.get(key)['scan_id'] == saved['scan_id']


def test_cancelled_live_job_does_not_publish_lifecycle(setup, monkeypatch):
    store = dashboard._alert_jobs()
    key = store.create({'source':'live_scan', 'semantic':False})['job_id']
    key, payload = store.claim()
    async def prepare(_):
        store.control(key, 'cancel')
        return {'candidate_pairs':[]}
    async def finalize(*_):
        pytest.fail('cancelled job must not publish a live observation')
    asyncio.run(store.process(key, payload, prepare, lambda _: (None, 1),
                              dashboard._compression_store(), finalize))
    assert store.get(key)['status'] == 'cancelled'


@pytest.mark.parametrize('path', ['/api/alerts/scan-jobs', '/api/alerts/compression-jobs'])
@pytest.mark.parametrize('budget', [{'max_model_calls': -1}, {'max_model_calls': True}, {'unknown': 2}])
def test_invalid_review_budget_is_rejected_before_queueing(setup, path, budget):
    response = setup.post(path, json={'cluster_id': 'c', 'alerts': [], 'review_budget': budget})
    assert response.status_code == 400
    assert dashboard._alert_jobs().claim() is None


def test_partial_job_api_exposes_summary_raw_evidence_and_pending_page(setup, monkeypatch):
    monkeypatch.setattr(dashboard, '_llm_configured', lambda _: True)
    response = setup.post('/api/alerts/scan-jobs', json={
        'cluster_id': 'c', 'review_budget': {'max_model_calls': 0}})
    assert response.status_code == 202
    store = dashboard._alert_jobs()
    key, payload = store.claim()
    assert payload['review_budget']['max_model_calls'] == 0
    async def prepare(_):
        result = compress_alerts([dict(name='Failure', message='failed', timestamp=1000,
            cluster_id='c', namespace='ns', pod=pod) for pod in ('a', 'b')])
        a, b = result['groups']
        result['candidate_pairs'] = [dict(left=a['group_id'], right=b['group_id'])]
        return result
    class Forbidden:
        async def async_json_chat(self, *a, **kw):
            pytest.fail('zero budget must not call a model')
    asyncio.run(store.process(key, payload, prepare, lambda _: (Forbidden(), 60), dashboard._compression_store()))
    info = setup.get('/api/alerts/compression-jobs/'+key).json()
    assert info['status'] == 'partial' and info['can_resume'] is False
    summary = setup.get(info['result_url'])
    assert summary.status_code == 200 and summary.json()['semantic']['deferred_pairs'] == 1
    base = '/api/alerts/compression/'+info['scan_id']
    raw = setup.get(base+'/page?section=raw_alerts').json()
    pending = setup.get(base+'/page?section=review_pairs').json()
    assert raw['total'] == 2 and pending['items'][0]['review_status'] == 'deferred'
    assert setup.post('/api/alerts/compression-jobs/'+key+'/resume').status_code == 409
