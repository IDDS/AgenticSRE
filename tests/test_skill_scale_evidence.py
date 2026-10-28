"""Typed observations qualify further review, never an execution grant."""
import copy
import pytest

from orchestrator.skill_scale_evidence import assess_scale_zero_evidence


@pytest.fixture
def sample():
    root = {'cluster_id':'east', 'namespace':'shop', 'kind':'Deployment', 'name':'engine',
            'uid':'deploy-uid', 'resource_version':'v1'}
    scope = {'status':'collected', 'gaps':[], 'candidate_workloads':['engine'],
             'selected_pod_count':0, 'relation':'matches_workload_template'}
    receipt = {'schema_version':1, 'source':'provider_response', 'completion_status':'complete',
               'finish_reason':'stop', 'refusal_present':False, 'tool_call_present':False}
    def fact(number, kind, name, uid, spec=None, **extra):
        return {'id':'E'+str(number), 'timestamp':'2026-10-28T01:00:00+00:00',
                'raw_ref':{'kind':kind, 'cluster_id':'east', 'topology_fields_complete':True,
                    'metadata':{'namespace':'shop','name':name,'uid':uid,'resourceVersion':'v1'},
                    'spec':spec or {}, **extra}}
    facts = [fact(1,'Service','gateway','service-uid',{'selector':{'app':'engine'}}),
             fact(2,'Endpoints','gateway','endpoints-uid',subsets=[]),
             fact(3,'Deployment','engine','deploy-uid',{'replicas':0,
                 'template':{'metadata':{'labels':{'app':'engine'}}}})]
    judge = {'needs_review':False,'review_blockers':[], 'skill_review':{'provider_receipt':receipt},
             'llm_based':{'supported_evidence_ids':['E1','E2','E3']},'review_evidence':facts}
    return {'root_entity':root,'service':'gateway','scope':scope,'judge':judge}


def test_cited_structured_zero_state_only_qualifies_further_review(sample):
    result = assess_scale_zero_evidence(**sample)
    assert result['eligible_for_further_review'] is True
    assert result['execution_authorized'] is False and result['is_causal_proof'] is False
    assert result['cited_evidence_ids'] == {'Service':'E1','Endpoints':'E2','Deployment':'E3'}


@pytest.mark.parametrize('mutation', ['review','receipt','receipt_bool','scope','pod_count','ambiguous',
    'missing_citation','replacement','version','replicas','bool_replicas','selector','ready_endpoint',
    'missing_timestamp','other_cluster','malformed_spec','multiple_matching'])
def test_incomplete_or_conflicting_evidence_never_qualifies(sample, mutation):
    value=copy.deepcopy(sample)
    scope, judge = value['scope'], value['judge']
    svc, endpoints, deploy = [f['raw_ref'] for f in judge['review_evidence']]
    if mutation == 'review': judge['needs_review']=True
    if mutation == 'receipt': judge['skill_review']['provider_receipt']=None
    if mutation == 'receipt_bool': judge['skill_review']['provider_receipt']['schema_version']=True
    if mutation == 'scope': scope['gaps']=['collection_incomplete']
    if mutation == 'pod_count': scope['selected_pod_count']=False
    if mutation == 'ambiguous': scope['candidate_workloads'].append('other')
    if mutation == 'missing_citation': judge['llm_based']['supported_evidence_ids'].remove('E2')
    if mutation == 'replacement': deploy['metadata']['uid']='other'
    if mutation == 'version': deploy['metadata']['resourceVersion']='v2'
    if mutation == 'replicas': deploy['spec']['replicas']=1
    if mutation == 'bool_replicas': deploy['spec']['replicas']=False
    if mutation == 'selector': svc['spec']['selector']={'app':'another'}
    if mutation == 'ready_endpoint': endpoints['subsets']=[{'addresses':[{'ip':'192.0.2.1'}]}]
    if mutation == 'missing_timestamp': judge['review_evidence'][0]['timestamp']=''
    if mutation == 'other_cluster': svc['cluster_id']='west'
    if mutation == 'malformed_spec': deploy['spec']=['unexpected']
    if mutation == 'multiple_matching':
        extra=copy.deepcopy(judge['review_evidence'][2]);extra['id']='E4'
        extra['raw_ref']['metadata']['uid']='other-incarnation'
        judge['review_evidence'].append(extra);judge['llm_based']['supported_evidence_ids'].append('E4')
    result=assess_scale_zero_evidence(**value)
    assert not result['eligible_for_further_review'] and result['blockers']
    assert result['execution_authorized'] is False


def test_equivalent_cited_reads_keep_all_provenance_without_authorizing_execution(sample):
    value=copy.deepcopy(sample)
    for i, fact in enumerate(value['judge']['review_evidence'][:], 4):
        duplicate=copy.deepcopy(fact)
        duplicate.update(id='E'+str(i), timestamp='2026-10-28T01:00:01+00:00', source='other projection')
        duplicate['raw_ref']['metadata']['annotations']={'unrelated':'metadata'}
        value['judge']['review_evidence'].append(duplicate)
        value['judge']['llm_based']['supported_evidence_ids'].append(duplicate['id'])
    result=assess_scale_zero_evidence(**value)
    assert result['eligible_for_further_review'] is True
    assert result['execution_authorized'] is False and result['is_causal_proof'] is False
    assert result['cited_evidence_ids']=={'Service':'E1','Endpoints':'E2','Deployment':'E3'}
    assert result['equivalent_cited_evidence_ids']=={
        'Service':['E1','E4'],'Endpoints':['E2','E5'],'Deployment':['E3','E6']}


@pytest.mark.parametrize('mutation', ['uid','version','replicas','labels','selector','status',
                                      'subsets','deletion','timestamp','duplicate_id','partial'])
def test_conflicting_cited_reads_remain_blocked(sample, mutation):
    value=copy.deepcopy(sample)
    index=0 if mutation=='selector' else 1 if mutation=='subsets' else 2
    duplicate=copy.deepcopy(value['judge']['review_evidence'][index]); duplicate['id']='E4'
    raw=duplicate['raw_ref']; meta=raw['metadata']
    if mutation=='uid': meta['uid']='another'
    if mutation=='version': meta['resourceVersion']='v2'
    if mutation=='replicas': raw['spec']['replicas']=1
    if mutation=='labels': raw['spec']['template']['metadata']['labels']['app']='other'
    if mutation=='selector': raw['spec']['selector']['app']='other'
    if mutation=='status': raw['status']={'readyReplicas':1}
    if mutation=='subsets': raw['subsets']=[{'addresses':[{'ip':'192.0.2.1'}]}]
    if mutation=='deletion': meta['deletionTimestamp']='2026-10-28T01:00:01Z'
    if mutation=='timestamp': duplicate['timestamp']=''
    if mutation=='duplicate_id': duplicate['id']=value['judge']['review_evidence'][0]['id']
    if mutation=='partial': raw['spec'].pop('template')
    value['judge']['review_evidence'].append(duplicate)
    value['judge']['llm_based']['supported_evidence_ids'].append(duplicate['id'])
    result=assess_scale_zero_evidence(**value)
    assert not result['eligible_for_further_review']
    assert 'cited_objects_incomplete_or_ambiguous' in result['blockers']
