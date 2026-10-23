"""Read-only production observations; run inside the deployed remote container.

Repeated polling is reported separately from natural notification traffic.
No generated alerts, production-store writes or fault injection are performed.
"""
import argparse
import asyncio
from collections import Counter
import json
import time

from agents.alert_agent import AlertAgent
from agents.alert_compression import compress_alerts
from tools import build_tool_registry
from tools.llm_client import LLMClient
from web_app.app import _cluster_scoped_config, _enrich_alert_groups, _llm_configured, _apply_platform_config


def audit(records, result):
    indices = [i for g in result['groups'] for i in g['alert_indices']]
    raw_preserved = [r['raw'] for r in result['raw_alerts']] == records
    partition = sorted(indices) == list(range(len(records)))
    critical = {i for i, row in enumerate(records) if row.get('severity') == 'critical'}
    retained = {i for g in result['groups'] if g['severity'] == 'critical' for i in g['alert_indices'] if i in critical}
    merged=[]
    for g in result['groups']:
        rows=[result['raw_alerts'][i] for i in g['alert_indices']]
        identities={(r['cluster_id'],r['namespace'],r['entity_kind'],r['entity'],r['source'],r['state']) for r in rows}
        if len(rows)>1:
            merged.append(dict(group_id=g['group_id'],name=g['representative'],count=len(rows),
                               identities=[list(x) for x in sorted(identities)]))
    scope_safe=all(len(g['identities'])==1 for g in merged)
    assert raw_preserved and partition and critical==retained and scope_safe
    return dict(input=len(records),groups=len(result['groups']),compression_ratio=result['compression_ratio'],
                raw_exactly_preserved=raw_preserved,partition_valid=partition,critical_input=len(critical),
                critical_retained=len(retained),merged_groups=merged,no_cross_identity_merge=scope_safe,
                sources=dict(Counter(r['source'] for r in records)),
                precision=None,recall=None,quality_note='No independent human incident labels; identity checks are not semantic accuracy.')


async def main(args):
    # Standalone imports do not run the FastAPI startup hook. Apply the same
    # persisted config-center overrides before cloning the effective config.
    _apply_platform_config()
    cfg=_cluster_scoped_config(args.cluster)
    registry=build_tool_registry(cfg,isolated=True)
    agent=AlertAgent(None,registry,cfg)
    snapshots=[]
    for n in range(args.samples):
        started=time.time()
        alerts=await agent._collect_alerts('')
        for alert in alerts:alert.cluster_id=args.cluster
        records=[vars(a) for a in alerts]
        t=time.perf_counter()
        result=await agent.compress_and_recommend(alerts,enrich=False)
        duration=time.perf_counter()-t
        snapshots.append(dict(collected_at=started,collection=dict(agent.collection_status),
                              grouping_seconds=duration,audit=audit(records,result),records=records,result=result))
        print(f'collected sample {n+1}/{args.samples}: {len(records)} alerts',file=__import__('sys').stderr,flush=True)
        if n+1<args.samples:await asyncio.sleep(args.interval)
    records=[r for snapshot in snapshots for r in snapshot['records']]
    started=time.perf_counter()
    pooled=compress_alerts(records,cluster_id=args.cluster,time_window=cfg.alert.time_window)
    pooled_seconds=time.perf_counter()-started
    result=snapshots[-1]['result']
    result.update(origin='live_scan',cluster_id=args.cluster,collection_started_at=snapshots[-1]['collected_at'])
    cfg.observability.prometheus_url=registry.get('prometheus').base_url
    llm=LLMClient(cfg.llm) if _llm_configured(cfg.llm) else None
    started=time.perf_counter()
    await _enrich_alert_groups(result,cfg,llm=llm,live_cluster=args.cluster)
    elapsed=time.perf_counter()-started
    audit(snapshots[-1]['records'],result)
    report=dict(read_only=True,generated_alerts=0,model_configured=llm is not None,
        samples=len(snapshots),interval_seconds=args.interval,time_window=cfg.alert.time_window,
        snapshots=[{k:v for k,v in s.items() if k not in ('records','result')} for s in snapshots],
        repeated_poll_observations=dict(audit=audit(records,pooled),grouping_seconds=pooled_seconds,
            interpretation='Repeated real state observations, not independent alert deliveries or production notification reduction.'),
        enrichment_seconds=elapsed,semantic=result.get('semantic'),retrieval=result.get('retrieval'),
        metric_summary={k:v for k,v in result.get('metric_evidence',{}).items() if k!='groups'},
        graph_status_counts=dict(Counter(e['status'] for e in result.get('scag',{}).get('edges',[]))),
        groups=[dict(name=g['representative'],entity=g['entity'],namespace=g['namespace'],count=g['alert_count']) for g in result['groups']])
    print(json.dumps(dict(report=report,snapshots=snapshots,pooled_result=pooled),ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cluster',default='default')
    parser.add_argument('--samples',type=int,default=3)
    parser.add_argument('--interval',type=float,default=30)
    args=parser.parse_args()
    if not 1<=args.samples<=10 or args.interval<1:parser.error('samples 1..10 and interval >=1 required')
    asyncio.run(main(args))
