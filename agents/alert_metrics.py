"""Read-only metric evidence tied to a unique live Prometheus alert rule."""
import hashlib
import json
import math
import re
import time
import requests
from agents.alert_semantics import number
from agents.alert_metric_adapters import reviewed_measurement

_NAME = r'[a-zA-Z_:][a-zA-Z0-9_:]*'
_LABEL = r'[a-zA-Z_][a-zA-Z0-9_]*\s*(?:=~|!~|!=|=)\s*"(?:[^"\\]|\\.)*"'
_SELECTOR = rf'{_NAME}(?:\s*\{{\s*(?:{_LABEL}(?:\s*,\s*{_LABEL})*\s*,?)?\s*\}})?'
_COMPARISON = re.compile(rf'^\s*({_SELECTOR})\s*(?:>=|<=|==|!=|>|<)\s*[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?\s*$')
_ENTITY_LABELS = {'pod':'pod','instance':'instance','node':'node','service':'service','deployment':'deployment','job':'job_name'}


def measurement_query(rule, group):
    explicit = rule.get('annotations', {}).get('agenticsre_metric_query')
    if explicit:
        if not isinstance(explicit, str) or '{{' in explicit or re.search(r'\bALERTS(?:_FOR_STATE)?\b', explicit):
            return None, 'invalid measurement annotation'
        return explicit, 'rule_annotation'
    adapter = reviewed_measurement(rule.get('query'))
    if adapter:
        return adapter['query'], 'reviewed_expression'
    match = _COMPARISON.fullmatch(rule.get('query', ''))
    if not match:
        return None, 'unsupported rule expression'
    selector=match.group(1).strip()
    if re.match(r'ALERTS(?:_FOR_STATE)?\b', selector):
        return None, 'alert-state series is not measurement evidence'
    # Restrict the observed rule selector, preserving any existing matchers.
    constraints={'namespace':group['namespace'],_ENTITY_LABELS[group['entity_kind']]:group['entity']}
    suffix=','.join(k+'='+json.dumps(v) for k,v in constraints.items())
    if selector.endswith('}'):
        selector=selector[:-1].rstrip().rstrip(',')
        selector += ('' if selector.endswith('{') else ',')+suffix+'}'
    else:
        selector += '{'+suffix+'}'
    return selector, 'simple_comparison_operand'


def collect_metric_evidence(base_url, result, fetch_json=None, now=None):
    """Never changes alert membership or imports live provenance into uploaded data."""
    report={'status':'skipped','provenance':'live_prometheus_rule','groups':{}}
    if result.get('origin') != 'live_scan':
        report['reason']='only live scans may acquire live metric evidence'
        return report
    groups=[g for g in result['groups'] if g['alert_indices'] and all(result['raw_alerts'][i]['source']=='prometheus' for i in g['alert_indices'])]
    if not groups:
        report['reason']='no Prometheus alert groups'
        return report
    if not base_url and fetch_json is None:
        return dict(report,status='unconfigured')
    end=number(result.get('collection_started_at')) or (time.time() if now is None else now)
    window=float(result['time_window'])
    end_ms=math.floor(end*1000)
    start=(end_ms-math.ceil(window*1000))/1000
    end=end_ms/1000
    deadline=time.monotonic()+25
    def get(path, params):
        remaining=deadline-time.monotonic()
        if remaining<=0:raise TimeoutError('metric collection deadline exceeded')
        if fetch_json is not None:
            payload=fetch_json(path,params)
        else:
            response=requests.get(base_url.rstrip('/')+path,params=params,timeout=min(8,remaining))
            response.raise_for_status();payload=response.json()
        if payload.get('status')!='success' or payload.get('warnings'):
            raise ValueError('Prometheus response failed or contains warnings')
        return payload['data']
    try:
        rules_data=get('/api/v1/rules',{'type':'alert'})
        rules=[(rg,r) for rg in rules_data['groups'] for r in rg['rules'] if r.get('type')=='alerting']
    except Exception as exc:
        return dict(report,status='unavailable',error=type(exc).__name__)
    cache={}
    for group in groups:
        entry={'status':'unknown','alert_name':group['representative'],'alert_ids':group['alert_ids'],'cluster_id':group['cluster_id'],
               'namespace':group['namespace'],'entity_kind':group['entity_kind'],'entity':group['entity']}
        report['groups'][group['group_id']]=entry
        try:
            if time.monotonic()>=deadline:raise TimeoutError('metric collection deadline exceeded')
            if group['cluster_id']!=result.get('cluster_id'):
                entry['reason']='missing or unsupported resource identity';continue
            labels=[result['raw_alerts'][i]['labels'] for i in group['alert_indices']]
            scope=({'namespace':group['namespace'],_ENTITY_LABELS[group['entity_kind']]:group['entity']}
                   if group['entity_kind'] in _ENTITY_LABELS else {})
            expected=[{**{k:v for k,v in row.items() if k not in ('__name__','alertstate')},
                       **scope} for row in labels]
            matches=[]
            for rg,rule in rules:
                if rule.get('name')!=group['representative'] or rule.get('health')!='ok':continue
                active=[a for a in rule.get('alerts',[]) if a.get('state')=='firing']
                if all(any(all(a.get('labels',{}).get(k)==v for k,v in wanted.items()) for a in active) for wanted in expected):
                    matches.append((rg,rule))
            if len(matches)!=1:
                entry.update(reason='firing rule is missing or ambiguous',matching_rules=len(matches));continue
            rg,rule=matches[0]
            if rule['query']=='vector(1)' and not group['namespace'] and not group['entity']:
                entry.update(status='not_applicable',reason='constant heartbeat rule has no workload measurement',
                             rule={'name':rule['name'],'expression':rule['query'],'file':rg.get('file')})
                continue
            if (group['cluster_id']!=result.get('cluster_id') or not group['namespace']
                    or not group['entity'] or group['entity_kind'] not in _ENTITY_LABELS):
                entry['reason']='missing or unsupported resource identity';continue
            query,basis=measurement_query(rule,group)
            entry.update(rule={'name':rule['name'],'group':rg.get('name'),'file':rg.get('file'),
                               'expression':rule['query'],'labels':rule.get('labels',{})},query=query,query_basis=basis)
            trace_field = rule.get('annotations', {}).get('agenticsre_trace_field')
            if trace_field in ('duration_ms', 'http_status_code'):
                entry['trace_field'] = trace_field
            annotations = rule.get('annotations', {})
            if annotations.get('agenticsre_window_contract') == 'pod_request_window_v1':
                entry['request_window'] = {k: annotations.get('agenticsre_window_'+k) for k in
                    ('field', 'seconds', 'count_metric', 'end_metric', 'operation')}
                if 'agenticsre_window_service' in annotations:
                    entry['request_window']['service'] = annotations['agenticsre_window_service']
            if basis=='reviewed_expression':
                entry['measurement_adapter']=reviewed_measurement(rule['query'])
            if query is None:
                entry['reason']=basis;continue
            step=max(1.,math.ceil((number(rg.get('interval')) or 15.)*1000)/1000)
            entry.update(start=start,end=end,step_s=step,expected_samples=math.floor((end-start)/step+1e-6)+1)
            key=(query,start,end,step)
            if key not in cache:
                cache[key]=get('/api/v1/query_range',{'query':query,'start':start,'end':end,'step':step,'timeout':'5s'})
            data=cache[key]
            if data.get('resultType')!='matrix':raise ValueError('range query did not return a matrix')
            entity_label=_ENTITY_LABELS[group['entity_kind']]
            series=[s for s in data['result'] if s.get('metric',{}).get('namespace')==group['namespace']
                    and s.get('metric',{}).get(entity_label)==group['entity']
                    and s.get('metric',{}).get('cluster_id',group['cluster_id'])==group['cluster_id']]
            if basis in ('simple_comparison_operand','reviewed_expression'):
                # These reviewed operands preserve the alert expression labels.
                # Rule overrides cannot identify the original measurement labels.
                excluded={'alertname',*rule.get('labels',{})}
                identities=[{k:v for k,v in row.items() if k not in excluded} for row in expected]
                entry['measurement_label_constraints']=identities
                series=[s for s in series if all(
                    all(s.get('metric',{}).get(k)==v for k,v in identity.items())
                    for identity in identities)]
            if len(series)!=1:
                entry.update(reason='measurement series is missing or ambiguous',matching_series=len(series));continue
            observed=series[0]
            entry.update(series_labels=observed['metric'],raw_values=observed.get('values',[]))
            points={}
            for pair in entry['raw_values']:
                if not isinstance(pair,list) or len(pair)!=2:raise ValueError('invalid sample')
                stamp,value=map(number,pair)
                if stamp is None or value is None or not start<=stamp<=end:raise ValueError('invalid sample')
                if abs((stamp-start)/step-round((stamp-start)/step))>1e-4:raise ValueError('sample outside evaluation grid')
                if stamp in points and points[stamp]!=value:raise ValueError('conflicting sample')
                points[stamp]=value
            entry['coverage']=len(points)/entry['expected_samples']
            if len(points)<5 or not .8<=entry['coverage']<=1.01:
                entry['reason']='insufficient numeric history coverage';continue
            evidence_id='m-'+hashlib.sha256(json.dumps([entry['rule'],observed,start,end],sort_keys=True).encode()).hexdigest()[:24]
            entry.update(status='ready',evidence_id=evidence_id,points=sorted(points.items()),provenance='live_prometheus_rule')
        except Exception as exc:
            entry.update(status='unavailable',error=type(exc).__name__)
    ready=sum(e['status']=='ready' for e in report['groups'].values())
    report.update(status='ready' if ready==len(groups) else 'partial' if ready else 'unavailable',ready_groups=ready,total_groups=len(groups),
                  not_applicable_groups=sum(e['status']=='not_applicable' for e in report['groups'].values()))
    return report
