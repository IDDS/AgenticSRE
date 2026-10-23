#!/usr/bin/env python3
"""Dry-run end-to-end regression for self-healing fault types.

The test intentionally does not inject destructive real faults.  For every
HEAL_RECIPES fault type it exercises the same API stages used by the UI:

1. fault injection planning/evidence payload
2. diagnosis/fault-type recognition
3. healing capability generation
4. dry-run execution
5. rollback dry-run when rollback commands exist

Run:
    PYTHONPYCACHEPREFIX=/private/tmp/agenticsre_pycache \
      python3 tests/e2e/test_self_healing_flow.py
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
from typing import Any, Dict, List, Tuple

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
import web_app.app as app_module  # noqa: E402
from web_app.app import HEAL_RECIPES, _normalize_heal_fault_type, app  # noqa: E402


NAMESPACE = "test-social-network"
POD = "frontend-7d8c9f6b5d-x9q2p"
DEPLOYMENT = "frontend"
SERVICE = "frontend"
NODE = "node-1"

TEST_DATA_DIR = pathlib.Path(os.environ.get("AGENTICSRE_HEAL_E2E_DATA_DIR", "/tmp/agenticsre_heal_e2e"))
TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
app_module._HEAL_RUNS_FILE = TEST_DATA_DIR / "heal_runs.json"
app_module._HEAL_ARTIFACT_DIR = TEST_DATA_DIR / "heal_runs"


FAULT_PAYLOADS: Dict[str, Dict[str, Any]] = {
    "OOMKilled": {"message": "Pod frontend exit code 137 OOMKilled memory limit exceeded"},
    "PodCrashLoop": {"message": "CrashLoopBackOff Back-off restarting failed container frontend"},
    "ImagePullError": {"message": "ImagePullBackOff ErrImagePull image pull failed"},
    "NodeNotReady": {"message": "Node node-1 is NotReady and node is not ready"},
    "MetricAnomaly": {"message": "Prometheus metric anomaly detected for container_cpu and latency"},
    "ProbeFailure": {"message": "readiness probe failed liveness probe failed unhealthy pod"},
    "PendingScheduling": {"message": "Pod Pending FailedScheduling Insufficient cpu"},
    "HighCpu": {"message": "High CPU usage detected container_cpu throttling"},
    "HighMemory": {"message": "High memory usage detected container_memory pressure"},
    "HighCPUUsage": {"message": "HighCPU CPU usage container_cpu above threshold"},
    "HighMemoryUsage": {"message": "HighMemory memory usage container_memory above threshold"},
    "DiskPressure": {"message": "Node node-1 reports DiskPressure filesystem pressure disk full"},
    "DNSResolution": {"message": "CoreDNS kube-dns DNS lookup timeout"},
    "KubeProxyUnhealthy": {"message": "[KubeProxyDown] kube-proxy target down"},
    "ApiServerUnhealthy": {"message": "[TargetDown] kube-apiserver API server unavailable"},
    "EtcdUnhealthy": {"message": "[TargetDown] etcd leader changed etcdserver unhealthy"},
    "KubeletUnhealthy": {"message": "[TargetDown] kubelet node agent down"},
    "VolumeMountFailed": {"message": "FailedMount MountVolume FailedAttachVolume for pod frontend"},
    "ConfigMissing": {"message": "configmap app-config not found secret not registered"},
    "ConfigKeyMissing": {"message": "references non-existent configmap key DB_HOST"},
    "SchedulingFailed": {"message": "FailedScheduling Unschedulable Insufficient memory"},
    "ResourceQuotaExceeded": {"message": "exceeded quota forbidden: quota ResourceQuota denied the request"},
    "HPAScalingFailed": {"message": "FailedGetResourceMetric FailedGetScale unable to get target metrics"},
    "DeploymentUnhealthy": {"message": "Deployment frontend unhealthy readiness probe failed replicas unavailable"},
    "NetworkPolicyDeny": {"message": "NetworkPolicy egress denied ingress denied for frontend"},
    "HighLatency": {"message": "p99 latency timeout slow service response"},
    "ServiceUnavailable": {"message": "ServiceUnavailable 5xx no endpoints connection refused"},
    "TargetDown": {"message": "[TargetDown] scrape failed target disappeared from Prometheus"},
    "LogErrorSpike": {"message": "log error spike ERROR rate increased after release"},
    "KubeDeploymentReplicasMismatch": {"message": "KubeDeploymentReplicasMismatch expected number of replicas unavailable"},
    "NodeClockNotSynchronising": {"message": "NodeClockNotSynchronising clock skew time sync issue"},
    "AppArmorProfileUnsupported": {"message": "AppArmor vArmor CreateContainerError profile unsupported denied"},
}

NODE_TYPES = {
    "NodeNotReady",
    "DiskPressure",
    "KubeletUnhealthy",
    "NodeClockNotSynchronising",
}
CONTROL_PLANE_TYPES = {"ApiServerUnhealthy", "EtcdUnhealthy", "DNSResolution", "KubeProxyUnhealthy"}


def _base_payload(fault_type: str) -> Dict[str, Any]:
    payload = {
        "source": "e2e-self-healing",
        "namespace": NAMESPACE,
        "fault_type": fault_type,
        "message": FAULT_PAYLOADS.get(fault_type, {}).get("message", fault_type),
        "root_cause": f"{fault_type} injected evidence",
        "diagnosis": {
            "fault_type": fault_type,
            "root_cause": f"{fault_type} synthetic diagnosis",
            "confidence": 0.95,
            "evidence_summary": {"synthetic": "matched dry-run regression payload"},
        },
    }
    if fault_type in NODE_TYPES:
        payload["node"] = NODE
    elif fault_type in CONTROL_PLANE_TYPES:
        payload["service"] = SERVICE
    else:
        payload.update({"pod": POD, "deployment": DEPLOYMENT, "service": SERVICE})
    return payload


def _cleanup_heal_runs(run_ids: List[str]) -> None:
    runs_file = app_module._HEAL_RUNS_FILE
    if runs_file.exists() and run_ids:
        data = json.loads(runs_file.read_text(encoding="utf-8"))
        data = [r for r in data if r.get("id") not in run_ids]
        runs_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    for run_id in run_ids:
        artifact = app_module._HEAL_ARTIFACT_DIR / f"{run_id}.json"
        if artifact.exists():
            artifact.unlink()


def _assert_ok(condition: bool, detail: str) -> None:
    if not condition:
        raise AssertionError(detail)


def _exercise_fault_type(client: TestClient, fault_type: str) -> Tuple[str, int, int]:
    payload = _base_payload(fault_type)
    normalized = _normalize_heal_fault_type(payload["message"])
    _assert_ok(
        normalized == fault_type or payload["fault_type"] == fault_type,
        f"diagnosis normalization mismatch: expected={fault_type}, normalized={normalized}",
    )

    capability = client.post("/api/heal/capability", json=payload).json()
    _assert_ok(capability.get("fault_type") == fault_type, f"capability fault_type mismatch: {capability}")
    suggestions = capability.get("suggestions") or []
    _assert_ok(suggestions, f"no healing suggestions generated for {fault_type}")
    _assert_ok(all(s.get("executable") for s in suggestions), f"non-executable suggestion for {fault_type}: {suggestions}")
    _assert_ok((capability.get("verification_plan") or {}).get("checks"), f"missing verification plan for {fault_type}: {capability}")
    _assert_ok(capability.get("strategy_trace", {}).get("recipe_matched") is True, f"recipe not matched for {fault_type}: {capability}")
    gate = capability.get("diagnosis_gate") or {}
    _assert_ok(gate.get("ready") is True, f"diagnosis gate should pass for {fault_type}: {capability}")
    if fault_type == "ServiceUnavailable":
        sources = {s.get("source") for s in suggestions}
        _assert_ok("kb" in sources, f"ServiceUnavailable should include KB actions: {suggestions}")
        _assert_ok(
            any("scale deployment" in s.get("command", "") and "--replicas=1" in s.get("command", "") for s in suggestions),
            f"ServiceUnavailable should include scale recovery action: {suggestions}",
        )

    run = client.post("/api/heal/execute", json={**payload, "dry_run": True}).json()
    _assert_ok(run.get("status") == "dry_run", f"execute did not dry-run for {fault_type}: {run}")
    _assert_ok(run.get("success") is True, f"execute failed for {fault_type}: {run}")
    _assert_ok(len(run.get("commands") or []) >= 1, f"no dry-run commands for {fault_type}: {run}")
    verification = run.get("verification") or {}
    _assert_ok(verification.get("status") == "skipped", f"dry-run verification should be skipped for {fault_type}: {run}")
    _assert_ok(verification.get("reason") == "dry_run", f"dry-run verification reason mismatch for {fault_type}: {run}")

    rollback_count = len(run.get("rollback_commands") or [])
    if rollback_count:
        rollback = client.post(
            f"/api/heal/runs/{run['id']}/rollback",
            json={"dry_run": True, "approved": True},
        ).json()
        _assert_ok(rollback.get("success") is True, f"rollback failed for {fault_type}: {rollback}")

    return run["id"], len(suggestions), rollback_count


def main() -> int:
    client = TestClient(app)
    created_run_ids: List[str] = []
    failures: List[Tuple[str, str]] = []
    recipes = client.get("/api/heal/recipes").json()
    fault_types = [r["fault_type"] for r in HEAL_RECIPES]
    _assert_ok(recipes.get("total") >= len(set(fault_types)), f"recipe api total mismatch: {recipes}")
    _assert_ok(bool(recipes.get("source")), f"recipe api missing source: {recipes}")
    _assert_ok(bool(recipes.get("version")), f"recipe api missing version: {recipes}")
    _assert_ok((recipes.get("validation") or {}).get("ok") is True, f"recipe validation failed: {recipes.get('validation')}")
    validation = client.get("/api/heal/recipes/validate").json()
    _assert_ok(validation.get("ok") is True, f"recipe validation endpoint failed: {validation}")

    print(f"recipes={recipes['total']} fault_types={len(set(fault_types))}")
    for fault_type in fault_types:
        try:
            run_id, suggestions, rollback_count = _exercise_fault_type(client, fault_type)
            created_run_ids.append(run_id)
            print(f"PASS {fault_type:<34} suggestions={suggestions:<2} rollback={rollback_count}")
        except Exception as exc:  # noqa: BLE001 - report all failures in one run
            failures.append((fault_type, str(exc)))
            print(f"FAIL {fault_type:<34} {exc}")

    safety = client.post(
        "/api/heal/execute",
        json={**_base_payload("DeploymentUnhealthy"), "dry_run": True, "commands": ["kill -9 1"]},
    )
    if safety.status_code == 400:
        print("PASS safety forbidden-command gate")
    else:
        failures.append(("safety", f"expected 400, got {safety.status_code}: {safety.text}"))

    no_diag = client.post(
        "/api/heal/execute",
        json={
            "fault_type": "DeploymentUnhealthy",
            "namespace": NAMESPACE,
            "deployment": DEPLOYMENT,
            "commands": [f"kubectl rollout status deployment/{DEPLOYMENT} -n {NAMESPACE}"],
            "dry_run": False,
            "approved": True,
        },
    )
    if no_diag.status_code == 400 and "diagnosis_gate" in no_diag.text:
        print("PASS real-execution diagnosis gate")
    else:
        failures.append(("diagnosis_gate", f"expected 400 diagnosis gate, got {no_diag.status_code}: {no_diag.text}"))

    low_conf_payload = _base_payload("DeploymentUnhealthy")
    low_conf_payload["diagnosis"]["confidence"] = 0.01
    low_conf = client.post(
        "/api/heal/execute",
        json={**low_conf_payload, "dry_run": False, "approved": True},
    )
    if low_conf.status_code == 400 and "low_confidence" in low_conf.text:
        print("PASS real-execution confidence gate")
    else:
        failures.append(("confidence_gate", f"expected 400 low confidence, got {low_conf.status_code}: {low_conf.text}"))

    _cleanup_heal_runs(created_run_ids)

    if failures:
        print("\nFailures:")
        for fault_type, detail in failures:
            print(f"- {fault_type}: {detail}")
        return 1
    print(f"\nAll self-healing dry-run E2E checks passed: {len(fault_types)} fault types")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
