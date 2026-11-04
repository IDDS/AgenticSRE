"""Bounded endpoint follow-up for structurally related zero-replica workloads."""
from datetime import datetime, timezone

from agents.evidence import make_evidence_item
from agents.service_endpoint_detector import inspect_service_endpoints


def observe_related_service_endpoints(kubectl, namespace, state, deployments):
    """Collect UID-bound endpoint state; template matching is not ownership."""
    if (not isinstance(namespace, str) or not namespace or not isinstance(state, dict)
            or not isinstance(deployments, list) or len(deployments) != 1):
        return {'status': 'unknown', 'reason': 'target_scope_not_unique', 'evidence_items': []}
    target = deployments[0]
    resources = state.get('topology_resources')
    if (not isinstance(target, dict) or target.get('namespace') != namespace
            or not all(isinstance(target.get(k), str) and target[k].strip() for k in ('name', 'uid', 'resource_version'))
            or not isinstance(resources, list)):
        return {'status': 'unknown', 'reason': 'topology_missing', 'evidence_items': []}
    collection = state.get('topology_collection') or {}
    workload_collection = collection.get('workload_resources') if isinstance(collection, dict) else None
    if not isinstance(workload_collection, dict) or workload_collection.get('status') != 'complete':
        return {'status': 'unknown', 'reason': 'workload_collection_incomplete', 'evidence_items': []}
    workloads = []
    for resource in resources:
        if not isinstance(resource, dict) or resource.get('kind') != 'Deployment':
            continue
        meta = resource.get('metadata') or {}
        if (isinstance(meta, dict) and meta.get('namespace') == namespace and meta.get('name') == target.get('name')
                and meta.get('uid') == target.get('uid') and meta.get('resourceVersion') == target.get('resource_version')):
            workloads.append(resource)
    if len(workloads) != 1:
        return {'status': 'unknown', 'reason': 'workload_identity_ambiguous', 'evidence_items': []}
    workload = workloads[0]
    wmeta, wspec = workload.get('metadata') or {}, workload.get('spec') or {}
    if (workload.get('topology_fields_complete') is not True or wmeta.get('deletionTimestamp')
            or not isinstance(wspec, dict) or type(wspec.get('replicas')) is not int or wspec['replicas'] != 0):
        return {'status': 'unknown', 'reason': 'workload_state_incomplete', 'evidence_items': []}
    template = wspec.get('template') or {}
    tmeta = template.get('metadata') if isinstance(template, dict) else None
    labels = tmeta.get('labels') if isinstance(tmeta, dict) else None
    if not isinstance(labels, dict) or not labels:
        return {'status': 'unknown', 'reason': 'workload_labels_missing', 'evidence_items': []}
    services = []
    identities = set()
    for resource in resources:
        if not isinstance(resource, dict) or resource.get('kind') != 'Service':
            continue
        meta, spec = resource.get('metadata'), resource.get('spec')
        if not isinstance(meta, dict) or not isinstance(spec, dict) or meta.get('namespace') != namespace:
            continue
        selector = spec.get('selector')
        if not isinstance(selector, dict) or not selector or spec.get('type') == 'ExternalName':
            continue
        if not all(isinstance(k, str) and isinstance(v, str) and labels.get(k) == v for k, v in selector.items()):
            continue
        if (resource.get('topology_fields_complete') is not True or meta.get('deletionTimestamp')
                or not all(isinstance(meta.get(k), str) and meta[k].strip() for k in ('name', 'uid', 'resourceVersion'))
                or meta['name'] in identities):
            return {'status': 'unknown', 'reason': 'service_identity_ambiguous', 'evidence_items': []}
        identities.add(meta['name'])
        services.append(meta)
    if not 1 <= len(services) <= 2:
        return {'status': 'unknown', 'reason': 'related_service_count_invalid', 'evidence_items': []}
    facts, unknown = [], 0
    for service in services:
        result = inspect_service_endpoints(kubectl, namespace, service['name'], service['uid'])
        if (not isinstance(result, dict) or result.get('status') not in ('healthy', 'unavailable')
                or not isinstance(result.get('service_resource_version'), str)
                or result['service_resource_version'] != service['resourceVersion']
                or type(result.get('ready_endpoints')) is not int or result['ready_endpoints'] < 0
                or not isinstance(result.get('slice_uids'), list) or not result['slice_uids']
                or any(not isinstance(uid, str) or not uid for uid in result['slice_uids'])
                or not isinstance(result.get('slice_versions'), list)
                or len(result['slice_versions']) != len(result['slice_uids'])
                or any(not isinstance(v, dict) or not isinstance(v.get('uid'), str)
                       or not isinstance(v.get('resource_version'), str) or not v['resource_version']
                       for v in result['slice_versions'])
                or sorted(v['uid'] for v in result['slice_versions']) != sorted(result['slice_uids'])):
            unknown += 1
            continue
        stamp = datetime.now(timezone.utc).isoformat()
        raw = {'kind': 'Service', 'name': service['name'], 'namespace': namespace,
               'uid': service['uid'], 'resource_version': result['service_resource_version'],
               'observed_at': stamp, 'endpoint_status': result['status'],
               'ready_endpoints': result['ready_endpoints'], 'slice_uids': result['slice_uids'],
               'slice_versions': result.get('slice_versions', []),
               'related_workload': {'kind': 'Deployment', 'name': target['name'], 'uid': target['uid'],
                                    'resource_version': target['resource_version']},
               'association': 'selector_matches_workload_template_not_ownership', 'atomic_snapshot': False}
        description = (f"Service/{service['name']} UID-bound EndpointSlice observation: {result['status']}, "
                       f"ready endpoints={result['ready_endpoints']}. The selector matches a workload template; "
                       'the relation is not proof of ownership or causation and reads are not atomic.')
        facts.append(make_evidence_item('service_endpoint_slice_followup', description, service=service['name'],
            source='kubectl UID-bound Service and EndpointSlice reads', timestamp=stamp, raw_ref=raw))
    return {'status': 'complete' if not unknown else 'partial', 'unknown_count': unknown, 'evidence_items': facts}
