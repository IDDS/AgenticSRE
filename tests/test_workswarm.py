import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from integrations.hermes_skill_runtime import HermesSkillRuntimeClient
from web_app import workswarm


@pytest.mark.asyncio
async def test_selectorless_service_never_reads_unscoped_pods():
    calls = []

    async def read(command, namespace):
        calls.append((command, namespace))
        return {"spec": {}}

    result = await workswarm.read_evidence("read_pods", "demo", "frontend", read)
    assert result["selector"] == {}
    assert calls == [("get service frontend", "demo")]


@pytest.mark.asyncio
async def test_empty_endpoints_and_failed_reads_are_distinct():
    async def read(command, namespace):
        assert command == "get endpointslices -l kubernetes.io/service-name=frontend"
        return {"items": []}

    result = await workswarm.read_evidence("read_endpoints", "demo", "frontend", read)
    assert result["count"] == result["endpoint_count"] == 0
    assert not result["truncated"]

    async def empty_slice(*args):
        return {"items": [{"metadata": {"name": "frontend-1"}, "endpoints": None}]}

    result = await workswarm.read_evidence("read_endpoints", "demo", "frontend", empty_slice)
    assert result["slices"] == [{"name": "frontend-1", "count": 0, "endpoints": []}]

    async def denied(*args):
        return {"error": "Forbidden"}
    with pytest.raises(workswarm.EvidenceUnavailable, match="拒绝读取权限"):
        await workswarm.read_evidence("read_endpoints", "demo", "frontend", denied)


def test_workswarm_skill_attribution():
    def transport(method, path, body):
        assert method == "POST"
        assert body["payload"]["source"] == "workswarm"
        assert body["payload"]["invocation_mode"] == "workswarm_skill_tool"
        assert body["stages"] == ["detect", "diagnose"]
        assert body["dry_run"] is True
        return {"status": "completed"}

    HermesSkillRuntimeClient(transport=transport, source="workswarm").run_skill("endpoint", "check")


def test_session_rejects_overlapping_turn_and_target_change(monkeypatch):
    import threading
    release = threading.Event()

    class Client:
        async def run(self, request, on_tool_call, deadline_seconds):
            await asyncio.to_thread(release.wait, 3)
            return {"status": "completed", "output": "No evidence"}

    monkeypatch.setattr(workswarm, "process_client", lambda llm: Client())
    app = FastAPI()
    app.include_router(workswarm.build_router(lambda: SimpleNamespace(llm=None), lambda llm: True, None))
    with TestClient(app) as client:
        body = {"namespace": "default", "service": "frontend", "question": "检查端点"}
        response = client.post("/api/workswarm/turn", json=body)
        assert response.status_code == 202
        sid = response.json()["session_id"]
        body["session_id"] = sid
        assert client.post("/api/workswarm/turn", json=body).status_code == 409
        release.set()
        import time
        for _ in range(100):
            data = client.get(f"/api/workswarm/sessions/{sid}").json()
            if data["status"] != "running":
                break
            time.sleep(.02)
        assert data["status"] == "error"
        assert "未取得" in data["turns"][0]["error"]
        assert client.post("/api/workswarm/turn", json={**body, "service": "other"}).status_code == 409
        assert client.post("/api/workswarm/turn", json={**body, "namespace": "invalid namespace"}).status_code == 422


def test_endpoint_totals_cover_unshown_slices_and_rows():
    slices = [{"metadata": {"name": str(i)}, "endpoints": []} for i in range(21)]
    slices[0]["endpoints"] = [{"conditions": {"ready": True}} for _ in range(30)] + [{}]
    slices[-1]["endpoints"] = [{"conditions": {"ready": False}}]
    result = workswarm.endpoint_observation(slices)
    assert result["readiness"] == {"true": 30, "false": 1, "unspecified": 1}
    assert result["endpoint_count"] == 32
    assert result["shown_endpoint_count"] == 30
    assert result["truncated"]
    assert len(result["slices"]) == 20


def test_latest_failed_endpoint_read_marks_turn_partial(monkeypatch):
    reads = 0

    async def read(command, namespace):
        nonlocal reads
        if command.startswith("get endpointslices"):
            reads += 1
            return {"items": []} if reads == 1 else {"error": "Forbidden"}
        return {"metadata": {"name": "frontend"}, "spec": {}}

    class Client:
        async def run(self, request, on_tool_call, deadline_seconds):
            for idx, tool in enumerate(("read_service", "read_endpoints", "read_pods", "read_endpoints")):
                await on_tool_call({"payload": {"name": tool, "call_id": str(idx), "arguments": {}}})
            return {"status": "completed", "session_id": "runtime-id", "output": "读取受限"}

    monkeypatch.setattr(workswarm, "process_client", lambda llm: Client())
    app = FastAPI()
    app.include_router(workswarm.build_router(lambda: SimpleNamespace(llm=None), lambda llm: True, read))
    with TestClient(app) as client:
        response = client.post("/api/workswarm/turn", json={
            "namespace": "default", "service": "frontend", "question": "复核",
        })
        sid = response.json()["session_id"]
        state = client.get(f"/api/workswarm/sessions/{sid}").json()
        assert state["status"] == "partial"
        turn = state["turns"][-1]
        assert turn["missing_tools"] == ["read_endpoints"]
        assert turn["evidence"][-1]["observation"] == {"error": "集群拒绝读取权限"}


def test_process_omits_empty_proxy_overrides_only_in_child(monkeypatch, tmp_path):
    import os
    import sys

    class SDKClient:
        def __init__(self, command, *, cwd, env):
            self.env = {**os.environ, **env}

    monkeypatch.setitem(sys.modules, "jiuwenswarm_sdk", SimpleNamespace(Client=SDKClient))
    monkeypatch.setenv("WORKSWARM_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("WORKSWARM_BIN", sys.executable)
    monkeypatch.setenv("HTTPS_PROXY", " ")
    monkeypatch.setenv("all_proxy", "")
    monkeypatch.setenv("http_proxy", "http://proxy.example:8080")
    monkeypatch.setenv("NO_PROXY", "localhost")
    client = workswarm.process_client(SimpleNamespace(model="test", base_url="", api_key=""))
    assert "HTTPS_PROXY" not in client.env and "all_proxy" not in client.env
    assert client.env["http_proxy"] == "http://proxy.example:8080"
    assert client.env["NO_PROXY"] == "localhost"
    assert os.environ["HTTPS_PROXY"] == " "


@pytest.mark.asyncio
@pytest.mark.parametrize("tools_used,status", [
    (("read_service", "read_endpoints", "read_pods"), "completed"),
    (("read_service",), "partial"), ((), "error"),
])
async def test_rca_workswarm_preserves_scope_evidence_and_history(monkeypatch, tmp_path, tools_used, status):
    import json
    import httpx
    import web_app.app as dashboard

    cfg = SimpleNamespace(llm=None, remediation=SimpleNamespace(auto_loop_enabled=True))
    monkeypatch.setattr(dashboard, "_get_config", lambda: cfg)
    monkeypatch.setattr(dashboard, "_llm_configured", lambda llm: True)
    monkeypatch.setattr(dashboard, "_find_k8s_cluster", lambda cid: {"context": "east-context"} if cid == "east" else None)
    monkeypatch.setattr(dashboard, "_RCA_HISTORY_FILE", tmp_path / "history.json")
    monkeypatch.setitem(dashboard._state, "rca_runs", {})
    monkeypatch.setitem(dashboard._state, "rca_dedup", {})
    calls = []

    def shell(command, timeout, cluster):
        assert "--context=east-context" in command and "-n shop -o json" in command
        calls.append(command)
        result = {"items": []} if "endpointslices" in command else {"metadata": {"name": "cart"}, "spec": {}}
        return {"ok": True, "stdout": json.dumps(result)}

    class Client:
        async def run(self, request, on_tool_call, deadline_seconds):
            assert {row["name"] for row in request["host_tools"]} == workswarm.CLUSTER_TOOLS
            with pytest.raises(ValueError):
                await on_tool_call({"payload": {"name": "run_diagnostic_skill"}})
            for name in tools_used:
                await on_tool_call({"payload": {"name": name, "call_id": name}})
            return {"status": "completed", "session_id": "private-sdk-id", "output": "取证记录"}

    runtime = workswarm.WorkSwarmRuntime(lambda: cfg, lambda llm: True, None, dashboard._workswarm_cluster_reader)
    monkeypatch.setattr(workswarm, "process_client", lambda llm: Client())
    monkeypatch.setattr(dashboard, "_workswarm_runtime", runtime)
    monkeypatch.setattr(dashboard, "_shell_sync", shell)
    transport = httpx.ASGITransport(app=dashboard.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        body = {"query": "检查端点", "namespace": "shop", "service": "cart", "cluster_id": "east",
                "diagnosis_runtime": "workswarm"}
        for invalid in ({"service": "bad name"}, {"skill_workflow": {}}, {"query": " "},
                        {"cluster_id": "unknown"}, {"diagnosis_runtime": "unknown"}):
            assert (await client.post("/api/rca/run", json={**body, **invalid})).status_code in (400, 422)
        assert not dashboard._state["rca_runs"]
        reply = await client.post("/api/rca/run", json=body)
        assert reply.status_code == 200, reply.text
        run_id = reply.json()["run_id"]
        stream = (await client.get(f"/api/rca/{run_id}/stream")).text
        done = [json.loads(line[6:]) for line in stream.splitlines() if line.startswith("data: ")][-1]
        history = (await client.get("/api/rca/history", params={"q": "cart"})).json()["runs"]
        assert [(row["id"], row["diagnosis_runtime"], row["workswarm_status"]) for row in history] == [
            (run_id, "workswarm", status)]
        record = (await client.get(f"/api/rca/{run_id}")).json()
        result = record["result"]
        assert done["type"] == "done" and result == done["result"]
        assert record["guarded_auto_recovery"] is False and result["automation_gate"]["allowed"] is False
        assert record["status"] == ("failed" if status == "error" else "completed")
        assert result["missing_tools"] == sorted(workswarm.CLUSTER_TOOLS - set(tools_used))
        assert [row["tool"] for row in result["tool_evidence"]] == list(tools_used)
        assert all(row["status"] == "ok" for row in result["tool_evidence"])
        assert len(calls) == len(tools_used)
        assert (await client.post(f"/api/rca/{run_id}/auto-heal")).json()["status"] == "blocked"
        assert (await client.post(f"/api/rca/{run_id}/remediation/approve")).status_code == 400
        assert (await client.post(f"/api/rca/{run_id}/remediation/rollback")).status_code == 409
        stored = dashboard._RCA_HISTORY_FILE.read_text()
        assert "private-sdk-id" not in stored and json.loads(stored)[0]["result"] == result
