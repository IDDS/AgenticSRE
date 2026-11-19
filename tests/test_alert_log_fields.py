import pytest

from agents.alert_compression import CompressionStore, compress_alerts
from agents.alert_history import AlertHistory
from eval.alert_compression_acceptance import record, score_partition


def labelled_log_fields():
    """External group labels are never passed to the compressor."""
    rows, truth = [], []

    def add(family, message, i, **labels):
        rows.append(record(message=message, timestamp=1700000000+i,
                           severity="critical" if i == 2 else "warning", labels={"pod": "api", **labels}))
        truth.append(family)

    for i in range(8):
        for status in (200, 404, 503):
            add(f"http-{status}", f"GET /search?request_id=req-{i}&status={status}", i)
        for reason in ("timeout", "refused"):
            add(reason, f"span_id=id-{i}&upstream={reason} duration_ms={i+1}", i)
        for value in ("timeout", "not-recorded", "250us", "-1", "1e999", "<VAR>"):
            add("message-" + value, f"request_id=r{i} latency_ms={value}", i)
            add("label-" + value, "gateway latency", i, latency_ms=value)
        add("valid-message", f"request_id=r{i} latency_ms={i}.5", i)
        add("valid-label", "gateway latency", i, latency_ms=i*10)
        add("wait", f"retry after {i+1}e1 ms", i)
    return rows, truth


def test_log_fields_keep_compatible_pairs_and_separate_business_outcomes():
    rows, truth = labelled_log_fields()
    for order in (slice(None), slice(None, None, -1)):
        scores = score_partition(rows[order], truth[order], compress_alerts(rows[order]))
        assert scores["false_merge_pairs"] == scores["missed_compatible_pairs"] == 0
        assert scores["passed"] and scores["groups"] == len(set(truth))


@pytest.mark.parametrize("left,right", [
    ("peer.request_id=a", "peer.request_id=b"),
    ("request_id=a/failed", "request_id=b/recovered"),
    ("duration_ms=1/20", "duration_ms=1/500"),
    ("request_id=<VAR> duration_ms=2", "request_id=a duration_ms=<VAR>"),
    ("request_id=missing", "request_id=abc"),
])
def test_nonvolatile_content_survives_display_template_masking(left, right):
    result = compress_alerts([record(message=m) for m in (left, right)])
    assert result["compressed_groups"] == 2
    assert len({group["template_id"] for group in result["groups"]}) == 2


def test_query_string_durations_have_retrievable_numeric_evidence(tmp_path):
    rows = [record(message=f"GET /?duration_ms={v}&status=503") for v in ("0", "1e2", "4")]
    store = CompressionStore(tmp_path)
    scan = store.create(compress_alerts(rows))
    (group,) = scan["groups"]
    dossier = store.dossier(scan["scan_id"], group["group_id"], 1)
    stats = dossier["log_numeric_summary"]["message.duration_ms"]
    assert (stats["min"], stats["max"], stats["observations"]) == (0, 100, 3)
    assert stats["max_alert_id"] == scan["raw_alerts"][1]["alert_id"]


def test_valid_ids_and_literal_id_labels_follow_different_rules():
    rows = [record(message=f'lookup trace_id="req-{i}"', labels={"pod": "api", "request_id": i})
            for i in range(3)]
    for value in ("missing", "a&status=404", "a&status=503"):
        rows.append(record(message='lookup trace_id="req-4"', labels={"pod": "api", "request_id": value}))
    result = compress_alerts(rows)
    assert sorted(g["alert_count"] for g in result["groups"]) == [1, 1, 1, 3]


def test_sentence_final_request_uuid_keeps_punctuation_and_status():
    def message(i, status):
        return f"HTTP {status}: request {i:08x}-1234-1234-1234-123456789abc."
    result = compress_alerts([record(message=message(i, status)) for i, status in enumerate([503, 503, 200])])
    assert sorted(g["alert_count"] for g in result["groups"]) == [1, 2]


def test_history_excludes_counts_from_older_template_rules(tmp_path):
    scan = compress_alerts([record(message="GET /?request_id=r1&status=503")])
    scan.update(origin="live_scan", collection={"prometheus": "ok"}, cluster_id="c1", scan_id="new")
    old = dict(scan, scan_id="old")
    old.pop("template_version", None)
    history = AlertHistory(tmp_path / "history.db")
    for stamp in (300, 600, 900):
        history.record(old, "", now=stamp)
    assert history.apply(scan, "", now=1200)["automatic_baseline"]["snapshots"] == 0
    for stamp in (1200, 1500, 1800):
        history.record(scan, "", now=stamp)
    assert history.apply(scan, "", now=2100)["automatic_baseline"]["status"] == "ready"
