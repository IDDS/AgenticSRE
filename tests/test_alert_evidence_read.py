import asyncio
import copy
import json

import pytest
from fastapi.testclient import TestClient

from agents.alert_compression import CompressionStore, compress_alerts, make_group, regroup_stats
from agents.alert_evidence_read import hydrate_dossier, encoded
from agents.diagnosis_grounding import claim_context, evidence_catalog, apply_alert_grounding


def fixture(tmp_path, messages=None):
    messages = messages or ['ordinary failure'] * 21
    data = compress_alerts([dict(name='Failure', source='logs', message=msg,
        cluster_id='c', namespace='ns', pod='pod-a', timestamp=1000+i,
        severity='warning') for i, msg in enumerate(messages)])
    data['groups'] = [make_group(data['raw_alerts'], list(range(len(messages))), 'manual_merge')]
    regroup_stats(data)
    store = CompressionStore(tmp_path)
    scan = store.create(data)
    return store, scan, scan['groups'][0]['group_id']


def test_pages_reconstruct_all_originals_and_refuse_changed_revision(tmp_path):
    store, scan, group = fixture(tmp_path)
    before = copy.deepcopy(scan)
    offset, actual, digests = 0, [], set()
    while offset is not None:
        page = store.evidence_page(scan['scan_id'], group, 1, offset=offset, limit=4)
        actual.extend(page['records'])
        digests.add(page['ref']['snapshot_sha256'])
        offset = page['next_offset']
    assert actual == scan['raw_alerts'] and len(digests) == 1
    assert store.get(scan['scan_id']) == before
    store.edit(scan['scan_id'], revision=1, action='split', group_ids=[group],
               alert_ids=[actual[0]['alert_id']], reason='split for evidence revision test')
    with pytest.raises(ValueError, match='stale revision'):
        store.evidence_page(scan['scan_id'], group, 1, offset=4)


@pytest.mark.parametrize('offset,limit', [(-1, 1), (True, 1), (0, True), (0, 0), (0, 101), (22, 1)])
def test_invalid_paging_is_rejected(tmp_path, offset, limit):
    store, scan, group = fixture(tmp_path)
    with pytest.raises(ValueError):
        store.evidence_page(scan['scan_id'], group, 1, offset=offset, limit=limit)


@pytest.mark.parametrize('mutation', ['namespace', 'cluster_id', 'duplicate_index', 'wrong_id', 'wrong_count'])
def test_corrupt_membership_cannot_cross_group_scope(tmp_path, mutation):
    store, scan, group = fixture(tmp_path)
    if mutation in ('namespace', 'cluster_id'):
        scan['raw_alerts'][10][mutation] = 'other'
    if mutation == 'duplicate_index':
        scan['groups'][0]['alert_indices'][10] = 0
    if mutation == 'wrong_id':
        scan['groups'][0]['alert_ids'][10] = 'a-other'
    if mutation == 'wrong_count':
        scan['groups'][0]['alert_count'] += 1
    store._write(store._path(scan['scan_id']), scan)
    with pytest.raises(ValueError):
        store.evidence_page(scan['scan_id'], group, 1)


def test_middle_rare_message_and_tail_after_500_reach_real_report_context(tmp_path):
    rare = 'diagnostic prefix ' * 35 + ' ConfigMap required-settings not found'
    assert 500 < len(rare) < 1000
    messages = ['ordinary failure'] * 21
    messages[10] = rare
    store, scan, group = fixture(tmp_path, messages)
    old = store.dossier(scan['scan_id'], group, 1)
    assert all('required-settings' not in sample['message'] for sample in old['alert_samples'])
    new = store.dossier(scan['scan_id'], group, 1, for_diagnosis=True)
    evidence = {'agent_skill_context': {'payload': new}}
    context = json.loads(claim_context(evidence))
    aid = scan['raw_alerts'][10]['alert_id']
    assert context['observations'][0]['id'] == aid
    assert 'required-settings not found' in context['observations'][0]['observation']
    assert evidence_catalog(evidence)[aid]['signal_type'] == 'alert_observed'
    assert new['raw_evidence']['selected_chars'] <= 12000
    assert store.get(scan['scan_id']) == scan


def test_duplicate_notifications_do_not_fill_unused_model_budget(tmp_path):
    store, scan, group = fixture(tmp_path, ['same notification'] * 1000)
    dossier = store.dossier(scan['scan_id'], group, 1, for_diagnosis=True)
    ids = [s['alert_id'] for s in dossier['alert_samples']]
    assert ids == [scan['raw_alerts'][0]['alert_id'], scan['raw_alerts'][-1]['alert_id']]
    assert dossier['raw_evidence']['omitted_alerts'] == 998
    assert dossier['alert_count'] == len(dossier['raw_alert_ids']) == 1000
    context = json.loads(claim_context({'agent_skill_context': {'payload': dossier}}))
    assert context['alert_scope']['entity_count'] == 1 and context['alert_scope']['total_alerts'] == 1000


@pytest.mark.parametrize('message', ['configmap "cfg" not found', 'path C:\\data\\input missing',
                                   'first line\nsecond line', 'literal \\n is not a newline', '字段\t未找到：配置甲'])
def test_original_quotes_backslashes_and_newlines_are_literal_not_double_encoded(tmp_path, message):
    store, scan, group = fixture(tmp_path, [message])
    payload = store.dossier(scan['scan_id'], group, 1, for_diagnosis=True)
    evidence = {'agent_skill_context': {'payload': payload}}
    aid = payload['alert_samples'][0]['alert_id']
    context = json.loads(claim_context(evidence))
    assert message in context['observations'][0]['observation']
    claim = dict(id='C1', kind='observation', statement=message,
                 supports=[dict(evidence_id=aid, quote=message)])
    report = dict(alert_citations=[aid], diagnosis_claims=[claim], root_cause_claim_ids=[])
    final, _ = apply_alert_grounding(report, {}, evidence)
    assert final['claim_validation']['status'] == 'passed'
    report['diagnosis_claims'][0]['supports'][0]['quote'] = message + ' invented suffix'
    final, _ = apply_alert_grounding(report, {}, evidence)
    assert final['claim_validation']['status'] == 'requires_review'


def test_large_group_budget_and_excerpt_are_explicit_and_original_retrievable(tmp_path):
    store, scan, group = fixture(tmp_path, ['x' * 9000] * 100)
    dossier = store.dossier(scan['scan_id'], group, 1)
    loaded = hydrate_dossier(dossier, scan, max_chars=6000, max_records=3)
    coverage = loaded['raw_evidence']
    assert len(encoded(loaded['alert_samples'])) <= 6000
    assert coverage['loaded_alerts'] == coverage['excerpted_messages'] == 1
    assert coverage['omitted_alerts'] == 99
    page = store.evidence_page(scan['scan_id'], group, 1)
    assert page['records'][0]['message'] == 'x' * 9000
    assert page['ref']['snapshot_sha256'] == coverage['snapshot_sha256']
    evidence = {'agent_skill_context': {'payload': loaded}}
    context = json.loads(claim_context(evidence))
    assert context['observations'][0]['excerpt']
    final, _ = apply_alert_grounding({}, {}, evidence)
    assert final['alert_evidence_coverage']['prompt_alerts'] == 1
    assert final['alert_evidence_coverage']['prompt_excerpted_messages'] == 1
    assert final['diagnosis_status'] == 'requires_review'


def test_record_count_cap_and_snapshot_isolation(tmp_path):
    store, scan, group = fixture(tmp_path, [f'unique error code={i}' for i in range(40)])
    dossier = hydrate_dossier(store.dossier(scan['scan_id'], group, 1), scan, max_chars=32000)
    assert dossier['raw_evidence']['loaded_alerts'] == 32
    before = copy.deepcopy(dossier)
    store.edit(scan['scan_id'], revision=1, action='split', group_ids=[group],
               alert_ids=[scan['raw_alerts'][0]['alert_id']], reason='later manual edit')
    assert dossier == before and dossier['revision'] == 1
    with pytest.raises(ValueError):
        store.evidence_page(scan['scan_id'], group, 1)


def test_mixed_object_samples_keep_their_own_identity(tmp_path):
    store, scan, group = fixture(tmp_path)
    scan['raw_alerts'][10].update(entity='pod-b', message='independent failure')
    store._write(store._path(scan['scan_id']), scan)
    dossier = store.dossier(scan['scan_id'], group, 1, for_diagnosis=True)
    catalog = evidence_catalog({'agent_skill_context': {'payload': dossier}})
    assert catalog[scan['raw_alerts'][10]['alert_id']]['service'] == 'pod-b'


def test_missing_entity_is_unknown_instead_of_one_confirmed_object(tmp_path):
    store, scan, group = fixture(tmp_path)
    scan['raw_alerts'][0].update(entity='', entity_kind='')
    store._write(store._path(scan['scan_id']), scan)
    payload = store.dossier(scan['scan_id'], group, 1, for_diagnosis=True)
    assert payload['raw_evidence']['entity_count'] is None


def test_duplicate_error_text_does_not_make_imported_alert_a_mechanism_source(tmp_path):
    message = 'configmap "required" not found'
    store, scan, group = fixture(tmp_path, [message])
    payload = store.dossier(scan['scan_id'], group, 1, for_diagnosis=True)
    payload['alert_samples'][0]['signal_type'] = 'pod_waiting_state'
    evidence = {'agent_skill_context': {'payload': payload}, 'event_agent': {'evidence_items': [dict(
        source='kubectl get pods -o json', signal_type='pod_waiting_state', service='pod-a',
        observation=message, raw_ref={'namespace':'ns'})]}}
    context = json.loads(claim_context(evidence))
    live, imported = context['observations']
    assert live['id'].startswith('ev-') and live['mechanism_eligible']
    assert imported['id'].startswith('a-') and not imported['mechanism_eligible']
    claim = dict(id='C1', kind='mechanism', statement='The missing ConfigMap prevents startup',
                 supports=[dict(evidence_id=imported['id'], quote=message)])
    report = dict(alert_citations=[imported['id']], diagnosis_claims=[claim], root_cause_claim_ids=['C1'],
                  root_entity={'namespace':'ns','name':'pod-a'},confidence=.99)
    judge = dict(needs_review=False,llm_based={'claim_reviews':[dict(claim_id='C1',statement=claim['statement'],status='supported')]})
    final, _ = apply_alert_grounding(report, judge, evidence)
    assert final['claim_validation']['rejected'][0]['reason'] == 'symptoms_do_not_establish_mechanism'
    report['diagnosis_claims'][0]['supports'][0]['evidence_id'] = live['id']
    final, _ = apply_alert_grounding(report, judge, evidence)
    assert final['diagnosis_status'] == 'supported_hypothesis'


def test_api_revision_is_mandatory_and_bad_groups_rejected(tmp_path, monkeypatch):
    import web_app.app as app
    store, scan, group = fixture(tmp_path)
    monkeypatch.setattr(app, '_compression_store', lambda: store)
    client = TestClient(app.app)
    url = f"/api/alerts/compression/{scan['scan_id']}/groups/{group}/evidence"
    assert client.get(url).status_code == 422
    assert client.get(url, params={'revision': 1, 'limit': 3}).json()['records'] == scan['raw_alerts'][:3]
    assert client.get(url, params={'revision': 2}).status_code == 409
    assert client.get(url.replace(group, 'g-other'), params={'revision': 1}).status_code == 409
    assert client.get(url, params={'revision': 1, 'limit': 101}).status_code == 422


def test_rca_api_passes_hydrated_snapshot_to_pipeline(tmp_path, monkeypatch):
    import web_app.app as app
    from types import SimpleNamespace
    from threading import Event
    store, scan, group = fixture(tmp_path, ['x' * 700 + ' exact tail'])
    seen = []
    done = Event()
    class Pipeline:
        async def run(self, *args, **kwargs):
            seen.append(kwargs['initial_evidence']['payload'])
            return SimpleNamespace(status='completed', to_dict=lambda: {'status': 'completed'})
    monkeypatch.setattr(app, '_compression_store', lambda: store)
    monkeypatch.setattr(app, '_resolve_rca_cluster_scope', lambda *args: ('', None))
    monkeypatch.setattr(app, '_llm_configured', lambda *args: True)
    monkeypatch.setattr(app, '_get_pipeline', Pipeline)
    def saved(*, required_run_id=None):
        if any(row['status'] == 'completed' for row in app._state['rca_runs'].values()):
            done.set()
        return True
    monkeypatch.setattr(app, '_save_rca_history', saved)
    monkeypatch.setitem(app._state, 'rca_runs', {})
    monkeypatch.setitem(app._state, 'rca_dedup', {})
    body = dict(alert_group=dict(scan_id=scan['scan_id'], group_id=group, revision=1))
    class Request:
        async def json(self): return body
    asyncio.run(app.rca_run(Request()))
    assert done.wait(5)
    assert len(seen) == 1 and seen[0]['alert_samples'][0]['message'].endswith(' exact tail')
    assert seen[0]['raw_evidence']['loaded_alerts'] == 1
