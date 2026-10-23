#!/usr/bin/env python3
"""Regression tests for configuration-center persistence and runtime effect."""

from __future__ import annotations

import json
import os
import pathlib
import stat
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import web_app.app as app_module  # noqa: E402
from web_app.app import app  # noqa: E402


def test_llm_platform_config_persists_and_reloads(monkeypatch, tmp_path) -> None:
    config_file = tmp_path / "platform_config.json"
    monkeypatch.setattr(app_module, "_PLATFORM_CONFIG_FILE", config_file)
    app_module._reload_runtime_config()

    client = TestClient(app)
    response = client.put(
        "/api/platform/config",
        json={
            "llm": {
                "model": "unit-test-model",
                "base_url": "http://unit-test-llm/v1",
                "api_key": "unit-test-secret",
                "temperature": 0.2,
                "max_tokens": 1234,
                "timeout": 17,
            }
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    llm = payload["effective"]["llm"]
    assert config_file.exists()
    stored = json.loads(config_file.read_text(encoding="utf-8"))
    assert stored["llm"]["model"] == "unit-test-model"
    assert stored["llm"]["base_url"] == "http://unit-test-llm/v1"
    assert stored["llm"]["api_key"] == "unit-test-secret"
    assert llm["model"] == "unit-test-model"
    assert llm["base_url"] == "http://unit-test-llm/v1"
    assert llm["api_key_configured"] is True
    assert llm["api_key_masked"] != "unit-test-secret"
    assert "unit-test-secret" not in json.dumps(payload["stored"], ensure_ascii=False)
    assert payload["stored"]["llm"]["api_key_configured"] is True

    app_module._reload_runtime_config()
    reloaded_payload = client.get("/api/platform/config").json()
    assert "unit-test-secret" not in json.dumps(reloaded_payload, ensure_ascii=False)
    reloaded = reloaded_payload["effective"]["llm"]
    assert reloaded["model"] == "unit-test-model"
    assert reloaded["base_url"] == "http://unit-test-llm/v1"
    assert reloaded["timeout"] == 17
    assert app_module._get_config().llm.model == "unit-test-model"
    assert app_module._get_config().llm.api_key == "unit-test-secret"


def test_empty_llm_api_key_save_keeps_existing_secret(monkeypatch, tmp_path) -> None:
    config_file = tmp_path / "platform_config.json"
    monkeypatch.setattr(app_module, "_PLATFORM_CONFIG_FILE", config_file)
    app_module._reload_runtime_config()

    client = TestClient(app)
    first = client.put(
        "/api/platform/config",
        json={"llm": {"model": "first-model", "api_key": "first-secret"}},
    )
    assert first.status_code == 200, first.text

    second = client.put(
        "/api/platform/config",
        json={"llm": {"model": "second-model", "api_key": ""}},
    )
    assert second.status_code == 200, second.text

    stored = json.loads(config_file.read_text(encoding="utf-8"))
    assert stored["llm"]["model"] == "second-model"
    assert stored["llm"]["api_key"] == "first-secret"
    assert app_module._get_config().llm.model == "second-model"
    assert app_module._get_config().llm.api_key == "first-secret"


def test_platform_config_reset_removes_overrides(monkeypatch, tmp_path) -> None:
    config_file = tmp_path / "platform_config.json"
    monkeypatch.setattr(app_module, "_PLATFORM_CONFIG_FILE", config_file)
    app_module._reload_runtime_config()

    client = TestClient(app)
    saved = client.put("/api/platform/config", json={"llm": {"model": "reset-model"}})
    assert saved.status_code == 200, saved.text
    assert config_file.exists()

    reset = client.delete("/api/platform/config")
    assert reset.status_code == 200, reset.text
    assert not config_file.exists()
    assert reset.json()["stored"] == {}


def test_ai_event_thresholds_saved_from_platform_config_take_effect(monkeypatch, tmp_path) -> None:
    original_config = app_module._get_config()
    monkeypatch.setitem(app_module._state, "config", original_config)
    config_file = tmp_path / "platform_config.json"
    monkeypatch.setattr(app_module, "_PLATFORM_CONFIG_FILE", config_file)
    app_module._reload_runtime_config()

    client = TestClient(app)
    response = client.put(
        "/api/platform/config",
        json={
            "detection": {
                "confirmation_points": 2,
                "thresholds": {
                    "ai": {
                        "waiting_requests": 99,
                        "node_cpu_usage": 10,
                        "teller_kernel_p95_ms": 7,
                    }
                },
            }
        },
    )
    assert response.status_code == 200, response.text
    detection = response.json()["effective"]["detection"]
    assert detection["confirmation_points"] == 2
    assert detection["thresholds"]["ai"]["node_cpu_usage"] == 10

    event_config = app_module._ai_event_detection_config()
    assert event_config["confirmation_points"] == 2
    assert event_config["rules"]["waiting_requests"] == 99
    assert event_config["rules"]["node_cpu_usage"] == 10
    assert event_config["rules"]["teller_kernel_p95_ms"] == 7

    events = app_module._generate_ai_events_from_dashboard_payload({
        "cluster": {"id": "ai-config-threshold", "name": "AI Config Threshold", "kind": "ai"},
        "storage": {"available": True},
        "summary": {
            "running_requests": 0,
            "waiting_requests": 0,
            "ttft_p95": 0,
            "e2e_latency_p95": 0,
            "kv_cache": 0,
            "gpu_util": 0,
            "gpu_memory_used": 0,
            "success_rate": 1,
            "node_cpu_usage": 12,
        },
    })
    node_events = [event for event in events if event.get("event_type") == "node_resource_pressure"]
    assert len(node_events) == 1
    assert node_events[0]["threshold"]["node_cpu_usage"] == 10


def test_detection_extended_fields_survive_platform_reload(monkeypatch, tmp_path) -> None:
    platform_file = tmp_path / "platform_config.json"
    detection_file = tmp_path / "detection_config.json"
    monkeypatch.setattr(app_module, "_PLATFORM_CONFIG_FILE", platform_file)
    monkeypatch.setattr(app_module, "_DETECTION_CONFIG_FILE", detection_file)
    app_module._reload_runtime_config()

    client = TestClient(app)
    response = client.put(
        "/api/detection/config",
        json={
            "sources_enabled": {"prometheus": False},
            "critical_event_reasons": ["ProbeEvent"],
            "critical_pod_reasons": ["ProbePod"],
            "default_detect_methods": ["iqr"],
            "default_algorithm": "iqr",
            "default_lookback_m": 44,
            "default_z_threshold": 2.2,
            "default_ewma_span": 8,
            "min_samples": 7,
            "confirmation_points": 2,
            "relative_change_threshold": 0.15,
            "spectral_residual_threshold": 2.5,
            "categories_enabled": {"application": False},
            "business_services": ["probe-business"],
            "db_services": ["probe-database"],
            "thresholds": {"probe_metric": {"warn": 7, "crit": 9}},
        },
    )
    assert response.status_code == 200, response.text
    assert stat.S_IMODE(platform_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(detection_file.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".*.tmp"))

    app_module._state["config"] = app_module.reload_config()
    app_module._apply_platform_config()
    detection = app_module._get_config().detection
    assert detection.sources_enabled["prometheus"] is False
    assert detection.critical_event_reasons == ["ProbeEvent"]
    assert detection.critical_pod_reasons == ["ProbePod"]
    assert detection.default_detect_methods == ["iqr"]
    assert detection.default_algorithm == "iqr"
    assert detection.default_lookback_m == 44
    assert detection.min_samples == 7
    assert detection.confirmation_points == 2
    assert detection.categories_enabled["application"] is False
    assert detection.business_services == ["probe-business"]
    assert detection.db_services == ["probe-database"]
    assert detection.thresholds["probe_metric"]["crit"] == 9


def test_platform_config_rejects_invalid_ranges(monkeypatch, tmp_path) -> None:
    config_file = tmp_path / "platform_config.json"
    monkeypatch.setattr(app_module, "_PLATFORM_CONFIG_FILE", config_file)
    app_module._reload_runtime_config()
    client = TestClient(app)

    invalid_payloads = [
        {"llm": {"temperature": 9}},
        {"llm": {"max_tokens": 0}},
        {"llm": {"timeout": -1}},
        {"detection": {"confirmation_points": -3}},
        {"detection": {"min_samples": 0}},
        {"remediation": {"confidence_threshold": 3.0}},
        {"remediation": {"max_steps": 0}},
        {"remediation": {"enabled": "false"}},
    ]
    for payload in invalid_payloads:
        response = client.put("/api/platform/config", json=payload)
        assert response.status_code == 400, (payload, response.text)
    assert not config_file.exists()


def test_detection_config_invalid_type_is_client_error_without_mutation(monkeypatch, tmp_path) -> None:
    platform_file = tmp_path / "platform_config.json"
    detection_file = tmp_path / "detection_config.json"
    monkeypatch.setattr(app_module, "_PLATFORM_CONFIG_FILE", platform_file)
    monkeypatch.setattr(app_module, "_DETECTION_CONFIG_FILE", detection_file)
    app_module._reload_runtime_config()
    original = app_module._get_config().detection.default_lookback_m

    response = TestClient(app, raise_server_exceptions=False).put(
        "/api/detection/config",
        json={"default_lookback_m": "not-an-integer"},
    )
    assert response.status_code == 400, response.text
    assert app_module._get_config().detection.default_lookback_m == original
    assert not platform_file.exists()
    assert not detection_file.exists()


def test_unconfigured_host_template_can_persist_but_cannot_execute(monkeypatch, tmp_path) -> None:
    targets_file = tmp_path / "cluster_profiles.json"
    monkeypatch.setattr(app_module, "_FAULT_TARGETS_FILE", targets_file)
    app_module._reload_runtime_config()
    client = TestClient(app, raise_server_exceptions=False)

    response = client.put(
        "/api/fault-targets",
        json={
            "k8s_clusters": [{"id": "probe", "name": "Probe", "kubectl_path": "kubectl"}],
            "llm_hosts": [{"id": "host-template", "name": "Host Template", "host": ""}],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["llm_hosts"][0]["configured"] is False
    assert stat.S_IMODE(targets_file.stat().st_mode) == 0o600

    app_module._state["config"] = app_module.reload_config()
    app_module._load_fault_targets()
    assert app_module._get_config().fault_targets.llm_hosts[0]["id"] == "host-template"
    probe = client.post("/api/fault-targets/llm-host/host-template/probe")
    assert probe.status_code == 400
    assert "not configured" in probe.json()["detail"]


def test_fault_target_write_failure_returns_error_and_rolls_back(monkeypatch, tmp_path) -> None:
    failed_path = tmp_path / "cluster_profiles.json"
    failed_path.mkdir()
    monkeypatch.setattr(app_module, "_FAULT_TARGETS_FILE", failed_path)
    app_module._reload_runtime_config()
    cfg = app_module._get_config()
    previous_clusters = list(cfg.fault_targets.k8s_clusters)
    previous_hosts = list(cfg.fault_targets.llm_hosts)

    response = TestClient(app, raise_server_exceptions=False).put(
        "/api/fault-targets",
        json={
            "k8s_clusters": [{"id": "should-not-stick", "name": "Should Not Stick", "kubectl_path": "kubectl"}],
            "llm_hosts": [],
        },
    )
    assert response.status_code == 500
    assert cfg.fault_targets.k8s_clusters == previous_clusters
    assert cfg.fault_targets.llm_hosts == previous_hosts


def test_existing_auth_config_permissions_are_hardened(monkeypatch, tmp_path) -> None:
    auth_file = tmp_path / "auth_config.json"
    auth_file.write_text(json.dumps({"enabled": False}), encoding="utf-8")
    if os.name != "nt":
        auth_file.chmod(0o644)
    monkeypatch.setattr(app_module, "_AUTH_CONFIG_FILE", auth_file)

    assert app_module._load_auth_config_file() == {"enabled": False}
    if os.name != "nt":
        assert stat.S_IMODE(auth_file.stat().st_mode) == 0o600
