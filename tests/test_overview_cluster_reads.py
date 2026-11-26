"""Differentiate successful empty inventories from failed or malformed Kubernetes reads."""
from importlib import import_module

import pytest
from fastapi.testclient import TestClient

dashboard = import_module("web_app.app")
client = TestClient(dashboard.app)
routes = {
    "overview": ("nodes", "pods"),
    "nodes": ("nodes",),
    "events": ("events",),
    "topology": ("nodes", "pods", "deployments", "services"),
}


@pytest.mark.parametrize("route", routes)
def test_successful_empty_list(monkeypatch, route):
    async def read(*args, **kwargs):
        return {"items": []}

    monkeypatch.setattr(dashboard, "_kubectl_json", read)
    response = client.get("/api/cluster/" + route)
    assert response.status_code == 200
    body = response.json()
    if route == "topology":
        assert body["summary"]["total_pods"] == 0
    elif route == "overview":
        assert body["nodes"] == body["pods_total"] == 0
    else:
        assert body[route] == []


@pytest.mark.parametrize(("route", "resource"), [
    (route, resource) for route, resources in routes.items() for resource in resources
])
@pytest.mark.parametrize("failure", [
    {"error": "upstream diagnostics"}, {"kind": "Status", "status": "Failure"}, {"items": None}, None,
])
def test_failed_resource_does_not_become_zero(monkeypatch, route, resource, failure):
    async def read(command, *args, **kwargs):
        return failure if command.split()[1] == resource else {"items": []}

    monkeypatch.setattr(dashboard, "_kubectl_json", read)
    response = client.get("/api/cluster/" + route)
    assert response.status_code == 503
    assert response.json() == {"detail": f"无法读取集群资源 {resource}"}
