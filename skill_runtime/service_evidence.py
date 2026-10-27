"""Bounded, read-only Service discovery using observed selectors and identities.

The fixed JSONPath protocol emits one JSON array per field, including [] for a
missing field. It never requests container specs, env, or arbitrary annotations.
Template matches are structural candidates, not ownership or causal proofs.
"""
from __future__ import annotations

import json
import re
import shlex
from datetime import datetime, timezone

from .kubectl_safety import readonly_kubectl_args

PROJECTION = 'service-evidence/v1'
END_MARKER = PROJECTION + ':end'
MAX_TOOL_CALLS = 12
IDENTITY = ('metadata.name', 'metadata.namespace', 'metadata.uid', 'metadata.resourceVersion')
FIELDS = {
    'Service': IDENTITY + ('spec.selector', 'spec.type', 'spec.publishNotReadyAddresses'),
    'Endpoints': IDENTITY + ('subsets',),
    'Deployment': IDENTITY + ('spec.template.metadata.labels', 'spec.replicas',
        'status.readyReplicas', 'status.availableReplicas', 'metadata.deletionTimestamp'),
    'Pod': IDENTITY + ('metadata.labels', 'metadata.ownerReferences', 'spec.nodeName',
        'status.phase', 'status.conditions', 'status.containerStatuses'),
    'ReplicaSet': IDENTITY + ('metadata.ownerReferences', 'spec.template.metadata.labels', 'spec.replicas'),
}
RESOURCES = {'Service': 'service', 'Endpoints': 'endpoints', 'Deployment': 'deployment',
             'Pod': 'pod', 'ReplicaSet': 'replicaset'}
MAX_ROWS = 512
MAX_DATA = 262144


def _name(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}', value))


def _labels(value):
    if not isinstance(value, dict) or len(value) > 128:
        raise ValueError('invalid_labels')
    if any(not isinstance(k, str) or not re.fullmatch(r'[a-zA-Z0-9_./-]{1,253}', k)
           or not isinstance(v, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{0,63}', v)
           for k, v in value.items()):
        raise ValueError('invalid_labels')
    return dict(value)


def selector_text(labels):
    labels = _labels(labels)
    if not labels:
        raise ValueError('empty_selector')
    return ','.join(k + '=' + labels[k] for k in sorted(labels))


def projection_command(kind, namespace, name=None, selector=None):
    if kind not in FIELDS or name is not None and not _name(name):
        raise ValueError('invalid_projection_target')
    if name is None and kind not in {'Deployment', 'Pod'}:
        raise ValueError('inventory_not_supported')
    if selector is not None and (kind != 'Pod' or name is not None):
        raise ValueError('invalid_projection_selector')
    expression = ''.join('{.' + path + '}' for path in FIELDS[kind])
    if name is None:
        expression = '{range .items[*]}' + expression + '{end}'
    expression += '{"' + END_MARKER + '"}'
    args = ['get', RESOURCES[kind] + ('/' + name if name else 's'), '-n', namespace]
    if selector is not None:
        args += ['-l', selector_text(selector)]
    args += ['--chunk-size=0', '-o', 'jsonpath-as-json=' + expression]
    return shlex.join(readonly_kubectl_args(shlex.join(args), namespace))


def projection_request(command, namespace):
    """Recognize only our exact command grammar and fixed field schema."""
    args = readonly_kubectl_args(command, namespace)
    for kind in FIELDS:
        target = args[1] if len(args) > 1 else ''
        single = target.startswith(RESOURCES[kind] + '/')
        if not single and target != RESOURCES[kind] + 's':
            continue
        name = target.split('/', 1)[1] if single else None
        selector = None
        if '-l' in args:
            text = args[args.index('-l')+1]
            pairs = [row.split('=', 1) for row in text.split(',')]
            if any(len(row) != 2 for row in pairs) or len({row[0] for row in pairs}) != len(pairs):
                raise ValueError('invalid_projection_selector')
            selector = dict(pairs)
        try:
            expected = shlex.split(projection_command(kind, namespace, name, selector))
        except ValueError:
            continue
        if args == expected:
            return kind, name
    return None


def _safe_value(path, value):
    if path in {'spec.selector', 'metadata.labels', 'spec.template.metadata.labels'}:
        return _labels(value)
    if path in {'spec.replicas', 'status.readyReplicas', 'status.availableReplicas'}:
        if type(value) is not int or value < 0:
            raise ValueError('invalid_replica_count')
    if path == 'spec.publishNotReadyAddresses' and type(value) is not bool:
        raise ValueError('invalid_publish_not_ready')
    if path == 'metadata.ownerReferences':
        if not isinstance(value, list) or len(value) > 8:
            raise ValueError('invalid_owners')
        owners = []
        for owner in value:
            if not isinstance(owner, dict) or any(not isinstance(owner.get(k), str)
                    or not owner[k] or len(owner[k]) > 256 for k in ('kind', 'name', 'uid')):
                raise ValueError('invalid_owner_identity')
            if 'controller' in owner and type(owner['controller']) is not bool:
                raise ValueError('invalid_owner_controller')
            owners.append({k: owner[k] for k in ('kind', 'name', 'uid', 'controller') if k in owner})
        return owners
    if path == 'status.conditions':
        if not isinstance(value, list) or len(value) > 64:
            raise ValueError('invalid_conditions')
        return [{k: r[k] for k in ('type', 'status') if isinstance(r.get(k), str)}
                for r in value if isinstance(r, dict)]
    if path == 'status.containerStatuses':
        if not isinstance(value, list) or len(value) > 64:
            raise ValueError('invalid_container_statuses')
        return [{k: r[k] for k in ('name', 'ready', 'restartCount') if k in r}
                for r in value if isinstance(r, dict)]
    if path == 'subsets':
        if not isinstance(value, list) or len(value) > 1000:
            raise ValueError('invalid_endpoint_subsets')
        subsets = []
        for subset in value:
            if not isinstance(subset, dict):
                raise ValueError('invalid_endpoint_subset')
            safe = {}
            for group in ('addresses', 'notReadyAddresses'):
                addresses = subset.get(group, [])
                if addresses is None:
                    addresses = []
                if not isinstance(addresses, list) or len(addresses) > 1000:
                    raise ValueError('invalid_endpoint_addresses')
                safe[group] = []
                for address in addresses:
                    if not isinstance(address, dict) or not isinstance(address.get('ip'), str):
                        raise ValueError('invalid_endpoint_address')
                    ref = address.get('targetRef') or {}
                    if not isinstance(ref, dict):
                        raise ValueError('invalid_endpoint_target')
                    safe[group].append({'ip': address['ip'], 'targetRef': {
                        k: ref[k] for k in ('kind', 'name', 'namespace', 'uid') if isinstance(ref.get(k), str)}})
            subsets.append(safe)
        return subsets
    if path in IDENTITY or path in {'spec.type', 'spec.nodeName', 'status.phase', 'metadata.deletionTimestamp'}:
        if not isinstance(value, str) or not value or len(value) > 512:
            raise ValueError('invalid_text_field')
    return value


def project_endpoint_subsets(value):
    """Shared whitelist; malformed endpoint arrays must not become healthy/empty."""
    return _safe_value('subsets', [] if value is None else value)


def decode_projection(command, namespace, wire):
    request = projection_request(command, namespace)
    if request is None:
        raise ValueError('unrecognized_projection')
    if not isinstance(wire, str) or len(wire.encode()) > 2 * 1024 * 1024:
        raise ValueError('projection_wire_limit')
    kind, name = request
    decoder, cells, offset = json.JSONDecoder(), [], 0
    while offset < len(wire):
        while offset < len(wire) and wire[offset].isspace():
            offset += 1
        if offset == len(wire):
            break
        cell, offset = decoder.raw_decode(wire, offset)
        if not isinstance(cell, list) or len(cell) > 1:
            raise ValueError('invalid_projection_cell')
        cells.append(cell)
        if len(cells) > MAX_ROWS * len(FIELDS[kind]) + 1:
            raise ValueError('projection_row_limit')
    if not cells or cells.pop() != [END_MARKER]:
        raise ValueError('projection_end_marker_missing')
    width = len(FIELDS[kind])
    if len(cells) % width or name and len(cells) != width:
        raise ValueError('incomplete_projection_row')
    rows, seen = [], set()
    for start in range(0, len(cells), width):
        row = {'kind': kind, 'projection': PROJECTION}
        for index, path in enumerate(FIELDS[kind]):
            cell = cells[start+index]
            if not cell or cell == [None]:
                if path in IDENTITY:
                    raise ValueError('missing_projection_identity')
                # Empty Kubernetes endpoint slices can be omitted or null.
                if path == 'subsets':
                    row['subsets'] = []
                continue
            current = row
            parts = path.split('.')
            for part in parts[:-1]:
                current = current.setdefault(part, {})
            current[parts[-1]] = _safe_value(path, cell[0])
        meta = row['metadata']
        if meta['namespace'] != namespace or not _name(meta['name']) or name and meta['name'] != name:
            raise ValueError('projection_target_mismatch')
        if meta['name'] in seen:
            raise ValueError('duplicate_projection_identity')
        seen.add(meta['name'])
        rows.append(row)
    result = rows[0] if name else {'kind': 'List', 'items': rows, 'projection': PROJECTION,
                                 'inventory_complete': True}
    result['observed_at'] = datetime.now(timezone.utc).isoformat()
    if len(json.dumps(result).encode()) > MAX_DATA:
        raise ValueError('projection_data_limit')
    return result


def evidence_sample(data, budget=12000):
    """Never cut a projected JSON document mid-field to fit a model budget."""
    try:
        row = json.loads(data) if isinstance(data, str) else data
    except (ValueError, TypeError):
        return str(data or '')[:budget]
    if not isinstance(row, dict) or row.get('projection') != PROJECTION:
        return str(data or '')[:budget]
    text = json.dumps(row, ensure_ascii=False, separators=(',', ':'))
    if len(text) <= budget:
        return text
    summary = {k: row[k] for k in ('kind', 'metadata', 'projection', 'observed_at') if k in row}
    summary['excerpted'] = True
    if row.get('kind') == 'List':
        summary['observed_item_count'] = len(row.get('items', []))
        summary['items_omitted'] = True
    elif row.get('kind') == 'Endpoints':
        summary['ready_address_count'] = sum(len(s.get('addresses', [])) for s in row.get('subsets', []))
        summary['not_ready_address_count'] = sum(len(s.get('notReadyAddresses', [])) for s in row.get('subsets', []))
        # Legacy structured predicates inspect sample before full data. Preserve
        # one *actual* ready address so an excerpt cannot look like an empty
        # healthy Endpoints object. Identity/ports are deliberately not inferred.
        ready = next((a for s in row.get('subsets', []) for a in s.get('addresses', [])), None)
        summary['subsets'] = [{'addresses': [{'ip': ready['ip']}]}] if ready else []
    else:
        summary['detail_omitted'] = True
    return json.dumps(summary, ensure_ascii=False, separators=(',', ':'))


def evidence_observed_at(data):
    try:
        row = json.loads(data) if isinstance(data, str) else data
        return row.get('observed_at', '') if isinstance(row, dict) and row.get('projection') == PROJECTION else ''
    except (ValueError, TypeError):
        return ''


def observed_entity_catalog(commands, cluster_id, namespace):
    """Only successful committed, named reads may bind a model-selected identity."""
    catalog = []
    for index, call in enumerate(commands):
        if (not isinstance(call, dict) or call.get('executed') is not True or call.get('success') is not True
                or call.get('outcome_unknown') or call.get('cluster_id') != cluster_id
                or call.get('namespace') != namespace or not cluster_id):
            continue
        try:
            request = projection_request(call.get('command', ''), namespace)
            if request is None or request[1] is None:
                continue
            row = json.loads(call.get('data') or '')
            meta = row['metadata']
            if row.get('projection') != PROJECTION or row.get('kind') != request[0]:
                continue
            if meta['namespace'] != namespace or meta['name'] != request[1] or any(
                    not isinstance(meta.get(k), str) or not meta[k] for k in ('uid', 'resourceVersion')):
                continue
            catalog.append({'cluster_id': cluster_id, 'namespace': namespace, 'kind': row['kind'],
                'name': meta['name'], 'uid': meta['uid'], 'resource_version': meta['resourceVersion'],
                'source_command_index': index, 'is_causal_proof': False})
        except (ValueError, TypeError, KeyError):
            continue
    return catalog[:32]


def bind_root_entity(proposal, catalog, service_scope=None):
    validation = {'status': 'unresolved', 'is_causal_proof': False, 'independent_review_required': True}
    if not isinstance(proposal, dict):
        validation['reason'] = 'model_did_not_select_observed_entity'
        return None, validation
    keys = ('cluster_id', 'namespace', 'kind', 'name', 'uid', 'resource_version')
    if any(not isinstance(proposal.get(k), str) or not proposal[k] for k in keys):
        validation['reason'] = 'incomplete_entity_identity'
        return None, validation
    logical = [r for r in catalog if all(r[k] == proposal[k] for k in keys[:4])]
    if len({(r['uid'], r['resource_version']) for r in logical}) > 1:
        validation['reason'] = 'conflicting_entity_observations'
        return None, validation
    if not any(all(r[k] == proposal[k] for k in keys) for r in logical):
        validation['reason'] = 'entity_not_in_observed_catalog'
        return None, validation
    if service_scope is not None and service_scope.get('status') != 'collected':
        validation['reason'] = 'service_scope_not_resolved'
        return None, validation
    validation['status'] = 'identity_bound'
    return {k: proposal[k] for k in keys}, validation


def collect_service_evidence(tools, payload):
    namespace = payload.get('namespace') or 'default'
    service = payload.get('service')
    scope = {'status': 'incomplete', 'candidate_workloads': [], 'gaps': [], 'is_causal_proof': False,
             'snapshot_consistency': 'not_atomic'}
    results = []

    def read(kind, name=None, selector=None):
        command = projection_command(kind, namespace, name, selector)
        result = tools.execute('kubectl', command=command, namespace=namespace)
        results.append(result)
        if result.get('executed') is not True or result.get('success') is not True or result.get('outcome_unknown'):
            return None
        body = result.get('data')
        try:
            # The host normally normalizes before committing its tool trace.
            # Standalone Skill runners can return the same fixed wire protocol.
            if isinstance(body, str):
                try:
                    value = json.loads(body)
                except ValueError:
                    value = None
                if not isinstance(value, dict) or value.get('projection') != PROJECTION:
                    value = decode_projection(command, namespace, body)
            else:
                value = body
            if not isinstance(value, dict) or value.get('projection') != PROJECTION:
                return None
            return value
        except (ValueError, TypeError, IndexError):
            return None

    try:
        first = read('Service', service)
        if not first:
            scope['gaps'].append('service_unavailable')
            return results, scope
        endpoints = read('Endpoints', service)
        if endpoints is None:
            scope['gaps'].append('endpoints_unavailable')
        selector = first.get('spec', {}).get('selector')
        if not selector or first.get('spec', {}).get('type') == 'ExternalName':
            scope['status'] = 'unsupported_selector'
            return results, scope
        selector_text(selector)  # Validate before constructing any query.
        pods = read('Pod', selector=selector)
        deployments = read('Deployment')
        if pods is None:
            scope['gaps'].append('pod_inventory_unavailable')
        if deployments is None:
            scope['gaps'].append('deployment_inventory_unavailable')
        candidates = []
        for row in (deployments or {}).get('items', []):
            labels = row.get('spec', {}).get('template', {}).get('metadata', {}).get('labels', {})
            if all(labels.get(k) == v for k, v in selector.items()):
                candidates.append(row)
        scope['candidate_workloads'] = sorted(r['metadata']['name'] for r in candidates)
        scope['selected_pod_count'] = len(pods['items']) if pods is not None else None
        scope['relation'] = 'matches_workload_template'
        if len(candidates) == 1:
            target = candidates[0]
            current = read('Deployment', target['metadata']['name'])
            if not current or current['metadata']['uid'] != target['metadata']['uid'] or (
                    current.get('spec') != target.get('spec')) or current['metadata'].get('deletionTimestamp'):
                scope['gaps'].append('workload_changed_or_unavailable')
        elif len(candidates) > 1:
            scope['status'] = 'ambiguous'
        else:
            scope['gaps'].append('no_matching_deployment_template')
        # A real selected Pod's controller chain is separate from a template match.
        owners = {(ref.get('name'), ref.get('uid')) for pod in (pods or {}).get('items', [])
                  for ref in pod.get('metadata', {}).get('ownerReferences', [])
                  if ref.get('kind') == 'ReplicaSet' and ref.get('controller') is True}
        if len(owners) == 1 and len(results) < 7:
            name, uid = next(iter(owners))
            rs = read('ReplicaSet', name)
            if not rs or rs['metadata']['uid'] != uid:
                scope['gaps'].append('pod_controller_changed_or_unavailable')
            elif len(candidates) == 1 and not any(ref.get('kind') == 'Deployment'
                    and ref.get('controller') is True and ref.get('uid') == candidates[0]['metadata']['uid']
                    and ref.get('name') == candidates[0]['metadata']['name']
                    for ref in rs.get('metadata', {}).get('ownerReferences', [])):
                scope['gaps'].append('pod_controller_workload_conflict')
        elif len(owners) > 1:
            scope['gaps'].append('multiple_pod_controllers')
        elif (pods or {}).get('items'):
            scope['gaps'].append('selected_pod_controller_unresolved')
        last = read('Service', service)
        if not last or any(first['metadata'][key] != last['metadata'][key] for key in ('uid', 'resourceVersion')):
            scope['status'] = 'scope_changed'
            scope['gaps'].append('service_changed_during_collection')
        elif scope['status'] != 'ambiguous':
            scope['status'] = 'incomplete' if scope['gaps'] else 'collected'
        if scope['status'] == 'collected':
            checks = []
            selected = (pods or {}).get('items', [])
            if selected:
                checks.append(shlex.join(['kubectl', 'logs', 'pod/'+selected[0]['metadata']['name'],
                    '-n', namespace, '--tail=40', '--since=5m', '--timestamps=true']))
            event_uid = candidates[0]['metadata']['uid'] if len(candidates) == 1 else first['metadata']['uid']
            checks.append(shlex.join(['kubectl', 'get', 'events', '-n', namespace,
                '--field-selector=involvedObject.uid='+event_uid, '--sort-by=.lastTimestamp']))
            scope['supplemental_commands'] = checks
    except (ValueError, TypeError, KeyError, IndexError):
        scope['gaps'].append('invalid_service_scope')
    return results, scope
