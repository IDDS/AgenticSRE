#!/usr/bin/env python3
"""Run an AgenticSRE fault injection -> monitoring -> diagnosis -> healing loop.

This script targets the OpenTelemetry Demo deployment in Kubernetes. It uses
Kubernetes commands for controlled fault injection and observation, and uses
AgenticSRE's self-healing APIs for capability retrieval and real execution.

Default scenario:
  ServiceUnavailable by scaling deployment/frontend to zero replicas, then
  restoring it through AgenticSRE's diagnosis-gated self-healing endpoint.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List
from urllib import error, request, parse


BASE_URL = os.environ.get("AGENTICSRE_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
NAMESPACE = os.environ.get("OTEL_DEMO_NAMESPACE", "otel-demo")
DEPLOYMENT = os.environ.get("OTEL_DEMO_TARGET_DEPLOYMENT", "frontend")
SERVICE = os.environ.get("OTEL_DEMO_TARGET_SERVICE", "frontend")
FRONTEND_PROXY_PORT = int(os.environ.get("OTEL_DEMO_FRONTEND_NODEPORT", "30088"))
RUN_REAL = os.environ.get("AGENTICSRE_RUN_OTEL_LOOP") == "1"


def sh(cmd: str, timeout: int = 120, check: bool = True) -> str:
    proc = subprocess.run(cmd, shell=True, text=True, capture_output=True, timeout=timeout)
    if check and proc.returncode != 0:
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


def api_get(path: str, timeout: int = 90) -> Dict[str, Any]:
    return api("GET", path, timeout=timeout)


def wait_for(condition: Callable[[], bool], label: str, timeout: int = 120, interval: int = 3) -> None:
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


def get_json(cmd: str) -> Dict[str, Any]:
    return json.loads(sh(cmd))


def deployment_replicas() -> Dict[str, int]:
    obj = get_json(f"kubectl -n {NAMESPACE} get deployment {DEPLOYMENT} -o json")
    return {
        "desired": int((obj.get("spec") or {}).get("replicas") or 0),
        "ready": int((obj.get("status") or {}).get("readyReplicas") or 0),
        "available": int((obj.get("status") or {}).get("availableReplicas") or 0),
    }


def endpoints_ready() -> bool:
    out = sh(f"kubectl -n {NAMESPACE} get endpoints {SERVICE} -o jsonpath='{{.subsets[*].addresses[*].ip}}'", check=False)
    return bool(out.strip())


def frontend_http_code() -> str:
    node_ip = sh("kubectl get node -o jsonpath='{.items[0].status.addresses[?(@.type==\"InternalIP\")].address}'")
    return sh(f"curl -s -o /dev/null -w '%{{http_code}}' http://{node_ip}:{FRONTEND_PROXY_PORT}/", timeout=20, check=False)


def pod_line(component: str) -> str:
    return sh(f"kubectl -n {NAMESPACE} get pods -l app.kubernetes.io/component={component} --no-headers", check=False)


def prom_first_value(query: str, source: str = "cluster") -> str:
    encoded = parse.quote(query, safe="")
    data = api_get(f"/api/prometheus/query?query={encoded}&source={source}", timeout=45)
    if data.get("error"):
        return f"error: {data['error']}"
    results = data.get("results") or []
    if not results:
        return "n/a"
    value = (results[0].get("value") or ["", "n/a"])[1]
    try:
        return str(round(float(value), 4))
    except Exception:
        return str(value)


def alerts_summary() -> Dict[str, Any]:
    try:
        data = api_get(f"/api/alerts/list?namespace={parse.quote(NAMESPACE, safe='')}", timeout=45)
        alerts = data.get("alerts") or []
        return {
            "count": len(alerts),
            "top": [
                {
                    "source": a.get("source"),
                    "severity": a.get("severity"),
                    "title": a.get("title") or a.get("name"),
                    "service": a.get("service"),
                }
                for a in alerts[:5]
            ],
        }
    except Exception as exc:
        return {"count": "unknown", "error": str(exc)}


def metrics_snapshot() -> Dict[str, Any]:
    return {
        "frontend_requests_per_s": prom_first_value(
            'sum(rate(app_frontend_requests_total{service_name="frontend"}[5m]))',
            source="otel",
        ),
        "frontend_latency_p99_ms": prom_first_value(
            'histogram_quantile(0.99, sum by(le)(rate(http_server_duration_milliseconds_bucket{service_name="frontend"}[5m])))',
            source="otel",
        ),
        "frontend_5xx_per_s": prom_first_value(
            'sum(rate(http_server_duration_milliseconds_count{service_name="frontend",http_status_code=~"5.."}[5m])) or vector(0)',
            source="otel",
        ),
        "frontend_cpu": prom_first_value(
            f'sum(rate(container_cpu_usage_seconds_total{{namespace="{NAMESPACE}",pod=~"frontend-.*"}}[1m]))'
        ),
        "frontend_memory_mb": prom_first_value(
            f'sum(container_memory_working_set_bytes{{namespace="{NAMESPACE}",pod=~"frontend-.*"}}) / 1024 / 1024'
        ),
    }


def snapshot(stage: str) -> Dict[str, Any]:
    return {
        "stage": stage,
        "replicas": deployment_replicas(),
        "endpoints_ready": endpoints_ready(),
        "frontend_http_code": frontend_http_code(),
        "frontend_pod": pod_line(DEPLOYMENT),
        "frontend_proxy_pod": pod_line("frontend-proxy"),
        "load_generator_pod": pod_line("load-generator"),
        "metrics": metrics_snapshot(),
        "alerts": alerts_summary(),
    }


def print_stage(data: Dict[str, Any]) -> None:
    print(f"\n=== {data['stage']} ===", flush=True)
    print(json.dumps(data, indent=2, ensure_ascii=False), flush=True)


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


def otel_payload() -> Dict[str, Any]:
    return {
        "source": "otel-demo-agenticsre-loop",
        "namespace": NAMESPACE,
        "fault_type": "ServiceUnavailable",
        "service": SERVICE,
        "deployment": DEPLOYMENT,
        "message": f"OpenTelemetry Demo {SERVICE} has no ready endpoints after controlled scale-to-zero injection",
        "diagnosis": {
            "fault_type": "ServiceUnavailable",
            "root_cause": f"{DEPLOYMENT} deployment in namespace {NAMESPACE} was scaled to zero replicas",
            "root_cause_component": DEPLOYMENT,
            "affected_services": [SERVICE, "frontend-proxy"],
            "remediation_suggestion": f"scale deployment/{DEPLOYMENT} back to one replica and verify rollout/endpoints",
            "confidence": 0.97,
            "evidence_summary": {
                "deployment": "spec.replicas=0 and availableReplicas=0",
                "endpoints": f"service/{SERVICE} has no ready endpoint addresses",
                "user_impact": "frontend-proxy returns non-2xx or cannot route to frontend",
            },
        },
    }


def run_loop() -> Dict[str, Any]:
    if not RUN_REAL:
        print("SKIP real otel loop; set AGENTICSRE_RUN_OTEL_LOOP=1 to run")
        return {"skipped": True}

    original_config = api("GET", "/api/platform/config").get("stored") or {}
    result: Dict[str, Any] = {
        "scenario": "otel-demo ServiceUnavailable scale-to-zero",
        "namespace": NAMESPACE,
        "deployment": DEPLOYMENT,
        "service": SERVICE,
        "stages": [],
    }
    try:
        set_heal_execution(True)

        baseline = snapshot("baseline_with_background_load")
        print_stage(baseline)
        result["stages"].append(baseline)
        if baseline["replicas"]["ready"] < 1 or not baseline["endpoints_ready"]:
            raise RuntimeError(f"baseline is not healthy: {baseline}")

        sh(f"kubectl -n {NAMESPACE} scale deployment/{DEPLOYMENT} --replicas=0")
        wait_for(lambda: deployment_replicas()["desired"] == 0, "deployment scaled to zero", timeout=30)
        wait_for(lambda: not endpoints_ready(), "service endpoints disappear", timeout=60)
        time.sleep(10)
        injected = snapshot("scale_to_zero_injected")
        print_stage(injected)
        result["stages"].append(injected)

        payload = otel_payload()
        capability = api("POST", "/api/heal/capability", payload)
        capability_stage = {
            "stage": "diagnosis_and_capability",
            "fault_type": capability.get("fault_type"),
            "diagnosis_gate": capability.get("diagnosis_gate"),
            "strategy_trace": capability.get("strategy_trace"),
            "suggestions": capability.get("suggestions", [])[:5],
        }
        print_stage(capability_stage)
        result["stages"].append(capability_stage)
        if not (capability.get("diagnosis_gate") or {}).get("ready"):
            raise RuntimeError(f"diagnosis gate did not pass: {capability.get('diagnosis_gate')}")

        commands = [s.get("command", "") for s in capability.get("suggestions", [])]
        scale_cmd = next(cmd for cmd in commands if "scale deployment" in cmd and "--replicas=1" in cmd)
        run = api("POST", "/api/heal/execute", {
            **payload,
            "dry_run": False,
            "approved": True,
            "commands": [scale_cmd],
            "rollback_commands": [f"kubectl scale deployment/{DEPLOYMENT} -n {NAMESPACE} --replicas=0"],
        }, timeout=180)
        healed_stage = {
            "stage": "self_healing_executed",
            "run_id": run.get("id"),
            "success": run.get("success"),
            "verification": run.get("verification"),
            "commands": run.get("commands"),
        }
        print_stage(healed_stage)
        result["stages"].append(healed_stage)
        if run.get("success") is not True or (run.get("verification") or {}).get("recovered") is not True:
            raise RuntimeError(f"healing did not recover: {run}")

        wait_for(lambda: deployment_replicas()["ready"] >= 1, "deployment ready after healing", timeout=120)
        wait_for(endpoints_ready, "service endpoints after healing", timeout=60)
        time.sleep(10)
        recovered = snapshot("recovered")
        print_stage(recovered)
        result["stages"].append(recovered)
        result["passed"] = True
        return result
    finally:
        try:
            if deployment_replicas()["desired"] == 0:
                sh(f"kubectl -n {NAMESPACE} scale deployment/{DEPLOYMENT} --replicas=1", check=False)
                sh(f"kubectl -n {NAMESPACE} rollout status deployment/{DEPLOYMENT} --timeout=120s", timeout=140, check=False)
        finally:
            restore_platform_config(original_config)


def main() -> int:
    result = run_loop()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("skipped") or result.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
