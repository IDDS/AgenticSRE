import copy

import pytest

from eval.alert_acceptance_gate import assess, score_live_scan


def inputs():
    contract=dict(candidate_image='sha256:fixture',synthetic_cases={'labelled':2},
        live_scenarios={'independent-faults':[2,2]},scans_per_live_scenario=1,
        production_quality={'root_accuracy_target':.8})
    row=dict(case='labelled',input=2,partition_valid=True,raw_exactly_preserved=True,
        order_invariant=True,sampled_peak_retention=True,sampled_critical_representatives=True,
        false_merge_pairs=0,missed_compatible_pairs=0,critical_input=1,critical_visible_in_groups=1,
        provenance='controlled_synthetic',dataset_sha256='a'*64)
    live=dict(status='passed',namespace='qa',production_lifecycle_unchanged=True,
        namespace_removed=True,containers_cleaned=True,stages=[dict(phase='independent-faults',
            requests=10,request_errors=[],scans=[{}])])
    raw=[]
    for pod,name,severity in [('qa-a','ProbeError','warning'),('qa-c','ProbeIndependentCritical','critical')]:
        raw.append(dict(entity=pod,name=name,severity=severity,cluster_id='default',namespace='qa',source='prometheus',
            raw=dict(name=name,labels={'pod':pod,'namespace':'qa'})))
    scan=dict(raw_alerts=raw,groups=[dict(alert_indices=[0],severity='warning'),dict(alert_indices=[1],severity='critical')],
        collection={'prometheus':'ok','kubectl':'ok'},semantic={'model_calls':0})
    return contract,dict(cases=[row]),live,scan


def test_engineering_pass_cannot_certify_production_accuracy():
    contract,report,live,scan=inputs()
    result=assess(contract,report,report,live,{('independent-faults',0):scan})
    assert result['engineering_passed']
    assert result['verdict']=='not_accepted' and result['production_root_accuracy'] is None
    assert result['natural_incidents']==0
    assert all(g['status']=='blocked' for g in result['gates'] if g['name'] in ('production_quality','full_rca_and_cost'))


@pytest.mark.parametrize('field,value',[
    ('false_merge_pairs',1),('missed_compatible_pairs',1),('critical_visible_in_groups',0),
    ('raw_exactly_preserved',False),('sampled_peak_retention',False),('provenance','natural_production'),
    ('input',1)])
def test_declared_pass_does_not_override_failed_metrics(field,value):
    contract,report,live,scan=inputs()
    candidate=copy.deepcopy(report)
    candidate['passed']=True
    candidate['cases'][0][field]=value
    assert not assess(contract,report,candidate,live,{('independent-faults',0):scan})['engineering_passed']


def test_missing_cases_are_not_vacuous_success():
    contract,report,live,scan=inputs()
    assert not assess(contract,report,{'passed':True,'cases':[]},live,{})['engineering_passed']
    assert not assess(contract,None,None)['engineering_passed']


def test_changed_input_breaks_pairing():
    contract,report,live,scan=inputs()
    candidate=copy.deepcopy(report)
    candidate['cases'][0]['dataset_sha256']='b'*64
    out=assess(contract,report,candidate,live,{('independent-faults',0):scan})
    assert next(g for g in out['gates'] if g['name']=='paired_inputs')['status']=='failed'


@pytest.mark.parametrize('change',['missing_scan','missing_stage','request_error','cleanup','degraded','critical','duplicate_member','wrong_entity','hidden_model_call','false_merge'])
def test_live_failures_are_not_hidden_by_runner_pass(change):
    contract,report,live,scan=inputs()
    scans={('independent-faults',0):scan}
    if change=='missing_scan':scans={}
    if change=='missing_stage':live['stages']=[]
    if change=='request_error':live['stages'][0]['request_errors']=['timeout']
    if change=='cleanup':live['namespace_removed']=False
    if change=='degraded':scan['degraded']=True
    if change=='critical':scan['groups'][1]['severity']='warning'
    if change=='duplicate_member':scan['groups'][1]['alert_indices']=[0]
    if change=='wrong_entity':scan['raw_alerts'][0]['entity']='qa-c'
    if change=='hidden_model_call':scan['semantic']['model_calls']=1
    if change=='false_merge':
        scan['groups']=[dict(alert_indices=[0,1],severity='critical')]
        # Even an expected count agreeing with the bad result cannot waive truth.
        contract['live_scenarios']['independent-faults']=[2,1]
    assert not assess(contract,report,report,live,scans)['engineering_passed']


def test_singletons_and_empty_scans_do_not_claim_perfect_pair_accuracy():
    _,_,_,scan=inputs()
    scored=score_live_scan(scan,'independent-faults','qa',[2,2])
    assert scored['passed'] and scored['pair_precision'] is None and scored['pair_recall'] is None
    scan.update(raw_alerts=[],groups=[])
    assert score_live_scan(scan,'healthy-load','qa',[0,0])['pair_precision'] is None


@pytest.mark.parametrize('count',[2,3,4])
def test_disjoint_rolling_thresholds_keep_two_groups_for_allowed_counts(count):
    _,_,_,scan=inputs()
    base=copy.deepcopy(scan['raw_alerts'][0])
    raw=[base]
    for index in range(count-1):
        row=copy.deepcopy(base)
        row['name']=row['raw']['name']='ProbeLatency'+str(100*(index+1))
        row['severity']='critical' if index==2 else 'warning'
        raw.append(row)
    scan.update(raw_alerts=raw,groups=[dict(alert_indices=[0],severity='warning'),
        dict(alert_indices=list(range(1,count)),severity='critical' if count==4 else 'warning')])
    assert score_live_scan(scan,'disjoint-requests-same-pod','qa',[[2,3,4],2])['passed']
    scan['groups']=[dict(alert_indices=list(range(count)),severity='critical')]
    result=score_live_scan(scan,'disjoint-requests-same-pod','qa',[[2,3,4],1])
    assert not result['passed'] and result['false_merge_pairs']==count-1


@pytest.mark.parametrize('count',[0,1,5])
def test_dynamic_count_contract_rejects_outside_range(count):
    _,_,_,scan=inputs()
    scan['raw_alerts']=[copy.deepcopy(scan['raw_alerts'][0]) for _ in range(count)]
    scan['groups']=[dict(alert_indices=list(range(count)),severity='warning')]
    scored=score_live_scan(scan,'disjoint-requests-same-pod','qa',[[2,3,4],1])
    assert not scored['checks']['expected_counts'] and not scored['passed']
