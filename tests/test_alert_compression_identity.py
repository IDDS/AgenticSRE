"""Incarnation isolation and edit invalidation are lossless compression contracts."""
import copy
import pytest

from agents.alert_compression import CompressionStore, compress_alerts


def event(uid, **changes):
    return dict(dict(name='BackOff',source='k8s_event',timestamp=1000,
        cluster_id='c',namespace='ns',labels={'kind':'Pod','name':'api','namespace':'ns'},
        message='Back-off restarting container',raw={'metadata':{'uid':'event-'+uid},
        'involvedObject':{'kind':'Pod','name':'api','namespace':'ns','uid':uid}}),**changes)


def test_same_name_recreated_pod_is_not_one_compressed_observation():
    records=[event('old'),event('new')]
    result=compress_alerts(records)
    assert result['compressed_groups']==2
    assert [r['raw'] for r in result['raw_alerts']]==records


def test_manual_edit_cannot_reuse_prior_model_review_for_new_membership(tmp_path):
    store=CompressionStore(tmp_path)
    scan=compress_alerts([event('one',timestamp=1000+i) for i in range(2)])
    scan['semantic']={'status':'ok','coverage_complete':True,'completed_pairs':12,
        'model_calls':2,'batches':[{'status':'ok','offset':0,'pairs':12}]}
    scan=store.create(scan)
    edited=store.edit(scan['scan_id'],revision=1,action='split',
        group_ids=[scan['groups'][0]['group_id']],alert_ids=[scan['raw_alerts'][0]['alert_id']],reason='review split')
    assert edited['semantic']['status']=='invalidated_by_edit'
    assert edited['semantic']['coverage_complete'] is False
    assert 'batches' not in edited['semantic']
    dossier=store.dossier(edited['scan_id'],edited['groups'][0]['group_id'],edited['revision'])
    assert dossier['semantic_review']['status']=='invalidated_by_edit'


@pytest.mark.parametrize('field',['entity_uid','pod_uid','object_uid','k8s.pod.uid'])
def test_explicit_target_uid_is_not_lost_when_input_is_normalized(field):
    rows=[event('one'),event('one')]
    for index,row in enumerate(rows):
        row['raw'].pop('involvedObject')
        row[field]=str(index)
    result=compress_alerts(rows)
    assert result['compressed_groups']==2
    assert {g['entity_uid'] for g in result['groups']}=={'0','1'}


def test_event_record_uid_is_not_mislabelled_as_target_uid():
    rows=[event('one'),event('two')]
    for row in rows:
        row['raw'].pop('involvedObject')
    result=compress_alerts(rows)
    assert result['compressed_groups']==1
    assert result['groups'][0]['identity_status']=='uid_not_reported'


def test_known_and_unknown_uid_do_not_implicitly_join_the_same_incarnation():
    rows=[event('one'),event('one')]
    rows[1]['raw']['involvedObject'].pop('uid')
    assert compress_alerts(rows)['compressed_groups']==2


def test_conflicting_identity_is_retained_as_singletons_instead_of_aborting_scan():
    rows=[event('one',entity_uid='two') for _ in range(2)]
    result=compress_alerts(rows)
    assert result['compressed_groups']==2
    assert all(g['identity_status']=='conflicting' for g in result['groups'])
    assert [r['raw'] for r in result['raw_alerts']]==rows


def test_manual_merge_rejects_different_incarnations_without_mutating_persisted_scan(tmp_path):
    store=CompressionStore(tmp_path)
    scan=store.create(compress_alerts([event('old'),event('new')]))
    with pytest.raises(ValueError,match='incarnations'):
        store.edit(scan['scan_id'],revision=1,action='merge',group_ids=[g['group_id'] for g in scan['groups']],reason='same name')
    assert store.get(scan['scan_id'])==scan


def test_dossier_original_pages_and_hydration_preserve_target_identity(tmp_path):
    store=CompressionStore(tmp_path)
    scan=store.create(compress_alerts([event('pod-one',timestamp=1000+i) for i in range(3)]))
    gid=scan['groups'][0]['group_id']
    dossier=store.dossier(scan['scan_id'],gid,1,for_diagnosis=True)
    assert dossier['entity_uid']=='pod-one'
    assert all(s['entity_uid']=='pod-one' for s in dossier['alert_samples'])
    page=store.evidence_page(scan['scan_id'],gid,1)
    assert len(page['records'])==3 and all(r['entity_uid']=='pod-one' for r in page['records'])
    assert scan['integrity']['partition_members']==scan['integrity']['original_alerts']==3


@pytest.mark.parametrize('mutation',['duplicate_member','bool_index','missing_id','wrong_count','lower_severity','duplicate_group'])
def test_invalid_partition_is_rejected_before_publication(tmp_path,mutation):
    result=compress_alerts([event('one',severity='critical'),event('one')])
    group=result['groups'][0]
    if mutation=='duplicate_member':group['alert_indices']=[0,0]
    if mutation=='bool_index':group['alert_indices']=[False,True]
    if mutation=='missing_id':group['alert_ids'].pop()
    if mutation=='wrong_count':group['alert_count']=99
    if mutation=='lower_severity':group['severity']='warning'
    if mutation=='duplicate_group':result['groups'].append(copy.deepcopy(group))
    store=CompressionStore(tmp_path)
    with pytest.raises(ValueError):store.create(result)
    assert not list(tmp_path.glob('scan-*.json'))


def test_edit_receipt_keeps_raw_hash_and_changes_partition_hash(tmp_path):
    store=CompressionStore(tmp_path)
    scan=store.create(compress_alerts([event('one',timestamp=1000+i) for i in range(3)]))
    edited=store.edit(scan['scan_id'],revision=1,action='split',group_ids=[scan['groups'][0]['group_id']],
        alert_ids=[scan['raw_alerts'][0]['alert_id']],reason='controlled split')
    assert scan['integrity']['raw_sha256']==edited['integrity']['raw_sha256']
    assert scan['integrity']['partition_sha256']!=edited['integrity']['partition_sha256']
    assert edited['raw_alerts']==scan['raw_alerts']


def test_threshold_auto_merge_cannot_join_different_target_uids():
    from tests.test_alert_thresholds import fixture
    from agents.alert_thresholds import merge_nested_thresholds
    data=fixture()
    for i,row in enumerate(data['raw_alerts']):row['raw']['entity_uid']='target-'+str(i)
    original=copy.deepcopy(data)
    assert not merge_nested_thresholds(data)
    assert data==original


def test_frequency_and_lifecycle_signatures_separate_target_incarnations():
    from agents.alert_semantics import frequency_key
    from agents.alert_lifecycle import incident_id
    data=compress_alerts([dict(event(uid),source='logs',entity_uid=uid) for uid in ('old','new')])
    first,second=data['raw_alerts']
    assert frequency_key(first)!=frequency_key(second)
    assert incident_id(first)!=incident_id(second)


def test_async_publication_rejects_corrupt_partition_and_preserves_existing_file(tmp_path):
    import asyncio
    from agents.alert_jobs import AlertJobs
    jobs,store=AlertJobs(tmp_path/'jobs'),CompressionStore(tmp_path/'scans')
    key=jobs.create({'semantic':False})['job_id']
    _,payload=jobs.claim()
    async def prepare(_):
        return compress_alerts([event('one')])
    asyncio.run(jobs.process(key,payload,prepare,lambda _:(None,1),store))
    published=store.get('scan-'+key[4:])
    invalid=copy.deepcopy(published)
    invalid['groups'][0]['alert_ids']=['foreign']
    with pytest.raises(ValueError):jobs.publish(key,store,invalid,1)
    assert store.get(published['scan_id'])==published


def test_partition_contract_scales_to_ten_thousand_records_without_loss():
    data=compress_alerts([event('stable',timestamp=1000+i%200,severity='critical' if i==9999 else 'warning') for i in range(10000)])
    assert data['total_alerts']==data['retained_alerts']==10000 and data['compressed_groups']==1
    assert data['groups'][0]['severity']=='critical'
    assert data['integrity']['partition_members']==10000


def test_scag_cannot_verify_same_name_different_incarnation_by_name_only(monkeypatch):
    import agents.alert_semantics as semantics
    data=compress_alerts([event('old'),event('new')])
    a,b=data['groups']
    data['relations']=[{'left':a['group_id'],'right':b['group_id'],'type':'equivalent'}]
    monkeypatch.setattr(semantics,'statistical_check',lambda *args:{'status':'pass'})
    semantics.build_scag(data)
    edge=data['scag']['edges'][0]
    assert edge['checks']['structure']['status']=='unknown'
    assert edge['status']=='unverified' and not edge['causal_confirmed']


@pytest.mark.parametrize('status',[401,402,403,404])
def test_terminal_provider_refusal_stops_further_batches_without_losing_pending_pairs(status):
    import asyncio
    import httpx
    from openai import APIStatusError
    from tests.test_alert_jobs import fixture_result
    from agents.alert_compression import propose_relations
    calls=[]
    class Model:
        async def async_json_chat(self,*args,**kwargs):
            calls.append(True)
            raise APIStatusError('sensitive-provider-body',response=httpx.Response(status,
                request=httpx.Request('POST','https://provider.invalid/')),body={})
    data=fixture_result(45)
    asyncio.run(propose_relations(data,Model()))
    report=data['semantic']
    assert len(calls)==1
    assert report['failed_pairs']==20 and report['deferred_pairs']==25
    assert report['provider_failure']['status_code']==status
    assert report['completed_pairs']==0 and not report['coverage_complete']
    assert report['budget']['model_calls']==1 and not report['budget']['exhausted_reason']
    assert len(data['raw_alerts'])==data['retained_alerts']
    assert 'sensitive-provider-body' not in str(report)


def test_explicit_resume_after_provider_repair_reuses_original_checkpoint_and_budget(tmp_path):
    import asyncio
    import httpx
    from openai import APIStatusError
    from tests.test_alert_jobs import fixture_result,Model
    from agents.alert_jobs import AlertJobs
    jobs,store=AlertJobs(tmp_path/'jobs'),CompressionStore(tmp_path/'scans')
    key=jobs.create({'semantic':True})['job_id']
    _,payload=jobs.claim()
    async def prepare(_):return fixture_result(45)
    class Refusal:
        async def async_json_chat(self,*args,**kwargs):
            raise APIStatusError('sensitive-provider-body',response=httpx.Response(402,
                request=httpx.Request('POST','https://provider.invalid/')),body={})
    asyncio.run(jobs.process(key,payload,prepare,lambda _:(Refusal(),30),store))
    partial=store.get('scan-'+key[4:])
    assert jobs.get(key)['model_calls']==1 and jobs.get(key)['can_resume']
    jobs.control(key,'resume');_,payload=jobs.claim()
    async def forbidden(_):raise AssertionError('must reuse the prepared original observations')
    repaired=Model()
    asyncio.run(jobs.process(key,payload,forbidden,lambda _:(repaired,30),store))
    final=store.get('scan-'+key[4:])
    assert jobs.get(key)['status']=='completed' and jobs.get(key)['model_calls']==4
    assert len(repaired.calls)==3 and final['semantic']['completed_pairs']==45
    assert final['integrity']['raw_sha256']==partial['integrity']['raw_sha256']
    assert final['raw_alerts']==partial['raw_alerts']
    assert final['revision']==2 and 'provider_failure' not in final['semantic']
