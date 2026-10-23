"""A review job belongs to its published partition, not a later manual edit."""
import asyncio
import pytest

from agents.alert_compression import CompressionStore
from agents.alert_jobs import AlertJobs
from tests.test_alert_review_resume import fixture,Model


def partial(tmp_path):
    output=CompressionStore(tmp_path/'scans')
    jobs=AlertJobs(tmp_path/'jobs')
    key=jobs.create({'semantic':True})['job_id']
    _,payload=jobs.claim()
    async def prepare(_):return fixture(1)
    asyncio.run(jobs.process(key,payload,prepare,lambda _:(Model(fail=True),60),output))
    return jobs,output,key


def edit(output,key):
    scan=output.get('scan-'+key[4:])
    return output.edit(scan['scan_id'],revision=scan['revision'],action='merge',
        group_ids=[g['group_id'] for g in scan['groups']],reason='controlled manual regrouping')


def test_edited_result_is_not_advertised_as_resumable(tmp_path):
    jobs,output,key=partial(tmp_path)
    assert jobs.get(key)['can_resume'] is True
    edit(output,key)
    info=jobs.get(key)
    assert info['can_resume'] is False and info['resume_blocked_reason']=='result_edited'
    assert info['review_revision']==1 and info['result_revision']==2
    assert info['review_applies_to_result'] is False and info['result_state']=='edited'
    assert 'resume_action' not in info
    with pytest.raises(ValueError,match='result_edited'):jobs.control(key,'resume')


def test_restart_recovers_edit_from_scan_without_an_extra_sql_update(tmp_path):
    jobs,output,key=partial(tmp_path)
    before=jobs.get(key)
    edited=edit(output,key)
    restored=AlertJobs(tmp_path/'jobs',output=output).get(key)
    assert restored['result_state']=='edited' and not restored['can_resume']
    for field in ('model_calls','failed_pairs','completed_pairs','budget'):
        assert restored[field]==before[field]
    assert output.get(edited['scan_id'])==edited


def test_resume_waits_for_edit_commit_then_rejects_stale_work(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    jobs,output,key=partial(tmp_path)
    entered,release,resuming,assessing=(threading.Event() for _ in range(4))
    original=output._write
    def pause(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)
    monkeypatch.setattr(output,'_write',pause)
    original_get=jobs.get
    def tracked_get(*args):
        assessing.set()
        return original_get(*args)
    monkeypatch.setattr(jobs,'get',tracked_get)
    def resume():
        resuming.set()
        return jobs.control(key,'resume')
    with ThreadPoolExecutor(max_workers=2) as pool:
        editing=pool.submit(edit,output,key)
        try:
            assert entered.wait(3)
            retry=pool.submit(resume)
            assert resuming.wait(3)
            assert not assessing.wait(.1),'resume read the old version while edit still held its lock'
            # Signal the writer to commit; the retry must assess that version.
            release.set()
            edited=editing.result(timeout=3)
            with pytest.raises(ValueError,match='result_edited'):retry.result(timeout=3)
        finally:release.set()
    assert jobs.get(key)['status']=='partial'
    assert output.get(edited['scan_id'])==edited


def test_api_binds_current_result_and_rejects_resume_without_starting_worker(tmp_path,monkeypatch):
    import web_app.app as app
    from fastapi.testclient import TestClient
    from unittest.mock import AsyncMock
    monkeypatch.setattr(app,'ROOT_DIR',tmp_path)
    output=app._compression_store()
    jobs=app._alert_jobs()
    assert jobs.output.directory==output.directory
    key=jobs.create({'semantic':True})['job_id']
    _,payload=jobs.claim()
    async def prepare(_):return fixture(1)
    asyncio.run(jobs.process(key,payload,prepare,lambda _:(Model(fail=True),30),output))
    edit(output,key)
    worker=AsyncMock()
    monkeypatch.setattr(app,'_ensure_alert_job_worker',worker)
    client=TestClient(app.app)
    info=client.get('/api/alerts/compression-jobs/'+key).json()
    assert info['can_resume'] is False and info['result_state']=='edited'
    assert info['review_revision']==1 and info['result_revision']==2
    response=client.post('/api/alerts/compression-jobs/'+key+'/resume',json={})
    assert response.status_code==409 and 'result_edited' in response.json()['detail']
    worker.assert_not_called()


@pytest.mark.parametrize('mode',['missing','invalid_json','wrong_scan','revision_changed'])
def test_unverifiable_result_cannot_be_resumed(tmp_path,mode):
    jobs,output,key=partial(tmp_path)
    path=output._path('scan-'+key[4:])
    if mode=='missing':path.rename(path.with_suffix('.withheld'))
    elif mode=='invalid_json':path.write_text('{')
    else:
        data=output.get('scan-'+key[4:])
        data['scan_id']='scan-'+'f'*32 if mode=='wrong_scan' else data['scan_id']
        if mode=='revision_changed':data['revision']+=1
        output._write(path,data)
    info=jobs.get(key)
    expected='result_revision_changed' if mode=='revision_changed' else 'result_unavailable'
    assert not info['can_resume'] and info['resume_blocked_reason']==expected
    with pytest.raises(ValueError,match=expected):jobs.control(key,'resume')


def test_edit_during_inflight_review_prevents_later_batches_and_keeps_receipts(tmp_path):
    import httpx
    from openai import APIStatusError
    output=CompressionStore(tmp_path/'scans')
    jobs=AlertJobs(tmp_path/'jobs',output=output)
    key=jobs.create({'semantic':True})['job_id']
    _,payload=jobs.claim()
    async def prepare(_):return fixture(45)
    class Refusal:
        async def async_json_chat(self,*args,**kwargs):
            raise APIStatusError('controlled',response=httpx.Response(402,
                request=httpx.Request('POST','https://provider.invalid/')),body={})
    asyncio.run(jobs.process(key,payload,prepare,lambda _:(Refusal(),30),output))
    jobs.control(key,'resume');_,payload=jobs.claim()
    edited=[]
    class EditingModel(Model):
        async def async_json_chat(self,*args,**kwargs):
            # Keep two selected groups while the original review is in flight.
            scan=output.get('scan-'+key[4:])
            edited.append(output.edit(scan['scan_id'],revision=1,action='merge',
                group_ids=[g['group_id'] for g in scan['groups'][:2]],reason='edit during review'))
            return await super().async_json_chat(*args,**kwargs)
    model=EditingModel()
    async def forbidden(_):raise AssertionError('must reuse prepared snapshot')
    asyncio.run(jobs.process(key,payload,forbidden,lambda _:(model,30),output))
    info=jobs.get(key)
    assert len(model.calls)==1 and info['status']=='cancelled'
    assert info['model_calls']==2 and info['completed_pairs']==20
    assert info['result_state']=='edited' and info['review_applies_to_result'] is False
    assert not info['can_resume'] and info['error']=='result_edited'
    assert output.get(edited[0]['scan_id'])==edited[0]
