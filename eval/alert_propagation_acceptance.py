"""Evaluate real captured probe observations against trace association checks."""
import argparse
import json
from pathlib import Path
from agents.alert_compression import compress_alerts
from agents.alert_context import collect_traces, trace_evidence, qualify_context, recall_context
from agents.alert_semantics import build_scag


def evaluate_statistics(root, inventory):
    captured=json.loads((root/'statistics-observations.json').read_text())
    rows=[]
    for observation in captured['observations']:
        leaf=observation
        while leaf.get('dependency'): leaf=leaf['dependency']
        for sample in (leaf, observation):
            rows.append(dict(name='HTTPFailureIndicator',source='http_probe',cluster_id='default',
                namespace=inventory['pods'][0]['namespace'],pod=sample['pod'],timestamp=sample['end_ns']/1e9,
                metric_name='http_request_failed',metric_value=int(sample['code']>=500),unit='boolean',
                trace_id=sample['trace_id'],span_id=sample['span_id']))
    result=compress_alerts(rows,cluster_id='default')
    assert len(result['groups'])==2
    groups={g['entity']:g for g in result['groups']}
    dependency,caller=groups[leaf['pod']],groups[observation['pod']]
    result['context_evidence']={'traces':trace_evidence(captured['trace'],'default',inventory,'live_jaeger_probe')}
    result['relations']=[dict(left=dependency['group_id'],right=caller['group_id'],type='propagates_to')]
    build_scag(result)
    edge=result['scag']['edges'][0]
    check=edge['checks']['statistics']
    assert check['status']=='pass' and check['aligned_samples']==12 and check['coverage']==1,check
    assert check['max_offset_s']>0 and check['pearson']==1,check
    assert edge['status']=='evidence_supported' and not edge['causal_confirmed'],edge
    assert [r['raw'] for r in result['raw_alerts']]==rows
    exact=len({r['timestamp'] for r in rows if r['pod']==leaf['pod']} & {r['timestamp'] for r in rows if r['pod']==observation['pod']})
    assert exact==0
    (root/'statistics-graph.json').write_text(json.dumps(result,indent=2))
    return dict(passed=True,requests=12,raw_numeric_observations=len(rows),exact_timestamp_matches=exact,
                statistics=check,graph_status=edge['status'],causal_confirmed=False,
                source='actual HTTP response status observations; not Prometheus series or autonomous RCA')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--jaeger-url',required=True)
    args=parser.parse_args()
    inventory=json.loads((args.evidence/'inventory.json').read_text())
    ns=inventory['pods'][0]['namespace']
    reports=[]
    for phase in ('healthy','dependency_failed','recovered'):
        observation=json.loads((args.evidence/(phase+'.observation.json')).read_text())
        trace=json.loads((args.evidence/(phase+'.trace.json')).read_text())
        leaf=observation
        while leaf.get('dependency'): leaf=leaf['dependency']
        samples=[leaf,observation]  # Intermediate services deliberately have no alert input.
        records=[dict(name='HTTPResponse',source='logs',cluster_id='default',namespace=ns,pod=s['pod'],
            timestamp=s['end_ns']/1e9,message=f"HTTP response {s['code']}",severity='critical' if s['code']>=500 else 'info',
            trace_id=s['trace_id'],span_id=s['span_id']) for s in samples]
        result=compress_alerts(records,cluster_id='default',namespace=ns)
        result['cluster_id']='default'
        context=trace_evidence(trace,'default',inventory,'live_jaeger_probe')
        assert len(context['edges'])==len(inventory['pods'])-1,context
        result['context_evidence']={'traces':context}
        by_entity={g['entity']:g for g in result['groups']}
        dependency,caller=[by_entity[s['pod']] for s in samples]
        qualified=qualify_context(result,dependency,caller,'propagates_to')
        assert qualified['status']==('pass' if phase=='dependency_failed' else 'unknown'),qualified
        assert qualify_context(result,caller,dependency,'propagates_to')['status']=='unknown'
        recall_context(result)
        if len(inventory['pods']) == 2 or phase == 'dependency_failed':
            assert len(result['candidate_pairs'])==1
        else:
            assert not result['candidate_pairs']
        # Explicit test hypothesis, not a model-generated or confirmed root cause.
        result['relations']=[dict(left=dependency['group_id'],right=caller['group_id'],type='propagates_to')]
        build_scag(result)
        edge=result['scag']['edges'][0]
        assert not edge['causal_confirmed'] and edge['status']=='unverified'
        assert [r['raw'] for r in result['raw_alerts']]==records
        if phase=='dependency_failed':
            assert len(qualified['propagation_path']) == len(inventory['pods'])
            assert len(qualified['trace_refs']) == inventory.get('expected_spans', len(inventory['pods']))-1
            collected=collect_traces(args.jaeger_url,result,inventory)
            assert collected['status']=='mapped',collected
            assert set(collected['queried_services'])=={s['pod'] for s in samples}
            collected_result=dict(result,context_evidence={'traces':collected})
            live_qualified=qualify_context(collected_result,dependency,caller,'propagates_to')
            assert live_qualified['status']=='pass'
            assert len(live_qualified['propagation_path'])==len(inventory['pods'])
            assert any(e['trace_id']==observation['trace_id'] and e['callee_error'] is True for e in collected['edges'])
            (args.evidence/'live-collector.json').write_text(json.dumps(collected,indent=2))
        (args.evidence/(phase+'.graph.json')).write_text(json.dumps(result,indent=2))
        reports.append(dict(phase=phase,http_status=observation['code'],call_source=context['edges'][0]['source'],call_target=context['edges'][0]['target'],
            propagation_status=qualified['status'],propagation_direction=qualified['propagation_direction'],propagation_path=qualified['propagation_path'],span_links=len(qualified['trace_refs']),
            graph_status=edge['status'],statistics=edge['checks']['statistics']['status'],causal_confirmed=edge['causal_confirmed']))
    report=dict(passed=True,cases=reports,raw_exactly_preserved=True,model_calls=0,
                scope='real HTTP requests and Jaeger ingestion; explicit hypothesis qualification, not end-to-end autonomous RCA')
    if (args.evidence/'statistics-observations.json').exists():
        report['statistics_acceptance']=evaluate_statistics(args.evidence,inventory)
    (args.evidence/'acceptance-report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report),flush=True)


if __name__=='__main__':main()
