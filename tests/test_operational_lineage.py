"""Provenance must not confuse name reuse, runtime images or reviewed evidence."""
import asyncio
import copy
import json

import pytest

from operational_lineage import collect_lineage, traceable_object
from operational_ontology import build_investigation


TARGET = {"cluster_id": "east", "namespace": "shop", "kind": "Pod", "name": "worker", "uid": "pod-1"}


def resource(kind="Pod", name="worker", uid="pod-1", owner=None):
    return {"kind": kind, "metadata": {"name": name, "namespace": "shop", "uid": uid,
        "resourceVersion": "123", "annotations": {"deployment.kubernetes.io/revision": "4",
        "kubectl.kubernetes.io/last-applied-configuration": "PRIVATE_CONFIG"},
        "ownerReferences": [owner] if owner else []}, "spec": {"containers": [
        {"name": "app", "image": "registry/app:mutable", "env": [{"value": "PRIVATE_ENV"}]}]},
        "status": {"containerStatuses": [{"name": "app", "image": "registry/app:mutable", "imageID": "containerd://sha256:abc"}]}}


def owner(kind, name, uid):
    return {"kind": kind, "name": name, "uid": uid, "controller": True}


def collect(reader, target=TARGET):
    return collect_lineage(target, reader, source_run_id="run-1", source_revision="rev-1")


def test_uid_verified_chain_and_safe_projection():
    rows = [resource(owner=owner("ReplicaSet", "worker-rs", "rs-1")),
            resource("ReplicaSet", "worker-rs", "rs-1", owner("Deployment", "worker-deploy", "deploy-1")),
            resource("Deployment", "worker-deploy", "deploy-1")]
    reads = []
    def read(target):
        reads.append(dict(target))
        return {"ok": True, "json": rows[len(reads)-1]}
    before = copy.deepcopy(rows)
    result = collect(read)
    assert result["status"] == "complete" and result["release_trace_complete"] is True
    assert len(result["observations"]) == 3 and len(result["relations"]) == 2
    assert all(r["verification"] == "owner_reference_uid_matched" for r in result["relations"])
    assert all(r["namespace"] == "shop" and r["cluster_id"] == "east" for r in reads)
    assert "PRIVATE_" not in json.dumps(result)
    assert result["semantics"]["reviewed_by_original_judge"] is False
    assert result["semantics"]["atomic_multi_resource_snapshot"] is False
    assert rows == before


@pytest.mark.parametrize("change", [{"uid":"pod-new"}, {"namespace":"other"}, {"name":"peer"}])
def test_same_name_recreation_or_scope_mismatch_never_becomes_observed_target(change):
    row = resource()
    row["metadata"].update(change)
    result = collect(lambda t: {"ok":True,"json":row})
    assert result["status"] == "unavailable" and result["observations"] == []
    assert "resource_identity_changed" in result["gaps"]


def test_recreated_owner_does_not_complete_or_link_controller_chain():
    rows = iter([resource(owner=owner("ReplicaSet","worker-rs","rs-old")), resource("ReplicaSet","worker-rs","rs-new")])
    result = collect(lambda t: {"ok":True,"json":next(rows)})
    assert result["status"] == "partial" and len(result["observations"]) == 1
    assert result["relations"] == [] and result["release_trace_complete"] is False
    assert result["observations"][0]["raw_ref"]["metadata"]["ownerReferences"][0]["uid"] == "rs-old"


def test_runtime_image_identity_is_separate_from_declared_reference_and_container_kind():
    row = resource()
    row["spec"]["initContainers"] = [{"name":"app","image":"registry/init:v1"}]
    row["status"]["initContainerStatuses"] = [{"name":"app","imageID":"docker-pullable://init@sha256:def"}]
    values = collect(lambda t:{"ok":True,"json":row})["observations"][0]["raw_ref"]["containers"]
    assert values[0]["declared_image"] == "registry/app:mutable"
    assert values[0]["runtime_image_id"] == "containerd://sha256:abc"
    assert values[1]["runtime_image_id"] == "docker-pullable://init@sha256:def"
    row["status"] = {}
    missing = collect(lambda t:{"ok":True,"json":row})["observations"][0]["raw_ref"]["containers"]
    assert all(c["runtime_image_id"] is None and c["runtime_status"] == "not_reported" for c in missing)


@pytest.mark.parametrize("bad", [{"uid":""}, {"name":"--all"}, {"namespace":"shop;id"}, {"kind":"Secret"}, {"cluster_id":""}])
def test_invalid_or_unsupported_target_cannot_dispatch_reads(bad):
    target = TARGET | bad
    assert not traceable_object(target)
    with pytest.raises(ValueError):
        collect(lambda t: pytest.fail("must not read"), target)


def test_cycle_is_bounded_and_never_claims_complete():
    row = resource(owner=owner("Pod","worker","pod-1"))
    calls = []
    result = collect(lambda t: calls.append(t) or {"ok":True,"json":row})
    assert len(calls) == 1 and "owner_cycle" in result["gaps"]


def test_read_failure_does_not_store_unfiltered_tool_output():
    result = collect(lambda t:{"ok":False,"stdout":"PRIVATE_CONFIG","error":"PRIVATE_ENV"})
    assert result["status"] == "unavailable" and "PRIVATE" not in json.dumps(result)


def test_failed_ancestor_read_retains_previous_verified_observation():
    rows = iter([{"ok":True,"json":resource(owner=owner("ReplicaSet","worker-rs","rs-1"))}, {"ok":False}])
    result = collect(lambda t:next(rows))
    assert len(result["observations"]) == 1 and "resource_read_failed" in result["gaps"]
    assert result["status"] == "partial"


def record():
    return {"id":"rca-lineage-test", "status":"completed", "cluster_id":"east", "namespace":"shop",
        "result":{"observations":[{"signal_type":"pod_waiting_state", "source":"kubectl", "observation":"Pending",
        "raw_ref":{"kind":"Pod","metadata":{"name":"worker","namespace":"shop","uid":"pod-1","resourceVersion":"100"}}}]}}


def setup_api(monkeypatch):
    import web_app.app as app
    run = record()
    monkeypatch.setitem(app._state, "rca_runs", {run["id"]:run})
    monkeypatch.setattr(app,"_save_rca_history",lambda **kw:True)
    monkeypatch.setattr(app,"_ontology_live_resource",lambda t,**kw:{"ok":True,"json":resource()})
    model = build_investigation(run)
    return app, run, {"entity_id":model["objects"][0]["id"],"revision":model["revision"]}


def test_api_saves_append_only_supplement_without_changing_reviewed_model(monkeypatch):
    app, run, body = setup_api(monkeypatch)
    before = build_investigation(run)
    snapshots = [app._collect_recorded_lineage(run["id"],body) for _ in range(2)]
    assert run["lineage_snapshots"] == snapshots and snapshots[0]["id"] != snapshots[1]["id"]
    assert build_investigation(run) == before
    assert asyncio.run(app.rca_lineage(run["id"]))["snapshots"] == snapshots
    assert app._build_report(run)["supplemental_lineage"] == snapshots


@pytest.mark.parametrize("bad", [{"revision":"old"},{"revision":""},{"entity_id":"forged-object"}])
def test_api_rejects_stale_or_forged_target_before_read(monkeypatch,bad):
    app, run, body = setup_api(monkeypatch)
    monkeypatch.setattr(app,"_ontology_live_resource",lambda *a,**k:pytest.fail("must not read"))
    with pytest.raises(app.HTTPException) as exc:
        app._collect_recorded_lineage(run["id"],body | bad)
    assert exc.value.status_code == 409


def test_persistence_failure_does_not_report_success_or_retain_unpersisted_snapshot(monkeypatch):
    app, run, body = setup_api(monkeypatch)
    monkeypatch.setattr(app,"_save_rca_history",lambda **kw:False)
    with pytest.raises(app.HTTPException) as exc:
        app._collect_recorded_lineage(run["id"],body)
    assert exc.value.status_code == 503 and run["lineage_snapshots"] == []
    monkeypatch.setattr(app,"_save_rca_history",lambda **kw:True)
    assert app._collect_recorded_lineage(run["id"],body)["id"]


def test_concurrent_collection_cannot_overwrite_or_dispatch_another_read(monkeypatch):
    app, run, body = setup_api(monkeypatch)
    monkeypatch.setattr(app,"_ontology_live_resource",lambda *a,**k:pytest.fail("must not read"))
    with app._LINEAGE_COLLECTION_LOCK:
        with pytest.raises(app.HTTPException) as exc:
            app._collect_recorded_lineage(run["id"],body)
    assert exc.value.status_code == 409


def test_required_old_run_cannot_be_silently_omitted_from_successful_save(monkeypatch,tmp_path):
    import web_app.app as app
    records = {f"run-{i}":{"id":f"run-{i}","status":"completed","started_at":i} for i in range(201)}
    monkeypatch.setitem(app._state,"rca_runs",records)
    path = tmp_path / "history.json"
    monkeypatch.setattr(app,"_RCA_HISTORY_FILE",path)
    assert app._save_rca_history(required_run_id="run-0") is False
    assert not path.exists()
    assert app._save_rca_history(required_run_id="run-200") is True
    assert len(json.loads(path.read_text())) == 200


@pytest.mark.parametrize("body",[b"{broken",b"[]"])
def test_http_invalid_body_returns_validation_error_without_any_read(monkeypatch,body):
    app, run, _ = setup_api(monkeypatch)
    from fastapi.testclient import TestClient
    monkeypatch.setattr(app,"_ontology_live_resource",lambda *a,**k:pytest.fail("must not read"))
    response = TestClient(app.app).post(f"/api/rca/{run['id']}/lineage",content=body,headers={"content-type":"application/json"})
    assert response.status_code == 422
