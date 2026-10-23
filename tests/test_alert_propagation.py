import copy
import pytest
from agents.alert_compression import compress_alerts
from agents.alert_context import trace_evidence, qualify_context, recall_context
from agents.alert_semantics import build_scag


def fixture(error=True):
    inv={'pods':[{'namespace':'ns','name':n,'uid':n+'-uid'} for n in ('caller','dependency')]}
    processes={n:{'tags':[{'key':'k8s.pod.uid','value':n+'-uid'}]} for n in ('caller','dependency')}
    tags=[{'key':'error','value':error}]
    payload={'data':[{'traceID':'t','processes':processes,'spans':[
        {'spanID':'parent','processID':'caller','startTime':1000000000,'duration':2000000,'tags':tags},
        {'spanID':'child','processID':'dependency','startTime':1000500000,'duration':500000,'tags':tags,
         'references':[{'refType':'CHILD_OF','traceID':'t','spanID':'parent'}]}]}]}
    records=[dict(name='DependencyError',message='request failed',pod='dependency',timestamp=1001),
             dict(name='CallerError',message='request failed',pod='caller',timestamp=1002)]
    result=compress_alerts([dict(r,cluster_id='c',namespace='ns') for r in records])
    result['context_evidence']={'traces':trace_evidence(payload,'c',inv,'fixture')}
    by_entity={g['entity']:g for g in result['groups']}
    return result,by_entity['dependency'],by_entity['caller'],payload,inv


def test_fault_direction_is_reverse_of_observed_call_and_never_causal():
    result,dependency,caller,_,_=fixture()
    evidence=qualify_context(result,dependency,caller,'propagates_to')
    assert evidence['status']=='pass'
    assert evidence['propagation_direction']==['c|ns|pod|dependency','c|ns|pod|caller']
    assert evidence['trace_refs'][0]['call_source']=='c|ns|pod|caller'
    assert qualify_context(result,caller,dependency,'propagates_to')['status']=='unknown'
    recall_context(result)
    assert len(result['candidate_pairs'])==1
    result['relations']=[dict(left=dependency['group_id'],right=caller['group_id'],type='propagates_to')]
    build_scag(result)
    edge=result['scag']['edges'][0]
    assert edge['checks']['structure']['path']==['c|ns|pod|dependency','c|ns|pod|caller']
    assert edge['checks']['time']['status']=='pass'
    assert edge['checks']['context']['status']=='pass'
    assert edge['checks']['statistics']['status']=='unknown'
    assert edge['status']=='unverified' and not edge['causal_confirmed']


@pytest.mark.parametrize('mode',['healthy','missing','one_error','bad_completion','stale'])
def test_calls_without_coupled_error_evidence_do_not_support_propagation(mode):
    result,dependency,caller,_,_=fixture(error=mode!='healthy')
    edge=result['context_evidence']['traces']['edges'][0]
    if mode=='missing':
        edge.pop('caller_error');edge.pop('callee_error')
    if mode=='one_error': edge['caller_error']=False
    if mode=='bad_completion': edge['callee_end']=edge['caller_end']+1
    if mode=='stale': edge['at']=100
    assert qualify_context(result,dependency,caller,'propagates_to')['status']=='unknown'


def test_conflicting_identity_rejected_even_when_uid_matches():
    _,_,_,payload,inv=fixture()
    payload['data'][0]['processes']['dependency']['tags'].append({'key':'k8s.namespace.name','value':'other'})
    assert trace_evidence(payload,'c',inv,'fixture')['edges']==[]


def test_prompt_context_exposes_both_directions_with_explicit_uncertainty():
    result,dependency,caller,_,_=fixture()
    context=qualify_context(result,caller,dependency,'related')
    candidate=context['propagation_candidates'][0]
    assert candidate['source']=='c|ns|pod|dependency'
    assert candidate['target']=='c|ns|pod|caller'
    assert not candidate['causal_confirmed']
    assert context['trace_refs'][0]['call_source']=='c|ns|pod|caller'


def test_explicit_alert_trace_id_must_match_supporting_trace():
    result,dependency,caller,_,_=fixture()
    for row in result['raw_alerts']:
        row['raw']['trace_id']='another-trace'
    assert qualify_context(result,dependency,caller,'propagates_to')['status']=='unknown'
    for row in result['raw_alerts']:
        row['raw']['trace_id']='t'
    assert qualify_context(result,dependency,caller,'propagates_to')['status']=='pass'


def multihop_fixture(hops=2):
    result, dependency, caller, _, _ = fixture()
    base=result['context_evidence']['traces']['edges'][0]
    names=['dependency']+[f'middle-{i}' for i in range(hops-1)]+['caller']
    edges=[]
    for i in range(hops):
        edge=copy.deepcopy(base)
        edge.update(source='c|ns|pod|'+names[i+1],target='c|ns|pod|'+names[i],
                    span_id=f's{i}',parent_span_id=f's{i+1}',at=1000.5-i*.1,
                    callee_end=1001+i*.1,caller_end=1001+(i+1)*.1)
        edges.append(edge)
    result['context_evidence']['traces']['edges']=edges
    return result,dependency,caller


def test_multihop_recall_works_without_an_alert_on_intermediate_service():
    result,dependency,caller=multihop_fixture()
    recall_context(result)
    assert len(result['candidate_pairs'])==1
    assert result['candidate_pairs'][0]['retrieval']['observed_context']=='propagates_to'
    result['relations']=[dict(left=dependency['group_id'],right=caller['group_id'],type='propagates_to')]
    build_scag(result)
    edge=result['scag']['edges'][0]
    assert edge['context']['status']=='pass'
    assert edge['checks']['structure']['path']==['c|ns|pod|dependency','c|ns|pod|middle-0','c|ns|pod|caller']
    assert edge['checks']['structure']['observed_call_path']==['c|ns|pod|caller','c|ns|pod|middle-0','c|ns|pod|dependency']
    assert len(edge['context']['trace_refs'])==2
    assert edge['status']=='unverified' and not edge['causal_confirmed']
    assert qualify_context(result,caller,dependency,'propagates_to')['status']=='unknown'


@pytest.mark.parametrize('mode',['different_trace','different_span','healthy_middle','missing_error','time_gap','wrong_scope','stale_hop'])
def test_multihop_never_stitches_unrelated_or_unsupported_hops(mode):
    result,dependency,caller=multihop_fixture()
    edge=result['context_evidence']['traces']['edges'][1]
    if mode=='different_trace': edge['trace_id']='other'
    if mode=='different_span': edge['span_id']='other'
    if mode=='healthy_middle': edge['callee_error']=False
    if mode=='missing_error': edge.pop('caller_error')
    if mode=='time_gap': edge['callee_end']+=.01
    if mode=='wrong_scope': edge['source']='other|ns|pod|caller'
    if mode=='stale_hop': edge['at']=0
    assert qualify_context(result,dependency,caller,'propagates_to')['status']=='unknown'
    recall_context(result)
    assert not result['candidate_pairs']


def test_multihop_keeps_all_hops_in_supporting_trace_references():
    result,dependency,caller=multihop_fixture(hops=5)
    evidence=qualify_context(result,dependency,caller,'propagates_to')
    assert evidence['status']=='pass'
    assert len(evidence['trace_refs'])==5
    assert len(evidence['propagation_path'])==6
    assert evidence['propagation_candidates'][0]['hops']==5


def test_repeated_backend_trace_results_do_not_multiply_paths():
    result,dependency,caller=multihop_fixture(hops=5)
    result['context_evidence']['traces']['edges'] *= 3
    evidence=qualify_context(result,dependency,caller,'propagates_to')
    assert evidence['status']=='pass'
    assert len(evidence['propagation_candidates'])==1
    assert len(evidence['trace_refs'])==5


def test_cyclic_entity_path_is_not_a_propagation_candidate():
    from agents.alert_context import error_propagation_paths
    result,_,_=multihop_fixture()
    edges=result['context_evidence']['traces']['edges']
    edges[1]['source']=edges[0]['target']
    assert all(len(chain)==1 for chain in error_propagation_paths(edges))


def bridged_fixture():
    result,dependency,caller=multihop_fixture()
    edges=result['context_evidence']['traces']['edges']
    edges[0]['parent_span_id']='middle-client'
    edges[1].update(span_id='middle-server',callee_end=1001.15,caller_end=1002)
    bridge=dict(edges[0],source='c|ns|pod|middle-0',target='c|ns|pod|middle-0',
                span_id='middle-client',parent_span_id='middle-server',at=1000.45,
                callee_end=1001.1,caller_end=1001.15,kind='internal_span')
    result['context_evidence']['traces']['internal_span_edges']=[bridge]
    return result,dependency,caller


def test_internal_span_bridge_retains_proof_without_duplicate_entities():
    result,dependency,caller=bridged_fixture()
    evidence=qualify_context(result,dependency,caller,'propagates_to')
    assert evidence['status']=='pass'
    assert evidence['propagation_path']==['c|ns|pod|dependency','c|ns|pod|middle-0','c|ns|pod|caller']
    assert len(evidence['trace_refs'])==3
    assert evidence['propagation_candidates'][0]['hops']==2
    assert evidence['propagation_candidates'][0]['span_link_count']==3
    assert evidence['trace_refs'][1]['kind']=='internal_span'
    recall_context(result)
    assert len(result['candidate_pairs'])==1


@pytest.mark.parametrize('mode',['missing','healthy','unknown_error','other_trace','other_parent','wrong_time','unmarked'])
def test_internal_span_bridge_requires_explicit_contiguous_error_evidence(mode):
    result,dependency,caller=bridged_fixture()
    bridge=result['context_evidence']['traces']['internal_span_edges'][0]
    if mode=='missing': result['context_evidence']['traces']['internal_span_edges']=[]
    if mode=='healthy': bridge['caller_error']=False
    if mode=='unknown_error': bridge.pop('callee_error')
    if mode=='other_trace': bridge['trace_id']='different'
    if mode=='other_parent': bridge['parent_span_id']='different'
    if mode=='wrong_time': bridge['callee_end']+=.01
    if mode=='unmarked': bridge['kind']='observed_call'
    assert qualify_context(result,dependency,caller,'propagates_to')['status']=='unknown'


def test_trace_mapper_keeps_internal_edges_separate_from_service_calls():
    _,_,_,payload,inv=fixture()
    trace=payload['data'][0]
    parent=trace['spans'][0]
    client=dict(parent,spanID='client',startTime=1000100000,duration=1500000,
                references=[{'refType':'CHILD_OF','traceID':'t','spanID':'parent'}])
    trace['spans'].append(client)
    trace['spans'][1]['references'][0]['spanID']='client'
    mapped=trace_evidence(payload,'c',inv,'fixture')
    assert len(mapped['edges'])==1
    assert len(mapped['internal_span_edges'])==1
    assert mapped['internal_span_edges'][0]['kind']=='internal_span'
    assert mapped['internal_span_edges'][0]['source']==mapped['internal_span_edges'][0]['target']
