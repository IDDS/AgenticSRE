"""Evidence-backed review suggestions; never mutate the alert partition."""
import time
from collections import Counter

from agents.alert_model_evidence import metric_context


def build_merge_review(result, as_of=None):
    now = time.time() if as_of is None else as_of
    started = result.get('collection_started_at')
    report = {'status': 'ineligible', 'suggestions': [], 'rejected': {},
              'automatic_merge': False, 'requires_human_review': True,
              'interpretation': 'association checks support review, not shared-cause confirmation'}
    result['merge_review'] = report
    if (result.get('origin') != 'live_scan' or result.get('history') or result.get('degraded')
            or result.get('collection') != {'prometheus': 'ok', 'kubectl': 'ok'}
            or result.get('scag', {}).get('status') != 'ready'):
        return report
    if not isinstance(started, (int, float)) or not started <= now <= started + result['time_window']:
        report['status'] = 'expired'
        return report
    report.update(status='ready', valid_until=started + result['time_window'])
    groups = {g['group_id']: g for g in result['groups']}
    edges = result['scag'].get('edges', [])
    conflicts = {frozenset((e['left'], e['right'])) for e in edges
                 if e.get('type') == 'conflicts' or e.get('status') == 'contradicted'}
    seen, rejected = set(), Counter()
    metrics = {}
    for edge in edges:
        if edge.get('type') not in ('equivalent', 'shared_root'):
            continue
        keys = (edge['left'], edge['right'])
        pair = frozenset(keys)
        if len(pair) != 2 or any(k not in groups for k in keys):
            rejected['invalid_pair'] += 1
            continue
        if pair in conflicts:
            rejected['conflicting_evidence'] += 1
            continue
        if pair in seen:
            continue
        checks = edge.get('checks', {})
        if edge.get('status') != 'evidence_supported' or any(
                checks.get(k, {}).get('status') != 'pass' for k in ('structure', 'time', 'statistics')):
            rejected['incomplete_checks'] += 1
            continue
        a, b = (groups[k] for k in keys)
        if any(a[k] != b[k] or not a[k] for k in ('cluster_id', 'namespace', 'entity_kind', 'entity')):
            rejected['different_object'] += 1
            continue
        rows = [result['raw_alerts'][i] for g in (a, b) for i in g['alert_indices']]
        from agents.alert_identity import compatible_members
        if not compatible_members(rows):
            rejected['different_incarnation'] += 1
            continue
        if any(r['source'] != 'prometheus' or r['state'] != 'firing' or r['timestamp'] is None for r in rows):
            rejected['ineligible_members'] += 1
            continue
        dimensions = [{k: v for k, v in r['labels'].items()
                       if k not in ('__name__', 'alertname', 'alertstate', 'severity')} for r in rows]
        if any(d != dimensions[0] for d in dimensions):
            rejected['different_dimensions'] += 1
            continue
        if max(r['timestamp'] for r in rows) - min(r['timestamp'] for r in rows) > result['time_window']:
            rejected['outside_window'] += 1
            continue
        for g in (a, b):
            if g['group_id'] not in metrics:
                metrics[g['group_id']] = metric_context(result, g)
        evidence = [metrics[k] for k in keys]
        if any(e.get('status') != 'ready' or not e.get('evidence_id') or not e.get('query') for e in evidence):
            rejected['missing_metric_evidence'] += 1
            continue
        if evidence[0]['query'] == evidence[1]['query']:
            rejected['same_measurement'] += 1
            continue
        seen.add(pair)
        report['suggestions'].append({'group_ids': list(keys), 'relation_type': edge['type'],
            'reason': edge.get('reason', ''), 'checks': {k: {'status': checks[k]['status'],
                'reason': checks[k].get('reason', '')} for k in ('structure', 'time', 'statistics')},
            'metric_evidence_ids': [e['evidence_id'] for e in evidence],
            'alert_count': len(rows), 'causal_confirmed': False,
            'warning': '同对象的独立故障也可能同时发生；请核对原始证据后填写合并原因。'})
    report['rejected'] = dict(rejected)
    # Pairwise suggestions intentionally do not imply transitive event membership.
    return report
