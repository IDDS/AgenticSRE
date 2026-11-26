"""Reject empty or non-ready observations as recovery evidence."""
import pytest
import web_app.app as dashboard


@pytest.mark.parametrize("output", ["", "No resources found in default namespace.",
    "api 0/1 Running 0 10s", "api 1/2 Running 0 10s",
    "api 1/1 Terminating 0 10s", "api 0/0 Running 0 10s",
    "api 1/1 Unknown 0 10s", "malformed"])
def test_namespace_requires_positive_readiness(output):
    assert dashboard._namespace_health_from_pods(output)["healthy"] is False


@pytest.mark.parametrize("output", ["api 1/1 Running 0 10s",
    "api 2/2 Running 0 10s\njob 0/1 Completed 0 10s"])
def test_namespace_positive_controls(output):
    assert dashboard._namespace_health_from_pods(output)["healthy"] is True


@pytest.mark.parametrize("obj", [{}, {"status": None},
    {"status": {"phase": "Running", "containerStatuses": [None]}},
    {"status": {"phase": "Running", "containerStatuses": [{"ready": "false"}]}},
    {"metadata": {"deletionTimestamp": "2026-09-21T00:00:00Z"},
     "status": {"phase": "Running", "containerStatuses": [{"ready": True}]}},
    {"status": {"phase": "Running", "containerStatuses": [{"ready": True}],
                "conditions": [{"type": "Ready", "status": "False"}]}}])
def test_pod_rejects_missing_or_negative_evidence(obj):
    assert dashboard._pod_ready(obj) is False


def test_pod_positive_control():
    assert dashboard._pod_ready({"status": {"phase": "Running",
        "containerStatuses": [{"ready": True}],
        "conditions": [{"type": "Ready", "status": "True"}]}})


@pytest.mark.parametrize("output", ["", "api 0/1 Running 0 10s"])
def test_recovery_path_does_not_promote_bad_namespace_evidence(monkeypatch, output):
    monkeypatch.setattr(dashboard, "_kubectl_heal_check", lambda *a, **k:
                        {"ok": True, "stdout": output})
    result = dashboard._verify_heal_recovery(namespace="default", suggestion={},
        commands=[], cluster_id=None, dry_run=False, executed_success=True)
    assert result["recovered"] is False


def verify_target(target, command="kubectl get pods"):
    return dashboard._verify_heal_recovery(namespace="shop", suggestion={"target": target},
        commands=[command], cluster_id="east", dry_run=False, executed_success=True)


@pytest.mark.parametrize("kind,command", [
    ("deployment", "kubectl scale deployment/api --replicas=1"),
    ("pod", "kubectl get pod api"), ("service", "kubectl get service api"),
    ("node", "kubectl get node api"), ("pod", "kubectl delete pod api"),
    ("network_policy", "kubectl delete networkpolicy api"),
    ("network_chaos", "kubectl delete networkchaos api"),
])
def test_unreadable_target_stays_unknown(monkeypatch, kind, command):
    monkeypatch.setattr(dashboard, "_kubectl_heal_check", lambda *a, **kw: {"ok": False, "returncode": 124})
    receipt = verify_target({kind: "api"}, command)
    assert receipt["status"] == "unknown" and receipt["recovered"] is None
    assert receipt["reason"] == "cluster_evidence_unavailable"


def test_pod_lookup_failure_cannot_use_another_objects_readiness(monkeypatch):
    calls = []
    def read(command, **kwargs):
        calls.append(command)
        return {"ok": False} if command.startswith("get pod ") else {
            "ok": True, "json": {"subsets": [{"addresses": [{"ip": "10.0.0.1"}]}]}}
    monkeypatch.setattr(dashboard, "_kubectl_heal_check", read)
    receipt = verify_target({"pod": "broken", "service": "other"})
    assert receipt["recovered"] is None and len(calls) == 1


@pytest.mark.parametrize("desired,generation,observed,rollout,ready,expected", [
    (1, 3, 2, True, 1, None), (1, 3, None, True, 1, None),
    (0, 3, 3, True, 1, False), (1, 3, 3, False, 1, None),
    (1, 3, 3, True, 1, True), (1, 3, 3, True, 0, False),
])
def test_deployment_uses_current_generation(monkeypatch, desired, generation, observed, rollout, ready, expected):
    obj = {"metadata": {"generation": generation}, "spec": {"replicas": desired},
           "status": {"readyReplicas": ready, "availableReplicas": ready}}
    if observed is not None:
        obj["status"]["observedGeneration"] = observed
    monkeypatch.setattr(dashboard, "_kubectl_heal_check", lambda command, **kw:
                        {"ok": rollout} if command.startswith("rollout") else {"ok": True, "json": obj})
    assert verify_target({"deployment": "api"})["recovered"] is expected


@pytest.mark.parametrize("raw", ["", "{}", "null", "[]", "broken json"])
def test_empty_or_invalid_json_is_not_a_successful_resource_read(monkeypatch, raw):
    monkeypatch.setattr(dashboard, "_shell_sync", lambda *a: {"ok": True, "stdout": raw})
    assert dashboard._kubectl_heal_check("get pod api -o json", cluster_id="east", parse_json=True)["ok"] is False
