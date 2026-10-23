import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from agents.alert_compression import CompressionStore, compress_alerts, propose_relations
from agents.alert_jobs import AlertJobs, JobReviewBudget, scan_page
from agents.alert_review import ReviewBudget, batches, limits, message_bytes, messages


def fixture(n=45):
    result = compress_alerts([dict(name='Failure', message='connection refused', source='logs',
        timestamp=1000, cluster_id='c', namespace='ns', pod=f'p-{i}') for i in range(n+1)])
    ids = [g['group_id'] for g in result['groups']]
    result['candidate_pairs'] = [dict(left=ids[0], right=key) for key in ids[1:]]
    return result


class Model:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    async def async_json_chat(self, request, **kw):
        self.calls.append((json.loads(request[-1]['content']), kw))
        if self.fail == len(self.calls):
            raise TimeoutError('fixture')
        return {'relations': []}


def run_job(tmp_path, cap=None, n=45, model=None):
    store, output = AlertJobs(tmp_path/'jobs'), CompressionStore(tmp_path/'scans')
    payload = {'semantic': True}
    if cap is not None:
        payload['review_budget'] = cap
    key = store.create(payload)['job_id']
    job, payload = store.claim()
    original = fixture(n)
    async def prepare(_):
        return copy.deepcopy(original)
    model = model or Model()
    asyncio.run(store.process(job, payload, prepare, lambda _: (model, 60), output))
    return store, output, key, model, original


def test_budget_exhaustion_publishes_all_facts_and_paged_pending_pairs(tmp_path):
    store, output, key, model, original = run_job(tmp_path, {'max_model_calls': 1})
    info = store.get(key)
    saved = output.get(info['scan_id'])
    assert info['status'] == 'partial' and len(model.calls) == 1
    assert saved['semantic']['completed_pairs'] == 20
    assert saved['semantic']['deferred_pairs'] == 25
    assert saved['semantic']['coverage_complete'] is False
    assert saved['raw_alerts'] == original['raw_alerts'] and saved['groups'] == original['groups']
    page = scan_page(saved, 'review_pairs', 19, 26)
    assert page['items'][0]['review_status'] == 'ok'
    assert all(p['review_status'] == 'deferred' for p in page['items'][1:])
    dossier = output.dossier(saved['scan_id'], saved['groups'][-1]['group_id'], saved['revision'])
    assert dossier['alert_samples'] and dossier['semantic_review']['deferred_pairs'] == 25
    assert all(e['causal_confirmed'] is False for e in saved['scag']['edges'])
    with pytest.raises(ValueError, match='budget exhausted'):
        AlertJobs(tmp_path/'jobs').control(key, 'resume')


def test_failed_batch_keeps_readable_partial_then_resume_increments_revision(tmp_path):
    store, output, key, model, original = run_job(tmp_path, model=Model(fail=2))
    before = store.get(key)
    assert before['status'] == 'partial' and before['result_revision'] == 1
    partial = output.get(before['scan_id'])
    assert partial['semantic']['completed_pairs'] == 25 and partial['semantic']['failed_pairs'] == 20
    assert len(scan_page(partial, 'review_pairs', 0, 100)['items']) == 45
    store = AlertJobs(tmp_path/'jobs')
    store.control(key, 'resume')
    _, payload = store.claim()
    async def forbidden(_):
        pytest.fail('must reuse prepared snapshot')
    retry = Model()
    asyncio.run(store.process(key, payload, forbidden, lambda _: (retry, 60), output))
    after = store.get(key)
    assert after['status'] == 'completed' and after['result_revision'] == 2
    assert after['model_calls'] == 4 and len(retry.calls) == 1
    assert after['budget']['reserved_output_tokens'] <= after['budget']['limits']['max_output_tokens']
    assert output.get(after['scan_id'])['raw_alerts'] == original['raw_alerts']
    with pytest.raises(ValueError, match='stale revision'):
        output.dossier(after['scan_id'], original['groups'][0]['group_id'], 1)


def test_reservation_survives_crash_before_checkpoint(tmp_path):
    store, output = AlertJobs(tmp_path/'jobs'), CompressionStore(tmp_path/'scans')
    payload = {'semantic': True, 'review_budget': {'max_model_calls': 1}}
    key = store.create(payload)['job_id']
    store.claim()
    store.prepare(key, fixture(2))
    assert JobReviewBudget(store, key, payload['review_budget']).reserve(100, 2048)['allowed']
    store = AlertJobs(tmp_path/'jobs')
    async def forbidden(_):
        pytest.fail('prepared data lost')
    model = Model()
    asyncio.run(store.process(key, payload, forbidden, lambda _: (model, 60), output))
    assert model.calls == [] and store.get(key)['model_calls'] == 1
    assert output.get(store.get(key)['scan_id'])['semantic']['deferred_pairs'] == 2


def test_deadline_is_shared_by_batches_and_survives_new_budget_object(tmp_path, monkeypatch):
    import agents.alert_review as module
    clock = [1000.0]
    monkeypatch.setattr(module.time, 'time', lambda: clock[0])
    class Slow(Model):
        async def async_json_chat(self, *args, **kw):
            answer = await super().async_json_chat(*args, **kw)
            clock[0] += 11
            return answer
    store, output, key, model, _ = run_job(tmp_path, {'max_review_seconds': 10}, model=Slow())
    assert len(model.calls) == 1
    assert store.get(key)['budget']['exhausted_reason'] == 'deadline'
    assert JobReviewBudget(store, key, {}).snapshot()['deadline_at'] == 1010
    assert output.get(store.get(key)['scan_id'])['semantic']['deferred_pairs'] == 25


@pytest.mark.parametrize('cap,reason', [({'max_model_calls': 0}, 'model_calls'),
    ({'max_review_seconds': 0}, 'deadline'), ({'max_output_tokens': 0}, 'output_tokens'),
    ({'max_input_bytes': 1}, 'input_bytes'), ({'max_batch_input_bytes': 1}, 'batch_input_bytes')])
def test_zero_or_tiny_budgets_defer_without_sending_or_dropping_facts(tmp_path, cap, reason):
    store, output, key, model, original = run_job(tmp_path, cap, n=2)
    saved = output.get(store.get(key)['scan_id'])
    assert model.calls == [] and saved['raw_alerts'] == original['raw_alerts']
    assert saved['semantic']['deferred_pairs'] == 2
    assert saved['semantic']['stop_reason'] == reason
    assert saved['semantic']['budget']['actual_input_tokens'] is None


def test_output_token_reservation_bounds_the_model_call():
    result, model = fixture(1), Model()
    budget = ReviewBudget({'max_output_tokens': 600})
    asyncio.run(propose_relations(result, model, budget=budget))
    assert model.calls[0][1]['max_tokens'] == 600
    assert budget.snapshot()['reserved_output_tokens'] == 600
    assert budget.snapshot()['actual_output_tokens'] is None


def test_input_total_is_reserved_before_call():
    result, model = fixture(21), Model()
    batch = next(batches(result, 65536))[1]
    first_size = message_bytes(messages(batch))
    budget = ReviewBudget({'max_input_bytes': first_size})
    asyncio.run(propose_relations(result, model, budget=budget))
    assert len(model.calls) == 1
    assert budget.snapshot()['input_bytes'] == first_size
    assert result['semantic']['deferred_pairs'] == 1


@pytest.mark.parametrize('value', [False, [], {'typo': 1}, {'max_model_calls': True},
    {'max_model_calls': 1.2}, {'max_model_calls': -1}, {'max_model_calls': 101},
    {'max_review_seconds': float('nan')}, {'max_review_seconds': float('inf')}])
def test_invalid_budget_rejected_at_submission(tmp_path, value):
    with pytest.raises(ValueError):
        AlertJobs(tmp_path).create({'review_budget': value})


def test_group_interior_peak_beyond_truncated_text_reaches_reviewer_with_origin():
    prefix = 'x'*550+' duration_ms='
    rows = [dict(name='Timeout', source='logs', message=prefix+str(value), timestamp=1000+i,
                 cluster_id='c', namespace='ns', pod='p') for i, value in enumerate([20, 9000, 40])]
    rows.append(dict(name='Other', source='logs', message='connection failed', timestamp=1001,
                     cluster_id='c', namespace='ns', pod='p'))
    result = compress_alerts(rows)
    left = next(g for g in result['groups'] if g['alert_count'] == 3)
    right = next(g for g in result['groups'] if g['alert_count'] == 1)
    result['candidate_pairs'] = [dict(left=left['group_id'], right=right['group_id'])]
    original = copy.deepcopy(result)
    class PeakModel(Model):
        async def async_json_chat(self, request, **kw):
            pair = json.loads(request[-1]['content'])[0]
            evidence = pair['group_evidence'][left['group_id']]
            stats = evidence['log_numeric_summary']['message.duration_ms']
            assert stats['max'] == 9000 and stats['unit'] == 'ms'
            peak = next(s for s in evidence['samples'] if s['alert_id'] == stats['max_alert_id'])
            assert peak['message_truncated'] and '9000' not in peak['message']
            assert peak['log_numeric_observations']['message.duration_ms']['max'] == 9000
            return {'relations': [dict(left=pair['left'], right=pair['right'], type='related',
                evidence_alert_ids=[stats['max_alert_id'], pair['samples'][1]['alert_id']])]}
    asyncio.run(propose_relations(result, PeakModel()))
    assert len(result['relations']) == 1 and result['relations'][0]['status'] == 'unverified'
    assert result['groups'] == original['groups'] and result['raw_alerts'] == original['raw_alerts']


def test_small_batch_limit_resumes_without_repacking_successful_offsets(tmp_path):
    result = fixture(7)
    three = next(batches(result, 65536))[1][:3]
    cap = message_bytes(messages(three))
    store, output, key, model, _ = run_job(tmp_path, {'max_batch_input_bytes': cap}, n=7, model=Model(fail=2))
    assert list(map(lambda c: len(c[0]), model.calls)) == [3, 3, 1]
    store.control(key, 'resume')
    _, payload = store.claim()
    retry = Model()
    async def forbidden(_):
        pytest.fail('must reuse preparation')
    asyncio.run(store.process(key, payload, forbidden, lambda _: (retry, 60), output))
    assert len(retry.calls) == 1 and len(retry.calls[0][0]) == 3
    assert store.get(key)['completed_pairs'] == 7


def test_missing_model_still_publishes_facts(tmp_path):
    store, output = AlertJobs(tmp_path/'jobs'), CompressionStore(tmp_path/'scans')
    key = store.create({'semantic': True})['job_id']
    _, payload = store.claim()
    async def prepare(_):
        return fixture(2)
    asyncio.run(store.process(key, payload, prepare, lambda _: (None, 10), output))
    assert store.get(key)['status'] == 'partial'
    assert output.get(store.get(key)['scan_id'])['semantic']['status'] == 'unavailable'


def test_partial_result_cannot_overwrite_human_edits(tmp_path):
    store, output, key, _, _ = run_job(tmp_path, model=Model(fail=2))
    saved = output.get(store.get(key)['scan_id'])
    modified = copy.deepcopy(saved)
    modified['history'] = [{'action': 'human edit'}]
    modified['revision'] += 1
    output._write(output._path(saved['scan_id']), modified)
    with pytest.raises(ValueError, match='overwrite'):
        store.publish(key, output, saved, 1)
    assert output.get(saved['scan_id']) == modified


def test_real_client_transport_retries_and_timeout_are_bounded():
    from tools.llm_client import LLMClient
    configured, requests = [], []
    def create(**kw):
        requests.append(kw)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='{"relations": []}'))], usage=None)
    transport = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    def options(**kw):
        configured.append(kw)
        return transport
    client = object.__new__(LLMClient)
    client.client = SimpleNamespace(with_options=options)
    client.model, client.temperature, client.max_tokens = 'fixture', 0, 65536
    result = fixture(1)
    asyncio.run(propose_relations(result, client, timeout_s=2))
    assert result['semantic']['status'] == 'ok'
    assert configured[0]['max_retries'] == 0 and 0 < configured[0]['timeout'] <= 2
    assert len(requests) == 1


def test_zero_budget_does_not_build_model_prompts(monkeypatch):
    import agents.alert_review as module
    def forbidden(*args):
        pytest.fail('exhausted work must not prepare model evidence')
    monkeypatch.setattr(module, 'group_evidence', forbidden)
    result = fixture(100)
    asyncio.run(propose_relations(result, Model(), budget=ReviewBudget({'max_model_calls': 0})))
    assert result['semantic']['deferred_pairs'] == 100


def test_missing_model_on_resume_keeps_successful_checkpoint_relations(tmp_path):
    class Relations(Model):
        async def async_json_chat(self, request, **kw):
            await super().async_json_chat(request, **kw)
            return {'relations': [dict(left=p['left'], right=p['right'], type='shared_root',
                evidence_alert_ids=[s['alert_id'] for s in p['samples']])
                for p in json.loads(request[-1]['content'])]}
    store, output, key, _, _ = run_job(tmp_path, model=Relations(fail=2))
    store.control(key, 'resume')
    _, payload = store.claim()
    async def forbidden(_):
        pytest.fail('must reuse snapshot')
    asyncio.run(store.process(key, payload, forbidden, lambda _: (None, 60), output))
    saved = output.get(store.get(key)['scan_id'])
    assert len(saved['relations']) == 25
    assert saved['semantic']['completed_pairs'] == 25 and saved['semantic']['failed_pairs'] == 20
    assert store.get(key)['model_calls'] == 3


def test_legacy_checkpoint_without_reservations_does_not_reset_unknown_spend(tmp_path):
    store, output = AlertJobs(tmp_path/'jobs'), CompressionStore(tmp_path/'scans')
    payload = {'semantic': True}
    key = store.create(payload)['job_id']
    store.claim()
    store.prepare(key, fixture(21))
    store.checkpoint(key, 0, 20, {'semantic': {'status':'ok', 'model_calls':1}, 'relations': []})
    async def forbidden(_):
        pytest.fail('must reuse snapshot')
    model = Model()
    asyncio.run(store.process(key, payload, forbidden, lambda _: (model, 60), output))
    info = store.get(key)
    assert not model.calls and info['budget']['legacy_usage_unknown']
    assert info['model_calls'] == 1
    assert output.get(info['scan_id'])['semantic']['completed_pairs'] == 20
