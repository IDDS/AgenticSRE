#!/usr/bin/env python3
"""Run controlled end-to-end AI/vLLM fault experiments against AgenticSRE.

The script intentionally uses the public web API instead of importing server
internals, so the produced report reflects the deployed system behavior.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


DEFAULT_SCENARIOS = [
    "sw-001",
    "sw-007",
    "llm-sw-continuous-batch-off",
    "net-004",
    "net-001",
    "os-002",
    "os-003",
    "llm-os-disk-pressure",
]


def http_json(method: str, base_url: str, path: str, body: Optional[Dict[str, Any]] = None, timeout: int = 180) -> Dict[str, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(base_url.rstrip("/") + path, data=data, method=method)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return {"ok": True, "status": response.status, "data": json.loads(raw) if raw else {}}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except Exception:
            data = {"raw": raw}
        return {"ok": False, "status": exc.code, "data": data}
    except Exception as exc:
        return {"ok": False, "status": 0, "data": {"error": repr(exc)}}


def kubectl(kubeconfig: str, namespace: str, args: Iterable[str], timeout: int = 180) -> Dict[str, Any]:
    command = ["kubectl", "--kubeconfig", kubeconfig, "-n", namespace, *args]
    try:
        proc = subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)
        return {"rc": proc.returncode, "out": proc.stdout[-4000:], "err": proc.stderr[-4000:]}
    except Exception as exc:
        return {"rc": -1, "out": "", "err": repr(exc)}


def build_body(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "target_mode": "k8s",
        "cluster_id": args.cluster_id,
        "namespace": args.namespace,
        "deployment": args.deployment,
        "service": args.service,
        "model": args.model,
        "dry_run": False,
        "confirm": True,
        "confirmed": True,
        "approval": True,
        "experiment_ack": True,
        "risk_ack": True,
        "background_load": True,
        "loadgen_duration_s": args.loadgen_duration_s,
        "loadgen_concurrency": args.loadgen_concurrency,
        "loadgen_prompt_len": args.loadgen_prompt_len,
        "loadgen_max_tokens": args.loadgen_max_tokens,
        "loadgen_qps": args.loadgen_qps,
        "loadgen_timeout_s": args.loadgen_timeout_s,
        "baseline_samples": args.baseline_samples,
        "fault_samples": args.fault_samples,
        "sample_interval": args.sample_interval,
    }


def compact_event(event: Dict[str, Any]) -> Dict[str, Any]:
    keys = ["id", "type", "status", "severity", "root_cause", "fault_type", "confidence", "summary"]
    return {key: event.get(key) for key in keys if key in event}


def refresh_events(args: argparse.Namespace) -> Dict[str, Any]:
    return http_json(
        "POST",
        args.base_url,
        "/api/ai-events/refresh",
        {"cluster_id": args.cluster_id, "duration": args.event_duration, "include_experiments": True},
        timeout=90,
    )


def active_events(args: argparse.Namespace) -> Dict[str, Any]:
    path = f"/api/ai-events?cluster_id={args.cluster_id}&status=active&limit=20"
    return http_json("GET", args.base_url, path, timeout=30)


def recent_events(args: argparse.Namespace, limit: int = 100) -> Dict[str, Any]:
    path = f"/api/ai-events?cluster_id={args.cluster_id}&duration={args.event_duration}&limit={limit}"
    return http_json("GET", args.base_url, path, timeout=45)


def _event_scenario_id(event: Dict[str, Any]) -> str:
    return ""


def event_for_fault_run(events: List[Dict[str, Any]], fault_run_id: str = "") -> Optional[Dict[str, Any]]:
    fault_run_id = str(fault_run_id or "").strip()
    matches = [
        event for event in events
        if event.get("source") == "fault_experiment"
        and (not fault_run_id or str(event.get("experiment_run_id") or "") == fault_run_id)
    ]
    if not matches:
        return None
    return sorted(
        matches,
        key=lambda event: float(event.get("last_seen_at") or event.get("updated_at") or event.get("created_at") or 0),
        reverse=True,
    )[0]


def poll_rca(args: argparse.Namespace, run_id: str, timeout_s: int = 150) -> Dict[str, Any]:
    deadline = time.time() + max(5, timeout_s)
    last: Dict[str, Any] = {}
    while time.time() < deadline:
        response = http_json("GET", args.base_url, f"/api/rca/{run_id}?_={int(time.time() * 1000)}", timeout=30)
        last = response
        status = (response.get("data") or {}).get("status")
        if status in {"completed", "failed"}:
            return response
        time.sleep(3)
    return last


def trigger_agent_skill(args: argparse.Namespace, event: Dict[str, Any]) -> Dict[str, Any]:
    event_id = event.get("id")
    if not event_id:
        return {"ok": False, "status": 0, "data": {"error": "missing event id"}}
    trigger = http_json(
        "POST",
        args.base_url,
        f"/api/ai-events/{event_id}/skill",
        {"stages": ["detect", "diagnose", "heal", "verify"]},
        timeout=120,
    )
    run = (trigger.get("data") or {}).get("agent_run") or {}
    run_id = run.get("run_id")
    polled = poll_rca(args, run_id, timeout_s=args.rca_timeout_s) if run_id else {}
    listing = recent_events(args, limit=120)
    latest = event_for_fault_run((listing.get("data") or {}).get("events") or [], event.get("experiment_run_id")) or event
    return {
        "trigger": trigger,
        "run_id": run_id,
        "rca": polled,
        "latest_event": latest,
    }


def diagnose_events(args: argparse.Namespace, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    diagnoses = []
    for event in events:
        event_id = event.get("id")
        if not event_id:
            continue
        result = trigger_agent_skill(args, event)
        diagnoses.append({"event_id": event_id, "event_type": event.get("event_type") or event.get("type"), "result": result})
    return diagnoses


def cleanup(args: argparse.Namespace, scenario_id: str) -> List[Dict[str, Any]]:
    steps: List[Dict[str, Any]] = []
    steps.append(
        {
            "step": "stop_loadgen",
            "result": http_json(
                "DELETE",
                args.base_url,
                f"/api/ai-clusters/{args.cluster_id}/loadgen",
                {"dry_run": False, "confirm": True, "confirmed": True, "approval": True},
                timeout=80,
            ),
        }
    )
    steps.append(
        {
            "step": "scenario_cleanup",
            "result": http_json(
                "POST",
                args.base_url,
                f"/api/llm-faultlab/cleanup/{scenario_id}",
                build_body(args),
                timeout=180,
            ),
        }
    )
    steps.append(
        {
            "step": "rollout",
            "result": kubectl(args.kubeconfig, args.namespace, ["rollout", "status", f"deploy/{args.deployment}", "--timeout=180s"], timeout=200),
        }
    )
    steps.append({"step": "pods", "result": kubectl(args.kubeconfig, args.namespace, ["get", "deploy,pod", "-o", "wide"], timeout=60)})
    time.sleep(args.post_cleanup_wait_s)
    steps.append({"step": "refresh_after_cleanup", "result": refresh_events(args)})
    steps.append({"step": "active_after_cleanup", "result": active_events(args)})
    return steps


def observe_fault_window(args: argparse.Namespace, fault_run_id: str = "") -> Dict[str, Any]:
    snapshots: List[Dict[str, Any]] = []
    scenario_event: Optional[Dict[str, Any]] = None
    deadline = time.time() + max(1, args.observe_seconds)
    while time.time() < deadline:
        refreshed = refresh_events(args)
        active = active_events(args)
        events = active.get("data", {}).get("events") or []
        scenario_event = event_for_fault_run(events, fault_run_id) if fault_run_id else None
        snapshots.append(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "refresh": refreshed,
                "active": active,
                "compact_events": [compact_event(event) for event in events],
            }
        )
        if (scenario_event or (events and not fault_run_id)) and not args.observe_full_window:
            break
        time.sleep(max(1, args.observe_interval_s))
    last_events = snapshots[-1].get("active", {}).get("data", {}).get("events", []) if snapshots else []
    selected_events = [scenario_event] if scenario_event else ([] if fault_run_id else last_events)
    return {
        "snapshots": snapshots,
        "scenario_event": scenario_event,
        "skill_diagnosis": diagnose_events(args, selected_events),
    }


def run_one(args: argparse.Namespace, scenario_id: str) -> Dict[str, Any]:
    record: Dict[str, Any] = {"scenario_id": scenario_id, "started_at": datetime.now(timezone.utc).isoformat()}
    try:
        record["pre_status"] = kubectl(args.kubeconfig, args.namespace, ["get", "deploy", args.deployment, "-o", "jsonpath={.status.readyReplicas}/{.status.replicas}"], timeout=60)
        preflight = http_json("POST", args.base_url, f"/api/llm-faultlab/preflight/{scenario_id}", build_body(args), timeout=60)
        record["preflight"] = preflight
        if not preflight.get("ok"):
            record["outcome"] = "preflight_http_failed"
            return record

        if args.mode == "inject-observe":
            injected = http_json("POST", args.base_url, f"/api/llm-faultlab/inject/{scenario_id}", build_body(args), timeout=args.experiment_timeout_s)
            record["inject"] = injected
            record["fault_run_id"] = (injected.get("data") or {}).get("fault_run_id") or ""
            observation = observe_fault_window(args, record.get("fault_run_id") or "")
            record["observation"] = observation
            snapshots = observation.get("snapshots") or []
            record["active_events_during_fault"] = snapshots[-1].get("active") if snapshots else {}
            recent = recent_events(args, limit=120)
            recent_events_list = recent.get("data", {}).get("events") or []
            scenario_event = observation.get("scenario_event") or event_for_fault_run(
                recent_events_list,
                record.get("fault_run_id") or "",
            )
            selected_events = [scenario_event] if scenario_event else (snapshots[-1].get("active", {}).get("data", {}).get("events", []) if snapshots else [])
            record["recent_events_during_fault"] = recent
            record["scenario_event"] = scenario_event
            record["compact_events_during_fault"] = [compact_event(event) for event in selected_events]
            observed_diagnosis = observation.get("skill_diagnosis") or []
            record["skill_diagnosis"] = observed_diagnosis or diagnose_events(args, selected_events)
            status = injected.get("data", {}).get("status")
            record["outcome"] = "executed" if injected.get("ok") and status in {"executed", "success", "completed"} else "executed_with_warning"
        else:
            experiment = http_json("POST", args.base_url, f"/api/llm-faultlab/experiment/{scenario_id}", build_body(args), timeout=args.experiment_timeout_s)
            record["experiment"] = experiment
            record["fault_run_id"] = (experiment.get("data") or {}).get("fault_run_id") or ""
            time.sleep(args.fault_event_wait_s)
            record["refresh_during_fault"] = refresh_events(args)
            active = active_events(args)
            recent = recent_events(args, limit=120)
            record["active_events_during_fault"] = active
            record["recent_events_during_fault"] = recent
            active_events_list = active.get("data", {}).get("events") or []
            recent_events_list = recent.get("data", {}).get("events") or []
            scenario_event = event_for_fault_run(recent_events_list, record.get("fault_run_id") or "")
            selected_events = [scenario_event] if scenario_event else active_events_list
            record["scenario_event"] = scenario_event
            record["compact_events_during_fault"] = [compact_event(event) for event in selected_events]
            record["skill_diagnosis"] = diagnose_events(args, selected_events)
            status = experiment.get("data", {}).get("status")
            record["outcome"] = "executed" if experiment.get("ok") and status in {"executed", "success", "completed"} else "executed_with_warning"
    except Exception as exc:
        record["outcome"] = "exception"
        record["exception"] = repr(exc)
        record["traceback"] = traceback.format_exc()[-4000:]
    finally:
        record["cleanup"] = cleanup(args, scenario_id)
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
    return record


def write_report(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _nested(data: Dict[str, Any], *keys: str) -> Any:
    cur: Any = data
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def summarize_report(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows: List[Dict[str, Any]] = []
    for record in data.get("results") or []:
        event = record.get("scenario_event") or {}
        diagnosis_records = record.get("skill_diagnosis") or []
        latest = {}
        rca_status = ""
        run_id = ""
        for diagnosis in diagnosis_records:
            result = diagnosis.get("result") if isinstance(diagnosis, dict) else {}
            if not isinstance(result, dict):
                continue
            latest = result.get("latest_event") or latest
            run_id = result.get("run_id") or run_id
            rca_status = _nested(result, "rca", "data", "status") or rca_status
        latest_diag = (latest or event).get("diagnosis") if isinstance(latest or event, dict) else {}
        if not isinstance(latest_diag, dict):
            latest_diag = {}
        cleanup_steps = record.get("cleanup") or []
        cleanup_ok = all(
            bool(step.get("result", {}).get("ok"))
            for step in cleanup_steps
            if isinstance(step, dict) and isinstance(step.get("result"), dict) and step.get("step") in {"stop_loadgen", "scenario_cleanup", "refresh_after_cleanup", "active_after_cleanup"}
        )
        rows.append({
            "scenario": record.get("scenario_id"),
            "outcome": record.get("outcome"),
            "event_id": (event or {}).get("id"),
            "event_status": (latest or event or {}).get("status"),
            "fault_type": (latest or event or {}).get("fault_type"),
            "agent_skill_run": run_id,
            "rca_status": rca_status,
            "root_cause": latest_diag.get("root_cause"),
            "confidence": latest_diag.get("confidence"),
            "skill_id": latest_diag.get("skill_id"),
            "cleanup_ok": cleanup_ok,
        })
    return {
        "report": str(path),
        "cluster_id": data.get("cluster_id"),
        "mode": data.get("mode"),
        "started_at": data.get("started_at"),
        "finished_at": data.get("finished_at"),
        "results": rows,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run real AI fault experiments and write an evidence report.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--cluster-id", default="t4-vllm-k8s")
    parser.add_argument("--namespace", default="llm-k8s")
    parser.add_argument("--deployment", default="vllm-server")
    parser.add_argument("--service", default="vllm-server")
    parser.add_argument("--model", default="qwen2.5-coder-3b-k8s")
    parser.add_argument("--kubeconfig", default="/home/ubuntu/agenticsre/data/t4-admin.conf")
    parser.add_argument("--report", default="")
    parser.add_argument("--summarize-only", action="store_true", help="Parse --report and print a compact closure summary without running faults.")
    parser.add_argument("--scenario", action="append", dest="scenarios", help="Scenario id to run. Can be repeated.")
    parser.add_argument("--mode", choices=["experiment", "inject-observe"], default="experiment")
    parser.add_argument("--loadgen-duration-s", type=int, default=50)
    parser.add_argument("--loadgen-concurrency", type=int, default=3)
    parser.add_argument("--loadgen-prompt-len", type=int, default=96)
    parser.add_argument("--loadgen-max-tokens", type=int, default=24)
    parser.add_argument("--loadgen-timeout-s", type=int, default=35)
    parser.add_argument("--loadgen-qps", type=float, default=0.0)
    parser.add_argument("--baseline-samples", type=int, default=1)
    parser.add_argument("--fault-samples", type=int, default=1)
    parser.add_argument("--sample-interval", type=int, default=3)
    parser.add_argument("--fault-event-wait-s", type=int, default=8)
    parser.add_argument("--event-duration", default="1h")
    parser.add_argument("--rca-timeout-s", type=int, default=150)
    parser.add_argument("--observe-seconds", type=int, default=45)
    parser.add_argument("--observe-interval-s", type=int, default=8)
    parser.add_argument("--observe-full-window", action="store_true")
    parser.add_argument("--post-cleanup-wait-s", type=int, default=8)
    parser.add_argument("--experiment-timeout-s", type=int, default=260)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.summarize_only:
        if not args.report:
            raise SystemExit("--summarize-only requires --report")
        print(json.dumps(summarize_report(Path(args.report)), ensure_ascii=False, indent=2))
        return 0
    scenarios = args.scenarios or DEFAULT_SCENARIOS
    report = Path(args.report or f"/tmp/ai_fault_batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    payload: Dict[str, Any] = {
        "report": str(report),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "cluster_id": args.cluster_id,
        "namespace": args.namespace,
        "deployment": args.deployment,
        "scenarios": scenarios,
        "mode": args.mode,
        "initial": {
            "kube": kubectl(args.kubeconfig, args.namespace, ["get", "deploy,pod", "-o", "wide"], timeout=60),
            "active_events": active_events(args),
        },
        "results": [],
    }
    print(f"AI fault batch start: {report}", flush=True)
    for scenario_id in scenarios:
        print(f"=== RUN {scenario_id}", flush=True)
        record = run_one(args, scenario_id)
        payload["results"].append(record)
        write_report(report, payload)
        active_after = record.get("cleanup", [])[-1].get("result", {}).get("data", {}).get("events", []) if record.get("cleanup") else []
        event_types = [
            event.get("event_type") or event.get("type")
            for event in record.get("active_events_during_fault", {}).get("data", {}).get("events", [])
        ]
        action_result = record.get("inject") if args.mode == "inject-observe" else record.get("experiment")
        print(
            json.dumps(
                {
                    "scenario": scenario_id,
                    "outcome": record.get("outcome"),
                    "action_status": (action_result or {}).get("data", {}).get("status"),
                    "events": event_types,
                    "active_after_cleanup": len(active_after),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    payload["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_report(report, payload)
    print(f"AI fault batch done: {report}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
