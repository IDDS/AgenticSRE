"""Score frozen engineering evidence; missing business evidence never passes.

Live labels describe compatible observations under the probe protocol, not
natural incident membership or root causes. Repeated scans are not incidents.
"""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import statistics


def pairs(count):
    return count*(count-1)//2


def score_live_scan(scan, phase, namespace, expected):
    raw, groups = scan.get('raw_alerts',[]), scan.get('groups',[])
    indexes = [i for group in groups for i in group.get('alert_indices',[])]
    allowed_raw=expected[0] if isinstance(expected[0],list) else [expected[0]]
    checks = dict(expected_counts=len(raw) in allowed_raw and len(groups)==expected[1],
        complete_partition=all(type(i) is int for i in indexes) and sorted(indexes)==list(range(len(raw))),
        collection_complete=not scan.get('degraded') and
            all(scan.get('collection',{}).get(k)=='ok' for k in ('prometheus','kubectl')))
    if not checks['complete_partition']:
        return dict(passed=False,checks=checks)
    labels=[]
    critical=[]
    identity_ok=True
    for i,row in enumerate(raw):
        source=row.get('raw',{})
        observed=source.get('labels',{})
        pod, name = observed.get('pod'),source.get('name')
        identity_ok &= (pod in {namespace+'-'+role for role in ('a','b','c')}
                        and observed.get('namespace')==namespace and row.get('entity')==pod
                        and row.get('cluster_id')=='default' and row.get('source')=='prometheus'
                        and row.get('name')==name and row.get('namespace')==namespace)
        identity_ok &= name in {'ProbeLatency100','ProbeLatency200','ProbeLatency300','ProbeError','ProbeIndependentCritical'}
        kind='latency' if isinstance(name,str) and name.startswith('ProbeLatency') else 'error'
        # Missing trace is a conservative compatibility boundary, not proof
        # that these observations have different real-world root causes.
        labels.append((pod,kind) if phase in ('disjoint-requests-same-pod','missing-traces') else (pod,))
        if name in ('ProbeLatency300','ProbeIndependentCritical'):
            critical.append(i)
            identity_ok &= row.get('severity')=='critical'
    predicted=sum(pairs(len(g['alert_indices'])) for g in groups)
    actual=sum(pairs(n) for n in Counter(labels).values())
    tp=sum(sum(pairs(n) for n in Counter(labels[i] for i in g['alert_indices']).values()) for g in groups)
    checks.update(source_identity=bool(identity_ok), no_false_merge=predicted==tp, no_missed_compatible_pair=actual==tp,
        critical_visible=all(any(i in g['alert_indices'] and g.get('severity')=='critical' for g in groups) for i in critical),
        no_model_calls=scan.get('semantic',{}).get('model_calls',0)==0)
    return dict(passed=all(checks.values()),checks=checks,input=len(raw),groups=len(groups),
        true_positive_pairs=tp,false_merge_pairs=predicted-tp,missed_compatible_pairs=actual-tp,
        pair_precision=tp/predicted if predicted else None,pair_recall=tp/actual if actual else None,
        critical=len(critical))


def assess(contract, baseline, candidate, live=None, scans=None):
    gates=[]
    def add(name,status,reason,**details):
        gates.append(dict(name=name,status=status,reason=reason,**details))
    expected=contract['synthetic_cases']
    required=('partition_valid','raw_exactly_preserved','order_invariant',
              'sampled_peak_retention','sampled_critical_representatives')
    for arm,report in [('baseline',baseline),('candidate',candidate)]:
        if report is None:
            add('grouping_'+arm,'blocked','missing labelled grouping report');continue
        rows=report.get('cases',[])
        valid=(len(rows)==len(expected) and {r.get('case') for r in rows}==set(expected))
        valid &= all(r.get('input')==expected.get(r.get('case'))
            and all(r.get(k) is True for k in required)
            and r.get('false_merge_pairs')==r.get('missed_compatible_pairs')==0
            and r.get('critical_input')==r.get('critical_visible_in_groups')
            and r.get('provenance')=='controlled_synthetic' for r in rows)
        add('grouping_'+arm,'passed' if valid else 'failed','labelled deterministic cases only',
            records=sum(r.get('input',0) for r in rows),cases=len(rows))
    if baseline is not None and candidate is not None:
        before={r.get('case'):r.get('dataset_sha256') for r in baseline.get('cases',[])}
        after={r.get('case'):r.get('dataset_sha256') for r in candidate.get('cases',[])}
        equal=bool(before) and before==after and all(isinstance(v,str) and len(v)==64 for v in before.values())
        add('paired_inputs','passed' if equal else 'failed','both arms must use identical labelled inputs')
    live_details=[]
    if live is None:
        add('live_intake','blocked','missing actual collection run')
    else:
        phases=contract['live_scenarios']
        stages=live.get('stages',[])
        structural=(live.get('status')=='passed' and len(stages)==len(phases)
                    and {s.get('phase') for s in stages}==set(phases))
        namespace=live.get('namespace','')
        for phase,counts in phases.items():
            stage=next((s for s in stages if s.get('phase')==phase),{})
            structural &= (len(stage.get('scans',[]))==contract['scans_per_live_scenario']
                           and type(stage.get('requests')) is int and stage['requests']>0
                           and stage.get('request_errors')==[])
            for sample in range(contract['scans_per_live_scenario']):
                scan=(scans or {}).get((phase,sample))
                if scan is None:
                    structural=False
                    live_details.append(dict(phase=phase,sample=sample,passed=False,reason='missing scan'));continue
                scored=score_live_scan(scan,phase,namespace,counts)
                structural &= scored['passed']
                live_details.append(dict(phase=phase,sample=sample,**scored))
        add('live_intake','passed' if structural else 'failed','controlled live HTTP/Prometheus/Jaeger observations',
            scans=len(live_details),requests=sum(s.get('requests',0) for s in stages))
        clean=all(live.get(k) is True for k in ('production_lifecycle_unchanged','namespace_removed','containers_cleaned'))
        add('isolation_cleanup','passed' if clean else 'failed','production lifecycle and experiment resource checks')
    # These gates require separately collected, independently labelled evidence.
    # The engineering reports accepted above cannot certify them.
    for name,reason in [
        ('production_quality','independent incident membership, candidate and root labels are not supplied'),
        ('acceptance_thresholds','customer quality margins, sample size and load SLO are not frozen'),
        ('full_rca_and_cost','no paired full diagnosis or real provider usage in this run'),
        ('released_version','candidate-only run; production release acceptance is separate')]:
        add(name,'blocked',reason)
    engineering=[g for g in gates if g['name'] not in ('production_quality','acceptance_thresholds','full_rca_and_cost','released_version')]
    return dict(verdict='not_accepted',engineering_passed=all(g['status']=='passed' for g in engineering),
        candidate_image=contract['candidate_image'],root_accuracy_target=contract['production_quality']['root_accuracy_target'],
        production_root_accuracy=None,natural_incidents=0,gates=gates,live_scores=live_details,
        scope='engineering acceptance on frozen synthetic and controlled-live inputs; not production quality approval')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--live-dir',type=Path)
    parser.add_argument('--contract',default='contract.json')
    parser.add_argument('--output',default='acceptance-verdict.json')
    args=parser.parse_args()
    root=args.root
    contract_path=root/args.contract
    body=contract_path.read_bytes()
    assert hashlib.sha256(body).hexdigest()==contract_path.with_suffix('.sha256').read_text().strip()
    contract=json.loads(body)
    def read(path):return json.loads(path.read_text()) if path.exists() else None
    live_dir=args.live_dir
    if live_dir is None:
        found=[p.parent for p in root.glob('????????/report.json')]
        if len(found)>1:parser.error('multiple campaigns: specify --live-dir; do not silently select a passing run')
        live_dir=found[0] if found else root/'missing-live'
    live=read(live_dir/'report.json')
    scans={(phase,i):value for phase in contract['live_scenarios'] for i in range(contract['scans_per_live_scenario'])
           if (value:=read(live_dir/f'{phase}-{i}-scan.json')) is not None}
    result=assess(contract,read(root/'grouping-baseline/report.json'),read(root/'grouping-candidate/report.json'),live,scans)
    durations=[r['scan_seconds'] for s in (live or {}).get('stages',[]) for r in s.get('scans',[])]
    result['live_scan_latency_seconds']=(dict(samples=len(durations),p50=statistics.median(durations),
        p95=sorted(durations)[math.ceil(.95*len(durations))-1],interpretation='observed small-sample scan latency, not load SLO') if durations else None)
    result['contract_sha256']=hashlib.sha256(body).hexdigest()
    result['live_directory']=live_dir.name
    (root/args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k!='live_scores'},ensure_ascii=False,indent=2))
    raise SystemExit(1 if any(g['status']=='failed' for g in result['gates']) else 2)


if __name__=='__main__':main()
