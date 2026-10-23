#!/usr/bin/env python3
"""Regression tests for vLLM fault lab execution approvals.

These checks cover the 2026-06-25 UI feedback: real fault actions must carry
the explicit confirmation fields that the backend requires, while dry-run
requests remain usable without touching the target cluster.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import pathlib
import re
import sys
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import web_app.app as app_module  # noqa: E402
import scripts.run_ai_fault_batch as fault_batch  # noqa: E402
from web_app.app import (  # noqa: E402
    _apply_cluster_to_cmd,
    _active_fault_runs,
    _build_host_ssh_prefix,
    _build_kubectl_cmd,
    _build_vllm_k8s_loadgen_plan,
    _find_llm_fault_scenario,
    _ai_event_to_skill_payload,
    _generate_ai_events_from_fault_runs,
    _host_ssh_env,
    _llm_fault_preflight,
    _load_llm_fault_scenarios,
    _merge_cluster_defaults_for_vllm_loadgen,
    _parse_llm_host_observation,
    _require_fault_execution_approval,
    _run_llm_fault,
    app,
)
from vllm_fault_injector.software_faults import SoftwareFaultInjector, VLLM_PID_SH as SOFTWARE_VLLM_PID_SH  # noqa: E402
from vllm_fault_injector.driver_faults import DriverFaultInjector, VLLM_PID_SH as DRIVER_VLLM_PID_SH  # noqa: E402
from vllm_fault_injector.network_faults import NetworkFaultInjector  # noqa: E402
from vllm_fault_injector.os_faults import OSFaultInjector  # noqa: E402


def test_active_fault_runs_excludes_injections_with_matching_cleanup() -> None:
    runs = [
        {
            "id": "cleanup-2", "status": "executed", "action": "cleanup",
            "cluster_id": "ai-b", "namespace": "llm", "deployment": "vllm-b", "scenario_id": "sw-003",
            "results": [{"source_fault_run_id": "inject-2"}],
        },
        {
            "id": "inject-2", "status": "executed", "action": "inject",
            "cluster_id": "ai-b", "namespace": "llm", "deployment": "vllm-b", "scenario_id": "sw-003",
            "result": {"recovery_cmd": "kubectl delete pod load-b"},
        },
        {
            "id": "inject-1", "status": "executed", "action": "inject",
            "cluster_id": "ai-a", "namespace": "llm", "deployment": "vllm-a", "scenario_id": "sw-003",
            "started_at": 1, "result": {"recovery_cmd": "kubectl delete pod load-a"},
        },
    ]

    active = _active_fault_runs(runs)

    assert [run["id"] for run in active] == ["inject-1"]
    assert active[0]["recoverable"] is True
    assert active[0]["age_seconds"] >= 0


def test_active_fault_runs_treats_matching_target_cleanup_as_recovered() -> None:
    runs = [
        {
            "id": "cleanup-latest", "status": "executed", "action": "cleanup",
            "cluster_id": "ai-a", "namespace": "llm", "deployment": "vllm-a", "scenario_id": "sw-003",
            "results": [],
        },
        {
            "id": "inject-old", "status": "executed", "action": "inject",
            "cluster_id": "ai-a", "namespace": "llm", "deployment": "vllm-a", "scenario_id": "sw-003",
            "result": {"recovery_cmd": "kubectl delete pod load-a"},
        },
    ]

    assert _active_fault_runs(runs) == []


def test_fault_batch_observation_correlates_only_current_fault_run(monkeypatch) -> None:
    unrelated = {
        "id": "event-old",
        "source": "k8s_pod_health",
        "event_type": "k8s_pod_imagepullbackoff",
    }
    target = {
        "id": "event-current",
        "source": "fault_experiment",
        "event_type": "fault_experiment_signal",
        "experiment_run_id": "fault-current",
    }
    diagnosed = []

    monkeypatch.setattr(fault_batch, "refresh_events", lambda _args: {"ok": True, "data": {}})
    monkeypatch.setattr(
        fault_batch,
        "active_events",
        lambda _args: {"ok": True, "data": {"events": [unrelated, target]}},
    )
    monkeypatch.setattr(
        fault_batch,
        "diagnose_events",
        lambda _args, events: diagnosed.extend(events) or [{"event_id": item["id"]} for item in events],
    )
    args = SimpleNamespace(observe_seconds=5, observe_interval_s=1, observe_full_window=False)

    result = fault_batch.observe_fault_window(args, "fault-current")

    assert result["scenario_event"] == target
    assert diagnosed == [target]
    assert result["skill_diagnosis"] == [{"event_id": "event-current"}]


def test_fault_batch_load_profile_is_controlled_by_arguments_not_scenario_id() -> None:
    args = SimpleNamespace(
        cluster_id="cluster-a",
        namespace="inference",
        deployment="model-server",
        service="model-server",
        model="model-a",
        loadgen_duration_s=57,
        loadgen_concurrency=7,
        loadgen_prompt_len=128,
        loadgen_max_tokens=32,
        loadgen_qps=1.5,
        loadgen_timeout_s=40,
        baseline_samples=2,
        fault_samples=3,
        sample_interval=4,
    )

    body = fault_batch.build_body(args)

    assert body["loadgen_duration_s"] == 57
    assert body["loadgen_concurrency"] == 7
    assert body["cluster_id"] == "cluster-a"
from vllm_fault_injector.collector import MetricSnapshot, PerformanceCollector  # noqa: E402


def _scenario_id(client: TestClient, fault_type: str | None = None) -> str:
    data = client.get("/api/llm-faultlab/scenarios").json()
    scenarios = data.get("scenarios") or []
    if fault_type:
        for scenario in scenarios:
            if scenario.get("fault_type") == fault_type:
                return scenario["id"]
    assert scenarios, data
    return scenarios[0]["id"]


def _assert_approval_denied(body: dict, action: str, fault_type: str, detail: str) -> None:
    try:
        _require_fault_execution_approval(
            body=body,
            target_mode=body.get("target_mode", "k8s"),
            action=action,
            fault_type=fault_type,
        )
    except HTTPException as exc:
        assert exc.status_code == 400
        assert detail in str(exc.detail)
    else:
        raise AssertionError(f"approval should reject {body}")


def test_llm_fault_real_execution_requires_confirm() -> None:
    _assert_approval_denied(
        {"dry_run": False, "target_mode": "k8s"},
        action="inject",
        fault_type="request_queue_overflow",
        detail="confirm=true",
    )


def test_llm_fault_real_experiment_requires_experiment_ack() -> None:
    _assert_approval_denied(
        {"dry_run": False, "target_mode": "k8s", "confirm": True},
        action="experiment",
        fault_type="request_queue_overflow",
        detail="experiment_ack=true",
    )


def test_llm_fault_high_risk_requires_risk_ack() -> None:
    _assert_approval_denied(
        {"dry_run": False, "target_mode": "k8s", "confirm": True},
        action="inject",
        fault_type="tokenizer_error",
        detail="risk_ack=true",
    )


def test_llm_fault_host_mode_requires_host_ack() -> None:
    _assert_approval_denied(
        {"dry_run": False, "target_mode": "host", "confirm": True},
        action="inject",
        fault_type="request_queue_overflow",
        detail="host_ack=true",
    )


def test_llm_fault_dry_run_endpoint_never_requires_confirm() -> None:
    client = TestClient(app)
    scenario_id = _scenario_id(client)
    response = client.post(
        f"/api/llm-faultlab/inject/{scenario_id}",
        json={
            "dry_run": True,
            "target_mode": "k8s",
            "namespace": "vllm",
            "deployment": "vllm-server",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload.get("status") in {"dry_run", "unavailable", "import_error"}
    if payload.get("status") == "dry_run":
        summary = payload.get("effect_summary") or {}
        assert summary.get("dry_run") is True
        assert summary.get("target")
        assert summary.get("observation_hints")


def test_ai_fault_run_events_hide_experiment_truth_from_diagnosis(monkeypatch) -> None:
    now = app_module.time.time()
    runs = [
        {
            "id": "fault-http503",
            "target_mode": "k8s",
            "cluster_id": "t4-vllm-k8s",
            "action": "experiment",
            "scenario_id": "net-004",
            "fault_type": "http_503",
            "layer": "network",
            "started_at": now - 5,
            "finished_at": now - 2,
            "status": "executed",
        },
        {
            "id": "fault-disk",
            "target_mode": "k8s",
            "cluster_id": "t4-vllm-k8s",
            "action": "experiment",
            "scenario_id": "llm-os-disk-pressure",
            "fault_type": "disk_pressure",
            "layer": "os",
            "started_at": now - 10,
            "finished_at": now - 3,
            "status": "executed",
        },
    ]
    monkeypatch.setattr(app_module, "_load_fault_runs", lambda: runs)

    events = _generate_ai_events_from_fault_runs({"id": "t4-vllm-k8s", "name": "T4 vLLM"})

    by_run_id = {event.get("experiment_run_id"): event for event in events}
    http_event = by_run_id["fault-http503"]
    disk_event = by_run_id["fault-disk"]
    assert http_event["fault_type"] == "fault_experiment_signal"
    assert http_event["fault_type"] != "vllm_process_crash"
    assert http_event["evaluation_context"]["fault_type"] == "http_503"
    assert disk_event["fault_type"] == "fault_experiment_signal"
    assert disk_event["evaluation_context"]["fault_type"] == "disk_pressure"
    for event in (http_event, disk_event):
        payload = _ai_event_to_skill_payload(event)
        text = str(payload)
        assert "experiment_fault_type" not in payload
        assert "experiment_context" not in payload
        assert event["evaluation_context"]["fault_type"] not in text
        assert event["evaluation_context"]["scenario_id"] not in text
        assert not any(item.get("signal") in {"fault_family", "fault_type", "scenario_id"} for item in event["evidence"])


def test_vllm_k8s_loadgen_plan_targets_service_and_model() -> None:
    plan = _build_vllm_k8s_loadgen_plan(
        {
            "service": "vllm-server",
            "model": "qwen2.5-coder-3b-k8s",
            "loadgen_duration_s": 120,
            "loadgen_concurrency": 6,
            "loadgen_prompt_len": 1024,
            "loadgen_max_tokens": 96,
            "loadgen_node_name": "dds-128",
        },
        cluster_id=None,
        namespace="llm-k8s",
        deployment="vllm-server",
    )

    joined = "\n".join(plan["commands"])
    assert plan["endpoint"] == "http://vllm-server.llm-k8s.svc.cluster.local:8000/v1/completions"
    assert plan["model"] == "qwen2.5-coder-3b-k8s"
    assert "127.0.0.1" not in joined
    assert "--overrides=" in joined
    assert "/opt/loadgen/loadgen.py" in joined
    assert "imagePullPolicy" in joined
    assert "node.kubernetes.io/disk-pressure" in joined
    assert "dds-128" in joined
    assert "app=agenticsre-vllm-loadgen" in joined


def test_vllm_k8s_loadgen_plan_can_target_gemma_when_explicit() -> None:
    plan = _build_vllm_k8s_loadgen_plan(
        {
            "service": "vllm-gemma",
            "model": "gemma-3-4b-it",
            "loadgen_duration_s": 120,
            "loadgen_concurrency": 1,
        },
        cluster_id=None,
        namespace="llm-k8s",
        deployment="vllm-gemma",
    )

    joined = "\n".join(plan["commands"])
    assert plan["endpoint"] == "http://vllm-gemma.llm-k8s.svc.cluster.local:8000/v1/completions"
    assert plan["model"] == "gemma-3-4b-it"
    assert "vllm-gemma" in joined


def test_vllm_loadgen_uses_ai_cluster_model_when_request_omits_or_uses_placeholder() -> None:
    cluster = {
        "id": "t4-vllm-k8s",
        "service": "vllm-server",
        "model": "qwen2.5-coder-3b-k8s",
    }

    for body in ({}, {"model": "vllm"}):
        merged = _merge_cluster_defaults_for_vllm_loadgen(
            body,
            cluster,
            namespace="llm-k8s",
            deployment="vllm-server",
            service="vllm-server",
        )
        plan = _build_vllm_k8s_loadgen_plan(
            merged,
            cluster_id="t4-vllm-k8s",
            namespace="llm-k8s",
            deployment="vllm-server",
        )
        assert plan["model"] == "qwen2.5-coder-3b-k8s"
        assert plan["service"] == "vllm-server"


def test_ai_network_faults_stay_inside_k8s_service_selector() -> None:
    injector = NetworkFaultInjector(
        kubectl_cmd="kubectl --kubeconfig=/app/data/t4-admin.conf",
        namespace="llm-k8s",
        dry_run=True,
    )

    timeout = asyncio.run(injector.inject_endpoint_timeout("vllm-server"))
    http_503 = asyncio.run(injector.inject_http_503("vllm-server"))
    recovery = asyncio.run(injector.recover_all("vllm-server"))
    joined = "\n".join(
        value or ""
        for value in [
            timeout.command,
            timeout.recovery_cmd,
            http_503.command,
            http_503.recovery_cmd,
            recovery.command,
        ]
    )

    assert "patch service/vllm-server" in joined
    assert "agenticsre-network-blackhole" in joined
    assert "agenticsre-http-blackhole" in joined
    assert "Routed service/vllm-server to an empty selector" in timeout.message
    assert '"app":"vllm-server"' in joined
    assert "iptables" not in joined
    assert "tc qdisc" not in joined
    assert "rollout restart" not in joined


def test_llm_fault_dry_run_surfaces_k8s_background_load_plan() -> None:
    client = TestClient(app)
    scenario_id = _scenario_id(client, "request_queue_overflow")
    response = client.post(
        f"/api/llm-faultlab/inject/{scenario_id}",
        json={
            "dry_run": True,
            "target_mode": "k8s",
            "namespace": "llm-k8s",
            "deployment": "vllm-gemma",
            "service": "vllm-gemma",
            "model": "gemma-3-4b-it",
            "background_load": True,
            "loadgen_concurrency": 4,
            "loadgen_duration_s": 90,
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] in {"dry_run", "unavailable", "import_error"}
    if payload["status"] == "dry_run":
        loadgen = payload.get("loadgen") or {}
        assert loadgen.get("enabled") is True
        assert loadgen.get("service") == "vllm-gemma"
        assert loadgen.get("model") == "gemma-3-4b-it"
        assert "vllm-gemma.llm-k8s.svc.cluster.local" in "\n".join(loadgen.get("commands") or [])


def test_llm_fault_experiment_recovers_only_matching_fault_layer(monkeypatch) -> None:
    from vllm_fault_injector.injector import FaultInjector

    captured: dict = {}

    async def fake_run_experiment(self, fault_type: str, **kwargs):
        captured["fault_type"] = fault_type
        captured.update(kwargs)
        return {
            "fault_type": fault_type,
            "deployment": self.deployment,
            "namespace": self.namespace,
            "dry_run": self.dry_run,
            "duration_s": 0.1,
            "injection": {"success": True, "message": "injected"},
            "recovery": [{"layer": kwargs.get("recovery_layer"), "success": True, "message": "recovered"}],
            "recovery_layer": kwargs.get("recovery_layer"),
            "metrics_comparison": {},
            "all_snapshots": [],
        }

    monkeypatch.setattr(app_module, "_llm_fault_tool_status", lambda: {"available": True})
    monkeypatch.setattr(app_module, "_append_fault_run", lambda _run: None)
    monkeypatch.setattr(FaultInjector, "run_experiment", fake_run_experiment)

    client = TestClient(app)
    scenario_id = _scenario_id(client, "request_queue_overflow")
    response = client.post(
        f"/api/llm-faultlab/experiment/{scenario_id}",
        json={
            "dry_run": False,
            "confirm": True,
            "experiment_ack": True,
            "target_mode": "k8s",
            "namespace": "llm-k8s",
            "deployment": "vllm-server",
        },
    )

    assert response.status_code == 200, response.text
    assert captured["fault_type"] == "request_queue_overflow"
    assert captured["recovery_layer"] == "software"
    assert response.json()["result"]["recovery_layer"] == "software"


def test_os_recovery_only_removes_stress_pod_without_restarting_vllm() -> None:
    injector = OSFaultInjector(kubectl_cmd="kubectl", namespace="llm-k8s", dry_run=True)

    result = asyncio.run(injector.recover_all("vllm-server"))

    assert "delete pod agenticsre-cpu-stress" in (result.command or "")
    assert "agenticsre-memory-pressure" in (result.command or "")
    assert "agenticsre-disk-pressure" in (result.command or "")
    assert "agenticsre-fd-exhaustion" in (result.command or "")
    assert "agenticsre-io-pressure" in (result.command or "")
    assert "rollout restart" not in (result.command or "")


def test_os_pressure_faults_use_isolated_node_local_pods() -> None:
    injector = OSFaultInjector(
        kubectl_cmd="kubectl --kubeconfig=/app/data/t4-admin.conf",
        namespace="llm-k8s",
        dry_run=True,
    )

    results = [
        asyncio.run(injector.inject_memory_pressure("vllm-server", size_mb=4096)),
        asyncio.run(injector.inject_disk_pressure("vllm-server")),
        asyncio.run(injector.inject_fd_exhaustion("vllm-server")),
        asyncio.run(injector.inject_io_scheduler_interference("vllm-server")),
    ]
    joined = "\n".join(result.command or "" for result in results)

    for pod_name in [
        "agenticsre-memory-pressure",
        "agenticsre-disk-pressure",
        "agenticsre-fd-exhaustion",
        "agenticsre-io-pressure",
    ]:
        assert f"run {pod_name}" in joined
        assert f"delete pod {pod_name}" in joined
        assert f"wait --for=condition=Ready pod/{pod_name}" in joined

    assert "jsonpath=" in joined
    assert ".items[0].spec.nodeName" in joined
    assert '"nodeName":"%s"' in joined
    assert "agenticsre.io/fault=os-pressure" in joined
    assert "base64 -d | /bin/sh" in joined
    assert "vllm/vllm-openai:latest" in joined
    assert "alpine:3.18" not in joined
    assert results[0].message == "[DRY-RUN] Memory pressure pod (2048MB) scheduled beside vllm-server"
    assert "exec -n llm-k8s deploy/vllm-server" not in joined
    assert "rollout restart deploy/vllm-server" not in joined


def test_cpu_stress_waits_until_pressure_pod_is_ready_and_uses_cached_image() -> None:
    injector = OSFaultInjector(
        kubectl_cmd="kubectl --kubeconfig=/app/data/t4-admin.conf",
        namespace="llm-k8s",
        dry_run=True,
    )

    result = asyncio.run(injector.inject_cpu_stress("vllm-server", cores=2, duration=15))
    command = result.command or ""

    assert "run agenticsre-cpu-stress" in command
    assert "--image=vllm/vllm-openai:latest" in command
    assert "alpine:3.18" not in command
    assert "wait --for=condition=Ready pod/agenticsre-cpu-stress --timeout=30s" in command
    assert "CPU stress" in result.message


def test_performance_collector_outputs_json_safe_numbers() -> None:
    collector = PerformanceCollector()
    collector._snapshots.extend([
        MetricSnapshot(
            timestamp=float("nan"),
            label="baseline_0",
            throughput_rps=float("nan"),
            avg_latency_ms=float("inf"),
            p99_latency_ms=-float("inf"),
            gpu_utilization=math.nan,
        ),
        MetricSnapshot(
            timestamp=1.0,
            label="fault_0",
            throughput_rps=1.5,
            avg_latency_ms=100.0,
            p99_latency_ms=200.0,
            gpu_utilization=30.0,
            node_cpu_usage=91.0,
            node_memory_usage=72.0,
        ),
    ])

    payload = {
        "comparison": collector.get_comparison(),
        "snapshots": collector.get_all_snapshots(),
    }

    json.dumps(payload, allow_nan=False)
    assert payload["comparison"]["baseline"]["avg_latency_ms"] == 0.0
    assert payload["comparison"]["fault"]["gpu_utilization"] == 30.0
    assert payload["comparison"]["fault"]["node_cpu_usage"] == 91.0
    assert payload["comparison"]["delta"]["node_memory_usage"] == 72.0
    assert payload["snapshots"][0]["timestamp"] == 0.0
    assert payload["snapshots"][1]["node_cpu_usage"] == 91.0


def test_ai_event_skill_payload_includes_live_observation(monkeypatch) -> None:
    monkeypatch.setattr(app_module, "_ai_event_live_observation", lambda payload: {
        "endpoint_status": "ok",
        "metrics_status": "ok",
        "deployment_ready_replicas": 1,
        "deployment_desired_replicas": 1,
        "process_count": 1,
        "gpu_count": 6,
        "metrics_count": 3,
    })

    payload = _ai_event_to_skill_payload({
        "id": "evt-test",
        "source": "fault_experiment",
        "event_type": "fault_experiment_signal",
        "cluster_id": "t4-vllm-k8s",
        "namespace": "llm-k8s",
        "deployment": "vllm-server",
        "service": "vllm-server",
        "fault_type": "endpoint_timeout",
        "evidence": [{"source": "fault_run", "signal": "scenario_id", "value": "net-001"}],
        "experiment_context": {"fault_type": "endpoint_timeout", "status": "executed"},
    })

    assert "net-001" not in str(payload)
    assert "experiment_context" not in payload
    assert "experiment_fault_type" not in payload
    assert payload["endpoint_status"] == "ok"
    assert payload["metrics_status"] == "ok"
    assert payload["process_count"] == 1
    assert payload["gpu_count"] == 6
    assert payload["live_observation"]["metrics_count"] == 3


def test_llm_experiment_uses_cluster_prometheus_url(monkeypatch) -> None:
    captured = {}

    class FakeInjector:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run_experiment(self, fault_type, **kwargs):
            return {
                "fault_type": fault_type,
                "injection": {"success": True, "message": "ok"},
                "recovery": [{"layer": "os", "success": True, "message": "ok"}],
            }

    scenario = _find_llm_fault_scenario("os-002")
    monkeypatch.setattr(app_module, "_find_k8s_cluster", lambda cluster_id: {
        "id": cluster_id,
        "name": "T4",
        "default_namespace": "llm-k8s",
        "prometheus_url": "http://127.0.0.1:19090",
    })
    monkeypatch.setattr(app_module, "_llm_fault_tool_status", lambda: {
        "available": True,
        "fault_types": ["cpu_stress"],
    })
    monkeypatch.setattr(app_module, "_require_fault_execution_approval", lambda **kwargs: None)
    monkeypatch.setattr(app_module, "_build_vllm_k8s_loadgen_plan", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "_append_fault_run", lambda record: None)
    monkeypatch.setattr(app_module, "_summarize_llm_fault_effect", lambda payload: {"ok": True})
    monkeypatch.setattr("vllm_fault_injector.injector.FaultInjector", FakeInjector)

    result = asyncio.run(_run_llm_fault(
        "experiment",
        scenario,
        {
            "cluster_id": "t4-vllm-k8s",
            "namespace": "llm-k8s",
            "deployment": "vllm-server",
            "dry_run": False,
            "confirm": True,
            "experiment_ack": True,
        },
    ))

    assert result["status"] == "executed"
    assert captured["prometheus_url"] == "http://127.0.0.1:19090"


def test_kv_cache_fault_uses_business_load_pod_not_serving_container() -> None:
    injector = SoftwareFaultInjector(
        kubectl_cmd="kubectl --kubeconfig=/app/data/t4-admin.conf",
        namespace="llm-k8s",
        dry_run=True,
    )

    result = asyncio.run(injector.inject_kv_cache_exhaustion("vllm-server"))
    command = result.command or ""

    assert "run agenticsre-kv-cache-load" in command
    assert "get pod agenticsre-kv-cache-load" in command
    assert "Timed out waiting for old load pod deletion" in command
    assert "|| exit $?" in command
    assert "agenticsre.io/fault=business-load" in command
    assert "base64 -d | /bin/sh" in command
    assert "business-side KV cache pressure load" in result.message
    assert "exec -n llm-k8s deploy/vllm-server" not in command
    assert "--command -- /bin/sh -c" in command
    assert "rollout restart deploy/vllm-server" not in (result.recovery_cmd or "")
    assert "delete pod agenticsre-kv-cache-load" in (result.recovery_cmd or "")
    encoded = re.search(r"printf %s ([A-Za-z0-9+/=]+) \| base64 -d", command)
    assert encoded is not None
    script = base64.b64decode(encoded.group(1)).decode("utf-8")
    assert "max_model_len = int(model_info.get('max_model_len') or 4096)" in script
    assert "target_prompt_tokens = max(128, int(max_model_len * 0.82))" in script
    assert "post_json('/tokenize'" in script
    assert "prompt_tokens = int(tokenized.get('count')" in script
    assert "max_tokens = max(16, min(128, max_model_len - prompt_tokens - 16))" in script
    assert "worker_count = max(12, min(96, 49152 // max(256, max_model_len)))" in script
    assert "range(worker_count)" in script
    assert "deadline = time.time() + 180" in script
    assert "'上下文片段 ' * 4096" not in script


def test_prometheus_vllm_target_selector_uses_matching_instance(monkeypatch) -> None:
    monkeypatch.setattr(app_module, "_prom_instant_values", lambda _base_url, _query: [
        {"metric": {"instance": "vllm-large", "model_name": "large-model"}, "value": 0.95},
        {"metric": {"instance": "vllm-server", "model_name": "target-model"}, "value": 0.10},
    ])

    selector, metadata = app_module._prometheus_vllm_target_selector(
        "http://prometheus",
        deployment="vllm-server",
        service="vllm-server",
        model="target-model",
    )

    assert selector == 'instance="vllm-server"'
    assert metadata["scope"] == "target"
    assert metadata["matched_label"] == "instance"


def test_prometheus_range_summary_preserves_context_extrema(monkeypatch) -> None:
    monkeypatch.setattr(app_module, "_prom_query_url_sync", lambda *args, **kwargs: {
        "results": [{"values": [[1, "0"], [2, "42.5"], [3, "96.4"]]}],
    })

    summary = app_module._prom_range_numeric_summary(
        "http://prometheus",
        "max(vllm:gpu_cache_usage_perc) * 100",
        start=1,
        end=3,
        step_s=5,
    )

    assert summary == {"count": 3, "min": 0.0, "max": 96.4, "last": 96.4}


def test_kv_cache_diagnosis_uses_targeted_baseline_and_incident_context() -> None:
    client = TestClient(app)
    scenario_id = _scenario_id(client, "kv_cache_exhaustion")
    response = client.post(f"/api/llm-faultlab/diagnose/{scenario_id}", json={
        "target_mode": "k8s",
        "deployment": "model-server",
        "effect_summary": {"dry_run": False, "outcome": "load executed"},
        "observation": {
            "endpoint_status": "ok",
            "metrics_status": "ok",
            "process_count": 1,
            "gpu_count": 1,
            "metrics_count": 12,
            "running_requests": 4,
            "waiting_requests": 46,
            "kv_cache_usage": 8.6,
        },
        "observation_extrema": {"max": {"kv_cache_usage": 99.5, "waiting_requests": 46}},
        "metrics_target": {"scope": "target", "selector": 'instance="model-server"'},
        "observation_context": {
            "baseline_window_seconds": 900,
            "incident_window_seconds": 300,
            "signals": {
                "kv_cache_usage": {
                    "baseline": {"count": 60, "min": 0.0, "max": 5.0, "last": 0.0},
                    "incident": {"count": 20, "min": 0.0, "max": 99.5, "last": 8.6},
                    "current": 8.6,
                    "max_delta": 94.5,
                },
            },
        },
    })

    assert response.status_code == 200, response.text
    diagnosis = response.json()["diagnosis"]
    assert diagnosis["fault_type"] == "LLM::kv_cache_exhaustion"
    assert diagnosis["diagnosis_ready"] is True
    assert diagnosis["evidence_summary"]["kv_cache_baseline_max_pct"] == 5.0
    assert diagnosis["evidence_summary"]["kv_cache_incident_max_pct"] == 99.5
    assert diagnosis["confidence_explanation"]["evidence_gate"]["attribution_supported"] is True


def test_kv_cache_diagnosis_reads_nested_fault_run_context(monkeypatch) -> None:
    client = TestClient(app)
    scenario_id = _scenario_id(client, "kv_cache_exhaustion")
    monkeypatch.setattr(app_module, "_load_fault_runs", lambda: [{
        "id": "fault-kv-1",
        "observation_extrema": {"max": {"kv_cache_usage": 59.4, "waiting_requests": 36}},
        "metrics_target": {"scope": "target", "selector": 'instance="model-server"'},
        "observation_context": {
            "signals": {
                "kv_cache_usage": {
                    "baseline": {"count": 10, "max": 0.0},
                    "incident": {"count": 10, "max": 98.8},
                },
            },
        },
    }])

    response = client.post(f"/api/llm-faultlab/diagnose/{scenario_id}", json={
        "target_mode": "k8s",
        "deployment": "model-server",
        "result": {"fault_run_id": "fault-kv-1"},
        "effect_summary": {"dry_run": False, "outcome": "load executed"},
        "observation": {
            "endpoint_status": "ok",
            "metrics_status": "ok",
            "process_count": 1,
            "gpu_count": 1,
            "metrics_count": 12,
            "running_requests": 14,
            "waiting_requests": 36,
            "kv_cache_usage": 59.4,
        },
    })

    assert response.status_code == 200, response.text
    diagnosis = response.json()["diagnosis"]
    assert diagnosis["fault_type"] == "LLM::kv_cache_exhaustion"
    assert diagnosis["diagnosis_ready"] is True
    gate = diagnosis["confidence_explanation"]["evidence_gate"]
    assert gate["observed_pct"] == 98.8
    assert gate["target_scope"] == "target"
    assert gate["attribution_supported"] is True


def test_k8s_cleanup_executes_recorded_recovery_contract_without_layer_reset(monkeypatch) -> None:
    scenario = _find_llm_fault_scenario("sw-003")
    calls = []

    class FakeInjector:
        def __init__(self, **kwargs):
            pass

    monkeypatch.setattr(app_module, "_find_k8s_cluster", lambda _cluster_id: {
        "id": "test-cluster",
        "default_namespace": "llm-k8s",
        "prometheus_url": "http://prometheus",
    })
    monkeypatch.setattr(app_module, "_llm_fault_tool_status", lambda: {"available": True})
    monkeypatch.setattr(app_module, "_require_fault_execution_approval", lambda **kwargs: None)
    monkeypatch.setattr(app_module, "_build_vllm_k8s_loadgen_plan", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "_collect_llm_fault_live_observation", lambda **kwargs: asyncio.sleep(0, result={}))
    monkeypatch.setattr(app_module, "_append_fault_run", lambda _record: None)
    monkeypatch.setattr(app_module, "_load_fault_runs", lambda: [{
        "id": "fault-source",
        "action": "inject",
        "cluster_id": "test-cluster",
        "namespace": "llm-k8s",
        "deployment": "vllm-server",
        "scenario_id": "sw-003",
        "result": {"recovery_cmd": "kubectl -n llm-k8s delete pod agenticsre-kv-cache-load"},
    }])
    monkeypatch.setattr(app_module, "_shell_sync", lambda cmd, *args, **kwargs: (
        calls.append(cmd) or {"ok": True, "returncode": 0, "stdout": "deleted", "stderr": ""}
    ))
    monkeypatch.setattr("vllm_fault_injector.injector.FaultInjector", FakeInjector)

    result = asyncio.run(_run_llm_fault("cleanup", scenario, {
        "cluster_id": "test-cluster",
        "namespace": "llm-k8s",
        "deployment": "vllm-server",
        "service": "vllm-server",
        "fault_run_id": "fault-source",
        "dry_run": False,
        "confirm": True,
        "experiment_ack": True,
    }))

    assert result["status"] == "executed"
    assert calls == ["kubectl -n llm-k8s delete pod agenticsre-kv-cache-load"]
    assert "rollout restart" not in str(result)
    assert result["results"][0]["source"] == "recorded_run"


def test_llm_fault_endpoint_surfaces_missing_confirm_before_tool_execution(monkeypatch) -> None:
    client = TestClient(app)
    scenario_id = _scenario_id(client)

    called = False

    def fake_status() -> dict:
        nonlocal called
        called = True
        return {"available": True}

    monkeypatch.setattr(app_module, "_llm_fault_tool_status", fake_status)
    response = client.post(
        f"/api/llm-faultlab/inject/{scenario_id}",
        json={
            "dry_run": False,
            "target_mode": "k8s",
            "namespace": "vllm",
            "deployment": "vllm-server",
        },
    )

    assert response.status_code == 400
    assert "confirm=true" in response.text
    assert called is False


def test_vllm_process_lookup_command_is_shell_safe() -> None:
    soft = SoftwareFaultInjector(
        kubectl_cmd="kubectl --kubeconfig=/app/data/t4-admin.conf --context=kubernetes-admin@kubernetes",
        namespace="llm-k8s",
        dry_run=True,
    )
    driver = DriverFaultInjector(
        kubectl_cmd="kubectl --kubeconfig=/app/data/t4-admin.conf --context=kubernetes-admin@kubernetes",
        namespace="llm-k8s",
        dry_run=True,
    )

    assert "awk '/vllm|api_server|openai/" not in SOFTWARE_VLLM_PID_SH
    assert "awk '/vllm|api_server|openai/" not in DRIVER_VLLM_PID_SH
    assert "/proc/[0-9]*/cmdline" in SOFTWARE_VLLM_PID_SH
    assert "/proc/[0-9]*/cmdline" in DRIVER_VLLM_PID_SH
    assert '[ "$pid" = "1" ] && continue' in SOFTWARE_VLLM_PID_SH
    assert '[ "$pid" = "1" ] && continue' in DRIVER_VLLM_PID_SH
    assert "vllm.entrypoints.openai.api_server" in SOFTWARE_VLLM_PID_SH
    assert "teller.cli" in SOFTWARE_VLLM_PID_SH

    commands = [
        asyncio.run(soft.inject_process_crash("vllm-server")).command or "",
        asyncio.run(soft.inject_process_hang("vllm-server")).command or "",
        asyncio.run(driver.inject_cupti_trace_overhead("vllm-server")).command or "",
    ]
    for command in commands:
        assert "awk '/vllm|api_server|openai/" not in command
        assert "/proc/[0-9]*/cmdline" in command
        assert "range $k" not in command
        assert "kubectl --kubeconfig=/app/data/t4-admin.conf" in command


def test_cuda_fault_hides_devices_without_assuming_gpu_count() -> None:
    injector = DriverFaultInjector(
        kubectl_cmd="kubectl",
        namespace="inference",
        dry_run=True,
    )

    result = asyncio.run(injector.inject_cuda_error("model-server"))

    assert "CUDA_VISIBLE_DEVICES=-1" in (result.command or "")
    assert "CUDA_VISIBLE_DEVICES=99" not in (result.command or "")
    assert "CUDA_VISIBLE_DEVICES-" in (result.recovery_cmd or "")


def test_cuda_fault_recycles_only_pods_discovered_from_deployment_selector(monkeypatch) -> None:
    injector = DriverFaultInjector(
        kubectl_cmd="kubectl --context test",
        namespace="inference",
        dry_run=False,
    )
    calls = []

    async def fake_pods(_deployment):
        return ["model-server-old-a", "model-server-old-b"]

    async def fake_run(args, timeout=30):
        calls.append((list(args), timeout))
        return 0, "", ""

    monkeypatch.setattr(injector, "_deployment_pod_names", fake_pods)
    monkeypatch.setattr(injector, "_run_kubectl", fake_run)

    result = asyncio.run(injector.inject_cuda_error("model-server"))

    assert result.success is True
    delete_args = next(args for args, _ in calls if args and args[0] == "delete")
    assert "pod/model-server-old-a" in delete_args
    assert "pod/model-server-old-b" in delete_args
    assert not any("vllm-server" in arg for arg in delete_args)


def test_llm_host_passwords_are_runtime_env_only(monkeypatch) -> None:
    monkeypatch.setenv("TARGET_SECRET_ENV", "target-secret")
    monkeypatch.setenv("JUMP_SECRET_ENV", "jump-secret")
    host = {
        "id": "t4-test",
        "host": "gpu.example",
        "ssh_user": "root",
        "jump_host": "guest@jump.example",
        "ssh_password_env": "TARGET_SECRET_ENV",
        "jump_password_env": "JUMP_SECRET_ENV",
    }

    prefix = _build_host_ssh_prefix(host)
    env = _host_ssh_env(host)

    assert "sshpass" in prefix
    assert "SSHPASS_JUMP" in prefix
    assert '-W "$1:$2"' in prefix
    assert "ProxyCommand" in prefix
    assert "target-secret" not in prefix
    assert "jump-secret" not in prefix
    assert env == {"SSHPASS": "target-secret", "SSHPASS_JUMP": "jump-secret"}


def test_fault_targets_reject_plaintext_llm_host_passwords() -> None:
    client = TestClient(app)
    response = client.put(
        "/api/fault-targets",
        json={
            "k8s_clusters": [{"id": "default", "name": "default"}],
            "llm_hosts": [
                {
                    "id": "bad",
                    "name": "bad",
                    "host": "gpu.example",
                    "ssh_user": "root",
                    "ssh_password": "plain-text",
                }
            ],
        },
    )

    assert response.status_code == 400
    assert "plaintext passwords" in response.text


def test_fault_targets_reject_unsafe_kubectl_path() -> None:
    client = TestClient(app)
    response = client.put(
        "/api/fault-targets",
        json={
            "k8s_clusters": [
                {"id": "bad", "name": "bad", "kubectl_path": "kubectl; rm -rf /"}
            ],
            "llm_hosts": [],
        },
    )

    assert response.status_code == 400
    assert "kubectl_path" in response.text


def test_cluster_kubectl_path_rewrites_commands(monkeypatch) -> None:
    monkeypatch.setattr(
        app_module,
        "_find_k8s_cluster",
        lambda cluster_id: {
            "id": cluster_id,
            "name": "custom",
            "kubectl_path": "/usr/local/bin/kubectl",
            "kubeconfig": "/etc/kube/custom.yaml",
            "context": "custom-context",
        },
    )

    rewritten = _apply_cluster_to_cmd("kubectl get pods -n default", "custom")
    assert rewritten == (
        "KUBECONFIG=/etc/kube/custom.yaml /bin/sh -c "
        "'/usr/local/bin/kubectl --context=custom-context get pods -n default'"
    )
    assert _build_kubectl_cmd("custom") == (
        "/usr/local/bin/kubectl --kubeconfig=/etc/kube/custom.yaml --context=custom-context"
    )


def test_llm_host_dry_run_returns_effect_summary(monkeypatch) -> None:
    client = TestClient(app)
    scenario_id = _scenario_id(client, "process_crash")
    monkeypatch.setattr(app_module, "_find_llm_host", lambda host_id: {
        "id": host_id,
        "name": "GPU test",
        "host": "gpu.example",
        "ssh_user": "root",
    })

    response = client.post(
        f"/api/llm-faultlab/inject/{scenario_id}",
        json={
            "dry_run": True,
            "target_mode": "host",
            "host_id": "gpu-test",
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    summary = payload.get("effect_summary") or {}
    assert payload.get("status") == "dry_run"
    assert summary.get("dry_run") is True
    assert summary.get("target") == "gpu-test"
    assert summary.get("fault_type") == "process_crash"
    assert any("主机探测" in item or "vLLM" in item for item in summary.get("observation_hints", []))


def test_llm_host_load_fault_uses_configured_model(monkeypatch) -> None:
    client = TestClient(app)
    scenario_id = _scenario_id(client, "request_queue_overflow")
    monkeypatch.setattr(app_module, "_find_llm_host", lambda host_id: {
        "id": host_id,
        "name": "GPU test",
        "host": "gpu.example",
        "ssh_user": "root",
    })

    response = client.post(
        f"/api/llm-faultlab/inject/{scenario_id}",
        json={
            "dry_run": True,
            "target_mode": "host",
            "host_id": "gpu-test",
            "endpoint": "http://127.0.0.1:18000/v1/completions",
            "model": "Qwen2.5-7B-Instruct",
            "concurrent": 9,
            "rounds": 3,
            "prompt_len": 1024,
            "max_tokens": 64,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["parameters"]["model"] == "Qwen2.5-7B-Instruct"
    assert payload["parameters"]["concurrent"] == 9
    assert payload["parameters"]["rounds"] == 3
    assert payload["parameters"]["prompt_len"] == 1024
    assert payload["parameters"]["max_tokens"] == 64
    assert "Qwen2.5-7B-Instruct" in payload["remote_command"]
    assert "range(9)" in payload["remote_command"]
    assert "range(3)" in payload["remote_command"]
    assert '"max_tokens": 64' in payload["remote_command"]
    assert '"model": "vllm"' not in payload["remote_command"]


def test_llm_preflight_blocks_missing_host_business_params(monkeypatch) -> None:
    scenario = next(s for s in _load_llm_fault_scenarios() if s.get("fault_type") == "request_queue_overflow")
    monkeypatch.setattr(app_module, "_find_llm_host", lambda host_id: {
        "id": host_id,
        "name": "GPU test",
        "host": "gpu.example",
        "ssh_user": "root",
    })

    preflight = _llm_fault_preflight(
        scenario,
        {"dry_run": False, "target_mode": "host", "host_id": "gpu-test", "confirm": True, "host_ack": True},
    )

    blocker_fields = {item["field"] for item in preflight["blockers"]}
    assert preflight["ok"] is False
    assert {"endpoint", "model"}.issubset(blocker_fields)
    assert any(p["name"] == "model" and p["required"] == "true" for p in preflight["parameters"])


def test_llm_preflight_covers_overlay_scenarios(monkeypatch) -> None:
    overlay_scenarios = [s for s in _load_llm_fault_scenarios() if str(s.get("id", "")).startswith("llm-")]
    assert overlay_scenarios
    monkeypatch.setattr(app_module, "_find_llm_host", lambda host_id: {
        "id": host_id,
        "name": "GPU test",
        "host": "gpu.example",
        "ssh_user": "root",
    })
    k8s_types = set(app_module._llm_fault_tool_status()["fault_types"])

    for scenario in overlay_scenarios:
        fault_type = scenario["fault_type"]
        assert fault_type in k8s_types, scenario
        host_body = {
            "dry_run": True,
            "target_mode": "host",
            "host_id": "gpu-test",
            "endpoint": "http://127.0.0.1:8000/v1/completions",
            "model": "test-model",
            "interface": "eth0",
            "model_dir": "/models/test-model",
        }
        host_preflight = _llm_fault_preflight(scenario, host_body)
        assert host_preflight["supported"] is True, host_preflight
        assert not host_preflight["blockers"], host_preflight


def test_llm_k8s_preflight_shows_selected_target_and_recovery_commands() -> None:
    scenario = next(s for s in _load_llm_fault_scenarios() if s.get("fault_type") == "endpoint_timeout")

    preflight = _llm_fault_preflight(
        scenario,
        {
            "dry_run": True,
            "target_mode": "k8s",
            "namespace": "ai-test",
            "deployment": "serving-deployment",
            "service": "serving-entry",
        },
    )

    assert preflight["ok"] is True, preflight
    assert "service/serving-entry" in preflight["command_preview"]
    assert "service/serving-entry" in preflight["recovery_preview"]
    assert "agenticsre-network-blackhole" in preflight["command_preview"]
    assert '"app":"serving-entry"' in preflight["recovery_preview"]


def test_critical_llm_scenarios_require_risk_ack_from_metadata() -> None:
    scenarios = _load_llm_fault_scenarios()
    critical = [scenario for scenario in scenarios if scenario.get("severity") == "critical"]

    assert critical
    assert all(scenario.get("requires_risk_ack") is True for scenario in critical)

    gpu_oom = next(scenario for scenario in critical if scenario.get("fault_type") == "gpu_oom")
    preflight = _llm_fault_preflight(
        gpu_oom,
        {
            "dry_run": False,
            "target_mode": "k8s",
            "namespace": "ai-test",
            "deployment": "vllm-server",
            "service": "vllm-server",
            "confirm": True,
            "experiment_ack": True,
        },
        action="experiment",
    )

    assert preflight["high_risk"] is True
    assert any(item["field"] == "risk_ack" for item in preflight["blockers"])


def test_parse_llm_host_direct_observation() -> None:
    parsed = _parse_llm_host_observation(
        "\n".join([
            "HOST=t4-node",
            "TS=1710000000",
            "GPUCSV_BEGIN",
            "0, NVIDIA T4, 42, 1024, 15360, 35.5, 61, 1590",
            "GPUCSV_END",
            "PROC=123 python -m vllm.entrypoints.openai.api_server|",
            "ENDPOINT=ok",
            "METRICS=ok",
            "METRIC_LINE=vllm:num_requests_waiting 7",
            "METRIC_LINE=vllm:num_requests_running{model_name=\"qwen2.5-coder-3b\"} 17.0",
            "METRIC_LINE=vllm:gpu_cache_usage_perc{model_name=\"qwen2.5-coder-3b\"} 1.2e-1",
        ])
    )

    assert parsed["requires_prometheus"] is False
    assert parsed["observability_mode"] == "host_direct"
    assert parsed["hostname"] == "t4-node"
    assert parsed["endpoint_status"] == "ok"
    assert parsed["metrics_status"] == "ok"
    assert parsed["gpus"][0]["utilization_gpu_pct"] == 42
    assert parsed["gpus"][0]["memory_used_mb"] == 1024
    assert parsed["processes"] == ["123 python -m vllm.entrypoints.openai.api_server"]
    assert parsed["metrics"]["vllm:num_requests_waiting"] == 7
    assert parsed["metrics"]["vllm:num_requests_running"] == 17
    assert parsed["metrics"]["vllm:gpu_cache_usage_perc"] == 0.12


def test_llm_host_observe_endpoint_uses_direct_observation(monkeypatch) -> None:
    client = TestClient(app)
    monkeypatch.setattr(app_module, "_find_llm_host", lambda host_id: {
        "id": host_id,
        "name": "GPU test",
        "host": "gpu.example",
        "ssh_user": "root",
    })

    def fake_shell_sync(cmd, timeout=90, cluster_id=None, env=None):
        return {
            "ok": True,
            "command": cmd,
            "stdout": "\n".join([
                "HOST=gpu-test",
                "TS=1710000001",
                "GPUCSV_BEGIN",
                "0, NVIDIA T4, 0, 0, 15360, 25.0, 40, 300",
                "GPUCSV_END",
                "PROC=",
                "ENDPOINT=unavailable",
                "METRICS=unavailable",
            ]),
            "stderr": "",
            "returncode": 0,
        }

    monkeypatch.setattr(app_module, "_shell_sync", fake_shell_sync)
    response = client.post("/api/fault-targets/llm-host/gpu-test/observe", json={})

    assert response.status_code == 200, response.text
    payload = response.json()
    obs = payload["observation"]
    assert payload["ok"] is True
    assert obs["requires_prometheus"] is False
    assert obs["endpoint_status"] == "unavailable"
    assert obs["metrics_status"] == "unavailable"
    assert obs["gpus"][0]["name"] == "NVIDIA T4"


def test_llm_host_metrics_history_uses_prometheus_series(monkeypatch) -> None:
    client = TestClient(app)
    monkeypatch.setattr(app_module, "_find_llm_host", lambda host_id: {
        "id": host_id,
        "name": "GPU test",
        "host": "gpu.example",
        "ssh_user": "root",
        "prometheus_url": "http://host-profile-prometheus:9090",
    })
    monkeypatch.setattr(app_module, "_prometheus_api_ok", lambda base_url: True)

    seen_base_urls = []

    def fake_prom_query(base_url, query, *, query_type="instant", start="", end="", step="60s"):
        seen_base_urls.append(base_url)
        return {
            "results": [{
                "metric": {"model_name": "qwen2.5-coder-3b"},
                "values": [[1710000000, "0"], [1710000005, "1.25"]],
            }],
            "resultType": "matrix",
            "base_url": base_url,
        }

    monkeypatch.setattr(app_module, "_prom_query_url_sync", fake_prom_query)
    response = client.get(
        "/api/fault-targets/llm-host/gpu-test/metrics-history"
        "?duration=5m&prometheus_url=http://prometheus.example:9090"
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["mode"] == "prometheus"
    assert payload["storage"]["available"] is True
    assert payload["prometheus_url"] == "http://prometheus.example:9090"
    assert seen_base_urls and all(url == "http://prometheus.example:9090" for url in seen_base_urls)
    assert payload["charts"]["running"]["series"][0]["label"] == "qwen2.5-coder-3b"
    assert payload["charts"]["e2e_latency_p95"]["series"][0]["values"] == [0.0, 1.25]


def test_llm_host_metrics_history_uses_host_profile_prometheus(monkeypatch) -> None:
    client = TestClient(app)
    monkeypatch.setattr(app_module, "_find_llm_host", lambda host_id: {
        "id": host_id,
        "name": "GPU test",
        "host": "gpu.example",
        "ssh_user": "root",
        "prometheus_url": "http://host-profile-prometheus:9090",
    })
    monkeypatch.setattr(app_module, "_prometheus_api_ok", lambda base_url: True)

    seen_base_urls = []

    def fake_prom_query(base_url, query, *, query_type="instant", start="", end="", step="60s"):
        seen_base_urls.append(base_url)
        return {
            "results": [{
                "metric": {"model_name": "qwen2.5-coder-3b"},
                "values": [[1710000000, "0.5"]],
            }],
            "resultType": "matrix",
            "base_url": base_url,
        }

    monkeypatch.setattr(app_module, "_prom_query_url_sync", fake_prom_query)
    response = client.get("/api/fault-targets/llm-host/gpu-test/metrics-history?duration=5m")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["mode"] == "prometheus"
    assert payload["prometheus_url"] == "http://host-profile-prometheus:9090"
    assert seen_base_urls and all(url == "http://host-profile-prometheus:9090" for url in seen_base_urls)


def test_llm_host_metrics_history_falls_back_to_snapshot(monkeypatch) -> None:
    client = TestClient(app)
    monkeypatch.setattr(app_module, "_find_llm_host", lambda host_id: {
        "id": host_id,
        "name": "GPU test",
        "host": "gpu.example",
        "ssh_user": "root",
    })
    monkeypatch.setattr(app_module, "_prometheus_api_ok", lambda base_url: False)

    def fake_shell_sync(cmd, timeout=90, cluster_id=None, env=None):
        return {
            "ok": True,
            "command": cmd,
            "stdout": "\n".join([
                "HOST=gpu-test",
                "TS=1710000001",
                "GPUCSV_BEGIN",
                "0, NVIDIA T4, 12, 1024, 15360, 33.0, 48, 300",
                "GPUCSV_END",
                "PROC=123 python -m vllm.entrypoints.openai.api_server",
                "ENDPOINT=ok",
                "METRICS=ok",
                "METRIC_LINE=vllm:num_requests_running{model_name=\"qwen2.5-coder-3b\"} 1",
                "METRIC_LINE=vllm:num_requests_waiting{model_name=\"qwen2.5-coder-3b\"} 2",
                "METRIC_LINE=vllm:gpu_cache_usage_perc{model_name=\"qwen2.5-coder-3b\"} 0.25",
            ]),
            "stderr": "",
            "returncode": 0,
        }

    monkeypatch.setattr(app_module, "_shell_sync", fake_shell_sync)
    response = client.get(
        "/api/fault-targets/llm-host/gpu-test/metrics-history"
        "?duration=5m&prometheus_url=http://127.0.0.1:9"
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["mode"] == "host_direct_snapshot"
    assert payload["storage"]["available"] is False
    assert payload["snapshot"]["endpoint_status"] == "ok"
    assert payload["snapshot"]["metrics"]["vllm:num_requests_waiting"] == 2


def test_llm_faultlab_host_diagnose_and_heal_are_guarded(monkeypatch) -> None:
    client = TestClient(app)
    scenario_id = _scenario_id(client, "request_queue_overflow")
    monkeypatch.setattr(app_module, "_find_llm_host", lambda host_id: {
        "id": host_id,
        "name": "GPU test",
        "host": "gpu.example",
        "ssh_user": "root",
    })
    observation = _parse_llm_host_observation(
        "\n".join([
            "HOST=t4-node",
            "GPUCSV_BEGIN",
            "0, NVIDIA T4, 95, 12000, 15360, 65.0, 70, 1590",
            "GPUCSV_END",
            "PROC=123 python -m vllm.entrypoints.openai.api_server|",
            "ENDPOINT=ok",
            "METRICS=ok",
            "METRIC_LINE=vllm:num_requests_waiting 9",
        ])
    )
    body = {
        "target_mode": "host",
        "host_id": "gpu-test",
        "dry_run": True,
        "effect_summary": {"dry_run": False, "outcome": "fault command executed"},
        "observation": observation,
    }

    diagnosis_response = client.post(f"/api/llm-faultlab/diagnose/{scenario_id}", json=body)
    assert diagnosis_response.status_code == 200, diagnosis_response.text
    diagnosis = diagnosis_response.json()["diagnosis"]
    assert diagnosis["fault_type"] == "LLM::request_queue_overflow"
    assert diagnosis["k8s_fault_type"] == "ResourceExhausted"
    assert "请求排队" in diagnosis["root_cause"]
    assert diagnosis["evidence_summary"]["vllm_num_requests_waiting"] == 9
    assert "推理接口状态: ok" in diagnosis["evidence"]
    assert "等待队列请求数: 9.0" in diagnosis["evidence"]
    assert diagnosis["diagnosis_ready"] is True

    heal_response = client.post(
        f"/api/llm-faultlab/heal/{scenario_id}",
        json={**body, "diagnosis": diagnosis},
    )
    assert heal_response.status_code == 200, heal_response.text
    heal = heal_response.json()["heal"]
    assert heal["mode"] == "host"
    assert heal["execution_mode"] == "guarded_cleanup"
    assert heal["capability"]["real_execution_endpoint"].endswith(f"/api/llm-faultlab/cleanup/{scenario_id}")
    assert any(item["action"] == "llm_fault_cleanup" for item in heal["suggestions"])
    assert "复用当前受控的 vLLM 故障注入恢复路径" in heal["suggestions"][0]["selection_reason"]
    assert "目标: gpu-test" in heal["summary"]
    assert all("command" not in item for item in heal["suggestions"])


def test_llm_faultlab_k8s_diagnosis_uses_aggregated_runtime_signals() -> None:
    client = TestClient(app)
    scenario_id = _scenario_id(client, "process_crash")
    body = {
        "target_mode": "k8s",
        "deployment": "model-server",
        "effect_summary": {"dry_run": False, "outcome": "fault command executed"},
        "observation": {
            "endpoint_status": "ok",
            "metrics_status": "ok",
            "process_count": 1,
            "gpu_count": 4,
            "metrics_count": 13,
            "pod_restart_count": 1,
            "container_terminated_reasons": ["Error"],
            "running_requests": 0,
            "waiting_requests": 0,
        },
    }

    response = client.post(f"/api/llm-faultlab/diagnose/{scenario_id}", json=body)

    assert response.status_code == 200, response.text
    diagnosis = response.json()["diagnosis"]
    assert diagnosis["evidence_summary"]["vllm_process_count"] == 1
    assert diagnosis["evidence_summary"]["pod_restart_count"] == 1
    assert diagnosis["evidence_summary"]["vllm_num_requests_waiting"] == 0
    assert "Pod 重启次数: 1" in diagnosis["evidence"]
    assert "容器异常退出: Error" in diagnosis["evidence"]
    assert "异常退出或重启" in diagnosis["root_cause"]
    assert diagnosis["confidence_raw"] >= 0.9


def test_kv_cache_diagnosis_requires_observed_threshold_crossing() -> None:
    client = TestClient(app)
    scenario_id = _scenario_id(client, "kv_cache_exhaustion")
    body = {
        "target_mode": "k8s",
        "deployment": "model-server",
        "effect_summary": {"dry_run": False, "outcome": "load executed"},
        "observation": {
            "endpoint_status": "ok",
            "metrics_status": "ok",
            "process_count": 1,
            "gpu_count": 1,
            "metrics_count": 12,
            "running_requests": 8,
            "waiting_requests": 0,
            "kv_cache_usage": 39.38,
            "e2e_p95_seconds": 38.8,
        },
    }

    response = client.post(f"/api/llm-faultlab/diagnose/{scenario_id}", json=body)

    assert response.status_code == 200, response.text
    diagnosis = response.json()["diagnosis"]
    assert diagnosis["fault_type"] == "LLM::business_performance_degradation"
    assert diagnosis["k8s_fault_type"] == "LLMBusinessSLODegraded"
    assert "不能确认 KV Cache 耗尽" in diagnosis["root_cause"]
    assert diagnosis["confidence"] <= 0.74
    assert diagnosis["diagnosis_ready"] is False
    assert diagnosis["confidence_explanation"]["evidence_gate"]["confirmed"] is False


def test_kv_cache_diagnosis_accepts_observed_threshold_crossing() -> None:
    client = TestClient(app)
    scenario_id = _scenario_id(client, "kv_cache_exhaustion")
    body = {
        "target_mode": "k8s",
        "deployment": "model-server",
        "effect_summary": {"dry_run": False, "outcome": "load executed"},
        "observation": {
            "endpoint_status": "ok",
            "metrics_status": "ok",
            "process_count": 1,
            "gpu_count": 1,
            "metrics_count": 12,
            "running_requests": 8,
            "waiting_requests": 2,
            "kv_cache_usage": 29.0,
        },
        "observation_extrema": {
            "max": {"kv_cache_usage": 96.0, "waiting_requests": 12.0, "running_requests": 20.0},
            "min": {"kv_cache_usage": 0.0, "waiting_requests": 0.0, "running_requests": 0.0},
        },
    }

    response = client.post(f"/api/llm-faultlab/diagnose/{scenario_id}", json=body)

    assert response.status_code == 200, response.text
    diagnosis = response.json()["diagnosis"]
    assert diagnosis["fault_type"] == "LLM::kv_cache_exhaustion"
    assert diagnosis["k8s_fault_type"] == "LLMGPUResourcePressure"
    assert diagnosis["diagnosis_ready"] is True
    assert diagnosis["evidence_summary"]["kv_cache_usage_pct"] == 96.0


def test_failed_endpoint_is_strong_observable_signal_for_heal_gate() -> None:
    client = TestClient(app)
    scenario_id = _scenario_id(client, "http_503")
    response = client.post(f"/api/llm-faultlab/diagnose/{scenario_id}", json={
        "target_mode": "k8s",
        "deployment": "model-server",
        "effect_summary": {"dry_run": False, "outcome": "network action executed"},
        "observation": {
            "endpoint_status": "failed",
            "metrics_status": "ok",
            "process_count": 1,
            "gpu_count": 1,
            "metrics_count": 10,
            "running_requests": 0,
            "waiting_requests": 0,
        },
    })

    assert response.status_code == 200, response.text
    diagnosis = response.json()["diagnosis"]
    assert diagnosis["k8s_fault_type"] == "ServiceUnavailable"
    assert diagnosis["confidence"] >= 0.85
    assert diagnosis["diagnosis_ready"] is True


def test_llm_host_cleanup_verifies_queue_recovery(monkeypatch) -> None:
    client = TestClient(app)
    scenario_id = _scenario_id(client, "request_queue_overflow")
    monkeypatch.setattr(app_module, "_find_llm_host", lambda host_id: {
        "id": host_id,
        "name": "GPU test",
        "host": "gpu.example",
        "ssh_user": "root",
    })

    calls = []

    def fake_shell_sync(cmd, timeout=90, cluster_id=None, env=None):
        calls.append(cmd)
        if "RUNNING" in cmd and "WAITING" in cmd:
            return {"ok": True, "stdout": "METRICS=ok\nRUNNING=0.0\nWAITING=0.0\n", "stderr": "", "returncode": 0}
        return {"ok": True, "stdout": "", "stderr": "", "returncode": 0}

    monkeypatch.setattr(app_module, "_shell_sync", fake_shell_sync)

    response = client.post(
        f"/api/llm-faultlab/cleanup/{scenario_id}",
        json={
            "dry_run": False,
            "target_mode": "host",
            "host_id": "gpu-test",
            "endpoint": "http://127.0.0.1:8000/v1/completions",
            "model": "test-model",
            "confirm": True,
            "host_ack": True,
            "recovery_timeout_s": 5,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "executed"
    assert payload["recovery_verification"]["checked"] is True
    assert payload["recovery_verification"]["recovered"] is True
    assert any("METRICS=ok" in cmd for cmd in calls)


def test_llm_faultlab_k8s_heal_reuses_self_healing_plan() -> None:
    client = TestClient(app)
    scenario_id = _scenario_id(client, "request_queue_overflow")
    body = {
        "target_mode": "k8s",
        "namespace": "vllm",
        "deployment": "vllm-server",
        "dry_run": True,
        "effect_summary": {"dry_run": False, "outcome": "fault command executed"},
        "observation": {
            "endpoint_status": "ok",
            "metrics_status": "ok",
            "metrics": {"vllm:num_requests_waiting": 5},
            "requires_prometheus": False,
        },
    }

    diagnosis_response = client.post(f"/api/llm-faultlab/diagnose/{scenario_id}", json=body)
    assert diagnosis_response.status_code == 200, diagnosis_response.text
    diagnosis = diagnosis_response.json()["diagnosis"]
    heal_response = client.post(
        f"/api/llm-faultlab/heal/{scenario_id}",
        json={**body, "diagnosis": diagnosis},
    )

    assert heal_response.status_code == 200, heal_response.text
    payload = heal_response.json()
    assert payload["heal_payload"]["source"] == "llm_faultlab"
    assert payload["heal_payload"]["fault_type"] == "ResourceExhausted"
    assert payload["heal"]["suggestions"]
    assert any("vllm-server" in item.get("command", "") for item in payload["heal"]["suggestions"])
