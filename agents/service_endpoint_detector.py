"""Read-only, UID-bound service endpoint availability observation."""
import json
import re

_NAME = re.compile(r'^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$')
_NAMESPACE = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$')


def inspect_service_endpoints(kubectl, namespace, service, service_uid):
    """Return healthy/unavailable/unknown; missing data is never an outage."""
    if (not isinstance(namespace, str) or not _NAMESPACE.fullmatch(namespace)
            or not isinstance(service, str) or not _NAME.fullmatch(service)
            or not isinstance(service_uid, str) or not service_uid.strip() or len(service_uid) > 256):
        return {'status': 'unknown', 'reason': 'invalid_watched_identity'}
    if kubectl is None:
        return {'status': 'unknown', 'reason': 'kubectl_unavailable'}
    try:
        svc = kubectl.execute(command=f'get service {service} -o json', namespace=namespace, timeout=2)
        if not svc.success:
            return {'status': 'unknown', 'reason': 'service_query_failed'}
        data = json.loads(svc.data) if isinstance(svc.data, str) else svc.data
        if not isinstance(data, dict):
            return {'status': 'unknown', 'reason': 'invalid_service_response'}
        meta, spec = data.get('metadata') or {}, data.get('spec') or {}
        if (data.get('kind') != 'Service' or not isinstance(meta, dict) or not isinstance(spec, dict)
                or meta.get('namespace') != namespace or meta.get('name') != service
                or meta.get('uid') != service_uid or not meta.get('resourceVersion')
                or meta.get('deletionTimestamp') or not isinstance(spec.get('selector'), dict)
                or not spec.get('selector') or spec.get('type') == 'ExternalName'):
            return {'status': 'unknown', 'reason': 'service_identity_or_selector_unverified'}
        response = kubectl.execute(command=f'get endpointslices -l kubernetes.io/service-name={service} -o json',
                                   namespace=namespace, timeout=2)
        if not response.success:
            return {'status': 'unknown', 'reason': 'endpointslice_query_failed'}
        listing = json.loads(response.data) if isinstance(response.data, str) else response.data
        if not isinstance(listing, dict) or not isinstance(listing.get('items'), list):
            return {'status': 'unknown', 'reason': 'invalid_endpointslice_response'}
        items = listing['items']
        listing_meta = listing.get('metadata') or {}
        if not isinstance(listing_meta, dict) or listing_meta.get('continue') or not 1 <= len(items) <= 1000:
            return {'status': 'unknown', 'reason': 'missing_or_incomplete_endpointslice_list'}
        ready = uncertain = 0
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                return {'status': 'unknown', 'reason': 'invalid_endpointslice'}
            m = item.get('metadata') or {}
            if not isinstance(m, dict):
                return {'status': 'unknown', 'reason': 'invalid_endpointslice_metadata'}
            refs = m.get('ownerReferences') or []
            owners = [r for r in refs if isinstance(r, dict) and r.get('controller') is True] if isinstance(refs, list) else []
            labels = m.get('labels') or {}
            if (item.get('kind') != 'EndpointSlice' or m.get('namespace') != namespace
                    or not m.get('uid') or not m.get('resourceVersion') or m.get('deletionTimestamp')
                    or m['uid'] in seen or not isinstance(labels, dict)
                    or labels.get('kubernetes.io/service-name') != service or len(owners) != 1
                    or owners[0].get('kind') != 'Service' or owners[0].get('name') != service
                    or owners[0].get('uid') != service_uid or 'endpoints' not in item):
                return {'status': 'unknown', 'reason': 'endpointslice_identity_unverified'}
            seen.add(m['uid'])
            endpoints = item['endpoints']
            if endpoints is None:
                endpoints = []
            if not isinstance(endpoints, list):
                return {'status': 'unknown', 'reason': 'invalid_endpoint_array'}
            for endpoint in endpoints:
                if not isinstance(endpoint, dict) or not isinstance(endpoint.get('conditions'), dict):
                    uncertain += 1
                    continue
                condition = endpoint['conditions']
                if condition.get('terminating') is True:
                    continue
                if condition.get('ready') is True:
                    ready += 1
                elif condition.get('ready') is not False:
                    uncertain += 1
        status = 'healthy' if ready else 'unknown' if uncertain else 'unavailable'
        return {'status': status, 'namespace': namespace, 'service': service, 'service_uid': service_uid,
                'service_resource_version': meta['resourceVersion'], 'ready_endpoints': ready,
                'uncertain_endpoints': uncertain, 'slice_uids': sorted(seen)}
    except (ValueError, TypeError, KeyError, AttributeError):
        return {'status': 'unknown', 'reason': 'malformed_or_unavailable_observation'}
    except Exception:
        return {'status': 'unknown', 'reason': 'observation_failed'}
