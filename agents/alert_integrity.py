"""Validate the complete partition before publishing or revising a scan."""
import hashlib
import json


def partition_receipt(result):
    rows,groups=result['raw_alerts'],result['groups']
    ids=[r['alert_id'] for r in rows]
    if len(set(ids))!=len(ids):
        raise ValueError('duplicate original alert identity')
    covered=[]
    gids=set()
    ranks={'critical':4,'high':3,'warning':2,'info':1}
    for group in groups:
        indices=group['alert_indices']
        if (not indices or any(type(i) is not int or not 0<=i<len(rows) for i in indices)
                or group['group_id'] in gids):
            raise ValueError('invalid compression partition')
        gids.add(group['group_id'])
        members=[rows[i] for i in indices]
        if (group['alert_ids']!=[r['alert_id'] for r in members]
                or type(group['alert_count']) is not int or group['alert_count']!=len(members)
                or group['representative_alert_id'] not in group['alert_ids']
                or any((r['cluster_id'],r['namespace'])!=(group['cluster_id'],group['namespace']) for r in members)
                or ranks.get(group['severity'],0)!=max(ranks.get(r['severity'],0) for r in members)):
            raise ValueError('inconsistent compression membership, scope or severity')
        covered.extend(indices)
    if sorted(covered)!=list(range(len(rows))):
        raise ValueError('group membership must cover every original alert exactly once')
    for key,expected in [('total_alerts',len(rows)),('retained_alerts',len(rows)),('omitted_alerts',0),('compressed_groups',len(groups))]:
        if key in result and (type(result[key]) is not int or result[key]!=expected):
            raise ValueError('inconsistent compression counters')
    def digest(value):
        return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    return {'schema':'alert-partition-integrity/v1','status':'passed','original_alerts':len(rows),
        'partition_members':len(covered),'groups':len(groups),'omitted_alerts':0,
        'raw_sha256':digest(rows),'partition_sha256':digest([{'group_id':g['group_id'],'alert_ids':g['alert_ids']} for g in groups]),
        'interpretation':'structural conservation and severity checks; not semantic or causal accuracy'}
