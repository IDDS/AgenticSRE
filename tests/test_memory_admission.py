"""Memory applicability regressions; execute only on the remote test host."""
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from memory.fault_context_store import FaultContextStore


def scope(**kwargs):
    from memory.memory_scope import MemoryScope
    return MemoryScope(cluster_id='cluster-a', namespace='payments', **kwargs)


def json_store(tmp_path):
    return FaultContextStore(SimpleNamespace(memory=SimpleNamespace(
        backend='json', db_path=str(tmp_path), max_similar_results=3)))


def rule(**patch):
    return dict(condition='pool full', conclusion='pool exhausted',
                cluster_id='cluster-a', namespace='payments', **patch)


def test_scope_filter_precedes_top_k_and_reload_observes_external_edit(tmp_path):
    store = json_store(tmp_path)
    for i in range(12):
        store.add_rule(dict(condition='pool full exhausted', conclusion=str(i),
                            cluster_id='cluster-b', namespace='payments'))
    good = store.add_rule(rule())
    assert [r['rule_id'] for r in store.query_similar_rules('pool full exhausted', n=1, memory_scope=scope())] == [good]
    editor = json_store(tmp_path)
    original = next(r for r in editor.list_rules() if r['rule_id'] == good)
    editor.update_rule(good, {'status': 'inactive'}, editor.rule_revision(original))
    assert store.query_similar_rules('pool full', memory_scope=scope()) == []


@pytest.mark.parametrize('patch', [
    {'cluster_id': 'cluster-b'}, {'namespace': 'other'}, {'cluster_id': ''},
    {'namespace': 'general'}, {'status': 'candidate'}, {'status': 'revoked'},
    {'valid_to': 99}, {'valid_from': 101}, {'recorded_at': 101},
    {'valid_to': 'not-a-time'}, {'valid_to': True}, {'valid_to': float('inf')},
    {'review_expires_at': 100}, {'workload_version': 'v1'}, {'entity_uid': 'old'},
    {'updated_at': 101},
])
def test_inapplicable_memory_never_enters_context(patch):
    from memory.memory_scope import admission_reason
    record = {**rule(), 'timestamp': 90, **patch}
    assert admission_reason(record, scope(as_of=100, workload_version='v2', entity_uid='new'), 'rules') != 'admitted'


def test_unscoped_is_not_global_and_global_requires_review():
    from memory.memory_scope import admission_reason, global_scope_revision
    record = {'condition': 'pool', 'timestamp': 90, 'scope_kind': 'global'}
    assert admission_reason(record, scope(as_of=100), 'rules') != 'admitted'
    record.update(scope_reviewed_by='sre-review', scope_reviewed_at=95)
    record['scope_review_revision'] = global_scope_revision(record)
    assert admission_reason(record, scope(as_of=100), 'rules') == 'admitted'
    assert admission_reason(record, scope(as_of=100), 'faults') != 'admitted'
    record['scope_reviewed_at'] = 101
    assert admission_reason(record, scope(as_of=100), 'rules') != 'admitted'
    record['scope_reviewed_at'] = 95
    record['condition'] = 'changed content'
    assert admission_reason(record, scope(as_of=100), 'rules') != 'admitted'


def test_missing_request_scope_cannot_fall_back_to_global_read(tmp_path):
    from memory.memory_scope import MemoryScope
    from memory.history_retrieval import retrieve_history
    factory = Mock(return_value=json_store(tmp_path))
    result = retrieve_history(factory, 'pool', memory_scope=MemoryScope(namespace='payments'))
    assert result.context['retrieval']['status'] == 'blocked_scope'
    factory.assert_not_called()


def test_legacy_provider_cannot_silently_ignore_scope():
    from memory.history_retrieval import retrieve_history
    legacy = SimpleNamespace(get_historical_context=Mock(return_value={'rules': [rule()], 'faults': []}))
    result = retrieve_history(lambda: legacy, 'pool', memory_scope=scope())
    assert result.context['rules'] == []
    assert result.context['retrieval']['status'] == 'unavailable'
    legacy.get_historical_context.assert_not_called()


def test_supervised_memory_uses_observed_scope_not_model_scope(tmp_path):
    from memory.context_learner import ContextLearner
    store = json_store(tmp_path)
    client = SimpleNamespace(json_chat=lambda _: {'correct': True, 'rules': [
        {'condition': 'pool full', 'conclusion': 'pool exhausted', 'namespace': 'general', 'cluster_id': 'invented'}]})
    learner = ContextLearner(client, store, SimpleNamespace(memory=SimpleNamespace(auto_learn=True)))
    result = learner.learn_supervised('diagnosis', 'assertion', memory_scope=scope(), source_incident_id='incident-a')
    saved = store.list_rules()[0]
    assert result['rules_added'] == 1
    assert saved['cluster_id'] == 'cluster-a' and saved['namespace'] == 'payments'
    assert saved['source_incident_id'] == 'incident-a'
    assert saved['scope_kind'] == 'local'


def test_recent_feedback_filters_before_limit_and_rejects_future(tmp_path):
    from memory.expert_feedback import ExpertFeedbackStore
    from memory.feedback_contract import atomic_json
    store = ExpertFeedbackStore(str(tmp_path))
    records = [dict(incident_id='good', expert_diagnosis='pool', learning_status='success',
                    trace_matched=True, correct=True, timestamp=90, cluster_id='cluster-a', namespace='payments')]
    records += [{**records[0], 'incident_id': str(i), 'cluster_id': 'cluster-b'} for i in range(10)]
    records.append({**records[0], 'incident_id': 'future', 'timestamp': 101})
    atomic_json(store._feedback_file, records)
    rows = store.get_recent_feedback(n=1, memory_scope=scope(as_of=100))
    assert [r['incident_id'] for r in rows] == ['good']
    assert store.get_recent_feedback(n=1, for_context=True, memory_scope=scope(as_of=100)) == []


def test_context_builder_rechecks_supplied_snapshot_scope():
    from memory.context_builder import ContextBuilder
    historical = {'rules': [{**rule(), 'timestamp': 90}, {**rule(), 'cluster_id': 'cluster-b', 'timestamp': 90}],
                  'faults': [], 'retrieval': {'status': 'complete'}}
    context = ContextBuilder().build_context('pool', historical_context=historical, memory_scope=scope(as_of=100))
    assert len(context.historical_rules) == 1
    assert context.history_retrieval['admission']['rules']['rejected'] == 1


def test_rule_revision_covers_scope_and_validity():
    original = rule()
    revision = FaultContextStore.rule_revision(original)
    for key, value in [('cluster_id', 'b'), ('valid_to', 120), ('scope_kind', 'global'),
                       ('scope_reviewed_by', 'reviewer'), ('entity_uid', 'uid')]:
        assert FaultContextStore.rule_revision({**original, key: value}) != revision


def test_actual_chroma_admits_before_vector_query(tmp_path):
    import chromadb
    calls = []
    class Embedding:
        def __call__(self, input):
            calls.extend(input)
            return [[1.0, float('other' in value), 0.5] for value in input]
        def embed_query(self, input):
            return self(input)
        @staticmethod
        def name():
            return 'controlled-scope-embedding'
    store = json_store(tmp_path / 'json')
    client = chromadb.PersistentClient(path=str(tmp_path / 'chroma'))
    store._backend = 'chromadb'
    store._rules_col = client.get_or_create_collection('scope-rules', embedding_function=Embedding())
    store._faults_col = client.get_or_create_collection('scope-faults', embedding_function=Embedding())
    for i in range(12):
        store.add_rule(dict(condition='pool full', conclusion=str(i), cluster_id='other', namespace='payments'))
    good = store.add_rule(rule())
    store.add_rule({**rule(), 'condition': 'pool future', 'valid_from': time.time() + 1000})
    assert [r['rule_id'] for r in store.query_similar_rules('pool full', n=1, memory_scope=scope(), _raise_errors=True)] == [good]
    calls.clear()
    missing_scope = scope(as_of=1)
    assert store.query_similar_rules('pool full', memory_scope=missing_scope) == []
    assert calls == []
    store.add_fault({'description': 'pool full', 'root_cause': 'other',
                     'cluster_id': 'other', 'namespace': 'payments'})
    good_fault = store.add_fault({'description': 'pool full', 'root_cause': 'exhausted',
                                  'cluster_id': 'cluster-a', 'namespace': 'payments'})
    store.add_fault({'description': 'pool full', 'root_cause': 'expired',
                     'cluster_id': 'cluster-a', 'namespace': 'payments', 'valid_to': 1})
    assert [r['fault_id'] for r in store.query_similar_faults('pool full', memory_scope=scope(),
                                                            _raise_errors=True)] == [good_fault]


def test_skill_provider_cannot_reuse_another_namespace_or_unscoped_rule(tmp_path, monkeypatch):
    import importlib
    from orchestrator.skill_agent import SkillAgentRequest
    dashboard = importlib.import_module('web_app.app')
    store = json_store(tmp_path)
    good = store.add_rule(rule())
    bad = store.add_rule({**rule(), 'namespace': 'other', 'condition': 'pool full unrelated'})
    unscoped = store.add_rule({'condition': 'pool full', 'conclusion': 'unscoped'})
    monkeypatch.setattr(dashboard, '_get_fault_store', lambda: store)
    monkeypatch.setattr(dashboard._get_config().memory, 'enabled', True)
    monkeypatch.setattr(dashboard, '_get_feedback_store', lambda: SimpleNamespace(diagnosis_stats=lambda **_: {}))
    monkeypatch.setitem(dashboard._state, 'rca_runs', {})
    result = dashboard._agent_skill_context_provider(SkillAgentRequest(query='pool full', namespace='payments',
        skill_workflow={'payload': {'cluster_id': 'cluster-a', 'namespace': 'payments'}}))
    assert [r['rule_id'] for r in result['rules']] == [good], (bad, unscoped)


def test_scoped_scan_limit_does_not_start_vector_query(tmp_path):
    store = json_store(tmp_path)
    store._backend = 'chromadb'
    collection = SimpleNamespace(count=lambda: 10, query=Mock(),
        get=lambda **_: {'ids': ['x'] * 3, 'metadatas': [{}] * 3})
    store.MAX_SCOPE_CANDIDATES = 2
    store._rules_col = collection
    with pytest.raises(ValueError, match='memory_scope_candidate_limit'):
        store.query_similar_rules('pool full', memory_scope=scope(), _raise_errors=True)
    collection.query.assert_not_called()


def test_scoped_feedback_statistics_filter_before_sample_limit(tmp_path):
    from memory.expert_feedback import ExpertFeedbackStore
    from memory.feedback_contract import atomic_json
    store = ExpertFeedbackStore(str(tmp_path))
    base = dict(incident_id='a', learning_status='success', trace_matched=True, correct=True,
                fault_type='pool', timestamp=90, cluster_id='cluster-a', namespace='payments')
    atomic_json(store._feedback_file, [base, {**base, 'namespace': 'other', 'correct': False},
                                      {**base, 'timestamp': 101, 'correct': False}])
    result = store.diagnosis_stats(fault_type='pool', limit=1, memory_scope=scope(as_of=100))
    assert result['diagnosis_sample_count'] == result['diagnosis_correct_count'] == 1


def test_fault_archive_preserves_source_identity(tmp_path):
    from memory.context_learner import ContextLearner
    store = json_store(tmp_path)
    learner = ContextLearner(None, store, SimpleNamespace(memory=SimpleNamespace(auto_learn=True)))
    key = learner.store_fault_context('pool full', {'root_cause': 'exhausted'}, {}, [],
        memory_scope=scope(entity_uid='current', workload_version='v2'), source_incident_id='incident-a')
    record = next(r for r in store.list_faults() if r['fault_id'] == key)
    assert record['namespace'] == 'payments' and record['cluster_id'] == 'cluster-a'
    assert record['entity_uid'] == 'current' and record['workload_version'] == 'v2'
    assert record['source_incident_id'] == 'incident-a'


def test_global_review_is_not_reused_after_content_edit(tmp_path):
    from memory.memory_scope import global_scope_revision
    store = json_store(tmp_path)
    content = {'condition': 'pool full', 'conclusion': 'pool exhausted', 'scope_kind': 'global'}
    content.update(scope_reviewed_by='controlled-reviewer', scope_reviewed_at=90,
                   scope_review_revision=global_scope_revision(content))
    key = store.add_rule(content)
    assert len(store.query_similar_rules('pool full', memory_scope=scope())) == 1
    original = store.list_rules()[0]
    store.update_rule(key, {'conclusion': 'unreviewed change'}, store.rule_revision(original))
    assert store.query_similar_rules('pool full', memory_scope=scope()) == []


@pytest.mark.parametrize('payload_patch,namespace', [({}, 'other'), ({'target_cluster_id': 'cluster-b'}, 'payments')])
def test_conflicting_request_identities_block_all_memory(monkeypatch, payload_patch, namespace):
    import importlib
    from orchestrator.skill_agent import SkillAgentRequest
    dashboard = importlib.import_module('web_app.app')
    monkeypatch.setattr(dashboard._get_config().memory, 'enabled', True)
    factory = Mock(side_effect=AssertionError('conflicted identity must never query storage'))
    monkeypatch.setattr(dashboard, '_get_fault_store', factory)
    monkeypatch.setattr(dashboard, '_get_feedback_store', factory)
    monkeypatch.setitem(dashboard._state, 'rca_runs', {})
    context = dashboard._agent_skill_context_provider(SkillAgentRequest(query='pool full', namespace=namespace,
        skill_workflow={'payload': {'cluster_id': 'cluster-a', 'namespace': 'payments', **payload_patch}}))
    assert context['history_retrieval']['status'] == 'blocked_scope'
    assert context['history_retrieval']['error_code'] == 'memory_scope_conflict'
    assert context['rules'] == context['faults'] == context['recent_runs'] == []
    factory.assert_not_called()
