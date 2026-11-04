"""Bounded, read-only HPA configuration observations; never audit attribution."""
import json
from datetime import datetime, timezone

from agents.evidence import make_evidence_item


def observe_hpa_targets(kubectl, namespace, deployments):
    """Observe current HPA references by name, without inferring past actions."""
    if not isinstance(namespace, str) or not namespace or not isinstance(deployments, list):
        return {'status': 'unknown', 'reason': 'scope_incomplete', 'evidence_items': []}
    targets = {}
    for item in deployments:
        if not isinstance(item, dict) or item.get('namespace') != namespace:
            continue
        if not all(isinstance(item.get(key), str) and item[key].strip()
                   for key in ('name', 'uid', 'resource_version')):
            continue
        if item['name'] in targets and targets[item['name']] != item:
            return {'status': 'unknown', 'reason': 'ambiguous_deployment_identity', 'evidence_items': []}
        targets[item['name']] = item
    if not targets or len(targets) > 25:
        return {'status': 'unknown', 'reason': 'target_count_invalid', 'evidence_items': []}
    if kubectl is None:
        return {'status': 'unknown', 'reason': 'kubectl_unavailable', 'evidence_items': []}
    try:
        response = kubectl.execute(command='get hpa -o json', namespace=namespace, timeout=5)
        if not response.success:
            return {'status': 'unknown', 'reason': 'hpa_query_failed', 'evidence_items': []}
        data = json.loads(response.data) if isinstance(response.data, str) else response.data
        if (not isinstance(data, dict) or data.get('kind') not in ('List', 'HorizontalPodAutoscalerList')
                or not isinstance(data.get('items'), list)):
            raise ValueError('invalid_list')
        metadata = data.get('metadata') or {}
        if (not isinstance(metadata, dict) or metadata.get('continue')
                or metadata.get('remainingItemCount') not in (None, 0) or len(data['items']) > 1000):
            raise ValueError('incomplete_list')
        matched = {name: [] for name in targets}
        seen = set()
        for hpa in data['items']:
            if not isinstance(hpa, dict) or hpa.get('kind') != 'HorizontalPodAutoscaler':
                raise ValueError('invalid_hpa')
            meta, spec = hpa.get('metadata'), hpa.get('spec')
            if not isinstance(meta, dict) or not isinstance(spec, dict):
                raise ValueError('invalid_hpa')
            ref = spec.get('scaleTargetRef')
            if (meta.get('namespace') != namespace or meta.get('deletionTimestamp')
                    or not all(isinstance(meta.get(key), str) and meta[key].strip()
                               for key in ('name', 'uid', 'resourceVersion'))
                    or not isinstance(ref, dict) or not all(isinstance(ref.get(key), str) and ref[key].strip()
                               for key in ('apiVersion', 'kind', 'name')) or meta['uid'] in seen):
                raise ValueError('incomplete_hpa_identity')
            seen.add(meta['uid'])
            if ref['kind'] == 'Deployment' and ref['name'] in matched and ref['apiVersion'] != 'apps/v1':
                raise ValueError('unrecognized_deployment_api_version')
            if ref['apiVersion'] == 'apps/v1' and ref['kind'] == 'Deployment' and ref['name'] in matched:
                minimum, maximum = spec.get('minReplicas'), spec.get('maxReplicas')
                if ((minimum is not None and (type(minimum) is not int or minimum < 0))
                        or type(maximum) is not int or maximum < 1
                        or (minimum is not None and minimum > maximum)):
                    raise ValueError('invalid_hpa_replicas')
                matched[ref['name']].append({'name': meta['name'], 'uid': meta['uid'],
                                             'resource_version': meta['resourceVersion'],
                                             'min_replicas': minimum, 'max_replicas': maximum})
        stamp = datetime.now(timezone.utc).isoformat()
        facts = []
        for name, deployment in targets.items():
            links = matched[name]
            raw = {'kind': 'Deployment', 'name': name, 'namespace': namespace,
                   'uid': deployment['uid'], 'resource_version': deployment['resource_version'],
                   'observed_at': stamp, 'deployment_observed_at': deployment.get('observed_at'),
                   'atomic_snapshot': False, 'hpa_target_reference_binding': 'name_only',
                   'matching_hpas': links, 'temporal_scope': 'current_configuration_only'}
            text = (f'Current namespace HPA list contains {len(links)} HPA(s) targeting Deployment/{name} by name. '
                    'Deployment and HPA observations are not atomic. '
                    'This does not establish historical configuration, scaling actor, or cause.')
            facts.append(make_evidence_item('deployment_hpa_configuration_observed', text, service=name,
                source='kubectl get hpa -o json', timestamp=stamp, raw_ref=raw))
        return {'status': 'complete', 'target_count': len(targets), 'evidence_items': facts}
    except Exception:
        return {'status': 'unknown', 'reason': 'hpa_response_invalid_or_unavailable', 'evidence_items': []}
