import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.alert_compression import CompressionStore, compress_alerts, propose_relations


def alert(**changes):
    return dict(dict(name="Failed", source="k8s_event", message="image missing", timestamp=1000,
                     cluster_id="c1", namespace="shop", labels={"pod": "api-1"}, severity="warning"), **changes)


def test_partition_preserves_raw_and_critical_transition():
    records = [alert(timestamp=1000+i, severity="critical" if i == 8 else "warning") for i in range(100)]
    result = compress_alerts(records)
    assert result["compressed_groups"] == 1
    assert result["compression_ratio"] == .99
    assert [r["raw"] for r in result["raw_alerts"]] == records
    assert result["groups"][0]["severity"] == "critical"
    assert [s["severity"] for s in result["groups"][0]["severity_changes"]] == ["warning", "critical", "warning"]


@pytest.mark.parametrize("change", [dict(cluster_id="c2"), dict(namespace="other"),
    dict(labels={"pod": "api-2"}), dict(name="BackOff"), dict(message="network failed"),
    dict(state="resolved"), dict(source="prometheus"), dict(timestamp=1301)])
def test_independent_alerts_never_automatically_merge(change):
    assert compress_alerts([alert(), alert(**change)])["compressed_groups"] == 2


@pytest.mark.parametrize("change", [dict(cluster_id=""), dict(namespace=""), dict(labels={}), dict(timestamp=None), dict(timestamp="NaN")])
def test_unknown_identity_or_time_not_merged(change):
    assert compress_alerts([alert(**change), alert(**change)])["compressed_groups"] == 2


def test_window_does_not_chain_and_is_order_independent():
    data = compress_alerts([alert(timestamp=t) for t in [1500, 1000, 1250]])
    assert sorted(g["alert_count"] for g in data["groups"]) == [1, 2]


@pytest.mark.parametrize("change", [dict(cluster_id="c2"), dict(namespace="other")])
def test_requested_scope_is_enforced(change):
    with pytest.raises(ValueError):
        compress_alerts([alert(**change)], cluster_id="c1", namespace="shop")


def test_manual_edits_preserve_members_and_reject_stale_diagnosis(tmp_path):
    store = CompressionStore(tmp_path)
    scan = store.create(compress_alerts([alert(timestamp=1000+i) for i in range(4)]))
    original = copy.deepcopy(scan["raw_alerts"])
    group = scan["groups"][0]
    dossier = store.dossier(scan["scan_id"], group["group_id"], 1)
    assert dossier["alert_count"] == 4
    split = store.edit(scan["scan_id"], revision=1, action="split", group_ids=[group["group_id"]],
                       alert_ids=[group["alert_ids"][1]], reason="independent observation")
    assert sorted(g["alert_count"] for g in split["groups"]) == [1, 3]
    with pytest.raises(ValueError, match="stale"):
        store.dossier(scan["scan_id"], group["group_id"], 1)
    merged = store.edit(scan["scan_id"], revision=2, action="merge",
                        group_ids=[g["group_id"] for g in split["groups"]], reason="verified duplicate")
    assert merged["raw_alerts"] == original
    assert len(merged["history"]) == 2
    assert store.get(scan["scan_id"])["revision"] == 3


def test_manual_cross_scope_merge_rejected(tmp_path):
    store = CompressionStore(tmp_path)
    scan = store.create(compress_alerts([alert(), alert(cluster_id="c2")]))
    with pytest.raises(ValueError, match="different clusters"):
        store.edit(scan["scan_id"], revision=1, action="merge",
                   group_ids=[g["group_id"] for g in scan["groups"]], reason="unsafe")
    assert store.get(scan["scan_id"])["revision"] == 1


@pytest.mark.parametrize("top_level_array", [False, True])
def test_model_cannot_merge_omit_or_fabricate_evidence(top_level_array):
    data = compress_alerts([alert(), alert(name="BackOff")])
    original = copy.deepcopy(data["groups"])
    class Model:
        async def async_json_chat(self, messages, **kwargs):
            pair = json.loads(messages[-1]["content"])[0]
            result = {"relations": [dict(left=pair["left"], right=pair["right"], reason="maybe",
                evidence_alert_ids=[s["alert_id"] for s in pair["samples"]]),
                dict(left="invented", right=pair["right"], evidence_alert_ids=["fake"])]}
            return result["relations"] if top_level_array else result
    asyncio.run(propose_relations(data, Model()))
    assert data["groups"] == original
    assert len(data["relations"]) == 1
    assert data["relations"][0]["status"] == "unverified"
    assert data["semantic"]["model_calls"] == 1


def test_model_timeout_keeps_partition():
    class Slow:
        async def async_json_chat(self, *args, **kwargs):
            await asyncio.sleep(1)
    data = compress_alerts([alert(), alert(name="BackOff")])
    asyncio.run(propose_relations(data, Slow(), timeout_s=.001))
    assert data["semantic"]["status"] == "failed"
    assert data["retained_alerts"] == 2
    assert data["compressed_groups"] == 2


def test_pipeline_consumes_dossier_without_recollecting(monkeypatch):
    import orchestrator.pipeline as module
    pipeline = object.__new__(module.Pipeline)
    pipeline.cfg = SimpleNamespace(pipeline=SimpleNamespace(skip_alert_compression_for_manual_rca=True))
    pipeline.registry = None
    pipeline.validator = SimpleNamespace(validate_pipeline=lambda _: {})
    pipeline._history = []
    rca = AsyncMock(return_value={"status": "completed"})
    monkeypatch.setattr(module, "run_rca", rca)
    payload = dict(source="alert_evidence_dossier", scan_id="scan-x", group_id="g-x", revision=1, alert_count=100)
    result = asyncio.run(pipeline.run("investigate", initial_evidence={"payload": payload}))
    assert result.alerts_compressed == 100
    assert result.alert_groups == 1
    assert rca.call_args.kwargs["initial_evidence"]["payload"] == payload


def test_slow_source_does_not_hide_kubernetes_events():
    import time
    from agents.alert_agent import AlertAgent
    event = {"reason": "Failed", "message": "image pull failed", "lastTimestamp": "2026-09-15T10:00:00Z",
             "involvedObject": {"kind": "Pod", "name": "probe", "namespace": "shop"}}
    class Slow:
        def execute(self, **kwargs):
            time.sleep(.1)
            return SimpleNamespace(success=True, data={"results": []})
    registry = SimpleNamespace(get=lambda name: Slow() if name == "prometheus" else
        SimpleNamespace(execute=lambda **kwargs: SimpleNamespace(success=True, data={"items": [event]})))
    cfg = SimpleNamespace(kubernetes=SimpleNamespace(cluster_id="c1"),
        alert=SimpleNamespace(time_window=300, similarity_threshold=.8, max_group_size=100))
    agent = AlertAgent(None, registry, cfg)
    agent.collection_timeout_s = .01
    result = asyncio.run(agent.compress_and_recommend(namespace="shop"))
    assert result["total_alerts"] == 1
    assert result["raw_alerts"][0]["raw"]["raw"] == event
    assert result["collection"] == {"prometheus": "TimeoutError", "kubectl": "ok"}
    assert result["degraded"] is True
    assert "prometheus" in result["warning"]


def test_unparseable_model_response_is_not_success():
    model = SimpleNamespace(async_json_chat=AsyncMock(return_value={"parse_error": True, "raw_response": "invalid"}))
    data = compress_alerts([alert(), alert(name="BackOff")])
    asyncio.run(propose_relations(data, model))
    assert data["semantic"]["status"] == "failed"
    assert data["relations"] == []
    assert data["retained_alerts"] == 2
