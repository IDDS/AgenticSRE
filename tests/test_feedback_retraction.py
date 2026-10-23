"""Durable withdrawal, concurrent learning, and consumption regressions; remote only."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from memory.expert_feedback import ExpertFeedbackStore
from memory.fault_context_store import FaultContextStore
from memory.feedback_contract import FeedbackConflict, FeedbackStorageError, atomic_json
from memory.memory_scope import MemoryScope
from memory.rule_lifecycle import effective_status


@pytest.fixture(params=['json', 'chromadb'])
def system(tmp_path, monkeypatch, request):
    monkeypatch.setenv('AGENTICSRE_FEEDBACK_DIR', str(tmp_path/'feedback'))
    feedback = ExpertFeedbackStore()
    rules = FaultContextStore(SimpleNamespace(memory=SimpleNamespace(db_path=str(tmp_path/'rules'), backend='json')))
    if request.param == 'chromadb':
        import chromadb
        class Embedding:
            def __call__(self, input): return [[1., 0., 0.] for _ in input]
            def embed_query(self, input): return self(input)
            @staticmethod
            def name(): return 'controlled-withdrawal-embedding'
        client=chromadb.PersistentClient(path=str(tmp_path/'chroma'))
        rules._client=client
        rules._rules_col=client.get_or_create_collection('withdrawal-rules', embedding_function=Embedding())
        rules._faults_col=client.get_or_create_collection('withdrawal-faults', embedding_function=Embedding())
        rules._backend='chromadb'
    return feedback, rules


def scope():
    return MemoryScope(cluster_id='a', namespace='payments')


def trace(refs=None):
    return SimpleNamespace(get_trace_by_id=lambda _: SimpleNamespace(result={
        'root_cause':'pool exhausted','fault_type':'Pool','cluster_id':'a','namespace':'payments',
        'recalled_rule_refs':refs or []}))


def service():
    return SimpleNamespace(learn_supervised=Mock(return_value=dict(status='success',correct=True,rules_added=0)))


def submit(feedback, **kw):
    return feedback.submit_feedback('incident-a','expert pool assertion',trace_store=trace(),context_learner=service(),**kw)


def retract(feedback, receipt, **kw):
    return feedback.retract_feedback(receipt['feedback_id'],revision=receipt['feedback_revision'],
        reviewer='fixture operator',reason='controlled evidence corrected',event_id='withdraw',**kw)


def row(rules, key):
    return next(r for r in rules.list_rules() if r['rule_id']==key)


def approve(rules, key):
    return rules.decide_rule(key,revision=rules.rule_revision(row(rules,key)),decision='approve',
        reviewer='fixture reviewer',reason='controlled review',event_id='approve')


def derived(rules, feedback_id):
    return rules.add_rule(dict(condition='pool full',conclusion='pool exhausted',source='supervised',
        source_feedback_id=feedback_id,cluster_id='a',namespace='payments'))


def test_retraction_blocks_previously_approved_memory_and_replay_never_revives(system):
    feedback,rules=system
    receipt=submit(feedback)
    key=derived(rules,receipt['feedback_id'])
    approve(rules,key)
    assert rules.query_similar_rules('pool full',memory_scope=scope())
    withdrawn=retract(feedback,receipt)
    assert withdrawn['feedback_state']=='retracted'
    assert rules.query_similar_rules('pool full',memory_scope=scope())==[]
    assert rules.query_similar_rules('pool full')==[]
    assert effective_status(row(rules,key))=='source_blocked'
    assert row(rules,key)['status']=='active'  # Original audit is preserved.
    assert retract(feedback,receipt)['replayed']
    assert submit(feedback)['feedback_state']=='retracted'
    assert feedback.diagnosis_stats(fault_type='Pool')['diagnosis_sample_count']==0
    assert ExpertFeedbackStore().get_recent_feedback()[0]['feedback_history'][0]['operation']=='retract'


def test_edit_cannot_remove_withdrawn_source_or_reapprove(system):
    feedback,rules=system
    receipt=submit(feedback)
    key=derived(rules,receipt['feedback_id'])
    retract(feedback,receipt)
    rules.update_rule(key,{'conclusion':'edited hypothesis'},rules.rule_revision(row(rules,key)))
    with pytest.raises(ValueError): approve(rules,key)
    assert rules.query_similar_rules('pool full',memory_scope=scope())==[]


def test_new_rules_written_after_withdrawal_are_also_blocked(system):
    feedback,rules=system
    receipt=submit(feedback)
    retract(feedback,receipt)
    key=derived(rules,receipt['feedback_id'])
    with pytest.raises(ValueError): approve(rules,key)


def test_stale_and_changed_retraction_events_cannot_overwrite_history(system):
    feedback,_=system
    receipt=submit(feedback)
    retract(feedback,receipt)
    saved=feedback._feedback_file.read_bytes()
    with pytest.raises(FeedbackConflict):
        feedback.retract_feedback(receipt['feedback_id'],revision=receipt['feedback_revision'],
            reviewer='changed actor',reason='other reason',event_id='withdraw')
    with pytest.raises(FeedbackConflict):
        feedback.retract_feedback(receipt['feedback_id'],revision=receipt['feedback_revision'],
            reviewer='fixture operator',reason='controlled evidence corrected',event_id='new')
    assert feedback._feedback_file.read_bytes()==saved


def test_failed_ledger_write_does_not_claim_retraction(system,monkeypatch):
    feedback,rules=system
    receipt=submit(feedback)
    key=derived(rules,receipt['feedback_id']);approve(rules,key)
    with monkeypatch.context() as patch:
        patch.setattr(feedback,'_save',Mock(side_effect=OSError('controlled disk failure')))
        with pytest.raises(OSError): retract(feedback,receipt)
    assert rules.query_similar_rules('pool full',memory_scope=scope())
    retract(feedback,receipt)
    assert not rules.query_similar_rules('pool full',memory_scope=scope())


def test_late_learning_finalization_cannot_overwrite_retraction(system):
    feedback,rules=system
    started,release=threading.Event(),threading.Event()
    def learn(**kw):
        started.set();assert release.wait(10)
        key=derived(rules,kw['source_feedback_id'])
        return dict(status='success',correct=True,rules_added=1,rule_ids=[key])
    with ThreadPoolExecutor(1) as pool:
        work=pool.submit(feedback.submit_feedback,'incident-a','expert pool assertion',trace_store=trace(),
            context_learner=SimpleNamespace(learn_supervised=learn),fault_store=rules,event_id='inflight')
        assert started.wait(10)
        pending=feedback.get_recent_feedback()[0]
        retract(ExpertFeedbackStore(),pending)
        release.set();result=work.result(10)
    assert result['learning_status']=='success' and result['feedback_state']=='retracted'
    assert len(feedback.get_recent_feedback()[0]['feedback_history'])==1
    assert not rules.query_similar_rules('pool full',memory_scope=scope())
    assert feedback.diagnosis_stats(fault_type='Pool')['diagnosis_sample_count']==0


def test_withdrawal_does_not_resurrect_an_older_incident_label(system):
    feedback,_=system
    submit(feedback,event_id='old')
    newer=submit(feedback,event_id='new')
    retract(feedback,newer)
    assert feedback.diagnosis_stats(fault_type='Pool')['diagnosis_sample_count']==0
    assert feedback.diagnosis_stats(fault_type='Pool',memory_scope=scope())['diagnosis_sample_count']==0
    submit(feedback,event_id='explicit-correction')
    assert feedback.diagnosis_stats(fault_type='Pool')['diagnosis_sample_count']==1


def test_original_votes_are_quarantined_without_fabricated_counter_reversal(system):
    feedback,rules=system
    key=rules.add_rule(dict(condition='pool full',conclusion='pool exhausted',cluster_id='a',namespace='payments'))
    ref=dict(rule_id=key,revision=rules.rule_revision(row(rules,key)))
    receipt=feedback.submit_feedback('incident-a','expert pool assertion',trace_store=trace([ref]),
        context_learner=service(),fault_store=rules,rule_feedback=[dict(ref,useful=True)])
    assert receipt['rules_voted']==1 and int(row(rules,key)['positive_votes'])==1
    assert rules.query_similar_rules('pool full',memory_scope=scope())
    retract(feedback,receipt)
    assert int(row(rules,key)['positive_votes'])==1
    assert not rules.query_similar_rules('pool full',memory_scope=scope())
    assert rules.record_rule_usage([key],positive=True,expected_revisions={key:ref['revision']})['updated']==0
    # Editing resets evidence. A separately reviewed new content version may
    # be used; it does not inherit votes from the withdrawn feedback.
    rules.update_rule(key,{'conclusion':'independently reviewed pool limit'},ref['revision'])
    approve(rules,key)
    assert int(row(rules,key)['positive_votes'])==0
    assert rules.query_similar_rules('pool full',memory_scope=scope())


@pytest.mark.parametrize('failure',['missing','corrupt'])
def test_missing_or_corrupt_authority_cannot_reactivate_source(system,failure):
    feedback,rules=system
    receipt=submit(feedback);key=derived(rules,receipt['feedback_id']);approve(rules,key)
    retract(feedback,receipt)
    if failure=='missing': feedback._feedback_file.unlink()
    else: feedback._feedback_file.write_text('not json')
    assert rules.query_similar_rules('pool full',memory_scope=scope())==[]
    if failure=='corrupt':
        with pytest.raises(FeedbackStorageError): rules.query_similar_rules('pool full',memory_scope=scope(),_raise_errors=True)


def test_misconfigured_custom_ledger_cannot_report_success(tmp_path):
    feedback=ExpertFeedbackStore(str(tmp_path/'alternate'))
    receipt=submit(feedback)
    with pytest.raises(ValueError,match='directory_mismatch'): retract(feedback,receipt)


def test_blocked_rule_does_not_starve_eligible_memory_before_top_k(system):
    feedback,rules=system
    receipt=submit(feedback);blocked=derived(rules,receipt['feedback_id']);approve(rules,blocked)
    good=rules.add_rule(dict(condition='pool full',conclusion='pool independently documented',cluster_id='a',namespace='payments'))
    retract(feedback,receipt)
    result=rules.query_similar_rules('pool full',n=1,memory_scope=scope(),_raise_errors=True)
    assert len(result)==1 and result[0]['rule_id']==good
    report=rules.governance_report()
    assert report['source_blocked_rules']==1 and report['active_rules']==1


def test_unknown_vote_completion_is_not_consumable(system):
    feedback,rules=system
    receipt=submit(feedback)
    key=rules.add_rule(dict(condition='pool full',conclusion='pool exhausted',cluster_id='a',namespace='payments'))
    revision=rules.rule_revision(row(rules,key))
    rules.record_rule_usage([key],positive=True,expected_revisions={key:revision},feedback_id=receipt['feedback_id'])
    entries=json.loads(feedback._feedback_file.read_text());entries[0]['learning_status']='outcome_unknown'
    atomic_json(feedback._feedback_file,entries)
    assert rules.query_similar_rules('pool full',memory_scope=scope())==[]


def test_legacy_vote_receipt_is_bound_to_original_version(system):
    feedback,rules=system
    receipt=submit(feedback)
    key=rules.add_rule(dict(condition='pool full',conclusion='pool exhausted',cluster_id='a',namespace='payments'))
    revision=rules.rule_revision(row(rules,key))
    rules.record_rule_usage([key],positive=True,expected_revisions={key:revision})
    entries=json.loads(feedback._feedback_file.read_text())
    entries[0]['rule_vote_results']=[dict(rule_id=key,revision=revision,updated=1,useful=True)]
    atomic_json(feedback._feedback_file,entries)
    retract(feedback,receipt)
    assert not rules.query_similar_rules('pool full',memory_scope=scope())
    rules.update_rule(key,{'conclusion':'new evidence'},revision)
    approve(rules,key)
    assert rules.query_similar_rules('pool full',memory_scope=scope())


@pytest.fixture
def api_client(system,monkeypatch):
    import web_app.app as web
    from fastapi.testclient import TestClient
    feedback,rules=system
    monkeypatch.setattr(web,'_get_feedback_store',lambda:feedback)
    monkeypatch.setattr(web,'_get_fault_store',lambda:rules)
    monkeypatch.setattr(web,'_auth_enabled',lambda:False)
    with TestClient(web.app) as client: yield client


def test_api_retraction_has_real_state_and_strict_receipts(system,api_client):
    feedback,rules=system
    receipt=submit(feedback);key=derived(rules,receipt['feedback_id']);approve(rules,key)
    body=dict(revision=receipt['feedback_revision'],reviewer='operator',reason='controlled correction',event_id='ui')
    route='/api/knowledge/feedback/'+receipt['feedback_id']+'/retract'
    assert api_client.post(route,json={**body,'source':'forged'}).status_code==422
    assert api_client.post(route,json={**body,'reason':''}).status_code==422
    response=api_client.post(route,json=body)
    assert response.status_code==200 and response.json()['feedback_state']=='retracted'
    assert api_client.post(route,json=body).json()['replayed']
    assert api_client.post(route,json={**body,'event_id':'stale'}).status_code==409
    assert api_client.get('/api/knowledge/rules').json()['rules'][0]['status']=='source_blocked'
    assert api_client.get('/api/knowledge/feedback').json()['stats']['retracted']==1


def test_api_uses_server_authenticated_actor(system,api_client,monkeypatch):
    import web_app.app as web
    receipt=submit(system[0])
    monkeypatch.setattr(web,'_auth_enabled',lambda:True)
    monkeypatch.setattr(web,'_get_auth_session',lambda _:dict(username='server-operator'))
    result=api_client.post('/api/knowledge/feedback/'+receipt['feedback_id']+'/retract',json=dict(
        revision=receipt['feedback_revision'],reviewer='forged',reason='checked',event_id='auth'))
    assert result.status_code==200
    assert result.json()['retraction']['reviewer']=='server-operator'
    assert result.json()['retraction']['identity_source']=='authenticated_session'
