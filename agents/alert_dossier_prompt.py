"""Bounded observation handoff, separate from the complete audit dossier.

The shortest downstream incident budget is the planner's 2000 characters.
Never slice serialized evidence at that boundary; fit complete facts and report
what did not fit. The original dossier stays in the RCA session for inspection.
"""
import copy
import json

from agents.alert_root_candidates import diagnosis_candidates


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str)


def dossier_incident(query, payload, *, limit=2000, as_of=None):
    if type(limit) is not int or limit < 700:
        raise ValueError('dossier incident budget must be at least 700 characters')
    samples = payload.get('alert_samples', [])
    samples = samples if isinstance(samples, list) else []
    samples = [s for s in samples if isinstance(s, dict)]
    ranks = {'critical': 4, 'high': 3, 'warning': 2, 'info': 1}
    samples = sorted(samples, key=lambda s: -ranks.get(str(s.get('severity')), 0))
    candidates = diagnosis_candidates(payload, as_of)
    measurement = payload.get('metric_evidence', {})
    measurement = measurement if isinstance(measurement, dict) else {}
    measurements = measurement.get('measurements', []) if measurement.get('status') == 'composite' else [measurement]
    measurements = measurements if isinstance(measurements, list) else []
    metrics = []
    for value in measurements:
        if isinstance(value, dict) and isinstance(value.get('summary'), dict):
            window = value.get('request_window')
            rule = value.get('rule')
            quantity = (window.get('field') if isinstance(window, dict) else None)
            rule_name = rule.get('name') if isinstance(rule, dict) else None
            metrics.append({'evidence_id': value.get('evidence_id'), 'quantity': quantity,
                            'rule_name': rule_name, 'summary': value['summary']})
    numeric = []
    for field in ('metric_summary', 'log_numeric_summary'):
        value = payload.get(field)
        if field == 'log_numeric_summary' and isinstance(value, dict):
            numeric.extend({'source': field, 'field': key, 'values': stats} for key, stats in value.items())
        elif value:
            numeric.append({'source': field, 'values': value})
    omitted = dict(alert_samples=len(samples), root_candidates=len(candidates),
                   measurements=len(metrics), numeric_summaries=len(numeric), messages=len(samples),
                   audit_details='available_in_full_dossier')
    result = {'source': 'alert_diagnosis_context',
        'scope': {k: payload.get(k) for k in ('cluster_id', 'namespace', 'entity_kind', 'entity','entity_uid','identity_status')},
        'ref': {k: payload.get(k) for k in ('scan_id', 'revision', 'group_id')},
        'alert_count': payload.get('alert_count'),
        'observations': [], 'root_candidates': [], 'measurements': [], 'numeric_summaries': [],
        'omitted': omitted,
        'interpretation': '仅为观测和待验证候选，非根因确认；未展示项须继续取证，完整证据保存在同版本扫描。'}
    if len(_json(result)) > limit:
        # Do not shorten an entity identifier into a different resource name.
        fallback = {'source': 'alert_diagnosis_context', 'status': 'scope_exceeds_budget',
                    'ref': result['ref'], 'interpretation': '范围超出输入预算，须读取完整证据，不能据此诊断。'}
        if len(_json(fallback)) > limit:
            raise ValueError('dossier reference exceeds incident budget')
        return _json(fallback)

    def append(key, value, counter):
        result[key].append(value)
        omitted[counter] -= 1
        if len(_json(result)) <= limit:
            return True
        result[key].pop()
        omitted[counter] += 1
        return False

    # Original IDs, names and severity precede narratives and audit receipts.
    admitted = []
    for sample in samples:
        observation = {k: sample[k] for k in ('alert_id', 'name', 'severity', 'timestamp') if k in sample}
        if append('observations', observation, 'alert_samples'):
            admitted.append((observation, sample))
    for candidate in candidates:
        append('root_candidates', candidate, 'root_candidates')
    # Extrema have priority over free-form messages and query text.
    for value in numeric:
        append('numeric_summaries', copy.deepcopy(value), 'numeric_summaries')
    for value in metrics:
        append('measurements', copy.deepcopy(value), 'measurements')
    for observation, sample in admitted:
        message = sample.get('message', '')
        if not isinstance(message, str) or not message:
            omitted['messages'] -= 1
            continue
        observation['message'] = message[:200]
        observation['message_truncated'] = bool(sample.get('message_truncated') or len(message) > 200)
        omitted['messages'] -= 1
        if len(_json(result)) > limit:
            observation.pop('message')
            observation.pop('message_truncated')
            omitted['messages'] += 1
    # The task is optional context; it cannot crowd out original observations.
    result['task'] = str(query)[:256]
    result['task_truncated'] = len(str(query)) > 256
    if len(_json(result)) > limit:
        result.pop('task')
        result['task_truncated'] = True
        if len(_json(result)) > limit:
            result.pop('task_truncated')
    return _json(result)


def initial_incident(query, payload, *, as_of=None):
    """Shared by the real RCA entry and replay; generic context keeps its API."""
    if payload.get('source') == 'alert_evidence_dossier':
        return dossier_incident(query, payload, as_of=as_of)
    return (f'{query}\n\n当前事件结构化观测上下文（仅含运行期证据，不含注入真值）：\n'
            + json.dumps(payload, ensure_ascii=False, default=str)[:8000])
