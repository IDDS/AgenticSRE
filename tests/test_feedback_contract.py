"""Feedback correctness, admission and replay regressions (remote execution only)."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from memory.context_learner import ContextLearner
from memory.expert_feedback import ExpertFeedbackStore
from memory.human_review import HumanReviewStore


def learner(payload):
    store = SimpleNamespace(add_rule=Mock(return_value='new-rule'))
    client = SimpleNamespace(json_chat=Mock(return_value=payload))
    return ContextLearner(client, store, SimpleNamespace(memory=SimpleNamespace(auto_learn=True))), store


def traces(result=None):
    value = result or {'root_cause': 'pool exhausted', 'reasoning_chain': 'observed pool limit',
                       'fault_type': 'ConnectionPool', 'used_rule_ids': ['rule-1']}
    return SimpleNamespace(get_trace_by_id=lambda _: SimpleNamespace(result=value))


def supervised():
    return SimpleNamespace(learn_supervised=Mock(return_value={
        'status': 'success', 'correct': True, 'rules_added': 0}))


@pytest.mark.parametrize('label', ['false', 'true', 0, 1, None, [], {}])
def test_invalid_correctness_cannot_write_rules(label):
    service, store = learner({'correct': label, 'rules': [
        {'condition': 'pool full', 'conclusion': 'pool capacity exhausted'}]})
    result = service.learn_supervised('observed diagnosis', 'expert assertion')
    assert result['status'] != 'success'
    assert result.get('correct') is None
    store.add_rule.assert_not_called()


def test_missing_correctness_is_unknown_before_any_write():
    service, store = learner({'rules': [{'condition': 'full', 'conclusion': 'exhausted'}]})
    result = service.learn_supervised('diagnosis', 'expert assertion')
    assert result.get('correct') is None
    store.add_rule.assert_not_called()


def test_pending_review_never_learns_from_expert_text(tmp_path):
    service = supervised()
    queue = HumanReviewStore(str(tmp_path))
    review = queue.create_review('incident-1', 'missing evidence', {'root_cause': 'unconfirmed'})
    result = queue.decide_review(review['review_id'], 'needs_more_evidence',
        expert_diagnosis='might be connection pool', context_learner=service)
    assert result['status'] == 'pending'
    service.learn_supervised.assert_not_called()


def test_unmatched_feedback_is_saved_without_model_or_rule_updates(tmp_path):
    service = supervised()
    store = ExpertFeedbackStore(str(tmp_path))
    result = store.submit_feedback('missing', 'expert assertion', context_learner=service,
        trace_store=SimpleNamespace(get_trace_by_id=lambda _: None))
    assert result['learning_status'] == 'blocked_no_trace'
    service.learn_supervised.assert_not_called()
    assert store.get_recent_feedback()[0]['incident_id'] == 'missing'


def test_feedback_retry_does_not_repeat_learning(tmp_path):
    service = supervised()
    first = ExpertFeedbackStore(str(tmp_path)).submit_feedback('incident-1', 'pool exhausted',
        context_learner=service, trace_store=traces())
    second = ExpertFeedbackStore(str(tmp_path)).submit_feedback('incident-1', 'pool exhausted',
        context_learner=service, trace_store=traces())
    assert first['feedback_id'] == second['feedback_id']
    assert service.learn_supervised.call_count == 1
    assert len(ExpertFeedbackStore(str(tmp_path)).get_recent_feedback()) == 1


def test_incident_verdict_does_not_vote_for_all_recalled_rules(tmp_path):
    votes = SimpleNamespace(record_rule_usage=Mock(return_value={'updated': 1}))
    result = ExpertFeedbackStore(str(tmp_path)).submit_feedback('incident-1', 'pool exhausted',
        context_learner=supervised(), trace_store=traces(), fault_store=votes)
    assert result['rules_voted'] == 0
    votes.record_rule_usage.assert_not_called()


def test_closed_review_replay_does_not_repeat_learning(tmp_path):
    service = supervised()
    queue = HumanReviewStore(str(tmp_path))
    review = queue.create_review('incident-1', 'review', traces().get_trace_by_id('incident-1').result)
    for _ in range(2):
        queue.decide_review(review['review_id'], 'approve', expert_diagnosis='pool exhausted',
            context_learner=service, feedback_store=ExpertFeedbackStore(str(tmp_path/'feedback')),
            trace_store=traces())
    assert service.learn_supervised.call_count == 1


@pytest.mark.parametrize('correct', [True, False])
def test_valid_boolean_is_preserved_with_model_label_provenance(correct):
    service, store = learner({'correct': correct, 'rules': []})
    result = service.learn_supervised('diagnosis', 'expert assertion')
    assert result['status'] == 'success' and result['correct'] is correct
    assert result['correct_source'] == 'model_comparison_to_expert_text'
    store.add_rule.assert_not_called()


def test_invalid_verdict_from_custom_learner_never_votes_or_counts(tmp_path):
    service = supervised()
    service.learn_supervised.return_value['correct'] = 'false'
    store = ExpertFeedbackStore(str(tmp_path))
    result = store.submit_feedback('incident-1', 'assertion', context_learner=service, trace_store=traces())
    assert result['learning_status'] == 'invalid_correctness' and result['correct'] is None
    assert store.diagnosis_stats(fault_type='ConnectionPool')['diagnosis_sample_count'] == 0


def test_same_event_cannot_change_its_content(tmp_path):
    from memory.feedback_contract import FeedbackConflict
    service = supervised()
    store = ExpertFeedbackStore(str(tmp_path))
    store.submit_feedback('i', 'first', event_id='retry-key', context_learner=service, trace_store=traces())
    with pytest.raises(FeedbackConflict):
        store.submit_feedback('i', 'different', event_id='retry-key', context_learner=service, trace_store=traces())
    assert service.learn_supervised.call_count == 1


def test_crash_after_effects_leaves_claim_and_never_reexecutes(tmp_path, monkeypatch):
    import os
    import memory.feedback_contract as contract
    actual = os.replace
    writes = []
    def fail_second(source, target):
        writes.append(str(target))
        if len(writes) == 2:
            raise OSError('controlled disk failure')
        return actual(source, target)
    monkeypatch.setattr(contract.os, 'replace', fail_second)
    service = supervised()
    store = ExpertFeedbackStore(str(tmp_path))
    with pytest.raises(OSError):
        store.submit_feedback('i', 'assertion', context_learner=service, trace_store=traces())
    result = ExpertFeedbackStore(str(tmp_path)).submit_feedback('i', 'assertion',
        context_learner=service, trace_store=traces())
    assert result['learning_status'] == 'outcome_unknown'
    assert service.learn_supervised.call_count == 1


def test_failed_claim_has_zero_effects(tmp_path, monkeypatch):
    import memory.feedback_contract as contract
    def fail(*_): raise OSError('controlled claim failure')
    monkeypatch.setattr(contract.os, 'replace', fail)
    service = supervised()
    with pytest.raises(OSError):
        ExpertFeedbackStore(str(tmp_path)).submit_feedback('i', 'assertion',
            context_learner=service, trace_store=traces())
    service.learn_supervised.assert_not_called()
    assert not (tmp_path/'feedback_entries.json').exists()


def test_corrupt_feedback_is_not_replaced_with_empty_state(tmp_path):
    from memory.feedback_contract import FeedbackStorageError
    path = tmp_path/'feedback_entries.json'
    path.write_text('{broken')
    with pytest.raises(FeedbackStorageError): ExpertFeedbackStore(str(tmp_path))
    assert path.read_text() == '{broken'


def test_concurrent_stores_consume_one_event(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    service = supervised()
    def submit(_):
        return ExpertFeedbackStore(str(tmp_path)).submit_feedback('i', 'assertion',
            context_learner=service, trace_store=traces())
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(submit, range(12)))
    assert len({r['feedback_id'] for r in results}) == 1
    assert service.learn_supervised.call_count == 1
    assert sum(r['replayed'] for r in results) == 11


def test_feedback_reads_do_not_wait_for_model_completion(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time
    entered, release = threading.Event(), threading.Event()
    service = supervised()
    def slow(**_):
        entered.set()
        assert release.wait(4)
        return {'status': 'success', 'correct': True, 'rules_added': 0}
    service.learn_supervised.side_effect = slow
    timer = threading.Timer(2, release.set)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(ExpertFeedbackStore(str(tmp_path)).submit_feedback, 'i', 'assertion',
            context_learner=service, trace_store=traces())
        assert entered.wait(2)
        timer.start()
        try:
            started = time.monotonic()
            records = ExpertFeedbackStore(str(tmp_path)).get_recent_feedback()
            assert time.monotonic() - started < 1
            assert records[0]['learning_status'] == 'processing'
        finally:
            release.set()
            timer.cancel()
        assert future.result()['learning_status'] == 'success'


@pytest.mark.parametrize('bad_rule', [None, {}, {'condition':'x', 'conclusion':''},
    {'condition':'x', 'conclusion':'y', 'confidence':'0.9'},
    {'condition':'x', 'conclusion':'y', 'confidence':float('nan')},
    {'condition':'x', 'conclusion':'y', 'rule_type':'invalid'}])
def test_all_extracted_rules_are_validated_before_first_write(bad_rule):
    service, store = learner({'correct': True, 'rules': [
        {'condition':'valid', 'conclusion':'valid cause'}, bad_rule]})
    assert service.learn_supervised('diagnosis', 'expert')['status'] == 'invalid_rules'
    store.add_rule.assert_not_called()


def test_repeated_incident_opinions_are_not_independent_samples(tmp_path):
    service = supervised()
    store = ExpertFeedbackStore(str(tmp_path))
    store.submit_feedback('i', 'first answer', context_learner=service, trace_store=traces())
    service.learn_supervised.return_value['correct'] = False
    store.submit_feedback('i', 'corrected answer', context_learner=service, trace_store=traces())
    stats = ExpertFeedbackStore(str(tmp_path)).diagnosis_stats(fault_type='ConnectionPool')
    assert stats['diagnosis_sample_count'] == 1 and stats['diagnosis_correct_count'] == 0
    assert stats['sample_unit'] == 'unique_incident' and stats['independent_truth_verified'] is False


def rule_store(path):
    from memory.fault_context_store import FaultContextStore
    return FaultContextStore(SimpleNamespace(memory=SimpleNamespace(db_path=str(path), backend='json')))


def test_explicit_rule_usefulness_is_independent_of_incident_verdict(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENTICSRE_FEEDBACK_DIR', str(tmp_path/'feedback'))
    from memory.fault_context_store import FaultContextStore
    store = rule_store(tmp_path/'memory')
    key = store.add_rule({'condition': 'pool full', 'conclusion': 'pool pressure'})
    unrelated = store.add_rule({'condition': 'disk full', 'conclusion': 'disk pressure'})
    row = next(r for r in store.list_rules() if r['rule_id'] == key)
    ref = {'rule_id': key, 'revision': FaultContextStore.rule_revision(row)}
    trace = traces({'root_cause': 'different final cause', 'fault_type': 'ConnectionPool',
                    'used_rule_ids': [key, unrelated], 'recalled_rule_refs': [ref]})
    service = supervised()
    service.learn_supervised.return_value['correct'] = False
    result = ExpertFeedbackStore(str(tmp_path/'feedback')).submit_feedback('i', 'corrected cause',
        context_learner=service, trace_store=trace, fault_store=store,
        rule_feedback=[dict(ref, useful=True)])
    rows = {r['rule_id']: r for r in store.list_rules()}
    assert result['correct'] is False and result['rules_voted'] == 1
    assert rows[key]['positive_votes'] == 1 and rows[unrelated]['positive_votes'] == 0
    assert rows[key]['usage_count'] == 0


def test_late_feedback_cannot_vote_on_edited_rule(tmp_path):
    from memory.fault_context_store import FaultContextStore
    store = rule_store(tmp_path/'memory')
    key = store.add_rule({'condition': 'old', 'conclusion': 'old cause'})
    old = FaultContextStore.rule_revision(store.list_rules()[0])
    store.update_rule(key, {'condition': 'new', 'conclusion': 'new cause'}, old)
    ref = dict(rule_id=key, revision=old)
    result = ExpertFeedbackStore(str(tmp_path/'feedback')).submit_feedback('i', 'assertion',
        context_learner=supervised(), trace_store=traces({'root_cause': 'old cause', 'recalled_rule_refs': [ref]}),
        fault_store=store, rule_feedback=[dict(ref, useful=True)])
    assert result['rules_voted'] == 0 and result['rule_vote_results'][0]['skipped'] == 1
    assert store.list_rules()[0]['positive_votes'] == 0


def test_rule_judgment_must_match_original_trace_before_learning(tmp_path):
    from memory.feedback_contract import FeedbackConflict
    service = supervised()
    with pytest.raises(FeedbackConflict):
        ExpertFeedbackStore(str(tmp_path)).submit_feedback('i', 'assertion',
            context_learner=service, trace_store=traces(),
            rule_feedback=[{'rule_id': 'invented', 'revision': 'a'*64, 'useful': True}])
    service.learn_supervised.assert_not_called()


def test_stale_diagnosis_is_recorded_without_effects(tmp_path):
    service = supervised()
    result = ExpertFeedbackStore(str(tmp_path)).submit_feedback('i', 'assertion',
        context_learner=service, trace_store=traces(), expected_diagnosis_revision='0'*64)
    assert result['learning_status'] == 'blocked_stale_diagnosis'
    service.learn_supervised.assert_not_called()


def test_blocked_feedback_cannot_enter_next_diagnostic_context(tmp_path):
    store = ExpertFeedbackStore(str(tmp_path))
    store.submit_feedback('unmatched', 'unverified assertion')
    assert len(store.get_recent_feedback()) == 1
    assert store.get_recent_feedback(for_context=True) == []


def test_old_store_learning_cannot_erase_an_intervening_edit_or_vote(tmp_path):
    from memory.fault_context_store import FaultContextStore
    first = rule_store(tmp_path)
    stale = rule_store(tmp_path)
    original = {'condition': 'old', 'conclusion': 'old cause', 'cluster_id': 'a', 'namespace': 'payments'}
    key = first.add_rule(original)
    assert 'rule_id' not in original
    rev = FaultContextStore.rule_revision(first.list_rules()[0])
    first.update_rule(key, {'condition': 'edited', 'conclusion': 'edited cause'}, rev)
    first.decide_rule(key, revision=first.rule_revision(first.list_rules()[0]), decision='approve',
        reviewer='regression', reason='controlled fixture review', event_id='approve-edited')
    current = FaultContextStore.rule_revision(first.list_rules()[0])
    first.record_rule_usage([key], positive=True, expected_revisions={key: current})
    stale.add_rule({'condition': 'another', 'conclusion': 'another cause'})
    stale.add_fault({'description': 'new incident', 'root_cause': 'another cause'})
    rows = {r['rule_id']: r for r in rule_store(tmp_path).list_rules()}
    assert rows[key]['condition'] == 'edited' and rows[key]['positive_votes'] == 1
    assert len(rows) == 2


def test_duplicate_rule_extraction_does_not_reset_votes(tmp_path):
    from memory.fault_context_store import FaultContextStore
    store = rule_store(tmp_path)
    content = {'condition': 'full', 'conclusion': 'pressure'}
    key = store.add_rule(content)
    rev = FaultContextStore.rule_revision(store.list_rules()[0])
    store.record_rule_usage([key], positive=True, expected_revisions={key: rev})
    assert store.add_rule(content) == key
    assert store.list_rules()[0]['positive_votes'] == 1


def test_api_replays_receipt_and_rejects_changed_event(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import web_app.app as app
    cfg = SimpleNamespace(llm=SimpleNamespace(), memory=SimpleNamespace(auto_learn=True))
    model = SimpleNamespace(json_chat=Mock(return_value={'correct': False, 'rules': []}))
    fb = ExpertFeedbackStore(str(tmp_path/'feedback'))
    monkeypatch.setattr(app, '_get_config', lambda: cfg)
    monkeypatch.setattr(app, '_llm_configured', lambda _: True)
    monkeypatch.setattr(app, 'LLMClient', lambda _: model)
    monkeypatch.setattr(app, '_get_feedback_store', lambda: fb)
    monkeypatch.setattr(app, '_get_fault_store', lambda: rule_store(tmp_path/'memory'))
    monkeypatch.setattr(app, '_get_trace_store', traces)
    client = TestClient(app.app)
    body = dict(incident_id='i', expert_diagnosis='corrected cause', event_id='api-1')
    first = client.post('/api/knowledge/feedback', json=body)
    assert first.status_code == 200 and first.json()['correct'] is False
    second = client.post('/api/knowledge/feedback', json=body)
    assert second.status_code == 200 and second.json()['replayed']
    changed = client.post('/api/knowledge/feedback', json=dict(body, expert_diagnosis='changed'))
    assert changed.status_code == 409 and model.json_chat.call_count == 1
    assert client.post('/api/knowledge/feedback', json=[]).status_code == 422
    assert client.post('/api/knowledge/feedback', json=dict(body, rule_feedback=[{'useful':'false'}])).status_code == 422
