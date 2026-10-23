"""Compact measured facts for hypothesis generation; never a merge authority."""
from agents.alert_semantics import number, statistical_check


def metric_context(result, group):
    entry = result.get('metric_evidence', {}).get('groups', {}).get(group['group_id'], {})
    if entry.get('status') == 'composite':
        originals = group.get('event_merge', {}).get('original_groups', [])
        measurements = entry.get('measurements', [])
        ids = [a for g in originals for a in g.get('alert_ids', [])]
        if (result.get('origin') != 'live_scan' or result.get('history')
                or entry.get('provenance') != 'live_prometheus_rule' or not originals
                or len(ids) != len(set(ids)) or set(ids) != set(group['alert_ids'])
                or any(g.get(k) != group.get(k) for g in originals for k in ('cluster_id','namespace','entity_kind','entity'))
                or len(originals) != len(measurements)):
            return {'status':'unknown','reason':'invalid composite measurement membership'}
        summaries = []
        for original in originals:
            matches = [m for m in measurements if set(m.get('alert_ids', [])) == set(original['alert_ids'])]
            if len(matches) != 1 or matches[0].get('status') != 'ready':
                return {'status':'unknown','reason':'ambiguous composite measurement membership'}
            bound = dict(result, metric_evidence={'groups':{original['group_id']:matches[0]}})
            summary = metric_context(bound, original)
            if summary['status'] != 'ready':
                return {'status':'unknown','reason':'invalid composite measurement evidence'}
            summaries.append(summary)
        return {'status':'composite', 'measurements':summaries,
                'interpretation':'separate units and measurements of verified request observations; no combined numeric series'}
    if (result.get('origin') != 'live_scan' or result.get('history')
            or entry.get('status') != 'ready' or entry.get('provenance') != 'live_prometheus_rule'
            or any(entry.get(k) != group.get(k) for k in ('cluster_id', 'namespace', 'entity_kind', 'entity'))
            or set(entry.get('alert_ids', [])) != set(group['alert_ids'])):
        return {'status': 'unknown', 'reason': 'no matching live measurement evidence'}
    points = entry.get('points', [])
    if len(points) < 5 or any(len(p) != 2 or any(number(v) is None for v in p) for p in points):
        return {'status': 'unknown', 'reason': 'insufficient finite measurement samples'}
    values = [p[1] for p in points]
    # Full-history extrema survive compaction; raw samples remain in the dossier.
    return {'status': 'ready', 'evidence_id': entry.get('evidence_id'),
            'provenance': entry['provenance'], 'query': entry.get('query'),
            'query_basis': entry.get('query_basis'), 'rule': entry.get('rule'),
            'rule_references': entry.get('rule_references', []),
            'series_labels': entry.get('series_labels'), 'start': entry.get('start'),
            'end': entry.get('end'), 'step_s': entry.get('step_s'), 'coverage': entry.get('coverage'),
            'summary': {'samples': len(points), 'first': points[0], 'last': points[-1],
                        'minimum': min(values), 'maximum': max(values), 'constant': len(set(values)) == 1},
            'interpretation': 'measured facts only; co-occurrence and correlation do not prove a shared cause'}


def pair_metric_context(result, left, right):
    members = {g['group_id']: metric_context(result, g) for g in (left, right)}
    if all(e['status'] == 'ready' for e in members.values()):
        checked = statistical_check(result, left, right)
        statistics = {k: checked[k] for k in ('status', 'reason', 'aligned_samples', 'coverage',
                      'pearson', 'metric_evidence_ids', 'interpretation') if k in checked}
    else:
        statistics = {'status': 'unknown', 'reason': 'missing matching live measurement evidence'}
    return {'groups': members, 'statistics': statistics}
