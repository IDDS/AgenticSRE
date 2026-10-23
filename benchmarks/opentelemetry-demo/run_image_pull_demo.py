#!/usr/bin/env python3
"""Recordable AgenticSRE ImagePullBackOff demo for OpenTelemetry Demo.

Loop:
1. Capture baseline business/system/alert snapshots under background load.
2. Patch deployment/frontend to a non-existing image tag.
3. Observe the new ReplicaSet pod enter ImagePullBackOff while the old pod keeps serving.
4. Send a high-confidence ImagePullError diagnosis to AgenticSRE.
5. Let AgenticSRE execute the approved remediation: rollout undo deployment/frontend.
6. Verify rollout recovery and print a concise recordable timeline.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any, Callable, Dict, List
from urllib import error, parse, request


BASE_URL = os.environ.get("AGENTICSRE_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
AUTH_TOKEN = os.environ.get("AGENTICSRE_AUTH_TOKEN", "").strip()
NAMESPACE = os.environ.get("OTEL_DEMO_NAMESPACE", "otel-demo")
DEPLOYMENT = os.environ.get("OTEL_DEMO_TARGET_DEPLOYMENT", "frontend")
CONTAINER = os.environ.get("OTEL_DEMO_TARGET_CONTAINER", "frontend")
SERVICE = os.environ.get("OTEL_DEMO_TARGET_SERVICE", "frontend")
BAD_IMAGE = os.environ.get(
    "OTEL_DEMO_BAD_IMAGE",
    "ghcr.io/open-telemetry/demo:2.2.0-frontend-agenticsre-missing",
)
NODE_PORT = int(os.environ.get("OTEL_DEMO_FRONTEND_NODEPORT", "30088"))
RUN_REAL = os.environ.get("AGENTICSRE_RUN_IMAGE_DEMO") == "1"
FAULT_HOLD_SECONDS = int(os.environ.get("AGENTICSRE_IMAGE_FAULT_HOLD_SECONDS", "90"))
RECOVERY_HOLD_SECONDS = int(os.environ.get("AGENTICSRE_IMAGE_RECOVERY_HOLD_SECONDS", "5"))
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


def get_json(cmd: str) -> Dict[str, Any]:
    return json.loads(sh(cmd))


def node_ip() -> str:
    return sh("kubectl get node -o jsonpath='{.items[0].status.addresses[?(@.type==\"InternalIP\")].address}'")


def http_code() -> str:
    return sh(f"curl -s -o /dev/null -w '%{{http_code}}' http://{node_ip()}:{NODE_PORT}/", timeout=20, check=False)


def deployment_obj() -> Dict[str, Any]:
    return get_json(f"kubectl -n {NAMESPACE} get deployment {DEPLOYMENT} -o json")


def current_image() -> str:
    obj = deployment_obj()
    containers = (((obj.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []
    for container in containers:
        if container.get("name") == CONTAINER:
            return str(container.get("image") or "")
    return str((containers[0] or {}).get("image") or "") if containers else ""


def deployment_summary() -> Dict[str, Any]:
    obj = deployment_obj()
    status = obj.get("status") or {}
    spec = obj.get("spec") or {}
    return {
        "image": current_image(),
        "generation": int(obj.get("metadata", {}).get("generation") or 0),
        "desired": int(spec.get("replicas") or 0),
        "ready": int(status.get("readyReplicas") or 0),
        "available": int(status.get("availableReplicas") or 0),
        "updated": int(status.get("updatedReplicas") or 0),
    }


def pods_json() -> Dict[str, Any]:
    return get_json(f"kubectl -n {NAMESPACE} get pods -l app.kubernetes.io/component={DEPLOYMENT} -o json")


def pod_rows() -> str:
    return sh(f"kubectl -n {NAMESPACE} get pods -l app.kubernetes.io/component={DEPLOYMENT} --no-headers", check=False)


def pod_line(component: str) -> str:
    return sh(f"kubectl -n {NAMESPACE} get pods -l app.kubernetes.io/component={component} --no-headers", check=False)


def image_pull_pods() -> List[Dict[str, str]]:
    pods = []
    for item in pods_json().get("items", []):
        name = item.get("metadata", {}).get("name", "")
        phase = item.get("status", {}).get("phase", "")
        reasons = []
        images = []
        for cs in item.get("status", {}).get("containerStatuses", []) or []:
            state = cs.get("state") or {}
            waiting = state.get("waiting") or {}
            reason = waiting.get("reason") or ""
            if reason:
                reasons.append(reason)
            if cs.get("image"):
                images.append(cs["image"])
        if any(r in {"ImagePullBackOff", "ErrImagePull"} for r in reasons):
            pods.append({"name": name, "phase": phase, "reasons": ",".join(reasons), "images": ",".join(images)})
    return pods


def bad_pod_name() -> str:
    pods = image_pull_pods()
    return pods[0]["name"] if pods else ""


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
        "frontend_cpu_cores": prom_first_value(
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
        "deployment": deployment_summary(),
        "image_pull_pods": image_pull_pods(),
        "frontend_pods": pod_rows(),
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


def compact_verification(verification: Dict[str, Any]) -> Dict[str, Any]:
    compact = dict(verification or {})
    checks = []
    for check in compact.get("checks", []) or []:
        item = dict(check)
        stdout = str(item.get("stdout") or "")
        if len(stdout) > 600:
            item["stdout"] = stdout[:600] + "...<truncated>"
        item.pop("json", None)
        checks.append(item)
    compact["checks"] = checks
    return compact


def inject_bad_image() -> None:
    sh(f"kubectl -n {NAMESPACE} set image deployment/{DEPLOYMENT} {CONTAINER}={BAD_IMAGE}")


def restore_image(original_image: str) -> None:
    if not original_image:
        sh(f"kubectl -n {NAMESPACE} rollout undo deployment/{DEPLOYMENT}", check=False)
    else:
        sh(f"kubectl -n {NAMESPACE} set image deployment/{DEPLOYMENT} {CONTAINER}={original_image}", check=False)
    sh(f"kubectl -n {NAMESPACE} rollout status deployment/{DEPLOYMENT} --timeout=120s", timeout=150, check=False)


def no_image_pull_pods() -> bool:
    return not image_pull_pods()


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


def diagnosis_payload(pod: str) -> Dict[str, Any]:
    return {
        "source": "otel-demo-image-pull-demo",
        "namespace": NAMESPACE,
        "fault_type": "ImagePullError",
        "pod": pod,
        "service": SERVICE,
        "deployment": DEPLOYMENT,
        "message": f"OpenTelemetry Demo {DEPLOYMENT} rollout created pod {pod} with ImagePullBackOff for image {BAD_IMAGE}",
        "diagnosis": {
            "fault_type": "ImagePullError",
            "root_cause": f"Deployment/{DEPLOYMENT} was updated to a non-existing image tag {BAD_IMAGE}, causing ImagePullBackOff",
            "root_cause_component": DEPLOYMENT,
            "affected_services": [SERVICE, DEPLOYMENT],
            "remediation_suggestion": f"rollout undo deployment/{DEPLOYMENT} to restore the previous working image",
            "confidence": 0.97,
            "evidence_summary": {
                "bad_image": BAD_IMAGE,
                "pod": pod,
                "reason": "ImagePullBackOff or ErrImagePull",
                "safe_recovery": "rollout undo restores the previous ReplicaSet and image",
            },
        },
    }


def run_demo() -> Dict[str, Any]:
    if not RUN_REAL:
        print("SKIP image pull demo; set AGENTICSRE_RUN_IMAGE_DEMO=1 to run")
        return {"skipped": True}

    original_config = api("GET", "/api/platform/config").get("stored") or {}
    original_image = current_image()
    stages: List[Dict[str, Any]] = []
    try:
        set_heal_execution(True)
        wait_for(lambda: http_code() == "200", "baseline frontend HTTP 200", timeout=90)
        wait_for(no_image_pull_pods, "no existing image pull pods", timeout=90)
        baseline = snapshot("baseline_with_background_load")
        print_stage(baseline)
        stages.append(baseline)

        print(f"\nInjecting bad image tag {BAD_IMAGE}...", flush=True)
        inject_bad_image()
        wait_for(lambda: bool(image_pull_pods()), "ImagePullBackOff pod appears", timeout=180)
        print(f"Holding ImagePullBackOff for {FAULT_HOLD_SECONDS}s so pod status, events and rollout metrics are visible...", flush=True)
        time.sleep(FAULT_HOLD_SECONDS)
        injected = snapshot("image_pull_fault_injected")
        print_stage(injected)
        stages.append(injected)

        pod = bad_pod_name()
        payload = diagnosis_payload(pod)
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
        undo_cmd = next(cmd for cmd in commands if f"rollout undo deployment/{DEPLOYMENT}" in cmd)
        run = api("POST", "/api/heal/execute", {
            **payload,
            "dry_run": False,
            "approved": True,
            "risk_ack": True,
            "commands": [undo_cmd],
        }, timeout=180)
        healed_stage = {
            "stage": "self_healing_executed",
            "run_id": run.get("id"),
            "success": run.get("success"),
            "verification": compact_verification(run.get("verification") or {}),
            "commands": run.get("commands"),
        }
        print_stage(healed_stage)
        stages.append(healed_stage)
        if run.get("success") is not True:
            raise RuntimeError(f"self-healing run failed: {run}")

        wait_for(no_image_pull_pods, "ImagePullBackOff pod removed", timeout=120)
        wait_for(lambda: current_image() == original_image, "deployment image restored", timeout=120)
        print(f"Holding recovered state for {RECOVERY_HOLD_SECONDS}s so rollout recovery is visible...", flush=True)
        time.sleep(RECOVERY_HOLD_SECONDS)
        recovered = snapshot("recovered")
        print_stage(recovered)
        stages.append(recovered)
        return {"scenario": "ImagePullBackOff", "passed": True, "stages": stages}
    finally:
        try:
            if current_image() != original_image or image_pull_pods():
                restore_image(original_image)
        finally:
            restore_platform_config(original_config)


def main() -> int:
    result = run_demo()
    print("\n=== summary ===")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("skipped") or result.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
