#!/usr/bin/env python3
"""Hermes Agent integration tests for the AgenticSRE Skill Runtime."""

from __future__ import annotations

import json
import pathlib
import time
from typing import Any, Dict, Optional

from fastapi.testclient import TestClient

from integrations.hermes_skill_runtime import (
    HERMES_SKILL_TOOL_NAMES,
    HermesSkillRuntimeClient,
    extract_hermes_tool_activity,
    register_hermes_skill_runtime_tools,
    skill_tool_name,
)
from web_app import app as dashboard


ROOT = pathlib.Path(__file__).resolve().parents[2]

SKILLS = [
    {
        "id": "k8s.service_unavailable",
        "name": "K8s Service 不可用",
        "domain": "k8s",
        "description": "Diagnose Service endpoints and backend readiness.",
        "stages": ["detect", "diagnose", "heal", "verify"],
        "risk": "medium",
        "requires_approval": True,
        "safe_auto_execute": False,
        "tags": ["kubernetes", "service"],
        "contract_status": {"maturity": "complete"},
    },
    {
        "id": "llm.vllm_process_crash",
        "name": "vLLM 进程崩溃",
        "domain": "llm",
        "description": "Diagnose a crashed vLLM serving process.",
        "stages": ["detect", "diagnose", "heal", "verify"],
        "risk": "high",
        "requires_approval": True,
        "safe_auto_execute": False,
        "tags": ["llm", "vllm", "process"],
        "contract_status": {"maturity": "complete"},
    },
    {
        "id": "generic.evidence_collection",
        "name": "通用证据采集",
        "domain": "generic",
        "description": "Collect bounded read-only evidence.",
        "stages": ["detect"],
        "risk": "low",
        "requires_approval": False,
        "safe_auto_execute": True,
        "tags": ["evidence"],
        "contract_status": {"maturity": "complete"},
    },
]


class FakeTransport:
    def __init__(self) -> None:
        self.calls = []

    def __call__(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        self.calls.append((method, path, body))
        if path == "/api/health":
            return {"status": "ok", "skills": len(SKILLS)}
        if path == "/api/skills":
            return {"skills": SKILLS}
        if path.endswith("/detail"):
            skill = next(item for item in SKILLS if item["id"] in path)
            return {
                "metadata": skill,
                "markdown": f"# {skill['name']}\n\n## Safety\n\nAlways dry-run.",
                "source_status": {"documentation_status": "complete"},
                "contract_status": skill["contract_status"],
            }
        if path.startswith("/api/skills/") and path.endswith("/run"):
            skill = next(item for item in SKILLS if item["id"] in path)
            return {
                "status": "completed",
                "dry_run": True,
                "skill": skill,
                "stages": {
                    "detect": {
                        "status": "matched",
                        "confidence": 0.91,
                        "evidence": ["Service endpoints are empty"],
                        "output": {"summary": "检测到 ServiceUnavailable"},
                    },
                    "diagnose": {
                        "status": "ok",
                        "confidence": 0.9,
                        "output": {"root_cause": "Service 后端无就绪副本"},
                    },
                },
            }
        if path == "/api/workflows/operate":
            return {
                "status": "completed",
                "domain": "k8s",
                "dry_run": True,
                "workflow_run_id": "skillwf-test",
                "skill_runtime": {
                    "primary_skill_id": "k8s.service_unavailable",
                    "matched_count": 1,
                    "runs": [{"skill": SKILLS[0]}],
                },
                "diagnosis": {
                    "fault_type": "ServiceUnavailable",
                    "root_cause": "Service 后端 endpoints 为空",
                    "confidence": 0.9,
                    "evidence": ["kubectl get endpoints returned none"],
                    "source": "skill_runtime",
                },
                "execution_plan": {"status": "dry_run", "command_count": 0, "commands": []},
                "verification_plan": {"status": "ready", "checks": ["kubectl get endpoints"]},
            }
        raise AssertionError(f"Unexpected request: {method} {path}")


class FakeRegistry:
    def __init__(self) -> None:
        self.entries: Dict[str, Dict[str, Any]] = {}

    def register(self, **kwargs: Any) -> None:
        self.entries[kwargs["name"]] = kwargs


def test_hermes_can_discover_filter_and_describe_agenticsre_skills() -> None:
    transport = FakeTransport()
    runtime = HermesSkillRuntimeClient("http://skill-runtime", transport=transport)

    catalog = runtime.list_skills(query="vllm", domain="llm", limit=100)
    detail = runtime.skill_detail("llm.vllm_process_crash")

    assert catalog["runtime_skill_count"] == 3
    assert catalog["matched_count"] == 1
    assert catalog["skills"][0]["id"] == "llm.vllm_process_crash"
    assert detail["skill"]["id"] == "llm.vllm_process_crash"
    assert "Always dry-run" in detail["instructions"]


def test_hermes_skill_tools_register_exact_skill_execution_only() -> None:
    transport = FakeTransport()
    runtime = HermesSkillRuntimeClient("http://skill-runtime", transport=transport)
    registry = FakeRegistry()

    names = register_hermes_skill_runtime_tools(client=runtime, registry_obj=registry)

    assert names[:3] == [
        "agenticsre_skills_list",
        "agenticsre_skill_detail",
        "agenticsre_skill_run",
    ]
    assert len(names) == 3 + len(SKILLS)
    assert {skill_tool_name(item["id"]) for item in SKILLS}.issubset(names)
    assert set(registry.entries) == set(names)
    assert all(item["toolset"] == "agenticsre_skills" for item in registry.entries.values())
    assert all(item["check_fn"]() is True for item in registry.entries.values())

    direct = json.loads(registry.entries["agenticsre_skill_run"]["handler"]({
        "skill_id": "k8s.service_unavailable",
        "query": "Service returns 503 and endpoints are empty",
        "namespace": "checkout",
        "stages": ["detect", "diagnose", "heal"],
    }))
    bound = json.loads(registry.entries[skill_tool_name("llm.vllm_process_crash")]["handler"]({
        "query": "vLLM process exited",
        "namespace": "llm-k8s",
        "stages": ["detect", "diagnose"],
    }))
    direct_request = next(call for call in transport.calls if call[1].endswith("/run"))[2]
    assert direct["dry_run"] is True
    assert direct["skill"]["id"] == "k8s.service_unavailable"
    assert direct_request["dry_run"] is True
    assert direct_request["payload"]["source"] == "hermes_agent"
    assert bound["skill"]["id"] == "llm.vllm_process_crash"
    assert not any(call[1] == "/api/workflows/operate" for call in transport.calls)


def test_hermes_subprocess_enables_agenticsre_skill_toolset_without_cluster_hardcoding() -> None:
    cfg = dashboard._get_config()
    prompt = dashboard._build_hermes_system_prompt(cfg)
    script = dashboard._build_hermes_subprocess_script("e30=")

    assert "agenticsre_skills_list" in prompt
    assert "agenticsre_skill_run" in prompt
    assert "agenticsre_skill_workflow" not in prompt
    assert "deterministic workflow" in prompt
    assert "limit=100" in prompt
    assert "DeathStarBench" not in prompt
    assert "test-social-network" not in prompt
    assert "register_hermes_skill_runtime_tools" in script
    assert "extract_hermes_tool_activity" in script
    assert 'enabled_toolsets=["terminal", "file", "agenticsre_skills"]' in script


def test_hermes_tool_activity_correlates_skill_results_without_dropping_other_tools() -> None:
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-catalog",
                    "function": {
                        "name": "agenticsre_skills_list",
                        "arguments": '{"limit":100}',
                    },
                },
                {
                    "id": "call-terminal",
                    "function": {
                        "name": "terminal",
                        "arguments": '{"command":"kubectl get pods"}',
                    },
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-catalog",
            "content": json.dumps({
                "status": "ok",
                "runtime_skill_count": len(SKILLS),
                "matched_count": len(SKILLS),
                "skills": SKILLS,
            }, ensure_ascii=False),
        },
        {
            "role": "tool",
            "tool_call_id": "call-terminal",
            "content": "No resources found",
        },
    ]

    calls, skill_results = extract_hermes_tool_activity(messages)

    assert len(calls) == 2
    assert calls[0]["status"] == "completed"
    assert calls[0]["args_json"] == {"limit": 100}
    assert calls[1]["tool"] == "terminal"
    assert calls[1]["is_skill_tool"] is False
    assert len(skill_results) == 1
    assert skill_results[0]["tool_call_id"] == "call-catalog"
    assert skill_results[0]["result"]["runtime_skill_count"] == len(SKILLS)


def test_hermes_tool_activity_keeps_full_runtime_catalog_up_to_public_limit() -> None:
    skills = [
        {
            "id": f"generic.skill_{index}",
            "name": f"Skill {index}",
            "domain": "generic",
            "description": "Generic runtime skill",
        }
        for index in range(59)
    ]
    calls, skill_results = extract_hermes_tool_activity([
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "call-all-skills",
                "function": {
                    "name": "agenticsre_skills_list",
                    "arguments": '{"limit":100}',
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call-all-skills",
            "content": json.dumps({
                "status": "ok",
                "runtime_skill_count": len(skills),
                "returned_count": len(skills),
                "skills": skills,
            }),
        },
    ])

    assert calls[0]["status"] == "completed"
    assert len(skill_results[0]["result"]["skills"]) == 59


def test_hermes_capabilities_report_live_runtime_catalog(monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "_init_hermes_agent", lambda: ("/tmp/hermes-agent", None))
    monkeypatch.setattr(dashboard, "_llm_configured", lambda _cfg: True)
    monkeypatch.setattr(dashboard, "_get_skill_runtime_skills", lambda: {"skills": SKILLS})

    response = TestClient(dashboard.app).get("/api/hermes/capabilities")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["skill_runtime"]["connected"] is True
    assert payload["skill_runtime"]["skill_count"] == len(SKILLS)
    assert {item["id"] for item in payload["skill_runtime"]["domains"]} == {"k8s", "llm", "generic"}
    assert payload["toolset"]["protocol_tools"] == list(HERMES_SKILL_TOOL_NAMES)
    assert payload["toolset"]["protocol_tool_count"] == 3
    assert payload["toolset"]["skill_tool_count"] == len(SKILLS)
    assert payload["toolset"]["tool_count"] == len(SKILLS) + 3
    assert len(payload["toolset"]["skill_tools"]) == len(SKILLS)
    assert payload["toolset"]["dry_run"] is True


def test_hermes_existing_directory_with_broken_import_is_not_ready(monkeypatch, tmp_path):
    runtime = tmp_path / "hermes-agent"
    runtime.mkdir()
    (runtime / "run_agent.py").write_text("import missing_hermes_dependency\n")
    monkeypatch.setattr(dashboard, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(dashboard, "_llm_configured", lambda _: True)
    monkeypatch.setattr(dashboard, "_get_skill_runtime_skills", lambda: {"skills": SKILLS})
    client = TestClient(dashboard.app)
    result = client.get("/api/hermes/capabilities").json()
    assert result["status"] == "degraded"
    assert result["hermes_agent"]["available"] is False
    assert "missing_hermes_dependency" in result["hermes_agent"]["error"]
    assert client.post("/api/hermes/chat", json={"message": "hello"}).status_code == 503


def test_hermes_import_probe_caches_success_without_model_request(monkeypatch, tmp_path):
    runtime = tmp_path / "hermes-agent"
    runtime.mkdir()
    counter = tmp_path / "imports"
    (runtime / "run_agent.py").write_text(
        f"from pathlib import Path\np = Path({str(counter)!r})\n"
        "p.write_text(p.read_text() + 'x' if p.exists() else 'x')\nclass AIAgent: pass\n")
    monkeypatch.setattr(dashboard, "ROOT_DIR", tmp_path)
    assert dashboard._init_hermes_agent() == (str(runtime), None)
    assert dashboard._init_hermes_agent() == (str(runtime), None)
    assert counter.read_text() == "x"


def test_hermes_chat_session_exposes_registered_skill_tools(monkeypatch) -> None:
    dashboard._hermes_sessions.clear()
    # The model transport below is controlled; satisfy the real configuration
    # gate explicitly instead of depending on a developer's environment key.
    monkeypatch.setattr(dashboard._get_config().llm, "api_key", "offline-hermes-fixture-placeholder")
    monkeypatch.setattr(dashboard, "_init_hermes_agent", lambda: ("/tmp/hermes-agent", None))
    monkeypatch.setattr(dashboard, "_run_hermes_sync", lambda *_args, **_kwargs: {
        "response": "已通过 Skill 目录工具列出技能。",
        "api_calls": 1,
        "tool_calls": [{"tool": "agenticsre_skills_list", "args": '{"limit":100}'}],
        "skill_tool_results": [{
            "tool_call_id": "call-catalog",
            "tool": "agenticsre_skills_list",
            "status": "ok",
            "args": {"limit": 100},
            "result": {
                "status": "ok",
                "runtime_skill_count": len(SKILLS),
                "matched_count": len(SKILLS),
                "returned_count": len(SKILLS),
                "skills": SKILLS,
            },
        }],
        "registered_agenticsre_tools": [
            "agenticsre_skills_list",
            "agenticsre_skill_detail",
            "agenticsre_skill_run",
        ],
        "input_tokens": 10,
        "output_tokens": 20,
    })

    client = TestClient(dashboard.app)
    started = client.post("/api/hermes/chat", json={"message": "列出所有 Skill"})
    assert started.status_code == 200
    session_id = started.json()["session_id"]
    assert started.json()["message_count"] == 1

    session = {}
    for _ in range(100):
        session = client.get(f"/api/hermes/chat/{session_id}").json()
        if session.get("status") != "thinking":
            break
        time.sleep(0.01)

    assert session["status"] == "idle"
    assert session["registered_tools"] == [
        "agenticsre_skills_list",
        "agenticsre_skill_detail",
        "agenticsre_skill_run",
    ]
    assistant = session["messages"][-1]
    assert assistant["tool_calls"][0]["tool"] == "agenticsre_skills_list"
    assert assistant["skill_tool_results"][0]["result"]["runtime_skill_count"] == len(SKILLS)
    assert assistant["registered_tools"] == session["registered_tools"]
    assert session["skill_tool_results_count"] == 1


def test_hermes_frontend_promotes_live_skill_discovery_without_fixed_targets() -> None:
    html = (ROOT / "web_app" / "templates" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "web_app" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    assert "列出所有 Skill" in html
    assert "查找智算 Skill" in html
    assert "诊断活跃告警" in html
    assert "运维对话" in html
    assert "Agent + Skill" in html
    assert 'class="assistant-workspace"' in html
    assert 'class="assistant-sidebar"' in html
    assert 'class="assistant-task"' in html
    assert "AgenticSRE Skill Runtime" not in html
    assert "agenticsre_skill_workflow" not in html
    assert 'id="hermes-input"' in html
    assert '<textarea id="hermes-input"' in html
    assert 'id="hermes-session-select"' in html
    assert 'id="hermes-capabilities"' in html
    assert '<span class="nav-text">智能助手</span>' in html
    assert "Hermes 助手" not in html
    assert "Hermes Agent ·" not in html
    assert "test-social-network 命名空间的所有 Pod" not in html
    assert "hermes: loadHermesView" in js
    assert "hermes: '智能助手'" in js
    assert "Hermes 文本回复" not in js
    assert "data.skill_tool_results" in js
    assert "_hermesRenderSkillCatalog" in js
    assert "_hermesRenderDirectSkillRun" in js
    assert "_hermesRenderSkillWorkflow" in js
    assert "function hermesHandleInputKeydown(event)" in js
    assert "AGENT_SKILL_TOOL_CALL:'智能体调用 Skill'" in js
    assert "function phaseDisplayName(event = {})" in js
    assert "const escaped = escapeHtml(String(text || ''))" in js
    assert "div.style.cssText = role === 'user'" not in js
