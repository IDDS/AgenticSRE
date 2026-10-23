from __future__ import annotations

import copy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from web_app.fault_mode_registry import FaultModeRegistry



def _registry(tmp_path: Path) -> FaultModeRegistry:
    return FaultModeRegistry(root_dir=ROOT, registry_dir=tmp_path / "fault-mode-registry")


def test_snapshot_diff_merge_activate_and_audit(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    base_document = registry.current_document("general")
    base = registry.create_snapshot("general", version="base", actor="pytest", document=base_document)
    assert base["scenario_count"] == 17

    incoming_document = copy.deepcopy(base_document)
    incoming_document["scenarios"][0]["description"] = "changed by version test"
    incoming_document["scenarios"].append({
        "id": "test-new-mode",
        "name": "New bounded mode",
        "category": "application",
        "fault_type": "bounded_test",
        "description": "test-only scenario",
    })
    registry.create_snapshot("general", version="incoming", actor="pytest", document=incoming_document)

    diff = registry.diff("general", "base", "incoming")
    assert diff["summary"] == {"added": 1, "removed": 0, "changed": 1}
    assert diff["changed"][0]["fields"] == ["description"]

    preview = registry.merge("general", "base", "incoming", commit=False)
    assert preview["status"] == "conflict"
    assert preview["conflict_count"] == 1

    merged = registry.merge(
        "general",
        "base",
        "incoming",
        target_version="merged",
        resolutions={base_document["scenarios"][0]["id"]: "incoming"},
        actor="pytest",
        commit=True,
    )
    assert merged["status"] == "created"
    assert merged["version"] == "merged"

    activated = registry.activate("general", "merged", actor="pytest", reason="rollback test")
    assert activated["active_version"] == "merged"
    assert len(registry.active_document("general")["scenarios"]) == 18

    listed = registry.list_versions("general")
    assert listed["active_version"] == "merged"
    assert listed["current_scenario_count"] == 17
    assert any(item["action"] == "merge" for item in listed["recent_audit"])
    assert any(item["action"] == "activate" for item in listed["recent_audit"])


def test_analysis_is_deterministic_and_llm_payload_excludes_commands(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    document = registry.current_document("general")
    analysis = registry.analyze("general")
    assert analysis["scenario_count"] == 17
    assert sum(analysis["class_counts"].values()) == 17
    assert "不参与版本发布" in analysis["llm_role"]

    payload = registry.llm_input(document)
    assert payload["scenario_count"] == 17
    assert payload["scenarios"]
    for scenario in payload["scenarios"]:
        assert "inject" not in scenario
        assert "commands" not in scenario
        assert "cleanup" not in scenario


def test_fault_mode_version_api_uses_activated_catalog(tmp_path: Path, monkeypatch) -> None:
    import web_app.app as dashboard

    registry = _registry(tmp_path)
    monkeypatch.setitem(dashboard._state, "fault_mode_registry", registry)
    client = TestClient(dashboard.app)

    snapshot = client.post(
        "/api/fault-mode-versions/general/snapshot",
        json={"version": "api-base", "actor": "pytest"},
    )
    assert snapshot.status_code == 200
    assert snapshot.json()["scenario_count"] == 17

    analysis = client.post(
        "/api/fault-mode-versions/general/analyze",
        json={"version": "api-base", "use_llm": False},
    )
    assert analysis.status_code == 200
    assert analysis.json()["analysis"]["scenario_count"] == 17
    assert analysis.json()["llm"] is None

    rollback = client.post(
        "/api/fault-mode-versions/general/rollback",
        json={"version": "api-base", "actor": "pytest"},
    )
    assert rollback.status_code == 200
    assert rollback.json()["operation"] == "catalog_version_rollback"
    assert "不执行任何故障清理" in rollback.json()["note"]

    catalog = client.get("/api/faultlab/scenarios")
    assert catalog.status_code == 200
    assert len(catalog.json()["scenarios"]) == 17


def test_llm_advisory_recovery_and_id_whitelist() -> None:
    import web_app.app as dashboard

    recovered = dashboard._recover_fault_mode_llm_json({
        "parse_error": True,
        "raw_response": "分析如下：\n```json\n{\"summary\":\"ok\",\"classes\":[{\"name\":\"GPU\",\"scenario_ids\":[\"sw-001\",\"invented\"],\"reason\":\"test\"}]}\n```",
    })
    assert recovered is not None
    sanitized = dashboard._sanitize_fault_mode_llm_result(recovered, {"sw-001"})
    assert sanitized["summary"] == "ok"
    assert sanitized["classes"][0]["scenario_ids"] == ["sw-001"]


def test_platform_config_llm_test_uses_effective_model(monkeypatch) -> None:
    import web_app.app as dashboard

    calls = {}

    class FakeLLMClient:
        def __init__(self, config):
            calls["configured_model"] = config.model

        async def async_chat(self, messages, **kwargs):
            calls["requested_model"] = kwargs.get("model")
            calls["messages"] = messages
            return "AGENTICSRE_LLM_OK"

    monkeypatch.setattr(dashboard, "LLMClient", FakeLLMClient)
    monkeypatch.setattr(dashboard, "_llm_configured", lambda _: True)
    client = TestClient(dashboard.app)
    response = client.post("/api/platform/config/test-llm")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["model"] == "deepseek-v4-pro"
    assert payload["response_preview"] == "AGENTICSRE_LLM_OK"
    assert calls["configured_model"] == "deepseek-v4-pro"
    assert calls["requested_model"] == "deepseek-v4-pro"


def test_platform_config_persists_and_reloads(tmp_path: Path, monkeypatch) -> None:
    import web_app.app as dashboard

    persisted_file = tmp_path / "platform_config.json"
    base_config = copy.deepcopy(dashboard._get_config())
    monkeypatch.setattr(dashboard, "_PLATFORM_CONFIG_FILE", persisted_file)
    monkeypatch.setattr(dashboard, "reload_config", lambda: copy.deepcopy(base_config))
    monkeypatch.setattr(dashboard, "_load_fault_targets", lambda: None)
    monkeypatch.setitem(dashboard._state, "config", copy.deepcopy(base_config))
    client = TestClient(dashboard.app)

    saved = client.put(
        "/api/platform/config",
        json={
            "llm": {
                "model": "deepseek-v4-pro",
                "base_url": "https://api.deepseek.com/v1",
                "temperature": 0.1,
                "max_tokens": 65536,
                "timeout": 300,
            }
        },
    )
    assert saved.status_code == 200
    assert persisted_file.exists()
    assert persisted_file.stat().st_mode & 0o777 == 0o600

    dashboard._state["config"] = copy.deepcopy(base_config)
    reloaded = client.post("/api/platform/config/reload")
    assert reloaded.status_code == 200
    assert reloaded.json()["effective"]["llm"]["model"] == "deepseek-v4-pro"
    assert reloaded.json()["stored"]["llm"]["model"] == "deepseek-v4-pro"
