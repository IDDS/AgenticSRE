import asyncio
import copy
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from agents.alert_compression import CompressionStore, compress_alerts
from agents.alert_jobs import AlertJobs
from agents.alert_lifecycle import AlertLifecycle
from agents.alert_review import batches, messages
from agents.alert_review_context import ReviewContextGuard
from agents.alert_review_observations import ReviewObserver


def rows(at=1001,source='prometheus'):
    return [dict(name='Failure',message='refused',cluster_id='c',namespace='ns',pod=f'p-{i}',
                 source=source,timestamp=at,raw={'value':[at,'1']} if source=='prometheus'
                 else {'metadata':{'uid':f'event-{i}'}}) for i in range(2)]


def scan(at=1001,records=None):
    result = compress_alerts(rows(at) if records is None else records)
    result.update(origin='live_scan',cluster_id='c',scan_scope='ns',collection_started_at=at,
                  collection={'prometheus':'ok','kubectl':'ok'})
    ids = [g['group_id'] for g in result['groups']]
    result['candidate_pairs'] = [dict(left=ids[0],right=key) for key in ids[1:]]
    return result


def publish(lifecycle,at,records=None):
    data = scan(at,records)
    data.update(scan_id=f'fixture-{at}',revision=1,history=[])
    return lifecycle.observe(data)


@pytest.fixture
def setup(tmp_path,monkeypatch):
    import agents.alert_review_context as module
    clock = [1002.]
    monkeypatch.setattr(module.time,'time',lambda:clock[0])
    path = tmp_path/'lifecycle.sqlite3'
    lifecycle = AlertLifecycle(path)
    publish(lifecycle,1000)
    guard = ReviewContextGuard(path)
    result = scan()
    result['review_context'] = guard.capture(result,'live_scan')
    return lifecycle,guard,result,clock


def assessment(guard,result):
    batch = next(batches(result,65536))[1]
    return guard.assess(result,batch,'live_scan')


def test_projection_is_read_only_and_matches_stable_episode(setup):
    lifecycle,guard,result,_ = setup
    before = hashlib.sha256(lifecycle.path.read_bytes()).hexdigest()
    assert result['review_context']['status'] == 'verified'
    assert {m['episode'] for m in result['review_context']['members']} == {1}
    assert assessment(guard,result)['status'] == 'verified'
    assert guard.capture(result,'live_scan') == result['review_context']
    assert hashlib.sha256(lifecycle.path.read_bytes()).hexdigest() == before


def test_missing_database_does_not_create_files(tmp_path):
    guard = ReviewContextGuard(tmp_path/'missing'/'state.sqlite3')
    assert guard.capture(scan(),'live_scan')['reason'] == 'lifecycle_unavailable'
    assert not guard.path.parent.exists()


def test_projection_checks_all_members_in_a_large_repeated_group(setup):
    _,guard,_,_ = setup
    first,last = rows()
    records = [copy.deepcopy(first) for _ in range(1001)]+[last]
    result = scan(records=records)
    projection = guard.capture(result,'live_scan')
    assert projection['status'] == 'verified' and len(projection['members']) == 1002
    assert len({m['incident_id'] for m in projection['members']}) == 2
    assert len({m['alert_id'] for m in projection['members']}) == 1002
    records[-1]['name'] = 'NeverObservedFailure'
    assert guard.capture(scan(records=records),'live_scan')['reason'] == 'new_episode_unknown'


@pytest.mark.parametrize('change', ['import','origin','degraded','collection','scope','future_capture',
                                   'raw_changed','missing_projection','old_scan','new_entity'])
def test_untrusted_changed_or_unordered_snapshot_is_blocked(setup,change):
    _,guard,result,clock = setup
    source = 'live_scan'
    if change == 'import': source = 'import'
    if change == 'origin': result['origin'] = 'import'
    if change == 'degraded': result['degraded'] = True
    if change == 'collection': result['collection']['kubectl'] = 'truncated'
    if change == 'scope': result['scan_scope'] = 'other'
    if change == 'future_capture': clock[0] = 1001.5
    if change == 'raw_changed': result['raw_alerts'][0]['raw']['unseen_fact'] = 'changed'
    if change == 'missing_projection': result.pop('review_context')
    if change in ('old_scan','new_entity'):
        result = scan(1000 if change=='old_scan' else 1001,
                      [dict(r,name='NewFailure') for r in rows()] if change=='new_entity' else None)
        result['review_context'] = guard.capture(result,source)
    batch = next(batches(result,65536))[1]
    assert guard.assess(result,batch,source)['status'] == 'blocked'


def test_resolution_recurrence_and_pending_states_never_reuse_old_projection(setup):
    lifecycle,guard,result,clock = setup
    publish(lifecycle,1010,[])
    assert assessment(guard,result)['status'] == 'blocked'
    pending = scan(1011)
    assert guard.capture(pending,'live_scan')['reason'] == 'episode_unstable'
    publish(lifecycle,1040,[])
    reopening = scan(1041)
    assert guard.capture(reopening,'live_scan')['reason'] == 'episode_reopening'
    publish(lifecycle,1050)
    next_scan = scan(1051)
    next_scan['review_context'] = guard.capture(next_scan,'live_scan')
    clock[0] = 1052
    assert {m['episode'] for m in next_scan['review_context']['members']} == {2}
    assert assessment(guard,next_scan)['status'] == 'verified'
    assert next_scan['review_context']['episode_sha256'] != result['review_context']['episode_sha256']
    assert assessment(guard,result)['status'] == 'blocked'


def test_same_episode_concurrent_update_invalidates_snapshot_version(setup):
    lifecycle,guard,result,_ = setup
    # Update predates collection, but occurred after capture. Watermark order alone is insufficient.
    publish(lifecycle,1000.5)
    assert assessment(guard,result)['reason'] == 'lifecycle_version_changed'


@pytest.mark.parametrize('change,reason', [('missing_sample','missing_evidence_time'),
    ('stale_sample','evidence_expired'),('future_sample','future_evidence_time'),
    ('expired_snapshot','evidence_expired'),('future_snapshot','future_evidence_time')])
def test_live_collection_time_cannot_hide_stale_or_missing_source_sample(setup,change,reason):
    _,guard,result,clock = setup
    if change == 'missing_sample': result['raw_alerts'][0]['raw']['raw'] = {}
    if change == 'stale_sample': result['raw_alerts'][0]['raw']['raw']['value'][0] = 600
    if change == 'future_sample': result['raw_alerts'][0]['raw']['raw']['value'][0] = 1003
    if change == 'expired_snapshot': clock[0] = 1301
    if change == 'future_snapshot': result['collection_started_at'] = 1003
    result['review_context'] = guard.capture(result,'live_scan')
    assert assessment(guard,result)['reason'] == reason


def test_kubernetes_event_expiry_is_not_recovery_and_missing_source_times_block(setup):
    lifecycle,guard,_,clock = setup
    publish(lifecycle,1010,rows(1009,'k8s_event'))
    event_scan = scan(1011,rows(1011,'k8s_event'))
    clock[0] = 1012
    event_scan['review_context'] = guard.capture(event_scan,'live_scan')
    assert assessment(guard,event_scan)['status'] == 'verified'
    publish(lifecycle,1020,[])
    publish(lifecycle,1050,[])
    candidate = scan(1051,rows(1051,'k8s_event'))
    assert guard.capture(candidate,'live_scan')['reason'] == 'episode_reopening'
    assert all(s['business_recovery_verified'] is False for s in lifecycle.list('c'))


@pytest.mark.parametrize('kind', ['metric','composite_metric','trace','propagation','change'])
def test_used_context_timestamps_also_bound_freshness(setup,kind):
    _,guard,result,_ = setup
    batch = next(batches(result,65536))[1]
    if kind in ('metric','composite_metric'):
        metric = {'status':'ready','end':600,'summary':{'last':[600,1]}}
        batch[0]['metric_context']['groups'] = {'x':metric if kind=='metric' else {'status':'composite','measurements':[metric]}}
    else:
        ref = dict(at=600,provenance='live_kubectl' if kind=='change' else 'live_jaeger')
        context = batch[0]['observed_context']
        result['context_evidence'] = {'traces':{'status':'mapped','edges':[]}}
        if kind == 'propagation':
            ref.update(trace_id='t',span_id='s',parent_span_id='p')
            result['context_evidence']['traces']['edges'] = [ref]
            context['propagation_candidates'] = [{'trace_id':'t','span_links':[{'span_id':'s','parent_span_id':'p'}]}]
        else:
            context['change_refs' if kind=='change' else 'trace_refs'] = [ref]
    assert guard.freshness(result,batch)['reason'] == 'evidence_expired'


class Model:
    model, temperature = 'fixture-model', .1
    client = SimpleNamespace(base_url='https://fixture.invalid/v1')
    def __init__(self,action=None): self.action,self.calls = action,0
    async def async_json_chat(self,*args,**kwargs):
        self.calls += 1
        if self.action: self.action()
        return {'relations':[]}


def process(tmp_path,result,guard,model,source='live_scan'):
    store,output = AlertJobs(tmp_path/'jobs'),CompressionStore(tmp_path/'scans')
    key = store.create({'semantic':True,'source':source})['job_id']
    _,payload = store.claim()
    async def prepare(_): return copy.deepcopy(result)
    asyncio.run(store.process(key,payload,prepare,lambda _: (model,60),output,review_guard=guard))
    return store,key,output


@pytest.mark.parametrize('change', ['none','resolution','expiry','concurrent_observation'])
def test_before_and_after_checks_record_invalidation_without_losing_review(setup,tmp_path,change):
    lifecycle,guard,result,clock = setup
    def action():
        if change == 'resolution':
            publish(lifecycle,1010,[])
            publish(lifecycle,1040,[])
        if change == 'expiry': clock[0] = 1302
        if change == 'concurrent_observation': publish(lifecycle,1000.5)
    model = Model(action)
    store,key,output = process(tmp_path,result,guard,model)
    info = store.get(key)
    assert info['status'] == 'completed' and model.calls == 1
    assert output.get(info['scan_id'])['raw_alerts'] == result['raw_alerts']
    obs = info['review_observation']
    assert obs['outcomes'] == {('empty' if change=='none' else 'invalidated'):1}
    assert obs['context_verified_reviews'] == (1 if change=='none' else 0)
    assert obs['reuse_allowed'] is False and obs['reused_pairs'] == 0
    with store.connect() as db:
        row = db.execute('SELECT identity,match_key FROM review_observations').fetchone()
    identity = json.loads(row['identity'])
    if change != 'none': assert row['match_key'] is None and 'review_context_changed' in identity['reuse_blockers']
    else: assert 'episode_unverified' not in identity['reuse_blockers']


def test_import_cannot_claim_live_guard_and_failure_keeps_raw_evidence(setup,tmp_path):
    _,guard,result,_ = setup
    store,key,output = process(tmp_path,result,guard,Model(),source='import')
    saved = store.prepared(key)
    assert saved['review_context']['reason'] == 'not_complete_live_observation'
    assert store.get(key)['review_observation']['context_verified_reviews'] == 0
    assert output.get(store.get(key)['scan_id'])['retained_alerts'] == 2


def test_retry_keeps_absolute_snapshot_and_does_not_rebind_new_lifecycle(setup,tmp_path):
    lifecycle,guard,result,_ = setup
    class Failing(Model):
        async def async_json_chat(self,*args,**kwargs): raise TimeoutError('fixture')
    store,key,output = process(tmp_path,result,guard,Failing())
    original = store.prepared(key)['review_context']
    publish(lifecycle,1000.5)
    store.control(key,'resume')
    _,payload = store.claim()
    async def forbidden(_): raise AssertionError('must retain prepared projection')
    model = Model()
    asyncio.run(store.process(key,payload,forbidden,lambda _: (model,60),output,review_guard=guard))
    assert store.prepared(key)['review_context'] == original
    assert model.calls == 1 and store.get(key)['status'] == 'completed'
    assert store.get(key)['review_observation']['context_verified_reviews'] == 0


def test_matching_live_requests_require_same_verified_episode(setup,tmp_path):
    _,guard,result,_ = setup
    store = AlertJobs(tmp_path/'jobs')
    batch = next(batches(result,65536))[1]
    for _ in range(2):
        key = store.create({})['job_id']
        observer = ReviewObserver(store,key,result,Model(),guard=guard,source='live_scan')
        receipt = observer.begin(0,batch,messages(batch),2048)
        observer.finish(receipt,{'semantic':{'status':'ok'},'relations':[]},0)
    assert store.get(key)['review_observation']['prior_empty_requests'] == 1
    assert store.get(key)['review_observation']['context_verified_reviews'] == 1


def test_identical_request_bytes_across_recurrence_do_not_match_old_review(setup,tmp_path):
    lifecycle,guard,result,clock = setup
    store = AlertJobs(tmp_path/'jobs')
    batch = next(batches(result,65536))[1]
    first = ReviewObserver(store,store.create({})['job_id'],result,Model(),guard=guard,source='live_scan')
    receipt = first.begin(0,batch,messages(batch),2048)
    first.finish(receipt,{'semantic':{'status':'ok'},'relations':[]},0)
    publish(lifecycle,1010,[])
    publish(lifecycle,1040,[])
    publish(lifecycle,1050,rows(1001))
    clock[0] = 1052
    repeated = scan(1051,rows(1001))
    repeated['review_context'] = guard.capture(repeated,'live_scan')
    new_batch = next(batches(repeated,65536))[1]
    assert messages(new_batch) == messages(batch)
    key = store.create({})['job_id']
    observer = ReviewObserver(store,key,repeated,Model(),guard=guard,source='live_scan')
    receipt = observer.begin(0,new_batch,messages(new_batch),2048)
    observer.finish(receipt,{'semantic':{'status':'ok'},'relations':[]},0)
    with store.connect() as db:
        identities = [json.loads(row[0]) for row in db.execute('SELECT identity FROM review_observations ORDER BY rowid')]
    assert identities[0]['messages_sha256'] == identities[1]['messages_sha256']
    assert identities[0]['evidence_sha256'] == identities[1]['evidence_sha256']
    assert identities[0]['episode_sha256'] != identities[1]['episode_sha256']
    assert store.get(key)['review_observation']['prior_empty_requests'] == 0
