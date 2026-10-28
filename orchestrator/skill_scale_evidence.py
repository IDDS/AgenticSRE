"""Assess narrowly scoped scale-zero evidence without authorizing actions."""
from __future__ import annotations

from skill_runtime.ontology_history import collection_time


def assess_scale_zero_evidence(*, root_entity, service, scope, judge):
    """Check cited observations; structural agreement is not causal proof."""
    result = {'schema': 'skill-scale-zero-evidence/v1', 'eligible_for_further_review': False,
              'execution_authorized': False, 'is_causal_proof': False, 'blockers': []}
    blockers = result['blockers']
    if not isinstance(root_entity, dict) or root_entity.get('kind') != 'Deployment' or not all(
            isinstance(root_entity.get(k), str) and root_entity[k]
            for k in ('cluster_id', 'namespace', 'name', 'uid', 'resource_version')):
        blockers.append('root_identity_unresolved')
        return result
    if not isinstance(service, str) or not service:
        blockers.append('service_missing')
    if (not isinstance(scope, dict) or scope.get('status') != 'collected' or scope.get('gaps') != []
            or scope.get('candidate_workloads') != [root_entity['name']]
            or type(scope.get('selected_pod_count')) is not int or scope['selected_pod_count'] != 0
            or scope.get('relation') != 'matches_workload_template'):
        blockers.append('service_scope_incomplete')
    if not isinstance(judge, dict) or judge.get('needs_review') is not False or judge.get('review_blockers'):
        blockers.append('independent_review_incomplete')
        return result
    review_meta = judge.get('skill_review') if isinstance(judge.get('skill_review'), dict) else {}
    receipt = review_meta.get('provider_receipt') or {}
    if not isinstance(receipt, dict) or type(receipt.get('schema_version')) is not int or any(
            type(receipt.get(k)) is not bool for k in ('refusal_present', 'tool_call_present')) or any(receipt.get(k) != v for k, v in {
            'schema_version': 1, 'source': 'provider_response', 'completion_status': 'complete',
            'finish_reason': 'stop', 'refusal_present': False, 'tool_call_present': False}.items()):
        blockers.append('review_receipt_incomplete')
    review = judge.get('llm_based') or {}
    cited = review.get('supported_evidence_ids') if isinstance(review, dict) else None
    facts = judge.get('review_evidence')
    if not isinstance(cited, list) or not isinstance(facts, list):
        blockers.append('review_evidence_missing')
        return result
    selected = {}
    for fact in facts:
        if not isinstance(fact, dict) or fact.get('id') not in cited:
            continue
        raw = fact.get('raw_ref') if isinstance(fact.get('raw_ref'), dict) else {}
        meta = raw.get('metadata') if isinstance(raw.get('metadata'), dict) else {}
        kind = raw.get('kind')
        expected_name = root_entity['name'] if kind == 'Deployment' else service
        if (kind not in {'Service', 'Endpoints', 'Deployment'} or meta.get('name') != expected_name
                or meta.get('namespace') != root_entity['namespace']
                or raw.get('cluster_id') != root_entity['cluster_id']
                or not isinstance(meta.get('uid'), str) or not meta['uid']
                or not isinstance(meta.get('resourceVersion'), str) or not meta['resourceVersion']
                or raw.get('topology_fields_complete') is False or collection_time(fact.get('timestamp')) is None):
            continue
        selected.setdefault(kind, []).append(fact)
    if any(len(selected.get(k, [])) != 1 for k in ('Service', 'Endpoints', 'Deployment')):
        blockers.append('cited_objects_incomplete_or_ambiguous')
        return result
    svc, endpoints, deploy = (selected[k][0]['raw_ref'] for k in ('Service', 'Endpoints', 'Deployment'))
    meta = deploy['metadata']
    dep_spec = deploy.get('spec') if isinstance(deploy.get('spec'), dict) else {}
    svc_spec = svc.get('spec') if isinstance(svc.get('spec'), dict) else {}
    template = dep_spec.get('template') if isinstance(dep_spec.get('template'), dict) else {}
    template_meta = template.get('metadata') if isinstance(template.get('metadata'), dict) else {}
    selector = svc_spec.get('selector')
    labels = template_meta.get('labels')
    subsets = endpoints.get('subsets')
    if (meta.get('uid') != root_entity['uid'] or meta.get('resourceVersion') != root_entity['resource_version']
            or meta.get('deletionTimestamp') or type(dep_spec.get('replicas')) is not int or dep_spec['replicas'] != 0):
        blockers.append('deployment_state_not_zero')
    if (not isinstance(selector, dict) or not selector or not isinstance(labels, dict)
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in selector.items())
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in labels.items())
            or svc_spec.get('type') == 'ExternalName' or any(labels.get(k) != v for k, v in selector.items())):
        blockers.append('selector_not_matched')
    if (not isinstance(subsets, list) or any(not isinstance(item, dict)
            or item.get('addresses') not in (None, []) for item in subsets)):
        blockers.append('ready_endpoints_not_empty')
    result['cited_evidence_ids'] = {k: selected[k][0]['id'] for k in ('Service', 'Endpoints', 'Deployment')}
    result['eligible_for_further_review'] = not blockers
    return result
