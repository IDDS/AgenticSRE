"""Pure contracts for manual rule review; no implied independent evaluation."""
import json
import math
import re
from memory.feedback_contract import FeedbackStorageError, digest

EDITABLE = {'name', 'condition', 'conclusion', 'fault_type', 'namespace', 'cluster_id',
            'confidence', 'workload_version', 'entity_uid', 'valid_from', 'valid_to', 'review_expires_at'}
MAX_EVENTS = 100


def content_revision(rule):
    keys = EDITABLE | {'source', 'source_incident_id', 'source_feedback_id', 'rule_type', 'lesson'}
    return digest({key: str(rule.get(key, '')) for key in sorted(keys)})


def effective_status(rule):
    state = rule.get('status', 'active')
    from memory.feedback_lifecycle import rule_block_reason
    reason = rule_block_reason(rule)
    if reason and (state == 'active' or 'retracted' in reason):
        return 'source_blocked'
    if (state == 'active' and rule.get('source') in ('auto_learn', 'supervised')
            and rule.get('activation_content_revision') != content_revision(rule)):
        return 'candidate'
    return state


def is_active(rule):
    return effective_status(rule) == 'active' and rule.get('memory_status', 'active') == 'active'


def reset_votes(rule, old):
    rule['feedback_vote_ids'] = '[]'
    for polarity in ('positive', 'negative'):
        key = polarity + '_votes'
        rule['lifetime_' + key] = int(float(old.get('lifetime_' + key, 0))) + int(float(old.get(key, 0)))
        rule[key] = 0
    rule['quality_score'] = float(rule.get('confidence', .5))


def valid_rules(rules):
    if not isinstance(rules, list) or len(rules) > 3:
        return False
    for rule in rules:
        if not isinstance(rule, dict):
            return False
        if any(not isinstance(rule.get(key), str) or not rule[key].strip() or len(rule[key]) > 8000
               for key in ('condition', 'conclusion')):
            return False
        confidence = rule.get('confidence', .5)
        if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            return False
        if (rule.get('rule_type', 'positive') not in ('positive', 'negative')
                or any(not isinstance(rule.get(k, ''), str) or len(rule.get(k, '')) > 8000
                       for k in ('lesson', 'fault_type'))):
            return False
    return True


def history(rule):
    try:
        def invalid_number(value):
            raise ValueError('invalid_history_number')
        events = json.loads(rule.get('lifecycle_history', '[]'), parse_constant=invalid_number)
    except (ValueError, TypeError) as exc:
        raise FeedbackStorageError('rule_history_unavailable') from exc
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        raise FeedbackStorageError('rule_history_invalid')
    return events


def snapshot(rule):
    return {key: rule.get(key) for key in sorted(EDITABLE | {'status', 'source', 'source_incident_id',
        'source_feedback_id', 'positive_votes', 'negative_votes', 'revision_seq', 'rule_type', 'lesson'})}


def decision_request(revision, decision, reviewer, reason, event_id, identity_source):
    if decision not in ('approve', 'reject', 'deactivate', 'resubmit'):
        raise ValueError('invalid_rule_decision')
    if not isinstance(revision, str) or not re.fullmatch(r'[a-f0-9]{64}', revision):
        raise ValueError('rule_revision_required')
    if not isinstance(event_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', event_id):
        raise ValueError('invalid_rule_event_id')
    for value, maximum in ((reviewer, 100), (reason, 2000)):
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError('reviewer_and_reason_required')
    if identity_source not in ('operator_label', 'authenticated_session', 'api_token'):
        raise ValueError('invalid_review_identity_source')
    value = dict(revision=revision, decision=decision, reviewer=reviewer.strip(), reason=reason.strip(),
                 identity_source=identity_source)
    return value, digest(value)
