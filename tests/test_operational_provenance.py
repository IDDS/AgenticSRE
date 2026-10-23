"""Source declarations must not become verified commits or causal changes."""
import asyncio
import copy
import json

import pytest

from operational_provenance import OCI, PROJECTION_VERSION, compare_snapshots, metadata_declarations, latest_comparisons
from operational_lineage import collect_lineage


TARGET = {"cluster_id":"east","namespace":"shop","kind":"Pod","name":"worker","uid":"pod-1"}


def resource(annotations=None, template_annotations=None):
    return {"kind":"Deployment", "metadata":{"annotations":annotations or {}},
            "spec":{"template":{"metadata":{"annotations":template_annotations or {}}}}}


def snapshot(id="before", *, start="2026-10-20T01:00:00Z", end="2026-10-20T01:00:01Z", image="repo/app:v1", runtime="sha256:abc"):
    return {"id":id,"source_run_id":"run-1","target":copy.deepcopy(TARGET),"status":"complete",
        "started_at":start,"finished_at":end,"observations":[{"id":"obs-"+id,"entity":copy.deepcopy(TARGET),
        "resource_version":"opaque-version","raw_ref":{"projection_version":PROJECTION_VERSION,
        "metadata":{"ownerReferences":[]},"controller_revision":"4",
        "source_metadata":{"claims":[]},"containers":[{"container":"app","container_kind":"application",
        "declared_image":image,"runtime_image_id":runtime}]}}]}


def pair():
    return snapshot(), snapshot("after",start="2026-10-20T01:01:00Z",end="2026-10-20T01:01:01Z")


def test_valid_source_pair_is_still_unverified_and_keeps_exact_source_path():
    declarations = metadata_declarations(resource({OCI+"source":"https://github.com/org/repo.git",OCI+"revision":"a"*40}))
    candidate = declarations["code_candidates"][0]
    assert candidate["path"] == "metadata.annotations" and candidate["revision_format"] == "full_hex_identifier"
    assert candidate["verification"] == "declared_unverified" and candidate["image_digest_binding_verified"] is False
    assert declarations["semantics"] == {"git_commit_verified":False,"image_provenance_verified":False,
                                        "gitops_resource_verified":False,"external_requests":0}


def test_source_and_revision_from_different_locations_never_form_code_link():
    result = metadata_declarations(resource({OCI+"source":"https://git.example/org/repo.git"},{OCI+"revision":"a"*40}))
    assert len(result["claims"]) == 2 and result["code_candidates"] == []


def test_conflicting_resource_and_template_declarations_are_preserved_separately():
    result = metadata_declarations(resource({OCI+"source":"https://git.example/org/one",OCI+"revision":"a"*40},
        {OCI+"source":"https://git.example/org/two",OCI+"revision":"b"*40}))
    assert len(result["code_candidates"]) == 2
    assert {c["revision"] for c in result["code_candidates"]} == {"a"*40,"b"*40}
    assert all(c["verification"] == "declared_unverified" for c in result["code_candidates"])


@pytest.mark.parametrize("url",["https://user:PRIVATE_TOKEN@git.example/repo", "https://git.example/repo?token=PRIVATE_TOKEN",
    "https://git.example/repo#PRIVATE_TOKEN", "file:///PRIVATE_TOKEN", "http://git.example/PRIVATE_TOKEN",
    "javascript:PRIVATE_TOKEN", "https://git.example/repo\nPRIVATE_TOKEN", "https://git.example:bad/PRIVATE_TOKEN"])
def test_unsupported_or_credential_bearing_source_urls_are_not_persisted(url):
    result = metadata_declarations(resource({OCI+"source":url}))
    assert result["claims"] == [] and len(result["rejected_fields"]) == 1
    assert "PRIVATE_TOKEN" not in json.dumps(result)


def test_unknown_annotations_and_arbitrary_free_text_do_not_enter_projection():
    result = metadata_declarations(resource({"kubectl.kubernetes.io/last-applied-configuration":"PRIVATE_CONFIG",
        "ci.example/secret":"PRIVATE_CONFIG",OCI+"revision":"PRIVATE_CONFIG\nfree text"}))
    assert "PRIVATE_CONFIG" not in json.dumps(result)


def test_gitops_labels_are_only_declarations_without_resource_links():
    row = resource()
    row["metadata"]["labels"] = {"argocd.argoproj.io/instance":"app-a",
        "kustomize.toolkit.fluxcd.io/name":"overlay-a","app.kubernetes.io/version":"v2"}
    result = metadata_declarations(row)
    assert len(result["claims"]) == 3 and result["code_candidates"] == []
    assert result["semantics"]["gitops_resource_verified"] is False


def test_real_resource_collector_projects_metadata_without_environment_or_arbitrary_annotations():
    row = resource({OCI+"source":"https://git.example/org/repo",OCI+"revision":"a"*40})
    row["kind"] = "Pod"
    row["metadata"].update(name="worker",namespace="shop",uid="pod-1",resourceVersion="1")
    row["spec"]["containers"] = [{"name":"app","image":"repo/app:v1","env":[{"value":"PRIVATE_ENV"}]}]
    trace = collect_lineage(TARGET,lambda t:{"ok":True,"json":row},source_run_id="run-1",source_revision="rev-1")
    projected = trace["observations"][0]["raw_ref"]
    assert projected["projection_version"] == PROJECTION_VERSION
    assert len(projected["source_metadata"]["code_candidates"]) == 1
    assert "PRIVATE_ENV" not in json.dumps(trace) and trace["semantics"]["reviewed_by_original_judge"] is False


def test_image_changes_cite_both_snapshots_but_do_not_prove_cause():
    a,b = pair()
    b["observations"][0]["raw_ref"]["containers"][0]["declared_image"] = "repo/app:v2"
    original = copy.deepcopy([a,b])
    diff = compare_snapshots(a,b)
    assert len(diff["changes"]) == 1
    change = diff["changes"][0]
    assert change["before"] == "repo/app:v1" and change["after"] == "repo/app:v2"
    assert change["evidence_ids"] == ["obs-before","obs-after"] and change["is_causal_proof"] is False
    assert [a,b] == original


def test_missing_runtime_image_is_availability_change_not_proven_image_replacement():
    a,b = pair()
    b["observations"][0]["raw_ref"]["containers"][0]["runtime_image_id"] = None
    diff = compare_snapshots(a,b)
    assert diff["changes"][0]["type"] == "observation_availability_changed"


@pytest.mark.parametrize("field,value",[("uid","new-pod"),("namespace","other"),("cluster_id","west"),("kind","Deployment")])
def test_root_identity_changes_cannot_be_compared_as_same_workload_incarnation(field,value):
    a,b = pair()
    b["target"][field] = value
    with pytest.raises(ValueError,match="different_target_incarnation"):
        compare_snapshots(a,b)


@pytest.mark.parametrize("time",["", "2026-10-20T00:00:00Z", "2026-10-20T01:00:00", "2026-10-20T01:00:00Z"])
def test_unknown_reversed_overlapping_or_naive_time_rejects_comparison(time):
    a,b = pair()
    b["started_at"] = time
    with pytest.raises(ValueError,match="snapshot_time_order_unverified"):
        compare_snapshots(a,b)


def test_different_or_missing_ancestors_are_not_claimed_as_created_or_deleted():
    a,b = pair()
    ancestor = copy.deepcopy(a["observations"][0])
    ancestor["entity"].update(kind="ReplicaSet",name="worker-rs",uid="rs-1")
    ancestor["id"] = "obs-ancestor"
    a["observations"].append(ancestor)
    diff = compare_snapshots(a,b)
    assert diff["unmatched_observations"][0]["meaning"] == "not_observed_in_other_snapshot"
    assert diff["semantics"]["unmatched_means_created_or_deleted"] is False
    assert "snapshot_coverage_differs_or_incomplete" in diff["gaps"]


def test_legacy_metadata_absence_is_not_reported_as_new_source_declaration():
    a,b = pair()
    del a["observations"][0]["raw_ref"]["projection_version"]
    del a["observations"][0]["raw_ref"]["source_metadata"]
    b["observations"][0]["raw_ref"]["source_metadata"] = metadata_declarations(resource({OCI+"revision":"a"*40}))
    diff = compare_snapshots(a,b)
    assert diff["changes"] == [] and "legacy_source_metadata_not_comparable" in diff["gaps"]


def test_resource_version_and_timestamps_alone_do_not_invent_release():
    a,b = pair()
    b["observations"][0]["resource_version"] = "not-a-number"
    b["observations"][0]["observed_at"] = "2026-10-20T01:01:01Z"
    diff = compare_snapshots(a,b)
    assert not diff["changes"] and diff["semantics"]["absence_of_changes_proves_no_release"] is False


def test_comparison_endpoint_reads_saved_snapshots_without_collection_or_mutation(monkeypatch):
    import web_app.app as app
    a,b = pair()
    record = {"id":"run-1","lineage_snapshots":[a,b]}
    monkeypatch.setitem(app._state,"rca_runs",{"run-1":record})
    monkeypatch.setattr(app,"_ontology_live_resource",lambda *a,**k:pytest.fail("must not collect"))
    original = copy.deepcopy(record)
    diff = asyncio.run(app.rca_compare_lineage("run-1","before","after"))
    assert diff["before_id"] == "before" and record == original
    with pytest.raises(app.HTTPException) as missing:
        asyncio.run(app.rca_compare_lineage("run-1","other-run-snapshot","after"))
    assert missing.value.status_code == 404
    with pytest.raises(app.HTTPException) as reversed:
        asyncio.run(app.rca_compare_lineage("run-1","after","before"))
    assert reversed.value.status_code == 409


def test_latest_pairs_do_not_merge_incarnations_or_hide_uncomparable_time():
    a,b = pair()
    other = copy.deepcopy(b)
    other["target"]["uid"] = "new-pod"
    other["id"] = "different-incarnation"
    assert len(latest_comparisons([a,b,other])) == 1
    b["started_at"] = ""
    result = latest_comparisons([a,b])
    assert result[0]["status"] == "not_comparable" and result[0]["reason"] == "snapshot_time_order_unverified"


def test_report_uses_same_latest_pair_as_readonly_compare_endpoint(monkeypatch):
    import web_app.app as app
    a,b = pair()
    b["observations"][0]["raw_ref"]["containers"][0]["declared_image"] = "repo/app:v2"
    record = {"id":"run-1","status":"completed","lineage_snapshots":[a,b]}
    monkeypatch.setitem(app._state,"rca_runs",{"run-1":record})
    result = asyncio.run(app.rca_compare_lineage("run-1","before","after"))
    assert app._build_report(record)["lineage_comparisons"] == [result]
    html = asyncio.run(app.export_report("run-1")).body.decode()
    assert "同一对象最近两次快照比较" in html and "obs-before" in html and "repo/app:v2" in html
