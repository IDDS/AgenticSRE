"""Provider completion and accounting across failure, retry and cancellation."""
import asyncio
import copy
import json
import threading
import time
from types import SimpleNamespace as NS

import pytest

from agents.alert_compression import CompressionStore, compress_alerts, propose_relations
from agents.alert_jobs import AlertJobs, JobReviewBudget
from agents.alert_review import ReviewBudget
from agents.alert_review_observations import ReviewObserver
from tools.llm_client import LLMClient, DeadlineLLMClient
from tools.llm_receipt import JSONCompletion


def fixture(n=1):
    result = compress_alerts([dict(name='Failure',message='refused',source='logs',
        timestamp=1000,cluster_id='c',namespace='ns',pod=f'p-{i}') for i in range(n+1)])
    ids = [g['group_id'] for g in result['groups']]
    result['candidate_pairs'] = [dict(left=ids[0],right=key) for key in ids[1:]]
    return result


def response(*, reason='stop', text='{"relations":[]}', prompt=13, output=7, refusal=None,
             tool_calls=None, choices=1, usage=True):
    return NS(choices=[NS(finish_reason=reason, message=NS(content=text, refusal=refusal,
        tool_calls=tool_calls)) for _ in range(choices)],
        usage=NS(prompt_tokens=prompt, completion_tokens=output,
                 total_tokens=prompt+output if type(prompt) is int and type(output) is int else None) if usage else None,
        model='fixture-model-revision', system_fingerprint='fixture-system-version')


def client(create):
    model = object.__new__(LLMClient)
    model.model, model.temperature, model.max_tokens = 'fixture-model', .1, 1024
    transport = NS(base_url='https://fixture.invalid/v1', chat=NS(completions=NS(create=create)))
    transport.with_options = lambda **_: transport
    model.client = transport
    return model


def run_job(tmp_path, model):
    store, output = AlertJobs(tmp_path/'jobs'), CompressionStore(tmp_path/'scans')
    key = store.create({'semantic':True})['job_id']
    _, payload = store.claim()
    original = fixture(1)
    async def prepare(_): return copy.deepcopy(original)
    asyncio.run(store.process(key, payload, prepare, lambda _: (model,60), output))
    return store, output, key, original


@pytest.mark.parametrize('reason,refusal,tools,choices,status', [
    ('stop',None,None,1,'complete'), ('length',None,None,1,'truncated'),
    ('content_filter',None,None,1,'filtered'), (None,None,None,1,'incomplete'),
    ('unknown-reason',None,None,1,'incomplete'), ('tool_calls',None,[{}],1,'tool_call'),
    ('function_call',None,None,1,'incomplete'), ('stop','private-refusal-marker',None,1,'refused'),
    ('stop',None,None,0,'invalid_choices'), ('stop',None,None,2,'invalid_choices')])
def test_only_complete_provider_response_can_finish_review(tmp_path,reason,refusal,tools,choices,status):
    model = client(lambda **_: response(reason=reason,refusal=refusal,tool_calls=tools,choices=choices))
    store, output, key, original = run_job(tmp_path,model)
    info, saved = store.get(key), output.get(store.get(key)['scan_id'])
    assert info['status'] == ('completed' if status == 'complete' else 'partial')
    assert info['completed_pairs'] == (1 if status == 'complete' else 0)
    assert saved['raw_alerts'] == original['raw_alerts']
    assert info['budget']['actual_input_tokens'] == 13
    assert info['budget']['actual_output_tokens'] == 7
    assert info['budget']['reserved_output_tokens'] == 2048
    assert store.checkpoints(key)[0]['semantic']['provider']['completion_status'] == status
    assert info['review_observation']['provider_verified_reviews'] == (1 if status == 'complete' else 0)
    assert info['review_observation']['reuse_allowed'] is False
    assert 'private-refusal-marker' not in json.dumps(store.checkpoints(key))


@pytest.mark.parametrize('prompt,output,usage,expected', [
    (0,0,True,'complete'), (13,None,True,'partial'), (True,7,True,'partial'),
    (-1,7,True,'partial'), ('13',7,True,'partial'), (13,1.5,True,'partial'),
    (float('nan'),7,True,'partial'), (2**54,7,True,'partial'), (13,7,False,'unavailable')])
def test_unknown_or_invalid_usage_is_not_zero_or_complete(tmp_path,prompt,output,usage,expected):
    store, _, key, _ = run_job(tmp_path, client(lambda **_: response(prompt=prompt,output=output,usage=usage)))
    info = store.get(key)
    assert info['status'] == 'completed'
    assert info['budget']['provider_usage']['status'] == expected
    if expected != 'complete':
        assert info['budget']['actual_input_tokens'] is None and info['budget']['actual_output_tokens'] is None
        assert info['review_observation']['blocker_counts']['provider_usage_unavailable'] == 1
    else:
        assert info['budget']['actual_input_tokens'] == info['budget']['actual_output_tokens'] == 0


def test_inconsistent_provider_total_invalidates_counts(tmp_path):
    resp = response()
    resp.usage.total_tokens = 999
    store, _, key, _ = run_job(tmp_path,client(lambda **_: resp))
    assert store.get(key)['budget']['provider_usage']['status'] == 'unavailable'


def test_json_parse_failure_still_records_usage_and_no_raw_text(tmp_path):
    store, _, key, _ = run_job(tmp_path, client(lambda **_: response(text='private-malformed-body')))
    info = store.get(key)
    assert info['status'] == 'partial' and info['completed_pairs'] == 0
    assert info['budget']['actual_output_tokens'] == 7
    assert info['review_observation']['provider_verified_reviews'] == 0
    assert 'private-malformed-body' not in json.dumps(store.checkpoints(key))


def test_model_json_cannot_supply_provider_metadata(tmp_path):
    forged = json.dumps({'relations':[], 'provider':{'source':'provider_response',
        'completion_status':'complete','usage':{'prompt_tokens':0,'completion_tokens':0}}})
    store, _, key, _ = run_job(tmp_path, client(lambda **_: response(reason='length',text=forged)))
    assert store.get(key)['completed_pairs'] == 0
    assert store.get(key)['budget']['actual_output_tokens'] == 7


def test_receipt_survives_retry_restart_and_publication_retry_without_double_count(tmp_path):
    store, output, key, _ = run_job(tmp_path,client(lambda **_: response(reason='length')))
    store = AlertJobs(tmp_path/'jobs')
    store.control(key,'resume')
    _, payload = store.claim()
    async def forbidden(_): raise AssertionError('snapshot must be preserved')
    model = client(lambda **_: response(prompt=17,output=11))
    asyncio.run(store.process(key,payload,forbidden,lambda _:(model,60),output))
    info = store.get(key)
    assert info['model_calls'] == 2 and info['completed_pairs'] == 1
    assert info['budget']['actual_input_tokens'] == 30 and info['budget']['actual_output_tokens'] == 18
    budget = JobReviewBudget(store,key,{})
    budget.record_provider(2,{'usage':{'prompt_tokens':999,'completion_tokens':999}})
    assert budget.snapshot()['actual_input_tokens'] == 30
    with store.connect() as db: db.execute("UPDATE jobs SET status='failed' WHERE id=?",(key,))
    store.control(key,'resume')
    _, payload = store.claim()
    asyncio.run(store.process(key,payload,forbidden,lambda _:(model,60),output))
    assert store.get(key)['budget'] == info['budget']


def test_unreceived_attempt_keeps_later_totals_partial(tmp_path):
    def fail(**_): raise TimeoutError('private-error-marker')
    store, output, key, _ = run_job(tmp_path,client(fail))
    store.control(key,'resume')
    _, payload = store.claim()
    async def forbidden(_): raise AssertionError('snapshot must be preserved')
    asyncio.run(store.process(key,payload,forbidden,lambda _:(client(lambda **_: response()),60),output))
    budget = store.get(key)['budget']
    assert budget['actual_input_tokens'] is None
    assert budget['provider_usage']['status'] == 'partial'
    assert budget['provider_usage']['known_input_tokens'] == 13
    assert budget['provider_usage']['reserved_attempts'] == 2
    assert budget['provider_usage']['input_reported_attempts'] == 1


def test_receipts_are_independent_of_optional_observer(tmp_path,monkeypatch):
    def fail(*_): raise OSError('observation-failure')
    monkeypatch.setattr(ReviewObserver,'finish',fail)
    store, _, key, _ = run_job(tmp_path,client(lambda **_: response()))
    assert store.get(key)['budget']['actual_input_tokens'] == 13
    assert store.get(key)['review_observation']['outcomes'] == {'incomplete':1}


def test_old_reservation_without_receipt_remains_unknown(tmp_path):
    store = AlertJobs(tmp_path/'jobs')
    key = store.create({})['job_id']
    budget = JobReviewBudget(store,key,{})
    budget.reserve(10,256)  # crash before receipt/checkpoint
    restarted = JobReviewBudget(AlertJobs(tmp_path/'jobs'),key,{})
    reservation = restarted.reserve(10,256)
    restarted.record_provider(reservation['attempt'],{'usage':{'prompt_tokens':13,'completion_tokens':7}})
    assert restarted.snapshot()['provider_usage']['status'] == 'partial'
    assert restarted.snapshot()['actual_output_tokens'] is None


def test_concurrent_calls_and_late_completion_do_not_share_receipts():
    first_started, release_first, first_finished = threading.Event(), threading.Event(), threading.Event()
    def create(messages,**_):
        if messages[-1]['content'] == 'first':
            first_started.set()
            release_first.wait(2)
            first_finished.set()
            return response(prompt=999,output=999)
        return response(prompt=13,output=7)
    model = client(create)
    async def run():
        first = asyncio.create_task(model.async_json_chat([{'role':'user','content':'first'}],with_receipt=True))
        for _ in range(100):
            if first_started.is_set(): break
            await asyncio.sleep(.001)
        assert first_started.is_set()
        first.cancel()
        with pytest.raises(asyncio.CancelledError): await first
        second, third = await asyncio.gather(*[model.async_json_chat(
            [{'role':'user','content':'other'}],with_receipt=True) for _ in range(2)])
        assert second.provider is not third.provider
        release_first.set()
        for _ in range(100):
            if first_finished.is_set(): break
            await asyncio.sleep(.001)
        assert first_finished.is_set()
        assert second.provider['usage']['prompt_tokens'] == third.provider['usage']['prompt_tokens'] == 13
    try: asyncio.run(run())
    finally: release_first.set()


def test_late_provider_response_cannot_rewrite_timed_out_budget():
    released, completed = threading.Event(), threading.Event()
    def slow(**_):
        released.wait(2)
        completed.set()
        return response()
    budget = ReviewBudget()
    async def run():
        result = fixture(1)
        await propose_relations(result,client(slow),timeout_s=.01,budget=budget)
        assert result['semantic']['failed_pairs'] == 1
        before = copy.deepcopy(budget.snapshot())
        released.set()
        for _ in range(100):
            if completed.is_set(): break
            await asyncio.sleep(.001)
        assert completed.is_set()
        assert budget.snapshot() == before
        assert before['provider_usage']['status'] == 'unavailable'
    try: asyncio.run(run())
    finally: released.set()


def test_receipt_opt_in_preserves_legacy_json_and_deadline():
    model = client(lambda **_: response())
    assert model.json_chat([]) == {'relations':[]}
    assert isinstance(model.json_chat([],with_receipt=True),JSONCompletion)
    deadline = object.__new__(DeadlineLLMClient)
    deadline.__dict__.update(model.__dict__)
    deadline.deadline, deadline.call_timeout_s, deadline.timeout_count = time.monotonic()+1, .1, 0
    assert deadline.json_chat([],with_receipt=True).provider['completion_status'] == 'complete'
    deadline.deadline = time.monotonic()-1
    with pytest.raises(TimeoutError): deadline.json_chat([],with_receipt=True)


def test_provider_error_logging_omits_exception_body(caplog):
    def fail(**_): raise RuntimeError('private-provider-body-marker')
    with pytest.raises(RuntimeError): client(fail).json_chat([],with_receipt=True)
    assert 'private-provider-body-marker' not in caplog.text
