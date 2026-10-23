import copy
import pytest

from agents.alert_compression import compress_alerts, CompressionStore
from agents.alert_semantics import recall_candidates, build_scag, entity_ref


def row(**kw):
    return dict(dict(name="Failure", source="logs", message="connection refused request_id=abc", timestamp=1000,
                     cluster_id="c1", namespace="shop", pod="api", severity="warning"), **kw)


def test_safe_templates_preserve_meaning_and_raw():
    records = [row(message=f"connection refused request_id={i} duration_ms={i}") for i in range(100)]
    records += [row(message="connection accepted request_id=x duration_ms=1", severity="critical")]
    result = compress_alerts(records)
    assert sorted(g["alert_count"] for g in result["groups"]) == [1, 100]
    assert [r["raw"] for r in result["raw_alerts"]] == records
    assert next(g for g in result["groups"] if g["group_id"] == result["ranking"][0])["severity"] == "critical"


@pytest.mark.parametrize("messages", [("HTTP 500", "HTTP 200"), ("version 1 failed", "version 2 failed"),
    ("failed", "not failed"), ("target=db1", "target=db2")])
def test_nonvolatile_differences_not_merged(messages):
    assert compress_alerts([row(message=m) for m in messages])["compressed_groups"] == 2


def metrics():
    return [row(pod=p, source="prometheus", message="latency", metric_name="latency", unit="ms",
                metric_value=v * scale, timestamp=1000+i) for p, scale in [("api", 1), ("db", 2)]
            for i, v in enumerate([1, 2, 10, 3, 4, 5])]


def test_metrics_extrema_and_series_boundaries(tmp_path):
    result = compress_alerts(metrics())
    assert result["compressed_groups"] == 2
    g = result["groups"][0]
    assert g["metric_summary"]["max"] == 10
    store = CompressionStore(tmp_path)
    scan = store.create(result)
    dossier = store.dossier(scan["scan_id"], g["group_id"], 1)
    assert g["metric_summary"]["max_alert_id"] in [a["alert_id"] for a in dossier["alert_samples"]]
    assert compress_alerts([row(metric_name="x", metric_value=1, unit=u) for u in ["ms", "s"]])["compressed_groups"] == 2


def test_baseline_is_explicit_and_validated():
    baseline = {"alerts": [row(timestamp=800)], "end_time": 900, "duration_s": 300}
    result = compress_alerts([row(timestamp=1000+i) for i in range(10)], baseline=baseline)
    assert result["groups"][0]["frequency"]["deviation_score"] == 9
    assert result["baseline_evidence"] == baseline
    assert compress_alerts([row()])["groups"][0]["frequency"]["status"] == "unknown"
    with pytest.raises(ValueError):
        compress_alerts([row()], baseline=dict(baseline, end_time=1100))


def test_retrieval_scope_caps_and_explicit_fallback():
    result = compress_alerts([row(pod=f"pod-{i}", cluster_id="c2" if i == 4 else "c1") for i in range(5)])
    recall_candidates(result, encoder=lambda texts: [[1, 0] for _ in texts], top_n=1, max_pairs=2)
    assert len(result["candidate_pairs"]) == 2
    groups = {g["group_id"]: g for g in result["groups"]}
    assert all(groups[p["left"]]["cluster_id"] == groups[p["right"]]["cluster_id"] for p in result["candidate_pairs"])
    recall_candidates(result, encoder=lambda _: (_ for _ in ()).throw(RuntimeError()), max_groups=2)
    assert result["retrieval"]["status"] == "lexical_fallback"
    assert result["retrieval"]["truncated"] is True


def graph():
    result = compress_alerts(metrics())
    a, b = result["groups"]
    result["candidate_pairs"] = [{"left": a["group_id"], "right": b["group_id"]}]
    topology = {"observed_at": 2000, "ttl_s": 300, "provenance": "test_fixture",
                "edges": [{"source": entity_ref("c1", "shop", "pod", "api"),
                           "target": entity_ref("c1", "shop", "pod", "db")}]}
    return result, topology


def test_scag_supported_association_is_never_causal():
    result, topology = graph()
    build_scag(result, topology, as_of=2001)
    edge = result["scag"]["edges"][0]
    assert edge["status"] == "evidence_supported"
    assert edge["causal_confirmed"] is False
    build_scag(result, topology, as_of=2301)
    assert result["scag"]["edges"][0]["status"] == "unverified"


def test_scag_cannot_follow_cross_cluster_paths():
    result, topology = graph()
    original = topology["edges"][0]
    topology["edges"] = [{"source": original["source"], "target": "c2|shop|pod|other"},
                         {"source": "c2|shop|pod|other", "target": original["target"]}]
    build_scag(result, topology, as_of=2001)
    assert result["scag"]["edges"][0]["checks"]["structure"]["status"] == "unknown"


def test_shared_dependency_and_directed_time_checks():
    result, topology = graph()
    edge = topology["edges"][0]
    topology["edges"] = [{"source": "c1||node|worker", "target": endpoint} for endpoint in edge.values()]
    pair = result["candidate_pairs"][0]
    result["relations"] = [dict(pair, type="shared_dependency")]
    build_scag(result, topology, as_of=2001)
    assert result["scag"]["edges"][0]["checks"]["structure"]["reason"] == "observed common dependency"
    result["relations"] = [dict(pair, type="propagates_to")]
    build_scag(result, topology, as_of=2001)
    assert result["scag"]["edges"][0]["status"] == "contradicted"
    assert result["scag"]["edges"][0]["checks"]["time"]["status"] == "fail"


def test_timeout_does_not_allow_late_mutation(monkeypatch):
    import asyncio
    import time
    import agents.alert_semantics as module
    original = module.recall_candidates
    def delayed(result, **kw):
        if "encoder" not in kw:
            time.sleep(.05)
            result["late_mutation"] = True
        return original(result, encoder=lambda _: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(module, "recall_candidates", delayed)
    result = compress_alerts([row()])
    asyncio.run(module.enrich_retrieval(result, timeout_s=.001))
    time.sleep(.1)
    assert "late_mutation" not in result
    assert result["retrieval"]["status"] == "lexical_fallback"


@pytest.mark.parametrize("model,override", [("deepseek-v4-pro", True), ("other-provider", False)])
def test_direct_classification_override_is_per_call(model, override):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from tools.llm_client import LLMClient
    llm = object.__new__(LLMClient)
    llm.model, llm.temperature, llm.max_tokens = model, .1, 2048
    create = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{}'))], usage=None))
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    llm.json_chat([], thinking=False)
    assert ("extra_body" in create.call_args.kwargs) is override
    llm.json_chat([])
    assert "extra_body" not in create.call_args.kwargs
    assert "response_format" not in create.call_args.kwargs


def test_scag_negative_statistics_and_expired_dossier(tmp_path):
    result, topology = graph()
    build_scag(result, topology, as_of=2001)
    store = CompressionStore(tmp_path)
    scan = store.create(result)
    assert store.dossier(scan["scan_id"], scan["groups"][0]["group_id"], 1)["association_candidates"][0]["status"] == "unverified"
    for i in result["groups"][1]["alert_indices"]:
        result["raw_alerts"][i]["raw"]["metric_value"] *= -1
    build_scag(result, topology, as_of=2001)
    assert result["scag"]["edges"][0]["status"] == "contradicted"
