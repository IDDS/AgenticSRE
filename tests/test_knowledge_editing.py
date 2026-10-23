"""Remote regression: durable recipe edits and lossless diagnostic-rule updates."""
import copy
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import web_app.app as dashboard
from memory.fault_context_store import FaultContextStore
from skill_runtime.adapters import legacy


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard, "HEAL_RECIPES_STATE", tmp_path / "heal_recipe_library.json", raising=False)
    return TestClient(dashboard.app)


def update_recipe(client, **patch):
    data = client.get('/api/heal/recipes').json()
    response = client.put('/api/heal/recipes/ImagePullError', json={
        'revision': data.get('revision'), 'recipe': patch,
    })
    return publish_response(client, response)


def publish_response(client, response):
    if response.status_code != 200 or 'draft' not in response.json():
        return response
    draft = response.json()['draft']
    return client.post(f"/api/heal/recipe-drafts/{draft['id']}/decision", json={
        'decision': 'approve', 'reviewer': 'regression-test',
        'revision': draft['base_revision'], 'content_hash': draft['content_hash'],
    })


def test_recipe_edit_persists_and_changes_runtime_plan(client, monkeypatch):
    before = client.get('/api/heal/recipes').json()
    action = {'step': 'knowledge edit verification', 'command': 'kubectl get pod <pod> -n <namespace>', 'risk': 'low', 'rollback_command': ''}
    response = update_recipe(client, description='edited description', actions=[action])
    assert response.status_code == 200, response.text
    after = client.get('/api/heal/recipes').json()
    assert before['revision'] != after['revision']
    item = next(r for r in after['recipes'] if r['fault_type'] == 'ImagePullError')
    assert item['description'] == 'edited description'
    assert item['requires_approval'] and item['coverage'] == 'unverified'
    assert item['auto_recovery_supported'] is False
    assert len(after['recipes']) == len(before['recipes'])
    state = json.loads(dashboard.HEAL_RECIPES_STATE.read_text())
    assert len(state['history']) == 1 and state['history'][0]['before'] != item
    cfg = copy.deepcopy(dashboard._get_config())
    cfg.kubernetes.enabled = False
    monkeypatch.setattr(dashboard, '_get_config', lambda: cfg)
    plan = legacy.LegacyAgenticSREAdapter().build_heal_suggestions({'fault_type': 'ImagePullError', 'pod': 'sample', 'namespace': 'default'})
    assert any(s['step'] == action['step'] for s in plan['suggestions'])


def test_recipe_conflict_does_not_overwrite(client):
    data = client.get('/api/heal/recipes').json()
    assert update_recipe(client, description='first writer').status_code == 200
    response = client.put('/api/heal/recipes/ImagePullError', json={'revision': data.get('revision'), 'recipe': {'description': 'stale writer'}})
    assert response.status_code == 409
    assert next(r for r in client.get('/api/heal/recipes').json()['recipes'] if r['fault_type'] == 'ImagePullError')['description'] == 'first writer'


def test_edited_signal_is_used_for_matching(client):
    assert update_recipe(client, signals=['unique-registry-failure-marker']).status_code == 200
    assert dashboard._normalize_heal_fault_type('unique-registry-failure-marker') == 'ImagePullError'


@pytest.mark.parametrize('patch', [
    {'actions': 'not a list'}, {'actions': [None]}, {'signals': 'string'},
    {'risk': 'invalid'}, {'description': ''}, {'requires_approval': False},
    {'actions': [{'step': 'bad', 'command': 'kubectl get pods; echo unsafe', 'risk': 'low'}]},
    {'actions': [{'step': 'bad', 'command': 'kubectl get pods $(echo unsafe)', 'risk': 'low'}]},
    {'actions': [{'step': 'bad', 'command': 'kubectl get pods > /tmp/file', 'risk': 'low'}]},
])
def test_invalid_recipe_never_changes_state(client, patch):
    response = update_recipe(client, **patch)
    assert response.status_code == 422, response.text
    assert not dashboard.HEAL_RECIPES_STATE.exists()


def test_recipe_disk_failure_preserves_previous_version(client, monkeypatch):
    assert update_recipe(client, description='saved').status_code == 200
    saved = dashboard.HEAL_RECIPES_STATE.read_bytes()
    def fail(*args):
        raise OSError('disk failure')
    monkeypatch.setattr(dashboard.os, 'replace', fail)
    with pytest.raises(OSError):
        update_recipe(client, description='must not replace')
    assert dashboard.HEAL_RECIPES_STATE.read_bytes() == saved


def test_corrupted_recipe_library_fails_closed(client, monkeypatch, tmp_path):
    dashboard.HEAL_RECIPES_STATE.write_text('{broken')
    assert client.get('/api/heal/recipes').status_code == 503
    # Make the compatibility loader see the same corrupt shared state.
    (tmp_path / 'data').mkdir()
    monkeypatch.setattr(legacy, 'ROOT_DIR', tmp_path)
    (tmp_path / 'data/heal_recipe_library.json').write_text('{broken')
    with pytest.raises(Exception, match='503'):
        legacy._load_recipes()


@pytest.fixture
def store(tmp_path):
    return FaultContextStore(SimpleNamespace(memory=SimpleNamespace(db_path=str(tmp_path), backend='json')))


def test_rule_update_keeps_identity_and_learning_history(store, client, monkeypatch):
    rule_id = store.add_rule({'condition': 'old symptom', 'conclusion': 'old cause', 'confidence': 0.8, 'usage_count': 9, 'positive_votes': 7, 'source': 'supervised'})
    monkeypatch.setattr(dashboard, '_get_fault_store', lambda: store)
    before = client.get('/api/knowledge/rules').json()['rules'][0]
    response = client.put(f'/api/knowledge/rules/{rule_id}', json={'revision': before.get('revision'), 'condition': 'new symptom', 'conclusion': 'new cause', 'confidence': 0})
    assert response.status_code == 200, response.text
    updated = store.list_rules()[0]
    assert updated['rule_id'] == rule_id
    assert updated['usage_count'] == 9 and updated['positive_votes'] == 0
    assert updated['lifetime_positive_votes'] == 7 and updated['status'] == 'candidate'
    assert json.loads(updated['lifecycle_history'])[-1]['before']['positive_votes'] == 7
    assert updated['source'] == 'supervised' and updated['confidence'] == 0
    assert updated['created_at'] == before['created_at']
    assert json.loads(store._rules_file.read_text())[0]['condition'] == 'new symptom'
    assert client.put(f'/api/knowledge/rules/{rule_id}', json={'revision': before['revision'], 'condition': 'stale', 'conclusion': 'stale'}).status_code == 409


def test_rule_failed_persistence_keeps_original(store, monkeypatch):
    rule_id = store.add_rule({'condition': 'old', 'conclusion': 'old'})
    old = copy.deepcopy(store.list_rules()[0])
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr('memory.fault_context_store.os.replace', fail)
    with pytest.raises(OSError):
        store.update_rule(rule_id, {'condition': 'new'}, store.rule_revision(old))
    assert store.list_rules()[0] == old
    assert json.loads(store._rules_file.read_text())[0] == old


def test_chroma_update_refreshes_document_and_preserves_metadata(store):
    class Collection:
        def get(self, **kwargs):
            return {'ids': ['rule-one'], 'metadatas': [{'condition': 'old', 'conclusion': 'cause', 'usage_count': '5'}]}
        def update(self, **kwargs):
            self.updated = kwargs
    store._backend = 'chromadb'
    store._rules_col = Collection()
    old = store._rules_col.get()['metadatas'][0]
    store.update_rule('rule-one', {'condition': 'new', 'conclusion': 'updated'}, store.rule_revision(old))
    assert store._rules_col.updated['ids'] == ['rule-one']
    assert store._rules_col.updated['documents'] == ['new -> updated']
    assert store._rules_col.updated['metadatas'][0]['usage_count'] == '5'


def test_chroma_does_not_return_inactive_rule(store):
    class Collection:
        def count(self):
            return 2
        def query(self, **kwargs):
            return {'documents': [['old', 'new']], 'metadatas': [[{'status': 'inactive'}, {'status': 'active'}]], 'distances': [[0.1, 0.2]]}
    store._backend = 'chromadb'
    store._rules_col = Collection()
    assert [r['text'] for r in store.query_similar_rules('symptom')] == ['new']


def test_restore_history_creates_guarded_new_version(client):
    before = client.get('/api/heal/recipes').json()
    original = next(r for r in before['recipes'] if r['fault_type'] == 'ImagePullError')
    assert update_recipe(client, description='changed').status_code == 200
    history = client.get('/api/heal/recipes/ImagePullError/history')
    assert history.status_code == 200
    view = history.json()
    entry = view['history'][0]
    response = client.post('/api/heal/recipes/ImagePullError/restore', json={
        'revision': view['revision'], 'entry_id': entry['entry_id'], 'snapshot': 'before',
    })
    assert response.status_code == 200, response.text
    response = publish_response(client, response)
    assert response.status_code == 200, response.text
    recipe = response.json()['recipe']
    assert recipe['description'] == original['description']
    assert recipe['actions'] == original['actions']
    assert recipe['requires_approval'] and recipe['auto_recovery_supported'] is False
    assert recipe['coverage'] == 'unverified'
    assert response.json()['revision'] not in (before['revision'], view['revision'])
    audit = client.get('/api/heal/recipes/ImagePullError/history').json()['history']
    assert len(audit) == 2 and audit[0]['operation'] == 'restore'
    assert audit[0]['restored_from']['entry_id'] == entry['entry_id']
    assert audit[0]['before']['description'] == 'changed'


def test_restore_stale_revision_and_missing_snapshot(client):
    assert update_recipe(client, description='first').status_code == 200
    view = client.get('/api/heal/recipes/ImagePullError/history').json()
    assert update_recipe(client, description='second').status_code == 200
    body = {'revision': view['revision'], 'entry_id': view['history'][0]['entry_id'], 'snapshot': 'before'}
    assert client.post('/api/heal/recipes/ImagePullError/restore', json=body).status_code == 409
    body['revision'] = client.get('/api/heal/recipes').json()['revision']
    body['entry_id'] = 'nonexistent'
    assert client.post('/api/heal/recipes/ImagePullError/restore', json=body).status_code == 404
    body['snapshot'] = 'arbitrary'
    assert client.post('/api/heal/recipes/ImagePullError/restore', json=body).status_code == 422


def test_history_is_recipe_scoped_and_reads_older_records(client):
    assert client.get('/api/heal/recipes/ImagePullError/history').json()['history'] == []
    assert client.get('/api/heal/recipes/missing/history').status_code == 404
    assert update_recipe(client, description='new').status_code == 200
    state = json.loads(dashboard.HEAL_RECIPES_STATE.read_text())
    state['history'][0].pop('operation')
    dashboard.HEAL_RECIPES_STATE.write_text(json.dumps(state))
    assert len(client.get('/api/heal/recipes/ImagePullError/history').json()['history']) == 1
    assert client.get('/api/heal/recipes/OOMKilled/history').json()['history'] == []


def test_restore_failed_write_preserves_current_and_audit(client, monkeypatch):
    assert update_recipe(client, description='current').status_code == 200
    view = client.get('/api/heal/recipes/ImagePullError/history').json()
    saved = dashboard.HEAL_RECIPES_STATE.read_bytes()
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(dashboard.os, 'replace', fail)
    with pytest.raises(OSError):
        client.post('/api/heal/recipes/ImagePullError/restore', json={
            'revision': view['revision'], 'entry_id': view['history'][0]['entry_id'], 'snapshot': 'before'})
    assert dashboard.HEAL_RECIPES_STATE.read_bytes() == saved


def submit_draft(client, description='pending description', fault_type='ImagePullError'):
    revision = client.get('/api/heal/recipes').json()['revision']
    return client.put(f'/api/heal/recipes/{fault_type}', json={
        'revision': revision, 'recipe': {'description': description}})


def test_draft_does_not_publish_until_approved(client):
    before = client.get('/api/heal/recipes').json()
    response = submit_draft(client)
    assert response.status_code == 200
    assert response.json()['publication_status'] == 'pending'
    after = client.get('/api/heal/recipes').json()
    assert before['revision'] == after['revision']
    assert before['recipes'] == after['recipes']
    assert client.get('/api/heal/recipes/ImagePullError/history').json()['history'] == []
    draft = response.json()['draft']
    assert client.get('/api/heal/recipe-drafts').json()['drafts'][0]['id'] == draft['id']
    approved = publish_response(client, response)
    assert approved.status_code == 200, approved.text
    assert approved.json()['publication_status'] == 'published'
    assert approved.json()['recipe'] == draft['recipe']
    event = client.get('/api/heal/recipes/ImagePullError/history').json()['history'][0]
    assert event['approval']['content_hash'] == draft['content_hash']
    assert publish_response(client, response).status_code == 409


def test_rejected_draft_never_changes_active_library(client):
    before = client.get('/api/heal/recipes').json()['revision']
    response = submit_draft(client)
    draft = response.json()['draft']
    result = client.post(f"/api/heal/recipe-drafts/{draft['id']}/decision", json={
        'decision': 'reject', 'reviewer': 'operator', 'content_hash': draft['content_hash'], 'comment': 'insufficient evidence'})
    assert result.status_code == 200
    assert client.get('/api/heal/recipes').json()['revision'] == before
    assert publish_response(client, response).status_code == 409
    assert submit_draft(client).status_code == 200


def test_approval_rejects_wrong_hash_missing_reviewer_and_stale_base(client):
    response = submit_draft(client)
    draft = response.json()['draft']
    url = f"/api/heal/recipe-drafts/{draft['id']}/decision"
    body = {'decision': 'approve', 'reviewer': 'operator', 'revision': draft['base_revision'], 'content_hash': 'wrong'}
    assert client.post(url, json=body).status_code == 409
    body['content_hash'] = draft['content_hash']
    body['reviewer'] = ''
    assert client.post(url, json=body).status_code == 422
    # Another recipe publication invalidates the shared publication baseline.
    other = submit_draft(client, fault_type='OOMKilled')
    assert publish_response(client, other).status_code == 200
    assert publish_response(client, response).status_code == 409


def test_approval_write_failure_keeps_draft_pending(client, monkeypatch):
    response = submit_draft(client)
    saved = dashboard.HEAL_RECIPES_STATE.read_bytes()
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(dashboard.os, 'replace', fail)
    with pytest.raises(OSError):
        publish_response(client, response)
    assert dashboard.HEAL_RECIPES_STATE.read_bytes() == saved


def test_restore_also_requires_publication_approval(client):
    assert update_recipe(client, description='published change').status_code == 200
    view = client.get('/api/heal/recipes/ImagePullError/history').json()
    response = client.post('/api/heal/recipes/ImagePullError/restore', json={
        'revision': view['revision'], 'entry_id': view['history'][0]['entry_id'], 'snapshot': 'before'})
    assert response.status_code == 200 and response.json()['publication_status'] == 'pending'
    assert client.get('/api/heal/recipes').json()['revision'] == view['revision']
    assert len(client.get('/api/heal/recipes/ImagePullError/history').json()['history']) == 1
    assert publish_response(client, response).status_code == 200
    assert len(client.get('/api/heal/recipes/ImagePullError/history').json()['history']) == 2


def test_approval_uses_authenticated_identity_not_client_label(client, monkeypatch):
    response = submit_draft(client)
    monkeypatch.setattr(dashboard, '_auth_enabled', lambda: True)
    monkeypatch.setattr(dashboard, '_get_auth_session', lambda request: {'username': 'authenticated-reviewer'})
    assert publish_response(client, response).status_code == 200
    event = client.get('/api/heal/recipes/ImagePullError/history').json()['history'][0]
    assert event['approval']['reviewer'] == 'authenticated-reviewer'
    assert event['approval']['identity_source'] == 'authenticated_session'
