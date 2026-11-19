"""Semantica ContextGraph projection of a single recorded investigation."""
from collections import Counter
from importlib.metadata import version


class UnknownFocus(LookupError):
    pass


class SemanticaUnavailable(RuntimeError):
    pass


def resource_graph(model):
    # Load on first graph request so other RCA readers can operate independently.
    try:
        from semantica.context.context_graph import ContextGraph
    except ImportError as exc:
        raise SemanticaUnavailable("Semantica 图引擎加载失败，请检查服务端依赖。") from exc

    graph = ContextGraph(advanced_analytics=False, extract_entities=False, extract_relationships=False)
    graph.add_nodes([{"id": o["id"], "type": o["kind"], "content": o["name"], "properties": o}
                     for o in model["objects"]])
    grouped = {}
    for link in model["relations"]:
        key = (link["source_id"], link["type"], link["target_id"])
        refs = [link.get("evidence_id"), *(link.get("evidence_ids") or [])]
        grouped.setdefault(key, set()).update(ref for ref in refs if ref)
    graph.add_edges([{"source_id": a, "type": kind, "target_id": b,
                      "metadata": {"evidence_ids": sorted(refs), "is_causal_proof": False}}
                     for (a, kind, b), refs in grouped.items()])
    return graph


def export_investigation(model):
    graph = resource_graph(model)
    observations = model["observations"]
    fields = ("source", "source_agent", "signal_type", "observed_at", "event_time", "sampled_at",
              "timestamp", "identity_status", "scope_status", "temporality", "origin", "excerpted")
    graph.add_nodes([{"id": o["id"], "type": "Observation", "content": o["observation"],
                      "properties": {k: o[k] for k in fields if o.get(k) is not None}}
                     for o in observations])
    graph.add_edges([{"source_id": o["id"], "type": "observed_on", "target_id": o["entity_id"]}
                     for o in observations if o.get("entity_id")])
    return {**graph.to_dict(), "investigation": {"run_id": model["run_id"], "revision": model["revision"],
            "scope": model["scope"], "semantics": model["semantics"], "gaps": model["gaps"],
            "engine": "Semantica", "engine_version": version("semantica")}}


def investigation_neighborhood(model: dict, *, focus_id: str, query: str, offset: int,
                               edge_offset: int, limit: int) -> dict:
    """Browse the actual directed Semantica graph, keeping parallel predicates distinct."""
    data = resource_graph(model).to_dict()
    objects = {node["id"]: node["properties"] for node in data["nodes"]}
    if focus_id and focus_id not in objects:
        raise UnknownFocus(focus_id)
    edges = data["edges"]
    degree = Counter()
    for e in edges:
        degree.update({e["source"], e["target"]})
    term = query.strip().casefold()
    matches = [o for o in objects.values() if not term or any(
        term in str(o[field]).casefold() for field in ("kind", "name", "cluster_id", "namespace", "uid", "id"))]
    matches.sort(key=lambda o: (-degree[o["id"]], o["kind"], o["name"], o["id"]))
    page = matches[offset:offset + limit]
    focus = objects.get(focus_id) or (page[0] if page else None)
    rows = []
    if focus:
        identity = focus["id"]
        for e in edges:
            source, relation, target = e["source"], e["type"], e["target"]
            if identity in (source, target):
                neighbor = objects[target if source == identity else source]
                rows.append({"source_id": source, "target_id": target, "type": relation,
                             "neighbor": neighbor, "evidence_ids": e["metadata"]["evidence_ids"]})
        rows.sort(key=lambda e: (e["type"], e["neighbor"]["name"], e["source_id"], e["target_id"]))
    return {"engine": "Semantica", "engine_version": version("semantica"),
            "run_id": model["run_id"], "revision": model["revision"], "scope": model["scope"],
            "counts": {"objects": len(objects), "edges": len(edges), "observations": len(model["observations"])},
            "objects": page, "total_objects": len(matches), "offset": offset, "limit": limit,
            "focus": focus, "edges": rows[edge_offset:edge_offset + limit],
            "total_edges": len(rows), "edge_offset": edge_offset, "gaps": model["gaps"]}
