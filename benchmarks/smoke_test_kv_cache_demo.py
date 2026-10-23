#!/usr/bin/env python3
"""Run a guarded KV Cache injection, diagnosis, and recovery smoke test."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional


BASE_URL = os.environ.get("AGENTICSRE_BASE_URL", "http://127.0.0.1:18081").rstrip("/")
TOKEN = os.environ.get("AGENTICSRE_AUTH_TOKEN", "").strip()
OUTPUT = Path(os.environ.get("AGENTICSRE_SMOKE_OUTPUT", "outputs/demo/kv-cache/smoke_test_latest.json"))
TARGET = {
    "cluster_id": "t4-vllm-k8s",
    "namespace": "llm-k8s",
    "deployment": "vllm-server",
    "service": "vllm-server",
    "model": "qwen2.5-coder-3b-k8s",
    "target_mode": "k8s",
    "dry_run": False,
    "confirm": True,
    "risk_ack": True,
    "background_load": False,
}


def request_json(method: str, path: str, body: Optional[Dict[str, Any]] = None, timeout: int = 180) -> Dict[str, Any]:
    if not TOKEN:
        raise RuntimeError("AGENTICSRE_AUTH_TOKEN is required for this smoke check")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def retry_request(method: str, path: str, body: Optional[Dict[str, Any]] = None, attempts: int = 5) -> Dict[str, Any]:
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            return request_json(method, path, body)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(min(attempt * 2, 8))
    raise RuntimeError(f"{method} {path} failed after {attempts} attempts: {last_error}")


def print_stage(index: int, title: str, detail: str = "") -> None:
    suffix = f" - {detail}" if detail else ""
    print(f"[{index}/7] {title}{suffix}", flush=True)


def kv_value(event: Dict[str, Any]) -> float:
    value = event.get("value") if isinstance(event.get("value"), dict) else {}
    try:
        return float(value.get("kv_cache") or 0)
    except (TypeError, ValueError):
        return 0.0


def main() -> int:
    report: Dict[str, Any] = {
        "started_at": time.time(),
        "base_url": BASE_URL,
        "target": TARGET,
        "stages": {},
        "checks": {},
    }
    injected = False
    cleanup: Dict[str, Any] = {}
    failure = ""
    try:
        print_stage(1, "安全预检")
        preflight_response = retry_request("POST", "/api/llm-faultlab/preflight/sw-003", TARGET)
        preflight = preflight_response.get("preflight") if isinstance(preflight_response.get("preflight"), dict) else preflight_response
        report["stages"]["preflight"] = preflight_response
        if not preflight.get("ok"):
            raise RuntimeError(f"preflight blocked: {preflight.get('blockers')}")

        retry_request("POST", "/api/llm-faultlab/cleanup/sw-003", TARGET)
        print_stage(2, "真实注入KV Cache压力")
        injected_result = retry_request("POST", "/api/llm-faultlab/inject/sw-003", TARGET)
        report["stages"]["inject"] = injected_result
        injected = injected_result.get("status") == "executed"
        if not injected:
            raise RuntimeError(f"injection failed: {injected_result}")

        print_stage(3, "等待指标传播", "约45秒")
        time.sleep(45)
        event: Optional[Dict[str, Any]] = None
        for _ in range(5):
            refreshed = retry_request("POST", "/api/ai-events/refresh", {
                "cluster_id": TARGET["cluster_id"],
                "duration": "15m",
                "include_experiments": True,
            })
            candidates = [
                item for item in refreshed.get("events", [])
                if item.get("event_type") == "gpu_memory_pressure" and kv_value(item) >= 60
            ]
            if candidates:
                event = max(candidates, key=lambda item: float(item.get("updated_at") or 0))
                break
            time.sleep(15)
        if not event:
            raise RuntimeError("gpu_memory_pressure event with KV Cache >= 60% was not detected")
        report["stages"]["event"] = event
        print_stage(4, "智算事件已生成", f"KV Cache={kv_value(event):.2f}%")

        print_stage(5, "Agent调用Skill诊断")
        triggered = retry_request("POST", f"/api/ai-events/{event['id']}/skill", {
            "stages": ["detect", "diagnose"],
            "analysis_phases": ["context", "hypothesis", "plan", "investigation", "reasoning"],
        })
        run_id = str((triggered.get("agent_run") or {}).get("run_id") or "")
        if not run_id:
            raise RuntimeError(f"Agent+Skill run was not created: {triggered}")
        run: Dict[str, Any] = {}
        deadline = time.time() + 300
        while time.time() < deadline:
            run = retry_request("GET", f"/api/rca/{run_id}", attempts=3)
            if run.get("status") in {"completed", "failed"}:
                break
            time.sleep(3)
        report["stages"]["diagnosis"] = run
        if run.get("status") != "completed":
            raise RuntimeError(f"Agent+Skill diagnosis did not complete: {run_id} {run.get('status')}")
        result = run.get("result") if isinstance(run.get("result"), dict) else {}
        root_cause = str(result.get("root_cause") or "")
        confidence = float(result.get("confidence") or 0)
        skill_id = str(((result.get("skill_runtime_result") or {}).get("diagnosis") or {}).get("skill_id") or result.get("skill_id") or "")
        report["checks"].update({
            "diagnosis_completed": True,
            "root_cause_mentions_kv_cache": any(term in root_cause.lower() for term in ("kv cache", "kv_cache", "缓存")),
            "confidence": confidence,
            "skill_id": skill_id,
        })
        print_stage(6, "诊断完成", f"skill={skill_id or '-'}, confidence={confidence:.3f}")
    except Exception as exc:
        failure = str(exc)
        report["error"] = failure
    finally:
        print_stage(7, "执行恢复与验证")
        try:
            cleanup = retry_request("POST", "/api/llm-faultlab/cleanup/sw-003", TARGET, attempts=8)
            recovery_samples = []
            for attempt in range(9):
                observation = cleanup.get("observation") if isinstance(cleanup.get("observation"), dict) else {}
                sample = {
                    "attempt": attempt + 1,
                    "waiting_requests": observation.get("waiting_requests"),
                    "running_requests": observation.get("running_requests"),
                    "kv_cache_usage": observation.get("kv_cache_usage"),
                    "endpoint_status": observation.get("endpoint_status"),
                }
                recovery_samples.append(sample)
                waiting = float(observation.get("waiting_requests") or 0)
                kv_cache = float(observation.get("kv_cache_usage") or 0)
                if waiting == 0 and kv_cache <= 5:
                    break
                if attempt < 8:
                    time.sleep(15)
                    cleanup = retry_request("POST", "/api/llm-faultlab/cleanup/sw-003", TARGET, attempts=3)
            report["stages"]["cleanup"] = cleanup
            report["stages"]["recovery_samples"] = recovery_samples
            observation = cleanup.get("observation") if isinstance(cleanup.get("observation"), dict) else {}
            report["checks"].update({
                "cleanup_executed": cleanup.get("status") == "executed",
                "deployment_ready": observation.get("deployment_ready_replicas") == observation.get("deployment_desired_replicas"),
                "endpoint_ok": observation.get("endpoint_status") == "ok",
                "waiting_requests_after": observation.get("waiting_requests"),
                "kv_cache_after": observation.get("kv_cache_usage"),
            })
        except Exception as cleanup_exc:
            report["cleanup_error"] = str(cleanup_exc)

    checks = report["checks"]
    passed = bool(
        not failure
        and checks.get("diagnosis_completed")
        and checks.get("root_cause_mentions_kv_cache")
        and checks.get("cleanup_executed")
        and checks.get("deployment_ready")
        and checks.get("endpoint_ok")
        and float(checks.get("waiting_requests_after") or 0) == 0
        and float(checks.get("kv_cache_after") or 0) == 0
    )
    report["passed"] = passed
    report["finished_at"] = time.time()
    report["duration_s"] = round(report["finished_at"] - report["started_at"], 3)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": passed, "duration_s": report["duration_s"], "checks": checks, "error": failure}, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
