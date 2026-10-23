import asyncio
import copy
import json

import pytest

from agents.alert_compression import CompressionStore, compress_alerts, propose_relations
from agents.alert_jobs import AlertJobs
from agents.alert_review import ReviewBudget, batches, message_bytes, messages


def fixture(n=3):
    result = compress_alerts([dict(name='Failure', message='connection refused', source='logs',
        timestamp=1000, cluster_id='c', namespace='ns', pod=f'p-{i}') for i in range(n+1)])
    ids = [g['group_id'] for g in result['groups']]
    result['candidate_pairs'] = [dict(left=ids[0], right=key) for key in ids[1:]]
    return result


class Model:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def async_json_chat(self, request, **kwargs):
        self.calls.append(json.loads(request[-1]['content']))
        if self.fail:
            raise TimeoutError('fixture')
        return {'relations': []}


def job(tmp_path, result, cap, model=None):
    store, output = AlertJobs(tmp_path/'jobs'), CompressionStore(tmp_path/'scans')
    key = store.create({'semantic':True,'review_budget':cap})['job_id']
    _, payload = store.claim()
    async def prepare(_):
        return copy.deepcopy(result)
    model = model or Model()
    asyncio.run(store.process(key, payload, prepare, lambda _: (model,60), output))
    return store, output, key, model


@pytest.mark.parametrize('field,reason', [('max_batch_input_bytes','batch_input_bytes'),
                                         ('max_input_bytes','input_bytes')])
def test_nonzero_but_unusable_input_budget_cannot_resume(tmp_path, field, reason):
    store, output, key, model = job(tmp_path, fixture(1), {field:1})
    info = AlertJobs(tmp_path/'jobs').get(key)
    assert info['status'] == 'partial' and not model.calls
    assert info['budget']['exhausted_reason'] is None
    assert info['can_resume'] is False
    assert info['resume_blocked_reason'] == reason
    with pytest.raises(ValueError, match=reason):
        store.control(key, 'resume')
    assert output.get(info['scan_id'])['retained_alerts'] == 2


def test_new_batch_shrinks_to_remaining_total_budget(tmp_path):
    result = fixture(3)
    entries = next(batches(result,65536))[1]
    one_size = message_bytes(messages(entries[:1]))
    store, output, key, model = job(tmp_path, result, {'max_input_bytes':one_size+1})
    assert [len(call) for call in model.calls] == [1]
    saved = output.get(store.get(key)['scan_id'])
    assert saved['semantic']['completed_pairs'] == 1
    assert saved['semantic']['deferred_pairs'] == 2
    assert saved['raw_alerts'] == result['raw_alerts']
    info = store.get(key)
    assert info['budget']['input_bytes'] == one_size
    assert info['can_resume'] is False and info['resume_blocked_reason'] == 'input_bytes'


def uneven_fixture():
    result = compress_alerts([dict(name='Failure', message=('x'*500 if i<2 else 'short'),
        source='logs',timestamp=1000,cluster_id='c',namespace='ns',pod=f'p-{i}') for i in range(4)])
    ids = [g['group_id'] for g in result['groups']]
    result['candidate_pairs'] = [dict(left=ids[0],right=ids[1]),dict(left=ids[2],right=ids[3])]
    entries = next(batches(result,65536))[1]
    assert message_bytes(messages(entries[:1])) > message_bytes(messages(entries[1:]))
    return result, message_bytes(messages(entries[1:]))


def test_oversized_first_pair_does_not_block_a_later_affordable_pair(tmp_path):
    result, small_size = uneven_fixture()
    store, output, key, model = job(tmp_path,result,{'max_input_bytes':small_size})
    assert len(model.calls) == 1
    assert model.calls[0][0]['left'] == result['candidate_pairs'][1]['left']
    saved = output.get(store.get(key)['scan_id'])
    assert saved['semantic']['completed_pairs'] == saved['semantic']['deferred_pairs'] == 1
    assert saved['semantic']['batches'][0]['reason'] == 'input_bytes'


def test_oversized_pair_does_not_disable_retry_of_an_affordable_failed_batch(tmp_path):
    result, small_size = uneven_fixture()
    store, output, key, model = job(tmp_path,result,{'max_batch_input_bytes':small_size},Model(fail=True))
    assert store.get(key)['can_resume'] is True
    assert store.get(key)['retryable_pairs'] == 1
    store.control(key,'resume')
    _, payload = store.claim()
    retry = Model()
    async def forbidden(_):
        raise AssertionError('must reuse prepared snapshot')
    asyncio.run(store.process(key,payload,forbidden,lambda _: (retry,60),output))
    assert len(retry.calls) == 1
    info = store.get(key)
    assert info['status'] == 'partial' and info['completed_pairs'] == 1
    assert info['can_resume'] is False and info['resume_blocked_reason'] == 'batch_input_bytes'
    assert output.get(info['scan_id'])['revision'] == 2


def test_retryable_failure_becomes_blocked_after_deadline(tmp_path,monkeypatch):
    store, _, key, _ = job(tmp_path,fixture(1),{},Model(fail=True))
    info = store.get(key)
    assert info['can_resume'] is True
    monkeypatch.setattr('agents.alert_review.time.time',lambda:info['budget']['deadline_at']+1)
    assert store.get(key)['can_resume'] is False
    assert store.get(key)['resume_blocked_reason'] == 'deadline'


def test_existing_failed_checkpoint_is_not_split_to_fit_a_smaller_remaining_budget(tmp_path):
    result = fixture(21)
    entries = next(batches(result,65536))[1]
    first_size = message_bytes(messages(entries))
    store, _, key, _ = job(tmp_path,result,{'max_input_bytes':first_size+8000},Model(fail=True))
    # Failed batch of 20 is immutable; the final failed singleton remains retryable.
    assert store.get(key)['can_resume'] is True
    store.control(key,'resume')
    _, payload = store.claim()
    retry = Model()
    output = CompressionStore(tmp_path/'scans')
    async def forbidden(_):
        raise AssertionError('must reuse checkpoint offsets')
    asyncio.run(store.process(key,payload,forbidden,lambda _: (retry,60),output))
    assert [len(call) for call in retry.calls] == [1]
    assert store.get(key)['failed_pairs'] == 20
    assert store.get(key)['completed_pairs'] == 1
    assert store.get(key)['can_resume'] is False


def test_old_partial_job_without_outcome_is_assessed_once(tmp_path, monkeypatch):
    store, output, key, _ = job(tmp_path,fixture(2),{'max_batch_input_bytes':1})
    with store.connect() as db:
        db.execute('DELETE FROM review_outcomes WHERE job=?',(key,))
    restored = AlertJobs(tmp_path/'jobs')
    info = restored.get(key)
    assert info['can_resume'] is False and info['resume_blocked_reason'] == 'batch_input_bytes'
    def forbidden(*a, **kw):
        raise AssertionError('must not rebuild unchanged prompts on each status poll')
    monkeypatch.setattr(restored, 'legacy_review_outcome', forbidden)
    assert restored.get(key)['can_resume'] is False
    assert output.get(info['scan_id'])['retained_alerts'] == 3


def test_legacy_assessment_does_not_overwrite_a_new_worker_outcome(tmp_path, monkeypatch):
    import agents.alert_review as module
    store, _, key, _ = job(tmp_path,fixture(1),{'max_batch_input_bytes':1})
    budget = store.get(key)['budget']
    with store.connect() as db:
        db.execute('DELETE FROM review_outcomes WHERE job=?',(key,))
    original = module.batches
    newer = {'batches':[{'status':'failed','pairs':1,'input_bytes':1}]}
    def racing(*args,**kwargs):
        for item in original(*args,**kwargs):
            yield item
        store.save_review_outcome(key,newer)
    monkeypatch.setattr(module,'batches',racing)
    actual = store.legacy_review_outcome(key,budget)
    assert actual == newer


def test_model_unavailable_is_retryable_within_remaining_budget(tmp_path):
    store, output = AlertJobs(tmp_path/'jobs'), CompressionStore(tmp_path/'scans')
    key = store.create({'semantic':True})['job_id']
    _, payload = store.claim()
    async def prepare(_):
        return fixture(2)
    asyncio.run(store.process(key,payload,prepare,lambda _: (None,60),output))
    assert store.get(key)['can_resume'] is True
    assert store.get(key)['retryable_pairs'] == 2
    store.control(key,'resume')
    _, payload = store.claim()
    model = Model()
    asyncio.run(store.process(key,payload,prepare,lambda _: (model,60),output))
    assert store.get(key)['status'] == 'completed' and len(model.calls) == 1


@pytest.mark.parametrize('interruption', ['publish_failure','cancel_after_last_call'])
def test_finished_review_can_resume_publication_even_with_exhausted_call_budget(tmp_path, monkeypatch, interruption):
    store, output = AlertJobs(tmp_path/'jobs'), CompressionStore(tmp_path/'scans')
    key = store.create({'semantic':True,'review_budget':{'max_model_calls':1}})['job_id']
    _,payload = store.claim()
    async def prepare(_):
        return fixture(1)
    class Finish(Model):
        async def async_json_chat(self,*args,**kwargs):
            answer = await super().async_json_chat(*args,**kwargs)
            if interruption == 'cancel_after_last_call':
                store.control(key,'cancel')
            return answer
    model = Finish()
    original = output._write
    def fail_write(*args):
        raise OSError('synthetic publish failure')
    if interruption == 'publish_failure':
        monkeypatch.setattr(output,'_write',fail_write)
    try:
        asyncio.run(store.process(key,payload,prepare,lambda _: (model,60),output))
    except OSError:
        store.state(key,'failed','OSError')
    monkeypatch.setattr(output,'_write',original)
    info = store.get(key)
    assert info['budget']['exhausted_reason'] == 'model_calls'
    assert info['can_resume'] is True and info['resume_action'] == 'publish_result'
    store.control(key,'resume')
    _,payload = store.claim()
    async def forbidden(_):
        raise AssertionError('must reuse saved evidence')
    asyncio.run(store.process(key,payload,forbidden,lambda _: (model,60),output))
    assert len(model.calls) == 1 and store.get(key)['status'] == 'completed'
    assert output.get(store.get(key)['scan_id'])['retained_alerts'] == 2
