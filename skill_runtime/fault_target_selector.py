"""Read-only injection target proposals grounded in Kubernetes object UIDs.

This module ranks structural coverage only. It does not authorize or execute a
fault, infer call traffic, or replace an execution-time identity/risk gate.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from typing import Any

_DNS = re.compile(r'^[a-z0-9](?:[a-z0-9.-]{0,61}[a-z0-9])?$')
_KINDS = {'Deployment', 'ReplicaSet', 'Pod', 'Service', 'EndpointSlice'}


def recommend_targets(resources: list[dict], *, cluster_id: str, allowed_namespaces: list[str],
                      allowed_deployment_uids: list[str], observed_at: str,
                      max_services: int = 1, max_ready_backends: int = 10,
                      pod_cpu_observations: list[dict] | None = None,
                      max_cpu_cores: float | None = None, max_cpu_age_seconds: int = 300) -> dict:
    if not isinstance(cluster_id, str) or not cluster_id.strip() or len(cluster_id) > 128:
        raise ValueError('cluster_id_required')
    if (not isinstance(allowed_namespaces, list) or not allowed_namespaces
            or any(not isinstance(ns, str) or not _DNS.fullmatch(ns) for ns in allowed_namespaces)):
        raise ValueError('explicit_namespaces_required')
    if (not isinstance(allowed_deployment_uids, list) or not 1 <= len(allowed_deployment_uids) <= 1000
            or any(not isinstance(uid, str) or not uid.strip() or len(uid) > 256 for uid in allowed_deployment_uids)):
        raise ValueError('explicit_deployment_uid_allowlist_required')
    allowed_uids = set(allowed_deployment_uids)
    if not isinstance(resources, list) or len(resources) > 10000:
        raise ValueError('bounded_resource_list_required')
    if (type(max_services) is not int or not 1 <= max_services <= 100 or
            type(max_ready_backends) is not int or not 1 <= max_ready_backends <= 1000):
        raise ValueError('invalid_blast_radius_limits')
    try:
        snapshot_time = dt.datetime.fromisoformat(observed_at.replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        raise ValueError('snapshot_time_required') from None
    if snapshot_time.tzinfo is None or snapshot_time.utcoffset() is None:
        raise ValueError('snapshot_time_requires_timezone')
    if type(max_cpu_age_seconds) is not int or not 1 <= max_cpu_age_seconds <= 3600:
        raise ValueError('invalid_cpu_evidence_age_limit')
    if max_cpu_cores is not None and (isinstance(max_cpu_cores, bool) or not isinstance(max_cpu_cores, (int, float))
                                      or not math.isfinite(max_cpu_cores) or not 0 < max_cpu_cores <= 1024):
        raise ValueError('invalid_cpu_ceiling')
    if pod_cpu_observations is not None and (not isinstance(pod_cpu_observations, list) or len(pod_cpu_observations) > 10000):
        raise ValueError('invalid_cpu_observations')
    if max_cpu_cores is None and pod_cpu_observations is not None:
        raise ValueError('cpu_ceiling_required_with_observations')
    if max_cpu_cores is not None and pod_cpu_observations is None:
        raise ValueError('cpu_observations_required_with_ceiling')
    namespaces = set(allowed_namespaces)
    by_uid: dict[str, dict] = {}
    for resource in resources:
        if not isinstance(resource, dict) or resource.get('kind') not in _KINDS:
            continue
        meta = resource.get('metadata')
        if not isinstance(meta, dict) or meta.get('namespace') not in namespaces:
            continue
        uid, name, version = meta.get('uid'), meta.get('name'), meta.get('resourceVersion')
        if not all(isinstance(v, str) and v and len(v) <= 256 for v in (uid, name, version)):
            continue
        if uid in by_uid:
            raise ValueError('duplicate_uid_in_snapshot')
        by_uid[uid] = resource

    cpu_by_pod: dict[str, dict] = {}
    for observation in pod_cpu_observations or []:
        if not isinstance(observation, dict):
            raise ValueError('invalid_cpu_observation')
        uid, name, ns = (observation.get(key) for key in ('pod_uid', 'pod_name', 'namespace'))
        cpu, path = observation.get('cpu_cores'), observation.get('cgroup_id')
        if (not isinstance(uid, str) or not uid or not isinstance(name, str) or not name
                or not isinstance(ns, str) or not ns or not isinstance(path, str)
                or not path.endswith('-pod' + uid.replace('-', '_') + '.slice')
                or type(observation.get('window_seconds')) is not int or observation['window_seconds'] != 300
                or isinstance(cpu, bool) or not isinstance(cpu, (int, float)) or not math.isfinite(cpu) or cpu < 0):
            raise ValueError('invalid_uid_bound_cpu_observation')
        try:
            measured = dt.datetime.fromisoformat(observation['observed_at'].replace('Z', '+00:00'))
            age = (snapshot_time - measured).total_seconds()
        except (KeyError, ValueError, AttributeError, TypeError):
            raise ValueError('invalid_cpu_observation_time') from None
        if uid in cpu_by_pod:
            raise ValueError('duplicate_cpu_observation')
        cpu_by_pod[uid] = {'namespace': ns, 'pod_name': name, 'cpu_cores': float(cpu),
                           'observed_at': measured.isoformat(), 'age_seconds': age}

    def owner_uid(obj: dict, expected_kind: str) -> str | None:
        meta = obj['metadata']
        refs = meta.get('ownerReferences')
        if not isinstance(refs, list):
            return None
        refs = [x for x in refs if isinstance(x, dict) and x.get('controller') is True]
        if len(refs) != 1 or refs[0].get('kind') != expected_kind:
            return None
        value = refs[0].get('uid')
        target = by_uid.get(value) if isinstance(value, str) else None
        if (not target or target.get('kind') != expected_kind or refs[0].get('name') != target['metadata']['name']
                or target['metadata']['namespace'] != meta['namespace']):
            return None
        return value

    def running_pod_owner(pod: dict) -> str | None:
        status = pod.get('status')
        if (pod['metadata'].get('deletionTimestamp') or not isinstance(status, dict)
                or status.get('phase') != 'Running'):
            return None
        rs = by_uid.get(owner_uid(pod, 'ReplicaSet'))
        if (not rs or rs.get('kind') != 'ReplicaSet'
                or rs['metadata']['namespace'] != pod['metadata']['namespace']):
            return None
        deployment_uid = owner_uid(rs, 'Deployment')
        dep = by_uid.get(deployment_uid)
        if not dep or dep.get('kind') != 'Deployment' or dep['metadata']['namespace'] != pod['metadata']['namespace']:
            return None
        return deployment_uid

    services = {uid: obj for uid, obj in by_uid.items() if obj['kind'] == 'Service'}
    service_names = {(obj['metadata']['namespace'], obj['metadata']['name']): uid for uid, obj in services.items()}
    coverage: dict[str, dict[str, dict]] = {}
    service_ready: dict[str, set[str]] = {}
    uncertain_services: set[str] = set()
    uncertain_namespaces: set[str] = set()
    skipped = []
    for _, item in by_uid.items():
        if item['kind'] != 'EndpointSlice' or item['metadata'].get('deletionTimestamp'):
            continue
        service_uid = owner_uid(item, 'Service')
        service = services.get(service_uid)
        if not service or service['metadata']['namespace'] != item['metadata']['namespace']:
            labels = item['metadata'].get('labels')
            named_service = labels.get('kubernetes.io/service-name') if isinstance(labels, dict) else None
            labelled_uid = service_names.get((item['metadata']['namespace'], named_service))
            if labelled_uid:
                uncertain_services.add(labelled_uid)
            else:
                uncertain_namespaces.add(item['metadata']['namespace'])
            skipped.append({'resource': item['metadata']['name'], 'reason': 'service_owner_uid_unverified'})
            continue
        service_spec = service.get('spec')
        selector = service_spec.get('selector') if isinstance(service_spec, dict) else None
        labels = item['metadata'].get('labels') or {}
        if (not isinstance(selector, dict) or not selector or not isinstance(labels, dict) or
                labels.get('kubernetes.io/service-name') != service['metadata']['name']):
            uncertain_services.add(service_uid)
            skipped.append({'resource': item['metadata']['name'], 'reason': 'service_selector_or_slice_label_unverified'})
            continue
        endpoints = item.get('endpoints')
        if not isinstance(endpoints, list):
            uncertain_services.add(service_uid)
            skipped.append({'resource': item['metadata']['name'], 'reason': 'endpoint_list_unavailable'})
            continue
        for endpoint in endpoints:
            if not isinstance(endpoint, dict) or not isinstance(endpoint.get('conditions'), dict):
                uncertain_services.add(service_uid)
                continue
            ready = endpoint['conditions'].get('ready')
            if ready is None or type(ready) is not bool:
                uncertain_services.add(service_uid)
                continue
            if not ready:
                continue
            ref = endpoint.get('targetRef') or {}
            if (not isinstance(ref, dict) or ref.get('kind') != 'Pod'
                    or not isinstance(ref.get('uid'), str) or not ref.get('uid')):
                uncertain_services.add(service_uid)
                continue
            service_ready.setdefault(service_uid, set()).add(ref['uid'])
            pod = by_uid.get(ref.get('uid'))
            if (not pod or pod['kind'] != 'Pod' or pod['metadata']['namespace'] != service['metadata']['namespace']
                    or ref.get('name') != pod['metadata']['name']
                    or ref.get('namespace') != pod['metadata']['namespace']):
                continue
            pod_labels = pod['metadata'].get('labels') or {}
            if not isinstance(pod_labels, dict) or not all(pod_labels.get(k) == v for k, v in selector.items()):
                continue
            owner = running_pod_owner(pod)
            if not owner:
                continue
            row = coverage.setdefault(owner, {}).setdefault(service_uid, {'service': service, 'pod_uids': set(), 'slices': set()})
            row['pod_uids'].add(pod['metadata']['uid'])
            row['slices'].add(item['metadata']['uid'])

    proposals, excluded = [], []
    for dep_uid, service_map in coverage.items():
        dep = by_uid[dep_uid]
        meta, status = dep['metadata'], dep.get('status') or {}
        if dep_uid not in allowed_uids:
            excluded.append({'deployment_uid': dep_uid, 'reason': 'deployment_not_in_allowlist'})
            continue
        if meta['namespace'] in uncertain_namespaces:
            excluded.append({'deployment_uid': dep_uid, 'reason': 'unattributed_endpoint_slice_in_namespace'})
            continue
        ready = status.get('readyReplicas') if isinstance(status, dict) else None
        if meta.get('deletionTimestamp') or type(ready) is not int or ready < 1:
            excluded.append({'deployment_uid': dep_uid, 'reason': 'deployment_not_ready'})
            continue
        if len(service_map) > max_services:
            excluded.append({'deployment_uid': dep_uid, 'reason': 'service_budget_exceeded'})
            continue
        if any(uid in uncertain_services for uid in service_map):
            excluded.append({'deployment_uid': dep_uid, 'reason': 'service_backends_uncertain'})
            continue
        if any(len(service_ready.get(uid, set())) > max_ready_backends for uid in service_map):
            excluded.append({'deployment_uid': dep_uid, 'reason': 'backend_budget_exceeded'})
            continue
        services_for_target = []
        for uid, row in sorted(service_map.items()):
            services_for_target.append({'name': row['service']['metadata']['name'], 'uid': uid,
                                        'observed_ready_backends': len(service_ready.get(uid, set())),
                                        'pod_uids': sorted(row['pod_uids']), 'endpoint_slice_uids': sorted(row['slices'])})
        # This score expresses observed structural test coverage only, not
        # production importance, probability of failure, or safety to inject.
        observed_pods = len({p for row in service_map.values() for p in row['pod_uids']})
        pod_uids = {p for row in service_map.values() for p in row['pod_uids']}
        cpu_values = []
        if max_cpu_cores is not None:
            for uid in sorted(pod_uids):
                pod, reading = by_uid[uid], cpu_by_pod.get(uid)
                if (not reading or reading['namespace'] != meta['namespace']
                        or reading['pod_name'] != pod['metadata']['name']
                        or not 0 <= reading['age_seconds'] <= max_cpu_age_seconds):
                    break
                cpu_values.append(reading['cpu_cores'])
            if len(cpu_values) != len(pod_uids):
                excluded.append({'deployment_uid': dep_uid, 'reason': 'cpu_evidence_missing_or_stale'})
                continue
            if max(cpu_values) > max_cpu_cores:
                excluded.append({'deployment_uid': dep_uid, 'reason': 'cpu_ceiling_exceeded'})
                continue
        proposals.append({'cluster_id': cluster_id, 'namespace': meta['namespace'], 'kind': 'Deployment',
                          'name': meta['name'], 'uid': dep_uid, 'resource_version': meta['resourceVersion'],
                          'ready_replicas': ready, 'observed_backend_pods': observed_pods,
                          'services': services_for_target,
                          'coverage_score': 3 * len(services_for_target) + min(observed_pods, 5),
                          'max_pod_cpu_cores_5m': max(cpu_values) if cpu_values else None,
                          'requires_fresh_observation': True, 'execution_authorized': False})
    proposals.sort(key=lambda x: (-x['coverage_score'], x['max_pod_cpu_cores_5m'] if x['max_pod_cpu_cores_5m'] is not None else 0,
                                  x['namespace'], x['name'], x['uid']))
    return {'status': 'proposal_only', 'observed_at': snapshot_time.isoformat(),
            'candidates': proposals, 'excluded': excluded, 'skipped': skipped,
            'limits': ['Coverage score uses observed structure; CPU is an optional reported observation, not CPU headroom or failure probability.',
                       'CPU timestamps and snapshots are caller-supplied; freshness and provenance must be rechecked at execution.',
                       'Caller-supplied namespace and deployment allowlists are not execution authorization.',
                       'Selection does not replace live identity checks, approval, cleanup, or execution policy.']}
