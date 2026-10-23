"""Opt-in, independently reconstructed request-window observations.

Window aggregates are never equated to one representative Span. An observed
cohort identifies co-observation, not a shared root cause or suppression policy.
"""
import copy
import json
import math
import re
import time
from collections import Counter

import requests

from agents.alert_compression import digest, make_group, regroup_stats
from agents.alert_context import trace_evidence
from agents.alert_semantics import annotate_groups, entity_ref, number
from agents.alert_thresholds import _THRESHOLD


def collect_window_proofs(result, prometheus_url, jaeger_url, inventory, fetch_json=None):
    report = {'status': 'ineligible', 'proofs': [], 'rejected': {}, 'rejection_details': []}
    if (result.get('origin') != 'live_scan' or result.get('history') or result.get('degraded')
            or result.get('collection') != {'prometheus':'ok', 'kubectl':'ok'}
            or result.get('metric_evidence', {}).get('provenance') != 'live_prometheus_rule'
            or number(result.get('collection_started_at')) is None):
        return report
    buckets = {}
    for group in result['groups']:
        m = result.get('metric_evidence', {}).get('groups', {}).get(group['group_id'], {})
        spec = m.get('request_window', {})
        if (m.get('status') != 'ready' or m.get('provenance') != 'live_prometheus_rule'
                or m.get('query_basis') != 'simple_comparison_operand' or group['entity_kind'] != 'pod'
                or spec.get('field') not in ('mean_duration_ms', 'error_ratio')
                or any(m.get(k) != group[k] for k in ('cluster_id','namespace','entity_kind','entity'))
                or set(m.get('alert_ids', [])) != set(group['alert_ids'])):
            continue
        rows = [result['raw_alerts'][i] for i in group['alert_indices']]
        dimensions = [{k:v for k,v in r['labels'].items() if k not in ('__name__','alertname','severity')} for r in rows]
        if (not rows or any(r['source'] != 'prometheus' or r['state'] != 'firing' or r['timestamp'] is None for r in rows)
                or any(d != dimensions[0] for d in dimensions)
                or any(k in dimensions[0] for k in ('trace_id','span_id'))):
            continue
        shared = {k:v for k,v in spec.items() if k != 'field'}
        key = digest([group['cluster_id'],group['namespace'],group['entity'],dimensions[0],shared])
        buckets.setdefault(key, []).append((group, m))
    report['status'] = 'ready'
    rejected = Counter()
    deadline = time.monotonic()+25

    def get(backend, path, params):
        if time.monotonic() >= deadline:
            raise ValueError('collection deadline exceeded')
        if fetch_json:
            return fetch_json(backend, path, params)
        base = prometheus_url if backend == 'prometheus' else jaeger_url
        if not base:
            raise ValueError('backend unavailable')
        response = requests.get(base.rstrip('/')+path, params=params, timeout=min(5, max(.1, deadline-time.monotonic())))
        response.raise_for_status()
        return response.json()

    for bucket in list(buckets.values())[:3]:
        if {m['request_window']['field'] for _,m in bucket} != {'mean_duration_ms','error_ratio'} or len(bucket) != 2:
            continue
        diagnostics = {'group_ids': [g['group_id'] for g, _ in bucket]}
        try:
            group, first = bucket[0]
            spec = first['request_window']
            service_name = spec.get('service', group['entity'])
            if (not isinstance(service_name, str) or not service_name.strip()
                    or len(service_name) > 256 or any(ord(c) < 32 for c in service_name)):
                raise ValueError('invalid trace service mapping')
            pods = [p for p in inventory.get('pods', [])
                    if p.get('namespace') == group['namespace'] and p.get('name') == group['entity']]
            if len(pods) != 1 or not isinstance(pods[0].get('uid'), str) or not pods[0]['uid']:
                raise ValueError('current Pod UID unavailable or ambiguous')
            pod_uid = pods[0]['uid']

            window = number(spec.get('seconds'))
            if window is None or not 1 <= window <= 60 or not isinstance(spec.get('operation'), str) or not spec['operation']:
                raise ValueError('invalid window contract')
            if any(not isinstance(spec.get(k), str) or not re.fullmatch(r'[a-zA-Z_:][a-zA-Z0-9_:]*', spec[k]) for k in ('count_metric','end_metric')):
                raise ValueError('invalid companion metric')
            labels = {k:v for k,v in first.get('series_labels', {}).items() if k != '__name__'}
            if set(labels)-{'namespace','pod','job','instance','cluster_id'}:
                raise ValueError('unsupported request partition dimensions')
            if labels.get('namespace') != group['namespace'] or labels.get('pod') != group['entity']:
                raise ValueError('companion scope mismatch')
            if any({k:v for k,v in m.get('series_labels', {}).items() if k!='__name__'} != labels for _,m in bucket):
                raise ValueError('measurement dimensions differ')
            points = [m.get('points', []) for _,m in bucket]
            if any(len(p)<5 for p in points) or points[0][-1][0] != points[1][-1][0]:
                raise ValueError('measurement anchors differ')
            anchor = number(points[0][-1][0])
            end = result['collection_started_at']
            if anchor is None or not end-2 <= anchor <= end:
                raise ValueError('stale measurement anchor')
            selector = '{'+','.join(k+'='+json.dumps(v) for k,v in sorted(labels.items()))+'}'
            # Fetch all four values in one PromQL evaluation. Separate queries at
            # the same evaluation time can observe different scrape commits.
            fields = {m['request_window']['field']:m for _,m in bucket}
            expressions = {field: m['query'] for field,m in fields.items()}
            expressions.update({key: spec[key]+selector for key in ('count_metric','end_metric')})
            expressions.update({'source_time_'+field: 'timestamp(('+expr+'))'
                                for field,expr in list(expressions.items())})
            role_label = '__agenticsre_window_field'
            query = ' or '.join('label_replace(('+expr+'), '+json.dumps(role_label)+', '+
                                json.dumps(field)+', "", "")' for field,expr in expressions.items())
            initial_anchor = anchor
            excluded_snapshot = None
            for attempt in range(2):
                response = get('prometheus','/api/v1/query',{'query':query,'time':anchor})
                data = response.get('data', {})
                series = data.get('result', [])
                if (response.get('status') != 'success' or response.get('warnings')
                        or data.get('resultType') != 'vector' or len(series) != 8):
                    raise ValueError('atomic window snapshot unavailable')
                values = {}
                for item in series:
                    metric = item.get('metric', {})
                    field = metric.get(role_label)
                    if (field not in expressions or field in values
                            or {k:v for k,v in metric.items() if k not in ('__name__',role_label)} != labels):
                        raise ValueError('atomic window snapshot scope mismatch')
                    stamp, value = map(number, item.get('value', []))
                    if stamp != anchor or value is None:
                        raise ValueError('invalid atomic window sample')
                    values[field] = value
                source_timestamps = {field.removeprefix('source_time_'):values.pop(field)
                                     for field in list(values) if field.startswith('source_time_')}
                companions = {key: values[key] for key in ('count_metric','end_metric')}
                snapshot = dict(evaluation_at=anchor, query=query, values=values, labels=labels,
                                source_timestamps=source_timestamps,
                                evidence_id='window-'+digest([query,anchor,values,labels,source_timestamps]))
                stop, count = companions['end_metric'], companions['count_metric']
                diagnostics.update(evaluation_at=anchor, window_end=stop,
                                   window_age_seconds=anchor-stop, request_count=count,
                                   source_timestamps=source_timestamps, initial_evaluation_at=initial_anchor,
                                   excluded_inflight_snapshot=excluded_snapshot,
                                   allowed_window_end=[initial_anchor-2,anchor], allowed_request_count=[2,200])
                # Query evaluation timestamps do not identify the original samples.
                # Reject lookback values from a different scrape, even if numbers match.
                if any(stamp > anchor for stamp in source_timestamps.values()):
                    raise ValueError('window source sample is in the future')
                if any(stamp < initial_anchor-2 for stamp in source_timestamps.values()):
                    raise ValueError('window source samples are stale')
                if len(set(source_timestamps.values())) != 1:
                    raise ValueError('window samples are not synchronized')
                if stop > anchor:
                    if attempt == 0:
                        previous_anchor = (math.floor(min(source_timestamps.values())*1000)-1)/1000
                        if initial_anchor-2 <= previous_anchor < anchor:
                            excluded_snapshot = snapshot
                            anchor = previous_anchor
                            continue
                    raise ValueError('request window ends in the future')
                if stop < initial_anchor-2:
                    raise ValueError('request window is stale')
                if count != int(count):
                    raise ValueError('window request count is not an integer')
                if count < 2:
                    raise ValueError('insufficient requests for window reconstruction')
                if count > 200:
                    raise ValueError('window request count exceeds coverage limit')
                if excluded_snapshot:
                    snapshot['selection'] = 'previous_completed_scrape'
                    snapshot['initial_evaluation_at'] = initial_anchor
                    snapshot['excluded_inflight_snapshot'] = excluded_snapshot
                break
            response = get('jaeger', '/api/traces', {'service':service_name, 'operation':spec['operation'],
                'tags':json.dumps({'k8s.pod.uid':pod_uid}),
                'start':int((stop-window-result['time_window'])*1e6), 'end':int(stop*1e6), 'limit':201})
            traces = response.get('data')
            if not isinstance(traces, list) or len(traces)>=201 or response.get('errors'):
                raise ValueError('trace response incomplete')
            spans, seen = [], set()
            ref = entity_ref(group['cluster_id'],group['namespace'],'pod',group['entity'])
            for trace in traces:
                observed = trace_evidence({'data':[trace]},group['cluster_id'],inventory,'live_jaeger')
                if observed['truncated']:
                    raise ValueError('trace response incomplete')
                raw = {s.get('spanID'):s for s in trace.get('spans', [])}
                for span in observed['observed_spans']:
                    source = raw.get(span['span_id'], {})
                    tags = {t.get('key'):t.get('value') for t in source.get('tags', []) if isinstance(t, dict)}
                    service = trace.get('processes', {}).get(source.get('processID'), {}).get('serviceName')
                    if (span['entity_ref'] != ref or source.get('operationName') != spec['operation']
                            or service != service_name or tags.get('span.kind') != 'server'
                            or not stop-window < span['end'] <= stop):
                        continue
                    key = (span['trace_id'],span['span_id'])
                    if key in seen:
                        raise ValueError('duplicate observed request')
                    seen.add(key)
                    spans.append(span)
            if len(spans) != count:
                raise ValueError('request coverage mismatch')
            if any(s['error'] is not True or s['http_status_code'] is None or s['http_status_code'] < 500 for s in spans):
                raise ValueError('request cohorts are not jointly failed')
            delay = fields['mean_duration_ms']
            rules = [r['rule'] for r in delay.get('rule_references', [])] or [delay['rule']]
            comparisons = [_THRESHOLD.fullmatch(r['expression']) for r in rules]
            if any(not c or c[2] not in ('>','>=') or number(c[3]) is None for c in comparisons):
                raise ValueError('unsupported latency threshold')
            if any(not (s['duration_ms'] > float(c[3]) if c[2]=='>' else s['duration_ms'] >= float(c[3])) for s in spans for c in comparisons):
                raise ValueError('slow and failed request cohorts differ')
            mean = sum(s['duration_ms'] for s in spans)/len(spans)
            diagnostics.update(reconstructed_mean_ms=mean, measured_mean_ms=values['mean_duration_ms'])
            if not math.isclose(mean, values['mean_duration_ms'], rel_tol=0, abs_tol=.01):
                raise ValueError('mean duration reconstruction mismatch')
            if values['error_ratio'] != 1:
                raise ValueError('error ratio reconstruction mismatch')
            report['proofs'].append({'group_ids':[g['group_id'] for g,_ in bucket],
                'measurement_evidence_ids':[m['evidence_id'] for _,m in bucket],
                'basis':'fully_reconstructed_joint_failed_request_window', 'causal_confirmed':False,
                'start':stop-window,'end':stop,'evaluation_at':anchor,'request_count':int(count),
                'trace_service':service_name,'pod_uid':pod_uid,'measurement_snapshot':snapshot,
                'mean_duration_ms':mean,'error_ratio':1,'spans':spans,'companion_metrics':companions,
                'provenance':{'measurements':'live_prometheus_rule','requests':'live_jaeger'}})
        except Exception as exc:
            reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            rejected[reason] += 1
            report['rejection_details'].append(dict(reason=reason, **diagnostics))
    if len(buckets)>3:
        rejected['window collection entity cap'] += len(buckets)-3
    report['rejected'] = dict(rejected)
    return report


def merge_window_observations(result, evidence):
    # Only called with evidence produced in the live collector pipeline.
    if (result.get('origin') != 'live_scan' or result.get('history') or result.get('degraded')
            or result.get('collection') != {'prometheus':'ok','kubectl':'ok'} or evidence.get('status') != 'ready'):
        return False
    metrics = result['metric_evidence']
    replacements, removed, rebound = [], set(), {}
    for proof in evidence.get('proofs', []):
        originals = [g for g in result['groups'] if g['group_id'] in proof['group_ids']]
        if len(originals)!=2 or any(g['group_id'] in removed for g in originals):
            continue
        measurements = [metrics['groups'][g['group_id']] for g in originals]
        if {m['evidence_id'] for m in measurements} != set(proof['measurement_evidence_ids']):
            continue
        indices = [i for g in originals for i in g['alert_indices']]
        from agents.alert_identity import compatible_members
        if not compatible_members([result['raw_alerts'][i] for i in indices]):
            continue
        stamps = [result['raw_alerts'][i]['timestamp'] for i in indices]
        if max(stamps)-min(stamps) > result['time_window']:
            continue
        group = make_group(result['raw_alerts'],indices,'verified_request_window')
        group['event_merge'] = {'basis':proof['basis'],'causal_confirmed':False,
            'window':copy.deepcopy(proof),'original_groups':copy.deepcopy(originals)}
        rebound[group['group_id']] = {'status':'composite','provenance':'live_prometheus_rule',
            'interpretation':'separate window measurements, jointly observed requests; not root cause confirmation',
            'measurements':copy.deepcopy(measurements),'alert_ids':group['alert_ids']}
        replacements.append(group)
        removed.update(g['group_id'] for g in originals)
    if not replacements:
        return False
    before = len(result['groups'])
    result['groups'] = [g for g in result['groups'] if g['group_id'] not in removed]+replacements
    metrics['groups'] = {k:v for k,v in metrics['groups'].items() if k not in removed}|rebound
    metrics.setdefault('source_ready_groups', metrics.get('ready_groups',0))
    metrics.update(ready_groups=sum(m['status']=='ready' for m in metrics['groups'].values()),
        composite_groups=sum(m['status']=='composite' for m in metrics['groups'].values()),total_groups=len(metrics['groups']))
    result['relations'],result['candidate_pairs'] = [],[]
    result.pop('scag',None);result.pop('merge_review',None)
    annotate_groups(result)
    result['signature_version'] = 5
    for group in replacements:
        group['template_id'] = digest(['request_window_v1',group['cluster_id'],group['namespace'],group['entity'],
            sorted([(m['query'],m['request_window']) for m in rebound[group['group_id']]['measurements']],key=lambda item:item[0])])
        group['symbolic'].update(kind='event',template='同一批慢且失败请求的窗口观测',merge_guard='verified_request_window')
    result['window_compression'] = dict(status='applied',before_groups=before,after_groups=len(result['groups']),merged_event_groups=len(replacements))
    result['automatic_baseline'] = {'status':'warming_up','reason':'new request window grouping signature requires matching history'}
    regroup_stats(result)
    return True
