import asyncio
import copy
from types import SimpleNamespace

import pytest

from agents.alert_compression import CompressionStore, compress_alerts
from agents.alert_lifecycle import AlertLifecycle


def alert(**changes):
    return dict(dict(name='KubeJobFailed', source='prometheus', cluster_id='c1', namespace='shop',
                     timestamp=1000, message='', severity='warning', labels={'job_name': 'backup', 'pod': 'exporter'}), **changes)


def scan(rows, at, *, scope='', cluster='c1', **changes):
    data = compress_alerts(rows)
    data.update(scan_id=f'scan-{at}-{scope}', revision=1, history=[], origin='live_scan', cluster_id=cluster,
                scan_scope=scope, collection_started_at=at, collection={'prometheus': 'ok', 'kubectl': 'ok'})
    data.update(changes)
    return data


def test_repeats_resolution_recurrence_and_process_restart(tmp_path):
    path = tmp_path/'lifecycle.sqlite3'
    store = AlertLifecycle(path)
    first = store.observe(scan([alert()], 1000))
    a = first['groups'][0]['lifecycle'][0]
    again = store.observe(scan([alert(timestamp=1020)], 1020))
    b = again['groups'][0]['lifecycle'][0]
    assert a['incident_id'] == b['incident_id'] and b['episode'] == 1 and b['observations'] == 2
    assert b['first_observed_at'] == 1000 and b['last_observed_at'] == 1020
    store.observe(scan([], 1030))
    assert store.list('c1')[0]['status'] == 'pending_resolution'
    store.observe(scan([], 1031))
    assert store.list('c1')[0]['status'] == 'pending_resolution'
    store = AlertLifecycle(path)
    store.observe(scan([], 1060))
    state = store.list('c1')[0]
    assert state['status'] == 'resolved' and state['business_recovery_verified'] is False
    reopened = store.observe(scan([alert()], 1070))['groups'][0]['lifecycle'][0]
    assert reopened['incident_id'] == a['incident_id'] and reopened['episode'] == 2
    assert reopened['first_observed_at'] == 1000 and reopened['episode_started_at'] == 1070
    assert [h['action'] for h in store.list('c1')[0]['history']] == ['opened', 'resolved', 'reopened']


@pytest.mark.parametrize('changes', [dict(degraded=True), dict(origin='import'), dict(revision=2),
    dict(collection={'prometheus': 'TimeoutError', 'kubectl': 'ok'}), dict(collection={}),
    dict(collection={'prometheus': 'ok', 'kubectl': 'invalid_response'})])
def test_incomplete_or_imported_scans_never_close_alerts(tmp_path, changes):
    store = AlertLifecycle(tmp_path/'state.db')
    store.observe(scan([alert()], 1000))
    for at in (1100, 1200):
        assert store.observe(scan([], at, **changes))['lifecycle']['status'] == 'ineligible'
    assert store.list('c1')[0]['status'] == 'active'


def test_duplicate_and_out_of_order_scans_do_not_increment_or_resolve(tmp_path):
    store = AlertLifecycle(tmp_path/'state.db')
    live = scan([alert()], 1100)
    store.observe(live)
    store.observe(live)
    for at in (1000, 1030):
        assert store.observe(scan([], at))['lifecycle']['status'] == 'stale'
    assert store.list('c1')[0]['observations'] == 1
    assert store.list('c1')[0]['status'] == 'active'


def test_scoped_and_global_observations_share_identity_but_not_unsafe_closure(tmp_path):
    store = AlertLifecycle(tmp_path/'state.db')
    store.observe(scan([alert(), alert(namespace='other')], 1000))
    store.observe(scan([alert()], 1100, scope='shop'))
    store.observe(scan([], 1110, scope='other'))
    store.observe(scan([], 1140, scope='other'))
    states = {s['namespace']: s for s in store.list('c1')}
    assert states['shop']['observations'] == 2 and states['shop']['status'] == 'active'
    assert states['other']['status'] == 'resolved'
    store.observe(scan([], 1050))  # Older full scan cannot undo a newer scoped scan.
    assert store.list('c1', 'shop')[0]['status'] == 'active'
    assert not store.list('c2')


def test_warning_events_expire_without_claiming_recovery(tmp_path):
    store = AlertLifecycle(tmp_path/'state.db')
    event = alert(source='k8s_event', raw={'metadata': {'uid': 'event-uid'}})
    store.observe(scan([event], 1000))
    store.observe(scan([dict(event, timestamp=1030)], 1040))
    assert store.list('c1')[0]['status'] == 'observed'
    store.observe(scan([], 1100))
    store.observe(scan([], 1130))
    state = store.list('c1')[0]
    assert state['status'] == 'expired' and state['resolved_at'] is None
    assert state['resolution_basis'] == 'event_absent_not_recovery'


def test_exporter_rotation_severity_changes_and_multiple_jobs(tmp_path):
    store = AlertLifecycle(tmp_path/'state.db')
    store.observe(scan([alert()], 1000))
    store.observe(scan([alert(severity='critical', labels={'job_name': 'backup', 'pod': 'exporter-new', 'severity': 'critical'}),
                        alert(labels={'job_name': 'other', 'pod': 'exporter-new'})], 1030))
    states = {s['entity']: s for s in store.list('c1')}
    assert len(states) == 2 and states['backup']['observations'] == 2


def test_dossier_and_saved_scan_include_lifecycle(tmp_path):
    lifecycle = AlertLifecycle(tmp_path/'state.db')
    store = CompressionStore(tmp_path/'scans')
    saved = store.create(scan([alert()], 1000), lifecycle=lifecycle)
    group = saved['groups'][0]
    dossier = store.dossier(saved['scan_id'], group['group_id'], 1)
    assert dossier['lifecycle'][0]['incident_id'] == group['lifecycle'][0]['incident_id']
    assert store.get(saved['scan_id'])['lifecycle']['status'] == 'ok'


@pytest.mark.parametrize('payload', [None, {}, {'results': None}, {'results': 'not-a-list'}])
def test_invalid_successful_collector_payload_is_not_empty_complete_snapshot(payload):
    from agents.alert_agent import AlertAgent
    cfg = SimpleNamespace(kubernetes=SimpleNamespace(cluster_id='c1'),
        alert=SimpleNamespace(time_window=300, similarity_threshold=.8, max_group_size=100))
    registry = SimpleNamespace(get=lambda name: SimpleNamespace(execute=lambda **kw:
        SimpleNamespace(success=True, data=payload if name == 'prometheus' else {'items': []})))
    agent = AlertAgent(None, registry, cfg)
    asyncio.run(agent._collect_alerts())
    assert agent.collection_status['prometheus'] == 'invalid_response'


def test_database_failure_degrades_without_losing_raw_scan(tmp_path):
    class Broken:
        def observe(self, scan):
            raise OSError('unavailable')
    saved = CompressionStore(tmp_path).create(scan([alert()], 1000), lifecycle=Broken())
    assert saved['lifecycle']['status'] == 'unavailable' and saved['total_alerts'] == 1


def test_lifecycle_read_api_is_cluster_scoped(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import web_app.app as webapp
    monkeypatch.setattr(webapp, 'ROOT_DIR', tmp_path)
    monkeypatch.setattr(webapp, '_resolve_general_cluster_id', lambda cluster: cluster)
    store = AlertLifecycle(tmp_path/'data'/'alert_compression'/'lifecycle.sqlite3')
    store.observe(scan([alert()], 1000))
    client = TestClient(webapp.app)
    assert len(client.get('/api/alerts/lifecycle?cluster_id=c1&namespace=shop').json()['incidents']) == 1
    assert client.get('/api/alerts/lifecycle?cluster_id=c2').json()['incidents'] == []


def test_truncated_vector_cannot_be_treated_as_complete_snapshot():
    from agents.alert_agent import AlertAgent
    cfg = SimpleNamespace(kubernetes=SimpleNamespace(cluster_id='c1'),
        alert=SimpleNamespace(time_window=300, similarity_threshold=.8, max_group_size=100))
    calls = []
    def execute(name, **kw):
        calls.append((name, kw))
        return SimpleNamespace(success=True, data={'results': [], 'result_count': 51, 'truncated': True}
                               if name == 'prometheus' else {'items': []})
    registry = SimpleNamespace(get=lambda name: SimpleNamespace(execute=lambda **kw: execute(name, **kw)))
    agent = AlertAgent(None, registry, cfg)
    result = asyncio.run(agent.compress_and_recommend(namespace='shop', enrich=False))
    assert result['degraded'] and result['collection']['prometheus'] == 'truncated'
    assert next(kw for name, kw in calls if name == 'prometheus')['max_results'] == 5000


def test_alert_collection_limit_is_independent_of_default_llm_query_limit(monkeypatch):
    from tools.observability import PrometheusTool
    tool = PrometheusTool('http://unused')
    monkeypatch.setattr(tool, '_ensure_reachable', lambda: None)
    vector = [{'metric': {'alertname': str(i)}} for i in range(75)]
    response = SimpleNamespace(raise_for_status=lambda: None,
        json=lambda: {'status': 'success', 'data': {'result': vector}})
    monkeypatch.setattr(tool.session, 'get', lambda *a, **kw: response)
    default = tool.execute(query='ALERTS')
    assert len(default.data['results']) == 50 and default.data['truncated']
    full = tool.execute(query='ALERTS', max_results=5000)
    assert len(full.data['results']) == 75 and not full.data['truncated']
