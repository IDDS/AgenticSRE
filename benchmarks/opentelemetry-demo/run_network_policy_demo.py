#!/usr/bin/env python3
"""Recordable AgenticSRE network fault demo for OpenTelemetry Demo.

The cluster uses ChaosMesh for the actual network fault because the current CNI
does not enforce Kubernetes NetworkPolicy. The injected object is a temporary
NetworkChaos named `agenticsre-demo-frontend-partition`, labelled
`purpose=agenticsre-demo`.

Loop:
1. Capture baseline business/system/alert snapshots under background load.
2. Inject a ChaosMesh partition between frontend-proxy and frontend.
3. Observe HTTP and metric state changes.
4. Send a high-confidence NetworkPolicyDeny/network-configuration diagnosis to
   AgenticSRE.
5. Let AgenticSRE retrieve and execute the safe demo remediation: delete the
   temporary NetworkChaos.
6. Verify recovery and print a concise recordable timeline.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import time
from typing import Any, Callable, Dict, List
from urllib import error, request, parse


BASE_URL = os.environ.get("AGENTICSRE_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
AUTH_TOKEN = os.environ.get("AGENTICSRE_AUTH_TOKEN", "").strip()
NAMESPACE = os.environ.get("OTEL_DEMO_NAMESPACE", "otel-demo")
DEPLOYMENT = os.environ.get("OTEL_DEMO_TARGET_DEPLOYMENT", "frontend")
SERVICE = os.environ.get("OTEL_DEMO_TARGET_SERVICE", "frontend")
SOURCE_COMPONENT = os.environ.get("OTEL_DEMO_NETWORK_SOURCE", "frontend-proxy")
CHAOS = os.environ.get("OTEL_DEMO_NETWORK_CHAOS", "agenticsre-demo-frontend-partition")
NODE_PORT = int(os.environ.get("OTEL_DEMO_FRONTEND_NODEPORT", "30088"))
RUN_REAL = os.environ.get("AGENTICSRE_RUN_NETWORK_DEMO") == "1"
FAULT_HOLD_SECONDS = int(os.environ.get("AGENTICSRE_NETWORK_FAULT_HOLD_SECONDS", "90"))
RECOVERY_HOLD_SECONDS = int(os.environ.get("AGENTICSRE_NETWORK_RECOVERY_HOLD_SECONDS", "5"))
METRIC_RATE_WINDOW = os.environ.get("AGENTICSRE_DEMO_RATE_WINDOW", "2m")


def sh(cmd: str, timeout: int = 120, check: bool = True) -> str:
    proc = subprocess.run(cmd, shell=True, text=True, capture_output=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(f"command failed: {cmd}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    return proc.stdout.strip()


def api(method: str, path: str, body: Dict[str, Any] | None = None, timeout: int = 120) -> Dict[str, Any]:
    data = json.dumps(body or {}).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {AUTH_TOKEN}"
    req = request.Request(f"{BASE_URL}{path}", data=data, method=method, headers=headers)
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


def node_ip() -> str:
    return sh("kubectl get node -o jsonpath='{.items[0].status.addresses[?(@.type==\"InternalIP\")].address}'")


def http_code() -> str:
    return sh(f"curl -s -o /dev/null -w '%{{http_code}}' http://{node_ip()}:{NODE_PORT}/", timeout=20, check=False)


def pod_line(component: str) -> str:
    return sh(f"kubectl -n {NAMESPACE} get pods -l app.kubernetes.io/component={component} --no-headers", check=False)


def chaos_exists() -> bool:
    return bool(sh(f"kubectl -n {NAMESPACE} get networkchaos {CHAOS} --ignore-not-found", check=False))


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
        # Use lightweight detection here. /api/alerts/scan performs LLM alert
        # compression and is better triggered interactively from the UI.
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
            f'sum(rate(app_frontend_requests_total{{service_name="frontend"}}[{METRIC_RATE_WINDOW}]))',
            source="otel",
        ),
        "frontend_latency_p99_ms": prom_first_value(
            f'histogram_quantile(0.99, sum by(le)(rate(http_server_duration_milliseconds_bucket{{service_name="frontend"}}[{METRIC_RATE_WINDOW}])))',
            source="otel",
        ),
        "frontend_5xx_per_s": prom_first_value(
            f'(sum(rate(app_frontend_requests_total{{service_name="frontend",status=~"5.."}}[{METRIC_RATE_WINDOW}])) or vector(0)) + (max(httpcheck_status{{service_name="frontend-proxy",http_status_class="5xx"}}) or vector(0))',
            source="otel",
        ),
        "frontend_proxy_5xx_status": prom_first_value(
            'max(httpcheck_status{service_name="frontend-proxy",http_status_class="5xx"}) or vector(0)',
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
        "event_ts": int(time.time()),
        "http_code": http_code(),
        "chaos_exists": chaos_exists(),
        "frontend_pod": pod_line(DEPLOYMENT),
        "frontend_proxy_pod": pod_line(SOURCE_COMPONENT),
        "load_generator_pod": pod_line("load-generator"),
        "metrics": metrics_snapshot(),
        "alerts": alerts_summary(),
    }


def print_stage(data: Dict[str, Any]) -> None:
    print(f"\n=== {data['stage']} ===", flush=True)
    print("AGENTICSRE_STAGE_EVENT " + json.dumps({
        "stage": data.get("stage"),
        "event_ts": data.get("event_ts"),
        "http_code": data.get("http_code"),
    }, ensure_ascii=False), flush=True)
    print(json.dumps(data, indent=2, ensure_ascii=False), flush=True)


def apply_chaos() -> None:
    manifest = textwrap.dedent(f"""
    apiVersion: chaos-mesh.org/v1alpha1
    kind: NetworkChaos
    metadata:
      name: {CHAOS}
      namespace: {NAMESPACE}
      labels:
        purpose: agenticsre-demo
        managed-by: agenticsre
    spec:
      action: partition
      mode: all
      selector:
        namespaces:
          - {NAMESPACE}
        labelSelectors:
          app.kubernetes.io/component: {SOURCE_COMPONENT}
      direction: both
      target:
        mode: all
        selector:
          namespaces:
            - {NAMESPACE}
          labelSelectors:
            app.kubernetes.io/component: {DEPLOYMENT}
    """).strip()
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(manifest)
        path = fh.name
    try:
        sh(f"kubectl apply -f {path}")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def cleanup_chaos() -> None:
    sh(f"kubectl -n {NAMESPACE} delete networkchaos {CHAOS} --ignore-not-found=true", check=False)


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


def diagnosis_payload() -> Dict[str, Any]:
    return {
        "source": "otel-demo-network-demo",
        "namespace": NAMESPACE,
        "fault_type": "NetworkPolicyDeny",
        "service": SERVICE,
        "deployment": DEPLOYMENT,
        "network_chaos": CHAOS,
        "message": f"ChaosMesh NetworkChaos {CHAOS} partitions {SOURCE_COMPONENT} and {DEPLOYMENT}; frontend proxy returns 503",
        "diagnosis": {
            "fault_type": "NetworkPolicyDeny",
            "root_cause": f"Temporary agenticsre-demo ChaosMesh NetworkChaos/{CHAOS} partitions {SOURCE_COMPONENT} from {DEPLOYMENT}",
            "root_cause_component": DEPLOYMENT,
            "network_chaos": CHAOS,
            "affected_services": [SERVICE, SOURCE_COMPONENT],
            "remediation_suggestion": f"delete temporary NetworkChaos/{CHAOS} after approval and verify frontend HTTP recovery",
            "confidence": 0.97,
            "evidence_summary": {
                "network_chaos": CHAOS,
                "chaos_label": "purpose=agenticsre-demo",
                "symptom": "frontend proxy changes from HTTP 200 to 503 after ChaosMesh partition injection",
                "background_load": "otel-demo load-generator is running and continuously sending requests",
            },
        },
    }


def run_demo() -> Dict[str, Any]:
    if not RUN_REAL:
        print("SKIP network demo; set AGENTICSRE_RUN_NETWORK_DEMO=1 to run")
        return {"skipped": True}

    original_config = api("GET", "/api/platform/config").get("stored") or {}
    stages: List[Dict[str, Any]] = []
    try:
        set_heal_execution(True)
        cleanup_chaos()
        wait_for(lambda: http_code() == "200", "baseline frontend HTTP 200", timeout=90)
        baseline = snapshot("baseline_with_background_load")
        print_stage(baseline)
        stages.append(baseline)

        print("\nInjecting ChaosMesh NetworkChaos partition...", flush=True)
        apply_chaos()
        wait_for(lambda: chaos_exists(), "NetworkChaos exists", timeout=20)
        wait_for(lambda: http_code() != "200", "frontend HTTP degraded by network chaos", timeout=90)
        print(f"Holding network fault for {FAULT_HOLD_SECONDS}s so Prometheus curves show a clear degraded interval...", flush=True)
        time.sleep(FAULT_HOLD_SECONDS)
        injected = snapshot("network_fault_injected")
        print_stage(injected)
        stages.append(injected)

        payload = diagnosis_payload()
        capability = api("POST", "/api/heal/capability", payload)
        capability_stage = {
            "stage": "diagnosis_and_self_healing_plan",
            "fault_type": capability.get("fault_type"),
            "diagnosis_gate": capability.get("diagnosis_gate"),
            "strategy_trace": capability.get("strategy_trace"),
            "suggestions": capability.get("suggestions", [])[:8],
        }
        print_stage(capability_stage)
        stages.append(capability_stage)
        if not (capability.get("diagnosis_gate") or {}).get("ready"):
            raise RuntimeError(f"diagnosis gate did not pass: {capability.get('diagnosis_gate')}")

        commands = [s.get("command", "") for s in capability.get("suggestions", [])]
        delete_cmd = next(cmd for cmd in commands if f"delete networkchaos {CHAOS}" in cmd)
        run = api("POST", "/api/heal/execute", {
            **payload,
            "dry_run": False,
            "approved": True,
            "risk_ack": True,
            "commands": [delete_cmd],
        }, timeout=180)
        healed_stage = {
            "stage": "self_healing_executed",
            "run_id": run.get("id"),
            "success": run.get("success"),
            "verification": run.get("verification"),
            "commands": run.get("commands"),
        }
        print_stage(healed_stage)
        stages.append(healed_stage)
        if run.get("success") is not True:
            raise RuntimeError(f"self-healing run failed: {run}")

        wait_for(lambda: not chaos_exists(), "NetworkChaos removed", timeout=30)
        wait_for(lambda: http_code() == "200", "frontend HTTP recovered", timeout=90)
        print(f"Holding recovered state for {RECOVERY_HOLD_SECONDS}s so recovery is visible in metrics...", flush=True)
        time.sleep(RECOVERY_HOLD_SECONDS)
        recovered = snapshot("recovered")
        print_stage(recovered)
        stages.append(recovered)
        return {"scenario": "NetworkChaosPartition", "passed": True, "stages": stages}
    finally:
        try:
            cleanup_chaos()
        finally:
            restore_platform_config(original_config)


def main() -> int:
    result = run_demo()
    print("\n=== summary ===")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("skipped") or result.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
