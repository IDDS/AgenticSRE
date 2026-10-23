"""Candidate isolation and explicit versioned rule review; remote execution only."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from memory.context_learner import ContextLearner
from memory.fault_context_store import FaultContextStore
from memory.memory_scope import MemoryScope


@pytest.fixture
def store(tmp_path):
    return FaultContextStore(SimpleNamespace(memory=SimpleNamespace(db_path=str(tmp_path), backend='json')))


def content(**patch):
    return {'condition': 'pool full', 'conclusion': 'pool exhausted', 'cluster_id': 'a', 'namespace': 'payments', **patch}


def scope():
    return MemoryScope(cluster_id='a', namespace='payments')


def learner(store, output):
    return ContextLearner(SimpleNamespace(json_chat=Mock(return_value=output)), store,
                          SimpleNamespace(memory=SimpleNamespace(auto_learn=True)))


@pytest.mark.parametrize('mode', ['auto', 'supervised'])
def test_learned_rules_are_candidates_and_cannot_be_recalled(store, mode):
    service = learner(store, {'correct': True, 'rules': [content()]})
    if mode == 'auto':
        result = service.learn_from_trace('pool limit observed', 'pool exhausted', .9,
            memory_scope=scope(), source_incident_id='incident-a')
    else:
        result = service.learn_supervised('pool exhausted', 'pool exhausted',
            memory_scope=scope(), source_incident_id='incident-a')
    row = store.list_rules()[0]
    assert row['status'] == 'candidate'
    assert row['source_incident_id'] == 'incident-a'
    assert result['rules_activated'] == 0 and result['rule_status'] == 'candidate'
    assert store.query_similar_rules('pool full', memory_scope=scope()) == []


def test_auto_fault_archive_is_not_a_learning_bypass(store):
    service = learner(store, {})
    service.store_fault_context('pool full', {'root_cause': 'pool exhausted'}, {}, [],
        memory_scope=scope(), source_incident_id='incident-a')
    assert store.list_faults()[0]['memory_status'] == 'candidate'
    assert store.query_similar_faults('pool full', memory_scope=scope()) == []


@pytest.mark.parametrize('bad', [None, {}, {'condition': '', 'conclusion': 'bad'},
    {'condition': 'pool', 'conclusion': 'bad', 'confidence': '0.9'},
    {'condition': 'pool', 'conclusion': 'bad', 'confidence': float('nan')}])
def test_auto_extraction_validates_entire_batch_before_first_write(store, bad):
    result = learner(store, {'rules': [content(), bad]}).learn_from_trace('trace', 'cause', .9)
    assert result['status'] == 'invalid_rules'
    assert store.list_rules() == []


def test_raw_feedback_is_not_a_candidate_bypass(tmp_path):
    from memory.expert_feedback import ExpertFeedbackStore
    from memory.feedback_contract import atomic_json
    feedback = ExpertFeedbackStore(str(tmp_path))
    atomic_json(feedback._feedback_file, [dict(incident_id='a', expert_diagnosis='unreviewed opinion',
        trace_matched=True, correct=True, learning_status='success', timestamp=1,
        cluster_id='a', namespace='payments')])
    assert len(feedback.get_recent_feedback()) == 1
    assert feedback.get_recent_feedback(for_context=True, memory_scope=scope()) == []


def decision(store, key, action, **patch):
    row = next(r for r in store.list_rules() if r['rule_id'] == key)
    return store.decide_rule(key, revision=store.rule_revision(row), decision=action,
        reviewer='operator', reason='checked original evidence; manual approval, benefit unverified',
        event_id='event-' + action, identity_source='operator_label', **patch)


def test_review_activation_edit_and_deactivation_keep_audit(store):
    key = store.add_rule(content(source='supervised'))
    assert store.query_similar_rules('pool full', memory_scope=scope()) == []
    activated = decision(store, key, 'approve')
    assert activated['status'] == 'active' and not activated['independent_benefit_verified']
    assert len(store.query_similar_rules('pool full', memory_scope=scope())) == 1
    store.record_rule_usage([key], positive=True, expected_revisions={key: activated['revision']})
    old = store.list_rules()[0]
    edited = store.update_rule(key, {'conclusion': 'new hypothesis'}, store.rule_revision(old))
    assert edited['status'] == 'candidate' and int(edited['positive_votes']) == 0
    assert int(edited['lifetime_positive_votes']) == 1
    assert store.query_similar_rules('pool full', memory_scope=scope()) == []
    decision(store, key, 'reject')
    assert store.list_rules()[0]['status'] == 'rejected'
    assert len(store.rule_history(key)['events']) >= 3


def test_closed_decision_replay_never_reactivates_after_later_deactivation(store):
    key = store.add_rule(content(source='auto_learn'))
    original = store.rule_revision(store.list_rules()[0])
    args = dict(revision=original, decision='approve', reviewer='operator', reason='checked fixture',
                event_id='same', identity_source='operator_label')
    first = store.decide_rule(key, **args)
    decision(store, key, 'deactivate')
    replay = store.decide_rule(key, **args)
    assert replay['replayed'] and replay['revision'] == first['revision']
    assert replay['current_status'] == 'inactive'
    assert store.list_rules()[0]['status'] == 'inactive'
    assert len(store.rule_history(key)['events']) == 2


def test_stale_review_and_event_content_conflicts_do_not_write(store):
    key = store.add_rule(content(source='supervised'))
    original = store.rule_revision(store.list_rules()[0])
    decision(store, key, 'approve')
    saved = store._rules_file.read_bytes()
    with pytest.raises(ValueError):
        store.decide_rule(key, revision=original, decision='reject', reviewer='operator',
            reason='stale review', event_id='other', identity_source='operator_label')
    assert store._rules_file.read_bytes() == saved


def test_unscoped_candidate_cannot_be_enabled(store):
    key = store.add_rule({'condition': 'pool', 'conclusion': 'cause', 'source': 'supervised'})
    with pytest.raises(ValueError, match='scope'):
        decision(store, key, 'approve')
    assert store.list_rules()[0]['status'] == 'candidate'


def test_edit_cannot_directly_activate_or_rewrite_provenance(store):
    key = store.add_rule(content(source='supervised'))
    original = store.list_rules()[0]
    for patch in ({'status': 'active'}, {'source_incident_id': 'forged'}, {'revision_seq': 900}):
        with pytest.raises(ValueError):
            store.update_rule(key, patch, store.rule_revision(original))
    assert store.list_rules()[0] == original


def test_failed_decision_write_preserves_state_and_audit(store, monkeypatch):
    key = store.add_rule(content(source='supervised'))
    before = store._rules_file.read_bytes()
    def fail(*args):
        raise OSError('controlled write failure')
    monkeypatch.setattr('memory.feedback_contract.os.replace', fail)
    with pytest.raises(OSError):
        decision(store, key, 'approve')
    assert store._rules_file.read_bytes() == before


def test_duplicate_learning_does_not_resurrect_rejected_candidate(store):
    value = content(source='supervised')
    key = store.add_rule(value)
    decision(store, key, 'reject')
    assert store.add_rule(value) == key
    assert store.list_rules()[0]['status'] == 'rejected'


def test_old_active_learned_rules_are_quarantined_without_rewriting_data(store):
    from memory.feedback_contract import atomic_json
    legacy = content(rule_id='legacy', source='auto_learn', status='active', timestamp=1, confidence=.9)
    atomic_json(store._rules_file, [legacy])
    before = store._rules_file.read_bytes()
    assert store.query_similar_rules('pool full', memory_scope=scope()) == []
    assert store.query_similar_rules('pool full') == []
    assert store.record_rule_usage(['legacy'])['updated'] == 0
    assert store._rules_file.read_bytes() == before
    decision(store, 'legacy', 'approve')
    assert len(store.query_similar_rules('pool full', memory_scope=scope())) == 1


def test_same_event_cannot_be_reused_for_another_decision(store):
    key = store.add_rule(content(source='supervised'))
    original = store.rule_revision(store.list_rules()[0])
    args = dict(revision=original, reviewer='operator', reason='checked fixture', event_id='immutable')
    store.decide_rule(key, decision='approve', **args)
    with pytest.raises(ValueError, match='event_content_conflict'):
        store.decide_rule(key, decision='reject', **args)
    assert store.list_rules()[0]['status'] == 'active'


def test_repeated_state_does_not_reuse_an_old_revision(store):
    key = store.add_rule(content(source='supervised'))
    original = store.rule_revision(store.list_rules()[0])
    decision(store, key, 'approve')
    decision(store, key, 'deactivate')
    decision(store, key, 'resubmit')
    current = store.rule_revision(store.list_rules()[0])
    assert current != original and store.list_rules()[0]['status'] == 'candidate'
    with pytest.raises(ValueError, match='revision_conflict'):
        store.decide_rule(key, revision=original, decision='approve', reviewer='operator',
            reason='stale previous candidate', event_id='old-candidate')


def test_concurrent_review_only_applies_one_transition(store):
    from concurrent.futures import ThreadPoolExecutor
    key = store.add_rule(content(source='supervised'))
    revision = store.rule_revision(store.list_rules()[0])
    def run(index):
        fresh = FaultContextStore(SimpleNamespace(memory=SimpleNamespace(db_path=str(store.db_path), backend='json')))
        try:
            return fresh.decide_rule(key, revision=revision, decision='approve', reviewer='operator',
                reason='concurrent checked fixture', event_id='event-' + str(index))
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(run, range(6)))
    assert sum(result is not None for result in results) == 1
    assert len(store.rule_history(key)['events']) == 1


@pytest.fixture
def api_client(store, monkeypatch):
    import importlib
    from fastapi.testclient import TestClient
    dashboard = importlib.import_module('web_app.app')
    monkeypatch.setattr(dashboard, '_get_fault_store', lambda: store)
    monkeypatch.setattr(dashboard, '_auth_enabled', lambda: False)
    return TestClient(dashboard.app)


def test_api_create_review_edit_and_history(api_client, store):
    created = api_client.post('/api/knowledge/rules', json=content())
    assert created.status_code == 200 and created.json()['rule_status'] == 'candidate'
    key = created.json()['rule_id']
    row = api_client.get('/api/knowledge/rules').json()['rules'][0]
    body = dict(revision=row['revision'], decision='approve', reviewer='operator',
                reason='verified fixture evidence', event_id='api-review')
    approved = api_client.post(f'/api/knowledge/rules/{key}/decision', json=body)
    assert approved.status_code == 200 and approved.json()['current_status'] == 'active'
    assert not approved.json()['independent_benefit_verified']
    assert api_client.post(f'/api/knowledge/rules/{key}/decision', json=body).json()['replayed']
    assert api_client.post(f'/api/knowledge/rules/{key}/decision', json={**body, 'reason': 'rewritten'}).status_code == 409
    assert api_client.delete(f'/api/knowledge/rules/{key}').status_code == 409
    updated = api_client.put(f'/api/knowledge/rules/{key}', json={**content(), 'conclusion': 'new cause',
                            'revision': approved.json()['revision']})
    assert updated.status_code == 200 and updated.json()['rule_status'] == 'candidate'
    assert api_client.post(f'/api/knowledge/rules/{key}/decision', json={**body, 'event_id': 'stale'}).status_code == 409
    history = api_client.get(f'/api/knowledge/rules/{key}/history').json()
    assert history['total'] == 2 and history['events'][0]['identity_source'] == 'operator_label'
    assert api_client.get(f'/api/knowledge/rules/{key}/history', params={'revision': row['revision']}).status_code == 409


@pytest.mark.parametrize('patch', [{'status': 'active'}, {'source': 'manual'}, {'confidence': True},
    {'confidence': '0.9'}, {'condition': ''}, {'activation_content_revision': 'forged'}])
def test_api_cannot_skip_candidate_review(api_client, patch):
    assert api_client.post('/api/knowledge/rules', json={**content(), **patch}).status_code == 422


def test_api_review_binds_authenticated_session_identity(api_client, store, monkeypatch):
    import importlib
    dashboard = importlib.import_module('web_app.app')
    key = store.add_rule(content(source='supervised'))
    monkeypatch.setattr(dashboard, '_get_auth_session', lambda _: {'username': 'real-reviewer'})
    monkeypatch.setattr(dashboard, '_auth_enabled', lambda: True)
    response = api_client.post(f'/api/knowledge/rules/{key}/decision', json=dict(
        revision=store.rule_revision(store.list_rules()[0]), decision='approve', reviewer='forged',
        reason='checked fixture', event_id='authenticated'))
    assert response.status_code == 200, response.text
    event = store.rule_history(key)['events'][0]
    assert event['reviewer'] == 'real-reviewer' and event['identity_source'] == 'authenticated_session'


def test_api_backend_failure_does_not_report_empty_success(api_client, store):
    store._backend = 'chromadb'
    def broken(**kwargs):
        raise RuntimeError('private backend detail')
    store._rules_col = SimpleNamespace(get=broken)
    response = api_client.get('/api/knowledge/rules')
    assert response.status_code == 503 and 'private' not in response.text


def test_actual_chroma_candidate_review_and_edit(store, tmp_path):
    import chromadb
    class Embedding:
        def __call__(self, input):
            return [[1.0, .5, .1] for _ in input]
        def embed_query(self, input):
            return self(input)
        @staticmethod
        def name():
            return 'controlled-candidate-embedding'
    client = chromadb.PersistentClient(path=str(tmp_path / 'chroma'))
    store._backend = 'chromadb'
    store._rules_col = client.get_or_create_collection('candidate-rules', embedding_function=Embedding())
    key = store.add_rule(content(source='supervised'))
    assert store.query_similar_rules('pool full', memory_scope=scope(), _raise_errors=True) == []
    enabled = decision(store, key, 'approve')
    assert store.query_similar_rules('pool full', memory_scope=scope(), _raise_errors=True)[0]['rule_id'] == key
    store.update_rule(key, {'condition': 'edited pool'}, enabled['revision'])
    assert store.query_similar_rules('pool full', memory_scope=scope(), _raise_errors=True) == []
    assert len(store.rule_history(key)['events']) == 2
    assert store.add_rule(content(source='supervised')) == key
    assert store.list_rules()[0]['condition'] == 'edited pool'


def test_zero_confidence_is_not_replaced_by_default_quality(store):
    key = store.add_rule(content(source='supervised', confidence=0))
    assert store.list_rules()[0]['quality_score'] == 0
    enabled = decision(store, key, 'approve')
    store.record_rule_usage([key], positive=True, expected_revisions={key: enabled['revision']})
    assert store.list_rules()[0]['quality_score'] == .4


def test_repeated_learning_reports_actual_insertions_and_existing_state(store, tmp_path, monkeypatch):
    from memory.feedback_contract import atomic_json
    monkeypatch.setenv('AGENTICSRE_FEEDBACK_DIR', str(tmp_path))
    atomic_json(tmp_path/'feedback_entries.json', [dict(feedback_id='feedback-a', learning_status='success')])
    service = learner(store, {'correct': True, 'rules': [content()]})
    args = dict(memory_scope=scope(), source_incident_id='incident-a', source_feedback_id='feedback-a')
    first = service.learn_supervised('agent assertion', 'expert assertion', **args)
    key = first['rule_ids'][0]
    assert first['rules_added'] == 1 and first['candidate_rule_ids'] == [key]
    decision(store, key, 'approve')
    second = service.learn_supervised('agent assertion', 'expert assertion', **args)
    assert second['rule_ids'] == [key] and second['rules_added'] == 0
    assert second['rule_status'] == 'no_new_rules' and second['candidate_rule_ids'] == []
    assert second['rule_results'][0]['stored_status'] == 'active' and second['rules_activated'] == 0
    decision(store, key, 'deactivate')
    third = service.learn_supervised('agent assertion', 'expert assertion', **args)
    assert third['rule_results'][0]['stored_status'] == 'inactive' and third['rules_added'] == 0


def test_feedback_receipt_links_unactivated_candidates_to_original_event(store, tmp_path):
    from memory.expert_feedback import ExpertFeedbackStore
    service = learner(store, {'correct': True, 'rules': [content()]})
    feedback = ExpertFeedbackStore(str(tmp_path / 'feedback'))
    trace = SimpleNamespace(get_trace_by_id=lambda _: SimpleNamespace(result={
        'root_cause': 'pool exhausted', 'reasoning_chain': 'synthetic pool limit',
        'cluster_id': 'a', 'namespace': 'payments'}))
    result = feedback.submit_feedback('incident-a', 'synthetic expert assertion', context_learner=service,
        trace_store=trace, fault_store=store, event_id='fixture')
    assert result['rule_status'] == 'candidate' and result['rules_generated'] == 1 and result['rules_activated'] == 0
    row = store.list_rules()[0]
    assert result['candidate_rule_ids'] == [row['rule_id']]
    assert row['source_feedback_id'] == result['feedback_id'] and row['source_incident_id'] == 'incident-a'
    assert feedback.get_recent_feedback(for_context=True, memory_scope=scope()) == []
    assert store.query_similar_rules('pool full', memory_scope=scope()) == []


def test_api_duplicate_create_reports_preserved_active_state(api_client, store):
    first = api_client.post('/api/knowledge/rules', json=content()).json()
    assert first['created'] and first['rule_status'] == 'candidate'
    decision(store, first['rule_id'], 'approve')
    repeated = api_client.post('/api/knowledge/rules', json=content()).json()
    assert not repeated['created'] and repeated['rule_status'] == 'active'
    assert repeated['rule_id'] == first['rule_id'] and repeated['rules_activated'] == 0


def test_full_audit_retention_still_allows_safe_deactivation(store):
    from memory.rule_lifecycle import MAX_EVENTS
    from memory.feedback_contract import atomic_json
    key = store.add_rule(content())
    rows = store.list_rules()
    rows[0]['lifecycle_history'] = json.dumps([{'operation': 'legacy-history'}] * MAX_EVENTS)
    atomic_json(store._rules_file, rows)
    result = decision(store, key, 'deactivate')
    assert result['current_status'] == 'inactive'
    history = store.rule_history(key, offset=MAX_EVENTS, limit=1)
    assert history['total'] == MAX_EVENTS + 1 and history['events'][0]['operation'] == 'deactivate'
    with pytest.raises(ValueError, match='retention_limit'):
        decision(store, key, 'resubmit')
