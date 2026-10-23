"""Evidence adapters: exact trace resource identity and observed rollout revisions."""
import hashlib
import json
import requests
import ipaddress
import re
from datetime import datetime
from agents.alert_semantics import entity_ref, number


def collect_traces(base_url, result, inventory, fetch_json=None):
    """Query only configured backend and target services, without global discovery."""
    if not base_url and fetch_json is None:
        return {"status": "unconfigured", "edges": []}
    def get(path, params=None):
        if fetch_json is not None:
            return fetch_json(path, params)
        response = requests.get(base_url.rstrip('/')+path, params=params, timeout=5)
        response.raise_for_status()
        return response.json()
    try:
        available = get('/api/services').get("data", [])
        desired = set()
        targets = {(g["namespace"], g["entity"]) for g in result["groups"] if g["entity_kind"] == "pod"}
        desired.update(g["entity"] for g in result["groups"] if g["entity_kind"] == "service")
        for pod in inventory.get("pods", []):
            if (pod.get("namespace"), pod.get("name")) not in targets:
                continue
            for svc in inventory.get("services", []):
                selector = svc.get("selector", {})
                if svc.get("namespace") == pod.get("namespace") and selector and all(pod.get("labels", {}).get(k) == v for k,v in selector.items()):
                    desired.add(svc["name"])
        selected = sorted(desired.intersection(available))[:3]
        payload = {"data": []}
        requested = set()
        invalid_ids = 0
        for row in result.get('raw_alerts', []):
            raw = row.get('raw', {})
            trace_id = raw.get('trace_id', raw.get('traceID')) or row.get('labels', {}).get('trace_id')
            if trace_id:
                if isinstance(trace_id, str) and re.fullmatch(r'(?:[0-9a-fA-F]{16}|[0-9a-fA-F]{32})', trace_id):
                    requested.add(trace_id.lower())
                else:
                    invalid_ids += 1
        queried_ids, missing_ids = [], []
        for trace_id in sorted(requested)[:20]:
            queried_ids.append(trace_id)
            try:
                response = get('/api/traces/'+trace_id)
                exact = [t for t in response.get('data', []) if str(t.get('traceID', '')).lower() == trace_id]
                payload['data'].extend(exact[:1])
                if not exact:
                    missing_ids.append(trace_id)
            except Exception:
                missing_ids.append(trace_id)
        for service in selected:
            response = get('/api/traces', {"service": service, "limit": 5, "lookback": "1h"})
            payload["data"].extend(response.get("data", [])[:5])
        result_data = trace_evidence(payload, result["cluster_id"], inventory, "live_jaeger")
        result_data.update(available_services=len(available), queried_services=selected,
                           queried_trace_ids=queried_ids, missing_trace_ids=missing_ids,
                           invalid_trace_ids=invalid_ids, requested_trace_ids=len(requested),
                           exact_trace_lookup_truncated=len(requested)>len(queried_ids),
                           truncated=result_data['truncated'] or len(requested)>len(queried_ids),
                           status=result_data["status"] if selected or queried_ids else "no_target_services")
        return result_data
    except Exception as exc:
        return {"status": "unavailable", "error": type(exc).__name__, "edges": [], "provenance": "live_jaeger"}


def scoped_trace_routes(result, inventory):
    """Only an unambiguous Jaeger service in the alert namespace is a fallback."""
    namespaces = {g["namespace"] for g in result["groups"] if g["namespace"]}
    candidates = {}
    for service in inventory.get("services", []):
        namespace = service.get("namespace")
        if namespace not in namespaces or service.get("name") != "jaeger" or "16686" not in service.get("ports", []):
            continue
        try:
            address = ipaddress.ip_address(service.get("cluster_ip", ""))
        except ValueError:
            continue
        host = f"[{address}]" if address.version == 6 else str(address)
        candidates.setdefault(namespace, []).append(f"http://{host}:16686")
    return [{"namespace":ns, "url":urls[0]} for ns,urls in sorted(candidates.items()) if len(urls)==1][:2]


def namespace_trace_query(namespace, path, params, shell, prefixes):
    """Fixed Jaeger service proxy with an allowlisted read-only API path."""
    from urllib.parse import quote, urlencode
    import shlex
    if path not in ('/api/services', '/api/traces') and not re.fullmatch(r'/api/traces/(?:[0-9a-fA-F]{16}|[0-9a-fA-F]{32})', path):
        raise ValueError('unsupported trace query')
    candidates = [prefixes[namespace]] if namespace in prefixes else ['', '/jaeger/ui']
    for prefix in candidates:
        target = f"/api/v1/namespaces/{quote(namespace, safe='')}/services/jaeger:16686/proxy{prefix}{path}"
        if params:
            target += '?'+urlencode(params)
        response = shell(f"kubectl get --raw {shlex.quote(target)} --request-timeout=5s", 8)
        if response.get('ok'):
            try:
                data = json.loads(response.get('stdout') or '{}')
            except (ValueError, TypeError):
                continue
            if isinstance(data, dict) and isinstance(data.get('data'), list):
                prefixes[namespace] = prefix
                return data
    raise RuntimeError('namespace Jaeger query proxy unavailable')


async def collect_scoped_traces(base_url, result, inventory, proxy_fetch=None):
    import asyncio
    from tools.llm_client import run_sync_in_daemon_thread
    async def fetch(url, target, namespace=None):
        try:
            callback = (lambda path, params: proxy_fetch(namespace, path, params)) if namespace and proxy_fetch else None
            return await asyncio.wait_for(run_sync_in_daemon_thread(collect_traces, url, target, inventory, callback), 21)
        except Exception as exc:
            return {"status":"unavailable", "error":type(exc).__name__, "edges":[]}
    primary = await fetch(base_url, result)
    if primary["status"] not in {"unavailable", "unconfigured"}:
        return primary
    routes = scoped_trace_routes(result, inventory)
    if not routes:
        return primary
    responses = await asyncio.gather(*(fetch(route["url"], dict(result, groups=[g for g in result["groups"] if g["namespace"]==route["namespace"]]), route["namespace"]) for route in routes))
    edges = [edge for response in responses for edge in response.get("edges", [])]
    internal_edges = [edge for response in responses for edge in response.get("internal_span_edges", [])]
    observed_spans = [span for response in responses for span in response.get('observed_spans', [])]
    return {"status":"mapped" if edges else "no_mapped_calls" if any(r["status"] not in {"unavailable","unconfigured"} for r in responses) else "unavailable",
        "edges":edges[:200], "internal_span_edges":internal_edges[:200], "observed_spans":observed_spans[:200], "truncated":len(edges)>200 or len(internal_edges)>200 or len(observed_spans)>200 or any(r.get("truncated") for r in responses),
        "routing":"namespace_service_proxy" if proxy_fetch else "namespace_service_fallback", "configured_status":primary["status"], "provenance":"live_jaeger",
        "backends":[dict(namespace=route["namespace"], **{k:v for k,v in response.items() if k!="edges"}) for route,response in zip(routes,responses)]}


def stamp(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def span_error(span):
    """Explicit error indicators only; missing instrumentation stays unknown."""
    tags = {t.get('key'): t.get('value') for t in span.get('tags', []) if isinstance(t, dict)}
    status = tags.get('otel.status_code', tags.get('otel.status.code'))
    if tags.get('error') is True or str(tags.get('error')).lower() == 'true' or status in ('ERROR', 'STATUS_CODE_ERROR', 2):
        return True
    code = number(tags.get('http.response.status_code', tags.get('http.status_code')))
    if code is not None:
        return code >= 500
    if tags.get('error') is False or str(tags.get('error')).lower() == 'false' or status in ('OK', 'STATUS_CODE_OK', 1):
        return False
    return None


def trace_evidence(payload, cluster, inventory, provenance):
    """Service names alone are insufficient to link shared-backend trace resources."""
    pods = inventory.get("pods", [])
    known = {entity_ref(cluster, p["namespace"], "pod", p["name"]) for p in pods}
    by_uid = {p["uid"]: entity_ref(cluster, p["namespace"], "pod", p["name"]) for p in pods if p.get("uid")}
    edges, internal_edges, observed_spans, unmapped, total = [], [], [], 0, 0
    seen_traces = set()
    truncated = False
    for trace in payload.get("data", []):
        trace_id = trace.get("traceID")
        if not trace_id or trace_id in seen_traces:
            continue
        if len(seen_traces) >= 20:
            truncated = True
            break
        seen_traces.add(trace_id)
        truncated = truncated or len(trace.get("spans", [])) > 1000
        processes = trace.get("processes", {})
        spans = {s.get("spanID"): s for s in trace.get("spans", [])[:1000]}
        refs = {}
        for pid, process in processes.items():
            tags = {t.get("key"): t.get("value") for t in process.get("tags", []) if isinstance(t, dict)}
            if tags.get("k8s.cluster.name") not in (None, "", cluster):
                refs[pid] = None
                continue
            ref = by_uid.get(tags.get("k8s.pod.uid"))
            if not ref and tags.get("k8s.cluster.name") == cluster:
                candidate = entity_ref(cluster, str(tags.get("k8s.namespace.name", "")), "pod", str(tags.get("k8s.pod.name", "")))
                ref = candidate if candidate in known else None
            if ref:
                parts = ref.split('|')
                if any(tags.get(tag) not in (None, '', expected) for tag, expected in
                       [('k8s.namespace.name', parts[1]), ('k8s.pod.name', parts[3])]):
                    ref = None
            refs[pid] = ref
        for span_id, child in spans.items():
            total += 1
            target = refs.get(child.get("processID"))
            if not target:
                unmapped += 1
                continue
            start, duration = number(child.get('startTime')), number(child.get('duration'))
            tags = {t.get('key'): t.get('value') for t in child.get('tags', []) if isinstance(t, dict)}
            process_tags = {t.get('key'):t.get('value') for t in processes.get(child.get('processID'), {}).get('tags', []) if isinstance(t, dict)}
            if (isinstance(trace_id, str) and re.fullmatch(r'(?:[0-9a-fA-F]{16}|[0-9a-fA-F]{32})', trace_id)
                    and isinstance(span_id, str) and re.fullmatch(r'[0-9a-fA-F]{16}', span_id)
                    and start is not None and start > 0 and duration is not None and duration >= 0
                    and len(spans) == len(trace.get('spans', [])[:1000])
                    and by_uid.get(process_tags.get('k8s.pod.uid')) == target):
                observed_spans.append(dict(trace_id=trace_id.lower(), span_id=span_id.lower(), entity_ref=target,
                    pod_uid=process_tags['k8s.pod.uid'],
                    start=start/1e6, end=(start+duration)/1e6, duration_ms=duration/1000,
                    http_status_code=number(tags.get('http.response.status_code', tags.get('http.status_code'))),
                    error=span_error(child), provenance=provenance))
            for reference in child.get("references", []):
                if reference.get("refType") != "CHILD_OF" or reference.get("traceID") != trace.get("traceID"):
                    continue
                parent = spans.get(reference.get("spanID"), {})
                source = refs.get(parent.get("processID"))
                start, parent_start = number(child.get("startTime")), number(parent.get("startTime"))
                duration = number(parent.get("duration"))
                if source and start is not None and parent_start is not None and duration is not None and duration >= 0 and parent_start <= start <= parent_start+duration:
                    (internal_edges if source == target else edges).append({"source": source, "target": target, "trace_id": trace.get("traceID"),
                        "parent_span_id": reference["spanID"], "span_id": span_id, "at": start/1e6,
                        "provenance": provenance, "kind": "internal_span" if source == target else "observed_call", "causal_confirmed": False,
                        "caller_error": span_error(parent), "callee_error": span_error(child),
                        "caller_end": (parent_start+duration)/1e6,
                        "callee_end": (start+number(child.get('duration')))/1e6 if number(child.get('duration')) is not None and number(child.get('duration')) >= 0 else None})
    return {"status": "mapped" if edges else "no_mapped_calls", "edges": edges[:200], "spans_seen": total,
        "internal_span_edges": internal_edges[:200],
        "observed_spans": observed_spans[:200],
        "unmapped_spans": unmapped, "truncated": truncated or len(edges) > 200 or len(internal_edges) > 200 or len(observed_spans) > 200,
        "provenance": provenance}


def rollout_evidence(payload, cluster, inventory, provenance, entities=None):
    owners = {}
    for rs in payload.get("items", [])[:10000]:
        meta = rs.get("metadata", {})
        owner = next((o for o in meta.get("ownerReferences", []) if o.get("kind") == "Deployment" and o.get("controller") is True), None)
        revision = str(meta.get("annotations", {}).get("deployment.kubernetes.io/revision", ""))
        if not owner or not owner.get("uid") or not revision.isdigit() or not meta.get("uid") or meta.get("annotations", {}).get("deployment.kubernetes.io/revision-history"):
            continue
        template = rs.get("spec", {}).get("template", {})
        labels = {k:v for k,v in template.get("metadata", {}).get("labels", {}).items() if k != "pod-template-hash"}
        # Keep only a content hash of potentially sensitive env/config values.
        fingerprint = hashlib.sha256(json.dumps({"spec": template.get("spec", {}), "labels": labels}, sort_keys=True).encode()).hexdigest()
        item = {"revision": int(revision), "at": stamp(meta.get("creationTimestamp")), "uid": meta["uid"],
            "replicaset": meta.get("name"), "fingerprint": fingerprint,
            "images": {c.get("name"): c.get("image") for c in template.get("spec", {}).get("containers", [])}}
        owners.setdefault((meta.get("namespace", ""), owner["uid"], owner.get("name", "")), []).append(item)
    changes = []
    for (namespace, uid, name), revisions in owners.items():
        # Duplicate revisions can result from inconsistent snapshots; do not guess.
        revisions.sort(key=lambda r: r["revision"])
        if len({r["revision"] for r in revisions}) != len(revisions):
            continue
        for before, after in zip(revisions, revisions[1:]):
            if after["revision"] != before["revision"]+1 or before["fingerprint"] == after["fingerprint"] or after["at"] is None or before["at"] is None or before["at"] >= after["at"]:
                continue
            targets = [entity_ref(cluster, namespace, "pod", p["name"]) for p in inventory.get("pods", [])
                       if p.get("namespace") == namespace and p.get("owner_kind") == "ReplicaSet" and p.get("owner_uid") == after["uid"]]
            deployment = entity_ref(cluster, namespace, "deployment", name)
            if entities is not None and deployment not in entities and not set(targets).intersection(entities):
                continue
            changes.append({"deployment": deployment, "deployment_uid": uid,
                "targets": targets, "before": before, "after": after, "at": after["at"], "provenance": provenance,
                "interpretation": "ReplicaSet creation time is rollout evidence, not proof of incident causation"})
    changes.sort(key=lambda c:c["at"])
    return {"status": "observed" if changes else "no_comparable_revisions", "changes": changes[-100:], "provenance": provenance, "truncated": len(payload.get("items", [])) > 10000 or len(changes) > 100}


def error_propagation_paths(edges, endpoints=None):
    """Reverse contiguous error spans in one trace; never join by service name alone."""
    valid = []
    unique = set()
    for edge in edges:
        parts = [str(edge.get(k, '')).split('|') for k in ('source', 'target')]
        times = [number(edge.get(k)) for k in ('at', 'callee_end', 'caller_end')]
        if (edge.get('caller_error') is True and edge.get('callee_error') is True
                and all(t is not None for t in times) and times[0] <= times[1] <= times[2]
                and edge.get('trace_id') and edge.get('span_id') and edge.get('parent_span_id')
                and edge['span_id'] != edge['parent_span_id']
                and all(len(p) == 4 and p[0] and p[1] for p in parts)
                and parts[0][:2] == parts[1][:2]
                and (parts[0] != parts[1] or edge.get('kind') == 'internal_span')):
            key = tuple(edge[k] for k in ('trace_id', 'span_id', 'parent_span_id', 'source', 'target', 'at', 'callee_end', 'caller_end'))
            if key not in unique:
                unique.add(key)
                valid.append(edge)
    by_child = {}
    for edge in valid:
        by_child.setdefault((edge['trace_id'], edge['span_id']), []).append(edge)
    paths = []
    for initial in valid:
        if endpoints is not None and initial['target'] not in endpoints:
            continue
        pending = [([initial], {initial['span_id'], initial['parent_span_id']})]
        while pending:
            chain, seen = pending.pop()
            last = chain[-1]
            if last['source'] != initial['target'] and (endpoints is None or last['source'] in endpoints):
                paths.append(chain)
            for parent in by_child.get((last['trace_id'], last['parent_span_id']), []):
                if (parent['target'] == last['source'] and parent['parent_span_id'] not in seen
                        and (parent['source'] == last['source'] or parent['source'] not in {chain[0]['target'], *(e['source'] for e in chain)})
                        and parent['at'] <= last['at'] and parent['callee_end'] == last['caller_end']):
                    pending.append((chain + [parent], seen | {parent['parent_span_id']}))
    return sorted(paths, key=len, reverse=True)


def trace_span_edges(context):
    traces = context.get('traces', {})
    return traces.get('edges', []) + traces.get('internal_span_edges', [])


def propagation_entities(chain):
    path = [chain[0]['target']]
    for edge in chain:
        if edge['source'] != path[-1]:
            path.append(edge['source'])
    return path


def qualify_context(result, a, b, kind):
    context = result.get("context_evidence", {})
    refs = [entity_ref(g["cluster_id"], g["namespace"], g["entity_kind"], g["entity"]) for g in (a, b)]
    firsts = [g["first_seen"] for g in (a, b)]
    if None in firsts:
        return {"status": "unknown", "reason": "missing alert timestamps", "trace_refs": [], "change_refs": []}
    known_traces = []
    for group in (a, b):
        values = set()
        for index in group.get('alert_indices', []):
            raw = result['raw_alerts'][index]['raw']
            value = raw.get('trace_id', raw.get('traceID'))
            if isinstance(value, str) and value:
                values.add(value.lower())
        known_traces.append(values)
    def matches_alert_trace(edge):
        return all(not values or str(edge.get('trace_id', '')).lower() in values for values in known_traces)
    start, end = min(firsts)-result["time_window"], max(g["last_seen"] for g in (a, b))
    traces = [e for e in context.get("traces", {}).get("edges", []) if [e["source"], e["target"]] == refs and start <= e["at"] <= end and matches_alert_trace(e)]
    changes = [e for e in context.get("rollouts", {}).get("changes", []) if (e["deployment"] == refs[0] or refs[0] in e["targets"])
               and (e["deployment"] == refs[1] or refs[1] in e["targets"]) and start <= e["at"] <= min(firsts)]
    reverse = [e for e in context.get('traces', {}).get('edges', [])
               if [e['target'], e['source']] == refs and start <= e['at'] <= end and matches_alert_trace(e)]
    eligible = [e for e in trace_span_edges(context)
                if start <= e['at'] <= end and matches_alert_trace(e)]
    paths = [chain for chain in error_propagation_paths(eligible, refs)
             if [chain[0]['target'], chain[-1]['source']] in (refs, refs[::-1])]
    forward_paths = [chain for chain in paths if chain[0]['target'] == refs[0]]
    propagated = bool(forward_paths)
    supported = propagated if kind == 'propagates_to' else bool(changes) if kind == 'change_trigger' else bool(traces) if kind == 'observed_call' else False
    # Keep every hop of the representative path, even when it exceeds three spans.
    selected = forward_paths[0] if kind == 'propagates_to' and propagated else [] if kind == 'propagates_to' else traces + reverse
    path_refs = [{'source': chain[0]['target'], 'target': chain[-1]['source'],
                  'path': propagation_entities(chain),
                  'trace_id': chain[0]['trace_id'], 'hops': len(propagation_entities(chain))-1,
                  'span_link_count': len(chain),
                  'span_links': [{'span_id': e['span_id'], 'parent_span_id': e['parent_span_id']} for e in chain],
                  'basis': 'contiguous error-bearing spans in one trace', 'causal_confirmed': False} for chain in paths]
    return {'status': 'pass' if supported else 'unknown',
        'reason': 'error-bearing dependency call in reverse direction; propagation hypothesis only' if propagated and kind == 'propagates_to' else 'observed context' if supported else 'no matching contextual evidence',
        'trace_refs': [{**{k:e[k] for k in ('trace_id','span_id','parent_span_id','at','provenance')},
                       'call_source': e['source'], 'call_target': e['target'],
                       'caller_error': e.get('caller_error'), 'callee_error': e.get('callee_error'),
                       'kind': e.get('kind', 'observed_call')}
                      for e in (selected if kind == 'propagates_to' else selected[:3])],
        'propagation_direction': refs if propagated else None,
        'propagation_candidates': path_refs,
        'propagation_path': next((p['path'] for p in path_refs if p['source'] == refs[0]), []),
        'matching_trace_count': len(selected),
        'interpretation': 'call direction and failure propagation direction are distinct; no causal confirmation',
        'change_refs': [{'deployment_uid':e['deployment_uid'], 'replicaset_uid':e['after']['uid'], 'revision':e['after']['revision'], 'at':e['at'], 'provenance':e['provenance']} for e in changes[:3]]}



def recall_context(result):
    """Observed links can recall dissimilar alert texts, without dropping semantic candidates."""
    by_id = {g["group_id"]: g for g in result["groups"]}
    groups = [by_id[k] for k in result.get("ranking", list(by_id))]
    entities = {}
    for g in groups:
        ref = entity_ref(g["cluster_id"], g["namespace"], g["entity_kind"], g["entity"])
        entities.setdefault(ref, []).append(g)
    links = [(e["source"], e["target"], "observed_call") for e in result.get("context_evidence", {}).get("traces", {}).get("edges", [])]
    links.extend((chain[0]['target'], chain[-1]['source'], 'propagates_to')
                 for chain in error_propagation_paths(trace_span_edges(result.get('context_evidence', {})))
                 if len(propagation_entities(chain)) > 2)
    for change in result.get("context_evidence", {}).get("rollouts", {}).get("changes", []):
        links.extend((change["deployment"], target, "change_trigger") for target in change["targets"])
    pairs = {}
    for source, target, kind in links:
        for a in entities.get(source, []):
            for b in entities.get(target, []):
                if a["group_id"] == b["group_id"] or a["namespace"] != b["namespace"] or a["cluster_id"] != b["cluster_id"]:
                    continue
                evidence = qualify_context(result, a, b, kind)
                if evidence["status"] != "pass":
                    continue
                key = tuple(sorted((a["group_id"], b["group_id"])))
                pairs[key] = {"left": a["group_id"], "right": b["group_id"], "score": 1, "retrieval": {"observed_context": kind}}
    for p in result.get("candidate_pairs", []):
        pairs.setdefault(tuple(sorted((p["left"], p["right"]))), p)
    result["candidate_pairs"] = list(pairs.values())
    result["context_candidate_pairs"] = sum(bool(p.get("retrieval", {}).get("observed_context")) for p in pairs.values())
