#!/usr/bin/env python3
"""Run the recordable AgenticSRE OpenTelemetry Demo fault suite.

The suite intentionally uses three representative, previously validated fault
types and prints a compact validation summary for recording:
- Network partition: detects outage and deletes the temporary NetworkChaos.
- CPU stress: detects resource pressure and deletes the temporary stress pod.
- ImagePullBackOff: detects bad image rollout and rolls back the Deployment.

Each scenario must include background load, observability metrics, alert or
detection snapshots, diagnosis, self-healing execution, and recovery evidence.
"""

from __future__ import annotations

import importlib
import json
import os
import time
from typing import Any, Dict, List


RUN_REAL = os.environ.get("AGENTICSRE_RUN_RECORDING_SUITE") == "1"

SCENARIOS = [
    {
        "id": "network_partition",
        "title": "Network partition -> NetworkChaos cleanup",
        "module": "run_network_policy_demo",
        "env": "AGENTICSRE_RUN_NETWORK_DEMO",
    },
    {
        "id": "cpu_stress",
        "title": "CPU stress -> temporary pod cleanup",
        "module": "run_cpu_stress_demo",
        "env": "AGENTICSRE_RUN_CPU_DEMO",
    },
    {
        "id": "image_pull",
        "title": "ImagePullBackOff -> rollout undo",
        "module": "run_image_pull_demo",
        "env": "AGENTICSRE_RUN_IMAGE_DEMO",
    },
]


def _stage_names(stages: List[Dict[str, Any]]) -> List[str]:
    return [str(stage.get("stage") or "") for stage in stages if isinstance(stage, dict)]


def _has_background_load(stages: List[Dict[str, Any]]) -> bool:
    for stage in stages:
        text = json.dumps(stage, ensure_ascii=False)
        if "load-generator" in text and "Running" in text:
            return True
    return False


def _has_observability(stages: List[Dict[str, Any]]) -> bool:
    for stage in stages:
        metrics = stage.get("metrics") if isinstance(stage, dict) else None
        if isinstance(metrics, dict) and metrics:
            meaningful = [v for v in metrics.values() if v not in ("n/a", "", None)]
            if meaningful:
                return True
    return False


def _has_detection(stages: List[Dict[str, Any]]) -> bool:
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        alerts = stage.get("alerts")
        if isinstance(alerts, dict) and alerts.get("count") not in (None, "unknown"):
            return True
        if stage.get("image_pull_pods") or stage.get("chaos_exists") is True or stage.get("stress_exists") is True:
            return True
    return False


def _has_diagnosis(stages: List[Dict[str, Any]]) -> bool:
    for stage in stages:
        if stage.get("stage") in {"diagnosis_and_self_healing_plan", "diagnosis_and_capability"}:
            gate = stage.get("diagnosis_gate") or {}
            return bool(gate.get("ready")) and bool(stage.get("fault_type"))
    return False


def _has_self_healing(stages: List[Dict[str, Any]]) -> bool:
    for stage in stages:
        if stage.get("stage") == "self_healing_executed":
            verification = stage.get("verification") or {}
            return stage.get("success") is True and verification.get("recovered") is True
    return False


def _has_recovery(stages: List[Dict[str, Any]]) -> bool:
    for stage in stages:
        if stage.get("stage") not in {"recovered", "recovery_verified"}:
            continue
        text = json.dumps(stage, ensure_ascii=False)
        if '"http_code": "200"' in text or '"frontend_http_code": "200"' in text:
            return True
    return False


def validate_result(result: Dict[str, Any]) -> Dict[str, Any]:
    stages = result.get("stages") if isinstance(result.get("stages"), list) else []
    checks = {
        "background_load": _has_background_load(stages),
        "observability_metrics": _has_observability(stages),
        "fault_detection": _has_detection(stages),
        "diagnosis_ready": _has_diagnosis(stages),
        "self_healing_success": _has_self_healing(stages),
        "recovery_verified": _has_recovery(stages),
    }
    return {
        "passed": result.get("passed") is True and all(checks.values()),
        "scenario": result.get("scenario"),
        "stage_names": _stage_names(stages),
        "checks": checks,
    }


def run_one(spec: Dict[str, str]) -> Dict[str, Any]:
    print(f"\n\n##### Scenario: {spec['title']} #####", flush=True)
    previous = os.environ.get(spec["env"])
    os.environ[spec["env"]] = "1"
    try:
        module = importlib.import_module(spec["module"])
        started = time.time()
        result = module.run_demo()
        validation = validate_result(result)
        validation["id"] = spec["id"]
        validation["title"] = spec["title"]
        validation["elapsed_s"] = round(time.time() - started, 1)
        print("\n=== scenario_validation ===", flush=True)
        print(json.dumps(validation, indent=2, ensure_ascii=False), flush=True)
        if not validation["passed"]:
            raise RuntimeError(f"scenario validation failed: {validation}")
        return {"result": result, "validation": validation}
    finally:
        if previous is None:
            os.environ.pop(spec["env"], None)
        else:
            os.environ[spec["env"]] = previous


def run_suite() -> Dict[str, Any]:
    if not RUN_REAL:
        print("SKIP recording suite; set AGENTICSRE_RUN_RECORDING_SUITE=1 to run")
        return {"skipped": True}

    validations = []
    for spec in SCENARIOS:
        outcome = run_one(spec)
        validations.append(outcome["validation"])

    summary = {
        "suite": "otel-demo-recording-suite",
        "passed": all(v.get("passed") for v in validations),
        "scenario_count": len(validations),
        "validations": validations,
    }
    print("\n\n=== recording_suite_summary ===", flush=True)
    print(json.dumps(summary, indent=2, ensure_ascii=False), flush=True)
    return summary


def main() -> int:
    result = run_suite()
    return 0 if result.get("skipped") or result.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
