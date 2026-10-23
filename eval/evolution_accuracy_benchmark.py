#!/usr/bin/env python3
"""Paired R0/R4 diagnosis benchmark for multi-agent workflow evolution.

R0 is the pre-optimization fixed-candidate workflow: generate four branches and
rerank them after evidence. R4 preserves that workflow and adds one bounded final
Critic pass which may replace a broad, contradicted, or mechanism-incomplete
selection. A direct single-pass diagnosis is retained as a secondary baseline.

All variants receive identical incident/evidence. Ground truth is used only after
model calls for offline scoring and is never included in a prompt.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import statistics
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

import yaml

from agents.evidence_adjudicator import build_hypothesis_refinement_prompt
from eval.hypothesis_ablation import (
    AblationRunner,
    DIRECT_PROMPT,
    INITIAL_PROMPT,
    RERANK_PROMPT,
    _matches,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "eval" / "evolution_holdout_cases.yaml"
DEFAULT_OUTPUT = ROOT / "eval" / "results" / "evolution_accuracy_latest.json"


def _mean(values: Iterable[float]) -> float:
    items = list(values)
    return statistics.mean(items) if items else 0.0


def _wilson(successes: int, total: int, z: float = 1.96) -> List[float]:
    if total <= 0:
        return [0.0, 0.0]
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denom
    return [round(max(0.0, centre - margin), 4), round(min(1.0, centre + margin), 4)]


def _mcnemar_exact(r0_only: int, r4_only: int) -> float:
    n = r0_only + r4_only
    if n == 0:
        return 1.0
    k = min(r0_only, r4_only)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2 ** n)
    return round(min(1.0, 2 * tail), 6)


def _paired_bootstrap_delta(samples: Sequence[Dict[str, Any]], iterations: int = 10000) -> List[float]:
    if not samples:
        return [0.0, 0.0]
    rng = random.Random(20260811)
    deltas: List[float] = []
    for _ in range(iterations):
        draw = [samples[rng.randrange(len(samples))] for _ in samples]
        deltas.append(_mean(
            (1.0 if item["r4_correct"] else 0.0) - (1.0 if item["r0_correct"] else 0.0)
            for item in draw
        ))
    deltas.sort()
    return [
        round(deltas[max(0, int(0.025 * len(deltas)) - 1)], 4),
        round(deltas[min(len(deltas) - 1, int(0.975 * len(deltas)))], 4),
    ]


def _call_dict(call) -> Dict[str, Any]:
    return {
        "latency_s": round(call.latency_s, 3),
        "prompt_tokens": call.prompt_tokens,
        "completion_tokens": call.completion_tokens,
        "reasoning_tokens": call.reasoning_tokens,
        "finish_reason": call.finish_reason,
        "error": call.error,
    }


def _root_field_only(item: Dict[str, Any]) -> Dict[str, Any]:
    """Restrict scoring to the concise root-cause fields for a strict secondary metric."""
    return {
        "root_cause": item.get("root_cause", item.get("description", "")),
        "fault_type": item.get("fault_type", ""),
        "component": item.get("component", ""),
    }


def _selected_r0(initial: List[Dict[str, Any]], reranked: List[Dict[str, Any]], selected_id: str) -> Dict[str, Any]:
    initial_item = next((h for h in initial if str(h.get("id", "")) == selected_id), {})
    update = next((h for h in reranked if str(h.get("id", "")) == selected_id), {})
    selected = dict(initial_item)
    selected.update(update)
    if initial_item.get("description"):
        selected["description"] = initial_item["description"]
    return selected


async def run_sample(runner: AblationRunner, case: Dict[str, Any], repeat: int) -> Dict[str, Any]:
    incident = str(case["incident"])
    evidence = "\n".join(f"- {line}" for line in case.get("evidence", []))
    initial_call, direct_call = await asyncio.gather(
        runner.call(INITIAL_PROMPT.format(incident=incident)),
        runner.call(DIRECT_PROMPT.format(incident=incident, evidence=evidence)),
    )
    initial = initial_call.payload.get("hypotheses", []) if not initial_call.error else []
    if not isinstance(initial, list):
        initial = []
    rerank_call = await runner.call(RERANK_PROMPT.format(
        incident=incident,
        hypotheses=json.dumps(initial, ensure_ascii=False),
        evidence=evidence,
    )) if initial else None
    reranked = rerank_call.payload.get("hypotheses", []) if rerank_call and not rerank_call.error else []
    if not isinstance(reranked, list):
        reranked = []
    selected_id = str(rerank_call.payload.get("selected_id", "")) if rerank_call and not rerank_call.error else ""
    if not selected_id and reranked:
        selected_id = str(max(reranked, key=lambda h: float(h.get("confidence", 0) or 0)).get("id", ""))
    r0_output = _selected_r0(initial, reranked, selected_id)

    if rerank_call and not rerank_call.error:
        r4_call = await runner.call(build_hypothesis_refinement_prompt(
            incident=incident,
            evidence=evidence,
            initial_hypotheses=initial,
            reranked_hypotheses=reranked,
            selected_id=selected_id,
        ))
    else:
        r4_call = None

    groups = case["ground_truth"]["match_groups"]
    r0_correct = bool(r0_output) and _matches(r0_output, groups)
    r4_output = r4_call.payload if r4_call and not r4_call.error else {}
    r4_correct = bool(r4_output) and _matches(r4_output, groups)
    direct_correct = not direct_call.error and _matches(direct_call.payload, groups)
    r0_root_field_only_correct = bool(r0_output) and _matches(_root_field_only(r0_output), groups)
    r4_root_field_only_correct = bool(r4_output) and _matches(_root_field_only(r4_output), groups)

    missing = {"latency_s": 0.0, "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "finish_reason": "skipped", "error": "upstream unavailable"}
    return {
        "case_id": case["id"],
        "domain": case["domain"],
        "repeat": repeat,
        "ground_truth_label": case["ground_truth"]["label"],
        "r0_correct": r0_correct,
        "r4_correct": r4_correct,
        "direct_correct": direct_correct,
        "r0_root_field_only_correct": r0_root_field_only_correct,
        "r4_root_field_only_correct": r4_root_field_only_correct,
        "outputs": {
            "initial_hypotheses": initial,
            "reranked_hypotheses": reranked,
            "selected_id": selected_id,
            "r0": r0_output,
            "r4": r4_output,
            "direct": direct_call.payload,
        },
        "calls": {
            "initial": _call_dict(initial_call),
            "rerank": _call_dict(rerank_call) if rerank_call else missing,
            "refinement": _call_dict(r4_call) if r4_call else missing,
            "direct": _call_dict(direct_call),
        },
    }


def aggregate(samples: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    valid = [s for s in samples if not s["calls"]["initial"]["error"] and not s["calls"]["rerank"]["error"] and not s["calls"]["refinement"]["error"]]
    total = len(valid)
    r0_success = sum(bool(s["r0_correct"]) for s in valid)
    r4_success = sum(bool(s["r4_correct"]) for s in valid)
    direct_valid = [s for s in valid if not s["calls"]["direct"]["error"]]
    direct_success = sum(bool(s["direct_correct"]) for s in direct_valid)
    r0_only = sum(bool(s["r0_correct"]) and not bool(s["r4_correct"]) for s in valid)
    r4_only = sum(bool(s["r4_correct"]) and not bool(s["r0_correct"]) for s in valid)
    r0_accuracy = r0_success / total if total else 0.0
    r4_accuracy = r4_success / total if total else 0.0
    r0_root_only = sum(bool(s["r0_root_field_only_correct"]) for s in valid)
    r4_root_only = sum(bool(s["r4_root_field_only_correct"]) for s in valid)
    r0_latency = [float(s["calls"]["initial"]["latency_s"]) + float(s["calls"]["rerank"]["latency_s"]) for s in valid]
    r4_latency = [r0_latency[i] + float(s["calls"]["refinement"]["latency_s"]) for i, s in enumerate(valid)]
    r0_tokens = [sum(int(s["calls"][stage][key]) for stage in ("initial", "rerank") for key in ("prompt_tokens", "completion_tokens")) for s in valid]
    r4_tokens = [r0_tokens[i] + sum(int(s["calls"]["refinement"][key]) for key in ("prompt_tokens", "completion_tokens")) for i, s in enumerate(valid)]
    return {
        "samples_requested": len(samples),
        "samples_valid": total,
        "direct_secondary_accuracy": round(direct_success / len(direct_valid), 4) if direct_valid else 0.0,
        "primary_metric": "scoped lexical RCA rubric: affirmative conclusion and separately labeled distractor handling",
        "r0_correct": r0_success,
        "r0_accuracy": round(r0_accuracy, 4),
        "r0_accuracy_wilson_95": _wilson(r0_success, total),
        "r4_correct": r4_success,
        "r4_accuracy": round(r4_accuracy, 4),
        "r4_accuracy_wilson_95": _wilson(r4_success, total),
        "absolute_improvement": round(r4_accuracy - r0_accuracy, 4),
        "relative_improvement": round((r4_accuracy / r0_accuracy - 1), 4) if r0_accuracy else None,
        "strict_root_field_only": {
            "r0_correct": r0_root_only,
            "r0_accuracy": round(r0_root_only / total, 4) if total else 0.0,
            "r4_correct": r4_root_only,
            "r4_accuracy": round(r4_root_only / total, 4) if total else 0.0,
            "absolute_improvement": round((r4_root_only - r0_root_only) / total, 4) if total else 0.0,
        },
        "paired_bootstrap_delta_95": _paired_bootstrap_delta(valid),
        "paired_outcomes": {
            "r4_only_wins": r4_only,
            "r0_only_wins": r0_only,
            "both_correct": sum(bool(s["r0_correct"]) and bool(s["r4_correct"]) for s in valid),
            "both_wrong": sum(not bool(s["r0_correct"]) and not bool(s["r4_correct"]) for s in valid),
        },
        "mcnemar_exact_p": _mcnemar_exact(r0_only, r4_only),
        "target_absolute_improvement": 0.10,
        "target_met": (
            r4_accuracy - r0_accuracy >= 0.10
            and (round(_mean(r4_latency) / _mean(r0_latency) - 1, 4) if _mean(r0_latency) else 1.0) <= 0.50
        ),
        "efficiency_budget": {
            "max_runtime_overhead": 0.50,
            "max_llm_call_multiplier": 2.0,
            "r0_llm_calls": 2,
            "r4_llm_calls": 3,
            "call_multiplier": 1.5,
        },
        "runtime": {
            "r0_mean_s": round(_mean(r0_latency), 3),
            "r4_mean_s": round(_mean(r4_latency), 3),
            "r4_overhead": round(_mean(r4_latency) / _mean(r0_latency) - 1, 4) if _mean(r0_latency) else None,
        },
        "avg_tokens": {"r0": round(_mean(r0_tokens), 1), "r4": round(_mean(r4_tokens), 1)},
    }


async def async_main(args: argparse.Namespace) -> int:
    cases = yaml.safe_load(Path(args.cases).read_text(encoding="utf-8")).get("cases", [])
    if args.case:
        selected = set(args.case.split(","))
        cases = [case for case in cases if case["id"] in selected]
    if not cases:
        raise SystemExit("no matching evaluation cases")
    if args.rescore:
        report = json.loads(Path(args.rescore).read_text(encoding="utf-8"))
        case_by_id = {case["id"]: case for case in cases}
        samples = report.get("samples", [])
        for sample in samples:
            case = case_by_id.get(sample.get("case_id"))
            if not case:
                continue
            groups = case["ground_truth"]["match_groups"]
            outputs = sample.get("outputs", {})
            sample["r0_correct"] = _matches(outputs.get("r0", {}), groups)
            sample["r4_correct"] = _matches(outputs.get("r4", {}), groups)
            sample["direct_correct"] = _matches(outputs.get("direct", {}), groups)
            sample["r0_root_field_only_correct"] = _matches(_root_field_only(outputs.get("r0", {})), groups)
            sample["r4_root_field_only_correct"] = _matches(_root_field_only(outputs.get("r4", {})), groups)
        report["rescored_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        report["summary"] = aggregate(samples)
        report["by_domain"] = {
            domain: aggregate([s for s in samples if s["domain"] == domain])
            for domain in sorted({s["domain"] for s in samples})
        }
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
        print(output)
        return 0
    runner = AblationRunner(max_tokens=args.max_tokens, concurrency=args.concurrency)
    if args.temperature is not None:
        runner.temperature = args.temperature
    tasks = [run_sample(runner, case, repeat) for case in cases for repeat in range(1, args.repeats + 1)]
    samples: List[Dict[str, Any]] = []
    for completed in asyncio.as_completed(tasks):
        sample = await completed
        samples.append(sample)
        print(json.dumps({
            "case": sample["case_id"], "repeat": sample["repeat"],
            "direct_correct": sample["direct_correct"],
            "r0_correct": sample["r0_correct"], "r4_correct": sample["r4_correct"],
            "errors": {k: v["error"] for k, v in sample["calls"].items() if v["error"]},
        }, ensure_ascii=False), flush=True)
    report = {
        "schema": "agenticsre.evolution-accuracy-benchmark.v2",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": runner.model,
        "temperature": runner.temperature,
        "repeats": args.repeats,
        "truth_isolation": "ground truth used only after all calls; prompts contain no expected label, injection action, or case id",
        "r0_definition": "pre-optimization four-branch generation plus fixed evidence reranking",
        "r4_definition": "R0 plus one bounded evidence-coverage, counterfactual and exact-mechanism Critic refinement",
        "case_ids": [case["id"] for case in cases],
        "summary": aggregate(samples),
        "by_domain": {domain: aggregate([s for s in samples if s["domain"] == domain]) for domain in sorted({s["domain"] for s in samples})},
        "samples": sorted(samples, key=lambda s: (s["case_id"], s["repeat"])),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(output)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--case", help="comma-separated case IDs")
    parser.add_argument("--rescore", help="saved v2 report to rescore without model calls")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--temperature", type=float, default=0.0)
    args = parser.parse_args()
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
