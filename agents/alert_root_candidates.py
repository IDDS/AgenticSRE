"""Rank observed propagation origins for investigation, never certify causality."""
from collections import Counter
import json
import re
import time

from agents.alert_context import qualify_context
from agents.alert_semantics import entity_ref, number, statistical_check


def propagation_targets(payload, namespace):
    """Validated lookup hints, not reusable proof or shell commands."""
    if not diagnosis_handoff(payload) or payload.get('namespace') != namespace:
        return {}
    for candidate in payload['root_candidates']['candidates']:
        if not isinstance(candidate, dict):
            continue
        ref = str(candidate.get('entity_ref', '')).split('|')
        if (len(ref) != 4 or ref[:3] != [payload.get('cluster_id'), namespace, 'pod']
                or not re.fullmatch(r'[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?', ref[3])):
            continue
        ids = []
        supplied = candidate.get('supporting_evidence')
        for evidence in supplied if isinstance(supplied, list) else []:
            if not isinstance(evidence, dict):
                continue
            refs = evidence.get('trace_refs')
            for item in refs if isinstance(refs, list) else []:
                if not isinstance(item, dict):
                    continue
                value = item.get('trace_id', '')
                if isinstance(value, str) and re.fullmatch(r'(?:[a-fA-F0-9]{16}|[a-fA-F0-9]{32})', value) and value not in ids:
                    ids.append(value)
        if ids:
            return dict(pod_name=ref[3], trace_ids=ids[:3])
    return {}


def diagnosis_candidates(payload, as_of=None):
    """Fresh, bounded investigation hints, never confirmed causes."""
    if payload.get('source') != 'alert_evidence_dossier':
        return []
    report = payload.get('root_candidates', {})
    if not isinstance(report, dict):
        return []
    now = time.time() if as_of is None else as_of
    until = number(report.get('valid_until'))
    if report.get('status') != 'candidates_ready' or until is None or now > until:
        return []
    supplied = report.get('candidates', [])
    if not isinstance(supplied, list):
        return []
    candidates = []
    for c in supplied[:3]:
        if not isinstance(c, dict) or not isinstance(c.get('entity_ref'), str) or not c['entity_ref']:
            continue
        paths = c.get('paths')
        path = paths[0] if isinstance(paths, list) and paths and isinstance(paths[0], list) else []
        candidates.append(dict(entity_ref=c['entity_ref'][:250],
            status='possible_downstream_symptom' if c.get('status') == 'possible_downstream_symptom' else 'requires_diagnosis', causal_confirmed=False,
            path=[p[:150] for p in path[:6] if isinstance(p, str)]))
    if not candidates:
        return []
    while len(candidates) > 1 and len(json.dumps(candidates, ensure_ascii=False)) > 2000:
        candidates.pop()
    return candidates


def diagnosis_handoff(payload, as_of=None):
    """Keep candidate scope before the diagnosis prompt's 3000-char truncation."""
    candidates = diagnosis_candidates(payload, as_of)
    if not candidates:
        return ''
    return ('待验证传播起点（不是根因结论；请主动寻找反证，核对依赖日志及更下游故障）：\n'
            + json.dumps(candidates, ensure_ascii=False) + '\n')


def build_root_candidates(result, as_of=None):
    now = time.time() if as_of is None else as_of
    report = dict(status='abstained', candidates=[], assessments=[], causal_confirmed=False,
                  confidence=None, confidence_basis='not_calibrated', automatic_action=False,
                  interpretation='传播起点是待验证根因候选；排序不是根因概率，不授权告警抑制或修复。')
    result['root_candidates'] = report
    started = number(result.get('collection_started_at'))
    window = number(result.get('time_window'))
    live = ((result.get('origin') == 'live_scan' and result.get('collection') == {'prometheus': 'ok', 'kubectl': 'ok'})
            or (result.get('origin') == 'live_probe' and result.get('collection') == {'http_probe': 'ok', 'jaeger': 'ok'}))
    if (not live or result.get('history') or result.get('degraded')
            or result.get('scag', {}).get('status') != 'ready'):
        report['reason'] = 'ineligible_observations'
        return report
    if started is None or window is None or not started <= now <= started + window:
        report.update(status='expired', reason='refresh_live_evidence')
        return report
    report['valid_until'] = started + window
    groups = {g['group_id']: g for g in result['groups']}
    refs = {k: entity_ref(*(g[f] for f in ('cluster_id','namespace','entity_kind','entity')))
            for k, g in groups.items()}
    edges = result['scag'].get('edges', [])
    conflicts = {frozenset((e['left'], e['right'])) for e in edges if e.get('type') == 'conflicts'}
    pairs = set()
    for edge in edges:
        a, b = edge['left'], edge['right']
        if a in groups and b in groups and a != b:
            # Models may omit or reverse a relation. Observed Trace paths propose
            # both directions; each is independently checked, never trusted as fact.
            pairs.update(((a, b), (b, a)))
    accepted = []
    for a, b in sorted(pairs):
        left, right = groups[a], groups[b]
        context = qualify_context(result, left, right, 'propagates_to')
        if context.get('status') != 'pass':
            continue
        scope = bool(left['cluster_id'] and left['namespace']) and all(
            left[k] == right[k] for k in ('cluster_id', 'namespace')) and refs[a] != refs[b]
        stats = statistical_check(result, left, right)
        stamps = [g[k] for g in (left, right) for k in ('first_seen', 'last_seen')]
        timing = (all(number(t) is not None for t in stamps)
                  and max(stamps)-min(stamps) <= window
                  and now-window <= min(stamps) <= max(stamps) <= now)
        trace = result.get('context_evidence', {}).get('traces', {})
        complete = trace.get('status') == 'mapped' and not trace.get('truncated') and all(
            str(e.get('provenance', '')).startswith('live_jaeger') for e in context['trace_refs'])
        checks = dict(scope='pass' if scope else 'fail', time='pass' if timing else 'fail',
                      statistics=stats['status'], trace='pass' if complete else 'unknown',
                      counterevidence='fail' if frozenset((a,b)) in conflicts else 'pass')
        blocked = [k for k, value in checks.items() if value != 'pass']
        assessment = dict(source_group=a, target_group=b, checks=checks, statistics=stats,
                          time_basis='same_trace_completion_order; alert timestamps only bound freshness',
                          status='rejected' if 'fail' in checks.values() else 'needs_evidence' if blocked else 'supported_candidate',
                          missing_or_conflicting=blocked, trace_refs=context['trace_refs'],
                          propagation_path=context['propagation_path'], causal_confirmed=False)
        report['assessments'].append(assessment)
        if not blocked:
            accepted.append(assessment)
    candidates = {}
    for assessment in accepted:
        key = assessment['source_group']
        candidate = candidates.setdefault(key, dict(group_id=key, entity_ref=refs[key],
            affected_group_ids=[key], paths=[], supporting_evidence=[], counterevidence=[],
            status='requires_diagnosis', causal_confirmed=False, confidence=None,
            next_checks=['核对依赖自身日志和状态，区分本地故障与更下游传播。',
                         '检查同时间窗内健康请求与失败请求，主动寻找反例。',
                         '使用独立变更或恢复证据检验假设；证据不足则拒绝根因确认。']))
        if assessment['target_group'] not in candidate['affected_group_ids']:
            candidate['affected_group_ids'].append(assessment['target_group'])
        candidate['paths'].append(assessment['propagation_path'])
        candidate['supporting_evidence'].append(assessment)
    # Preserve competing hypotheses; an observed upstream symptom may itself
    # have an observed dependency. Do not join separate traces into a causal path.
    for key, candidate in candidates.items():
        candidate['counterevidence'] = [a for a in accepted if a['target_group'] == key]
        if candidate['counterevidence']:
            candidate['status'] = 'possible_downstream_symptom'
    ranked = sorted(candidates.values(), key=lambda c: (bool(c['counterevidence']),
                    -len(c['affected_group_ids']), c['entity_ref'], c['group_id']))
    report['candidate_count'] = len(ranked)
    report['candidates'] = [dict(c, rank=i+1) for i,c in enumerate(ranked[:3])]
    report['rejected_checks'] = dict(Counter(k for a in report['assessments'] for k in a['missing_or_conflicting']))
    report['status'] = 'candidates_ready' if ranked else 'abstained'
    report['reason'] = 'diagnosis_required' if ranked else 'insufficient_independent_evidence'
    return report
