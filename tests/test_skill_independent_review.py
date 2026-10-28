"""Independent Skill review contracts; synthetic fixtures are not live faults."""
import copy
import json
from pathlib import Path

import pytest

from orchestrator.skill_agent import SkillAgent, SkillAgentRequest, SkillAgentResult
from skill_runtime.service_evidence import PROJECTION, projection_command

AT = '2026-10-27T02:00:00+00:00'
ROOT = Path(__file__).parents[1]


def commands():
    rows = [
        ('Service', 'gateway', {'spec': {'selector': {'backend': 'engine'}}}),
        ('Endpoints', 'gateway', {'subsets': []}),
        ('Deployment', 'engine', {'spec': {'replicas': 0, 'template': {'metadata': {
            'labels': {'backend': 'engine'}}}}, 'status': {}}),
    ]
    return [{'command': projection_command(kind, 'shop', name), 'namespace': 'shop', 'cluster_id': 'east',
        'success': True, 'executed': True, 'timestamp': AT, 'data': json.dumps({
            'kind': kind, 'projection': PROJECTION, 'observed_at': AT,
            'metadata': {'namespace': 'shop', 'name': name, 'uid': name+'-'+kind+'-uid',
                         'resourceVersion': 'opaque-a'}, **extra})}
        for kind, name, extra in rows]


def review_answer(**overrides):
    result = dict(judge_level=0, coherence_score=.9, evidence_score=.9,
        specificity_score=.9, actionability_score=.9, supported_evidence_ids=['E1','E2','E3'],
        unsupported_claims=[], contradictions=[], feedback='对象范围内的当前状态支持有限结论',
        hypothesis_reviews=[])
    result.update(overrides)
    return result


def reason(review=None, *, records=None, scope=None, root=None, hypotheses=None, review_client=None, review_timeout=.2, seeded_events=None):
    agent = SkillAgent(registry=None, skills_dir=ROOT/'skill_runtime/skills',
                       llm_client=review_client, llm_timeout_s=review_timeout if review_client else None)
    bounded_call = agent._call_llm_json
    agent.events.extend(copy.deepcopy(seeded_events or []))
    stages, prompts = [], []
    entity = {'cluster_id': 'east', 'namespace': 'shop', 'kind': 'Deployment', 'name': 'engine',
              'uid': 'engine-Deployment-uid', 'resource_version': 'opaque-a'}
    def model(stage, messages, **kwargs):
        stages.append(stage); prompts.append(messages)
        if stage == 'reasoning':
            return {'root_cause': root or 'Deployment/engine 期望副本为 0，Service/gateway 无就绪端点，具体触发者待调查',
                'root_entity': entity, 'confidence': .9, 'reasoning_chain': '命名对象及当前状态已读取'}
        assert stage == 'independent_review'
        if review_client is not None:
            return bounded_call(stage, messages, **kwargs)
        if isinstance(review, Exception): raise review
        return review_answer() if review is None else review
    agent._call_llm_json = model
    evidence = {'agent_evidence_collection': {'commands': commands() if records is None else records,
        'service_scope': scope or {'status': 'collected'}}}
    result = {'diagnosis': {'root_cause': '待诊断', 'confidence': .6}, 'skill_runtime': {'runs': []}}
    agent.reason_about_result(SkillAgentRequest('HTTP 502', 'shop', {'payload': {'cluster_id': 'east'}}),
        hypotheses=hypotheses or [], verification_plan={}, evidence=evidence, skill_result=result)
    exported = SkillAgentResult('completed', 'HTTP 502', 'shop', 0, result, evidence, {}, agent.events).to_dict()
    return exported, stages, prompts


def test_separate_review_call_consumes_observations_and_exports_verdict():
    result, stages, prompts = reason()
    assert stages == ['reasoning', 'independent_review']
    assert result['judge']['needs_review'] is False
    assert 'replicas' in json.dumps(prompts[-1], ensure_ascii=False)
    assert '"E3"' in prompts[-1][1]['content']
    assert result['diagnosis_status'] == 'reviewed'
    assert result['confidence'] <= .9
    assert result['automation_gate']['allowed'] is False
    assert 'skill_action_contract_required' in result['automation_gate']['blockers']


@pytest.mark.parametrize('override', [
    {'supported_evidence_ids': []}, {'supported_evidence_ids': ['E404']},
    {'unsupported_claims': ['selector 标签匹配证明真实所有权']},
    {'contradictions': ['当前副本为 1']}, {'judge_level': True},
    {'evidence_score': float('nan')}, {'evidence_score': True}, {'evidence_score': '0.9'},
    {'evidence_score': .1}, {'judge_level': 3}, {'contradictions': None},
    {'supported_evidence_ids': ['E1']}, {'feedback': ''}, {'feedback': None},
])
def test_invalid_or_unsupported_review_never_passes(override):
    result, _, _ = reason(review_answer(**override))
    assert result['judge']['needs_review']
    assert result['diagnosis_status'] == 'requires_review'
    assert result['automation_gate']['allowed'] is False
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize('review', [{}, {'raw_response': 'truncated'}, RuntimeError('timeout')])
def test_unavailable_or_malformed_reviewer_stays_closed(review):
    result, _, _ = reason(review)
    assert result['judge']['needs_review']
    assert result['automation_gate']['allowed'] is False


@pytest.mark.parametrize('field,value', [('success', False), ('executed', False), ('outcome_unknown', True),
    ('cluster_id', 'west'), ('namespace', 'other')])
def test_failed_or_cross_scope_observations_cannot_review_root(field, value):
    records = commands()
    records[-1][field] = value
    result, _, _ = reason(records=records)
    assert result['judge']['needs_review']
    assert result['automation_gate']['allowed'] is False


def test_scope_gap_cannot_be_repaired_by_reviewer_agreement():
    result, _, _ = reason(scope={'status': 'ambiguous', 'candidate_workloads': ['engine','other']})
    assert result['judge']['needs_review']
    assert result['diagnosis_status'] == 'requires_review'


def test_only_proposed_citations_are_not_raw_observations():
    result, _, prompts = reason(records=[])
    assert result['judge']['needs_review']
    assert 'independent_review' not in [e.get('stage') for e in result['events']]


def test_model_cannot_add_automation_authority_to_a_review():
    result, _, _ = reason(review_answer(automation_gate={'allowed': True}, needs_review=False, approved=True))
    assert result['judge']['needs_review'] is False
    assert 'automation_gate' not in result['judge']['llm_based']
    assert result['automation_gate']['allowed'] is False


def test_missing_observation_time_is_not_a_reviewable_current_fact():
    rows = commands()
    rows[-1].pop('timestamp')
    result, stages, _ = reason(records=rows)
    assert result['judge']['needs_review']
    assert stages == ['reasoning']
    assert 'review_observation_time_missing' in result['judge']['review_blockers']


def test_review_evidence_is_not_replaced_with_generated_summary():
    rows = commands()
    rows[-1]['data'] = json.dumps({'kind':'List', 'items':[]})
    result, _, _ = reason(records=rows)
    assert result['judge']['needs_review']
    assert result['root_entity'] is None


def candidate():
    return {'id':'H1', 'description':'当前副本为0解释无就绪后端，触发者未知', 'confidence':.7,
            'status':'active', 'service':'gateway'}


def candidate_review():
    return {'id':'H1', 'status':'uncertain', 'supporting_evidence_ids':[],
            'contradicting_evidence_ids':[], 'reason':'缩零触发者仍缺少观测',
            'missing_evidence':['原始变更或审计事件'], 'next_agents':['event_agent']}


def test_complete_uncertain_competing_candidate_review_is_recorded_without_inventing_support():
    result, stages, _ = reason(review_answer(hypothesis_reviews=[candidate_review()]), hypotheses=[candidate()])
    assert stages == ['reasoning','independent_review']
    assert result['judge']['needs_review'] is False
    reviewed = result['judge']['hypothesis_review']
    assert reviewed['all_candidates_reviewed']
    assert reviewed['items'][0]['status'] == 'uncertain'
    assert not reviewed['items'][0]['assertion_verified']
    assert result['diagnostic_strategy']['independent_reviews'][0]['candidate_count'] == 1


@pytest.mark.parametrize('reviews', [[], [dict(candidate_review(), id='H2')],
    [candidate_review(),candidate_review()],
    [dict(candidate_review(), status='supported')],
    [dict(candidate_review(), status='supported', supporting_evidence_ids=['obs-invented'])]])
def test_missing_unknown_duplicate_or_unresolved_candidate_review_is_closed(reviews):
    result, _, _ = reason(review_answer(hypothesis_reviews=reviews), hypotheses=[candidate()])
    assert result['judge']['needs_review']
    assert 'candidate_review_incomplete' in result['judge']['review_blockers']


def test_command_cap_and_input_byte_cap_do_not_silently_review_a_prefix():
    result, stages, _ = reason(records=commands()*43)
    assert stages == ['reasoning']
    assert 'review_input_exceeds_budget' in result['judge']['review_blockers']
    rows = commands()
    row = json.loads(rows[1]['data']); row['subsets']=[{'addresses':[{'ip':'192.0.2.1'}]*800}]
    rows[1]['data'] = json.dumps(row)
    result, stages, _ = reason(records=rows)
    assert stages == ['reasoning']
    assert 'review_input_exceeds_budget' in result['judge']['review_blockers']


def test_review_output_byte_cap_stays_closed_and_json_safe():
    result, _, _ = reason(review_answer(feedback='x'*13000))
    assert result['judge']['needs_review']
    assert result['judge']['skill_review']['recording_status'] == 'failed_or_invalid'
    json.dumps(result, allow_nan=False)


def test_later_uid_conflict_beyond_proposer_catalog_display_cap_is_not_ignored():
    rows = [copy.deepcopy(commands()[-1]) for _ in range(33)]
    changed = json.loads(rows[-1]['data']); changed['metadata']['uid']='replacement'
    rows[-1]['data']=json.dumps(changed)
    result, stages, _ = reason(records=rows)
    assert stages == ['reasoning']
    assert result['judge']['needs_review']
    assert 'root_identity_unresolved' in result['judge']['review_blockers']


def test_identical_rereads_keep_original_ids_and_times_without_inflating_review_facts():
    rows = commands()
    again = copy.deepcopy(rows)
    for row in again:
        row['timestamp'] = '2026-10-27T02:00:01+00:00'
        data = json.loads(row['data']); data['observed_at'] = row['timestamp']
        row['data'] = json.dumps(data)
    result, stages, prompts = reason(records=rows+again)
    assert result['judge']['needs_review'] is False
    assert result['judge']['evidence_count'] == 3
    summary = result['judge']['skill_review']
    assert summary['source_observation_count'] == 6
    assert summary['review_state_count'] == 3
    assert len(summary['observation_aliases']) == 3
    assert all(alias['collected_at'] == '2026-10-27T02:00:01+00:00' for alias in summary['observation_aliases'])
    from operational_ontology import build_investigation
    view = build_investigation({'id':'repeat','status':'completed','cluster_id':'east','namespace':'shop','result':result})
    assert len(view['observations']) == 6
    assert all(link['status'] == 'resolved' for link in view['review_evidence_links'])


def test_different_endpoint_state_is_not_collapsed_with_an_empty_endpoint():
    rows = commands()
    changed = copy.deepcopy(rows[1]); changed['timestamp']='2026-10-27T02:00:01+00:00'
    data = json.loads(changed['data']); data['metadata']['resourceVersion']='opaque-b'
    data['observed_at']=changed['timestamp']; data['subsets']=[{'addresses':[{'ip':'192.0.2.1'}]}]
    changed['data']=json.dumps(data)
    result, _, prompts = reason(review_answer(contradictions=['目标端点状态在采样间发生改变']), records=rows+[changed])
    assert result['judge']['needs_review']
    assert result['judge']['skill_review']['review_state_count'] == 4
    assert result['judge']['skill_review']['observation_aliases'] == []


def test_native_review_timeout_does_not_accept_a_late_success():
    from threading import Event
    import time
    released = Event()
    class Slow:
        def json_chat(self, *args, **kwargs):
            released.wait(2)
            return review_answer()
    try:
        result, stages, _ = reason(review_client=Slow(), review_timeout=.02)
        assert result['judge']['needs_review']
        assert result['automation_gate']['allowed'] is False
        assert any(e.get('event') == 'llm_independent_review_timeout' for e in result['events'])
        before = json.dumps(result, sort_keys=True, allow_nan=False)
        released.set(); time.sleep(.05)
        assert json.dumps(result, sort_keys=True, allow_nan=False) == before
    finally:
        released.set()


def complete_receipt(**overrides):
    value = dict(schema_version=1,source='provider_response',completion_status='complete',finish_reason='stop',
                 refusal_present=False,tool_call_present=False)
    value.update(overrides)
    return value


def test_review_propagates_native_network_deadline_and_disables_retries():
    from tools.llm_receipt import JSONCompletion
    calls = []
    class Client:
        def json_chat(self, messages, **kwargs):
            calls.append(kwargs)
            return JSONCompletion(review_answer(), complete_receipt())
    result, _, _ = reason(review_client=Client())
    assert result['judge']['needs_review'] is False
    assert calls[0]['request_timeout_s'] == .2
    assert calls[0]['max_retries'] == 0 and calls[0]['with_receipt'] is True
    assert result['judge']['skill_review']['provider_receipt']['completion_status'] == 'complete'


@pytest.mark.parametrize('metadata', [None, {'completion_status':'truncated','finish_reason':'length'},
    {'completion_status':'filtered','finish_reason':'content_filter'}, {'refusal_present':True},
    {'tool_call_present':True}, {'schema_version':True}])
def test_review_requires_provider_completion_even_if_generated_json_looks_valid(metadata):
    from tools.llm_receipt import JSONCompletion
    class Client:
        def json_chat(self, messages, **kwargs):
            if metadata is None:
                return dict(review_answer(),provider=complete_receipt())  # generated, not transport metadata
            receipt = complete_receipt(); receipt.update(metadata)
            return JSONCompletion(review_answer(),receipt)
    result, _, _ = reason(review_client=Client())
    assert result['judge']['needs_review']
    assert any(e.get('event')=='llm_independent_review_incomplete' for e in result['events'])
    json.dumps(result,allow_nan=False)


def test_legacy_client_without_receipts_stays_closed_without_an_argument_error():
    class Legacy:
        def json_chat(self, messages, temperature=None, max_tokens=None): return review_answer()
    result, _, _ = reason(review_client=Legacy())
    assert result['judge']['needs_review']
    assert any(e.get('event')=='llm_independent_review_incomplete' for e in result['events'])


@pytest.mark.parametrize('mode', ['new_rv','new_uid','same_rv_changed_state','inventory'])
def test_ordinary_json_or_inventory_contradiction_is_not_silently_excluded(mode):
    rows = commands()
    current = json.loads(rows[-1]['data']); current['spec']['replicas']=1
    if mode != 'same_rv_changed_state': current['metadata']['resourceVersion']='opaque-b'
    if mode == 'new_uid': current['metadata']['uid']='replacement'
    extra = dict(rows[-1], command='get deployments -n shop -o json' if mode=='inventory' else 'get deployment/engine -n shop -o json',
        data=json.dumps({'kind':'DeploymentList','items':[current]} if mode=='inventory' else current))
    result, stages, _ = reason(records=rows+[extra])
    assert stages == ['reasoning']
    assert result['judge']['needs_review']
    assert any(x in result['judge']['review_blockers'] for x in ('conflicting_resource_observations','conflicting_resource_state'))


@pytest.mark.parametrize('stamp', [float('nan'), float('inf'), -float('inf'), 'invalid-time'])
def test_invalid_alias_time_returns_a_strictly_serializable_refusal(stamp):
    from orchestrator.skill_review import review_skill_diagnosis
    from skill_runtime.service_evidence import observed_entity_catalog
    rows = commands(); extra = copy.deepcopy(rows[-1]); extra['timestamp']=stamp
    entity = observed_entity_catalog(rows,'east','shop')[-1]
    result = review_skill_diagnosis(root_cause='当前期望副本0',reasoning='实际命名对象读取',confidence=.9,
        root_entity=entity, commands=rows+[extra], cluster_id='east',namespace='shop',
        service_scope={'status':'collected'},hypotheses=[],call_model=lambda m:pytest.fail('invalid time cannot be reviewed'))
    assert result['needs_review']
    assert result['skill_review']['invalid_observation_times'] == 1
    json.dumps(result,allow_nan=False)


def test_current_zero_replicas_cannot_refute_a_competing_historical_mechanism():
    from orchestrator.skill_review import review_skill_diagnosis
    from skill_runtime.service_evidence import observed_entity_catalog
    from operational_ontology import committed_tool_observations, observation_id
    rows = commands(); entity=observed_entity_catalog(rows,'east','shop')[-1]
    eid=observation_id(committed_tool_observations(rows[-1])[0])
    h=dict(candidate(),status='rejected',description='资源不足驱逐或端口配置错误导致故障')
    answer=review_answer(hypothesis_reviews=[dict(candidate_review(),status='contradicted',
        contradicting_evidence_ids=[eid],reason='当前副本0足以排除所有其它机制')])
    result=review_skill_diagnosis(root_cause='当前期望副本0，触发者待调查',reasoning='有限观测结论',confidence=.9,
        root_entity=entity,commands=rows,cluster_id='east',namespace='shop',service_scope={'status':'collected'},
        hypotheses=[h],call_model=lambda m:answer)
    item=result['hypothesis_review']['items'][0]
    assert item['reviewer_status']=='contradicted' and item['status']=='uncertain'
    assert not item['assertion_verified'] and result['needs_review']
    assert 'direct_falsification_not_observed' in item['rationale_limits']


def test_tool_context_records_host_collection_time_for_plain_json():
    from skill_runtime.tool_context import SkillToolContext
    from skill_runtime.ontology_history import collection_time
    context=SkillToolContext(runner=lambda *args:{'executed':True,'success':True,'data':'{}'})
    result=context.execute('kubectl',command='get pods -o json',namespace='shop')
    assert collection_time(result['timestamp']) is not None
    assert context.calls[0]['timestamp']==result['timestamp']


@pytest.mark.parametrize('body', ['No resources found\nactual application failure',
    'No resources found', '{"kind":"EventList","items":[]}'])
def test_application_logs_cannot_use_empty_inventory_exceptions(body):
    rows=commands(); rows.append(dict(rows[-1],tool='kubectl',command='logs deployment/engine --tail=40',data=body))
    result, stages, _=reason(records=rows)
    assert stages==['reasoning']
    assert 'unreviewed_tool_output' in result['judge']['review_blockers']


def test_only_a_complete_empty_get_inventory_receipt_is_ignored():
    rows=commands(); rows.append(dict(rows[-1],command='get events -n shop',data='No resources found in shop namespace.'))
    result, _, _=reason(records=rows)
    assert result['judge']['needs_review'] is False
    rows[-1]['data'] += '\nWarning: actual error'
    result, _, _=reason(records=rows)
    assert result['judge']['needs_review']


def test_malformed_resource_kind_is_a_serializable_review_refusal():
    rows=commands(); rows.append(dict(rows[-1],command='get deployment/engine -n shop -o json',
        data=json.dumps({'kind':['Deployment'],'metadata':{'name':'engine','namespace':'shop'}})))
    result, stages, _=reason(records=rows)
    assert stages==['reasoning'] and result['judge']['needs_review']
    json.dumps(result['judge'],allow_nan=False)


def test_failed_review_cannot_reuse_a_prior_successful_transport_receipt():
    prior={'event':'llm_independent_review_result','provider_receipt':complete_receipt()}
    result, _, _=reason(RuntimeError('unavailable'),seeded_events=[prior])
    assert result['judge']['needs_review']
    assert result['judge']['skill_review']['provider_receipt'] is None


def test_long_unreviewed_tail_cannot_receive_a_pass():
    result, _, _ = reason(root='有界观测结论。'*100+'未经观测的尾部原因')
    assert result['judge']['needs_review']
    assert 'review_input_exceeds_budget' in result['judge']['review_blockers']


def test_ontology_and_strategy_use_the_persisted_review_but_deny_execution():
    from operational_ontology import build_investigation, action_context
    result, _, _ = reason()
    view = build_investigation({'id': 'review-test', 'cluster_id': 'east', 'namespace': 'shop',
        'status': 'completed', 'result': result})
    assert view['decision']['review_status'] == 'reviewed'
    assert view['review_evidence_links']
    assert all(row['status'] == 'resolved' for row in view['review_evidence_links'])
    action = action_context(view)
    assert 'independent_review_required' not in action['blockers']
    assert 'diagnosis_gate_not_allowed' in action['blockers']
    assert not action['recorded_conditions_met']
    reviews = result['diagnostic_strategy']['independent_reviews']
    assert len(reviews) == 1 and reviews[0]['needs_review'] is False
    typed = result['diagnostic_strategy']['scale_zero_evidence']
    assert typed['execution_authorized'] is False
    assert typed['eligible_for_further_review'] is False
    assert 'review_receipt_incomplete' in typed['blockers']
    from diagnostic_strategy import strategy_view
    saved_view = strategy_view({'id': 'review-test', 'status': 'completed', 'result': result})
    assert saved_view['scale_zero_evidence'] == typed
