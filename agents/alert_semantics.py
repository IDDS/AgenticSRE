"""Symbolic distillation, bounded semantic retrieval and evidence-qualified SCAG.

No relation produced here authorizes suppression, a repair or a causal conclusion.
Raw records remain in alert_compression; all derived views carry their provenance.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from bisect import bisect_left
import hashlib
import math
import re
import statistics
import time

from agents.alert_embeddings import local_embeddings, embedding_metadata


async def enrich_retrieval(result, timeout_s=120):
    """A timed-out worker never mutates a saved scan after the response."""
    import asyncio
    import copy
    from tools.llm_client import run_sync_in_daemon_thread
    snapshot = copy.deepcopy(result)
    try:
        enriched = await asyncio.wait_for(run_sync_in_daemon_thread(recall_candidates, snapshot), timeout_s)
    except asyncio.TimeoutError:
        def unavailable(_):
            raise TimeoutError("retrieval response deadline exceeded")
        enriched = recall_candidates(copy.deepcopy(result), encoder=unavailable)
    result.update(candidate_pairs=enriched["candidate_pairs"], retrieval=enriched["retrieval"])
    return result


RELATION_TYPES = {"equivalent", "shared_root", "propagates_to", "shared_dependency", "change_trigger", "conflicts"}
VOLATILE = re.compile(r"\b(request_id|trace_id|span_id|correlation_id|duration_ms|latency_ms)=([^\s,;]+)", re.I)
REQUEST_ID = re.compile(r"\brequest\s+(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{24,64})\b", re.I)
ELAPSED_MS = re.compile(r"\b(after|waiting)\s+\d+(?:\.\d+)?\s+(ms|milliseconds)\b", re.I)
ONTOLOGY = {
    "image_pull": ("imagepull", "image pull", "pull image", "pulling image", "镜像拉取"),
    "memory": ("oom", "out of memory", "memory pressure", "内存不足", "内存溢出"),
    "connectivity": ("connection refused", "connection reset", "timeout", "timed out", "连接拒绝", "超时"),
    "availability": ("unavailable", "not ready", "notready", "unhealthy", "不可用", "未就绪"),
}


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def symbolic(row):
    """Only explicitly volatile fields are masked; codes, versions and negation stay."""
    raw = row["raw"]
    metric = raw.get("metric_name")
    value = number(raw.get("metric_value"))
    if metric and value is not None:
        labels = raw.get("series_labels", row["labels"])
        if not isinstance(labels, dict):
            raise ValueError("series_labels must be an object")
        identity = repr(sorted((str(k), str(v)) for k, v in labels.items()))
        return {"kind": "metric", "key": (str(metric), str(raw.get("unit", "")), identity),
                "value": value, "template": str(metric)}
    template = VOLATILE.sub(lambda m: m.group(1).lower()+"=<VAR>", row["message"])
    template = REQUEST_ID.sub("request <VAR>", template)
    template = ELAPSED_MS.sub(lambda m: m.group(1).lower()+" <VAR> "+m.group(2).lower(), template)
    return {"kind": "log" if template != row["message"] or "log" in row["source"] else "event",
            "key": template, "template": template}


def annotate_groups(result, baseline=None):
    """Mine templates, summarize metric samples, and rank explicit baseline shifts."""
    result["signature_version"] = 2
    from drain3 import TemplateMiner
    from drain3.template_miner_config import TemplateMinerConfig
    cfg = TemplateMinerConfig()
    cfg.drain_sim_th = .8
    cfg.drain_max_clusters = 1000
    miner = TemplateMiner(config=cfg)
    rows = result["raw_alerts"]
    baseline_counts = Counter()
    if baseline is not None:
        if not isinstance(baseline, dict) or not isinstance(baseline.get("alerts"), list):
            raise ValueError("baseline must contain alerts and duration_s")
        from agents.alert_compression import normalize_alerts
        clusters = {r["cluster_id"] for r in rows}
        namespaces = {r["namespace"] for r in rows}
        history = normalize_alerts(baseline["alerts"], next(iter(clusters)) if len(clusters) == 1 else "",
                                   next(iter(namespaces)) if len(namespaces) == 1 else "")
        duration = number(baseline.get("duration_s"))
        end = number(baseline.get("end_time"))
        current = [r["timestamp"] for r in rows if r["timestamp"] is not None]
        if not duration or duration <= 0 or end is None or (current and end > min(current)):
            raise ValueError("baseline must be a positive window ending before current observations")
        if any(r["timestamp"] is None or not end-duration <= r["timestamp"] < end for r in history):
            raise ValueError("baseline records fall outside its declared window")
        for r in history:
            baseline_counts[frequency_key(r)] += 1
    result["baseline_evidence"] = baseline
    cluster_ids = {}
    for g in result["groups"]:
        members = [rows[i] for i in g["alert_indices"]]
        symbol = symbolic(members[0])
        mined = miner.add_log_message(symbol["template"] or members[0]["name"])
        cluster_ids[g["group_id"]] = mined["cluster_id"]
        g["symbolic"] = {"kind": symbol["kind"], "template": symbol["template"],
            "drain_template": mined["template_mined"], "miner": "drain3",
            "merge_guard": "equal safe template; generalized Drain wildcards alone never merge"}
        # Use a scoped signature rather than an ephemeral Drain cluster number.
        g["template_id"] = hashlib.sha256(repr(frequency_key(members[0])).encode()).hexdigest()[:24]
        if symbol["kind"] == "metric" and all(symbolic(r)["kind"] == "metric" and symbolic(r)["key"] == symbol["key"] for r in members):
            values = [symbolic(r)["value"] for r in members]
            g["metric_summary"] = {"min": min(values), "max": max(values), "mean": statistics.mean(values),
                "samples": len(values), "unique_samples": len({(r["timestamp"], symbolic(r)["value"]) for r in members}),
                "min_alert_id": members[values.index(min(values))]["alert_id"],
                "max_alert_id": members[values.index(max(values))]["alert_id"]}
        if any(symbolic(r)["key"] != symbol["key"] for r in members):
            g["symbolic"].update(kind="mixed", template="人工合并的多种模板；请查看原文", merge_guard="manual merge")
        count = baseline_counts[frequency_key(members[0])]
        expected = count * result["time_window"] / duration if baseline is not None else None
        # One-sided count deviation under a supplied Poisson baseline, not a p-value.
        score = max(0, (len(members)-expected)/math.sqrt(max(expected, 1))) if expected is not None else None
        g["frequency"] = {"status": "supplied_baseline" if baseline is not None else "unknown",
            "observed_records": len(members), "expected_records": expected,
            "deviation_score": round(score, 3) if score is not None else None}
        g["priority"] = {"severity_rank": {"critical": 4, "high": 3, "warning": 2, "info": 1}.get(g["severity"], 0),
            "frequency_score": score or 0, "rare": len(members) == 1}
    for g in result["groups"]:
        cluster = miner.drain.id_to_cluster.get(cluster_ids[g["group_id"]])
        if cluster is not None:
            g["symbolic"]["drain_template"] = cluster.get_template()
    result["ranking"] = [g["group_id"] for g in sorted(result["groups"], key=lambda g:
        (g["priority"]["severity_rank"], g["priority"]["frequency_score"], g["priority"]["rare"]), reverse=True)]
    result["distillation"] = {"template_miner": "drain3", "baseline": "supplied" if baseline is not None else "missing"}
    return result


def frequency_key(row):
    from agents.alert_compression import observation_dimensions
    s = symbolic(row)
    key = (row["cluster_id"], row["namespace"], row["entity_kind"], row["entity"],
            row["name"], row["source"], row["state"], observation_dimensions(row["labels"]), str(s["key"]))
    from agents.alert_identity import target_identity
    identity=target_identity(row)
    return key+(identity['entity_uid'],identity['identity_status']) if identity['identity_status']!='uid_not_reported' else key


def recall_candidates(result, encoder=local_embeddings, top_n=5, max_groups=None, max_pairs=None):
    """Index all groups by default; bound neighbor pools and downstream proposals."""
    import numpy as np
    by_id = {g["group_id"]: g for g in result["groups"]}
    ordered = result.get("ranking", list(by_id))
    groups = [by_id[key] for key in ordered[:max_groups]]
    generic_names = {"applicationlog", "log", "logs", "unknown"}
    texts = [("" if g["representative"].lower() in generic_names and g.get("symbolic", {}).get("template")
              else g["representative"]+" ")+g.get("symbolic", {}).get("template", "")[:600] for g in groups]
    tags = [{tag for tag, words in ONTOLOGY.items() if any(w in t.lower() for w in words)} for t in texts]
    tokens = [set(re.findall(r"[a-zA-Z][a-zA-Z_-]{2,}|[\u4e00-\u9fff]{2,}", t.lower())) for t in texts]
    similarities = None
    vectors = None
    vector_sets = []
    metadata = embedding_metadata() if encoder is local_embeddings else {
        'encoder': 'injected', 'embedding_channels': ['injected']}
    info = {"indexed_groups": len(groups), "total_groups": len(by_id), "top_n": top_n,
        "truncated": len(groups) < len(by_id), **metadata, "status": "ok",
        "embedded_groups": 0, "search_mode": "exact", "approximate": False, "queried_groups": len(groups)}
    info['cosine_aggregation'] = 'maximum_per_channel' if len(metadata['embedding_channels']) > 1 else 'single_channel'
    if groups:
        try:
            if len(groups) <= 256:
                vectors = np.asarray(encoder(texts), dtype=float)
            else:
                unique = list(dict.fromkeys(texts))
                encoded = []
                for start in range(0, len(unique), 128):
                    batch = encoder(unique[start:start+128])
                    if len(batch) != len(unique[start:start+128]):
                        raise ValueError('incomplete embedding batch')
                    encoded.extend(batch)
                lookup = dict(zip(unique, encoded))
                vectors = np.asarray([lookup[t] for t in texts], dtype=float)
                info['unique_texts'] = len(unique)
            if vectors.ndim != 2 or len(vectors) != len(groups) or not np.isfinite(vectors).all():
                raise ValueError("invalid embedding output")
            vector_sets = np.split(vectors, len(metadata['embedding_channels']), axis=1)
            for channel in vector_sets:
                norms = np.linalg.norm(channel, axis=1)
                if (norms == 0).any():
                    raise ValueError("zero embedding")
                channel /= norms[:, None]
            info['embedded_groups'] = len(groups)
            if len(groups) <= 256:
                similarities = np.maximum.reduce([v @ v.T for v in vector_sets])
        except Exception as exc:
            vectors = None
            vector_sets = []
            info.update(status="lexical_fallback", error=type(exc).__name__)
    pools = None
    if len(groups) > 256:
        from agents.alert_retrieval import neighbor_pools
        scoped_groups = [dict(g, _retrieval_window=result['time_window']) for g in groups]
        try:
            searches = [neighbor_pools(scoped_groups, tokens, tags, v, top_n)
                        for v in (vector_sets or [None])]
            pools, search_info = searches[0]
            if len(searches) > 1:
                for other, _ in searches[1:]:
                    for pool, neighbors in zip(pools, other):
                        pool.update(neighbors)
                search_info = dict(search_info, vector_search_channels=len(searches),
                    neighbor_pool_limit=sum(s['neighbor_pool_limit'] for _, s in searches),
                    ann_query_vectors=sum(s.get('ann_query_vectors', 0) for _, s in searches),
                    exact_query_vectors=sum(s.get('exact_query_vectors', 0) for _, s in searches))
        except Exception as exc:
            pools, search_info = neighbor_pools(scoped_groups, tokens, tags, None, top_n)
            info.update(status='lexical_fallback', index_error=type(exc).__name__)
        info.update(search_info)
    candidates = {}
    for i, a in enumerate(groups):
        neighbors = []
        for j in (sorted(pools[i]) if pools is not None else range(len(groups))):
            b = groups[j]
            if i == j or not a["cluster_id"] or a["cluster_id"] != b["cluster_id"] or a["namespace"] != b["namespace"]:
                continue
            if a["first_seen"] is None or b["first_seen"] is None or max(a["last_seen"], b["last_seen"])-min(a["first_seen"], b["first_seen"]) > result["time_window"]:
                continue
            same = bool(a["entity"]) and (a["entity_kind"], a["entity"]) == (b["entity_kind"], b["entity"])
            keyword = len(tokens[i] & tokens[j])/max(1, len(tokens[i] | tokens[j]))
            ontology = bool(tags[i] & tags[j])
            cosine = (float(similarities[i, j]) if similarities is not None else
                      max(float(v[i] @ v[j]) for v in vector_sets) if vector_sets else None)
            if not (same or ontology or keyword >= .3 or cosine is not None and cosine >= .55):
                continue
            score = .2*same + .2*ontology + .15*keyword + .45*max(0, cosine or 0)
            neighbors.append((score, j, dict(cosine=cosine, keyword=keyword, ontology=sorted(tags[i] & tags[j]), same_entity=same)))
        for score, j, evidence in sorted(neighbors, reverse=True)[:top_n]:
            pair = tuple(sorted((a["group_id"], groups[j]["group_id"])))
            candidates[pair] = {"left": pair[0], "right": pair[1], "score": round(score, 4), "retrieval": evidence}
    result["candidate_pairs"] = sorted(candidates.values(), key=lambda p: (-p["score"], p["left"], p["right"]))[:max_pairs]
    info.update(candidate_pairs_total=len(candidates), candidate_pairs_returned=len(result['candidate_pairs']),
                candidate_pairs_truncated=max_pairs is not None and len(candidates) > max_pairs)
    result["retrieval"] = info
    return result


def entity_ref(cluster, namespace, kind, name):
    return "|".join((cluster, namespace, kind.lower(), name))


def kubernetes_topology(payload, cluster_id, observed_at=None):
    """Physical dependency graph from observed selectors and scheduling, not tracing."""
    nodes, edges = set(), []
    for pod in payload.get("pods", []):
        ns = pod.get("namespace", "")
        target = entity_ref(cluster_id, ns, "pod", pod["name"])
        nodes.add(target)
        parents = []
        if pod.get("node"):
            parents.append((entity_ref(cluster_id, "", "node", pod["node"]), "scheduled_on"))
        for kind, plural in (("service", "services"), ("deployment", "deployments")):
            for owner in payload.get(plural, []):
                selector = owner.get("selector", {})
                if owner.get("namespace") == ns and selector and all(pod.get("labels", {}).get(k) == v for k, v in selector.items()):
                    parents.append((entity_ref(cluster_id, ns, kind, owner["name"]), "selects"))
        for parent, relation in parents:
            nodes.add(parent)
            edges.append({"source": parent, "target": target, "kind": relation})
    return {"nodes": sorted(nodes), "edges": edges, "observed_at": observed_at or time.time(),
        "ttl_s": 300, "provenance": "live_kubernetes", "complete": False}


def _path(edges, source, target):
    queue = deque([(source, [])])
    seen = {source}
    while queue:
        node, path = queue.popleft()
        if node == target:
            return path
        if len(path) >= 4:
            continue
        for neighbor in edges.get(node, []):
            if neighbor not in seen:
                seen.add(neighbor)
                queue.append((neighbor, path+[neighbor]))
    return None


def statistical_check(result, a, b):
    """Value-blind, mutual-nearest alignment; no lag search or missing-value fill."""
    series, evidence = [], []
    for group in (a, b):
        points = defaultdict(list)
        for i in group["alert_indices"]:
            row = result["raw_alerts"][i]
            if 'metric_value' not in row['raw']:
                continue
            value, stamp = number(row['raw']['metric_value']), number(row['timestamp'])
            if value is None or stamp is None:
                return {'status': 'unknown', 'reason': 'invalid numeric observation', 'aligned_samples': 0}
            points[stamp].append((value, row['alert_id']))
        if not points:
            metric = result.get('metric_evidence', {}).get('groups', {}).get(group['group_id'], {})
            if (result.get('origin') == 'live_scan' and metric.get('status') == 'ready'
                    and metric.get('provenance') == 'live_prometheus_rule'
                    and metric.get('cluster_id') == group['cluster_id']
                    and metric.get('namespace') == group['namespace']
                    and metric.get('entity_kind') == group['entity_kind'] and metric.get('entity') == group['entity']):
                for stamp, value in metric['points']:
                    points[stamp].append((value, metric['evidence_id']+'@'+str(stamp)))
        if any(len({v for v, _ in values}) > 1 for values in points.values()):
            return {'status': 'unknown', 'reason': 'conflicting values at one timestamp', 'aligned_samples': 0}
        series.append({t: values[0][0] for t, values in points.items()})
        evidence.append({t: [aid for _, aid in values] for t, values in points.items()})
    stamps = [sorted(points) for points in series]
    cadences = [statistics.median([y-x for x, y in zip(ts, ts[1:])]) for ts in stamps if len(ts) > 1]
    tolerance = min(1.0, .1*min(cadences)) if len(cadences) == 2 else 0.0
    def nearest(t, other):
        pos = bisect_left(other, t)
        candidates = other[max(0, pos-1):pos+1]
        if not candidates:
            return None
        ranked = sorted((abs(t-v), v) for v in candidates)
        if ranked[0][0] > tolerance or len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
            return None
        return ranked[0][1]
    matches = []
    for t in stamps[0]:
        u = nearest(t, stamps[1])
        if u is not None and nearest(u, stamps[0]) == t:
            matches.append((t, u))
    coverage = len(matches)/max(len(stamps[0]), len(stamps[1]), 1)
    details = {'aligned_samples': len(matches), 'series_samples': [len(ts) for ts in stamps],
               'alignment': 'mutual_nearest_timestamp', 'tolerance_s': tolerance,
               'coverage': coverage, 'minimum_coverage': .8,
               'max_offset_s': max((abs(t-u) for t,u in matches), default=0),
               'matched_evidence': [{'source_timestamp':t, 'target_timestamp':u,
                                     'source_alert_ids':evidence[0][t], 'target_alert_ids':evidence[1][u]} for t,u in matches],
               'interpretation': 'aligned correlation is not causation; no lag optimization or interpolation',
               'metric_evidence_ids': [result.get('metric_evidence', {}).get('groups', {}).get(g['group_id'], {}).get('evidence_id') for g in (a,b)]}
    if len(matches) < 5 or coverage < .8:
        return dict(details, status='unknown', reason='at least five aligned numeric samples and 80% coverage required')
    x = [series[0][t] for t, _ in matches]
    y = [series[1][u] for _, u in matches]
    if not statistics.pstdev(x) or not statistics.pstdev(y):
        return dict(details, status='unknown', reason='constant series')
    corr = statistics.correlation(x, y)
    return dict(details, status='pass' if corr >= .8 else 'fail', pearson=round(corr, 4),
                reason='positive linear co-variation threshold met' if corr >= .8 else 'positive linear co-variation threshold not met')


def build_scag(result, topology=None, as_of=None):
    topology = topology or {}
    as_of = time.time() if as_of is None else as_of
    observed = number(topology.get("observed_at"))
    ttl = number(topology.get("ttl_s"))
    fresh = observed is not None and ttl is not None and 0 < ttl <= 3600 and 0 <= as_of-observed <= ttl
    adjacency = defaultdict(list)
    for edge in topology.get("edges", [])[:10000]:
        source_parts, target_parts = edge["source"].split("|"), edge["target"].split("|")
        if len(source_parts) == len(target_parts) == 4 and source_parts[0] and source_parts[0] == target_parts[0]:
            adjacency[edge["source"]].append(edge["target"])
    groups = {g["group_id"]: g for g in result["groups"]}
    proposals = {(p["left"], p["right"]): dict(p, type="related", reason="semantic retrieval candidate") for p in result.get("candidate_pairs", [])}
    for r in result.get("relations", []):
        proposals.pop((r["right"], r["left"]), None)
        proposals[(r["left"], r["right"])] = r
    edges = []
    for pair, proposal in proposals.items():
        if pair[0] not in groups or pair[1] not in groups:
            continue
        a, b = (groups[key] for key in pair)
        scope = bool(a["cluster_id"]) and a["cluster_id"] == b["cluster_id"] and a["namespace"] == b["namespace"]
        source, target = [entity_ref(g["cluster_id"], g["namespace"], g["entity_kind"], g["entity"]) for g in (a, b)]
        path = _path(adjacency, source, target) if fresh else None
        reverse_path = _path(adjacency, target, source) if fresh else None
        scope = scope and all(g["namespace"] or g["entity_kind"].lower() == "node" for g in (a, b))
        same = source == target and bool(a["entity"])
        structural = {"status": "pass" if scope and (same or path is not None or reverse_path is not None) else "unknown",
            "reason": "same entity" if same else "fresh topology path" if path is not None or reverse_path is not None else "missing or stale topology path",
            "path": [source]+path if path is not None else [target]+reverse_path if reverse_path is not None else [],
            "provenance": topology.get("provenance", "missing")}
        kind = proposal.get("type", "related")
        if kind in {"shared_dependency", "shared_root"} and fresh:
            parents = [node for node, children in adjacency.items() if source in children and target in children]
            if parents:
                structural.update(status="pass", reason="observed common dependency", path=[source, parents[0], target])
        if kind in {"propagates_to", "change_trigger"} and not (fresh and path is not None and not same):
            structural.update(status="unknown", reason="directed dependency evidence required")
        stamps = [g[k] for g in (a, b) for k in ("first_seen", "last_seen")]
        temporal = {"status": "unknown", "reason": "missing timestamps"}
        if None not in stamps:
            consistent = max(stamps)-min(stamps) <= result["time_window"]
            if kind in {"propagates_to", "change_trigger"}:
                consistent = consistent and a["first_seen"] < b["first_seen"]
            temporal = {"status": "pass" if consistent else "fail", "window_s": result["time_window"], "source_first": a["first_seen"], "target_first": b["first_seen"]}
        checks = {"structure": structural, "time": temporal, "statistics": statistical_check(result, a, b)}
        from agents.alert_identity import compatible_members
        incarnation_ok=compatible_members([result['raw_alerts'][i] for g in (a,b) for i in g['alert_indices']])
        from agents.alert_context import qualify_context
        context_check = qualify_context(result, a, b, kind)
        if scope and incarnation_ok and context_check["trace_refs"] and (kind != "propagates_to" or context_check["status"] == "pass"):
            structural.update(status="pass", reason="error-bearing dependency call" if kind == "propagates_to" else "observed parent-child spans",
                path=context_check['propagation_path'] if kind == 'propagates_to' else [source, target], provenance=context_check["trace_refs"][0]["provenance"],
                observed_call_direction=[context_check['trace_refs'][0]['call_source'], context_check['trace_refs'][0]['call_target']])
            if kind == 'propagates_to':
                structural['observed_call_path'] = list(reversed(context_check['propagation_path']))
                structural['observed_call_direction'] = [target, source]
        if kind in {"propagates_to", "change_trigger"}:
            checks["context"] = context_check
        if not incarnation_ok:
            structural.update(status='unknown',reason='object incarnation differs or identity conflicts; association not verified')
        status = "contradicted" if not scope or any(c["status"] == "fail" for c in checks.values()) else "evidence_supported" if all(c["status"] == "pass" for c in checks.values()) else "unverified"
        edges.append({"left": pair[0], "right": pair[1], "type": kind, "status": status, "checks": checks,
            "causal_confirmed": False, "context": context_check, "reason": proposal.get("reason", ""),
            "evidence_alert_ids": proposal.get("evidence_alert_ids", [a["representative_alert_id"], b["representative_alert_id"]])})
    result["scag"] = {"version": 1, "status": "ready", "nodes": [{"id": g["group_id"], "entity": g["entity"], "severity": g["severity"]} for g in groups.values()],
        "edges": edges, "topology": topology, "evaluated_at": as_of,
        "interpretation": "checks qualify association evidence, never establish a root cause or authorize suppression"}
    from agents.alert_merge_review import build_merge_review
    build_merge_review(result, as_of)
    from agents.alert_root_candidates import build_root_candidates
    build_root_candidates(result, as_of)
    return result
