#!/usr/bin/env python3
"""Run a paired 2x2 full-workflow hypothesis ablation through AgenticSRE APIs."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


FULL_PHASES = ["context", "hypothesis", "plan", "investigation", "reasoning"]
NO_HYPOTHESIS_PHASES = ["context", "plan", "investigation", "reasoning"]


def request_json(base_url: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{request.method} {path} failed: HTTP {exc.code}: {detail}") from exc


def extract_run_id(response: dict[str, Any]) -> str:
    run = response.get("agent_run") if isinstance(response.get("agent_run"), dict) else {}
    run_id = response.get("run_id") or run.get("run_id")
    if not run_id:
        raise RuntimeError(f"RCA trigger did not return a run id: {response}")
    return str(run_id)


def trigger_runs(base_url: str, event_id: str, engines: set[str] | None = None) -> list[dict[str, Any]]:
    configs = [
        {
            "name": "multi_agent_with_hypothesis",
            "engine": "multi_agent",
            "hypothesis_enabled": True,
            "path": f"/api/ai-events/{event_id}/agent",
            "payload": {"mode": "analysis", "analysis_phases": FULL_PHASES},
        },
        {
            "name": "multi_agent_without_hypothesis",
            "engine": "multi_agent",
            "hypothesis_enabled": False,
            "path": f"/api/ai-events/{event_id}/agent",
            "payload": {"mode": "analysis", "analysis_phases": NO_HYPOTHESIS_PHASES},
        },
        {
            "name": "agent_skill_with_hypothesis",
            "engine": "agent_skill",
            "hypothesis_enabled": True,
            "path": f"/api/ai-events/{event_id}/skill",
            "payload": {"stages": ["detect", "diagnose"], "analysis_phases": FULL_PHASES},
        },
        {
            "name": "agent_skill_without_hypothesis",
            "engine": "agent_skill",
            "hypothesis_enabled": False,
            "path": f"/api/ai-events/{event_id}/skill",
            "payload": {"stages": ["detect", "diagnose"], "analysis_phases": NO_HYPOTHESIS_PHASES},
        },
    ]
    if engines:
        configs = [config for config in configs if config["engine"] in engines]
    triggered = []
    for config in configs:
        started_at = time.time()
        response = request_json(base_url, config["path"], config["payload"])
        triggered.append({
            **config,
            "run_id": extract_run_id(response),
            "triggered_at": started_at,
            "trigger_response": response,
        })
    return triggered


def poll_runs(base_url: str, runs: list[dict[str, Any]], timeout_s: float, interval_s: float) -> None:
    deadline = time.time() + timeout_s
    pending = {run["run_id"]: run for run in runs}
    while pending and time.time() < deadline:
        for run_id, run in list(pending.items()):
            status = request_json(base_url, f"/api/rca/{run_id}")
            run["raw_run"] = status
            if str(status.get("status") or "").lower() in {"completed", "failed", "error"}:
                run["completed_at"] = time.time()
                pending.pop(run_id, None)
        if pending:
            time.sleep(interval_s)
    for run_id, run in pending.items():
        run["timed_out"] = True
        run["completed_at"] = time.time()


def nested_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def diagnosis_result(result: dict[str, Any]) -> dict[str, Any]:
    """Unwrap PipelineResult/RCA wrappers until the diagnosis payload is reached."""
    current = result
    for _ in range(4):
        if any(key in current for key in ("root_cause", "diagnosis", "fault_type", "confidence")):
            return current
        nested = current.get("result")
        if not isinstance(nested, dict):
            break
        current = nested
    return current


def summarize_run(run: dict[str, Any], expected_terms: list[str]) -> dict[str, Any]:
    raw = nested_dict(run.get("raw_run"))
    result = nested_dict(raw.get("result"))
    final_result = diagnosis_result(result)
    diagnosis = nested_dict(final_result.get("diagnosis")) or final_result
    root_cause = str(
        diagnosis.get("root_cause")
        or final_result.get("root_cause")
        or final_result.get("conclusion")
        or ""
    )
    fault_type = str(diagnosis.get("fault_type") or final_result.get("fault_type") or "")
    searchable = f"{fault_type} {root_cause}".lower()
    matched_terms = [term for term in expected_terms if term.lower() in searchable]
    events = raw.get("events") if isinstance(raw.get("events"), list) else []
    hypotheses = [event for event in events if isinstance(event, dict) and event.get("event") == "hypotheses"]
    llm_events = [
        event for event in events
        if isinstance(event, dict) and (
            "llm" in str(event.get("event") or "").lower()
            or "token" in json.dumps(event, ensure_ascii=False).lower()
        )
    ]
    started_at = float(raw.get("started_at") or run.get("triggered_at") or 0)
    completed_at = float(run.get("completed_at") or time.time())
    duration_s = result.get("duration_s")
    if not isinstance(duration_s, (int, float)) or duration_s <= 0:
        duration_s = max(0.0, completed_at - started_at)
    return {
        "name": run["name"],
        "engine": run["engine"],
        "hypothesis_enabled": run["hypothesis_enabled"],
        "analysis_phases": run["payload"]["analysis_phases"],
        "run_id": run["run_id"],
        "status": raw.get("status"),
        "duration_s": round(float(duration_s), 3),
        "fault_type": fault_type,
        "root_cause": root_cause,
        "confidence": diagnosis.get("confidence", final_result.get("confidence")),
        "correct": bool(matched_terms),
        "matched_expected_terms": matched_terms,
        "hypothesis_event_count": len(hypotheses),
        "llm_event_count": len(llm_events),
        "event_count": len(events),
        "skill_id": diagnosis.get("skill_id"),
        "agent_skill_bridge": result.get("agent_skill_bridge"),
        "timed_out": bool(run.get("timed_out")),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18081")
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--scenario", default="unknown")
    parser.add_argument("--fault-run-id", default="")
    parser.add_argument("--engine", action="append", choices=["multi_agent", "agent_skill"])
    parser.add_argument("--expected-term", action="append", required=True)
    parser.add_argument("--timeout-s", type=float, default=900)
    parser.add_argument("--poll-interval-s", type=float, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    event_snapshot = request_json(
        args.base_url,
        "/api/ai-events?limit=100&cluster_id=t4-vllm-k8s&duration=24h",
    )
    event = next(
        (item for item in event_snapshot.get("events", []) if str(item.get("id")) == args.event_id),
        None,
    )
    if not event:
        raise RuntimeError(f"AI event not found: {args.event_id}")

    runs = trigger_runs(args.base_url, args.event_id, set(args.engine or []))
    poll_runs(args.base_url, runs, args.timeout_s, args.poll_interval_s)
    summaries = [summarize_run(run, args.expected_term) for run in runs]
    artifact = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "experiment": "full_workflow_hypothesis_ablation_2x2",
        "scenario": args.scenario,
        "fault_run_id": args.fault_run_id,
        "event_id": args.event_id,
        "event_snapshot": event,
        "expected_terms": args.expected_term,
        "method": {
            "paired_event": True,
            "concurrent_trigger": True,
            "engines": ["multi_agent", "agent_skill"],
            "variants": ["with_hypothesis", "without_hypothesis"],
            "with_hypothesis_phases": FULL_PHASES,
            "without_hypothesis_phases": NO_HYPOTHESIS_PHASES,
        },
        "summary": summaries,
        "runs": runs,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
