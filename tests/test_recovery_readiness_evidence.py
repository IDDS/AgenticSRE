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
