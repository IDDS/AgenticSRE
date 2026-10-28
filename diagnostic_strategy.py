"""Bounded evidence-driven investigation control; never an execution authority.

Novelty counts semantic observation differences, not information-theoretic gain,
independent samples, causal confirmation, or measured accuracy improvement.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections import Counter

from operational_ontology import observation_id, observation_entity, committed_tool_observations, result_envelopes


AGENTS = ("event_agent", "metric_agent", "log_agent", "trace_agent", "llm_inference_agent", "configuration_agent")
VOLATILE = {"observed_at", "collected_at", "sampled_at", "timestamp", "resource_version", "resourceVersion",
            "collection_attempt", "iteration"}


def _dict(value):
    return value if isinstance(value, dict) else {}


def _rows(value):
    return value if isinstance(value, list) else []


def _collection_failed(value):
    payload=_dict(value)
    return bool(payload.get("error") or payload.get("collection_errors") or payload.get("outcome_unknown")
                or payload.get("success") is False)


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _semantic(value):
    if isinstance(value, dict):
        return {k:_semantic(v) for k,v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [_semantic(v) for v in value]
    return value


def semantic_fingerprint(item):
    fields = {k:item.get(k) for k in ("source", "source_agent", "signal_type", "observation", "raw_ref")}
    return hashlib.sha256(_json(_semantic(fields)).encode()).hexdigest()


def evidence_catalog(evidence, history, *, limit=24):
    """Use only currently scoped facts that can resolve to recorded observations.

    Fair round-robin selection prevents dense metric series crowding out logs.
    No summary text is promoted to a fact, and failed/partial collectors cannot
    provide positive evidence for stopping early.
    """
    def signature(item):
        return _json({k:item.get(k) for k in ("signal_type", "source", "observation", "timestamp", "raw_ref")})
    index = {(h.get("source_agent"),signature(h)):h for h in _rows(history) if isinstance(h,dict)}
    groups = []
    for agent,payload in _dict(evidence).items():
        if agent == "structured_evidence" or not isinstance(payload,dict) or _collection_failed(payload):
            continue
        group=[]
        for item in _rows(payload.get("evidence_items")):
            if not isinstance(item,dict) or not item.get("source") or not item.get("observation"):
                continue
            saved=index.get((agent,signature(item)))
            if not saved:
                continue
            raw=_dict(saved.get("raw_ref"))
            if raw.get("previous") or raw.get("stale") or saved.get("signal_type")=="container_previous_termination":
                continue
            group.append({"id":observation_id(saved),"agent":agent,"signal_type":saved.get("signal_type"),
                "observation":str(saved["observation"])[:1000],"excerpted":len(str(saved["observation"]))>1000,
                "entity":observation_entity(saved),"source":saved.get("source"),
                "semantic_fingerprint":semantic_fingerprint(saved)})
            projection=_dict(raw.get('spec_checks'))
            if (agent in ('event_agent','event_agent_closure') and saved.get('signal_type')=='pod_spec_checks'
                    and saved.get('source')=='kubectl get pods -o json'
                    and projection.get('schema')=='pod-spec-checks/v1'
                    and raw.get('pod_uid') and raw.get('resource_version')):
                group[-1]['verification_checks']={key:value for key,value in _dict(projection.get('checks')).items()
                    if key in ('zero_cpu_memory_requests','no_pvc_or_ephemeral_volume') and (type(value) is bool or value is None)}
        if group:
            groups.append(group)
    result, seen=[],set()
    while groups and len(result)<limit:
        remaining=[]
        for group in groups:
            row=group.pop(0)
            if row["id"] not in seen:
                seen.add(row["id"]); result.append(row)
            if group:
                remaining.append(group)
            if len(result)>=limit:
                break
        groups=remaining
    return result


def finite_confidence(value, default=0.5):
    try:
        return min(1.0,max(0.0,float(value))) if not isinstance(value,bool) and math.isfinite(float(value)) else default
    except (ValueError,TypeError,OverflowError):
        return default


def tool_evidence_catalog(commands, *, limit=12):
    rows=[]
    for command in commands:
        for item in committed_tool_observations(command):
            rows.append({"id":observation_id(item),"agent":"skill_agent","signal_type":item.get("signal_type"),
                "observation":str(item.get("observation") or "")[:1000],
                "excerpted":True,"entity":observation_entity(item),"source":item.get("source"),
                "semantic_fingerprint":semantic_fingerprint(item)})
            if len(rows)>=limit:
                return rows
    return rows


def strategy_view(run):
    layers=result_envelopes(run)
    recorded=next((r["diagnostic_strategy"] for r in layers if isinstance(r.get("diagnostic_strategy"),dict) and r["diagnostic_strategy"]),None)
    if recorded is None:
        events=[e for e in _rows(run.get("events")) if isinstance(e,dict)]
        rounds=[e for e in events if e.get("event")=="diagnostic_strategy"]
        generation=next((e for e in events if e.get("event")=="hypothesis_generation_record"),None)
        recorded={"generation":generation,"rounds":rounds,"stop_reason":
                  rounds[-1]["decision"]["reason"] if rounds else "record_not_available"}
    if not recorded.get("generation"):
        recorded={**recorded,"generation":next((e for e in _rows(run.get("events")) if isinstance(e,dict)
            and e.get("event")=="hypothesis_generation_record"),None)}
    if "independent_reviews" not in recorded:
        recorded={**recorded,"independent_reviews":[e for e in _rows(run.get("events"))
            if isinstance(e,dict) and e.get("event")=="hypothesis_independent_review"]}
    result={"run_id":run.get("id"),"status":run.get("status"),"recording_status":
        "recorded" if recorded.get("generation") or recorded.get("rounds") or recorded.get("independent_reviews") else "legacy_or_not_yet_recorded",
        **copy.deepcopy(recorded),"semantics":{"historical_read_starts_new_diagnosis":False,
            "strategy_decision_authorizes_execution":False,"accuracy_improvement_measured":False}}
    result["revision"]="strategy-"+hashlib.sha256(_json(result).encode()).hexdigest()[:24]
    return result


def citation_update(update,catalog):
    """Validate references, never claim they logically entail the hypothesis."""
    known={c["id"] for c in catalog}
    support=_rows(update.get("supporting_evidence_ids"))[:8]
    oppose=_rows(update.get("contradicting_evidence_ids"))[:8]
    invalid=[x for x in support+oppose if not isinstance(x,str) or x not in known]
    support=list(dict.fromkeys(x for x in support if isinstance(x,str) and x in known))
    oppose=list(dict.fromkeys(x for x in oppose if isinstance(x,str) and x in known))
    errors=[]
    if invalid:
        errors.append("unresolved_evidence_reference")
    if set(support)&set(oppose):
        errors.append("same_observation_both_supports_and_contradicts")
    if not support and not oppose:
        errors.append("no_observed_evidence_reference")
    return support,oppose,errors


def hypothesis_review_context(hypotheses, catalog):
    """A bounded snapshot for the existing independent reviewer, not a verdict."""
    return {"hypotheses":copy.deepcopy(hypotheses[:6]),"observations":copy.deepcopy(catalog),
            "semantics":{"proposer_status_is_verified":False,"citation_resolution_is_entailment":False}}


def falsification_evidence(hypothesis,catalog,refs):
    """Resolve only host-assigned narrow tests against one observed Pod UID."""
    test=hypothesis.get('verification_test')
    if test not in ('zero_cpu_memory_requests','no_pvc_or_ephemeral_volume'):
        return []
    service=hypothesis.get('service')
    objects={tuple(_dict(c.get('entity')).get(k) for k in ('namespace','name','uid'))
        for c in catalog if _dict(c.get('entity')).get('kind')=='Pod'
        and _dict(c.get('entity')).get('uid') and (not service or _dict(c.get('entity')).get('name')==service)}
    if len(objects)!=1:
        return []
    target=next(iter(objects))
    return [c['id'] for c in catalog if c.get('id') in refs
        and tuple(_dict(c.get('entity')).get(k) for k in ('namespace','name','uid'))==target
        and _dict(c.get('verification_checks')).get(test) is True]


def validate_hypothesis_review(context, response):
    """Validate reviewer judgments separately from proposer states.

    The host checks shape, identity and citations; logical entailment remains a
    fallible model judgment. Missing reviews never silently certify assertions.
    """
    context=_dict(context)
    hypotheses=_rows(context.get("hypotheses"))
    catalog=_rows(context.get("observations"))
    rows=_rows(_dict(response).get("hypothesis_reviews"))
    expected={str(h.get("id")):h for h in hypotheses}
    counts=Counter(str(r.get("id")) for r in rows if isinstance(r,dict))
    updates={str(r.get("id")):r for r in rows if isinstance(r,dict)}
    global_errors=[]
    if any(not isinstance(r,dict) for r in rows):
        global_errors.append("malformed_review_entry")
    if set(updates)-set(expected):
        global_errors.append("unknown_hypothesis_review")
    if len(expected)!=len(hypotheses):
        global_errors.append("non_unique_candidate_identity")
    items=[]
    for hid,hypothesis in expected.items():
        row=updates.get(hid,{})
        errors=list(global_errors)
        if not row:
            errors.append("hypothesis_review_missing")
        if counts[hid]>1:
            errors.append("duplicate_hypothesis_review")
        status=row.get("status")
        if status not in ("supported","contradicted","uncertain"):
            errors.append("invalid_hypothesis_review_status")
        for field in ("supporting_evidence_ids","contradicting_evidence_ids"):
            if not isinstance(row.get(field),list) or len(row[field])>8:
                errors.append("invalid_review_reference_array")
        support,oppose,reference_errors=citation_update(row,catalog)
        if status=="uncertain":
            reference_errors=[e for e in reference_errors if e!="no_observed_evidence_reference"]
        errors.extend(reference_errors)
        if status=="supported" and (not support or oppose):
            errors.append("support_requires_uncontested_observation")
        if status=="contradicted" and (not oppose or support):
            errors.append("contradiction_requires_direct_counterevidence")
        if not isinstance(row.get("reason"),str) or not row.get("reason", "").strip():
            errors.append("review_reason_missing")
        # Reviewer claims cannot repair malformed proposer updates in place.
        pending_rejection=(hypothesis.get('status')=='active' and hypothesis.get('proposed_status')=='rejected'
                           and 'unverified_rejection' in _rows(hypothesis.get('validation_errors')))
        asserted=hypothesis.get("status") in ("confirmed","rejected") or pending_rejection
        if asserted and hypothesis.get("validation_errors"):
            errors.append("proposer_assertion_has_validation_gaps")
        errors=list(dict.fromkeys(errors))
        validated=not errors
        # A known failure observed in real runs: reviewers call a competing
        # scheduler reason an "indirect refutation" of unobserved PVC state.
        # Conservatively abstain on explicit absence/indirect rationales. This
        # narrow guard is not a general semantic entailment classifier.
        rationale_limits=[]
        host_test_evidence=falsification_evidence(hypothesis,catalog,[c['id'] for c in catalog])
        if validated and status=='supported' and host_test_evidence:
            rationale_limits.append('typed_test_contradicts_support')
        test_evidence=falsification_evidence(hypothesis,catalog,oppose) if status=='contradicted' else []
        if validated and status=='contradicted' and hypothesis.get('verification_test') and not test_evidence:
            rationale_limits.append('falsification_test_not_observed')
        if validated and status=="contradicted" and re.search(
            r"未提及|没有提及|未见|未发现|未观测|无[^。；\n]{0,35}信息|间接|不支持|"
            r"没有[^。；\n]{0,35}证据|缺少[^。；\n]{0,35}证据|"
            r"\b(?:absence|indirect|lack|not mentioned|no mention|not observed|no evidence|does not support)\b",
            row.get("reason", ""),re.IGNORECASE) and not test_evidence:
            rationale_limits.append("absence_or_indirect_rationale_is_not_refutation")
        qualified_status="uncertain" if rationale_limits else status
        required="supported" if hypothesis.get("status")=="confirmed" and not pending_rejection else "contradicted"
        assertion_verified=asserted and validated and qualified_status==required
        items.append({"id":hid,"description":hypothesis.get("description", ""),
            "proposer_status":hypothesis.get("status","active"),"proposed_status":hypothesis.get("proposed_status", ""),
            "proposer_confidence":hypothesis.get("confidence"),
            "status":qualified_status if validated else "invalid_or_missing",
            "reviewer_status":status if status in ("supported","contradicted","uncertain") else "",
            "rationale_limits":rationale_limits,
            "verification_test":hypothesis.get('verification_test',''),"verified_test_evidence_ids":test_evidence,
            "host_test_evidence_ids":host_test_evidence,
            "supporting_evidence_ids":support,"contradicting_evidence_ids":oppose,
            "reason":str(row.get("reason") or "")[:600],
            "missing_evidence":[q[:300] for q in _rows(row.get("missing_evidence")) if isinstance(q,str)][:6],
            "next_agents":[a for a in _rows(row.get("next_agents")) if isinstance(a,str) and a in AGENTS][:3],
            "validation_errors":errors,"assertion_requires_review":asserted,"assertion_verified":assertion_verified})
    blocked=any(item["assertion_requires_review"] and not item["assertion_verified"] for item in items)
    return {"recording_status":"recorded" if rows else "not_returned", "items":items,
        "candidate_count":len(hypotheses),"validated_count":sum(not item["validation_errors"] for item in items),
        "blockers":["asserted_hypothesis_not_independently_verified"] if blocked else [],
        "all_candidates_reviewed":bool(items) and all(not item["validation_errors"] for item in items),
        "semantics":{"same_model_may_fill_separate_roles":True,"human_review":False,
            "entailment_is_model_judgment":True,"execution_authorized":False}}


class InvestigationController:
    def __init__(self, agents, *, max_iterations, confidence_threshold):
        self.agents=list(dict.fromkeys(a for a in agents if a in AGENTS))
        self.max_iterations=max(1,int(max_iterations))
        self.threshold=finite_confidence(confidence_threshold,0.85)
        self.seen=set()
        self.attempts=Counter()
        self.source_states={a:{"status":"untried","attempts":0,"last_iteration":None,
            "last_observation_ids":[],"retry_eligible":False} for a in self.agents}
        self.rounds=[]
        self.generation=None

    def record_generation(self,hypotheses, *, mode="multi_agent"):
        self.generation={"event":"hypothesis_generation_record","mode":mode,
            "items":copy.deepcopy(hypotheses),"semantics":{"candidates_are_observed_causes":False,
                "confidence_is_calibrated_probability":False,"historical_context_is_current_evidence":False}}
        return copy.deepcopy(self.generation)

    def observe(self,catalog):
        novel,repeated=[],[]
        for row in catalog:
            key=row["semantic_fingerprint"]
            (repeated if key in self.seen else novel).append(row["id"])
            self.seen.add(key)
        return {"novel_observation_ids":novel,"repeated_observation_ids":repeated,
                "novelty_semantics":"semantic_content_excluding_collection_time_and_resource_version"}

    def decide(self, *, iteration, before, after, catalog, novelty, selected_agents, results, plan, budget_remaining=None, model_unavailable=False):
        # Only actual attempts update a source. A different collector's success
        # cannot erase an earlier failure, nor can an unselected payload invent one.
        selected=list(dict.fromkeys(a for a in selected_agents
            if isinstance(a,str) and a in (*AGENTS,'skill_agent')))
        failures=[]
        for name in selected:
            self.attempts[name]+=1
            result=_dict(results).get(name)
            failed=not isinstance(result,dict) or _collection_failed(result)
            refs=[c['id'] for c in catalog if c.get('agent')==name] if not failed else []
            status='failed' if failed else 'observed' if refs else 'no_citable_observations'
            self.source_states[name]={"status":status,"attempts":self.attempts[name],
                "last_iteration":iteration,"last_observation_ids":refs[:8],
                "retry_eligible":name in self.agents and failed and self.attempts[name]<2}
            if failed:
                failures.append(name)
        active=[h for h in after if str(h.get("status","active")).lower()!="rejected"]
        active.sort(key=lambda h:finite_confidence(h.get("confidence"),0),reverse=True)
        top=active[0] if active else {}
        support=_rows(top.get("supporting_evidence_ids"))
        oppose=_rows(top.get("contradicting_evidence_ids"))
        errors=_rows(top.get("validation_errors"))
        known={c["id"] for c in catalog}
        grounded=bool(support) and set(support)<=known and not oppose and not errors
        # A proposer score is a heuristic, not a reason to ignore its own
        # evidence gaps or a similarly supported alternative mechanism.
        questions=[q.strip()[:300] for q in _rows(top.get('unresolved_questions'))
                   if isinstance(q,str) and q.strip()][:6]
        margin=0.10
        alternatives=[]
        for hypothesis in active[1:]:
            refs=_rows(hypothesis.get('supporting_evidence_ids'))
            if (hypothesis.get('id')!=top.get('id') and refs and set(refs)<=known
                    and not _rows(hypothesis.get('contradicting_evidence_ids'))
                    and not _rows(hypothesis.get('validation_errors'))
                    and finite_confidence(top.get('confidence'),0)-finite_confidence(hypothesis.get('confidence'),0)<=margin+1e-9):
                alternatives.append(hypothesis)
        # A failed exclusion is an outstanding assertion even when its model
        # score is low or it has no supporting citations. Do not let ranking
        # hide it from the next investigation round.
        pending_rejections=[h for h in active if h.get('proposed_status')=='rejected'
                            and 'unverified_rejection' in _rows(h.get('validation_errors'))]
        requested_failed=[a for a in _rows(top.get('next_agents')) if a in self.agents
                          and self.source_states[a]['status']=='failed']
        requested_retries=[a for a in requested_failed if self.source_states[a]['retry_eligible']]
        gaps=[]
        if questions:
            gaps.append('leading_hypothesis_open_questions')
        if alternatives:
            gaps.append('close_supported_alternatives')
        if pending_rejections:
            gaps.append('unverified_candidate_rejections')
        if requested_failed:
            gaps.append('requested_source_retry_pending')
        score_ready=finite_confidence(top.get('confidence'),0)>=self.threshold and top.get('status')=='confirmed'
        readiness={"schema":"investigation-readiness/v1","leading_hypothesis_id":top.get('id'),
            "citation_grounded":grounded,"confirmed_above_threshold":score_ready,
            "ready_for_independent_review":grounded and score_ready and not gaps,
            "blockers":(['no_active_hypothesis'] if not top else
                ([] if grounded else ['leading_hypothesis_not_grounded'])+
                ([] if score_ready else ['leading_hypothesis_not_confirmed_above_threshold']))+gaps,
            "open_questions":questions,"close_alternative_ids":[h.get('id') for h in alternatives][:5],
            "unverified_rejection_ids":[h.get('id') for h in pending_rejections][:5],
            "requested_source_retries":list(dict.fromkeys(requested_retries)),
            "requested_failed_sources":list(dict.fromkeys(requested_failed)),
            "alternative_score_margin":margin,"score_comparison_is_heuristic":True}
        focused=[top,*pending_rejections,*alternatives] if pending_rejections or alternatives else active[:2]
        hints=list(dict.fromkeys(a for h in focused for a in _rows(h.get("next_agents")) if a in self.agents))
        untried=[a for a in self.agents if not self.attempts[a]]
        retry=[a for a in self.agents if self.source_states[a]['retry_eligible']]
        candidates=list(dict.fromkeys([a for a in hints if a in untried or a in retry]+untried+retry))
        next_agents=candidates[:2]
        next_reasons={a:('hypothesis_requested_failed_source_retry' if a in hints and a in retry
            else 'hypothesis_requested_untried_source' if a in hints
            else 'bounded_failed_source_retry' if a in retry else 'untried_source') for a in next_agents}
        enough=readiness['ready_for_independent_review']
        if model_unavailable:
            action,reason="stop","model_service_unavailable"
        elif budget_remaining is not None and budget_remaining<=0:
            action,reason="stop","model_budget_exhausted"
        elif enough:
            action,reason="stop","cited_hypothesis_ready_for_independent_review"
        elif iteration>=self.max_iterations:
            action,reason="stop","iteration_limit_reached"
        elif gaps and not next_agents:
            action,reason="stop","unresolved_gaps_no_eligible_sources"
        elif not novelty["novel_observation_ids"] and not next_agents:
            action,reason="stop","no_new_information_or_untried_sources"
        else:
            action,reason="continue",('resolve_investigation_gaps' if gaps else
                "switch_information_source" if next_agents else "replan_with_evidence_gaps")
        record={"event":"diagnostic_strategy","iteration":iteration,"before":copy.deepcopy(before),"after":copy.deepcopy(after),
            "catalog":copy.deepcopy(catalog),"novelty":novelty,"selected_agents":list(selected_agents),
            "collection_failures":failures,"source_states":copy.deepcopy(self.source_states),
            "readiness":readiness,
            "plan":copy.deepcopy(plan),"top_hypothesis_id":top.get("id"),
            "decision":{"action":action,"reason":reason,"next_agents":next_agents if action=="continue" else [],
                "next_agent_reasons":next_reasons if action=="continue" else {},
                "citation_grounded":grounded,"independent_review_required":True,"execution_authorized":False},
            "semantics":{"citation_resolution_is_entailment":False,"failure_is_counterevidence":False,
                         "novelty_is_information_gain_bits":False,"cross_incident_policy_learned":False}}
        self.rounds.append(record)
        return copy.deepcopy(record)

    def feedback(self):
        if not self.rounds:
            return {}
        last=self.rounds[-1]
        # Keep unresolved failures early in the bounded planner feedback. Full
        # per-source snapshots remain in the ledger; neither is current truth.
        failures=[a for a,s in last['source_states'].items() if s['status']=='failed']
        return copy.deepcopy({"investigation_gaps":{
                'unresolved_collection_failures':failures,
                'open_questions':[q[:160] for q in last['readiness']['open_questions'][:2]],
                **{key:last['readiness'][key] for key in
                    ('leading_hypothesis_id','blockers','close_alternative_ids','unverified_rejection_ids','requested_failed_sources')}},
            "previous_decision":last["decision"],
            "unresolved_collection_failures":failures,
            "last_top_hypothesis_id":last["top_hypothesis_id"],
            "collection_failures":last["collection_failures"],"attempts":dict(self.attempts),
            "unresolved_questions":[q for h in last["after"] for q in _rows(h.get("unresolved_questions"))][:8],
            "source_states":{a:{k:s[k] for k in ('status','attempts','retry_eligible')}
                             for a,s in last['source_states'].items()}})


def apply_investigation_readiness_gate(report, judge, rounds):
    """Unresolved discriminators survive an exhausted loop and a passing score.

    Closure observations alone do not record that these exact questions have
    been resolved. Keep review required until the investigation ledger does.
    """
    readiness=_dict(rounds[-1].get('readiness')) if rounds else {}
    unresolved=[code for code in _rows(readiness.get('blockers')) if code in (
        'leading_hypothesis_open_questions','close_supported_alternatives','requested_source_retry_pending',
        'unverified_candidate_rejections')]
    if not unresolved:
        return report,judge
    report,judge=copy.deepcopy(report),copy.deepcopy(judge)
    report['investigation_gaps']={'schema':'investigation-readiness/v1','unresolved':True,
        'leading_hypothesis_id':readiness.get('leading_hypothesis_id'),'blockers':unresolved,
        'open_questions':copy.deepcopy(readiness.get('open_questions',[])),
        'close_alternative_ids':copy.deepcopy(readiness.get('close_alternative_ids',[])),
        'unverified_rejection_ids':copy.deepcopy(readiness.get('unverified_rejection_ids',[])),
        'requested_failed_sources':copy.deepcopy(readiness.get('requested_failed_sources',[])),
        'resolution_recorded':False,'confidence_cap':.65,
        'confidence_before_cap':finite_confidence(report.get('confidence'),0)}
    report['confidence']=min(report['investigation_gaps']['confidence_before_cap'],.65)
    judge['needs_review']=True
    judge['review_blockers']=list(dict.fromkeys([*_rows(judge.get('review_blockers')),
                                               'unresolved_investigation_gaps']))
    return report,judge
