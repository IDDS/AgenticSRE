import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from agents.alert_compression import CompressionStore, compress_alerts
from agents.alert_jobs import AlertJobs, scan_page, scan_summary


def fixture_result(n=45):
    result = compress_alerts([dict(name='Failure', message='connection refused', timestamp=1000,
        cluster_id='c', namespace='ns', pod=f'p-{i}') for i in range(n+1)])
    ids = [g['group_id'] for g in result['groups']]
    result['candidate_pairs'] = [dict(left=ids[0], right=key) for key in ids[1:]]
    return result


class Model:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    async def async_json_chat(self, messages, **kwargs):
        pairs = json.loads(messages[-1]['content'])
        self.calls.append(pairs)
        if self.fail == len(self.calls):
            raise TimeoutError('fixture')
        return {'relations': []}


def test_partial_resume_skips_successful_batches_and_counts_attempts(tmp_path):
    store = AlertJobs(tmp_path/'jobs')
    output = CompressionStore(tmp_path/'scans')
    result = fixture_result()
    async def prepare(_):
        return copy.deepcopy(result)
    key = store.create({'semantic': True})['job_id']
    job, payload = store.claim()
    model = Model(fail=2)
    asyncio.run(store.process(job, payload, prepare, lambda _: (model, 1), output))
    assert store.get(key)['status'] == 'partial'
    assert store.get(key)['completed_pairs'] == 25
    assert store.get(key)['failed_pairs'] == 20
    partial = store.get(key)
    assert output.get(partial['scan_id'])['semantic']['failed_pairs'] == 20
    assert partial['result_url'].endswith('/summary')
    store = AlertJobs(tmp_path/'jobs')
    store.control(key, 'resume')
    job, payload = store.claim()
    retried = Model()
    async def no_reprepare(_):
        raise AssertionError('prepared input must survive restart')
    asyncio.run(store.process(job, payload, no_reprepare, lambda _: (retried, 1), output))
    info = store.get(key)
    assert info['status'] == 'completed'
    assert info['completed_pairs'] == 45
    assert info['model_calls'] == 4
    assert len(retried.calls) == 1 and len(retried.calls[0]) == 20
    saved = output.get(info['scan_id'])
    assert saved['raw_alerts'] == result['raw_alerts']
    assert saved['semantic']['model_calls'] == 4
    assert saved['semantic']['coverage_complete']
    with pytest.raises(ValueError):
        store.control(key, 'resume')


def test_cancel_during_batch_does_not_start_next_batch(tmp_path):
    store = AlertJobs(tmp_path/'jobs')
    key = store.create({'semantic': True})['job_id']
    job, payload = store.claim()
    async def prepare(_):
        return fixture_result()
    class CancelModel(Model):
        async def async_json_chat(self, *a, **kw):
            store.control(key, 'cancel')
            return await super().async_json_chat(*a, **kw)
    model = CancelModel()
    asyncio.run(store.process(job, payload, prepare, lambda _: (model, 1), CompressionStore(tmp_path/'scans')))
    assert len(model.calls) == 1
    assert store.get(key)['status'] == 'cancelled'
    assert store.get(key)['completed_pairs'] == 20


def test_restart_requeues_running_job_and_worker_lock_prevents_duplicates(tmp_path):
    async def scenario():
        store = AlertJobs(tmp_path/'jobs')
        key = store.create({'semantic': False})['job_id']
        store.claim()  # Simulate a process dying after claim.
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def prepare(_):
            calls.append(1)
            entered.set()
            await release.wait()
            return fixture_result(1)
        output = CompressionStore(tmp_path/'scans')
        first = asyncio.create_task(store.run(prepare, lambda _: (None, 1), output))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            await asyncio.wait_for(store.run(prepare, lambda _: (None, 1), output), 1)
            assert calls == [1]
            release.set()
            for _ in range(100):
                if store.get(key)['status'] == 'completed':
                    break
                await asyncio.sleep(.01)
            assert store.get(key)['status'] == 'completed'
        finally:
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
    asyncio.run(scenario())


def test_pagination_covers_tail_without_changing_snapshot():
    result = fixture_result(450)
    result.update(scan_id='scan-'+'a'*32, revision=3)
    original = copy.deepcopy(result)
    all_pairs = []
    offset = 0
    while offset is not None:
        page = scan_page(result, 'candidate_pairs', offset, 200)
        all_pairs.extend(page['items'])
        assert page['revision'] == 3 and page['total'] == 450
        offset = page['next_offset']
    assert all_pairs == result['candidate_pairs']
    summary = scan_summary(result)
    assert 'raw_alerts' not in summary and summary['counts']['candidate_pairs'] == 450
    assert result == original
    for section, start, limit in [('bad',0,1),('groups',-1,10),('groups',0,201),('groups',0,0)]:
        with pytest.raises(ValueError):
            scan_page(result, section, start, limit)


def test_invalid_job_and_failed_preparation_are_explicit(tmp_path):
    store = AlertJobs(tmp_path)
    with pytest.raises(ValueError):
        store.get('../jobs')
    with pytest.raises(FileNotFoundError):
        store.get('job-'+'a'*32)
    key = store.create({})['job_id']
    async def scenario():
        async def fail(_):
            raise RuntimeError('fixture')
        worker = asyncio.create_task(store.run(fail, lambda _: (None, 1), CompressionStore(tmp_path/'scans')))
        try:
            for _ in range(100):
                if store.get(key)['status'] == 'failed':
                    break
                await asyncio.sleep(.01)
            assert store.get(key)['status'] == 'failed'
            assert store.get(key)['error'] == 'RuntimeError'
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(scenario())


def test_recovery_repairs_missing_total_after_prepared_file_write(tmp_path):
    store = AlertJobs(tmp_path/'jobs')
    output = CompressionStore(tmp_path/'scans')
    key = store.create({'semantic': False})['job_id']
    job, payload = store.claim()
    CompressionStore(store.directory)._write(store.directory/(key+'.json'), fixture_result(1))
    assert store.get(key)['candidate_pairs'] is None
    async def forbidden(_):
        raise AssertionError('must reuse persisted preparation')
    asyncio.run(store.process(job, payload, forbidden, lambda _: (None, 1), output))
    assert store.get(key)['status'] == 'completed'
    assert store.get(key)['candidate_pairs'] == 1
