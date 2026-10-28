"""Restricted Skill action admission; controlled fixtures are not live faults."""
import copy
import asyncio
import time
from types import SimpleNamespace

import pytest
import web_app.app as web
from orchestrator.skill_scale_evidence import assess_scale_zero_evidence


@pytest.fixture
def contract(monkeypatch):
    root = {'cluster_id':'east', 'namespace':'shop', 'kind':'Deployment', 'name':'engine',
            'uid':'deployment-uid', 'resource_version':'dv1'}
    target = {'id':'d1', 'kind':'Deployment', 'cluster_id':'east', 'namespace':'shop',
              'name':'engine', 'uid':'deployment-uid'}
    service = {'id':'s1', 'kind':'Service', 'cluster_id':'east', 'namespace':'shop',
               'name':'gateway', 'uid':'service-uid', 'identity_status':'uid_bound'}
    scope = {'status':'collected', 'gaps':[], 'candidate_workloads':['engine'],
             'selected_pod_count':0, 'relation':'matches_workload_template'}
    def fact(identifier, kind, name, uid, version, spec, **extra):
        return {'id':identifier, 'timestamp':'2026-10-28T01:00:00+00:00', 'raw_ref':{
            'kind':kind, 'cluster_id':'east', 'topology_fields_complete':True,
            'metadata':{'name':name,'namespace':'shop','uid':uid,'resourceVersion':version},
            'spec':spec, **extra}}
    judge = {'needs_review':False, 'review_blockers':[], 'skill_review':{'provider_receipt':{
        'schema_version':1,'source':'provider_response','completion_status':'complete',
        'finish_reason':'stop','refusal_present':False,'tool_call_present':False}},
        'llm_based':{'supported_evidence_ids':['E1','E2','E3']},'review_evidence':[
            fact('E1','Service','gateway','service-uid','sv1',{'selector':{'app':'engine'}}),
            fact('E2','Endpoints','gateway','endpoints-uid','ev1',{},subsets=[]),
            fact('E3','Deployment','engine','deployment-uid','dv1',{'replicas':0,
                 'template':{'metadata':{'labels':{'app':'engine'}}}})]}
    proof = assess_scale_zero_evidence(root_entity=root,service='gateway',scope=scope,judge=judge)
    assert proof['eligible_for_further_review'] is True
    report = {'root_cause':'service unavailable while expected replicas are zero',
              'fault_type':'ServiceUnavailable','root_entity':root,'judge':judge,
              'agent_evidence':{'agent_evidence_collection':{'service_scope':scope}},
              'diagnostic_strategy':{'scale_zero_evidence':proof}}
    run = {'id':'rca-one','status':'completed','guarded_auto_recovery':True,'result':report}
    model = {'gaps':[], 'objects':[service], 'relations':[{'type':'matches_workload_template',
             'source_id':'s1','target_id':'d1'}]}
    context = {'blockers':['diagnosis_gate_not_allowed'], 'recorded_conditions_met':False,
               'target':target,'expected_resource_version':'dv1','ontology_revision':'rev1'}
    cfg = SimpleNamespace(remediation=SimpleNamespace(auto_loop_enabled=True, auto_scale_targets=[{
        'cluster_id':'east','namespace':'shop','service':'gateway','deployment':'engine','replicas':1}]))
    monkeypatch.setattr(web,'_get_config',lambda:cfg)
    monkeypatch.setattr(web,'action_context',lambda model:copy.deepcopy(context))
    return SimpleNamespace(run=run, model=model, context=context, cfg=cfg, report=report)


def test_only_internal_guarded_context_can_open_narrow_scale_contract(contract):
    context=web._guarded_skill_scale_context(contract.run,contract.model)
    assert context['recorded_conditions_met'] is True
    assert context['guarded_skill_scale_contract']['replicas'] == 1
    assert contract.report.get('automation_gate') is None
    assert web.action_context(contract.model)['recorded_conditions_met'] is False


@pytest.mark.parametrize('mutation', ['unguarded','incomplete','changed_version','other_fault',
    'no_policy','policy_bool','service_ambiguous','gap','unreviewed','forged_proof'])
def test_invalid_contract_conditions_cannot_open_admission(contract, mutation):
    if mutation=='unguarded': contract.run['guarded_auto_recovery']=False
    if mutation=='incomplete': contract.run['status']='running'
    if mutation=='changed_version': contract.report['root_entity']['resource_version']='dv2'
    if mutation=='other_fault': contract.report['fault_type']='ImagePullError'
    if mutation=='no_policy': contract.cfg.remediation.auto_scale_targets=[]
    if mutation=='policy_bool': contract.cfg.remediation.auto_scale_targets[0]['replicas']=True
    if mutation=='service_ambiguous': contract.model['relations']=[]
    if mutation=='gap': contract.model['gaps']=['collection_incomplete']
    if mutation=='unreviewed': contract.report['judge']['needs_review']=True
    if mutation=='forged_proof': contract.report['diagnostic_strategy']['scale_zero_evidence']['blockers']=['changed']
    result=web._guarded_skill_scale_context(contract.run,contract.model)
    assert result['recorded_conditions_met'] is False
    assert 'guarded_skill_scale_contract' not in result


@pytest.mark.parametrize('commands,allowed', [
    (['kubectl scale deployment/engine -n shop --replicas=1'],True),
    (['kubectl scale deployment/engine -n shop --replicas=1','kubectl scale deployment/engine -n shop --replicas=0'],True),
    (['kubectl scale deployment/engine -n shop --replicas=0'],False),
    (['kubectl scale deployment/engine -n shop --replicas=2'],False),
    (['kubectl rollout restart deployment/engine -n shop'],False),
    (['kubectl scale deployment/other -n shop --replicas=1'],False),
    (['kubectl scale deployment/engine -n shop --replicas=1','kubectl scale deployment/engine -n shop --replicas=2'],False),
])
def test_contract_restricts_actual_commands_and_target(contract,monkeypatch,commands,allowed):
    context=web._guarded_skill_scale_context(contract.run,contract.model)
    monkeypatch.setattr(web,'_ontology_live_resource',lambda target: {'ok':True,'json':{
        'metadata':{'uid':'deployment-uid','resourceVersion':'dv1'}}})
    if allowed:
        assert web._verify_ontology_action(context,commands)['metadata']['uid']=='deployment-uid'
    else:
        with pytest.raises(web.HTTPException): web._verify_ontology_action(context,commands)


def test_guarded_coordinator_reaches_real_heal_gate_with_controlled_dependencies(contract, monkeypatch, tmp_path):
    """Synthetic history and execution verify composition, not live recovery."""
    from remediation_history import build_recovery_scope
    from recovery_verification import verification_contract
    contract.run['started_at'] = time.time()
    contract.model.update(revision='rev1',scope={'cluster_id':'east','namespace':'shop'},
                          observations=[{'entity_id':'s1','temporality':'observed','resource_version':'sv1'}])
    cfg=contract.cfg
    cfg.kubernetes=SimpleNamespace(namespace='shop')
    cfg.remediation.enabled=True
    cfg.remediation.recommend_only=False
    cfg.remediation.require_approval=False
    cfg.remediation.max_steps=1
    cfg.remediation.business_recovery_probes=[]
    command='kubectl scale deployment/engine -n shop --replicas=1'
    rollback='kubectl scale deployment/engine -n shop --replicas=0'
    action={'source':'kb','executable':True,'risk':'medium','command':command,'rollback_command':rollback}
    scope={**build_recovery_scope(commands=[command],cluster_id='east',namespace='shop',
                                  target={'service':'gateway','deployment':'engine'}),
           **verification_contract(profiles=[],cluster_id='east',namespace='shop',service='gateway')}
    stats={**scope,'scope_required':True,'verification_required':True,'verification_scope_complete':True,
           'sample_count':20,'success_count':20,'failure_count':0,'success_rate':1.0}
    records,dispatched=[],[]
    monkeypatch.setattr(web,'_HEAL_ARTIFACT_DIR',tmp_path)
    monkeypatch.setitem(web._state,'rca_runs',{'rca-one':contract.run})
    monkeypatch.setattr(web,'build_investigation',lambda run:copy.deepcopy(contract.model))
    monkeypatch.setattr(web,'_build_heal_suggestions',lambda body:{'namespace':'shop',
        'diagnosis_gate':{'ready':True},'diagnosis_used':{'confidence':.99,'fault_type':'ServiceUnavailable'},
        'fault_type':'ServiceUnavailable','recipe':{'safe_auto_execute':True},
        'target':{'service':'gateway','deployment':'engine'},'suggestions':[action]})
    monkeypatch.setattr(web,'_recovery_scope_for_plan',lambda **kwargs:scope)
    monkeypatch.setattr(web,'_scoped_recovery_history_stats',lambda **kwargs:(stats,scope))
    monkeypatch.setattr(web,'_load_heal_runs',lambda:list(records))
    monkeypatch.setattr(web,'_save_heal_run',lambda record:records.insert(0,copy.deepcopy(record)))
    def live(target):
        if target['kind']=='Service':
            return {'ok':True,'json':{'metadata':{'uid':'service-uid','resourceVersion':'sv1'},
                                      'spec':{'selector':{'app':'engine'}}}}
        return {'ok':True,'json':{'metadata':{'uid':'deployment-uid','resourceVersion':'dv1'},
            'spec':{'replicas':0,'template':{'metadata':{'labels':{'app':'engine'}}}}}}
    monkeypatch.setattr(web,'_ontology_live_resource',live)
    monkeypatch.setattr(web,'_execute_ontology_command',lambda *args:dispatched.append(args) or {'ok':True,'executed':True})
    monkeypatch.setattr(web,'_verify_heal_recovery',lambda **kwargs:{'status':'verified','recovered':True})
    monkeypatch.setattr(web,'verify_business_recovery',lambda verification,**kwargs:verification)
    monkeypatch.setattr(web,'_record_verified_recovery_outcome',lambda **kwargs:{'recorded':True})
    # The public route does not receive the internal capability.
    public=web._bind_ontology_heal_input({'source_run_id':'rca-one','ontology_revision':'rev1'},execution=True)
    assert public['recorded_conditions_met'] is False
    result=asyncio.run(web._guarded_auto_recovery('rca-one'))
    assert result['status']=='succeeded' and result['verification']['recovered'] is True
    assert len(dispatched)==1 and dispatched[0][0]==command
    assert asyncio.run(web._guarded_auto_recovery('rca-one'))==result
    assert len(dispatched)==1
