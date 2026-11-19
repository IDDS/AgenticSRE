import asyncio
from types import SimpleNamespace

import pytest

from agents.alert_agent import AlertAgent
from agents.alert_history import AlertHistory
from agents.alert_lifecycle import AlertLifecycle
from tools.observability import PrometheusTool


def sample(stamp=1000.125, **labels):
    metric = dict(__name__="ALERTS", alertname="Latency", alertstate="firing",
                  namespace="shop", pod="checkout")
    return {"metric": dict(metric, **labels), "value": [stamp, "1"]}


def vector(rows, **annotations):
    return dict(status="success", data={"resultType": "vector", "result": rows}, **annotations)


def prometheus_tool(body):
    prom = PrometheusTool("" if body is None else "http://fixture.invalid")
    prom._ensure_reachable = lambda: None
    prom.session.get = lambda *a, **kw: SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)
    return prom


def collect(body, events=(), namespace=""):
    prom = prometheus_tool(body)
    kubectl = SimpleNamespace(execute=lambda **kw: SimpleNamespace(success=True, data={"items": list(events)}))
    cfg = SimpleNamespace(kubernetes=SimpleNamespace(cluster_id="c1"),
                          alert=SimpleNamespace(time_window=300, similarity_threshold=.8, max_group_size=100))
    agent = AlertAgent(None, {"prometheus": prom, "kubectl": kubectl}, cfg)
    return asyncio.run(agent.compress_and_recommend(namespace=namespace, enrich=False))


def test_group_window_and_raw_evidence_use_the_vector_observation_time():
    rows = [sample(t) for t in (1000.125, 1010.25, 2000.5)]
    scan = collect(vector(rows))
    assert [r["timestamp"] for r in scan["raw_alerts"]] == [r["value"][0] for r in rows]
    assert [r["raw"]["raw"] for r in scan["raw_alerts"]] == rows
    assert sorted(g["alert_indices"] for g in scan["groups"]) == [[0, 1], [2]]
    assert scan["collection_version"] == 3


@pytest.mark.parametrize("bad", [
    None, {}, {"metric": [], "value": [1000, "1"]}, sample(alertname=" "), sample(alertstate="pending"),
    sample(__name__="up"), sample(severity={}), sample(namespace="elsewhere"), sample(cluster_id="other"),
    dict(sample(), value=[1000]), dict(sample(), value=[1000, "0"]),
    dict(sample(), value=[1000, True]), dict(sample(), value=[1000, "Inf"]),
])
def test_bad_row_preserves_later_critical_and_independent_event(bad):
    first, last = sample(), sample(1010, alertname="DatabaseDown", severity="critical")
    event = dict(reason="FailedScheduling", type="Warning", involvedObject={"name": "api", "namespace": "shop"})
    scan = collect(vector([first, bad, last]), [event], namespace="shop")
    assert [r["raw"]["raw"] for r in scan["raw_alerts"]] == [first, last, event]
    assert scan["raw_alerts"][1]["severity"] == "critical"
    assert scan["degraded"] and scan["collection"] == {"prometheus": "invalid_response", "kubectl": "ok"}
    assert scan["collection_details"]["prometheus"] == dict(examined=3, retained=2, rejected=1, unknown_time=0)
    assert "跳过了 1 条无效样本" in scan["warning"]


@pytest.mark.parametrize("stamp", [-1, True, None, "NaN"])
def test_firing_series_with_unknown_time_remains_visible_without_merging(stamp):
    bad_clock = sample(stamp, severity="critical")
    rows = [bad_clock, bad_clock, sample(severity="critical")]
    scan = collect(vector(rows))
    assert scan["degraded"] and scan["collection"]["prometheus"] == "invalid_response"
    assert [r["raw"]["raw"] for r in scan["raw_alerts"]] == rows
    assert [r["timestamp"] for r in scan["raw_alerts"]] == [None, None, rows[-1]["value"][0]]
    assert sorted(g["alert_indices"] for g in scan["groups"]) == [[0], [1], [2]]
    assert all(g["severity"] == "critical" for g in scan["groups"])
    assert scan["collection_details"]["prometheus"] == dict(examined=3, retained=3, rejected=0, unknown_time=2)
    assert "2 条告警缺少有效观测时间" in scan["warning"]


@pytest.mark.parametrize("body", [
    None, {"status": "success", "data": {}},
    {"status": "success", "data": {"result": []}},
    {"status": "success", "data": {"resultType": "matrix", "result": []}},
    {"status": "success", "data": {"resultType": "scalar", "result": [1000, "1"]}},
    vector([], warnings=["partial response"]),
], ids=["unconfigured", "missing-result", "missing-type", "matrix", "scalar", "upstream-warning"])
def test_incomplete_observations_cannot_resolve_incidents_or_train_frequency(tmp_path, body):
    lifecycle = AlertLifecycle(tmp_path / "lifecycle.sqlite3")
    history = AlertHistory(tmp_path / "history.sqlite3")

    def observe(response, at):
        scan = collect(response)
        scan.update(scan_id=str(at), revision=1, collection_started_at=at, origin="live_scan", cluster_id="c1")
        history.record(scan, "", now=at)
        return lifecycle.observe(scan)

    assert observe(vector([sample()]), 1000)["lifecycle"]["status"] == "ok"
    for at in (1300, 1600):
        assert observe(body, at)["lifecycle"]["status"] == "ineligible"
    assert lifecycle.list("c1")[0]["status"] == "active"
    empty = collect(vector([], infos=["query annotation"]))
    empty.update(origin="live_scan", cluster_id="c1")
    baseline = history.apply(empty, "", now=1900)["automatic_baseline"]
    assert baseline["snapshots"] == 1
    for at in (1900, 2200):
        assert observe(vector([]), at)["lifecycle"]["status"] == "ok"
    assert lifecycle.list("c1")[0]["status"] == "resolved"


def test_upstream_warning_keeps_returned_alerts_visible():
    rows = [sample(severity="critical")]
    scan = collect(vector(rows, warnings=["partial response"]))
    assert [r["raw"]["raw"] for r in scan["raw_alerts"]] == rows
    assert scan["degraded"] and scan["collection"]["prometheus"] == "partial"


@pytest.mark.parametrize("kind,rows", [
    ("matrix", [{"metric": {"job": "api"}, "values": [[1000, "3"], [1060, "4"]]}]),
    ("scalar", [1000, "42"]), ("string", [1000, "ready"]),
])
def test_generic_queries_preserve_non_alert_result_types(kind, rows):
    body = {"status": "success", "data": {"resultType": kind, "result": rows}}
    result = prometheus_tool(body).execute(query="fixture", query_type="range" if kind == "matrix" else "instant")
    assert result.success and result.data["results"] == rows and result.data["result_type"] == kind
