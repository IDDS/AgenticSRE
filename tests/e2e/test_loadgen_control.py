#!/usr/bin/env python3
"""Regression coverage for independently controllable load tasks."""

from __future__ import annotations

import json
import pathlib
import sys

from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import web_app.app as app_module  # noqa: E402
from web_app.app import (  # noqa: E402
    _ai_loadgen_targets,
    _build_http_k8s_loadgen_plan,
    _build_vllm_k8s_loadgen_plan,
    _discover_loadgen_benchmarks,
    _load_loadgen_benchmarks,
    _loadgen_pod_records,
    _select_ai_loadgen_target,
    app,
)


def test_general_http_loadgen_plan_uses_selected_service_and_unique_run() -> None:
    plan = _build_http_k8s_loadgen_plan(
        {
            "method": "POST",
            "path": "/api/orders",
            "body": '{"count": 1}',
            "duration_s": 90,
            "concurrency": 5,
            "qps": 2.5,
        },
        cluster_id="general-a",
        namespace="shop",
        service="frontend-api",
        port=8080,
    )

    commands = "\n".join(plan["commands"])
    assert plan["endpoint"] == "http://frontend-api.shop.svc.cluster.local:8080/api/orders"
    assert plan["run_id"].startswith("http-")
    assert "nginx-thrift" not in commands
    assert "social-network" not in commands
    assert f"agenticsre.io/run-id={plan['run_id']}" in commands
    assert plan["cleanup_commands"] == [
        command for command in plan["cleanup_commands"]
        if f"agenticsre.io/run-id={plan['run_id']}" in command
    ]


def test_vllm_loadgen_plan_supports_exact_run_stop() -> None:
    plan = _build_vllm_k8s_loadgen_plan(
        {
            "service": "model-api",
            "model": "model-a",
            "run_id": "vllm-run-7",
        },
        cluster_id="ai-a",
        namespace="inference",
        deployment="model-api",
    )

    assert plan["run_id"] == "vllm-run-7"
    assert all("agenticsre.io/run-id=vllm-run-7" in command for command in plan["cleanup_commands"])


def test_loadgen_status_records_preserve_run_identity() -> None:
    records = _loadgen_pod_records({
        "items": [{
            "metadata": {
                "name": "agenticsre-http-load-http-123",
                "namespace": "demo",
                "creationTimestamp": "2026-07-11T00:00:00Z",
                "labels": {
                    "agenticsre.io/run-id": "http-123",
                    "agenticsre.io/service": "frontend",
                },
            },
            "spec": {"nodeName": "worker-1"},
            "status": {
                "phase": "Running",
                "startTime": "2026-07-11T00:00:01Z",
                "containerStatuses": [{"state": {"running": {}}}],
            },
        }]
    })

    assert records == [{
        "run_id": "http-123",
        "name": "agenticsre-http-load-http-123",
        "namespace": "demo",
        "service": "frontend",
        "deployment": "",
        "benchmark_id": "",
        "scenario_id": "",
        "model": "",
        "phase": "Running",
        "state": "",
        "started_at": "2026-07-11T00:00:01Z",
        "node": "worker-1",
    }]


def test_general_loadgen_start_and_stop_endpoints_are_run_scoped(monkeypatch) -> None:
    monkeypatch.setattr(app_module, "_find_k8s_cluster", lambda cluster_id: {
        "id": cluster_id,
        "name": "General",
        "kind": "general",
        "default_namespace": "demo",
    })
    commands = []

    def fake_shell(command, *_args, **_kwargs):
        commands.append(command)
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": "", "command": command}

    monkeypatch.setattr(app_module, "_shell_sync", fake_shell)
    async def fake_benchmarks(_cluster_id):
        return [{
            "id": "otel-demo",
            "name": "OTel Demo",
            "available": True,
            "namespace": "demo",
            "service": "frontend",
            "port": 8080,
            "trace_backend": "jaeger",
            "trace_service": "frontend",
            "scenarios": [{"id": "browse", "name": "Browse", "method": "GET", "path": "/"}],
        }]

    monkeypatch.setattr(app_module, "_discover_loadgen_benchmarks", fake_benchmarks)
    client = TestClient(app)
    started = client.post("/api/loadgen/general/general-a", json={
        "benchmark_id": "otel-demo",
        "scenario_id": "browse",
        "duration_s": 60,
    })
    assert started.status_code == 200, started.text
    payload = started.json()
    assert payload["status"] == "started"
    run_id = payload["loadgen"]["run_id"]

    stopped = client.delete(f"/api/loadgen/general/general-a/{run_id}?namespace=demo")
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["status"] == "stopped"
    assert f"agenticsre.io/run-id={run_id}" in commands[-1]


def test_general_loadgen_status_endpoint_reads_cluster_pods(monkeypatch) -> None:
    monkeypatch.setattr(app_module, "_find_k8s_cluster", lambda cluster_id: {
        "id": cluster_id,
        "kind": "general",
        "default_namespace": "demo",
    })
    pod_json = {"items": [{
        "metadata": {"name": "load-a", "namespace": "demo", "labels": {"agenticsre.io/run-id": "http-a"}},
        "status": {"phase": "Running"},
    }]}
    monkeypatch.setattr(app_module, "_shell_sync", lambda *_args, **_kwargs: {
        "ok": True,
        "stdout": json.dumps(pod_json),
        "stderr": "",
    })

    response = TestClient(app).get("/api/loadgen/general/general-a?namespace=demo")
    assert response.status_code == 200, response.text
    assert response.json()["runs"][0]["run_id"] == "http-a"


def test_benchmark_catalog_is_config_driven_and_discovered_from_services(monkeypatch) -> None:
    configured = _load_loadgen_benchmarks()
    assert {item["id"] for item in configured} >= {"opentelemetry-demo", "train-ticket", "social-network"}

    service_items = [
        {"metadata": {"namespace": "default", "name": name}}
        for name in ("frontend", "productcatalogservice", "recommendationservice")
    ]

    async def fake_kubectl_json(*_args, **_kwargs):
        return {"items": service_items}

    monkeypatch.setattr(app_module, "_kubectl_json", fake_kubectl_json)
    discovered = __import__("asyncio").run(_discover_loadgen_benchmarks("general-a"))
    by_id = {item["id"]: item for item in discovered}
    assert by_id["opentelemetry-demo"]["available"] is True
    assert by_id["train-ticket"]["available"] is False
    assert by_id["opentelemetry-demo"]["scenarios"][0]["id"] == "browse-home"


def test_loadgen_frontend_has_independent_start_stop_controls() -> None:
    html = (ROOT / "web_app" / "templates" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web_app" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    assert 'data-view="loadgen"' in html
    assert 'id="view-loadgen"' in html
    assert 'id="loadgen-general-benchmark"' in html
    assert 'id="loadgen-general-scenario"' in html
    assert "startGeneralLoadgen()" in html
    assert "startAILoadgen()" in html
    assert "async function stopGeneralLoadgen" in script
    assert "async function stopAILoadgen" in script


def test_skywalking_trace_apis_normalize_services_and_spans(monkeypatch) -> None:
    def fake_graphql(query, variables=None, *, cluster_id=""):
        assert cluster_id == "general-a"
        if "listServices" in query:
            return {
                "data": {"listServices": [{"id": "frontend-id", "name": "frontend::default"}]},
                "_selected_url": "http://worker:30002",
            }
        if "queryBasicTraces" in query:
            assert variables["condition"]["serviceId"] == "frontend-id"
            return {
                "data": {"queryBasicTraces": {"traces": [{
                    "segmentId": "segment-a",
                    "endpointNames": ["GET:/"],
                    "duration": 130,
                    "start": "1783782283582",
                    "isError": False,
                    "traceIds": ["trace-a"],
                }]}},
                "_selected_url": "http://worker:30002",
            }
        return {
            "data": {"queryTrace": {"spans": [{
                "traceId": "trace-a",
                "segmentId": "segment-a",
                "spanId": 0,
                "parentSpanId": -1,
                "serviceCode": "frontend::default",
                "startTime": "1783782283582",
                "endTime": "1783782283712",
                "endpointName": "GET:/",
                "isError": False,
            }]}},
            "_selected_url": "http://worker:30002",
        }

    monkeypatch.setattr(app_module, "_skywalking_graphql", fake_graphql)
    client = TestClient(app)
    services = client.get("/api/skywalking/services?cluster_id=general-a").json()
    assert services["services"][0]["name"] == "frontend::default"

    traces = client.get(
        "/api/skywalking/traces?cluster_id=general-a&service_id=frontend-id&service_name=frontend::default"
    ).json()
    assert traces["traces"][0]["traceID"] == "trace-a"
    assert traces["traces"][0]["total_duration_us"] == 130000

    detail = client.get("/api/skywalking/trace/trace-a?cluster_id=general-a").json()
    assert detail["span_count"] == 1
    assert detail["spans"][0]["serviceName"] == "frontend::default"
    assert detail["spans"][0]["duration_us"] == 130000


def test_trace_frontend_supports_skywalking_and_cluster_selection() -> None:
    html = (ROOT / "web_app" / "templates" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web_app" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    assert 'id="trace-cluster"' in html
    assert 'id="trace-source"' in html
    assert "/api/skywalking/services" in script
    assert "/api/skywalking/traces" in script


def test_ai_loadgen_targets_bind_service_to_runtime_models(monkeypatch) -> None:
    inventory = {
        "pods": [
            {
                "name": "vllm-small-a",
                "namespace": "llm-k8s",
                "node": "gpu-1",
                "phase": "Running",
                "ready": 1,
                "labels": {"app": "vllm-small"},
                "served_models": ["model-small"],
            },
            {
                "name": "vllm-large-a",
                "namespace": "llm-k8s",
                "node": "gpu-2",
                "phase": "Running",
                "ready": 1,
                "labels": {"app": "vllm-large"},
                "served_models": ["model-large"],
            },
        ],
        "services": [
            {
                "name": "vllm-small",
                "namespace": "llm-k8s",
                "selector": {"app": "vllm-small"},
                "port_records": [{"name": "http", "port": 8000}],
            },
            {
                "name": "vllm-large",
                "namespace": "llm-k8s",
                "selector": {"app": "vllm-large"},
                "port_records": [{"name": "http", "port": 8000}],
            },
        ],
    }

    async def fake_inventory(_cluster):
        return inventory

    monkeypatch.setattr(app_module, "_collect_ai_k8s_inventory", fake_inventory)
    monkeypatch.setattr(
        app_module,
        "_vllm_service_models_via_api",
        lambda _cluster_id, _namespace, service, _port, _port_name="": [
            "model-small" if service == "vllm-small" else "model-large"
        ],
    )
    targets = __import__("asyncio").run(_ai_loadgen_targets({
        "id": "ai-a",
        "kind": "ai",
        "model": "model-small",
        "default_namespace": "llm-k8s",
    }))

    assert [item["service"] for item in targets] == ["vllm-small", "vllm-large"]
    assert targets[0]["models"] == ["model-small"]
    assert targets[1]["models"] == ["model-large"]
    selected = _select_ai_loadgen_target(targets, service="vllm-large", model="model-large")
    assert selected["model"] == "model-large"


def test_ai_loadgen_rejects_model_service_mismatch() -> None:
    targets = [{"service": "vllm-small", "models": ["model-small"], "default_model": "model-small"}]
    try:
        _select_ai_loadgen_target(targets, service="vllm-small", model="model-large")
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 409
        assert "not served" in str(getattr(exc, "detail", exc))
    else:
        raise AssertionError("mismatched service/model selection must be rejected")


def test_ai_loadgen_frontend_exposes_service_and_model_selectors() -> None:
    html = (ROOT / "web_app" / "templates" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web_app" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    assert 'id="loadgen-ai-service"' in html
    assert 'id="loadgen-ai-model"' in html
    assert "/loadgen-targets" in script
    assert "populateAILoadgenModels" in script
    start_body = script.split("async function startAILoadgen()", 1)[1].split(
        "async function loadAILoadgenRuns()", 1
    )[0]
    assert "const body = aiIndependentLoadgenBody();" in start_body


def test_ai_dashboard_frontend_exposes_kv_cache_metric() -> None:
    html = (ROOT / "web_app" / "templates" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web_app" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    assert 'id="chart-ai-kv-cache"' in html
    assert "kv_cache: { group: 'llm', canvas: 'chart-ai-kv-cache', unit: '%' }" in script
    assert "aiSummaryValue(s.kv_cache, '%')" in script
