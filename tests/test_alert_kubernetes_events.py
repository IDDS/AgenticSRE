import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agents.alert_agent import AlertAgent
from agents.alert_history import AlertHistory
from skill_runtime.kubectl_safety import readonly_kubectl_args


def event(modern=False, *, field_path="spec.containers{app}", uid="pod-1", **fields):
    target = dict(kind="Pod", name="api", namespace="shop", uid=uid, fieldPath=field_path)
    data = dict(type="Warning", reason="Unhealthy", eventTime="2026-10-01T00:00:00Z",
                metadata={"namespace": "shop", "uid": "event-1"})
    if modern:
        data.update(apiVersion="events.k8s.io/v1", regarding=target, note="probe failed")
    else:
        data.update(apiVersion="v1", involvedObject=target, message="probe failed")
    return dict(data, **fields)


def collector(events, commands=None):
    def kubectl(command, namespace):
        if commands is not None:
            commands.append(readonly_kubectl_args(command, namespace or "default"))
        return SimpleNamespace(success=True, data={"items": events})

    prom = SimpleNamespace(execute=lambda **kw: SimpleNamespace(success=True, data={"results": []}))
    registry = {"prometheus": prom, "kubectl": SimpleNamespace(execute=kubectl)}
    cfg = SimpleNamespace(kubernetes=SimpleNamespace(cluster_id="c1"),
                          alert=SimpleNamespace(time_window=300, similarity_threshold=.8, max_group_size=100))
    return AlertAgent(None, registry, cfg)


@pytest.mark.parametrize("scope,expected", [("", "--all-namespaces"), ("shop", "shop")])
def test_warning_query_matches_selected_scope(scope, expected):
    calls = []
    asyncio.run(collector([], calls)._collect_alerts(scope))
    (args,) = calls
    assert expected in args and "type=Warning" in args
    assert ("--all-namespaces" in args) == (not scope)
    assert "default" not in args


def test_event_apis_share_a_window_only_for_the_same_target_subobject_and_message():
    series = {"count": 37, "lastObservedTime": "2026-10-01T01:00:00Z"}
    events = [
        event(series=series), event(True, series=series),
        event(series=series, field_path="spec.containers{proxy}"),
        event(True, series=series, uid="replacement"),
        event(True, series=series, note="readiness probe timed out"),
        event(),  # Same target and message one hour before the series update.
    ]
    result = asyncio.run(collector(events).compress_and_recommend(enrich=False))
    assert [r["raw"]["raw"] for r in result["raw_alerts"]] == events
    assert sorted(sorted(g["alert_indices"]) for g in result["groups"]) == [[0, 1], [2], [3], [4], [5]]
    assert result["raw_alerts"][1]["entity_uid"] == "pod-1"
    assert result["raw_alerts"][1]["message"] == "probe failed"
    assert result["retained_alerts"] == len(events)


@pytest.mark.parametrize("fields,minute", [
    ({"series": {"lastObservedTime": "2026-10-01T00:08:00Z"}, "lastTimestamp": "2026-10-01T00:01:00Z"}, 8),
    ({"deprecatedLastTimestamp": "2026-10-01T00:04:00Z"}, 4),
    ({"lastTimestamp": "2026-10-01T00:03:00Z"}, 3),
    ({}, 0),
    ({"eventTime": None, "metadata": {"creationTimestamp": "2026-10-01T00:02:00Z"}}, 2),
])
def test_latest_event_observation_clock(fields, minute):
    alerts = asyncio.run(collector([event(**fields)])._collect_alerts())
    assert alerts[0].timestamp == datetime(2026, 10, 1, 0, minute, tzinfo=timezone.utc).timestamp()


def test_unusable_series_clock_cannot_merge_unrelated_windows():
    events = [event(series={"count": 4, "lastObservedTime": value}) for value in ("invalid", None)]
    result = asyncio.run(collector(events).compress_and_recommend(enrich=False))
    assert result["compressed_groups"] == 2
    assert all(row["timestamp"] is None for row in result["raw_alerts"])


def test_bad_event_does_not_discard_later_valid_warnings():
    result = asyncio.run(collector([None, event()]).compress_and_recommend(enrich=False))
    assert result["raw_alerts"][0]["raw"]["raw"] == event()
    assert result["degraded"] and result["collection"]["kubectl"] == "invalid_response"


def test_scoped_scan_keeps_local_events_if_source_returns_a_foreign_target():
    foreign = event(True)
    foreign["regarding"]["namespace"] = "other"
    result = asyncio.run(collector([foreign, event()]).compress_and_recommend(namespace="shop", enrich=False))
    assert [row["namespace"] for row in result["raw_alerts"]] == ["shop"]
    assert result["degraded"] and result["collection"]["kubectl"] == "invalid_response"


def test_truncated_snapshot_includes_the_newest_observations():
    events = [event()] * 5000 + [event(True, series={"lastObservedTime": "2026-10-02T00:00:00Z"})]
    agent = collector(events)
    alerts = asyncio.run(agent._collect_alerts())
    assert len(alerts) == 5000 and agent.collection_status["kubectl"] == "truncated"
    assert any(alert.raw is events[-1] for alert in alerts)


def test_frequency_baseline_waits_for_three_scans_with_matching_collection_coverage(tmp_path):
    history = AlertHistory(tmp_path / "history.sqlite3")
    scan = asyncio.run(collector([event()]).compress_and_recommend(enrich=False))
    scan.update(origin="live_scan", cluster_id="c1", scan_id="current")
    legacy = dict(scan, scan_id="legacy")
    legacy.pop("collection_version", None)
    for at in (300, 600, 900):
        history.record(legacy, "", now=at)
    baseline = history.apply(scan, "", now=1200)["automatic_baseline"]
    assert baseline["status"] == "warming_up" and baseline["snapshots"] == 0
    for at in (1200, 1500, 1800):
        history.record(scan, "", now=at)
    baseline = history.apply(scan, "", now=2100)["automatic_baseline"]
    assert baseline["status"] == "ready" and baseline["snapshots"] == 3
    assert scan["groups"][0]["frequency"]["expected_records"] == 1
