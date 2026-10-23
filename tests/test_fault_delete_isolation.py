"""Delete only the selected fault; execute these API regressions remotely."""
import json
from types import SimpleNamespace

import pytest

from memory.fault_context_store import FaultContextStore


def json_store(directory):
    return FaultContextStore(SimpleNamespace(memory=SimpleNamespace(
        db_path=str(directory), backend='json')))


def api_client(monkeypatch, cached):
    import importlib
    from fastapi.testclient import TestClient

    web = importlib.import_module('web_app.app')
    monkeypatch.setattr(web, '_get_fault_store', lambda: cached)
    monkeypatch.setattr(web, '_auth_enabled', lambda: False)
    return TestClient(web.app)


@pytest.mark.parametrize('operation', ['deactivate', 'edit'])
def test_web_fault_delete_preserves_another_store_rule_changes(tmp_path, monkeypatch, operation):
    seed = json_store(tmp_path)
    key = seed.add_rule(dict(condition='pool full', conclusion='pool exhausted',
        cluster_id='cluster-a', namespace='payments', source='manual'))
    removed = seed.add_fault(dict(description='old incident', root_cause='old cause'))

    # The Web store predates another writer's rule decision and insertion.
    cached, editor = json_store(tmp_path), json_store(tmp_path)
    original = next(r for r in editor.list_rules() if r['rule_id'] == key)
    revision = editor.rule_revision(original)
    if operation == 'deactivate':
        editor.decide_rule(key, revision=revision, decision='deactivate',
            reviewer='regression operator', reason='controlled evidence correction',
            event_id='deactivate-original')
    else:
        editor.update_rule(key, dict(conclusion='newly observed cause'), revision)
    added = editor.add_rule(dict(condition='disk full', conclusion='disk pressure',
        cluster_id='cluster-a', namespace='payments', source='manual'))
    expected = {r['rule_id']: r for r in editor.list_rules()}
    expected_history = editor.rule_history(key)

    response = api_client(monkeypatch, cached).delete('/api/knowledge/faults/' + removed)
    assert response.status_code == 200, response.text
    fresh = json_store(tmp_path)
    actual = {r['rule_id']: r for r in fresh.list_rules()}
    assert actual == expected, 'Deleting an incident overwrote newer rule content, state or audit'
    assert added in actual
    assert actual[key]['status'] == ('inactive' if operation == 'deactivate' else 'candidate')
    assert fresh.rule_history(key) == expected_history
    assert removed not in {r['fault_id'] for r in fresh.list_faults()}


def test_web_fault_delete_retains_fault_added_after_cached_store_loaded(tmp_path, monkeypatch):
    seed = json_store(tmp_path)
    removed = seed.add_fault(dict(description='old incident', root_cause='old cause'))
    cached, writer = json_store(tmp_path), json_store(tmp_path)
    retained = writer.add_fault(dict(description='new incident', root_cause='new cause'))
    before = json.loads(writer._faults_file.read_text())
    expected = [row for row in before if row['fault_id'] != removed]

    response = api_client(monkeypatch, cached).delete('/api/knowledge/faults/' + removed)
    assert response.status_code == 200, response.text
    actual = json_store(tmp_path).list_faults()
    assert actual == expected, 'Deleting from a stale store lost a later incident'
    assert {row['fault_id'] for row in actual} == {retained}


def test_web_fault_delete_reports_chroma_failure_without_private_details(tmp_path, monkeypatch):
    store = json_store(tmp_path)

    def unavailable(**_):
        raise RuntimeError('private backend detail')

    store._backend = 'chromadb'
    store._faults_col = SimpleNamespace(delete=unavailable)
    response = api_client(monkeypatch, store).delete('/api/knowledge/faults/fault-missing')
    assert response.status_code == 503
    assert response.json()['detail'] == 'fault_storage_unavailable'
    assert 'private' not in response.text


def test_web_fault_delete_failed_replace_preserves_faults_and_rules(tmp_path, monkeypatch):
    store = json_store(tmp_path)
    store.add_rule(dict(condition='pool full', conclusion='pool exhausted'))
    removed = store.add_fault(dict(description='old incident', root_cause='old cause'))
    before_rules, before_faults = store._rules_file.read_bytes(), store._faults_file.read_bytes()

    def fail_replace(*_):
        raise OSError('controlled replacement failure')

    monkeypatch.setattr('memory.feedback_contract.os.replace', fail_replace)
    response = api_client(monkeypatch, store).delete('/api/knowledge/faults/' + removed)
    assert response.status_code == 503
    assert store._rules_file.read_bytes() == before_rules
    assert store._faults_file.read_bytes() == before_faults
