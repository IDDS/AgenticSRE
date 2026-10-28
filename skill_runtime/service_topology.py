"""Shared bounded projections and evidence-backed Service relations, never health proofs.

No network access, name conventions, IP-to-Pod guesses, or resourceVersion ordering.
Only observed object incarnations participate in verified relations.
"""
from __future__ import annotations

import copy
import hashlib
import json
from .service_evidence import project_endpoint_subsets, projection_request


RESOURCE_KINDS = {'Service', 'EndpointSlice', 'Endpoints', 'Pod', 'ReplicaSet',
                  'Deployment', 'StatefulSet', 'DaemonSet', 'Job', 'CronJob', 'Node'}
WORKLOAD_KINDS = {'Deployment', 'ReplicaSet', 'StatefulSet', 'DaemonSet', 'Job'}
MAX_RESOURCES = 1024


def _dict(value):
    return value if isinstance(value, dict) else {}


def project_resource(resource):
    """Whitelist topology fields; never carry env, annotations or arbitrary spec."""
    if (not isinstance(resource, dict) or not isinstance(resource.get('kind'), str)
            or resource['kind'] not in RESOURCE_KINDS):
        return None
    meta = _dict(resource.get('metadata'))
    if not isinstance(meta.get('name'), str) or not meta['name']:
        return None
    complete = True

    def labels(value):
        nonlocal complete
        if value is None:
            return {}
        if (not isinstance(value, dict) or len(value) > 128 or
                any(not isinstance(k, str) or not k or len(k) > 256 or
                    not isinstance(v, str) or len(v) > 512 for k, v in value.items())):
            complete = False
            return {}
        return dict(value)

    owners = meta.get('ownerReferences')
    if owners is None:
        owners = []
    if not isinstance(owners, list) or len(owners) > 8:
        complete, owners = False, []
    projected = {'kind': resource['kind'], 'metadata': {
        k: meta[k] for k in ('name', 'namespace', 'uid', 'resourceVersion', 'deletionTimestamp')
        if isinstance(meta.get(k), str)}, 'projection': 'resource-topology/v1'}
    projected['metadata']['labels'] = labels(meta.get('labels'))
    safe_owners = []
    for row in owners:
        if (not isinstance(row, dict) or any(k in row and
                (not isinstance(row[k], str) or len(row[k]) > 512) for k in ('kind', 'name', 'uid'))
                or 'controller' in row and row['controller'] is not None and type(row['controller']) is not bool):
            complete = False
            continue
        safe_owners.append({k: row[k] for k in ('kind', 'name', 'uid', 'controller') if k in row})
    projected['metadata']['ownerReferences'] = safe_owners
    if resource.get('spec') is not None and not isinstance(resource['spec'], dict):
        complete = False
    spec = _dict(resource.get('spec'))
    safe_spec = {k: copy.deepcopy(spec[k]) for k in
                 ('nodeName', 'nodeSelector', 'replicas', 'type', 'publishNotReadyAddresses') if k in spec}
    if 'selector' in spec:
        safe_spec['selector'] = (labels(spec['selector']) if resource['kind'] == 'Service'
                                 else copy.deepcopy(spec['selector']))
    template_meta = _dict(_dict(spec.get('template')).get('metadata'))
    if 'template' in spec:
        safe_spec['template'] = {'metadata': {'labels': labels(template_meta.get('labels'))}}
    projected['spec'] = safe_spec
    status = _dict(resource.get('status'))
    projected['status'] = {k: copy.deepcopy(status[k]) for k in
        ('phase', 'readyReplicas', 'availableReplicas', 'containerStatuses', 'podIP', 'podIPs') if k in status}
    if resource['kind'] == 'Endpoints':
        try:
            projected['subsets'] = project_endpoint_subsets(resource.get('subsets'))
        except ValueError:
            complete = False
            projected['subsets'] = []
    if resource['kind'] == 'EndpointSlice':
        endpoints = resource.get('endpoints')
        explicit_nil = 'endpoints' in resource and endpoints is None
        if explicit_nil:
            endpoints = []  # Kubernetes can serialize an empty Go slice as null.
        if not isinstance(endpoints, list) or len(endpoints) > 1000:
            complete, endpoints = False, []
        projected['endpoint_array_encoding'] = ('null_as_empty' if explicit_nil else
            resource.get('endpoint_array_encoding', 'array'))
        projected['addressType'] = resource.get('addressType')
        projected['endpoints'] = []
        for row in endpoints:
            if not isinstance(row, dict):
                complete = False
                continue
            conditions = row.get('conditions')
            if conditions is None:
                conditions = {}
            if not isinstance(conditions, dict) or any(
                    k in conditions and conditions[k] is not None and type(conditions[k]) is not bool
                    for k in ('ready', 'serving', 'terminating')):
                complete = False
                continue
            addresses = row.get('addresses')
            if not isinstance(addresses, list) or not 1 <= len(addresses) <= 8 or any(
                    not isinstance(a, str) or len(a) > 256 for a in addresses):
                complete = False
                continue
            ref = _dict(row.get('targetRef'))
            if (row.get('targetRef') is not None and not isinstance(row['targetRef'], dict)
                    or any(k in ref and (not isinstance(ref[k], str) or len(ref[k]) > 512)
                           for k in ('kind', 'name', 'namespace', 'uid'))):
                complete = False
                continue
            projected['endpoints'].append({'addresses': list(addresses),
                'conditions': {k: conditions.get(k) for k in ('ready', 'serving', 'terminating')},
                'targetRef': {k: ref[k] for k in ('kind', 'name', 'namespace', 'uid') if k in ref}})
    projected['topology_fields_complete'] = complete and resource.get('topology_fields_complete') is not False
    for key in ('cluster_id', 'observed_at', 'topology_source'):
        if key in resource:
            projected[key] = resource[key]
    return projected


def topology_relations(objects, observations):
    """Positive structural facts only. Missing inventories do not prove absence."""
    gaps, by_id = set(), {}
    if len(observations) > 4096:
        return [], ['topology_observation_limit_exceeded']
    for obs in observations:
        obj = objects.get(obs.get('entity_id'))
        raw = _dict(obs.get('raw_ref'))
        if (not obj or obj.get('identity_status') != 'uid_bound' or
                obs.get('scope_status') != 'in_run' or obs.get('temporality') == 'historical' or
                str(obs.get('origin', '')).endswith('excerpt') or raw.get('truncated')):
            continue
        # The selector-only Deployment read is a complementary, deliberately
        # narrow projection. Keep the observation, but do not compare its
        # incomplete structural fields with a full topology projection.
        if obs.get('origin') == 'committed_tool' and obj.get('kind') == 'Deployment':
            try:
                request = projection_request(obs.get('source') or '', obj.get('namespace') or '')
            except (ValueError, TypeError, IndexError):
                request = None
            if request and request[0] == 'DeploymentSelector' and request[1] == obj.get('name'):
                continue
        projected = project_resource(raw)
        if not projected:
            continue
        if not projected['topology_fields_complete']:
            gaps.add('topology_projection_incomplete')
            continue
        # Repeated identical structure can cite any of its observations. Conflicting
        # versions cannot be resolved by treating opaque resourceVersion as a clock.
        signature = json.dumps({k: projected.get(k) for k in ('kind', 'spec', 'endpoints', 'addressType')} | {
            'labels': projected['metadata']['labels'],
            'owners': projected['metadata']['ownerReferences']}, sort_keys=True)
        by_id.setdefault(obj['id'], []).append((signature, obj, projected, obs))
    active = {}
    for identity, rows in by_id.items():
        if len({r[0] for r in rows}) > 1:
            gaps.add('conflicting_topology_observations')
        else:
            active[identity] = rows[0][1:]
    incarnations = {}
    for identity, (obj, _, _) in active.items():
        logical = tuple(obj[k] for k in ('cluster_id', 'namespace', 'kind', 'name'))
        incarnations.setdefault(logical, []).append(identity)
    for ids in incarnations.values():
        if len(ids) > 1:
            gaps.add('topology_instance_ambiguous')
            for identity in ids:
                active.pop(identity, None)
    indexed = {(obj['cluster_id'], obj['namespace'], obj['kind'], obj['name'], obj['uid']): record
               for record in active.values() for obj in [record[0]]}
    services = {(obj['cluster_id'], obj['namespace'], obj['name']): record
                for record in active.values() for obj in [record[0]] if obj['kind'] == 'Service'}
    links, seen = [], set()

    def link(kind, source, target, **extra):
        a, _, a_obs = source
        b, _, b_obs = target
        evidence_ids = sorted({a_obs['id'], b_obs['id']})
        key = json.dumps([kind, a['id'], b['id'], evidence_ids, extra], sort_keys=True)
        if key in seen:
            return
        seen.add(key)
        links.append({'id': 'rel-' + hashlib.sha256(key.encode()).hexdigest()[:24],
            'source_id': a['id'], 'target_id': b['id'], 'type': kind,
            'evidence_id': a_obs['id'], 'evidence_ids': evidence_ids,
            'observed_at': a_obs.get('observed_at'),
            'observation_times': {o['id']: o.get('observed_at') for o in (a_obs, b_obs)},
            'snapshot_consistency': 'not_atomic', 'is_causal_proof': False, **extra})

    for record in active.values():
        obj, raw, _ = record
        for ref in raw['metadata']['ownerReferences']:
            if ref.get('controller') is not True:
                continue
            target = indexed.get((obj['cluster_id'], obj['namespace'], ref.get('kind'),
                                  ref.get('name'), ref.get('uid')))
            if target:
                link('controlled_by', record, target)
        if obj['kind'] != 'Service':
            continue
        spec = raw['spec']
        selector = spec.get('selector')
        if not selector or spec.get('type') == 'ExternalName':
            continue
        for target in active.values():
            other, target_raw, _ = target
            if (other['cluster_id'], other['namespace']) != (obj['cluster_id'], obj['namespace']):
                continue
            if other['kind'] == 'Pod':
                labels = target_raw['metadata']['labels']
                kind = 'selects_pod'
            elif other['kind'] in WORKLOAD_KINDS:
                labels = _dict(_dict(_dict(target_raw['spec'].get('template')).get('metadata')).get('labels'))
                kind = 'matches_workload_template'
            else:
                continue
            if all(labels.get(k) == v for k, v in selector.items()):
                link(kind, record, target, binding='observed_label_equality', selector=selector,
                     proves_runtime_ownership=False)

    for record in active.values():
        obj, raw, _ = record
        if obj['kind'] != 'EndpointSlice':
            continue
        service_name = raw['metadata']['labels'].get('kubernetes.io/service-name')
        service = services.get((obj['cluster_id'], obj['namespace'], service_name))
        if service is None:
            gaps.add('endpoint_slice_service_not_observed')
            continue
        service_obj = service[0]
        refs = [r for r in raw['metadata']['ownerReferences'] if r.get('kind') == 'Service']
        if refs:
            if len(refs) != 1 or (refs[0].get('name'), refs[0].get('uid')) != (service_obj['name'], service_obj['uid']):
                gaps.add('endpoint_slice_service_owner_mismatch')
                continue
            link('has_endpoint_slice', service, record, binding='service_label_and_owner_uid')
        else:
            # Supported manual slices use a name label. Do not upgrade this into
            # proof of the current Service incarnation after a same-name rebuild.
            link('endpoint_slice_label_association', service, record, binding='service_name_label_only',
                 service_instance_verified=False)
        for endpoint in raw['endpoints']:
            ref = endpoint['targetRef']
            target = indexed.get((obj['cluster_id'], ref.get('namespace') or obj['namespace'],
                                  ref.get('kind'), ref.get('name'), ref.get('uid')))
            if not target or target[0]['kind'] != 'Pod' or target[0]['namespace'] != obj['namespace']:
                gaps.add('endpoint_target_unresolved')
                continue
            cond = endpoint['conditions']
            link('endpoint_targets_pod', record, target, addresses=endpoint['addresses'],
                endpoint_conditions={**cond, 'effective_ready': cond['ready'] is not False,
                    'effective_serving': cond['serving'] is not False,
                    'effective_terminating': cond['terminating'] is True},
                publish_not_ready_addresses=service[1]['spec'].get('publishNotReadyAddresses') is True,
                readiness_is_health_proof=False)
    return links, sorted(gaps)
