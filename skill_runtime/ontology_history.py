"""Bounded history over retained investigation observations, never a live CMDB.

No I/O or actions. Collection time bounds the view; resource versions are not
used as a cross-object clock. Comparisons describe observations, not causation.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import time

from operational_ontology import (CLUSTER_SCOPED, committed_tool_observations, observation_entity,
                                  observation_id, observation_semantics, result_envelopes)
from skill_runtime.service_topology import project_resource

MAX_RUNS = 200
MAX_OBSERVATIONS = 4096
MAX_ENTRIES = 2000
MAX_TOOL_BYTES = 2 * 1024 * 1024
MAX_STATE_BYTES = 8192
ACTIVE = {'queued', 'starting', 'running'}


class HistoryBindingError(ValueError):
    pass


class HistoryUnavailable(RuntimeError):
    pass


def _dict(value):
    return value if isinstance(value, dict) else {}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))


def collection_time(value):
    """Only explicit timezone-aware collection times or finite epoch seconds."""
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, (float, int)):
            stamp = float(value)
        elif isinstance(value, str) and len(value) <= 64:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                return None
            stamp = parsed.timestamp()
        else:
            return None
        if not math.isfinite(stamp) or stamp < 0:
            return None
        datetime.fromtimestamp(stamp, timezone.utc)
        return stamp
    except (ValueError, OverflowError, OSError):
        return None


def _iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace('+00:00', 'Z')


def eligible_object_ids(model):
    run_id, cluster = model.get('run_id'), model['scope'].get('cluster_id')
    if not isinstance(run_id, str) or not run_id or len(run_id) > 256 or not cluster:
        return []
    bound = {obj['id'] for obj in model['objects'] if obj['identity_status'] == 'uid_bound'
        and obj['cluster_id'] == cluster and all(isinstance(obj.get(k), str) and len(obj[k]) <= 512
            for k in ('cluster_id', 'namespace', 'kind', 'name', 'uid'))}
    return sorted({o['entity_id'] for o in model['observations']
        if o.get('entity_id') and o.get('scope_status') == 'in_run'
        and o.get('origin') in {'collector', 'committed_tool'}
        and o.get('temporality') != 'historical' and collection_time(o.get('observed_at')) is not None
        and o['entity_id'] in bound})


def _check(deadline):
    if time.monotonic() > deadline:
        raise HistoryUnavailable('history_time_budget_exceeded')


def _within_budget(value, size_limit, *, deadline=None):
    stack, size, nodes = [(value, 0)], 0, 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > 100000 or depth > 32:
            return False
        if deadline is not None and nodes % 128 == 0:
            _check(deadline)
        if isinstance(item, dict):
            if len(item) > 20000 or any(not isinstance(key, str) for key in item):
                return False
            size += 2 + 2 * len(item)
            stack.extend((part, depth + 1) for pair in item.items() for part in pair)
        elif isinstance(item, (list, tuple)):
            if len(item) > 20000:
                return False
            size += 2 + 2 * len(item)
            stack.extend((part, depth + 1) for part in item)
        elif item is None or type(item) in (str, bool, int, float):
            if isinstance(item, float) and not math.isfinite(item):
                size += 4  # The dashboard's snapshot encoder normalizes these to null.
                if size > size_limit:
                    return False
                continue
            if isinstance(item, str) and len(item) > size_limit - size:
                return False
            try:
                size += len(_json(item).encode())
            except (ValueError, OverflowError):
                return False
        else:
            return False
        if size > size_limit:
            return False
    return True


def _normalized(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _normalized(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalized(item) for item in value]
    return value


def check_source_budget(run, *, deadline=None):
    """Fail before the existing full-model projector parses an oversized source."""
    for layer in result_envelopes(run):
        if isinstance(layer.get('observations'), list) and len(layer['observations']) > MAX_OBSERVATIONS:
            raise HistoryUnavailable('source_observation_limit_exceeded')
        commands = _dict(_dict(layer.get('agent_evidence')).get('agent_evidence_collection')).get('commands')
        if isinstance(commands, list):
            if len(commands) > 64:
                raise HistoryUnavailable('source_command_limit_exceeded')
            for call in commands:
                data = _dict(call).get('data')
                if isinstance(data, str) and len(data) > MAX_TOOL_BYTES:
                    raise HistoryUnavailable('source_tool_size_limit_exceeded')
    if isinstance(run.get('events'), list) and len(run['events']) > 20000:
        raise HistoryUnavailable('source_event_limit_exceeded')
    if not _within_budget({k: run.get(k) for k in ('id', 'status', 'cluster_id', 'namespace', 'result', 'events')},
                          8 * 1024 * 1024, deadline=deadline):
        raise HistoryUnavailable('source_size_or_shape_limit_exceeded')


def _candidates(run, gaps, deadline):
    layers = result_envelopes(run)
    native = next((r['observations'] for r in layers if isinstance(r.get('observations'), list)), None)
    count = 0
    if native is not None:
        if len(native) > MAX_OBSERVATIONS:
            gaps.add('run_observation_limit')
        for item in native[:MAX_OBSERVATIONS]:
            _check(deadline)
            if isinstance(item, dict):
                count += 1
                yield item, 'collector'
    else:
        events = run.get('events') if isinstance(run.get('events'), list) else []
        if len(events) > 20000:
            gaps.add('run_event_limit')
        for event in events[:20000]:
            _check(deadline)
            event = _dict(event)
            if event.get('event') != 'observation_batch':
                continue
            items = event.get('items') if isinstance(event.get('items'), list) else []
            for item in items:
                if count >= MAX_OBSERVATIONS:
                    gaps.add('run_observation_limit')
                    break
                count += 1
                if isinstance(item, dict):
                    yield item, 'collector'
            if count >= MAX_OBSERVATIONS:
                break
        if any(_dict(layer.get('judge')).get('review_evidence') for layer in layers) or any(
                _dict(event).get('event') == 'structured_evidence' for event in events[:20000]):
            gaps.add('legacy_excerpts_excluded')
    commands = next((v for layer in layers if isinstance(v := _dict(
        _dict(layer.get('agent_evidence')).get('agent_evidence_collection')).get('commands'), list)), [])
    if len(commands) > 64:
        gaps.add('run_command_limit')
    for call in commands[:64]:
        _check(deadline)
        if not isinstance(call, dict) or call.get('executed') is not True or call.get('success') is not True or call.get('outcome_unknown'):
            continue
        data = call.get('data')
        if isinstance(data, str):
            if len(data) > MAX_TOOL_BYTES:
                gaps.add('tool_result_size_limit')
                continue
            try:
                data = json.loads(data)
            except (ValueError, RecursionError):
                gaps.add('non_json_tool_result_excluded')
                continue
        if not isinstance(data, dict):
            continue
        if not _within_budget(data, MAX_TOOL_BYTES, deadline=deadline):
            gaps.add('tool_result_size_limit')
            continue
        if isinstance(data.get('items'), list) and len(data['items']) > MAX_OBSERVATIONS:
            gaps.add('tool_resource_limit')
            continue
        try:
            projected = committed_tool_observations({**call, 'data': data})
        except (TypeError, ValueError, RecursionError):
            gaps.add('invalid_tool_projection')
            continue
        for item in projected:
            if count >= MAX_OBSERVATIONS:
                gaps.add('run_observation_limit')
                break
            count += 1
            yield item, 'committed_tool'


def _scalar(value):
    return (isinstance(value, str) and len(value) <= 512 or type(value) in (bool, int)
            or value is None)


def _count(value):
    return type(value) is int and 0 <= value <= 2 ** 31


def _typed(value, expected):
    if value is None:
        return True
    if expected == 'count':
        return _count(value)
    if expected == 'bool':
        return type(value) is bool
    return isinstance(value, str) and len(value) <= 512


def _containers(rows):
    if not isinstance(rows, list) or len(rows) > 128:
        raise ValueError('invalid_container_statuses')
    result, names = {}, set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('invalid_container_status')
        name = row.get('name')
        if not isinstance(name, str) or len(name) > 63 or not re.fullmatch(r'[a-z0-9](?:[-a-z0-9]*[a-z0-9])?', name) or name in names:
            raise ValueError('invalid_container_name')
        names.add(name)
        prefix = f'status.containerStatuses[{name}].'
        for key in ('ready', 'restartCount', 'image', 'imageID'):
            value = row.get(key)
            if value is None:
                continue
            valid = _typed(value, 'bool' if key == 'ready' else 'count' if key == 'restartCount' else 'str')
            if not valid:
                raise ValueError('invalid_container_status_field')
            result[prefix + key] = value
        for field in ('state', 'lastState'):
            states = row.get(field)
            if states is None:
                continue
            if not isinstance(states, dict) or len(states) > 1:
                raise ValueError('invalid_container_state')
            for variant, body in states.items():
                if variant not in {'waiting', 'running', 'terminated'} or not isinstance(body, dict):
                    raise ValueError('invalid_container_state_variant')
                result[prefix + field + '.kind'] = variant
                for key in ('reason', 'exitCode', 'signal', 'startedAt', 'finishedAt'):
                    if key in body and body[key] is not None:
                        value = body[key]
                        if (key in {'exitCode', 'signal'} and type(value) is int and abs(value) <= 2 ** 31
                                or key not in {'exitCode', 'signal'} and isinstance(value, str) and len(value) <= 512):
                            result[prefix + field + '.' + key] = value
                        else:
                            raise ValueError('invalid_container_state_field')
    result['status.containerStatuses.names'] = sorted(names)
    return result


def _projection(item):
    raw = _dict(item.get('raw_ref'))
    if raw.get('truncated') or raw.get('topology_fields_complete') is False:
        return None, None
    projected = project_resource(raw)
    state = {}
    if projected:
        if not projected['topology_fields_complete']:
            return None, None
        for field in ('labels', 'ownerReferences', 'deletionTimestamp'):
            if field in _dict(raw.get('metadata')):
                value = projected['metadata'].get(field)
                state['metadata.' + field] = sorted(value, key=_json) if isinstance(value, list) else value
        spec_fields = ({'replicas': 'count'} if projected['kind'] in {'Deployment', 'ReplicaSet', 'StatefulSet'} else
                       {'nodeName': 'str'} if projected['kind'] == 'Pod' else
                       {'type': 'str', 'publishNotReadyAddresses': 'bool'} if projected['kind'] == 'Service' else {})
        for field, expected in spec_fields.items():
            if field in projected['spec']:
                value = projected['spec'][field]
                if not _typed(value, expected):
                    return None, None
                state['spec.' + field] = value
        if projected['kind'] == 'Service' and 'selector' in projected['spec']:
            state['spec.selector'] = projected['spec']['selector']
        if 'template' in projected['spec']:
            state['spec.template.metadata.labels'] = projected['spec']['template']['metadata']['labels']
        for field in ('phase', 'readyReplicas', 'availableReplicas', 'podIP'):
            if field in projected['status']:
                value = projected['status'][field]
                if not _typed(value, 'count' if field.endswith('Replicas') else 'str'):
                    return None, None
                state['status.' + field] = value
        if 'containerStatuses' in projected['status']:
            state.update(_containers(projected['status']['containerStatuses']))
        if projected['kind'] == 'EndpointSlice':
            if projected.get('addressType') not in ('IPv4', 'IPv6', 'FQDN'):
                return None, None
            members = []
            for row in projected['endpoints']:
                identity = {'addresses': sorted(row['addresses']), 'targetRef': row['targetRef']}
                key = hashlib.sha256(_json(identity).encode()).hexdigest()[:24]
                if key in members:
                    raise ValueError('duplicate_endpoint_identity')
                members.append(key)
                state[f'endpoints[{key}].identity'] = identity
                for condition, value in row['conditions'].items():
                    if value is not None:
                        state[f'endpoints[{key}].conditions.{condition}'] = value
            state['endpoints'] = sorted(set(members))
            state['addressType'] = projected.get('addressType')
        lane = 'resource-history/v1:' + projected['kind']
    else:
        for field in ('phase', 'reason', 'node_name', 'container', 'container_kind', 'exit_code',
                      'restart_count', 'desired_replicas', 'ready_replicas', 'ready_addresses', 'not_ready_addresses'):
            if field not in raw:
                continue
            value = raw[field]
            if field == 'exit_code':
                valid = value is None or type(value) is int and abs(value) <= 2 ** 31
            else:
                expected = 'count' if field in {'restart_count', 'desired_replicas', 'ready_replicas',
                                               'ready_addresses', 'not_ready_addresses'} else 'str'
                valid = _typed(value, expected)
            if not valid:
                return None, None
            state[field] = value
        lane = 'typed-signal-history/v1:' + str(item.get('signal_type') or '')
        if str(item.get('signal_type') or '').startswith('container_') or 'container' in raw or 'container_kind' in raw:
            name, kind = raw.get('container'), raw.get('container_kind')
            if (not isinstance(name, str) or len(name) > 63
                    or not re.fullmatch(r'[a-z0-9](?:[-a-z0-9]*[a-z0-9])?', name)
                    or not isinstance(kind, str) or kind not in {'application', 'init', 'ephemeral'}):
                return None, None
            lane += ':' + _json([kind, name])
    if not state or len(_json(state).encode()) > MAX_STATE_BYTES:
        return None, None
    return lane, state


def _run_order(run):
    value = run.get('started_at')
    try:
        return float(value) if type(value) in (int, float) and math.isfinite(value) else 0
    except OverflowError:
        return 0


def _has_collection_gaps(run):
    for layer in result_envelopes(run):
        rows = layer.get('collection_history')
        if isinstance(rows, list) and (len(rows) > MAX_OBSERVATIONS or any(
                _dict(row).get('status') in ('partial', 'failed') for row in rows[:MAX_OBSERVATIONS])):
            return True
    events = run.get('events') if isinstance(run.get('events'), list) else []
    return len(events) > 20000 or any(_dict(e).get('event') == 'evidence' and _dict(e).get('success') is False
        or _dict(_dict(e).get('collection')).get('status') in ('partial', 'failed') for e in events[:20000])


def build_object_history(model, source_run, records, entity_id, *, deadline=None):
    deadline = deadline if deadline is not None else time.monotonic() + 3
    target = next((o for o in model['objects'] if o['id'] == entity_id), None)
    if entity_id not in eligible_object_ids(model):
        raise HistoryBindingError('需要本次范围内具有真实 UID 和有效采集时间的直接观测。')
    as_of = max(collection_time(o['observed_at']) for o in model['observations']
        if o['entity_id'] == entity_id and o['scope_status'] == 'in_run'
        and o['origin'] in {'collector', 'committed_tool'} and o['temporality'] != 'historical'
        and collection_time(o['observed_at']) is not None)
    terminal = sorted((r for r in records if isinstance(r, dict) and not (
        isinstance(r.get('status'), str) and r['status'] in ACTIVE)),
                      key=_run_order, reverse=True)[:MAX_RUNS]
    outside = not any(r.get('id') == model['run_id'] for r in terminal)
    selected = [r for r in terminal if r.get('id') != model['run_id']] + [source_run]
    entries, other, gaps, omitted = [], {}, set(), Counter()
    scope_runs = 0
    logical = tuple(target[k] for k in ('cluster_id', 'namespace', 'kind', 'name'))
    for record in selected:
        _check(deadline)
        scope = {k: record[k].strip() if isinstance(record.get(k), str) else ''
                 for k in ('cluster_id', 'namespace')}
        if scope['cluster_id'] != target['cluster_id'] or (target['namespace'] and
                scope['namespace'] and scope['namespace'] != target['namespace']):
            continue
        if not isinstance(record.get('id'), str) or not record['id'] or len(record['id']) > 256:
            gaps.add('history_run_identity_missing')
            continue
        scope_runs += 1
        seen = set()
        collection_gaps = None
        for item, origin in _candidates(record, gaps, deadline):
            _check(deadline)
            if not item.get('source') or not item.get('observation'):
                continue
            entity = observation_entity(item, **scope)
            if not entity or tuple(entity[k] for k in ('cluster_id', 'namespace', 'kind', 'name')) != logical:
                continue
            if entity['identity_status'] != 'uid_bound':
                gaps.add('matching_object_uid_missing')
                omitted['missing_uid'] += 1
                continue
            if not _within_budget(item, MAX_TOOL_BYTES, deadline=deadline):
                gaps.add('observation_size_limit')
                omitted['oversized_observation'] += 1
                continue
            item = _normalized(item)
            semantics = observation_semantics(item)
            stamp = collection_time(semantics['observed_at'])
            if stamp is None:
                gaps.add('matching_observation_time_missing')
                omitted['missing_time'] += 1
                continue
            if stamp > as_of:
                omitted['after_as_of'] += 1
                continue
            try:
                oid = observation_id(item)
            except (ValueError, TypeError, RecursionError):
                gaps.add('invalid_state_projection')
                omitted['invalid_projection'] += 1
                continue
            try:
                lane, state = _projection(item)
            except (ValueError, TypeError, RecursionError, KeyError):
                lane, state = None, None
                gaps.add('invalid_state_projection')
            if oid in seen:
                continue
            seen.add(oid)
            if len(entries) + sum(row['observations'] for row in other.values()) >= MAX_ENTRIES:
                raise HistoryUnavailable('history_entry_limit_exceeded')
            if entity['uid'] != target['uid']:
                incarnation = other.setdefault(entity['id'], {**entity, 'observations': 0, 'run_ids': set(),
                    'first_seen': stamp, 'last_seen': stamp})
                incarnation['observations'] += 1
                incarnation['run_ids'].add(record['id'])
                incarnation['first_seen'] = min(stamp, incarnation['first_seen'])
                incarnation['last_seen'] = max(stamp, incarnation['last_seen'])
                continue
            if state is None:
                gaps.add('state_not_comparable')
            if collection_gaps is None:
                collection_gaps = _has_collection_gaps(record)
            if collection_gaps:
                gaps.add('collection_incomplete')
            status = record.get('status')
            if not isinstance(status, str) or status not in {'completed', 'failed', 'cancelled', 'canceled', *ACTIVE}:
                status = 'unknown'
            entries.append({'run_id': record['id'], 'run_status': status,
                'entity_id': entity_id, 'observation_id': oid, 'origin': origin,
                'observed_at': _iso(stamp), '_time': stamp, 'temporality': semantics['temporality'],
                'resource_version': semantics['resource_version'] if _scalar(semantics['resource_version']) else None,
                'projection': lane, 'state': state, 'collection_has_gaps': collection_gaps})
    entries.sort(key=lambda e: (e['_time'], str(e['run_id']), e['observation_id']))
    ties = Counter((e['_time'], e['projection']) for e in entries if e['state'] is not None and e['temporality'] != 'historical')
    previous = {}
    for entry in entries:
        lane, state = entry['projection'], entry['state']
        comparison = {'status': 'baseline', 'changes': [], 'availability': {'added_fields': [], 'missing_fields': []}}
        if entry['temporality'] == 'historical' or state is None:
            comparison['status'] = 'context_only' if entry['temporality'] == 'historical' else 'not_comparable'
        elif ties[(entry['_time'], lane)] > 1:
            comparison['status'] = 'ambiguous_time'
            previous.pop(lane, None)
        else:
            before = previous.get(lane)
            if before:
                a, b = before['state'], state
                comparison.update(previous_run_id=before['run_id'], previous_observation_id=before['observation_id'])
                comparison['changes'] = [{'field': k, 'before': a[k], 'after': b[k]}
                                         for k in sorted(a.keys() & b.keys()) if a[k] != b[k]]
                comparison['availability'] = {'added_fields': sorted(b.keys() - a.keys()), 'missing_fields': sorted(a.keys() - b.keys())}
                comparison['status'] = ('changed' if comparison['changes'] else 'availability_changed'
                    if any(comparison['availability'].values()) else 'unchanged')
            previous[lane] = entry
        entry['comparison'] = comparison
    for entry in entries:
        entry.pop('_time')
    incarnations = [{**row, 'run_ids': sorted(row['run_ids']), 'first_seen': _iso(row['first_seen']),
                     'last_seen': _iso(row['last_seen'])} for _, row in sorted(other.items())]
    revision = 'hist-' + hashlib.sha256(_json({'target': target, 'as_of': _iso(as_of),
        'entries': entries, 'other_incarnations': incarnations, 'gaps': sorted(gaps)}).encode()).hexdigest()[:24]
    return {'schema_version': 'object-history/v1', 'source_run_id': model['run_id'], 'source_revision': model['revision'],
        'history_revision': revision, 'target': target, 'as_of': _iso(as_of),
        'items': list(reversed(entries)), 'other_incarnations': incarnations, 'gaps': sorted(gaps),
        'coverage': {'retained_terminal_limit': MAX_RUNS, 'terminal_runs_considered': len(terminal),
                     'scope_runs_considered': scope_runs, 'source_outside_retained_window': outside,
                     'omitted': dict(omitted), 'partial': bool(gaps), 'lineage_snapshots_included': False},
        'semantics': {'is_causal_proof': False, 'live_state_verified': False, 'continuous_history': False,
                      'scope': 'retained_investigation_observations', 'equal_time_order': 'unknown',
                      'comparison': 'shared_observed_fields_only'}}
