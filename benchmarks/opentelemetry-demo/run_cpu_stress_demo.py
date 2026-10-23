#!/usr/bin/env python3
"""Recordable AgenticSRE CPU stress fault demo for OpenTelemetry Demo.

Loop:
1. Capture baseline business/system/alert snapshots under background load.
2. Inject a temporary CPU stress pod labelled `purpose=agenticsre-demo`.
3. Observe CPU pressure and service metrics.
4. Send a high-confidence HighCPUUsage diagnosis to AgenticSRE.
5. Let AgenticSRE execute the approved remediation: delete the stress pod.
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
from urllib import error, parse, request


BASE_URL = os.environ.get("AGENTICSRE_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
AUTH_TOKEN = os.environ.get("AGENTICSRE_AUTH_TOKEN", "").strip()
NAMESPACE = os.environ.get("OTEL_DEMO_NAMESPACE", "otel-demo")
DEPLOYMENT = os.environ.get("OTEL_DEMO_TARGET_DEPLOYMENT", "frontend")
SERVICE = os.environ.get("OTEL_DEMO_TARGET_SERVICE", "frontend")
STRESS_POD = os.environ.get("OTEL_DEMO_CPU_STRESS_POD", "agenticsre-demo-cpu-stress")
NODE_PORT = int(os.environ.get("OTEL_DEMO_FRONTEND_NODEPORT", "30088"))
RUN_REAL = os.environ.get("AGENTICSRE_RUN_CPU_DEMO") == "1"
FAULT_HOLD_SECONDS = int(os.environ.get("AGENTICSRE_CPU_FAULT_HOLD_SECONDS", "120"))
RECOVERY_HOLD_SECONDS = int(os.environ.get("AGENTICSRE_CPU_RECOVERY_HOLD_SECONDS", "5"))
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


def stress_pod_line() -> str:
    return sh(f"kubectl -n {NAMESPACE} get pod {STRESS_POD} --ignore-not-found --no-headers", check=False)


def stress_exists() -> bool:
    return bool(stress_pod_line().strip())


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
        "stress_cpu_cores": prom_first_value(
            f'sum(rate(container_cpu_usage_seconds_total{{namespace="{NAMESPACE}",pod="{STRESS_POD}"}}[1m])) or vector(0)'
        ),
        "frontend_cpu_cores": prom_first_value(
            f'sum(rate(container_cpu_usage_seconds_total{{namespace="{NAMESPACE}",pod=~"frontend-.*"}}[1m]))'
        ),
        "namespace_cpu_cores": prom_first_value(
            f'sum(rate(container_cpu_usage_seconds_total{{namespace="{NAMESPACE}"}}[1m]))'
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
        "stress_exists": stress_exists(),
        "stress_pod": stress_pod_line(),
        "frontend_pod": pod_line(DEPLOYMENT),
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


def apply_stress() -> None:
    manifest = textwrap.dedent(f"""
    apiVersion: v1
    kind: Pod
    metadata:
      name: {STRESS_POD}
      namespace: {NAMESPACE}
      labels:
        purpose: agenticsre-demo
        managed-by: agenticsre
        app.kubernetes.io/component: agenticsre-cpu-stress
    spec:
      restartPolicy: Never
      containers:
        - name: stress
          image: ghcr.io/open-telemetry/demo:2.2.0-load-generator
          imagePullPolicy: IfNotPresent
          command: ["sh", "-c"]
          args:
            - >-
              python -c "import os; [os.fork() for _ in range(4)]; exec('x=0\\nwhile True:\\n x+=1')"
          resources:
            requests:
              cpu: "1"
              memory: "64Mi"
            limits:
              cpu: "4"
              memory: "256Mi"
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


def cleanup_stress() -> None:
    sh(f"kubectl -n {NAMESPACE} delete pod {STRESS_POD} --ignore-not-found=true", check=False)


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
        "source": "otel-demo-cpu-demo",
        "namespace": NAMESPACE,
        "fault_type": "HighCPUUsage",
        "pod": STRESS_POD,
        "service": SERVICE,
        "deployment": DEPLOYMENT,
        "message": f"Temporary agenticsre-demo CPU stress pod {STRESS_POD} is consuming CPU in {NAMESPACE}",
        "diagnosis": {
            "fault_type": "HighCPUUsage",
            "root_cause": f"Temporary agenticsre-demo CPU stress pod/{STRESS_POD} is consuming CPU and raising namespace CPU usage",
            "root_cause_component": STRESS_POD,
            "affected_services": [SERVICE, DEPLOYMENT],
            "remediation_suggestion": f"delete temporary CPU stress pod/{STRESS_POD} after approval and verify CPU pressure decreases",
            "confidence": 0.96,
            "evidence_summary": {
                "stress_pod": STRESS_POD,
                "stress_label": "purpose=agenticsre-demo",
                "metric": "container_cpu_usage_seconds_total shows CPU usage for the stress pod",
                "background_load": "otel-demo load-generator is running and continuously sending requests",
            },
        },
    }


def run_demo() -> Dict[str, Any]:
    if not RUN_REAL:
        print("SKIP CPU stress demo; set AGENTICSRE_RUN_CPU_DEMO=1 to run")
        return {"skipped": True}

    original_config = api("GET", "/api/platform/config").get("stored") or {}
    stages: List[Dict[str, Any]] = []
    try:
        set_heal_execution(True)
        cleanup_stress()
        wait_for(lambda: http_code() == "200", "baseline frontend HTTP 200", timeout=90)
        baseline = snapshot("baseline_with_background_load")
        print_stage(baseline)
        stages.append(baseline)

        print("\nInjecting temporary CPU stress pod...", flush=True)
        apply_stress()
        wait_for(stress_exists, "CPU stress pod exists", timeout=60)
        wait_for(lambda: "Running" in stress_pod_line(), "CPU stress pod running", timeout=120)
        print(f"Holding CPU stress for {FAULT_HOLD_SECONDS}s so CPU and latency curves show a clear elevated interval...", flush=True)
        time.sleep(FAULT_HOLD_SECONDS)
        injected = snapshot("cpu_stress_injected")
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
        delete_cmd = next(cmd for cmd in commands if f"delete pod {STRESS_POD}" in cmd)
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

        wait_for(lambda: not stress_exists(), "CPU stress pod removed", timeout=60)
        print(f"Holding recovered state for {RECOVERY_HOLD_SECONDS}s so CPU curves show the drop after self-healing...", flush=True)
        time.sleep(RECOVERY_HOLD_SECONDS)
        recovered = snapshot("recovered")
        print_stage(recovered)
        stages.append(recovered)
        return {"scenario": "CpuStressPod", "passed": True, "stages": stages}
    finally:
        try:
            cleanup_stress()
        finally:
            restore_platform_config(original_config)


def main() -> int:
    result = run_demo()
    print("\n=== summary ===")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("skipped") or result.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
