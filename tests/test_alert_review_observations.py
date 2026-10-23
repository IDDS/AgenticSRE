import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from agents.alert_compression import CompressionStore, compress_alerts, propose_relations
from agents.alert_jobs import AlertJobs
from agents.alert_review import batches, messages, ReviewBudget
from agents.alert_review_observations import ReviewObserver, fingerprint


def prepared(n=1):
    result = compress_alerts([dict(name='Failure', message='refused', timestamp=1000,
        cluster_id='c', namespace='ns', pod=f'p-{i}', source='logs') for i in range(n+1)])
    ids = [g['group_id'] for g in result['groups']]
    result['candidate_pairs'] = [dict(left=ids[0], right=key) for key in ids[1:]]
    return result


class Model:
    model = 'fixture-model'
    temperature = .1

    def __init__(self, outcome='empty'):
        self.client = SimpleNamespace(base_url='https://fixture.invalid/v1')
        self.calls = 0
        self.outcome = outcome

    async def async_json_chat(self, request, **kwargs):
        self.calls += 1
        batch = json.loads(request[-1]['content'])
        if self.outcome == 'failed':
            raise TimeoutError('fixture')
        if self.outcome == 'empty':
            return {'relations': []}
        pair = batch[0]
        return {'relations': [dict(left=pair['left'], right=pair['right'], type='shared_root',
            reason='unverified fixture', evidence_alert_ids=(['outside-batch'] if self.outcome == 'rejected'
                else [row['alert_id'] for row in pair['samples']]))]}


def process(store, tmp_path, model, result=None, payload=None):
    result = result if result is not None else prepared()
    key = store.create(payload or {'semantic':True})['job_id']
    _, payload = store.claim()
    async def prepare(_): return copy.deepcopy(result)
    output = CompressionStore(tmp_path/'scans')
    asyncio.run(store.process(key,payload,prepare,lambda _: (model,60),output))
    return key, output.get(store.get(key)['scan_id'])


def records(store):
    with store.connect() as db:
        return [dict(row) for row in db.execute('SELECT * FROM review_observations ORDER BY rowid')]


@pytest.mark.parametrize('outcome,field', [('empty','prior_empty_requests'),('relations','prior_nonempty_requests')])
def test_exact_prior_review_is_observed_but_never_reused(tmp_path,outcome,field):
    store = AlertJobs(tmp_path/'jobs')
    model = Model(outcome)
    _, first = process(store,tmp_path,model)
    key, second = process(store,tmp_path,model)
    obs = store.get(key)['review_observation']
    assert obs[field] == obs['repeated_pairs'] == 1
    assert obs['reused_pairs'] == 0 and obs['reuse_allowed'] is False
    assert model.calls == 2 and first['raw_alerts'] == second['raw_alerts']
    assert second['semantic']['review_observation'] == obs
    assert all(r['status'] == 'unverified' for r in second['relations'])
    assert 'model_revision_missing' in obs['blocker_counts']


@pytest.mark.parametrize('change', ['model','provider','temperature','revision','prompt','output_tokens',
                                   'hidden_raw','batch_order','cluster','namespace','episode','policy'])
def test_contract_changes_do_not_count_as_equal_batches(tmp_path,monkeypatch,change):
    import agents.alert_review_observations as module
    store = AlertJobs(tmp_path/'jobs')
    result, model = prepared(2), Model()
    batch = next(batches(result,65536))[1]
    request = messages(batch)
    first = ReviewObserver(store,store.create({})['job_id'],result,model)
    receipt = first.begin(0,batch,request,2048)
    first.finish(receipt,{'semantic':{'status':'ok'},'relations':[]},0)
    result, batch, request = copy.deepcopy(result), copy.deepcopy(batch), copy.deepcopy(request)
    tokens = 2048
    if change == 'model': model.model = 'fixture-model-2'
    if change == 'provider': model.client.base_url = 'https://second.invalid/v1'
    if change == 'temperature': model.temperature = .3
    if change == 'revision': monkeypatch.setenv('AGENTICSRE_ALERT_REVIEW_MODEL_REVISION','fixture-r2')
    if change == 'prompt': request[0]['content'] += ' Additional constraint.'
    if change == 'output_tokens': tokens = 512
    if change == 'hidden_raw': result['raw_alerts'][0]['raw']['unsampled_field'] = 'changed'
    if change == 'batch_order':
        batch.reverse()
        request = messages(batch)
    if change in ('cluster','namespace'):
        result['groups'][0]['cluster_id' if change == 'cluster' else 'namespace'] = 'other'
    if change == 'episode': result['groups'][0]['lifecycle'] = [{'episode':2}]
    if change == 'policy': monkeypatch.setattr(module,'policy_fingerprint',lambda:'different-policy')
    second = ReviewObserver(store,store.create({})['job_id'],result,model)
    second.begin(0,batch,request,tokens)
    rows = records(store)
    assert rows[0]['match_key'] != rows[1]['match_key']
    assert rows[1]['prior_outcome'] is None


@pytest.mark.parametrize('outcome', ['failed','rejected'])
def test_failure_and_rejected_references_do_not_seed_success_matches(tmp_path,outcome):
    store = AlertJobs(tmp_path/'jobs')
    first, _ = process(store,tmp_path,Model(outcome))
    key, _ = process(store,tmp_path,Model())
    assert store.get(first)['review_observation']['outcomes'] == {outcome:1}
    assert store.get(key)['review_observation']['repeated_pairs'] == 0


def test_lookup_does_not_refresh_absolute_observation_window(tmp_path,monkeypatch):
    import agents.alert_review_observations as module
    clock = [1000.]
    monkeypatch.setattr(module.time,'time',lambda:clock[0])
    store, result, model = AlertJobs(tmp_path/'jobs'), prepared(), Model()
    batch = next(batches(result,65536))[1]
    def start():
        observer = ReviewObserver(store,store.create({})['job_id'],result,model)
        return observer, observer.begin(0,batch,messages(batch),2048)
    observer, receipt = start()
    clock[0] = 1001
    observer.finish(receipt,{'semantic':{'status':'ok'},'relations':[]},0)
    clock[0] = 1200
    start()  # Read the prior result, then simulate a crash before completion.
    assert records(store)[-1]['prior_outcome'] == 'empty'
    clock[0] = 1300.01
    start()
    assert records(store)[-1]['prior_outcome'] is None
    assert records(store)[-2]['outcome'] == 'incomplete'


def test_same_job_and_future_observations_are_not_cross_scan_matches(tmp_path,monkeypatch):
    import agents.alert_review_observations as module
    clock = [1000.]
    monkeypatch.setattr(module.time,'time',lambda:clock[0])
    store, result, model = AlertJobs(tmp_path/'jobs'), prepared(), Model()
    batch = next(batches(result,65536))[1]
    observer = ReviewObserver(store,store.create({})['job_id'],result,model)
    receipt = observer.begin(0,batch,messages(batch),2048)
    observer.finish(receipt,{'semantic':{'status':'ok'},'relations':[]},0)
    observer.begin(0,batch,messages(batch),2048)
    assert records(store)[-1]['prior_outcome'] is None
    clock[0] = 999
    ReviewObserver(store,store.create({})['job_id'],result,model).begin(0,batch,messages(batch),2048)
    assert records(store)[-1]['prior_outcome'] is None


@pytest.mark.parametrize('method', ['begin','finish'])
def test_observation_write_failure_does_not_change_review(tmp_path,monkeypatch,method):
    def fail(*args,**kwargs): raise OSError('private-error-marker')
    monkeypatch.setattr(ReviewObserver,method,fail)
    store, model = AlertJobs(tmp_path/'jobs'), Model('relations')
    key, saved = process(store,tmp_path,model)
    assert store.get(key)['status'] == 'completed' and model.calls == 1
    assert saved['semantic']['observation_errors'] == 1
    assert len(saved['relations']) == 1
    assert 'private-error-marker' not in json.dumps(saved)


def test_missing_observation_table_does_not_block_jobs(tmp_path):
    store, model = AlertJobs(tmp_path/'jobs'), Model()
    with store.connect() as db:
        db.execute('DROP TABLE review_observations')
    key, saved = process(store,tmp_path,model)
    assert store.get(key)['status'] == 'completed' and model.calls == 1
    assert saved['semantic']['observation_errors'] == 1
    assert saved['semantic']['review_observation']['status'] == 'unavailable'


def test_identity_is_digest_only_and_payload_cannot_claim_cache_eligibility(tmp_path):
    store, model = AlertJobs(tmp_path/'jobs'), Model()
    result = prepared()
    result['raw_alerts'][0]['raw']['private_context'] = 'private-evidence-marker'
    model.api_key = 'unused-credential-marker'
    key, _ = process(store,tmp_path,model,result,{'semantic':True,'review_contract':{
        'reuse_allowed':True,'model':'caller-model','episode':1}})
    encoded = json.dumps(records(store))
    assert all(value not in encoded for value in ('private-evidence-marker','unused-credential-marker',
                                                  model.model,model.client.base_url,'caller-model'))
    identity = json.loads(records(store)[0]['identity'])
    assert identity['model_sha256'] == fingerprint(model.model)
    assert store.get(key)['review_observation']['reuse_allowed'] is False


def test_unknown_identity_is_not_matched_and_legacy_jobs_are_not_backfilled(tmp_path):
    store, model = AlertJobs(tmp_path/'jobs'), Model()
    model.model = None
    process(store,tmp_path,model)
    key, _ = process(store,tmp_path,model)
    assert store.get(key)['review_observation']['repeated_pairs'] == 0
    assert store.get(key)['review_observation']['blocker_counts']['identity_incomplete'] == 1
    with store.connect() as db: db.execute('DELETE FROM review_observations')
    assert 'review_observation' not in store.get(key)
    assert records(store) == []


def test_zero_budget_creates_no_observations(tmp_path):
    store, model = AlertJobs(tmp_path/'jobs'), Model()
    key, _ = process(store,tmp_path,model,payload={'semantic':True,'review_budget':{'max_model_calls':0}})
    assert model.calls == 0 and records(store) == []
    assert 'review_observation' not in store.get(key)


def test_resume_records_new_attempt_contract_and_preserves_successful_checkpoint(tmp_path):
    store = AlertJobs(tmp_path/'jobs')
    key, _ = process(store,tmp_path,Model('failed'))
    first = records(store)[0]
    store.control(key,'resume')
    _, payload = store.claim()
    model = Model('relations')
    model.model = 'updated-fixture-model'
    async def forbidden(_): raise AssertionError('must keep prepared facts')
    output = CompressionStore(tmp_path/'scans')
    asyncio.run(store.process(key,payload,forbidden,lambda _: (model,60),output))
    rows = records(store)
    assert len(rows) == 2 and rows[0] == first
    assert rows[0]['match_key'] != rows[1]['match_key']
    assert store.checkpoints(key)[0]['semantic']['observation_id'] == rows[1]['id']
    assert store.get(key)['review_observation']['outcomes'] == {'failed':1,'relations':1}
    # Retry publication after a simulated failure; successful review is not repeated.
    with store.connect() as db:
        db.execute("UPDATE jobs SET status='failed' WHERE id=?", (key,))
    store.control(key,'resume')
    _, payload = store.claim()
    asyncio.run(store.process(key,payload,forbidden,lambda _: (model,60),output))
    assert len(records(store)) == 2 and model.calls == 1


def test_observer_time_is_included_in_review_deadline(monkeypatch):
    import agents.alert_review_observations as module
    clock = [1000.]
    monkeypatch.setattr(module.time,'time',lambda:clock[0])
    class SlowObserver:
        def begin(self,*args):
            clock[0] += 121
            return 'receipt'
        def finish(self,receipt,answer,rejected):
            assert answer['semantic']['status'] == 'failed'
    result, model = prepared(), Model()
    asyncio.run(propose_relations(result,model,budget=ReviewBudget(),observer=SlowObserver()))
    assert model.calls == 0 and result['semantic']['failed_pairs'] == 1
