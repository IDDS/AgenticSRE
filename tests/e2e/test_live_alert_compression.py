#!/usr/bin/env python3
"""Remote opt-in: real image fault, replay amplification, API edits and paired RCA.

Run with AGENTICSRE_RUN_LIVE_DIAGNOSIS=1. Uses an owned disposable namespace.
The 100 repeats are synthetic deliveries of a real observed event, explicitly
not 100 independently observed Kubernetes events. No expected cause in requests.
"""
import importlib.util
import json
import os
from pathlib import Path
import time
import uuid


def main():
    if os.environ.get("AGENTICSRE_RUN_LIVE_DIAGNOSIS") != "1":
        print("SKIP: opt in to remote live validation")
        return
    os.environ["AGENTICSRE_LIVE_DIAG_NS"] = "agenticsre-diag-e2e-compress-"+uuid.uuid4().hex[:6]
    spec = importlib.util.spec_from_file_location("live_helpers", Path(__file__).with_name("test_live_diagnosis_fault_injection.py"))
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)
    output = Path(os.environ.get("AGENTICSRE_COMPRESSION_EVIDENCE", "/tmp/agenticsre-alert-live.json"))
    report = {"status": "running", "namespace": h.NAMESPACE, "amplification": "100 synthetic deliveries of one real event", "runs": {}}
    save = lambda: output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    h.assert_safe_identifiers()
    try:
        h.setup_namespace()
        h.kubectl("-n", h.NAMESPACE, "create", "deployment", h.IMAGE_APP, f"--image={h.MISSING_IMAGE}")
        h.wait_for(h.image_pull_waiting, "real image pull failure", timeout=180, interval=3)
        events = json.loads(h.kubectl("-n", h.NAMESPACE, "get", "events", "--field-selector", "type=Warning", "-o", "json").stdout)["items"]
        event = next(e for e in events if e.get("reason") == "Failed" and "image" in e.get("message", "").lower())
        obj = event["involvedObject"]
        row = dict(name=event["reason"], source="k8s_event", severity="warning", namespace=h.NAMESPACE,
            labels={"kind": obj["kind"], "name": obj["name"]},
            message=event["message"], timestamp=time.time(), raw=event)
        if os.environ.get("AGENTICSRE_CHECK_LIVE_SCAN") == "1":
            collected = h.api("GET", "/api/alerts/scan?namespace="+h.NAMESPACE, timeout=100)
            report["live_scan"] = {k: collected.get(k) for k in ("total_alerts", "compressed_groups", "collection", "semantic", "warning")}
            save()
            assert collected["total_alerts"] > 0
            assert all(a["namespace"] == h.NAMESPACE for a in collected["raw_alerts"])
        rows = [dict(row) for _ in range(100)]
        scan = h.api("POST", "/api/alerts/compress", {"alerts": rows, "namespace": h.NAMESPACE})
        assert scan["total_alerts"] == 100 and scan["compressed_groups"] == 1
        group = scan["groups"][0]
        report["compression"] = {k: scan[k] for k in ("scan_id", "total_alerts", "compressed_groups", "compression_ratio", "retained_alerts", "omitted_alerts")}
        report["observed_event"] = event
        # Real persistence and edit endpoints, including optimistic concurrency.
        edited = h.api("POST", f"/api/alerts/compression/{scan['scan_id']}/edit", dict(
            revision=1, action="split", group_ids=[group["group_id"]], alert_ids=group["alert_ids"][:1], reason="remote acceptance split"))
        assert sorted(g["alert_count"] for g in edited["groups"]) == [1, 99]
        merged = h.api("POST", f"/api/alerts/compression/{scan['scan_id']}/edit", dict(
            revision=2, action="merge", group_ids=[g["group_id"] for g in edited["groups"]], reason="remote acceptance merge"))
        assert merged["raw_alerts"] == scan["raw_alerts"] and merged["revision"] == 3
        ref = dict(scan_id=scan["scan_id"], group_id=merged["groups"][0]["group_id"], revision=3)
        try:
            h.api("POST", "/api/rca/run", {"alert_group": dict(ref, revision=1)})
        except RuntimeError as exc:
            assert "HTTP 409" in str(exc)
        else:
            raise AssertionError("stale dossier accepted")
        try:
            h.api("POST", "/api/rca/run", {"alert_group": ref, "namespace": "default"})
        except RuntimeError as exc:
            assert "HTTP 409" in str(exc)
        else:
            raise AssertionError("cross-scope dossier accepted")
        report["api_edits_and_scope_checks"] = "passed"
        save()
        query = f"请调查 {h.NAMESPACE} 中 pod/{obj['name']} 的告警事件：{row['name']}。根据证据及实时只读观测验证根因。"
        requests = {
            "raw": dict(query=query, namespace=h.NAMESPACE, cluster_id=scan["cluster_id"],
                initial_evidence={"payload": {"source": "raw_alert_replay", "alerts": rows}}),
            "compressed": {"alert_group": ref},
        }
        if os.environ.get("AGENTICSRE_COMPRESSION_ONLY") == "1":
            requests.pop("raw")
        for name, body in requests.items():
            started_at = time.monotonic()
            started = h.api("POST", "/api/rca/run", body, timeout=30)
            report["runs"][name] = {"run_id": started["run_id"], "status": "running"}
            save()
            run = h.poll_rca(started["run_id"])
            view = h.diagnosis_view(run, skill=False)
            report["runs"][name] = dict(view, wall_seconds=round(time.monotonic()-started_at, 2),
                pipeline_counts={k: run.get("result", {}).get(k) for k in ("alerts_compressed", "alert_groups")})
            output.with_name(output.stem+"-"+name+".json").write_text(json.dumps(run, ensure_ascii=False, indent=2))
            save()
            assert run["status"] == "completed"
            h.assert_image_pull_diagnosis(view, skill=False)
            if name == "compressed":
                assert report["runs"][name]["pipeline_counts"] == {"alerts_compressed": 100, "alert_groups": 1}
        report["status"] = "passed"
    except Exception as exc:
        report.update(status="failed", error=str(exc))
        raise
    finally:
        h.cleanup_namespace()
        report["cleanup_verified"] = not h.namespace_exists()
        save()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
