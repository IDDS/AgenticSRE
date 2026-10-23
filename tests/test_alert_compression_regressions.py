"""Regressions from the remote 2026-09-15 compression effectiveness audit."""
import json

import pytest

from agents.alert_compression import CompressionStore, compress_alerts
from agents.alert_semantics import recall_candidates


def job(name="backup", **changes):
    return dict(dict(name="KubeJobFailed", source="prometheus", timestamp=1000,
                     cluster_id="c1", namespace="shop", message="", severity="warning",
                     labels={"job_name": name, "pod": "kube-state-metrics-exporter",
                             "instance": "10.0.0.1:8080", "condition": "true"}), **changes)


def test_failed_jobs_use_target_identity_and_keep_duplicates_compressible():
    records = [job(n, timestamp=1000+i) for n in ("backup", "probe", "verify") for i in range(3)]
    result = compress_alerts(records)
    assert len(result["groups"]) == 3
    assert {(g["entity_kind"], g["entity"], g["alert_count"]) for g in result["groups"]} == {
        ("job", n, 3) for n in ("backup", "probe", "verify")}
    assert [r["raw"] for r in result["raw_alerts"]] == records


def test_job_without_namespace_cannot_merge():
    assert compress_alerts([job(namespace=""), job(namespace="")])["compressed_groups"] == 2


@pytest.mark.parametrize("dimension", ["condition", "container", "reason", "persistentvolumeclaim"])
def test_distinct_observation_dimensions_do_not_merge(dimension):
    records = [job(labels={"pod": "api", dimension: value}) for value in ("a", "b")]
    assert compress_alerts(records)["compressed_groups"] == 2


def test_severity_changes_still_merge():
    a, b = job(), job(timestamp=1001, severity="critical")
    a["labels"]["severity"], b["labels"]["severity"] = "warning", "critical"
    result = compress_alerts([a, b])
    assert result["compressed_groups"] == 1
    assert result["groups"][0]["severity"] == "critical"


def test_compact_dossier_preserves_identity_samples_and_provenance(tmp_path):
    store = CompressionStore(tmp_path)
    scan = store.create(compress_alerts([job()]))
    ref = (scan["scan_id"], scan["groups"][0]["group_id"], scan["revision"])
    compact, full = store.dossier(*ref), store.dossier(*ref, compact=False)
    assert len(json.dumps(compact)) < len(json.dumps(full)) * .75
    for key in ("source", "scan_id", "group_id", "revision", "entity", "entity_kind", "alert_count", "evidence_url"):
        assert compact[key] == full[key]
    assert compact["entity_kind"] == "job"
    assert compact["alert_samples"][0]["labels"]["job_name"] == "backup"
    assert compact["alert_samples"][0]["alert_id"] == scan["raw_alerts"][0]["alert_id"]
    assert store.get(scan["scan_id"])["raw_alerts"] == scan["raw_alerts"]


def test_historical_exporter_group_is_rejected_before_diagnosis(tmp_path):
    store = CompressionStore(tmp_path)
    old = compress_alerts([job()])
    old["groups"][0].update(entity_kind="pod", entity="kube-state-metrics-exporter")
    scan = store.create(old)
    with pytest.raises(ValueError, match="run a new scan"):
        store.dossier(scan["scan_id"], scan["groups"][0]["group_id"], scan["revision"])


def test_old_signature_history_cannot_create_false_frequency_anomalies(tmp_path):
    from agents.alert_history import AlertHistory
    history = AlertHistory(tmp_path / "history.sqlite3")
    scan = compress_alerts([job()])
    scan.update(origin="live_scan", cluster_id="c1", scan_id="new", collection={"prometheus": "ok"})
    old = dict(scan)
    old.pop("signature_version")
    for at in (1000, 1300, 1600):
        history.record(old, "", now=at)
    history.apply(scan, "", now=2000)
    assert scan["automatic_baseline"]["status"] == "warming_up"
    assert scan["automatic_baseline"]["snapshots"] == 0
    assert scan["groups"][0]["frequency"]["status"] == "unknown"
    for at in (2200, 2500, 2800):
        history.record(scan, "", now=at)
    history.apply(scan, "", now=3200)
    assert scan["automatic_baseline"]["status"] == "ready"
    assert scan["groups"][0]["frequency"]["expected_records"] == 1
    assert scan["groups"][0]["frequency"]["deviation_score"] == 0


def test_generic_log_name_does_not_pollute_retrieval_text():
    texts = []
    def encoder(batch):
        texts.extend(batch)
        return [[1, i+1] for i in range(len(batch))]
    result = compress_alerts([job(name="x", message=m, source="logs", labels={"pod": str(i)})
                             for i, m in enumerate(("database connection refused", "backup completed"))])
    # name is an alert category; job_name here deliberately exercises a generic title.
    for g in result["groups"]:
        g["representative"] = "ApplicationLog"
    recall_candidates(result, encoder=encoder)
    assert texts == [g["symbolic"]["template"] for g in result["groups"]]


def test_request_uuid_and_elapsed_ms_compress_without_erasing_meaning():
    records = [job(source="logs", message=f"request {i:08x}-1234-1234-1234-123456789abc failed after {i} ms contacting db")
               for i in range(20)]
    records += [job(source="logs", message=f"timeout waiting {i} milliseconds for database") for i in range(20)]
    result = compress_alerts(records)
    assert sorted(g["alert_count"] for g in result["groups"]) == [20, 20]
    assert [r["raw"] for r in result["raw_alerts"]] == records


def test_opaque_request_hashes_compress():
    assert compress_alerts([job(source="logs", message=f"request {i:024x} failed after {i} ms")
                            for i in range(10)])["compressed_groups"] == 1


@pytest.mark.parametrize("messages", [
    ("GET /orders/1 returned HTTP 503", "GET /orders/2 returned HTTP 503"),
    ("failed after 10 ms HTTP 503", "failed after 20 ms HTTP 404"),
    ("failed after 10 ms", "not failed after 20 ms"),
    ("target 12345678-1234-1234-1234-123456789abc failed", "target 87654321-1234-1234-1234-123456789abc failed"),
    ("version 1 failed after 10 ms", "version 2 failed after 20 ms"),
])
def test_new_template_masks_preserve_targets_codes_versions_and_negation(messages):
    assert compress_alerts([job(source="logs", message=m) for m in messages])["compressed_groups"] == 2
