#!/usr/bin/env python3
"""Merge the evidence-gated baseline matrix with the optimized Plan replay."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from eval.paradigm_real_injection_replay import (
    ALL_PARADIGMS,
    _aggregate,
    _mcnemar_exact,
    _paired_bootstrap_delta,
    _sample_key,
)


ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def merge(base_path: Path, optimized_plan_path: Path, output_path: Path) -> Dict[str, Any]:
    base = json.loads(base_path.read_text(encoding="utf-8"))
    optimized = json.loads(optimized_plan_path.read_text(encoding="utf-8"))
    base_samples = [copy.deepcopy(item) for item in base.get("samples", [])]
    fast_samples = [
        copy.deepcopy(item) for item in optimized.get("samples", [])
        if item.get("paradigm") == "plan_and_execute"
    ]
    if len(fast_samples) != 17:
        raise ValueError(f"optimized Plan result must contain 17 samples, got {len(fast_samples)}")

    replaced = {
        _sample_key(item): item
        for item in base_samples
        if item.get("paradigm") == "plan_and_execute"
    }
    before_after: List[Dict[str, Any]] = []
    for after in fast_samples:
        key = _sample_key(after)
        before = replaced.get(key)
        if before is None:
            raise ValueError(f"missing legacy Plan sample for {key}")
        before_after.append({
            "case_id": after["case_id"],
            "repeat": after["repeat"],
            "legacy_correct": bool(before.get("correct", False)),
            "optimized_correct": bool(after.get("correct", False)),
            "legacy_diagnosis_correct": bool(before.get("diagnosis_correct", before.get("correct", False))),
            "optimized_diagnosis_correct": bool(after.get("diagnosis_correct", after.get("correct", False))),
            "legacy_evidence_complete": bool(before.get("evidence_complete", True)),
            "optimized_evidence_complete": bool(after.get("evidence_complete", False)),
            "legacy_latency_s": before.get("latency_s", 0.0),
            "optimized_latency_s": after.get("latency_s", 0.0),
            "legacy_actual_model_calls": before.get("actual_model_calls", 0),
            "optimized_actual_model_calls": after.get("actual_model_calls", 0),
        })

    merged_samples = [
        item for item in base_samples
        if item.get("paradigm") != "plan_and_execute"
    ] + fast_samples
    merged_samples.sort(key=_sample_key)

    pairs = [
        (row["legacy_correct"], row["optimized_correct"])
        for row in before_after
    ]
    legacy_only = sum(a and not b for a, b in pairs)
    optimized_only = sum(b and not a for a, b in pairs)
    comparison = {
        "paired_n": len(pairs),
        "legacy_correct": sum(a for a, _ in pairs),
        "optimized_correct": sum(b for _, b in pairs),
        "legacy_accuracy": round(sum(a for a, _ in pairs) / len(pairs), 4),
        "optimized_accuracy": round(sum(b for _, b in pairs) / len(pairs), 4),
        "accuracy_delta_optimized_minus_legacy": round(
            sum(float(b) - float(a) for a, b in pairs) / len(pairs), 4
        ),
        "legacy_only": legacy_only,
        "optimized_only": optimized_only,
        "mcnemar_exact_p": _mcnemar_exact(legacy_only, optimized_only),
        "bootstrap_delta_95": _paired_bootstrap_delta(pairs),
        "legacy_avg_latency_s": round(
            sum(float(row["legacy_latency_s"]) for row in before_after) / len(before_after), 3
        ),
        "optimized_avg_latency_s": round(
            sum(float(row["optimized_latency_s"]) for row in before_after) / len(before_after), 3
        ),
        "latency_reduction_pct": round(
            (1 - (
                sum(float(row["optimized_latency_s"]) for row in before_after)
                / sum(float(row["legacy_latency_s"]) for row in before_after)
            )) * 100,
            2,
        ),
        "legacy_avg_actual_model_calls": round(
            sum(float(row["legacy_actual_model_calls"]) for row in before_after) / len(before_after), 3
        ),
        "optimized_avg_actual_model_calls": round(
            sum(float(row["optimized_actual_model_calls"]) for row in before_after) / len(before_after), 3
        ),
        "case_rows": before_after,
    }

    payload: Dict[str, Any] = {
        "schema": "agenticsre.paradigm-real-replay.v2-combined",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_cases": base.get("source_cases", ""),
        "source_cases_sha256": base.get("source_cases_sha256", ""),
        "baseline_result": str(base_path.resolve().relative_to(ROOT)),
        "baseline_result_sha256": _sha256(base_path),
        "optimized_plan_result": str(optimized_plan_path.resolve().relative_to(ROOT)),
        "optimized_plan_result_sha256": _sha256(optimized_plan_path),
        "truth_isolation": base.get("truth_isolation", ""),
        "model": optimized.get("model", base.get("model", "")),
        "temperature": optimized.get("temperature", base.get("temperature", 0.0)),
        "repeats": 1,
        "seed": base.get("seed", 20260811),
        "paradigms": ALL_PARADIGMS,
        "cases": base.get("cases", []),
        "scheduled_runs": len(merged_samples),
        "completed_runs": len(merged_samples),
        "execution_profiles": {
            "baseline_non_plan": "legacy_v1_backfill",
            "plan_and_execute": optimized.get("execution_profile", "plan_fast_contract_v1"),
        },
        "samples": merged_samples,
        "plan_legacy_vs_optimized": comparison,
        "aggregate": _aggregate(merged_samples, ALL_PARADIGMS),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--optimized-plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = merge(args.baseline, args.optimized_plan, args.output)
    print(json.dumps({
        "output": str(args.output),
        "plan_legacy_vs_optimized": payload["plan_legacy_vs_optimized"],
        "aggregate": payload["aggregate"]["by_paradigm"]["plan_and_execute"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
