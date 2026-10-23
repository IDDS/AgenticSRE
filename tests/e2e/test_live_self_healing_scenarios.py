#!/usr/bin/env python3
"""Live self-healing validation against a real Kubernetes cluster.

This test intentionally mutates a dedicated namespace. It is disabled unless
AGENTICSRE_RUN_LIVE_HEALING=1 is set.

Run on the deployment host or through a shell with kubectl access:
    AGENTICSRE_RUN_LIVE_HEALING=1 \
    AGENTICSRE_BASE_URL=http://127.0.0.1:8080 \
    python3 tests/e2e/test_live_self_healing_scenarios.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional
from urllib import request, error


BASE_URL = os.environ.get("AGENTICSRE_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
NAMESPACE = os.environ.get("AGENTICSRE_LIVE_HEAL_NS", "fault-test")
APP = os.environ.get("AGENTICSRE_LIVE_HEAL_APP", "agenticsre-heal-e2e")
IMAGE = os.environ.get("AGENTICSRE_LIVE_HEAL_IMAGE", "nginx:1.27-alpine")
MISSING_IMAGE = os.environ.get("AGENTICSRE_LIVE_HEAL_MISSING_IMAGE", "nginx:agenticsre-missing-tag")


def sh(cmd: str, timeout: int = 120) -> str:
    proc = subprocess.run(cmd, shell=True, text=True, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed: {cmd}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    return proc.stdout.strip()


def api(method: str, path: str, body: Dict[str, Any] | None = None, timeout: int = 120) -> Dict[str, Any]:
    data = json.dumps(body or {}).encode("utf-8") if body is not None else None
    req = request.Request(
        f"{BASE_URL}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            detail = json.loads(raw)
        except Exception:
            detail = raw
        raise RuntimeError(f"{method} {path} failed: {exc.code} {detail}") from exc


def wait_for(condition, label: str, timeout: int = 90, interval: int = 3) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            if condition():
                return
        except Exception as exc:
            last = str(exc)
        time.sleep(interval)
    raise TimeoutError(f"timeout waiting for {label}: {last}")


def setup_workload() -> None:
    sh(f"kubectl create ns {NAMESPACE} --dry-run=client -o yaml | kubectl apply -f -")
    sh(f"kubectl -n {NAMESPACE} create deployment {APP} --image={IMAGE} --dry-run=client -o yaml | kubectl apply -f -")
    sh(f"kubectl -n {NAMESPACE} expose deployment {APP} --port=80 --target-port=80 --dry-run=client -o yaml | kubectl apply -f -")
    sh(f"kubectl -n {NAMESPACE} scale deployment/{APP} --replicas=1")
    sh(f"kubectl -n {NAMESPACE} rollout status deployment/{APP} --timeout=90s", timeout=100)


def cleanup_workload() -> None:
    for pod in (f"{APP}-crash", f"{APP}-imagepull"):
        sh(f"kubectl -n {NAMESPACE} delete pod {pod} --ignore-not-found=true", timeout=60)
    sh(f"kubectl -n {NAMESPACE} delete service {APP} --ignore-not-found=true", timeout=60)
    sh(f"kubectl -n {NAMESPACE} delete deployment {APP} --ignore-not-found=true", timeout=60)


def set_heal_execution(enabled: bool) -> None:
    if enabled:
        api("PUT", "/api/platform/config", {
            "remediation": {
                "enabled": True,
                "recommend_only": False,
                "dry_run": False,
                "require_approval": True,
                "confidence_threshold": 0.85,
                "max_steps": 5,
                "max_auto_risk_level": "medium",
            }
        })
    else:
        api("DELETE", "/api/platform/config")


def restore_platform_config(original: Dict[str, Any]) -> None:
    api("DELETE", "/api/platform/config")
    if original:
        api("PUT", "/api/platform/config", original)


def diagnosis(
    fault_type: str,
    root_cause: str,
    remediation: str,
    evidence: Dict[str, str],
    *,
    component: Optional[str] = APP,
    services: Optional[List[str]] = None,
) -> Dict[str, Any]:
    if services is None:
        services = [APP] if component else []
    return {
        "fault_type": fault_type,
        "root_cause": root_cause,
        "root_cause_component": component or "",
        "affected_services": services,
        "remediation_suggestion": remediation,
        "confidence": 0.96,
        "evidence_summary": evidence,
    }


def service_unavailable_scale_to_zero() -> Dict[str, Any]:
    print("SCENARIO ServiceUnavailable scale-to-zero")
    sh(f"kubectl -n {NAMESPACE} scale deployment/{APP} --replicas=0")
    time.sleep(3)
    dep = sh(f"kubectl -n {NAMESPACE} get deployment {APP} -o json")
    dep_obj = json.loads(dep)
    assert int((dep_obj.get("spec") or {}).get("replicas") or 0) == 0

    payload = {
        "source": "live-self-healing-e2e",
        "namespace": NAMESPACE,
        "fault_type": "ServiceUnavailable",
        "service": APP,
        "deployment": APP,
        "message": f"503 ServiceUnavailable no ready endpoints for {APP}",
        "diagnosis": diagnosis(
            "ServiceUnavailable",
            f"{APP} deployment was scaled to zero replicas, leaving the service without ready endpoints",
            f"scale deployment/{APP} back to one replica and verify endpoints are restored",
            {"deployment": "deployment shows replicas=0", "endpoints": "service has no ready endpoints"},
        ),
    }
    capability = api("POST", "/api/heal/capability", payload)
    assert capability["diagnosis_gate"]["ready"] is True
    assert capability["strategy_trace"]["recipe_matched"] is True
    commands = [s["command"] for s in capability["suggestions"]]
    scale_cmd = next(cmd for cmd in commands if "scale deployment" in cmd and "--replicas=1" in cmd)
    run = api("POST", "/api/heal/execute", {
        **payload,
        "dry_run": False,
        "approved": True,
        "commands": [scale_cmd],
        "rollback_commands": [f"kubectl scale deployment/{APP} -n {NAMESPACE} --replicas=0"],
    }, timeout=150)
    assert run["success"] is True
    assert run["verification"]["recovered"] is True
    wait_for(
        lambda: bool(sh(f"kubectl -n {NAMESPACE} get endpoints {APP} -o jsonpath='{{.subsets[*].addresses[*].ip}}'")),
        "service endpoints",
        timeout=30,
    )
    return {"scenario": "ServiceUnavailable", "run_id": run["id"], "verification": run["verification"]}


def pod_crashloop_delete_pod() -> Dict[str, Any]:
    print("SCENARIO PodCrashLoop delete-crashing-pod")
    crash = f"{APP}-crash"
    sh(f"kubectl -n {NAMESPACE} run {crash} --image={IMAGE} --restart=Never --command -- /bin/sh -c 'exit 1'")
    wait_for(lambda: "Error" in sh(f"kubectl -n {NAMESPACE} get pod {crash} --no-headers"), "crash pod error", timeout=60)

    payload = {
        "source": "live-self-healing-e2e",
        "namespace": NAMESPACE,
        "fault_type": "PodCrashLoop",
        "pod": crash,
        "message": f"CrashLoopBackOff Back-off restarting failed container {crash}",
        "diagnosis": diagnosis(
            "PodCrashLoop",
            f"{crash} exits immediately after start and should be deleted after evidence collection",
            f"delete pod/{crash} after collecting describe/log evidence",
            {"pod": "pod status Error", "events": "container terminated with non-zero exit"},
            component="",
            services=[],
        ),
    }
    capability = api("POST", "/api/heal/capability", payload)
    assert capability["diagnosis_gate"]["ready"] is True
    commands = [s["command"] for s in capability["suggestions"]]
    delete_cmd = next(cmd for cmd in commands if f"delete pod {crash}" in cmd)
    run = api("POST", "/api/heal/execute", {
        **payload,
        "dry_run": False,
        "approved": True,
        "risk_ack": True,
        "commands": [delete_cmd],
    }, timeout=150)
    assert run["success"] is True
    wait_for(lambda: sh(f"kubectl -n {NAMESPACE} get pod {crash} --ignore-not-found") == "", "crash pod deleted", timeout=30)
    return {"scenario": "PodCrashLoop", "run_id": run["id"], "verification": run["verification"]}


def image_pull_error_delete_pod() -> Dict[str, Any]:
    print("SCENARIO ImagePullError delete-failed-pod")
    pod = f"{APP}-imagepull"
    sh(f"kubectl -n {NAMESPACE} run {pod} --image={MISSING_IMAGE} --restart=Never")
    wait_for(
        lambda: any(
            state in sh(f"kubectl -n {NAMESPACE} get pod {pod} --no-headers")
            for state in ("ErrImagePull", "ImagePullBackOff")
        ),
        "image pull failure",
        timeout=120,
    )

    payload = {
        "source": "live-self-healing-e2e",
        "namespace": NAMESPACE,
        "fault_type": "ImagePullError",
        "pod": pod,
        "message": f"Failed to pull image {MISSING_IMAGE} for pod {pod}",
        "diagnosis": diagnosis(
            "ImagePullError",
            f"{pod} cannot pull image {MISSING_IMAGE}; the pod is unrecoverable without image correction",
            f"delete pod/{pod} after confirming the image pull event",
            {"pod": "pod status ImagePullBackOff", "events": f"failed to pull image {MISSING_IMAGE}"},
            component="",
            services=[],
        ),
    }
    capability = api("POST", "/api/heal/capability", payload)
    assert capability["diagnosis_gate"]["ready"] is True
    commands = [s["command"] for s in capability["suggestions"]]
    delete_cmd = next(cmd for cmd in commands if f"delete pod {pod}" in cmd)
    run = api("POST", "/api/heal/execute", {
        **payload,
        "dry_run": False,
        "approved": True,
        "risk_ack": True,
        "commands": [delete_cmd],
    }, timeout=150)
    assert run["success"] is True
    wait_for(lambda: sh(f"kubectl -n {NAMESPACE} get pod {pod} --ignore-not-found") == "", "image pull pod deleted", timeout=30)
    return {"scenario": "ImagePullError", "run_id": run["id"], "verification": run["verification"]}


def main() -> int:
    if os.environ.get("AGENTICSRE_RUN_LIVE_HEALING") != "1":
        print("SKIP live self-healing scenarios; set AGENTICSRE_RUN_LIVE_HEALING=1 to run")
        return 0

    results: List[Dict[str, Any]] = []
    original_config = api("GET", "/api/platform/config").get("stored") or {}
    try:
        setup_workload()
        set_heal_execution(True)
        results.append(service_unavailable_scale_to_zero())
        results.append(pod_crashloop_delete_pod())
        results.append(image_pull_error_delete_pod())
    finally:
        try:
            restore_platform_config(original_config)
        finally:
            cleanup_workload()

    print(json.dumps({"passed": len(results), "results": results}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
