import copy
import json
from urllib.parse import parse_qs, urlsplit
import shlex

import pytest
from fastapi.testclient import TestClient

from agents.alert_agent import Alert
from agents.alert_compression import compress_alerts
from agents.alert_sample_window import read_matrix, summarize_window, WindowUnavailable


def series(times, **labels):
    metric = dict(__name__="ALERTS", alertname="Watchdog", alertstate="firing", severity="none")
    return {"metric": dict(metric, **labels), "values": [[t, "1"] for t in times]}


def matrix(*items):
    return {"status": "success", "data": {"resultType": "matrix", "result": list(items)}}


def record(t, **labels):
    data = series([t], **labels)
    return vars(Alert.from_prometheus_sample({"metric": data["metric"], "value": data["values"][0]}, "", "c"))


def test_exact_series_collapses_targetless_observations_and_keeps_critical():
    rows = [record(1000), record(1150), record(1300), record(1301),
            record(1050, severity="critical"), record(1050, trace_id="123"), record(1050, trace_id="456")]
    result = compress_alerts(rows, cluster_id="c")
    assert sorted(sorted(g["alert_indices"]) for g in result["groups"]) == [[0, 1, 2], [3], [4], [5], [6]]
    merged = next(g for g in result["groups"] if g["alert_count"] == 3)
    assert merged["grouping_reason"] == "same_prometheus_series_window" and not merged["entity"]
    assert any(g["severity"] == "critical" for g in result["groups"])
    assert [r["raw"] for r in result["raw_alerts"]] == rows
    assert result["retained_alerts"] == 7


@pytest.mark.parametrize("bad", ["unverified", "other_labels", "bad_value", "bad_time", "other_name",
                                  "resolved", "missing_cluster", "conflicting_uid", "unscoped_pod"])
def test_missing_or_conflicting_evidence_keeps_separate_groups(bad):
    row = record(1000)
    if bad == "unverified": row.pop("raw")
    if bad == "other_labels": row["labels"] = dict(row["labels"], extra="x")
    if bad == "bad_value": row["raw"]["value"][1] = "0"
    if bad == "bad_time": row["timestamp"] = 1001
    if bad == "other_name": row["name"] = "AnotherRule"
    if bad == "resolved": row["state"] = "resolved"
    if bad == "conflicting_uid": row.update(entity_uid="uid-1", object_uid="uid-2")
    if bad == "unscoped_pod": row["labels"]["pod"] = "p"
    result = compress_alerts([row, copy.deepcopy(row)], cluster_id="" if bad == "missing_cluster" else "c")
    assert result["compressed_groups"] == 2


def test_window_rate_and_rule_totals_match_membership():
    payload = matrix(series([701, 900, 1000]), series([800, 900], alertname="Down", pod="api", namespace="shop"))
    report = summarize_window(payload, "c", 1000)
    assert (report["start"], report["end"], report["samples"], report["series"], report["groups"]) == (700, 1000, 5, 2, 2)
    assert report["compression_ratio"] == pytest.approx(.6)
    assert report["rules"] == [{"name":"Watchdog", "samples":3, "groups":1}, {"name":"Down", "samples":2, "groups":1}]
    assert summarize_window(matrix(), "c", 1000)["compression_ratio"] is None


@pytest.mark.parametrize("payload", [
    dict(matrix(), warnings=["partial"]), {"status":"error"}, {"data":{"resultType":"vector","result":[]},"status":"success"},
    matrix(series([800]), series([800])), matrix(series([900, 900])), matrix(series([901, 900])),
    matrix(series([700])), matrix(series([1001])), matrix(series([None])), matrix(series([])), matrix(None),
    matrix(series([900], cluster_id="other")), matrix(series([900], alertstate="pending")),
    matrix(dict(series([900]), histograms=[])), matrix(series([900] * 10001)),
])
def test_invalid_or_partial_denominator_is_never_reported(payload):
    with pytest.raises(WindowUnavailable):
        summarize_window(payload, "c", 1000)


def test_missing_profile_url_reads_only_unique_cluster_service_proxy():
    calls = []
    service = {"metadata":{"namespace":"observe","name":"prometheus-operated"}, "spec":{"ports":[{"name":"http", "port":9090}]}}
    def shell(cmd, timeout):
        calls.append(cmd)
        body = {"items":[service]} if len(calls) == 1 else matrix()
        return {"ok":True,"stdout":json.dumps(body)}
    assert read_matrix("", shell, 1000) == matrix()
    path = urlsplit(shlex.split(calls[-1])[3])
    assert path.path == "/api/v1/namespaces/observe/services/prometheus-operated:http/proxy/api/v1/query"
    assert parse_qs(path.query)["query"] == ['ALERTS{alertstate="firing"}[5m]']
    with pytest.raises(WindowUnavailable):
        read_matrix("", lambda *a: {"ok":True,"stdout":json.dumps({"items":[service, service]})}, 1000)


def test_window_http_entry_uses_selected_profile_and_fails_closed(monkeypatch):
    import agents.alert_sample_window as window
    import web_app.app as webapp
    monkeypatch.setattr(webapp, "_resolve_general_cluster_id", lambda c: c)
    monkeypatch.setattr(webapp, "_find_k8s_cluster", lambda c: {"id":c, "prometheus_url":"http://fixture"})
    def read(url, shell, end):
        assert url == "http://fixture"
        return matrix(series([end-1, end]))
    monkeypatch.setattr(window, "read_matrix", read)
    client = TestClient(webapp.app)
    response = client.get("/api/alerts/compression-window?cluster_id=c")
    assert response.status_code == 200 and response.json()["compression_ratio"] == .5
    monkeypatch.setattr(window, "read_matrix", lambda *a: dict(matrix(), warnings=["partial"]))
    response = client.get("/api/alerts/compression-window?cluster_id=c")
    assert response.status_code == 503 and "compression_ratio" not in response.json()
