#!/usr/bin/env python3
"""Controlled hypothesis-branch ablation using the configured LLM.

The evaluator compares two diagnosis strategies on identical incident/evidence
fixtures:

1. hypothesis: generate four mutually exclusive branches from the incident, then
   rerank them after evidence arrives;
2. direct: diagnose once from the incident and evidence without exposing an
   explicit candidate list.

Ground truth is loaded only after model calls and is used exclusively for offline
scoring. It is never interpolated into prompts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml
from openai import OpenAI

from configs.config_loader import get_config


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "eval" / "hypothesis_ablation_cases.yaml"
DEFAULT_OUTPUT = ROOT / "eval" / "results" / "hypothesis_ablation_latest.json"


INITIAL_PROMPT = """You are an SRE diagnosis agent. Based only on the incident symptom below,
generate exactly four mutually exclusive root-cause hypotheses. Do not assume an
injection action or hidden ground truth. Rank the branches by current probability.

Incident:
{incident}

Return valid JSON only:
{{"hypotheses":[{{"id":"H1","description":"...","fault_type":"...","component":"...","confidence":0.0}}]}}
"""


RERANK_PROMPT = """You are an SRE evidence-reasoning agent. Rerank the existing hypotheses using
the new evidence. Preserve hypothesis IDs. Reject contradicted branches instead of
rewriting them into a new answer. Select exactly one best branch.

Incident:
{incident}

Existing hypotheses:
{hypotheses}

Evidence:
{evidence}

Return valid JSON only:
{{"hypotheses":[{{"id":"H1","confidence":0.0,"status":"confirmed|active|rejected","reasoning":"..."}}],"selected_id":"H1"}}
"""


ADAPTIVE_RERANK_PROMPT = """You are an SRE evidence-reasoning agent. Rerank the existing hypotheses
using the new evidence. First check whether the current branches cover the strongest
evidence. If none does, add at most two evidence-supported branches. Do not add synonyms
of existing branches and do not use experiment IDs, injection actions, or hidden ground
truth. A branch for the same component but a different failure mechanism does not count
as coverage. New branches must preserve exact service/component names from the evidence.
Do not infer configuration parameters, internal protocols, consistency mechanisms, or
implementation details that are absent from the evidence. If evidence supports only
component unavailability, stop at that granularity. Select exactly one best branch from
existing or new branches, and never select a rejected branch.

Incident:
{incident}

Existing hypotheses:
{hypotheses}

Evidence:
{evidence}

Return valid JSON only:
{{"hypotheses":[{{"id":"H1","confidence":0.0,"status":"confirmed|active|rejected","reasoning":"..."}}],"new_hypotheses":[{{"id":"N1","description":"...","fault_type":"...","component":"...","confidence":0.0,"status":"confirmed|active","reasoning":"why existing branches do not cover the evidence"}}],"selected_id":"H1 or N1"}}
"""


DIRECT_PROMPT = """You are an SRE diagnosis agent. Diagnose the single most likely root cause from
the incident and evidence. Do not enumerate alternative hypotheses. Do not assume an
injection action or hidden ground truth.

Incident:
{incident}

Evidence:
{evidence}

Return valid JSON only:
{{"root_cause":"...","fault_type":"...","component":"...","confidence":0.0}}
"""


@dataclass
class CallResult:
    payload: Dict[str, Any]
    latency_s: float
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    finish_reason: str
    error: Optional[str] = None


def _extract_json(text: str) -> Dict[str, Any]:
    value = (text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
        value = re.sub(r"\s*```$", "", value)
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        start = value.find("{")
        end = value.rfind("}")
        if start >= 0 and end > start:
            parsed = json.loads(value[start : end + 1])
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        raise


def _normalize(value: Any) -> str:
    text = str(value or "").lower().replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", text).strip()


def _branch_text(item: Dict[str, Any]) -> str:
    # Different agents expose the same RCA conclusion through different
    # structured fields.  Fixed-candidate reranking uses ``reasoning`` while the
    # evolved Critic uses an explicit causal chain plus decisive/contradictory
    # evidence.  Score the complete diagnosis artifact rather than silently
    # discarding those fields.
    keys = (
        "description", "root_cause", "root_cause_chain", "fault_type",
        "component", "affected_services", "reasoning", "reasoning_chain",
        "causal_chain", "evidence_summary", "decisive_evidence",
        "contradictions_checked", "rejected_distractors",
    )

    def flatten(value: Any) -> str:
        if isinstance(value, dict):
            return " ".join(flatten(value.get(key, "")) for key in keys)
        if isinstance(value, (list, tuple)):
            return " ".join(flatten(part) for part in value)
        return str(value or "")

    return _normalize(" ".join(flatten(item.get(key, "")) for key in keys))


def _matches(item: Dict[str, Any], match_groups: Sequence[Any]) -> bool:
    """Score a lexical RCA rubric with separate cause and distractor scopes.

    List groups remain backward compatible. New distractor rubrics should use
    {"scope": "distractor", "terms": [...]}; rejected alternatives cannot satisfy
    cause groups. This is a lexical rubric, not a semantic correctness judge.
    """
    if not match_groups:
        return False
    cause = {key: value for key, value in item.items()
             if key not in {"rejected_distractors", "contradictions_checked",
                            "evidence_summary", "decisive_evidence"}}
    cause_text = _branch_text(cause)
    complete_text = _branch_text(item)
    conclusion = _branch_text({key: item.get(key, "") for key in
                              ("root_cause", "description", "fault_type", "component", "affected_services")})
    # Compatibility for the original explicit stale/distractor test rubric.
    legacy_distractor_terms = {"stale", "distractor", "unrelated", "independent"}
    cause_groups = 0
    for group in match_groups:
        if isinstance(group, dict):
            scope, terms = group.get("scope", "cause"), group.get("terms", [])
        else:
            terms = group
            scope = "distractor" if terms and all(
                _normalize(term) in legacy_distractor_terms for term in terms
            ) else "cause"
        if scope not in {"cause", "distractor"} or not isinstance(terms, (list, tuple)) or not terms:
            return False
        text = complete_text if scope == "distractor" else cause_text
        if not any(_normalize(term) and _normalize(term) in text for term in terms):
            return False
        if scope == "cause":
            # The first cause group identifies the selected root/component;
            # mentioning it only in background reasoning is insufficient.
            if cause_groups == 0 and not any(_normalize(term) in conclusion for term in terms):
                return False
            cause_groups += 1
    return cause_groups > 0


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def _mean(values: Iterable[float]) -> float:
    seq = list(values)
    return statistics.mean(seq) if seq else 0.0


class AblationRunner:
    def __init__(self, max_tokens: int = 8192, concurrency: int = 4):
        cfg = get_config().llm
        self.client = OpenAI(api_key=cfg.api_key, base_url=cfg.base_url, timeout=cfg.timeout)
        self.model = cfg.model
        self.temperature = cfg.temperature
        self.max_tokens = max_tokens
        self.semaphore = asyncio.Semaphore(max(1, concurrency))

    @staticmethod
    def _validate_payload(payload: Dict[str, Any], prompt: str) -> None:
        """Reject structurally incomplete JSON instead of scoring it as a diagnosis."""
        if not isinstance(payload, dict):
            raise ValueError("response payload is not an object")
        # Inspect the requested JSON key, not incidental prose.  DIRECT_PROMPT
        # deliberately says "Do not enumerate alternative hypotheses" and was
        # previously misclassified as a hypothesis-array response because of
        # that single word.
        if '"hypotheses"' in prompt.lower():
            items = payload.get("hypotheses")
            if not isinstance(items, list) or not items:
                raise ValueError("missing non-empty hypotheses array")
            for item in items:
                if not isinstance(item, dict) or not str(item.get("id", "")).strip():
                    raise ValueError("hypothesis item missing id")
        else:
            if not any(str(payload.get(key, "")).strip() for key in ("root_cause", "description", "fault_type", "component", "affected_services")):
                raise ValueError("diagnosis payload has no root-cause fields")

    def _call_sync(self, prompt: str) -> CallResult:
        started = time.perf_counter()
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a rigorous SRE evaluator. Respond with valid JSON only."},
                    {"role": "user", "content": prompt},
                ],
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
            elapsed = time.perf_counter() - started
            choice = response.choices[0]
            message = choice.message
            content = message.content or ""
            usage = response.usage
            details = getattr(usage, "completion_tokens_details", None) if usage else None
            reasoning_tokens = int(getattr(details, "reasoning_tokens", 0) or 0)
            if not content:
                raise ValueError(f"empty content (finish_reason={choice.finish_reason}, reasoning_tokens={reasoning_tokens})")
            payload = _extract_json(content)
            self._validate_payload(payload, prompt)
            return CallResult(
                payload=payload,
                latency_s=elapsed,
                prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
                reasoning_tokens=reasoning_tokens,
                finish_reason=str(choice.finish_reason or ""),
            )
        except Exception as exc:
            return CallResult({}, time.perf_counter() - started, 0, 0, 0, "error", str(exc))

    async def call(self, prompt: str, retries: int = 1) -> CallResult:
        async with self.semaphore:
            result = await asyncio.to_thread(self._call_sync, prompt)
            attempts = 0
            while result.error and attempts < retries:
                attempts += 1
                await asyncio.sleep(1)
                result = await asyncio.to_thread(self._call_sync, prompt)
            return result

    async def run_sample(self, case: Dict[str, Any], repeat: int) -> Dict[str, Any]:
        incident = case["incident"]
        evidence = "\n".join(f"- {line}" for line in case.get("evidence", []))

        initial, direct = await asyncio.gather(
            self.call(INITIAL_PROMPT.format(incident=incident)),
            self.call(DIRECT_PROMPT.format(incident=incident, evidence=evidence)),
        )

        initial_hypotheses = initial.payload.get("hypotheses", []) if not initial.error else []
        if not isinstance(initial_hypotheses, list):
            initial_hypotheses = []
        rerank = await self.call(RERANK_PROMPT.format(
            incident=incident,
            hypotheses=json.dumps(initial_hypotheses, ensure_ascii=False),
            evidence=evidence,
        )) if initial_hypotheses else CallResult({}, 0.0, 0, 0, 0, "skipped", "initial hypotheses unavailable")

        groups = case["ground_truth"]["match_groups"]
        initial_matches = [_matches(h, groups) for h in initial_hypotheses]
        initial_correct_ids = {
            str(h.get("id", f"H{index + 1}"))
            for index, h in enumerate(initial_hypotheses)
            if initial_matches[index]
        }

        reranked = rerank.payload.get("hypotheses", []) if not rerank.error else []
        if not isinstance(reranked, list):
            reranked = []
        selected_id = str(rerank.payload.get("selected_id", ""))
        if not selected_id and reranked:
            selected_id = str(max(reranked, key=lambda h: float(h.get("confidence", 0) or 0)).get("id", ""))

        by_id = {str(h.get("id", "")): h for h in initial_hypotheses}
        selected_initial = by_id.get(selected_id, {})
        selected_update = next((h for h in reranked if str(h.get("id", "")) == selected_id), {})
        selected_final = dict(selected_initial)
        selected_final.update(selected_update)
        if selected_initial.get("description"):
            selected_final["description"] = selected_initial["description"]
        selected_match = _matches(selected_final, groups)

        retained = False
        if initial_correct_ids:
            retained = any(
                str(h.get("id", "")) in initial_correct_ids
                and str(h.get("status", "active")).lower() != "rejected"
                and float(h.get("confidence", 0) or 0) >= 0.25
                for h in reranked
            )

        distractors = [h for h in reranked if str(h.get("id", "")) not in initial_correct_ids]
        rejected_distractors = [
            h for h in distractors
            if str(h.get("status", "active")).lower() == "rejected" or float(h.get("confidence", 0) or 0) < 0.25
        ]

        correct_scores = [float(h.get("confidence", 0) or 0) for h in reranked if str(h.get("id", "")) in initial_correct_ids]
        wrong_scores = [float(h.get("confidence", 0) or 0) for h in distractors]
        margin = (max(correct_scores) if correct_scores else 0.0) - (max(wrong_scores) if wrong_scores else 0.0)

        direct_match = _matches(direct.payload, groups) if not direct.error else False

        def call_dict(call: CallResult) -> Dict[str, Any]:
            return {
                "latency_s": round(call.latency_s, 3),
                "prompt_tokens": call.prompt_tokens,
                "completion_tokens": call.completion_tokens,
                "reasoning_tokens": call.reasoning_tokens,
                "finish_reason": call.finish_reason,
                "error": call.error,
            }

        return {
            "case_id": case["id"],
            "domain": case["domain"],
            "repeat": repeat,
            "ground_truth_label": case["ground_truth"]["label"],
            "metrics": {
                "initial_top1_correct": bool(initial_matches and initial_matches[0]),
                "initial_topk_coverage": any(initial_matches),
                "reranked_top1_correct": selected_match,
                "correct_branch_retained": retained,
                "distractor_rejection_rate": len(rejected_distractors) / len(distractors) if distractors else 1.0,
                "branch_confidence_margin": margin,
                "direct_correct": direct_match,
                "hypothesis_latency_s": initial.latency_s + rerank.latency_s,
                "direct_latency_s": direct.latency_s,
                "hypothesis_tokens": initial.prompt_tokens + initial.completion_tokens + rerank.prompt_tokens + rerank.completion_tokens,
                "direct_tokens": direct.prompt_tokens + direct.completion_tokens,
                "hypothesis_llm_calls": 2 if not rerank.error else 1,
                "direct_llm_calls": 1,
            },
            "calls": {"initial": call_dict(initial), "rerank": call_dict(rerank), "direct": call_dict(direct)},
            "outputs": {
                "initial_hypotheses": initial_hypotheses,
                "reranked_hypotheses": reranked,
                "selected_id": selected_id,
                "direct": direct.payload,
            },
        }


def aggregate(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    valid = [s for s in samples if not s["calls"]["initial"]["error"] and not s["calls"]["rerank"]["error"] and not s["calls"]["direct"]["error"]]
    metrics = [s["metrics"] for s in valid]

    def rate(key: str) -> float:
        return _mean(1.0 if m[key] else 0.0 for m in metrics)

    hyp_latency = [float(m["hypothesis_latency_s"]) for m in metrics]
    direct_latency = [float(m["direct_latency_s"]) for m in metrics]
    summary = {
        "samples_requested": len(samples),
        "samples_valid": len(valid),
        "initial_top1_accuracy": round(rate("initial_top1_correct"), 4),
        "initial_topk_coverage": round(rate("initial_topk_coverage"), 4),
        "reranked_top1_accuracy": round(rate("reranked_top1_correct"), 4),
        "correct_branch_retention": round(rate("correct_branch_retained"), 4),
        "distractor_rejection_rate": round(_mean(float(m["distractor_rejection_rate"]) for m in metrics), 4),
        "avg_branch_confidence_margin": round(_mean(float(m["branch_confidence_margin"]) for m in metrics), 4),
        "direct_accuracy": round(rate("direct_correct"), 4),
        "hypothesis_runtime": {
            "mean_s": round(_mean(hyp_latency), 3),
            "p50_s": round(statistics.median(hyp_latency), 3) if hyp_latency else 0.0,
            "p95_s": round(_percentile(hyp_latency, 0.95), 3),
        },
        "direct_runtime": {
            "mean_s": round(_mean(direct_latency), 3),
            "p50_s": round(statistics.median(direct_latency), 3) if direct_latency else 0.0,
            "p95_s": round(_percentile(direct_latency, 0.95), 3),
        },
        "avg_hypothesis_tokens": round(_mean(float(m["hypothesis_tokens"]) for m in metrics), 1),
        "avg_direct_tokens": round(_mean(float(m["direct_tokens"]) for m in metrics), 1),
    }
    if summary["direct_runtime"]["mean_s"]:
        summary["hypothesis_runtime_overhead"] = round(
            summary["hypothesis_runtime"]["mean_s"] / summary["direct_runtime"]["mean_s"] - 1,
            4,
        )
    adaptive_metrics = [m for m in metrics if "adaptive_top1_correct" in m]
    if adaptive_metrics:
        adaptive_latency = [float(m["adaptive_hypothesis_latency_s"]) for m in adaptive_metrics]
        uncovered = [m for m in adaptive_metrics if not m["initial_topk_coverage"]]
        summary.update({
            "adaptive_reranked_top1_accuracy": round(_mean(1.0 if m["adaptive_top1_correct"] else 0.0 for m in adaptive_metrics), 4),
            "adaptive_uncovered_branch_recovery": round(_mean(1.0 if m["adaptive_branch_recovered"] else 0.0 for m in uncovered), 4) if uncovered else 1.0,
            "adaptive_hypothesis_runtime": {
                "mean_s": round(_mean(adaptive_latency), 3),
                "p50_s": round(statistics.median(adaptive_latency), 3) if adaptive_latency else 0.0,
                "p95_s": round(_percentile(adaptive_latency, 0.95), 3),
            },
            "avg_adaptive_hypothesis_tokens": round(_mean(float(m["adaptive_hypothesis_tokens"]) for m in adaptive_metrics), 1),
        })
        if summary["direct_runtime"]["mean_s"]:
            summary["adaptive_runtime_overhead"] = round(
                summary["adaptive_hypothesis_runtime"]["mean_s"] / summary["direct_runtime"]["mean_s"] - 1,
                4,
            )
    return summary


def rescore_samples(samples: List[Dict[str, Any]], cases: Sequence[Dict[str, Any]]) -> None:
    """Recompute semantic match metrics from saved model outputs without new calls."""
    case_by_id = {case["id"]: case for case in cases}
    for sample in samples:
        case = case_by_id.get(sample.get("case_id"))
        if not case:
            continue
        groups = case["ground_truth"]["match_groups"]
        outputs = sample.get("outputs", {})
        initial = outputs.get("initial_hypotheses", [])
        reranked = outputs.get("reranked_hypotheses", [])
        selected_id = str(outputs.get("selected_id", ""))
        metrics = sample.setdefault("metrics", {})

        initial_matches = [_matches(h, groups) for h in initial]
        correct_ids = {
            str(h.get("id", f"H{index + 1}"))
            for index, h in enumerate(initial)
            if initial_matches[index]
        }
        selected_initial = next((h for h in initial if str(h.get("id", "")) == selected_id), {})
        selected_update = next((h for h in reranked if str(h.get("id", "")) == selected_id), {})
        selected = dict(selected_initial)
        selected.update(selected_update)
        if selected_initial.get("description"):
            selected["description"] = selected_initial["description"]
        retained = any(
            str(h.get("id", "")) in correct_ids
            and str(h.get("status", "active")).lower() != "rejected"
            and float(h.get("confidence", 0) or 0) >= 0.25
            for h in reranked
        ) if correct_ids else False
        distractors = [h for h in reranked if str(h.get("id", "")) not in correct_ids]
        rejected = [
            h for h in distractors
            if str(h.get("status", "active")).lower() == "rejected" or float(h.get("confidence", 0) or 0) < 0.25
        ]
        correct_scores = [float(h.get("confidence", 0) or 0) for h in reranked if str(h.get("id", "")) in correct_ids]
        wrong_scores = [float(h.get("confidence", 0) or 0) for h in distractors]

        metrics.update({
            "initial_top1_correct": bool(initial_matches and initial_matches[0]),
            "initial_topk_coverage": any(initial_matches),
            "reranked_top1_correct": _matches(selected, groups),
            "correct_branch_retained": retained,
            "distractor_rejection_rate": len(rejected) / len(distractors) if distractors else 1.0,
            "branch_confidence_margin": (max(correct_scores) if correct_scores else 0.0) - (max(wrong_scores) if wrong_scores else 0.0),
            "direct_correct": _matches(outputs.get("direct", {}), groups),
        })

        adaptive = outputs.get("adaptive_reranked_hypotheses", [])
        if adaptive:
            adaptive_selected_id = str(outputs.get("adaptive_selected_id", ""))
            initial_by_id = {str(h.get("id", "")): h for h in initial}
            adaptive_update = next((h for h in adaptive if str(h.get("id", "")) == adaptive_selected_id), {})
            adaptive_selected = dict(initial_by_id.get(adaptive_selected_id, {}))
            adaptive_selected.update(adaptive_update)
            if initial_by_id.get(adaptive_selected_id, {}).get("description"):
                adaptive_selected["description"] = initial_by_id[adaptive_selected_id]["description"]
            metrics["adaptive_top1_correct"] = _matches(adaptive_selected, groups)
            metrics["adaptive_branch_recovered"] = not any(initial_matches) and metrics["adaptive_top1_correct"]


async def add_adaptive_results(
    runner: AblationRunner,
    samples: List[Dict[str, Any]],
    cases: Sequence[Dict[str, Any]],
) -> None:
    case_by_id = {case["id"]: case for case in cases}

    async def run_one(sample: Dict[str, Any]) -> Tuple[Dict[str, Any], CallResult]:
        case = case_by_id[sample["case_id"]]
        evidence = "\n".join(f"- {line}" for line in case.get("evidence", []))
        initial = sample.get("outputs", {}).get("initial_hypotheses", [])
        call = await runner.call(ADAPTIVE_RERANK_PROMPT.format(
            incident=case["incident"],
            hypotheses=json.dumps(initial, ensure_ascii=False),
            evidence=evidence,
        ))
        return sample, call

    tasks = [run_one(sample) for sample in samples]
    for completed in asyncio.as_completed(tasks):
        sample, call = await completed
        payload = call.payload if not call.error else {}
        existing_updates = payload.get("hypotheses", []) if isinstance(payload.get("hypotheses", []), list) else []
        new_hypotheses = payload.get("new_hypotheses", []) if isinstance(payload.get("new_hypotheses", []), list) else []
        combined = existing_updates + new_hypotheses[:2]
        sample.setdefault("outputs", {})["adaptive_reranked_hypotheses"] = combined
        sample["outputs"]["adaptive_selected_id"] = str(payload.get("selected_id", ""))
        sample.setdefault("calls", {})["adaptive_rerank"] = {
            "latency_s": round(call.latency_s, 3),
            "prompt_tokens": call.prompt_tokens,
            "completion_tokens": call.completion_tokens,
            "reasoning_tokens": call.reasoning_tokens,
            "finish_reason": call.finish_reason,
            "error": call.error,
        }
        initial_call = sample["calls"]["initial"]
        metrics = sample.setdefault("metrics", {})
        metrics["adaptive_hypothesis_latency_s"] = float(initial_call["latency_s"]) + call.latency_s
        metrics["adaptive_hypothesis_tokens"] = (
            int(initial_call.get("prompt_tokens", 0))
            + int(initial_call.get("completion_tokens", 0))
            + call.prompt_tokens
            + call.completion_tokens
        )
        print(json.dumps({"adaptive_case": sample["case_id"], "repeat": sample["repeat"], "rerank_latency_s": round(call.latency_s, 3), "error": call.error}, ensure_ascii=False), flush=True)


async def async_main(args: argparse.Namespace) -> int:
    with Path(args.cases).open(encoding="utf-8") as handle:
        cases = yaml.safe_load(handle).get("cases", [])
    if args.case:
        selected = set(args.case.split(","))
        cases = [case for case in cases if case["id"] in selected]
    if not cases:
        raise SystemExit("no matching evaluation cases")

    if args.rescore:
        source = Path(args.rescore)
        report = json.loads(source.read_text(encoding="utf-8"))
        samples = report.get("samples", [])
        rescore_samples(samples, cases)
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

    if args.adaptive_from:
        source = Path(args.adaptive_from)
        report = json.loads(source.read_text(encoding="utf-8"))
        samples = report.get("samples", [])
        selected_case_ids = {case["id"] for case in cases}
        samples = [sample for sample in samples if sample.get("case_id") in selected_case_ids]
        report["samples"] = samples
        report["case_ids"] = [case["id"] for case in cases]
        runner = AblationRunner(max_tokens=args.max_tokens, concurrency=args.concurrency)
        await add_adaptive_results(runner, samples, cases)
        rescore_samples(samples, cases)
        report["adaptive_branch_expansion"] = True
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
    tasks = [runner.run_sample(case, repeat) for case in cases for repeat in range(1, args.repeats + 1)]
    samples = []
    for completed in asyncio.as_completed(tasks):
        sample = await completed
        samples.append(sample)
        print(json.dumps({"case": sample["case_id"], "repeat": sample["repeat"], "metrics": sample["metrics"]}, ensure_ascii=False), flush=True)

    report = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": runner.model,
        "temperature": runner.temperature,
        "repeats": args.repeats,
        "case_ids": [case["id"] for case in cases],
        "metric_definitions": {
            "initial_top1_accuracy": "正确根因分支在初始候选中排名第一的比例",
            "initial_topk_coverage": "正确根因分支出现在四个初始候选中的比例",
            "reranked_top1_accuracy": "证据重排后最终选择正确分支的比例",
            "correct_branch_retention": "初始已生成正确分支时，证据重排后该分支未被错误淘汰的比例",
            "distractor_rejection_rate": "错误候选被标记 rejected 或置信度降至 0.25 以下的比例",
            "branch_confidence_margin": "最佳正确分支置信度减去最佳错误分支置信度",
        },
        "summary": aggregate(samples),
        "by_domain": {
            domain: aggregate([s for s in samples if s["domain"] == domain])
            for domain in sorted({s["domain"] for s in samples})
        },
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
    parser.add_argument("--rescore", help="existing report to rescore without model calls")
    parser.add_argument("--adaptive-from", help="existing report whose saved initial branches should receive adaptive reranking")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--max-tokens", type=int, default=8192)
    args = parser.parse_args()
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
