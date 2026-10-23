"""Lossless alert grouping. Associations are hypotheses, never suppression rules."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import time
import uuid
from datetime import datetime
from pathlib import Path
import fcntl
import os
import tempfile


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def observation_dimensions(labels):
    """Keep fault dimensions; only severity transitions and explicit transient IDs vary."""
    transient = {"severity", "request_id", "trace_id", "span_id", "correlation_id", "duration_ms", "latency_ms"}
    return digest({k: v for k, v in labels.items() if str(k).lower() not in transient})


def normalize_alerts(records, cluster_id="", namespace=""):
    if not isinstance(records, list) or len(records) > 10000:
        raise ValueError("alerts must be a list with at most 10000 records")
    rows = []
    for index, raw in enumerate(records):
        if not isinstance(raw, dict):
            raise ValueError("each alert must be an object")
        labels = raw.get("labels") or {}
        if not isinstance(labels, dict):
            raise ValueError("alert labels must be an object")
        cluster = str(raw.get("cluster_id") or labels.get("cluster_id") or cluster_id)
        ns = str(raw.get("namespace") or labels.get("namespace") or namespace)
        if cluster_id and cluster != cluster_id or namespace and ns != namespace:
            raise ValueError("alert scope differs from requested cluster/namespace")
        stamp = raw.get("timestamp")
        try:
            stamp = float(stamp)
        except (TypeError, ValueError):
            try:
                stamp = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
            except ValueError:
                stamp = None
        if stamp is not None and (not math.isfinite(stamp) or stamp <= 0):
            stamp = None
        kind, entity = "", ""
        # kube-state-metrics includes its own Pod/service alongside the target Job.
        # job is a Prometheus scrape label; job_name is the Kubernetes resource.
        for field in ("job_name", "pod", "deployment", "service", "instance", "node"):
            if raw.get(field) or labels.get(field):
                kind, entity = "job" if field == "job_name" else field, str(raw.get(field) or labels[field])
                break
        if not entity and labels.get("name"):
            kind, entity = str(labels.get("kind") or "object").lower(), str(labels["name"])
        severity = (str(raw.get("severity") or "").strip()
                    or str(labels.get("severity") or "").strip() or "warning").lower()
        rows.append({
            "alert_id": "a-" + digest([raw, index]), "original_alert_id": raw.get("alert_id", ""),
            "name": str(raw.get("name") or raw.get("title") or "unknown"),
            "message": str(raw.get("message") or raw.get("description") or ""),
            "severity": severity,
            "source": str(raw.get("source") or "unknown"), "timestamp": stamp,
            "cluster_id": cluster, "namespace": ns, "entity_kind": kind, "entity": entity,
            "state": str(raw.get("state") or labels.get("alertstate") or "unknown"),
            "labels": copy.deepcopy(labels), "raw": copy.deepcopy(raw),
        })
        from agents.alert_identity import target_identity
        rows[-1].update(target_identity(rows[-1]))
    return rows


def make_group(rows, indices, reason="same_object_type_message_window"):
    indices = sorted(indices, key=lambda i: (rows[i]["timestamp"] or 0, i))
    members = [rows[i] for i in indices]
    from agents.alert_identity import compatible_members, group_identity
    if len(members)>1 and not compatible_members(members):
        raise ValueError('cannot merge conflicting or different object incarnations')
    rank = {"critical": 4, "high": 3, "warning": 2, "info": 1}
    representative = max(members, key=lambda a: (rank.get(a["severity"], 0), a["timestamp"] or 0))
    stamps = [a["timestamp"] for a in members if a["timestamp"] is not None]
    return {
        "group_id": "g-" + digest(sorted(a["alert_id"] for a in members)), **group_identity(members),
        "representative": representative["name"], "representative_alert_id": representative["alert_id"],
        "cluster_id": representative["cluster_id"], "namespace": representative["namespace"],
        "entity": representative["entity"], "entity_kind": representative["entity_kind"],
        "affected_entities": sorted({a["entity_kind"] + "/" + a["entity"] for a in members}),
        "severity": representative["severity"], "alert_count": len(members),
        "first_seen": min(stamps) if stamps else None, "last_seen": max(stamps) if stamps else None,
        "alert_ids": [a["alert_id"] for a in members], "alert_indices": indices,
        "severity_changes": [{"timestamp": a["timestamp"], "severity": a["severity"], "alert_id": a["alert_id"]}
                             for i, a in enumerate(members) if i == 0 or a["severity"] != members[i-1]["severity"]],
        "grouping_reason": reason, "root_cause_status": "not_diagnosed",
    }


def regroup_stats(result):
    rows, groups = result["raw_alerts"], result["groups"]
    indices = [i for g in groups for i in g["alert_indices"]]
    if sorted(indices) != list(range(len(rows))):
        raise ValueError("group membership must cover every original alert exactly once")
    result.update(total_alerts=len(rows), compressed_groups=len(groups),
                  compression_ratio=1-len(groups)/len(rows) if rows else 0,
                  retained_alerts=len(indices), omitted_alerts=0)
    from agents.alert_integrity import partition_receipt
    result['integrity']=partition_receipt(result)
    return result


def compress_alerts(records, *, cluster_id="", namespace="", time_window=300, baseline=None):
    from agents.alert_semantics import symbolic, annotate_groups
    rows = normalize_alerts(records, cluster_id, namespace)
    window = float(time_window)
    if not math.isfinite(window) or not 1 <= window <= 3600:
        raise ValueError("time_window must be between 1 and 3600 seconds")
    buckets = {}
    for i in sorted(range(len(rows)), key=lambda j: (rows[j]["timestamp"] or 0, j)):
        a = rows[i]
        # Missing identity/time cannot justify merging two observations.
        key = (a["cluster_id"], a["namespace"], a["entity_kind"], a["entity"],
               a['entity_uid'],a["name"], a["source"], a["state"], observation_dimensions(a["labels"]), symbolic(a)["key"])
        missing_namespace = a["entity_kind"] in {"pod", "deployment", "service", "job"} and not a["namespace"]
        if not a["cluster_id"] or not a["entity"] or a["timestamp"] is None or missing_namespace or a['identity_status']=='conflicting':
            key += (a["alert_id"],)
        segments = buckets.setdefault(key, [])
        if not segments or (a["timestamp"] or 0)-(rows[segments[-1][0]]["timestamp"] or 0) > window:
            segments.append([])
        segments[-1].append(i)
    result = regroup_stats({"agent": "alert_agent", "schema_version": 2, "time_window": window,
        "raw_alerts": rows, "groups": [make_group(rows, s) for segments in buckets.values() for s in segments],
        "relations": [], "semantic": {"status": "disabled", "model_calls": 0}})
    return annotate_groups(result, baseline)


async def propose_relations(result, llm, timeout_s=60, *, budget=None, checkpoints=None,
                            checkpoint=None, should_continue=None, observer=None):
    from agents.alert_review import ReviewBudget, batches, messages, message_bytes, reference_groups, resume_summary
    budget = budget or ReviewBudget()
    checkpoints = checkpoints or {}
    groups, rows = result["groups"], result["raw_alerts"]
    candidates = []
    if "candidate_pairs" in result:
        by_id = {g["group_id"]: g for g in groups}
        candidates = [(by_id[p["left"]], by_id[p["right"]]) for p in result["candidate_pairs"]]
    else:
        # Bounded locality buckets avoid an all-alert quadratic model comparison.
        buckets = {}
        for g in groups:
            if not g["entity"] or g["first_seen"] is None or not g["cluster_id"]:
                continue
            key = (g["cluster_id"], g["namespace"], g["entity_kind"], g["entity"])
            neighbors = buckets.setdefault(key, [])
            for other in neighbors[-4:]:
                if max(g["last_seen"], other["last_seen"])-min(g["first_seen"], other["first_seen"]) <= result["time_window"]:
                    candidates.append((other, g))
            neighbors.append(g)
    if not candidates:
        result["semantic"] = {"status": "no_candidates", "model_calls": 0, "coverage_complete": True}
        return result
    review_result = dict(result, candidate_pairs=[dict(left=a['group_id'], right=b['group_id']) for a, b in candidates])
    result["semantic"] = {"status": "ok", "model_calls": 0, "candidate_pairs": len(candidates),
        "completed_pairs": 0, "failed_pairs": 0, "deferred_pairs": 0, "batches": [],
        "returned_relations": 0, "rejected_relations": {},
        "trace_context_pairs": 0, "change_context_pairs": 0, "metric_context_pairs": 0,
        "coverage_scope": "retrieved_candidate_pairs",
        "retrieval_truncated": bool(result.get("retrieval", {}).get("truncated") or
                                    result.get("retrieval", {}).get("candidate_pairs_truncated"))}
    seen = {(r["left"], r["right"]) for r in result["relations"]}
    report = result['semantic']
    async def observe(method, *args):
        if observer is None:
            return None
        try:
            return await asyncio.to_thread(getattr(observer, method), *args)
        except Exception:
            # Observation failure must not prevent review or expose exception data.
            report['observation_errors'] = report.get('observation_errors', 0)+1
            return None
    # Successful checkpoints remain usable after a deadline; only new attempts need budget.
    restored = set()
    for offset, prior in checkpoints.items():
        if prior['semantic']['status'] == 'ok':
            restored.add(offset)
            report['completed_pairs'] += prior['count']
            report['batches'].append(dict(offset=offset, pairs=prior['count'], status='ok', reused=True))
            for relation in prior['relations']:
                pair = (relation['left'], relation['right'])
                if pair not in seen:
                    result['relations'].append(relation)
                    seen.add(pair)
    def available_input():
        state = budget.snapshot()
        return min(state['limits']['max_batch_input_bytes'], state['limits']['max_input_bytes']-state['input_bytes'])
    covered = set(restored)
    iterator = iter(batches(review_result, available_input, checkpoints))
    while report['completed_pairs'] < len(candidates):
        if should_continue is not None and not await should_continue():
            break
        if llm is None:
            report['stop_reason'] = 'model_unavailable'
            break
        reason = budget.snapshot()['exhausted_reason']
        if reason:
            report['stop_reason'] = reason
            break
        try:
            offset, batch = next(iterator)
        except StopIteration:
            break
        if offset in restored:
            continue
        request = messages(batch)
        input_bytes = message_bytes(request)
        output_budget = min(12288, max(2048, len(batch) * 512))
        reservation = await asyncio.to_thread(budget.reserve, input_bytes, output_budget)
        if not reservation['allowed']:
            reason = reservation['reason']
            report['stop_reason'] = reason
            if reason in ('batch_input_bytes', 'input_bytes'):
                prior = checkpoints.get(offset)
                status = 'failed' if prior else 'deferred'
                report['batches'].append(dict(offset=offset, pairs=len(batch), status=status,
                    reason=reason, input_bytes=input_bytes))
                if prior:
                    report['failed_pairs'] += len(batch)
                covered.add(offset)
                continue
            break
        output_budget = reservation['output_tokens']
        report['output_token_budget'] = output_budget
        for entry in batch:
            report['trace_context_pairs'] += bool(entry['observed_context'].get('trace_refs') or entry['observed_context'].get('propagation_candidates'))
            report['change_context_pairs'] += bool(entry['observed_context'].get('change_refs'))
            report['metric_context_pairs'] += all(g['status'] in ('ready', 'composite') for g in entry['metric_context']['groups'].values())
        batch_relations = []
        status, error = 'ok', None
        terminal_failure = None
        provider = None
        rejected_before = sum(report['rejected_relations'].values())
        receipt = await observe('begin', offset, batch, request, output_budget)
        try:
            # Prompt/observation work after reservation also consumes wall time.
            timeout = min(timeout_s, reservation['timeout_s'], budget.snapshot()['deadline_at']-time.time())
            if timeout <= 0:
                raise TimeoutError('review deadline elapsed before request')
            options = dict(max_tokens=output_budget, thinking=False)
            # The production client bounds HTTP retries and timeout as well as the await.
            from tools.llm_client import LLMClient
            if isinstance(llm, LLMClient):
                options.update(request_timeout_s=timeout, max_retries=0, with_receipt=True)
            response = await asyncio.wait_for(llm.async_json_chat(request, **options), timeout=timeout)
            if isinstance(llm, LLMClient):
                from tools.llm_receipt import JSONCompletion
                if not isinstance(response, JSONCompletion):
                    raise ValueError('provider receipt missing')
                provider, response = response.provider, response.data
                if provider.get('completion_status') != 'complete':
                    raise ValueError('provider completion is not complete')
            allowed = {(p["left"], p["right"]): reference_groups(p) for p in batch}
            allowed.update({(b, a): refs for (a, b), refs in list(allowed.items())})
            relations = response if isinstance(response, list) else response.get("relations") if isinstance(response, dict) else None
            if not isinstance(relations, list):
                raise ValueError("model response must contain a relations array")
            def reject(reason):
                counts = result["semantic"]["rejected_relations"]
                counts[reason] = counts.get(reason, 0) + 1
            result["semantic"]["returned_relations"] += len(relations)
            for relation in relations:
                if not isinstance(relation, dict):
                    reject("invalid_relation_object")
                    continue
                pair = (relation.get("left"), relation.get("right"))
                if not all(isinstance(key, str) for key in pair):
                    reject("invalid_pair")
                    continue
                refs = relation.get("evidence_alert_ids")
                if pair not in allowed:
                    reject("pair_outside_batch")
                elif pair in seen:
                    reject("duplicate_pair")
                elif (not isinstance(refs, list) or not all(isinstance(r, str) for r in refs)
                      or not set(refs) <= set.union(*allowed[pair])
                      or not all(set(refs) & members for members in allowed[pair])):
                    reject("evidence_mismatch")
                else:
                    seen.add(pair)
                    batch_relations.append({"left": pair[0], "right": pair[1], "status": "unverified",
                        "type": relation.get("type") if relation.get("type") in {"equivalent", "shared_root", "propagates_to", "shared_dependency", "change_trigger", "conflicts"} else "related",
                        "reason": str(relation.get("reason") or "")[:500], "evidence_alert_ids": refs})
        except Exception as exc:
            status, error = 'failed', type(exc).__name__
            report['error'] = error
            http_status=getattr(exc,'status_code',None)
            terminal_codes={401:'provider_authentication_failed',402:'provider_payment_required',
                403:'provider_permission_denied',404:'provider_endpoint_unavailable'}
            if type(http_status) is int and http_status in terminal_codes:
                terminal_failure={'code':terminal_codes[http_status],'status_code':http_status}
                report['provider_failure']=terminal_failure
                report['stop_reason']=terminal_failure['code']
        # Persist received usage even when completion or JSON validation failed.
        # Cancellation/crash before this point leaves a reserved, unknown attempt.
        await asyncio.to_thread(budget.record_provider, reservation['attempt'], provider)
        answer = dict(semantic=dict(status=status, model_calls=1, candidate_pairs=len(batch),
                                   output_token_budget=output_budget, input_bytes=input_bytes, error=error), relations=batch_relations)
        if terminal_failure:
            answer['semantic']['provider_failure']=dict(terminal_failure)
        if provider is not None:
            answer['semantic']['provider'] = provider
        if receipt is not None:
            answer['semantic']['observation_id'] = receipt
        if checkpoint is not None:
            await checkpoint(offset, len(batch), answer)
        if receipt is not None:
            await observe('finish', receipt, answer, sum(report['rejected_relations'].values())-rejected_before)
        result['relations'].extend(batch_relations)
        report['completed_pairs' if status == 'ok' else 'failed_pairs'] += len(batch)
        report['batches'].append(dict(offset=offset, pairs=len(batch), status=status, error=error,
                                     provider_status=provider.get('completion_status') if provider else None,
                                     input_bytes=input_bytes))
        covered.add(offset)
        if terminal_failure:
            # Preserve this failed checkpoint and the remaining pending pairs.
            # Only an explicit later resume may retry after service repair.
            break
    for offset, prior in checkpoints.items():
        if offset not in covered:
            report['failed_pairs'] += prior['count']
            report['batches'].append(dict(offset=offset, pairs=prior['count'], status='failed',
                                         reason=report.get('stop_reason'), error=prior['semantic'].get('error'),
                                         input_bytes=prior['semantic'].get('input_bytes')))
    # Fill gaps without rebuilding all deferred prompts. No candidate is marked unrelated.
    cursor = 0
    ordered = sorted(report['batches'], key=lambda b: b['offset'])
    report['batches'] = []
    for batch in ordered:
        if batch['offset'] > cursor:
            report['batches'].append(dict(offset=cursor, pairs=batch['offset']-cursor,
                                         status='deferred', reason=report.get('stop_reason', 'interrupted')))
        report['batches'].append(batch)
        cursor = batch['offset']+batch['pairs']
    if cursor < len(candidates):
        report['batches'].append(dict(offset=cursor, pairs=len(candidates)-cursor,
                                     status='deferred', reason=report.get('stop_reason', 'interrupted')))
    report['deferred_pairs'] = sum(b['pairs'] for b in report['batches'] if b['status'] == 'deferred')
    report['budget'] = budget.snapshot()
    report.update(resume_summary(report, report['budget']))
    report['model_calls'] = report['budget']['model_calls']
    report["accepted_relations"] = len(result["relations"])
    report["coverage_complete"] = report["completed_pairs"] == len(candidates) and not report["retrieval_truncated"]
    if report['deferred_pairs']:
        report['status'] = 'unavailable' if llm is None and not report['completed_pairs'] else 'partial'
    elif report["failed_pairs"]:
        report["status"] = "partial" if report["completed_pairs"] else "failed"

    return result


class CompressionStore:
    def __init__(self, directory):
        self.directory = Path(directory)

    def _path(self, scan_id):
        if not isinstance(scan_id, str) or not scan_id.startswith("scan-") or len(scan_id) != 37 or any(c not in "0123456789abcdef" for c in scan_id[5:]):
            raise ValueError("invalid scan id")
        return self.directory / (scan_id + ".json")

    def _write(self, path, data):
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=self.directory)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(data, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            if os.path.exists(temp): os.unlink(temp)

    def create(self, result, *, lifecycle=None):
        result = copy.deepcopy(result)
        from agents.alert_integrity import partition_receipt
        result['integrity']=partition_receipt(result)
        result.update(scan_id="scan-"+uuid.uuid4().hex, revision=1, created_at=time.time(), history=[])
        if lifecycle is not None:
            try:
                lifecycle.observe(result)
            except Exception as exc:
                result['lifecycle'] = {'status': 'unavailable', 'error': type(exc).__name__}
        self._write(self._path(result["scan_id"]), result)
        return result

    def get(self, scan_id):
        return json.loads(self._path(scan_id).read_text())

    def edit(self, scan_id, *, revision, action, group_ids, alert_ids=None, reason=""):
        path = self._path(scan_id)
        with open(str(path)+".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = self.get(scan_id)
            if type(revision) is not int or revision != data["revision"]:
                raise ValueError("stale revision; reload the event groups")
            if not isinstance(reason, str) or not reason.strip(): raise ValueError("an edit reason is required")
            if not isinstance(group_ids, list) or not group_ids or len(set(group_ids)) != len(group_ids):
                raise ValueError("select distinct groups")
            selected = [g for g in data["groups"] if g["group_id"] in group_ids]
            if len(selected) != len(group_ids): raise ValueError("unknown group")
            rows = data["raw_alerts"]
            before = copy.deepcopy(data["groups"])
            indices = [i for g in selected for i in g["alert_indices"]]
            if action == "merge" and len(selected) >= 2:
                if len({(rows[i]["cluster_id"], rows[i]["namespace"]) for i in indices}) != 1:
                    raise ValueError("cannot merge different clusters/namespaces")
                stamps = [rows[i]["timestamp"] for i in indices]
                if None in stamps or max(stamps)-min(stamps) > data["time_window"]:
                    raise ValueError("cannot merge outside the time window")
                replacement = [make_group(rows, indices, "manual_merge")]
            elif action == "split" and len(selected) == 1:
                chosen = set(alert_ids or [])
                if not chosen or not chosen < set(selected[0]["alert_ids"]):
                    raise ValueError("split requires a nonempty proper subset of member alert ids")
                replacement = [make_group(rows, [i for i in indices if (rows[i]["alert_id"] in chosen) == flag], "manual_split") for flag in (True, False)]
            else:
                raise ValueError("invalid edit action or group selection")
            data["groups"] = [g for g in data["groups"] if g not in selected] + replacement
            data["history"].append({"revision": data["revision"], "action": action, "reason": str(reason)[:1000], "at": time.time(), "groups": before})
            data["revision"] += 1
            data["relations"] = []  # Prior references no longer describe the edited partition.
            prior_semantic=data.get('semantic',{})
            data['semantic']={'status':'invalidated_by_edit','coverage_complete':False,
                'prior_revision':revision,'prior_status':prior_semantic.get('status'),
                'prior_model_calls':prior_semantic.get('model_calls',0),
                'interpretation':'prior review applies only to the previous partition; no model call made by edit'}
            data["candidate_pairs"] = []
            data["scag"] = {"status": "invalidated_by_edit", "nodes": [], "edges": []}
            data["root_candidates"] = {"status": "invalidated_by_edit", "candidates": [], "causal_confirmed": False}
            data["retrieval"] = {"status": "invalidated_by_edit"}
            data["automatic_baseline"] = {"status": "invalidated_by_edit"}
            data['lifecycle'] = {'status': 'invalidated_by_edit'}
            data['merge_review'] = {'status': 'invalidated_by_edit', 'suggestions': [], 'automatic_merge': False}
            if 'window_evidence' in data:
                data['window_evidence'] = {'status':'invalidated_by_edit','proofs':[]}
            if 'window_compression' in data:
                data['window_compression'] = {'status':'invalidated_by_edit'}
            from agents.alert_semantics import annotate_groups
            annotate_groups(data)
            regroup_stats(data)
            self._write(path, data)
            return data

    def evidence_page(self, scan_id, group_id, revision, *, offset=0, limit=20):
        from agents.alert_evidence_read import evidence_page
        return evidence_page(self.get(scan_id), group_id, revision, offset=offset, limit=limit)

    def dossier(self, scan_id, group_id, revision, *, compact=True, for_diagnosis=False):
        data = self.get(scan_id)
        if type(revision) is not int or data["revision"] != revision: raise ValueError("stale revision")
        group = next((g for g in data["groups"] if g["group_id"] == group_id), None)
        if group is None: raise ValueError("unknown group")
        rows = data["raw_alerts"]
        members = [rows[i] for i in group["alert_indices"]]
        job_names = {str(a["raw"].get("job_name") or a["labels"].get("job_name")) for a in members
                     if a["raw"].get("job_name") or a["labels"].get("job_name")}
        if job_names and (len(job_names) != 1 or group["entity_kind"] != "job" or group["entity"] not in job_names):
            raise ValueError("stored group has ambiguous or outdated Job identity; run a new scan")
        representative = next(a for a in members if a["alert_id"] == group["representative_alert_id"])
        sample_ids = list(dict.fromkeys([members[0]["alert_id"], representative["alert_id"], members[-1]["alert_id"]]))
        # Cross-metric groups can contain distinct alert types. First/last and
        # highest severity may all belong to latency thresholds and omit the
        # error alert entirely, so keep a representative of each source/name.
        by_type = {(a['source'], a['name']): a['alert_id'] for a in members}
        sample_ids = list(dict.fromkeys(sample_ids + list(by_type.values())))
        extrema = group.get("metric_summary", {})
        sample_ids = list(dict.fromkeys(sample_ids + [extrema[k] for k in ("min_alert_id", "max_alert_id") if k in extrema]))
        from agents.alert_numeric import summarize
        log_numeric, numeric_by_alert = summarize(members)
        sample_ids = list(dict.fromkeys(sample_ids + [stats[k] for stats in log_numeric.values()
                                                     for k in ("min_alert_id", "max_alert_id")]))
        samples = [{k: a[k] for k in ("alert_id", "name", "message", "severity", "timestamp", "source", "labels")}
                   for key in sample_ids for a in members if a["alert_id"] == key]
        for sample in samples:
            from agents.alert_identity import target_identity
            member=next(a for a in members if a['alert_id']==sample['alert_id'])
            sample.update(target_identity(member))
            if sample['alert_id'] in numeric_by_alert:
                sample['log_numeric_observations'] = numeric_by_alert[sample['alert_id']]
            sample["message_truncated"] = len(sample["message"]) > 500
            sample["message"] = sample["message"][:500]
            sample["labels"] = {k: str(v)[:150] for k, v in sample["labels"].items()
                                if k in ("job_name", "pod", "deployment", "service", "instance", "node", "kind", "name", "namespace",
                                         "condition", "container", "reason", "persistentvolumeclaim")}
        if data.get("scag", {}).get("status") == "ready":
            from agents.alert_semantics import build_scag
            build_scag(data, data["scag"].get("topology"))
        related = [e for e in data.get("scag", {}).get("edges", []) if group_id in (e["left"], e["right"])]
        dossier = {"source": "alert_evidence_dossier", "scan_id": scan_id, "revision": revision,
            "group_id": group_id, "cluster_id": group["cluster_id"], "namespace": group["namespace"],
            "entity": group["entity"], "entity_kind": group["entity_kind"], "alert_count": group["alert_count"],
            "entity_uid":group.get('entity_uid',''),"identity_status":group.get('identity_status','legacy_not_recorded'),
            "symbolic": group.get("symbolic", {}), "metric_summary": group.get("metric_summary", {}),
            "log_numeric_summary": log_numeric,
            "frequency": group.get("frequency", {}),
            "merge_review": {**{k: v for k, v in data.get('merge_review', {}).items() if k != 'suggestions'},
                "suggestions": [s for s in data.get('merge_review', {}).get('suggestions', []) if group_id in s['group_ids']][:3]},
            "automatic_baseline": {k:v for k,v in data.get("automatic_baseline", {}).items() if k != "scan_ids"},
            "association_candidates": [{k: e.get(k) for k in ("left", "right", "type", "status", "causal_confirmed", "checks", "context")} for e in related[:3]],
            "first_seen": group["first_seen"], "last_seen": group["last_seen"], "alert_samples": samples,
            "affected_entities": group["affected_entities"][:20],
            "affected_entities_total": len(group["affected_entities"]),
            "severity_changes": group["severity_changes"][:20],
            "severity_changes_total": len(group["severity_changes"]),
            "raw_alert_ids": group["alert_ids"][:40], "raw_alert_ids_total": len(group["alert_ids"]),
            "evidence_url": f"/api/alerts/compression/{scan_id}",
            "interpretation": "告警分组及原文是待验证观测，不代表根因已确认；请使用实时只读工具验证。"}
        dossier['semantic_review'] = {k: data.get('semantic', {}).get(k) for k in
            ('status', 'candidate_pairs', 'completed_pairs', 'failed_pairs', 'deferred_pairs', 'coverage_complete')}
        dossier['semantic_review']['review_pairs_url'] = f'/api/alerts/compression/{scan_id}/page?section=review_pairs'
        root_report = data.get('root_candidates', {})
        dossier['root_candidates'] = {k: root_report[k] for k in
            ('status','reason','valid_until','confidence_basis','causal_confirmed','interpretation') if k in root_report}
        dossier['root_candidates']['candidates'] = [c for c in root_report.get('candidates', [])
            if group_id in c['affected_group_ids']]
        if group.get('threshold_merge'):
            dossier['threshold_merge'] = group['threshold_merge']
        if group.get('event_merge'):
            dossier['event_merge'] = {k:v for k,v in group['event_merge'].items() if k != 'original_groups'}
            dossier['event_merge']['original_group_ids'] = [g['group_id'] for g in group['event_merge']['original_groups']]
        if group.get('lifecycle'):
            dossier['lifecycle'] = group['lifecycle'][:3]
            dossier['lifecycle_total'] = group.get('lifecycle_total', len(group['lifecycle']))
        metric = data.get('metric_evidence', {}).get('groups', {}).get(group_id)
        if metric:
            dossier['metric_evidence'] = metric if not compact else {k:v for k,v in metric.items() if k not in ('raw_values','points')}
            if compact and metric.get('measurements'):
                dossier['metric_evidence']['measurements'] = [{k:v for k,v in m.items() if k not in ('raw_values','points')} for m in metric['measurements']]
            if compact:
                # Removing points must not remove their observed extrema.
                from agents.alert_model_evidence import metric_context
                measured = metric_context(data, group)
                if measured.get('status') == 'ready':
                    dossier['metric_evidence']['summary'] = measured['summary']
                elif measured.get('status') == 'composite':
                    by_id = {m['evidence_id']: m['summary'] for m in measured['measurements']}
                    for entry in dossier['metric_evidence']['measurements']:
                        if entry.get('evidence_id') in by_id:
                            entry['summary'] = by_id[entry['evidence_id']]
        if not compact:
            if for_diagnosis:
                from agents.alert_evidence_read import hydrate_dossier
                return hydrate_dossier(dossier, data)
            return dossier
        # Aligned sample receipts are an audit trail, not additional findings.
        # Repeating every receipt in each endpoint's dossier can outweigh the
        # original observations. Keep qualification and scalar statistics in
        # full, with an explicit excerpt and revision-bound retrieval pointer.
        dossier['association_candidates'] = copy.deepcopy(dossier['association_candidates'])
        for association in dossier['association_candidates']:
            stats = association.get('checks', {}).get('statistics', {})
            matches = stats.get('matched_evidence')
            if isinstance(matches, list) and len(matches) > 3:
                stats['matched_evidence'] = [matches[i] for i in (0, len(matches)//2, len(matches)-1)]
                stats['matched_evidence_total'] = len(matches)
                stats['matched_evidence_truncated'] = True
                stats['matched_evidence_ref'] = {
                    'scan_id': scan_id, 'revision': revision, 'evidence_url': dossier['evidence_url'],
                    'path': 'scag.edges[].checks.statistics.matched_evidence',
                    'edge': {key: association[key] for key in ('left', 'right', 'type')}}
        # Raw records and the full derived view remain addressable by scan/group/revision.
        # Preserve observed statistics, transitions, extrema and qualified associations.
        for key in ("metric_summary", "log_numeric_summary", "association_candidates"):
            if not dossier[key]:
                del dossier[key]
        if dossier["frequency"].get("status") == "unknown":
            del dossier["frequency"]
        if not dossier["automatic_baseline"] or dossier["automatic_baseline"].get("status") != "ready":
            del dossier["automatic_baseline"]
        symbol = dossier["symbolic"]
        if symbol.get("kind") != "mixed" and symbol.get("template") == samples[0]["message"]:
            del dossier["symbolic"]
        else:
            dossier["symbolic"] = {k: v for k, v in symbol.items() if k in ("kind", "template")}
        if dossier["affected_entities_total"] == 1:
            del dossier["affected_entities"], dossier["affected_entities_total"]
        if dossier["severity_changes_total"] == 1:
            del dossier["severity_changes"], dossier["severity_changes_total"]
        if set(dossier["raw_alert_ids"]) == set(sample_ids) and dossier["raw_alert_ids_total"] == len(sample_ids):
            del dossier["raw_alert_ids"], dossier["raw_alert_ids_total"]
        for sample in samples:
            if not sample["message_truncated"]:
                del sample["message_truncated"]
            if not sample["labels"]:
                del sample["labels"]
        if for_diagnosis:
            from agents.alert_evidence_read import hydrate_dossier
            return hydrate_dossier(dossier, data)
        return dossier
