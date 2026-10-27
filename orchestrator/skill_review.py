"""Independent role review of committed Skill observations, never action admission."""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone

from diagnostic_strategy import hypothesis_review_context, finite_confidence, falsification_evidence
from memory.rca_judge import RCAJudge
from operational_ontology import committed_tool_observations, observation_id, observation_entity
from skill_runtime.service_evidence import observed_entity_catalog, bind_root_entity, projection_request
from skill_runtime.kubectl_safety import readonly_kubectl_args
from skill_runtime.ontology_history import collection_time

MAX_COMMANDS = 128
MAX_REVIEW_BYTES = 12000
MAX_PROMPT_BYTES = 32000


def review_skill_diagnosis(*, root_cause, reasoning, confidence, root_entity, commands,
                          cluster_id, namespace, service_scope, hypotheses, call_model):
    """A second bounded model call reviews the same immutable evidence snapshot.

    Identity/citation validation is deterministic. Semantic entailment remains a
    model judgment; a separate call may use the same model. Neither is execution
    permission, a human review, or a calibrated probability of correctness.
    """
    started = time.monotonic()
    stamp = datetime.now(timezone.utc).isoformat()
    blockers, facts, catalog = [], [], []
    if service_scope is not None and not isinstance(service_scope, dict):
        blockers.append('review_input_invalid')
        service_scope = {'status': 'invalid'}
    if not isinstance(hypotheses, list) or len(hypotheses) > 6 or any(not isinstance(h, dict) for h in hypotheses):
        blockers.append('review_input_invalid')
        hypotheses = []
    if not isinstance(commands, list) or len(commands) > MAX_COMMANDS:
        blockers.append('review_input_exceeds_budget')
        commands = []
    # Inspect every bounded named read. The proposer catalog has a display cap;
    # silently reusing its prefix could hide a later conflicting incarnation.
    identities = [{**row, 'source_command_index': index} for index, command in enumerate(commands)
        for row in observed_entity_catalog([command], cluster_id, namespace)]
    bound, _ = bind_root_entity(root_entity, identities, service_scope)
    if bound is None:
        blockers.append('root_identity_unresolved')
    if service_scope is not None and service_scope.get('status') != 'collected':
        blockers.append('service_scope_unresolved')
    if not isinstance(root_cause, str) or not root_cause.strip():
        blockers.append('review_conclusion_missing')
    if not isinstance(reasoning, str) or len(reasoning) > 3500 or len(str(root_cause)) > 500:
        blockers.append('review_input_exceeds_budget')
    logical = lambda row: tuple(row.get(key) for key in ('cluster_id','namespace','kind','name'))
    relevant = {logical(row) for row in identities}
    seen, states, aliases, versions, previous = set(), {}, [], {}, {}
    invalid_times, unrelated = 0, 0
    def value_at(row, path):
        for key in path.split('.'):
            if not isinstance(row, dict) or key not in row: return None
            row = row[key]
        return row
    for command in commands:
        if not isinstance(command, dict):
            blockers.append('review_input_invalid'); continue
        if command.get('executed') is not True or command.get('success') is not True or command.get('outcome_unknown'):
            blockers.append('review_tool_execution_incomplete'); continue
        if command.get('cluster_id') != cluster_id or command.get('namespace') != namespace:
            blockers.append('review_input_scope_conflict'); continue
        try:
            observed = committed_tool_observations(command)
        except (ValueError, TypeError, KeyError):
            blockers.append('review_tool_output_invalid'); continue
        if not observed:
            data = str(command.get('data') or '').strip()
            try: parsed = json.loads(data)
            except ValueError: parsed = None
            try:
                args = readonly_kubectl_args(command.get('command') or command.get('args', {}).get('command') or '', namespace)
                get = args[0] == 'get'
                projected = projection_request(command.get('command') or command.get('args', {}).get('command') or '', namespace)
            except (ValueError, TypeError, IndexError):
                args, get, projected = [], False, None
            empty_inventory = (get and isinstance(parsed, dict) and parsed.get('items') == [] and
                (parsed.get('projection') == 'service-evidence/v1' and projected is not None
                 or parsed.get('kind') == 'EventList' and args[1] == 'events'))
            no_resources = (get and len(args)>2 and args[1] in ('pods','deployments','replicasets','services','events','endpoints')
                and args[2].startswith('-') and not any(a in ('-o','--output') or a.startswith('--output=') for a in args)
                and re.fullmatch(r'(?:No resources found\.?|No resources found in '+re.escape(namespace)+r' namespace\.)',data) is not None)
            if data and not no_resources and not empty_inventory:
                blockers.append('unreviewed_tool_output')
        for item in observed:
            entity = observation_entity(item)
            if not entity:
                blockers.append('review_observation_identity_unresolved'); continue
            if entity.get('cluster_id') != cluster_id or entity.get('namespace') != namespace:
                blockers.append('review_observation_scope_conflict')
                continue
            key = logical(entity)
            if key not in relevant:
                unrelated += 1; continue
            raw_ref = item['raw_ref']
            if raw_ref.get('topology_fields_complete') is False:
                blockers.append('review_projection_incomplete'); continue
            version = raw_ref.get('metadata', {}).get('resourceVersion')
            if not entity.get('uid') or not isinstance(version, str) or not version:
                blockers.append('review_resource_identity_incomplete')
            incarnations = versions.setdefault(key, set())
            incarnations.add((entity.get('uid'), version))
            if len(incarnations) != 1:
                blockers.append('conflicting_resource_observations')
            # Compare common observed state fields, never infer missing fields
            # from a narrower projection or sort opaque versions as a clock.
            for earlier in previous.get(key, []):
                paths = ('spec.replicas','spec.selector','spec.template.metadata.labels','status.readyReplicas',
                         'status.availableReplicas','status.phase','spec.nodeName','subsets')
                if any(value_at(earlier,p) is not None and value_at(raw_ref,p) is not None
                       and value_at(earlier,p) != value_at(raw_ref,p) for p in paths):
                    blockers.append('conflicting_resource_state')
            previous.setdefault(key, []).append(raw_ref)
            if collection_time(item.get('timestamp')) is None:
                blockers.append('review_observation_time_missing')
                invalid_times += 1
                continue  # Never copy invalid NaN/Infinity times into a refusal.
            identity = observation_id(item)
            if identity in seen:
                continue
            seen.add(identity)
            # Identical state rereads are not independent evidence sources.
            # Keep every original ID/time as an alias, retaining UID, opaque
            # resourceVersion and all state fields in the equality check.
            raw = {key: value for key, value in item['raw_ref'].items() if key != 'observed_at'}
            try:
                state = json.dumps({'raw': raw, 'source': item.get('source'), 'signal_type': item.get('signal_type')},
                                   ensure_ascii=False, sort_keys=True, allow_nan=False)
            except (ValueError, TypeError):
                blockers.append('review_input_invalid')
                continue
            primary = states.get(state)
            if primary is None:
                states[state] = identity
                facts.append(item)
            else:
                aliases.append({'id': identity, 'same_state_as': primary, 'collected_at': item['timestamp']})
            catalog.append({'id': identity, 'agent': 'skill_agent', 'entity': entity,
                'source': item.get('source'), 'observation': item.get('observation') if primary is None
                    else 'Same projected state as '+primary,
                'signal_type': item.get('signal_type'), 'collected_at': item['timestamp'],
                **({'same_state_as': primary} if primary is not None else {})})
    if not facts:
        blockers.append('no_committed_review_observations')
    review_scope = ({key: service_scope.get(key) for key in ('status', 'candidate_workloads',
        'selected_pod_count', 'gaps', 'relation', 'is_causal_proof', 'snapshot_consistency')}
        if isinstance(service_scope, dict) else None)
    review_reasoning = ('Recorded root identity and non-causal collection scope:\n'+json.dumps(
        {'root_entity': bound, 'service_scope': review_scope, 'same_state_rereads': aliases}, ensure_ascii=False, default=str)
        +'\n'+str(reasoning))
    if len(review_reasoning) > 4000 or any(len(str(fact.get('observation', ''))) > 3000 for fact in facts):
        blockers.append('review_input_exceeds_budget')
    snapshot = {'root_cause': root_cause, 'reasoning': review_reasoning, 'root_entity': bound,
                'confidence': finite_confidence(confidence),
                'observations': facts, 'observation_aliases': aliases,
                'service_scope': review_scope, 'hypotheses': hypotheses}
    input_bytes = None
    try:
        encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, allow_nan=False)
        input_bytes = len(encoded.encode())
        if input_bytes > MAX_REVIEW_BYTES:
            blockers.append('review_input_exceeds_budget')
        digest = hashlib.sha256(encoded.encode()).hexdigest()
    except (ValueError, TypeError):
        digest = None
        blockers.append('review_input_invalid')

    called = False
    class Reviewer:
        def json_chat(self, messages, **kwargs):
            nonlocal called
            if len(json.dumps(messages, ensure_ascii=False, allow_nan=False).encode()) > MAX_PROMPT_BYTES:
                blockers.append('review_prompt_exceeds_budget')
                return None
            called = True
            answer = call_model(messages)
            if not isinstance(answer, dict):
                return None
            # Model-added authorization or review status fields have no host
            # authority and do not enter the persisted verdict envelope.
            allowed = {'judge_level', 'coherence_score', 'evidence_score', 'specificity_score',
                'actionability_score', 'supported_evidence_ids', 'unsupported_claims',
                'contradictions', 'feedback', 'hypothesis_reviews'}
            answer = {k: v for k, v in answer.items() if k in allowed}
            if len(json.dumps(answer, ensure_ascii=False, allow_nan=False).encode()) > MAX_REVIEW_BYTES:
                return None
            return answer

    context = hypothesis_review_context(hypotheses, catalog) if hypotheses else None
    # A failed input check cannot be repaired by agreement from a reviewer that
    # did not receive a complete, scope-matched root and evidence snapshot.
    judge = RCAJudge(Reviewer() if not blockers else None).judge(
        reasoning=review_reasoning if not blockers else '',
        root_cause=str(root_cause), confidence=finite_confidence(confidence),
        evidence={'skill_agent': {'evidence_items': facts}} if not blockers else {},
        hypothesis_context=context if not blockers else None)
    if not blockers and judge['evidence_count'] != len(facts):
        blockers.append('review_observations_omitted_by_budget')
    llm_review = judge.get('llm_based') or {}
    if called and (not isinstance(llm_review.get('feedback'), str) or not llm_review['feedback'].strip()):
        blockers.append('review_feedback_missing')
    cited = llm_review.get('supported_evidence_ids') or []
    if bound is not None and called and not any(fact.get('id') in cited and
            all((observation_entity(fact) or {}).get(key) == bound.get(key)
                for key in ('cluster_id', 'namespace', 'kind', 'name', 'uid'))
            for fact in judge['review_evidence']):
        blockers.append('root_observation_not_cited')
    if hypotheses and not (judge.get('hypothesis_review') or {}).get('all_candidates_reviewed'):
        blockers.append('candidate_review_incomplete')
    # A different current failure or a missing mention cannot refute an
    # alternative mechanism. Only host-observed, typed falsification tests may
    # qualify a contradiction in this resource-only review path.
    by_hypothesis = {str(h.get('id')): h for h in hypotheses}
    for item in (judge.get('hypothesis_review') or {}).get('items', []):
        hypothesis = by_hypothesis.get(item['id'], {})
        if item.get('status') == 'contradicted' and not falsification_evidence(
                hypothesis, catalog, item.get('contradicting_evidence_ids') or []):
            item.update(status='uncertain', assertion_verified=False)
            item['rationale_limits'].append('direct_falsification_not_observed')
            item['missing_evidence'].append('需要同对象和事故时间窗的直接反证，不能以当前状态或缺少信号排除机制。')
            if item.get('assertion_requires_review'):
                blockers.append('asserted_hypothesis_not_independently_verified')
    hypothesis_review = judge.get('hypothesis_review')
    if isinstance(hypothesis_review, dict) and any(i.get('assertion_requires_review') and not i.get('assertion_verified')
            for i in hypothesis_review.get('items', [])):
        hypothesis_review['blockers'] = list(dict.fromkeys(hypothesis_review.get('blockers', [])+
            ['asserted_hypothesis_not_independently_verified']))
    blockers = list(dict.fromkeys(blockers + judge.get('review_blockers', [])))
    if blockers:
        judge.update(needs_review=True, combined_score=0.0, judge_level=3, review_blockers=blockers)
    judge['skill_review'] = {'schema': 'skill-independent-review/v1', 'input_sha256': digest,
        'input_bytes': input_bytes, 'source_observation_count': len(seen),
        'review_state_count': len(facts), 'observation_aliases': aliases,
        'unrelated_resource_observations': unrelated, 'invalid_observation_times': invalid_times,
        'started_at': stamp, 'finished_at': datetime.now(timezone.utc).isoformat(),
        'duration_s': round(time.monotonic()-started, 3),
        'recording_status': ('skipped_input_incomplete' if not called else
                             'failed_or_invalid' if llm_review.get('error') else 'recorded'),
        'independent_role_call': called, 'same_model_possible': True, 'human_review': False,
        'execution_authorized': False, 'entailment_is_model_judgment': True}
    return judge
