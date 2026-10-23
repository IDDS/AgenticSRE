"""Conservative coalescing of verified, nested simple Prometheus thresholds."""
import copy
import re
from agents.alert_metrics import _SELECTOR
from agents.alert_compression import digest, make_group, regroup_stats
from agents.alert_semantics import annotate_groups, number
from agents.alert_identity import compatible_members, group_identity

_THRESHOLD = re.compile(rf'^\s*({_SELECTOR})\s*(>=|<=|>|<)\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s*$')


def merge_nested_thresholds(result):
    """Require live, unique, complete measurement evidence; never infer causation."""
    evidence=result.get('metric_evidence',{})
    if result.get('origin')!='live_scan' or result.get('history') or evidence.get('provenance')!='live_prometheus_rule':
        return False
    rows=result['raw_alerts'];buckets={}
    for group in result['groups']:
        entry=evidence.get('groups',{}).get(group['group_id'],{})
        if entry.get('status')!='ready' or entry.get('query_basis')!='simple_comparison_operand':continue
        match=_THRESHOLD.fullmatch(entry.get('rule',{}).get('expression',''))
        if not match or number(match[3]) is None:continue
        members=[rows[i] for i in group['alert_indices']]
        if not compatible_members(members):continue
        if not members or any(r['source']!='prometheus' or r['state']!='firing' or r['timestamp'] is None for r in members):continue
        identities=[{k:v for k,v in r['labels'].items() if k not in ('__name__','alertname','severity')} for r in members]
        if any(x!=identities[0] for x in identities):continue
        # Exact query and observed labels prevent selector, unit, dimension or exporter mixing.
        key=digest([group['cluster_id'],group['namespace'],group['entity_kind'],group['entity'],group_identity(members)['entity_uid'],
                    match[1],match[2],entry.get('query'),entry.get('series_labels'),identities[0],
                    entry.get('start'),entry.get('end'),entry.get('step_s'),entry.get('points')])
        buckets.setdefault(key,[]).append((group,entry,number(match[3]),match[2]))
    replacements=[];removed=set();new_evidence={}
    for bucket in buckets.values():
        segments=[]
        for item in sorted(bucket,key=lambda x:x[0]['first_seen']):
            if not segments or item[0]['last_seen']-segments[-1][0][0]['first_seen']>result['time_window']:
                segments.append([])
            segments[-1].append(item)
        for segment in segments:
            if len(segment)<2 or len({x[2] for x in segment})<2:continue
            indices=[i for g,*_ in segment for i in g['alert_indices']]
            if max(rows[i]['timestamp'] for i in indices)-min(rows[i]['timestamp'] for i in indices)>result['time_window']:continue
            group=make_group(rows,indices,'verified_nested_thresholds')
            rules=[dict(original_group_id=g['group_id'],rule=copy.deepcopy(e['rule']),threshold=t,operator=op,
                        evidence_id=e.get('evidence_id'),alert_ids=g['alert_ids']) for g,e,t,op in segment]
            group['threshold_merge']={'basis':'same_live_measurement_and_nested_thresholds','rules':rules,
                                      'original_groups':len(segment),'causal_confirmed':False}
            representative=next(e for g,e,*_ in segment if group['representative_alert_id'] in g['alert_ids'])
            rebound=copy.deepcopy(representative);rebound['alert_ids']=group['alert_ids'];rebound['rule_references']=rules
            new_evidence[group['group_id']]=rebound
            removed.update(g['group_id'] for g,*_ in segment);replacements.append(group)
    if not replacements:return False
    retained=[g for g in result['groups'] if g['group_id'] not in removed]
    old_count=len(result['groups'])
    result['groups']=retained+replacements
    evidence['groups']={k:v for k,v in evidence['groups'].items() if k not in removed}|new_evidence
    evidence['source_ready_groups']=evidence.get('ready_groups',0)
    evidence['ready_groups']=sum(v.get('status')=='ready' for v in evidence['groups'].values())
    evidence['total_groups']=len(evidence['groups'])
    result['relations']=[];result['candidate_pairs']=[];result.pop('scag',None)
    annotate_groups(result)
    # Counts for cross-rule groups must not reuse historical single-rule signatures.
    result['signature_version']=3
    for group in replacements:
        group['template_id']=digest(['nested_threshold_v1',group['cluster_id'],group['namespace'],group['entity_kind'],
            group['entity'],sorted(x['rule']['expression'] for x in group['threshold_merge']['rules'])])
        group['symbolic'].update(kind='event',template='同一测量的嵌套阈值告警',merge_guard='verified_nested_thresholds')
    result['automatic_baseline']={'status':'warming_up','reason':'new threshold grouping signature requires matching history'}
    result['threshold_compression']={'status':'applied','before_groups':old_count,'after_groups':len(result['groups']),
                                     'merged_event_groups':len(replacements),'original_groups_merged':len(removed)}
    regroup_stats(result)
    return True
