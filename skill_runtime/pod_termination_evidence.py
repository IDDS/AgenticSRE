"""Project observed Pod termination state without inferring incident causality."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from operational_ontology import committed_tool_observations, observation_id
from skill_runtime.service_evidence import PROJECTION, projection_request

MAX_TERMINATIONS = 24


def _time(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc).timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def observed_terminations(commands, *, cluster_id, namespace, allowed_ids):
    """Only complete host projections enter the catalog; absence proves nothing."""
    result = {'schema': 'pod-termination-evidence/v1', 'items': [], 'omitted': 0,
              'incident_window_bound': False, 'workload_ownership_proven': False,
              'catalog_limited_to_resolved_ids': True,
              'absence_is_refutation': False, 'execution_authorized': False}
    if not isinstance(cluster_id, str) or not cluster_id or not isinstance(namespace, str) or not namespace:
        return result
    seen = set()
    allowed = set(allowed_ids)
    for command in commands if isinstance(commands, list) else []:
        if (not isinstance(command, dict) or command.get('success') is not True
                or command.get('executed') is not True or command.get('outcome_unknown')
                or command.get('cluster_id') != cluster_id or command.get('namespace') != namespace):
            continue
        try:
            parsed = projection_request(command.get('command') or '', namespace)
            data = json.loads(command.get('data') or '')
        except (ValueError, TypeError, KeyError, IndexError):
            continue
        if not parsed or parsed[0] != 'Pod' or not isinstance(data, dict) or data.get('projection') != PROJECTION:
            continue
        collected = _time(command.get('timestamp'))
        if collected is None:
            continue
        for observation in committed_tool_observations(command):
            oid = observation_id(observation)
            raw = observation.get('raw_ref') or {}
            meta = raw.get('metadata') or {}
            if (oid not in allowed or raw.get('kind') != 'Pod' or meta.get('namespace') != namespace
                    or not all(isinstance(meta.get(k), str) and meta[k] for k in ('name','uid','resourceVersion'))):
                continue
            statuses = (raw.get('status') or {}).get('containerStatuses')
            if not isinstance(statuses, list):
                continue
            for status in statuses:
                if not isinstance(status, dict) or not isinstance(status.get('name'), str) or not status['name']:
                    continue
                for field, temporal in (('state','current_state'), ('lastState','previous_state')):
                    state = status.get(field)
                    terminated = state.get('terminated') if isinstance(state, dict) else None
                    if not isinstance(terminated, dict):
                        continue
                    reason = terminated.get('reason')
                    if not isinstance(reason, str) or not reason or len(reason) > 512:
                        continue
                    finished = terminated.get('finishedAt')
                    finished_s = _time(finished)
                    time_status = ('unavailable_or_invalid' if finished_s is None else
                                   'after_collection' if finished_s > collected + 5 else 'before_or_at_collection')
                    exit_code = terminated.get('exitCode')
                    if type(exit_code) is not int:
                        exit_code = None
                    key = (oid, status['name'], field)
                    if key in seen:
                        continue
                    seen.add(key)
                    if len(result['items']) >= MAX_TERMINATIONS:
                        result['omitted'] += 1
                        continue
                    result['items'].append({'observation_id': oid, 'cluster_id': cluster_id,
                        'namespace': namespace, 'pod': meta['name'], 'pod_uid': meta['uid'],
                        'resource_version': meta['resourceVersion'], 'container': status['name'],
                        'state': temporal, 'reason': reason, 'exit_code': exit_code,
                        'finished_at': finished if finished_s is not None else None,
                        'time_status': time_status, 'collected_at': command['timestamp'],
                        'causality_proven': False})
    return result
