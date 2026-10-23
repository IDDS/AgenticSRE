"""Applicability of historical memory, independent of similarity and permission.

Epoch seconds describe knowledge availability and validity. An incident remains
historical evidence, never an observation of the current resource instance.
"""
from dataclasses import dataclass, field, asdict
import math
import time
from memory.feedback_contract import digest


def epoch(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


@dataclass(frozen=True)
class MemoryScope:
    cluster_id: str = ''
    namespace: str = ''
    as_of: float = field(default_factory=time.time)
    workload_version: str = ''
    entity_uid: str = ''
    conflicted: bool = False

    @property
    def complete(self):
        return (self.conflicted is False and isinstance(self.cluster_id, str) and bool(self.cluster_id.strip())
                and isinstance(self.namespace, str) and bool(self.namespace.strip())
                and epoch(self.as_of) is not None)

    def to_dict(self):
        return {**asdict(self), 'schema': 'memory-scope/v1', 'historical_only': True}

    def source_fields(self):
        """Authoritative request identity, never identity proposed by an LLM."""
        return dict(cluster_id=self.cluster_id, namespace=self.namespace,
                    scope_kind='local', workload_version=self.workload_version,
                    entity_uid=self.entity_uid)


def global_scope_revision(record):
    """A global review approves this content and applicability, not just its ID."""
    keys = ('condition', 'conclusion', 'fault_type', 'scope_kind', 'cluster_id', 'namespace',
            'workload_version', 'entity_uid', 'valid_from', 'valid_to', 'review_expires_at')
    return digest({key: str(record.get(key, '')) for key in keys})


def admission_reason(record, scope, kind):
    if not isinstance(scope, MemoryScope) or not scope.complete:
        return 'missing_request_scope'
    if not isinstance(record, dict):
        return 'invalid_record'
    if kind == 'rules':
        from memory.rule_lifecycle import is_active
        if not is_active(record):
            return 'unreviewed_or_inactive'
    state = record.get('memory_status', record.get('status', 'active'))
    allowed = {'active'} if kind == 'rules' else {'active', 'completed', 'success', 'resolved', 'recovered'}
    if state not in allowed:
        return 'inactive'
    now = epoch(scope.as_of)
    recorded = epoch(record.get('recorded_at', record.get('timestamp')))
    if recorded is None:
        return 'unknown_recorded_time'
    if recorded > now:
        return 'future_record'
    for key in ('valid_from', 'valid_to', 'review_expires_at', 'observed_at', 'updated_at'):
        if key not in record:
            continue
        value = epoch(record[key])
        if value is None:
            return 'invalid_time'
        if key in ('valid_from', 'observed_at', 'updated_at') and value > now:
            return 'not_yet_valid'
        if key in ('valid_to', 'review_expires_at') and value <= now:
            return 'expired'
    if ('valid_from' in record and 'valid_to' in record
            and epoch(record['valid_from']) >= epoch(record['valid_to'])):
        return 'invalid_interval'
    global_rule = kind == 'rules' and record.get('scope_kind') == 'global'
    if global_rule:
        reviewed = epoch(record.get('scope_reviewed_at'))
        reviewer = record.get('scope_reviewed_by')
        if not isinstance(reviewer, str) or not reviewer.strip() or reviewed is None or reviewed > now:
            return 'unreviewed_global'
        if record.get('scope_review_revision') != global_scope_revision(record):
            return 'global_review_version_mismatch'
    elif (record.get('cluster_id') != scope.cluster_id or record.get('namespace') != scope.namespace):
        return 'scope_mismatch'
    for key in ('workload_version', 'entity_uid'):
        constraint = record.get(key)
        if constraint and constraint != getattr(scope, key):
            return key + '_mismatch'
    return 'admitted'


def filter_memory(records, scope, kind):
    accepted, reasons = [], {}
    for record in records:
        reason = admission_reason(record, scope, kind)
        if reason == 'admitted':
            accepted.append(record)
        else:
            reasons[reason] = reasons.get(reason, 0) + 1
    return accepted, {'accepted': len(accepted), 'rejected': sum(reasons.values()), 'reasons': reasons}
