#!/usr/bin/env python3
"""Run every registered K8s fault scenario against an isolated remote lab."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


LAB_DEPLOYMENTS = [
    "compose-post-service",
    "home-timeline-redis",
    "home-timeline-service",
    "nginx-thrift",
    "post-storage-mongodb",
    "social-graph-redis",
    "text-service",
    "url-shorten-service",
    "user-memcached",
    "user-timeline-service",
]
HELPER_PODS = [
    "cpu-stress",
    "mem-stress",
    "crash-test",
    "rca-distractor-crash",
    "compose-post-overrequest",
]


def _run(*args: str, timeout: int = 30, check: bool = True) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(args)}\n{result.stderr.strip()}")
    return result.stdout.strip()


def _kubectl(namespace: str, *args: str, timeout: int = 30, check: bool = True) -> str:
    return _run("kubectl", "-n", namespace, *args, timeout=timeout, check=check)


def _api(
    base_url: str,
    path: str,
    body: Optional[Dict[str, Any]] = None,
    timeout: int = 150,
) -> Dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="GET" if body is None else "POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API {path} returned HTTP {exc.code}: {detail}") from exc


def _deployment_state(namespace: str, name: str) -> Dict[str, int]:
    data = json.loads(_kubectl(namespace, "get", f"deployment/{name}", "-o", "json"))
    spec = data.get("spec") or {}
    status = data.get("status") or {}
    return {
        "desired": int(spec.get("replicas") or 0),
        "ready": int(status.get("readyReplicas") or 0),
        "available": int(status.get("availableReplicas") or 0),
    }


def _pod_state(namespace: str, name: str) -> Optional[Dict[str, Any]]:
    result = subprocess.run(
        ["kubectl", "-n", namespace, "get", f"pod/{name}", "-o", "json"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    statuses = (data.get("status") or {}).get("containerStatuses") or []
    waiting = (((statuses[0] if statuses else {}).get("state") or {}).get("waiting") or {}).get("reason", "")
    return {
        "phase": (data.get("status") or {}).get("phase", ""),
        "waiting_reason": waiting,
        "restart_count": sum(int(item.get("restartCount") or 0) for item in statuses),
    }


def _pid_marker(namespace: str, deployment: str, path: str = "/tmp/agenticsre-cpu-burn.pids") -> bool:
    result = subprocess.run(
        [
            "kubectl", "-n", namespace, "exec", f"deployment/{deployment}", "--", "sh", "-c",
            f"test -s {path} && for p in $(cat {path}); do kill -0 $p || exit 1; done",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    return result.returncode == 0


def _endpoint_count(namespace: str, service: str) -> int:
    data = json.loads(_kubectl(namespace, "get", f"endpoints/{service}", "-o", "json"))
    return sum(len(item.get("addresses") or []) for item in data.get("subsets") or [])


def _wait(predicate: Callable[[], bool], description: str, timeout_s: int = 30) -> None:
    deadline = time.time() + timeout_s
    last_error = ""
    while time.time() < deadline:
        try:
            if predicate():
                return
        except Exception as exc:  # noqa: BLE001 - retained for the final evidence message
            last_error = str(exc)
        time.sleep(1)
    raise RuntimeError(f"timed out waiting for {description}: {last_error}")


def _baseline(namespace: str, observe_namespace: str) -> Dict[str, Any]:
    deployments = {name: _deployment_state(namespace, name) for name in LAB_DEPLOYMENTS}
    endpoints = {name: _endpoint_count(namespace, name) for name in LAB_DEPLOYMENTS}
    observe = _deployment_state(observe_namespace, "kube-prom-kube-state-metrics")
    helpers = {name: _pod_state(namespace, name) for name in HELPER_PODS}
    return {
        "deployments": deployments,
        "endpoints": endpoints,
        "observe": observe,
        "helpers": helpers,
    }


def _baseline_ready(namespace: str, observe_namespace: str) -> bool:
    snapshot = _baseline(namespace, observe_namespace)
    deployments_ok = all(
        item["desired"] == 1 and item["ready"] == 1 and item["available"] == 1
        for item in snapshot["deployments"].values()
    )
    endpoints_ok = all(count >= 1 for count in snapshot["endpoints"].values())
    observe_ok = snapshot["observe"] == {"desired": 1, "ready": 1, "available": 1}
    helpers_ok = all(item is None for item in snapshot["helpers"].values())
    markers_ok = all(
        not _pid_marker(namespace, deployment)
        for deployment in ("compose-post-service", "text-service", "url-shorten-service")
    )
    return deployments_ok and endpoints_ok and observe_ok and helpers_ok and markers_ok


def _effect_evidence(scenario_id: str, namespace: str, observe_namespace: str) -> Dict[str, Any]:
    scale_targets = {
        "sn-network-delay": "nginx-thrift",
        "sn-svc-scale-down": "home-timeline-service",
        "sn-db-slow": "post-storage-mongodb",
        "sn-redis-down": "home-timeline-redis",
        "sn-memcached-down": "user-memcached",
        "sn-network-partition": "url-shorten-service",
        "sn-transient-cache-flap": "home-timeline-redis",
    }
    helper_targets = {
        "sn-cpu-stress": "cpu-stress",
        "sn-mem-pressure": "mem-stress",
        "sn-crashloop": "crash-test",
    }
    cpu_targets = {
        "sn-compose-cpu": "compose-post-service",
        "sn-gray-url-brownout": "url-shorten-service",
    }
    if scenario_id in scale_targets:
        target = scale_targets[scenario_id]
        state = _deployment_state(namespace, target)
        return {"kind": "replicas_zero", "target": target, "state": state, "confirmed": state["desired"] == 0}
    if scenario_id in helper_targets:
        target = helper_targets[scenario_id]
        state = _pod_state(namespace, target)
        confirmed = state is not None
        if scenario_id == "sn-crashloop":
            confirmed = bool(state and (state["restart_count"] > 0 or state["waiting_reason"] == "CrashLoopBackOff"))
        return {"kind": "helper_pod", "target": target, "state": state, "confirmed": confirmed}
    if scenario_id in cpu_targets:
        target = cpu_targets[scenario_id]
        confirmed = _pid_marker(namespace, target)
        return {"kind": "cpu_process", "target": target, "confirmed": confirmed}
    if scenario_id == "sn-multi-fault":
        cache = _deployment_state(namespace, "social-graph-redis")
        cpu = _pid_marker(namespace, "text-service")
        return {"kind": "compound", "cache": cache, "cpu": cpu, "confirmed": cache["desired"] == 0 and cpu}
    if scenario_id == "sn-staged-write-cascade":
        database = _deployment_state(namespace, "post-storage-mongodb")
        scheduled = _pid_marker(namespace, "compose-post-service", "/tmp/agenticsre-delayed-fault.pid")
        return {"kind": "staged", "database": database, "delayed_stage_scheduled": scheduled, "confirmed": database["desired"] == 0 and scheduled}
    if scenario_id == "sn-observability-blindspot":
        observe = _deployment_state(observe_namespace, "kube-prom-kube-state-metrics")
        cpu = _pid_marker(namespace, "compose-post-service")
        return {"kind": "cross_namespace", "observe": observe, "cpu": cpu, "confirmed": observe["desired"] == 0 and cpu}
    if scenario_id == "sn-stale-signal-distractor":
        helper = _pod_state(namespace, "rca-distractor-crash")
        cpu = _pid_marker(namespace, "compose-post-service")
        return {"kind": "distractor", "helper": helper, "cpu": cpu, "confirmed": helper is not None and cpu}
    if scenario_id == "sn-scaleout-capacity-failure":
        helper = _pod_state(namespace, "compose-post-overrequest")
        cpu = _pid_marker(namespace, "compose-post-service")
        pending = bool(helper and (helper["phase"] == "Pending" or helper["waiting_reason"]))
        return {"kind": "scheduling", "helper": helper, "cpu": cpu, "confirmed": pending and cpu}
    raise KeyError(f"missing evidence rule for {scenario_id}")


def _force_restore(namespace: str, observe_namespace: str) -> None:
    for name in HELPER_PODS:
        _kubectl(namespace, "delete", f"pod/{name}", "--ignore-not-found=true", timeout=30, check=False)
    _kubectl(namespace, "delete", "job", "-l", "app=agenticsre-bg-load", "--ignore-not-found=true", check=False)
    for name in LAB_DEPLOYMENTS:
        _kubectl(namespace, "scale", f"deployment/{name}", "--replicas=1", check=False)
    _kubectl(observe_namespace, "scale", "deployment/kube-prom-kube-state-metrics", "--replicas=1", check=False)
    for name in ("compose-post-service", "text-service", "url-shorten-service"):
        _kubectl(
            namespace,
            "exec", f"deployment/{name}", "--", "sh", "-c",
            "if [ -f /tmp/agenticsre-delayed-fault.pid ]; then kill $(cat /tmp/agenticsre-delayed-fault.pid) 2>/dev/null || true; fi; "
            "if [ -f /tmp/agenticsre-cpu-burn.pids ]; then xargs -r kill < /tmp/agenticsre-cpu-burn.pids 2>/dev/null || true; fi; "
            "rm -f /tmp/agenticsre-delayed-fault.pid /tmp/agenticsre-cpu-burn.pids",
            check=False,
        )


def run(args: argparse.Namespace) -> Dict[str, Any]:
    started_at = time.time()
    catalog = _api(args.base_url, "/api/faultlab/scenarios")
    precision = _api(args.base_url, "/api/faultlab/precision-scenarios")
    results: List[Dict[str, Any]] = []

    if len(catalog.get("scenarios") or []) != 17:
        raise RuntimeError("expected 17 registered K8s scenarios")
    if precision.get("scenario_count") != 18 or len(precision.get("method_counts") or {}) != 12:
        raise RuntimeError("expected 18 precision scenarios across 12 methods")

    for scenario in precision["scenarios"]:
        plan = _api(
            args.base_url,
            f"/api/faultlab/precision/plan/{scenario['id']}",
            {"dry_run": True, "namespace": args.namespace},
        )
        guarded = plan.get("guarded_plan") or {}
        if not guarded.get("ready") or guarded.get("real_execution_ready") is not True:
            raise RuntimeError(f"precision reviewed adapter is not ready: {scenario['id']}")

    _force_restore(args.namespace, args.observe_namespace)
    _wait(lambda: _baseline_ready(args.namespace, args.observe_namespace), "initial baseline", 120)

    selected_scenarios = [
        scenario for scenario in catalog["scenarios"]
        if not args.scenario_id or scenario["id"] == args.scenario_id
    ]
    if args.scenario_id and not selected_scenarios:
        raise RuntimeError(f"registered scenario not found: {args.scenario_id}")
    total = len(selected_scenarios)
    for index, scenario in enumerate(selected_scenarios, start=1):
        scenario_id = scenario["id"]
        item: Dict[str, Any] = {"scenario_id": scenario_id, "started_at": time.time()}
        source_fault_run_id = ""
        try:
            before = _baseline(args.namespace, args.observe_namespace)
            preview = _api(
                args.base_url,
                f"/api/faultlab/inject/{scenario_id}",
                {"dry_run": True, "namespace": args.namespace},
            )
            if not (preview.get("guarded_plan") or {}).get("ready"):
                raise RuntimeError("guarded Dry-run plan is not ready")

            injected = _api(
                args.base_url,
                f"/api/faultlab/inject/{scenario_id}",
                {
                    "dry_run": False,
                    "confirm": True,
                    "experiment_ack": True,
                    "namespace": args.namespace,
                    "duration_s": 30,
                },
            )
            source_fault_run_id = str(injected.get("fault_run_id") or "")
            if injected.get("status") != "executed" or not source_fault_run_id:
                raise RuntimeError(f"injection failed: {json.dumps(injected, ensure_ascii=False)}")
            if not all(result.get("ok") for result in injected.get("results") or []):
                raise RuntimeError("one or more registered injection commands failed")

            evidence: Dict[str, Any] = {}
            def fault_confirmed() -> bool:
                evidence.update(_effect_evidence(scenario_id, args.namespace, args.observe_namespace))
                return bool(evidence.get("confirmed"))

            try:
                _wait(
                    fault_confirmed,
                    f"fault effect for {scenario_id}",
                    35,
                )
            except RuntimeError as exc:
                raise RuntimeError(
                    f"{exc}; last_evidence={json.dumps(evidence, ensure_ascii=False)}"
                ) from exc
            if scenario_id == "sn-staged-write-cascade":
                time.sleep(47)
                evidence["second_stage_cpu"] = _pid_marker(args.namespace, "compose-post-service")
                if not evidence["second_stage_cpu"]:
                    raise RuntimeError("staged CPU fault did not activate")

            healed = _api(
                args.base_url,
                f"/api/faultlab/heal/{scenario_id}",
                {
                    "incident_id": f"remote-e2e-{scenario_id}",
                    "fault_run_id": source_fault_run_id,
                    "namespace": args.namespace,
                    "diagnosis": {
                        "fault_type": (scenario.get("expected") or {}).get("fault_type") or scenario.get("fault_type"),
                        "root_cause": f"Live target evidence confirmed the registered failure on {scenario.get('target_service')}",
                        "root_cause_component": ((injected.get("guarded_plan") or {}).get("action") or {}).get("target_entity_id"),
                        "confidence": 0.96,
                    },
                    "checkpoint": {"summary": before},
                },
            )
            if healed.get("status") != "planned":
                raise RuntimeError(f"transactional healing was blocked: {json.dumps(healed, ensure_ascii=False)}")

            cleaned = _api(
                args.base_url,
                f"/api/faultlab/cleanup/{scenario_id}",
                {
                    "dry_run": False,
                    "confirm": True,
                    "fault_run_id": source_fault_run_id,
                    "namespace": args.namespace,
                },
            )
            if cleaned.get("status") != "executed":
                raise RuntimeError(f"cleanup failed: {json.dumps(cleaned, ensure_ascii=False)}")
            _wait(lambda: _baseline_ready(args.namespace, args.observe_namespace), f"recovery for {scenario_id}", 120)
            after = _baseline(args.namespace, args.observe_namespace)
            item.update({
                "status": "passed",
                "fault_run_id": source_fault_run_id,
                "cleanup_run_id": cleaned.get("fault_run_id"),
                "evidence": evidence,
                "transactional_heal_operation": ((healed.get("transactional_heal") or {}).get("action") or {}).get("operation"),
                "target_scopes": ((injected.get("guarded_plan") or {}).get("action") or {}).get("parameters", {}).get("target_scopes"),
                "recovery": after,
            })
        except Exception as exc:  # noqa: BLE001 - every failure is recorded after forced recovery
            item.update({"status": "failed", "error": str(exc), "fault_run_id": source_fault_run_id})
            try:
                _api(
                    args.base_url,
                    f"/api/faultlab/cleanup/{scenario_id}",
                    {"dry_run": False, "confirm": True, "fault_run_id": source_fault_run_id, "namespace": args.namespace},
                )
            except Exception:
                pass
            _force_restore(args.namespace, args.observe_namespace)
            _wait(lambda: _baseline_ready(args.namespace, args.observe_namespace), f"forced recovery for {scenario_id}", 120)
        finally:
            item["duration_s"] = round(time.time() - item["started_at"], 3)
            results.append(item)
            print(f"[{index:02d}/{total:02d}] {scenario_id}: {item['status']} ({item['duration_s']}s)", flush=True)

    finished_at = time.time()
    artifact = {
        "schema": "agenticsre.remote-faultlab-e2e.v1",
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_s": round(finished_at - started_at, 3),
        "base_url": args.base_url,
        "namespace": args.namespace,
        "observe_namespace": args.observe_namespace,
        "precision": {
            "scenario_count": precision["scenario_count"],
            "method_count": len(precision["method_counts"]),
            "execution_mode": precision["execution_mode"],
            "status": "contract_validated",
        },
        "regular": {
            "scenario_count": len(results),
            "passed": sum(item["status"] == "passed" for item in results),
            "failed": sum(item["status"] != "passed" for item in results),
            "results": results,
        },
        "final_baseline": _baseline(args.namespace, args.observe_namespace),
    }
    output = Path(args.artifact)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18084")
    parser.add_argument("--namespace", default="test-social-network")
    parser.add_argument("--observe-namespace", default="agenticsre-faultlab-observe")
    parser.add_argument("--artifact", default="data/validation/remote_faultlab_e2e.json")
    parser.add_argument("--scenario-id", default="", help="Run one registered scenario after a failed full run")
    args = parser.parse_args()
    artifact = run(args)
    print(json.dumps({
        "passed": artifact["regular"]["passed"],
        "failed": artifact["regular"]["failed"],
        "precision": artifact["precision"],
        "artifact": args.artifact,
    }, ensure_ascii=False), flush=True)
    return 0 if artifact["regular"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
