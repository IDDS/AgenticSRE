"""Merge different measurements of one exactly identified, observed failed Span.

This identifies one request observation, never a shared root cause across requests.
"""
import copy
import math
import re

from agents.alert_compression import digest, make_group, regroup_stats
from agents.alert_semantics import annotate_groups, entity_ref, number
from agents.alert_identity import compatible_members, group_identity


def merge_trace_observations(result):
    metrics = result.get('metric_evidence', {})
    traces = result.get('context_evidence', {}).get('traces', {})
    end = number(result.get('collection_started_at'))
    if (result.get('origin') != 'live_scan' or result.get('history') or result.get('degraded')
            or result.get('collection') != {'prometheus': 'ok', 'kubectl': 'ok'} or end is None
            or metrics.get('provenance') != 'live_prometheus_rule'
            or traces.get('provenance') != 'live_jaeger' or traces.get('truncated')):
        return False
    spans = {}
    for span in traces.get('observed_spans', []):
        key = (span.get('trace_id'), span.get('span_id'), span.get('entity_ref'))
        spans.setdefault(key, []).append(span)
    buckets = {}
    for group in result['groups']:
        metric = metrics.get('groups', {}).get(group['group_id'], {})
        field = metric.get('trace_field')
        if (metric.get('status') != 'ready' or metric.get('provenance') != 'live_prometheus_rule'
                or metric.get('query_basis') != 'simple_comparison_operand'
                or field not in ('duration_ms', 'http_status_code')
                or any(metric.get(k) != group[k] for k in ('cluster_id','namespace','entity_kind','entity'))
                or set(metric.get('alert_ids', [])) != set(group['alert_ids']) or group['entity_kind'] != 'pod'):
            continue
        rows = [result['raw_alerts'][i] for i in group['alert_indices']]
        if not compatible_members(rows):
            continue
        if not rows or any(r['source'] != 'prometheus' or r['state'] != 'firing' or r['timestamp'] is None for r in rows):
            continue
        labels = [{k:v for k,v in r['labels'].items() if k not in ('__name__','alertname','severity')} for r in rows]
        if any(x != labels[0] for x in labels):
            continue
        trace_id, span_id = labels[0].get('trace_id', ''), labels[0].get('span_id', '')
        if not isinstance(trace_id, str) or not re.fullmatch(r'(?:[0-9a-fA-F]{16}|[0-9a-fA-F]{32})', trace_id):
            continue
        if not isinstance(span_id, str) or not re.fullmatch(r'[0-9a-fA-F]{16}', span_id):
            continue
        if any(metric.get('series_labels', {}).get(k) != labels[0].get(k) for k in ('trace_id','span_id')):
            continue
        ref = entity_ref(group['cluster_id'], group['namespace'], group['entity_kind'], group['entity'])
        key = (trace_id.lower(), span_id.lower(), ref)
        observed = spans.get(key, [])
        if len(observed) != 1:
            continue
        span = observed[0]
        start, stop, value = number(span.get('start')), number(span.get('end')), number(span.get(field))
        if (span.get('provenance') != 'live_jaeger' or span.get('error') is not True
                or start is None or stop is None or not end-result['time_window'] <= start <= stop <= end
                or value is None):
            continue
        # Repeated samples must describe this single request, not an aggregate or
        # a time series containing values from several different requests.
        points = metric.get('points', [])
        tolerance = .01 if field == 'duration_ms' else 0
        if len(points) < 5 or any(number(p[0]) is None or not stop <= float(p[0]) <= end
                or number(p[1]) is None or not math.isclose(float(p[1]), value, rel_tol=0, abs_tol=tolerance) for p in points):
            continue
        buckets.setdefault((key, digest(labels[0]),group_identity(rows)['entity_uid']), []).append((group, metric, span))
    replacements, removed, new_metrics = [], set(), {}
    for bucket in buckets.values():
        if len({m['trace_field'] for _,m,_ in bucket}) < 2:
            continue
        indices = [i for g,_,_ in bucket for i in g['alert_indices']]
        stamps = [result['raw_alerts'][i]['timestamp'] for i in indices]
        if max(stamps)-min(stamps) > result['time_window']:
            continue
        group = make_group(result['raw_alerts'], indices, 'verified_same_failed_span')
        group['event_merge'] = {'basis':'same_failed_span_and_matching_measurements', 'causal_confirmed':False,
            'span':copy.deepcopy(bucket[0][2]), 'original_groups':[copy.deepcopy(g) for g,_,_ in bucket],
            'measurement_evidence_ids':[m['evidence_id'] for _,m,_ in bucket]}
        new_metrics[group['group_id']] = {'status':'composite', 'provenance':'live_prometheus_rule',
            'interpretation':'different measurements retained separately; no single numeric series for this group',
            'measurements':[copy.deepcopy(m) for _,m,_ in bucket], 'alert_ids':group['alert_ids']}
        replacements.append(group)
        removed.update(g['group_id'] for g,_,_ in bucket)
    if not replacements:
        return False
    before = len(result['groups'])
    result['groups'] = [g for g in result['groups'] if g['group_id'] not in removed]+replacements
    metrics['groups'] = {k:v for k,v in metrics['groups'].items() if k not in removed}|new_metrics
    metrics.setdefault('source_ready_groups', metrics.get('ready_groups', 0))
    metrics['ready_groups'] = sum(m['status']=='ready' for m in metrics['groups'].values())
    metrics['composite_groups'] = sum(m['status']=='composite' for m in metrics['groups'].values())
    metrics['total_groups'] = len(metrics['groups'])
    result['relations'], result['candidate_pairs'] = [], []
    result.pop('scag', None)
    result.pop('merge_review', None)
    annotate_groups(result)
    result['signature_version'] = 4
    for group in replacements:
        group['template_id'] = digest(['same_failed_span_v1',group['cluster_id'],group['namespace'],group['entity'],
            sorted(m['query'] for m in new_metrics[group['group_id']]['measurements'])])
        group['symbolic'].update(kind='event', template='同一失败请求的多指标观测', merge_guard='verified_same_failed_span')
    result['trace_compression'] = {'status':'applied', 'before_groups':before, 'after_groups':len(result['groups']),
        'merged_event_groups':len(replacements), 'original_groups_merged':len(removed)}
    result['automatic_baseline'] = {'status':'warming_up','reason':'new request grouping signature requires matching history'}
    regroup_stats(result)
    return True
