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
