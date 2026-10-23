#!/usr/bin/env python3
"""Deterministic labeled replay; run on the remote deployment host.

Truth identifies repeated notifications, not shared root causes. This evaluates
grouping only; it does not measure causal or RCA accuracy on production traffic.
"""
import json
import time
from collections import Counter
from agents.alert_compression import compress_alerts, CompressionStore
from tempfile import TemporaryDirectory


def dataset():
    rows, truth = [], []
    # 12 independent incidents, 100 repeat deliveries each. Changes in scope,
    # object, message, lifecycle, or time identify different notifications.
    for incident in range(12):
        for repeat in range(100):
            rows.append(dict(name="Failed", message="image pull failed" if incident % 2 else "connection refused",
                cluster_id="c"+str(incident // 6), namespace="ns"+str(incident % 3),
                labels={"pod": "worker-"+str(incident % 2)}, source="k8s_event",
                state="resolved" if incident % 4 == 0 else "firing",
                timestamp=1000+repeat, severity="critical" if repeat == 55 else "warning"))
            truth.append(str(incident))
    # A rare new event must survive a storm of repeated warnings.
    for i in range(8):
        rows.append(dict(name="OOMKilled", message="memory limit", cluster_id="c0", namespace="ns0",
            labels={"pod": "rare-"+str(i)}, source="k8s_event", timestamp=1100, severity="critical"))
        truth.append("rare-"+str(i))
    return rows, truth


def main():
    rows, truth = dataset()
    start = time.perf_counter()
    result = compress_alerts(rows)
    elapsed = time.perf_counter()-start
    choose2 = lambda n: n*(n-1)//2
    predicted = sum(choose2(g["alert_count"]) for g in result["groups"])
    true_positive = sum(sum(choose2(n) for n in Counter(truth[i] for i in g["alert_indices"]).values()) for g in result["groups"])
    actual = sum(choose2(n) for n in Counter(truth).values())
    with TemporaryDirectory() as directory:
        store = CompressionStore(directory)
        scan = store.create(result)
        group = max(scan["groups"], key=lambda g: g["alert_count"])
        dossier = store.dossier(scan["scan_id"], group["group_id"], 1)
        raw_chars = len(json.dumps([rows[i] for i in group["alert_indices"]], ensure_ascii=False))
        dossier_chars = len(json.dumps(dossier, ensure_ascii=False))
    report = dict(dataset="controlled repeated-notification replay; not production accuracy",
        total_alerts=len(rows), groups=result["compressed_groups"], compression_ratio=result["compression_ratio"],
        pairwise_precision=true_positive/predicted, pairwise_recall=true_positive/actual,
        false_merge_pairs=predicted-true_positive, omitted_alerts=result["omitted_alerts"],
        critical_retained=sum(a["severity"] == "critical" for a in result["raw_alerts"]),
        critical_input=sum(a["severity"] == "critical" for a in rows), elapsed_ms=round(elapsed*1000, 2),
        compression_model_calls=result["semantic"]["model_calls"],
        sample_group_raw_chars=raw_chars, sample_dossier_chars=dossier_chars,
        sample_context_reduction=1-dossier_chars/raw_chars)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    assert report["false_merge_pairs"] == 0 and report["omitted_alerts"] == 0
    assert report["pairwise_recall"] == 1 and report["critical_retained"] == report["critical_input"]


if __name__ == "__main__":
    main()
