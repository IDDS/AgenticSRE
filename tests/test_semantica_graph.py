import json

import pytest
from fastapi import HTTPException, Response
from semantica.explorer.session import GraphSession

from integrations.semantica_graph import export_investigation, investigation_neighborhood
from operational_ontology import build_investigation
from web_app import app as dashboard


def investigation():
    def pod(uid):
        return {"kind": "Pod", "metadata": {"name": "api", "namespace": "shop", "uid": uid,
                "ownerReferences": [{"kind": "ReplicaSet", "name": "api-rs", "uid": "rs"}]},
                "spec": {"nodeName": "node-a"}}
    raws = [pod("p1"), pod("p1"), pod("p2"), {"kind": "Event", "metadata": {
        "uid": "event", "ownerReferences": [{"kind": "Job", "name": "event-owner", "uid": "job"}]},
        "regarding": {"kind": "Pod", "name": "api", "namespace": "shop", "uid": "p1"},
        "node_name": "event-emitter"}, {}]
    return {"id": "case", "namespace": "shop", "cluster_id": "east", "result": {"observations": [
        {"source": "kubectl", "signal_type": "observed", "raw_ref": raw, "observation": f"record {i}"}
        for i, raw in enumerate(raws)]}}


def browse(model, **options):
    return investigation_neighborhood(model, **dict(dict(focus_id="", query="", offset=0, edge_offset=0, limit=12), **options))


def test_directed_graph_pagination_and_uid_identity():
    model = build_investigation(investigation())
    m = browse(model, query="P1", limit=1)
    assert m["engine"] == "Semantica" and m["engine_version"]
    assert m["counts"] == {"objects": 4, "edges": 4, "observations": 5}
    assert m["total_objects"] == 1 and m["focus"]["uid"] == "p1" and m["total_edges"] == 2
    edge = m["edges"][0]
    assert edge["source_id"] == m["focus"]["id"] and len(edge["evidence_ids"]) == 2
    assert edge["evidence_ids"] == sorted(o["id"] for o in model["observations"][:2])
    assert browse(model, focus_id=m["focus"]["id"], limit=1, edge_offset=1)["edges"][0]["type"] != edge["type"]
    neighbor = browse(model, focus_id=edge["target_id"])
    assert {e["neighbor"]["uid"] for e in neighbor["edges"]} == {"p1", "p2"}
    assert all(e["target_id"] == neighbor["focus"]["id"] for e in neighbor["edges"])
    assert browse(model, query="absent")["focus"] is None
    assert browse(model, offset=1, limit=1)["objects"] != browse(model, limit=1)["objects"]


def test_native_explorer_roundtrip_preserves_references_and_parallel_predicates(tmp_path):
    model = build_investigation(investigation())
    link = model["relations"][0]
    # A second predicate between the same pair, supported by two different sources.
    refs = [o["id"] for o in model["observations"][:2]]
    model["relations"].append({**link, "type": "test_predicate", "evidence_ids": refs})
    result = export_investigation(model)
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(result))
    session = GraphSession.from_file(str(path))
    assert session.get_raw_counts() == (9, 9)
    assert result["investigation"]["revision"] == model["revision"]
    loaded = session.graph.to_dict()
    edges = [e for e in loaded["edges"] if (e["source"], e["target"]) == (link["source_id"], link["target_id"])]
    assert {e["type"] for e in edges} == {link["type"], "test_predicate"}
    assert all(session.get_node(ref) is not None for e in edges for ref in e["metadata"]["evidence_ids"])
    assert set(next(e for e in edges if e["type"] == "test_predicate")["metadata"]["evidence_ids"]) == set(refs)
    assert all("raw_ref" not in node["metadata"] for node in loaded["nodes"])
    unresolved = model["observations"][-1]["id"]
    assert session.get_node(unresolved) and not any(e["source"] == unresolved for e in loaded["edges"])


def test_routes_pin_revision_and_handle_empty_or_unknown_objects(monkeypatch):
    record = {"id": "empty", "status": "running"}
    monkeypatch.setitem(dashboard._state["rca_runs"], record["id"], record)
    def graph(**options):
        return dashboard.rca_ontology_graph("empty", Response(), **dict(
            dict(focus_id="", revision="", q="", offset=0, edge_offset=0, limit=12), **options))
    empty = graph()
    assert empty["focus"] is None and empty["counts"]["objects"] == 0
    result = dashboard.rca_semantica_export("empty", empty["revision"])
    assert result.headers["Cache-Control"] == "private, no-store"
    assert json.loads(result.body)["statistics"] == {"node_count": 0, "edge_count": 0}
    with pytest.raises(HTTPException) as exc:
        graph(focus_id="missing")
    assert exc.value.status_code == 404
    record["status"] = "completed"
    for fetch in [lambda: graph(revision=empty["revision"]),
                  lambda: dashboard.rca_semantica_export("empty", empty["revision"])]:
        with pytest.raises(HTTPException) as exc:
            fetch()
        assert exc.value.status_code == 409


def test_unbound_observations_remain_reachable_with_no_resource_and_pin_revision(monkeypatch):
    import asyncio
    record = investigation()
    source = record['result']['observations']
    source.extend([{**source[-1], 'observation': f'unassigned-{i}'} for i in range(12)])
    monkeypatch.setitem(dashboard._state['rca_runs'], record['id'], record)
    model = build_investigation(record)
    assert browse(model)['coverage'] == {'unbound_observations': 13, 'tool_executions': 0}
    def page(offset, revision):
        return asyncio.run(dashboard.rca_ontology_collection(record['id'], 'observations', revision=revision,
                           unbound_only=True, offset=offset, limit=8))
    first, second = page(0, model['revision']), page(8, model['revision'])
    assert first['total'] == second['total'] == 13
    expected = {o['id'] for o in model['observations'] if o['entity_id'] is None}
    assert [len(first['items']), len(second['items'])] == [8, 5]
    assert {o['id'] for o in first['items'] + second['items']} == expected
    source[:] = [source[-1]]
    with pytest.raises(HTTPException) as exc:
        page(0, model['revision'])
    assert exc.value.status_code == 409
    empty = build_investigation(record)
    assert browse(empty)['focus'] is None and page(0, empty['revision'])['total'] == 1


def test_endpoint_variants_and_their_support_survive_browser_projection_and_native_readback(tmp_path):
    model = build_investigation(investigation())
    first, second, third = [o['id'] for o in model['observations'][:3]]
    pair = model['relations'][0]
    claims = [
        (['192.0.2.1'], True, [first, second]),
        (['192.0.2.2'], None, [third]),
        (['192.0.2.1'], True, [third]),
    ]
    model['relations'] = [{**pair, 'type': 'endpoint_targets_pod', 'addresses': addresses,
        'endpoint_conditions': {'ready': ready}, 'evidence_id': refs[0], 'evidence_ids': refs,
        'publish_not_ready_addresses': False, 'snapshot_consistency': 'not_atomic',
        'readiness_is_health_proof': False} for addresses, ready, refs in claims]
    neighborhood = browse(model, focus_id=pair['source_id'])
    edge = neighborhood['edges'][0]
    assert neighborhood['counts']['edges'] == 1
    assert set(neighborhood['evidence_objects']) == {first, second, third}
    path = tmp_path / 'conditions.json'
    path.write_text(json.dumps(export_investigation(model)))
    native = next(e for e in GraphSession.from_file(str(path)).graph.to_dict()['edges']
                  if e['type'] == 'endpoint_targets_pod')['metadata']
    assert edge['assertions'] == native['assertions']
    assert set(native['evidence_ids']) == {first, second, third}
    by_address = {a['attributes']['addresses'][0]: a for a in edge['assertions']}
    assert len(by_address) == 2
    assert set(by_address['192.0.2.1']['evidence_ids']) == {first, second, third}
    assert by_address['192.0.2.1']['attributes']['endpoint_conditions']['ready'] is True
    variant = by_address['192.0.2.2']
    assert variant['evidence_ids'] == [third] and variant['attributes']['endpoint_conditions']['ready'] is None
    assert variant['attributes']['publish_not_ready_addresses'] is False
    assert variant['attributes']['readiness_is_health_proof'] is False
