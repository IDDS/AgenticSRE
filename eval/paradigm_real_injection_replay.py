#!/usr/bin/env python3
"""Compare all collaboration paradigms on frozen real-injection evidence.

The source cases are successful, cleaned-up fault-injection transactions.  This
benchmark does not inject a new fault and never sends the scenario id, source
record id, injection action, or ground truth to the diagnosis model.  Every
paradigm receives the same incident and the same deterministically routed
evidence, so the comparison isolates the collaboration topology instead of
cluster drift.

Examples:
    python -m eval.paradigm_real_injection_replay --dry-run
    python -m eval.paradigm_real_injection_replay --case replay-dns-error --paradigm chain
    python -m eval.paradigm_real_injection_replay --repeats 1 --parallel 1 \
        --output eval/results/paradigm_real_replay17_20260811.json
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import math
import random
import re
import statistics
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

import yaml

from configs.config_loader import get_config
from eval.hypothesis_ablation import _matches, _normalize
from paradigms import AgentPool, get_paradigm


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "eval" / "real_injection_replay_cases.yaml"
DEFAULT_OUTPUT = ROOT / "eval" / "results" / "paradigm_real_replay_latest.json"
ALL_PARADIGMS = ["chain", "react", "reflection", "plan_and_execute", "debate", "voting"]
AGENT_NAMES = ["metric_agent", "log_agent", "trace_agent", "event_agent"]
EVIDENCE_TYPE_TO_AGENT = {
    "metric": "metric_agent",
    "log": "log_agent",
    "trace": "trace_agent",
    "event": "event_agent",
}


def _mean(values: Iterable[float]) -> float:
    items = list(values)
    return statistics.mean(items) if items else 0.0


def _median(values: Iterable[float]) -> float:
    items = list(values)
    return statistics.median(items) if items else 0.0


def _wilson(successes: int, total: int, z: float = 1.96) -> List[float]:
    if total <= 0:
        return [0.0, 0.0]
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denom
    return [round(max(0.0, centre - margin), 4), round(min(1.0, centre + margin), 4)]


def _mcnemar_exact(a_only: int, b_only: int) -> float:
    discordant = a_only + b_only
    if discordant == 0:
        return 1.0
    smaller = min(a_only, b_only)
    tail = sum(math.comb(discordant, i) for i in range(smaller + 1)) / (2 ** discordant)
    return round(min(1.0, 2 * tail), 6)


def _paired_bootstrap_delta(
    pairs: Sequence[Tuple[bool, bool]], seed: int = 20260811, iterations: int = 10000,
) -> List[float]:
    if not pairs:
        return [0.0, 0.0]
    rng = random.Random(seed)
    deltas: List[float] = []
    for _ in range(iterations):
        draw = [pairs[rng.randrange(len(pairs))] for _ in pairs]
        deltas.append(_mean(float(b) - float(a) for a, b in draw))
    deltas.sort()
    return [
        round(deltas[max(0, int(0.025 * len(deltas)) - 1)], 4),
        round(deltas[min(len(deltas) - 1, int(0.975 * len(deltas)))], 4),
    ]


def _mechanism_family(case_id: str) -> str:
    if case_id in {
        "replay-http-header-delay", "replay-ebpf-connect-denial",
        "replay-network-loss", "replay-dns-error",
    }:
        return "network_request"
    if case_id in {
        "replay-strace-openat-eacces", "replay-seccomp-path-errno",
        "replay-gdb-function-return", "replay-elf-function-return",
        "replay-elf-function-errno", "replay-elf-function-delay",
    }:
        return "syscall_binary"
    if case_id in {
        "replay-container-cpu-stress", "replay-path-io-latency",
        "replay-container-time-offset", "replay-cgroup-cpu-quota",
        "replay-dm-flakey-write-error",
    }:
        return "os_resource_storage"
    return "application_runtime"


def _route_evidence(case: Dict[str, Any]) -> Dict[str, str]:
    """Create four deterministic domain views without using ground truth."""
    evidence = [str(line) for line in case.get("evidence", [])]
    patterns = {
        "metric_agent": re.compile(
            r"\b(?:latency|cpu|memory|quota|count|delta|ms|second|usec|%|nr_throttled|baseline)\b|\d",
            re.I,
        ),
        "log_agent": re.compile(
            r"\b(?:error|errno|fail|failed|failure|denied|status|result|eacces|eio|ok)\b",
            re.I,
        ),
        "trace_agent": re.compile(
            r"\b(?:request|path|process|syscall|call|function|connection|target|control|method|probe)\b",
            re.I,
        ),
        "event_agent": re.compile(
            r"\b(?:baseline|during|fault|after|recovery|cleanup|restart|detached|restored|removed|later)\b",
            re.I,
        ),
    }
    routed: Dict[str, List[str]] = {name: [] for name in AGENT_NAMES}
    for index, line in enumerate(evidence):
        matched = [name for name, pattern in patterns.items() if pattern.search(line)]
        if not matched:
            matched = [AGENT_NAMES[index % len(AGENT_NAMES)]]
        for name in matched:
            routed[name].append(line)
    output = {}
    for name in AGENT_NAMES:
        lines = routed[name]
        output[name] = "\n".join(f"- {line}" for line in lines) if lines else (
            "No domain-specific observation was captured in this frozen record."
        )
    return output


class FrozenEvidenceAgent:
    """Read-only domain-agent substitute for a frozen evidence record."""

    def __init__(self, name: str, summary: str, tracker: Dict[str, List[Dict[str, Any]]]):
        self.name = name
        self.summary = summary
        self.tracker = tracker
        self.evidence_available = bool(
            summary.strip()
            and not summary.lower().startswith("no domain-specific observation")
        )

    async def analyze(self, query: str, namespace: str = "", **_: Any) -> Dict[str, Any]:
        self.tracker.setdefault(self.name, []).append({
            "query": query,
            "available": self.evidence_available,
        })
        return {
            "summary": self.summary,
            "source": "frozen_real_injection_evidence",
            "agent": self.name,
            "namespace": namespace,
            "evidence_available": self.evidence_available,
        }


class CountingLLMClient:
    """Count real model requests made after frozen evidence has been supplied."""

    def __init__(self, wrapped: Any):
        self.wrapped = wrapped
        self.calls = 0
        self._lock = threading.Lock()

    def _increment(self) -> None:
        with self._lock:
            self.calls += 1

    def chat(self, *args: Any, **kwargs: Any) -> str:
        self._increment()
        return self.wrapped.chat(*args, **kwargs)

    def json_chat(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        self._increment()
        return self.wrapped.json_chat(*args, **kwargs)

    async def async_chat(self, *args: Any, **kwargs: Any) -> str:
        self._increment()
        return await self.wrapped.async_chat(*args, **kwargs)

    async def async_json_chat(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        self._increment()
        return await self.wrapped.async_json_chat(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.wrapped, name)


def _build_pool(case: Dict[str, Any]) -> Tuple[AgentPool, Dict[str, str], CountingLLMClient]:
    cfg = copy.deepcopy(get_config())
    cfg.memory.enabled = False
    cfg.evolution.auto_record = False
    pool = AgentPool(cfg, enrichment_enabled=False)
    pool.evidence_contract = copy.deepcopy(case.get("evidence_contract", {}))
    evidence_tracker: Dict[str, List[Dict[str, Any]]] = {}
    pool._evidence_tracker = evidence_tracker
    counting_llm = CountingLLMClient(pool.llm)
    pool.llm = counting_llm
    pool.hypothesis_agent.llm = counting_llm
    pool.correlation_agent.llm = counting_llm
    pool.planning_agent.llm = counting_llm
    routed = _route_evidence(case)
    for name in AGENT_NAMES:
        setattr(pool, name, FrozenEvidenceAgent(name, routed[name], evidence_tracker))
    return pool, routed, counting_llm


def _score_artifact(result: Dict[str, Any], groups: Sequence[Sequence[str]]) -> bool:
    # All fields are produced before ground truth is read for scoring.
    return bool(result.get("root_cause")) and _matches(result, groups)


def _evidence_contract_status(
    case: Dict[str, Any], tracker: Dict[str, List[Dict[str, Any]]]
) -> Dict[str, Any]:
    """Evaluate collection against the case's reviewed minimum evidence set."""
    contract = case.get("evidence_contract") or {}
    required_types = [
        str(item) for item in contract.get("required", [])
        if item in EVIDENCE_TYPE_TO_AGENT
    ]
    supporting_types = [
        str(item) for item in contract.get("supporting", [])
        if item in EVIDENCE_TYPE_TO_AGENT
    ]
    collected_agents = sorted({
        agent for agent, attempts in tracker.items()
        if any(bool(attempt.get("available")) for attempt in attempts)
    })
    collected_types = sorted({
        evidence_type for evidence_type, agent in EVIDENCE_TYPE_TO_AGENT.items()
        if agent in collected_agents
    })
    missing_types = sorted(set(required_types) - set(collected_types))
    supporting_collected = sorted(set(supporting_types) & set(collected_types))
    complete = bool(contract) and not missing_types
    return {
        "status": "COMPLETE" if complete else "INCOMPLETE_EVIDENCE",
        "complete": complete,
        "required": required_types,
        "supporting": supporting_types,
        "inapplicable": list(contract.get("inapplicable", [])),
        "collected": collected_types,
        "missing": missing_types,
        "supporting_collected": supporting_collected,
        "attempts": {name: len(attempts) for name, attempts in sorted(tracker.items())},
    }


def _validate_evidence_contract(case: Dict[str, Any]) -> None:
    contract = case.get("evidence_contract")
    if not isinstance(contract, dict):
        raise ValueError(f"{case.get('id')}: missing evidence_contract")
    required = set(contract.get("required", []))
    supporting = set(contract.get("supporting", []))
    inapplicable = set(contract.get("inapplicable", []))
    known = set(EVIDENCE_TYPE_TO_AGENT)
    unknown = (required | supporting | inapplicable) - known
    if unknown:
        raise ValueError(f"{case.get('id')}: unknown evidence types: {sorted(unknown)}")
    if required & inapplicable or required & supporting or supporting & inapplicable:
        raise ValueError(f"{case.get('id')}: overlapping evidence contract sets")
    if not required:
        raise ValueError(f"{case.get('id')}: evidence contract must require at least one type")


def _validate_ground_truth_reachability(case: Dict[str, Any]) -> None:
    """Reject scoring groups that cannot be reached from model-visible input."""
    visible = _normalize(
        str(case.get("incident", ""))
        + " "
        + " ".join(str(item) for item in case.get("evidence", []))
    )
    unreachable = []
    for group in (case.get("ground_truth") or {}).get("match_groups", []):
        terms = group.get("terms", []) if isinstance(group, dict) else group
        if not terms or not any(_normalize(term) in visible for term in terms):
            unreachable.append(group)
    if unreachable:
        raise ValueError(
            f"{case.get('id')}: scoring groups unreachable from incident/evidence: {unreachable}"
        )


def _apply_execution_profile(pool: AgentPool, paradigm_name: str, profile: str) -> None:
    if paradigm_name == "plan_and_execute" and profile == "plan_fast_contract_v1":
        pool.cfg.pipeline.max_evidence_iterations = 1
        pool.cfg.pipeline.enable_correlation = False
    elif paradigm_name == "plan_and_execute" and profile == "plan_accuracy_contract_v2":
        pool.cfg.pipeline.max_evidence_iterations = 1
        pool.cfg.pipeline.enable_correlation = False
        pool.cfg.pipeline.enable_precision_verifier = True
    elif paradigm_name == "plan_and_execute" and profile == "plan_accuracy_contract_v3":
        pool.cfg.pipeline.max_evidence_iterations = 1
        pool.cfg.pipeline.enable_correlation = False
        pool.cfg.pipeline.enable_precision_verifier = True
        pool.cfg.pipeline.enable_evidence_coverage_gate = True


async def _run_one(
    case: Dict[str, Any], paradigm_name: str, repeat: int,
    execution_profile: str = "default",
) -> Dict[str, Any]:
    pool, routed, counting_llm = _build_pool(case)
    _apply_execution_profile(pool, paradigm_name, execution_profile)
    paradigm = get_paradigm(paradigm_name)(pool)
    started = time.time()
    result = await paradigm.run(
        str(case["incident"]),
        namespace="frozen-real-injection-replay",
    )
    elapsed = time.time() - started
    evidence = _evidence_contract_status(case, getattr(pool, "_evidence_tracker", {}))
    result.evidence_status = evidence["status"]
    result.evidence_contract = copy.deepcopy(case.get("evidence_contract", {}))
    result.evidence_collected = evidence["collected"]
    result.evidence_missing = evidence["missing"]
    artifact = result.to_dict()
    valid = result.status == "completed" and not result.error and bool(result.root_cause)
    groups = case["ground_truth"]["match_groups"]
    diagnosis_correct = valid and _score_artifact(artifact, groups)
    # Strict end-to-end scoring never awards a hit when the required evidence
    # contract was not satisfied, even if the text happens to match.
    correct = evidence["complete"] and diagnosis_correct
    return {
        "case_id": case["id"],
        "source_record": case.get("source_record", ""),
        "mechanism_family": _mechanism_family(case["id"]),
        "repeat": repeat,
        "paradigm": paradigm_name,
        "execution_profile": execution_profile,
        "valid": valid,
        "diagnosis_correct": bool(diagnosis_correct),
        "evidence_complete": bool(evidence["complete"]),
        "evidence_status": evidence["status"],
        "evidence_required": evidence["required"],
        "evidence_supporting": evidence["supporting"],
        "evidence_collected": evidence["collected"],
        "evidence_missing": evidence["missing"],
        "evidence_attempts": evidence["attempts"],
        "correct": bool(correct),
        "latency_s": round(elapsed, 3),
        "actual_model_calls": counting_llm.calls,
        "ground_truth_label": case["ground_truth"]["label"],
        "routed_evidence": routed,
        "result": artifact,
    }


def _run_one_thread(
    case: Dict[str, Any], paradigm_name: str, repeat: int,
    execution_profile: str = "default",
) -> Dict[str, Any]:
    # Several legacy reasoning agents use synchronous LLM calls inside their
    # coroutine.  Give each scheduled sample its own event-loop thread so that
    # --parallel reflects real sample concurrency instead of being blocked by
    # one synchronous planning call.
    return asyncio.run(_run_one(case, paradigm_name, repeat, execution_profile))


def _sample_key(sample: Dict[str, Any]) -> Tuple[str, str, int]:
    return (str(sample["case_id"]), str(sample["paradigm"]), int(sample["repeat"]))


def _aggregate(samples: Sequence[Dict[str, Any]], paradigms: Sequence[str]) -> Dict[str, Any]:
    def is_complete(sample: Dict[str, Any]) -> bool:
        # Backward-compatible default for the earlier frozen matrix, which
        # predates the evidence-contract fields.
        return bool(sample.get("evidence_complete", True))

    by_paradigm: Dict[str, Dict[str, Any]] = {}
    for paradigm in paradigms:
        subset = [s for s in samples if s["paradigm"] == paradigm]
        valid = [s for s in subset if s["valid"]]
        complete = [s for s in subset if is_complete(s)]
        complete_valid = [s for s in complete if s["valid"]]
        strict_successes = sum(bool(s.get("correct", False)) for s in subset)
        conditional_successes = sum(
            bool(s.get("diagnosis_correct", s.get("correct", False)))
            for s in complete
        )
        scheduled = len(subset)
        by_paradigm[paradigm] = {
            "scheduled": scheduled,
            "valid": len(valid),
            "invalid": scheduled - len(valid),
            "evidence_complete": len(complete),
            "incomplete_evidence": scheduled - len(complete),
            "evidence_completeness_rate": round(len(complete) / scheduled, 4) if scheduled else 0.0,
            "correct": strict_successes,
            # Primary strict end-to-end accuracy: incomplete evidence, malformed
            # outputs, and diagnostic misses all remain in the denominator.
            "strict_end_to_end_accuracy": round(strict_successes / scheduled, 4) if scheduled else 0.0,
            "strict_accuracy_wilson_95": _wilson(strict_successes, scheduled),
            "accuracy": round(strict_successes / scheduled, 4) if scheduled else 0.0,
            "accuracy_wilson_95": _wilson(strict_successes, scheduled),
            # Conditional diagnostic quality is still reported, but only after
            # the case-level evidence contract has been met.
            "conditional_correct": conditional_successes,
            "conditional_accuracy": round(
                conditional_successes / len(complete), 4
            ) if complete else 0.0,
            "conditional_accuracy_wilson_95": _wilson(conditional_successes, len(complete)),
            "valid_only_accuracy": round(strict_successes / len(valid), 4) if valid else 0.0,
            "conditional_valid_only_accuracy": round(
                conditional_successes / len(complete_valid)
            , 4) if complete_valid else 0.0,
            "avg_latency_s": round(_mean(float(s["latency_s"]) for s in valid), 3),
            "median_latency_s": round(_median(float(s["latency_s"]) for s in valid), 3),
            "avg_latency_all_s": round(_mean(float(s["latency_s"]) for s in subset), 3),
            "avg_llm_calls": round(_mean(int(s["result"]["metrics"]["llm_calls"]) for s in valid), 3),
            "avg_actual_model_calls": round(_mean(int(s["actual_model_calls"]) for s in valid), 3),
            "avg_agent_calls": round(_mean(int(s["result"]["metrics"]["agent_calls"]) for s in valid), 3),
        }

    pairwise: Dict[str, Dict[str, Any]] = {}
    indexed = {_sample_key(s): s for s in samples}
    case_repeats = sorted({(case_id, repeat) for case_id, _, repeat in indexed})
    for i, first in enumerate(paradigms):
        for second in paradigms[i + 1:]:
            pairs: List[Tuple[bool, bool]] = []
            conditional_pairs: List[Tuple[bool, bool]] = []
            for case_id, repeat in case_repeats:
                a = indexed.get((case_id, first, repeat))
                b = indexed.get((case_id, second, repeat))
                if a is not None and b is not None:
                    pairs.append((bool(a["correct"]), bool(b["correct"])))
                    if is_complete(a) and is_complete(b):
                        conditional_pairs.append((
                            bool(a.get("diagnosis_correct", a["correct"])),
                            bool(b.get("diagnosis_correct", b["correct"])),
                        ))
            first_only = sum(a and not b for a, b in pairs)
            second_only = sum(b and not a for a, b in pairs)
            conditional_first_only = sum(a and not b for a, b in conditional_pairs)
            conditional_second_only = sum(b and not a for a, b in conditional_pairs)
            pairwise[f"{first}__vs__{second}"] = {
                "paired_n": len(pairs),
                "paired_complete_n": len(conditional_pairs),
                "first_only": first_only,
                "second_only": second_only,
                "accuracy_delta_second_minus_first": round(
                    _mean(float(b) - float(a) for a, b in pairs), 4
                ),
                "bootstrap_delta_95": _paired_bootstrap_delta(pairs),
                "mcnemar_exact_p": _mcnemar_exact(first_only, second_only),
                "conditional_first_only": conditional_first_only,
                "conditional_second_only": conditional_second_only,
                "conditional_accuracy_delta_second_minus_first": round(
                    _mean(float(b) - float(a) for a, b in conditional_pairs), 4
                ),
                "conditional_bootstrap_delta_95": _paired_bootstrap_delta(conditional_pairs),
                "conditional_mcnemar_exact_p": _mcnemar_exact(
                    conditional_first_only, conditional_second_only
                ),
            }

    by_family: Dict[str, Dict[str, Any]] = {}
    families = sorted(set(str(s["mechanism_family"]) for s in samples))
    for family in families:
        family_samples = [s for s in samples if s["mechanism_family"] == family]
        by_family[family] = {}
        for paradigm in paradigms:
            subset = [s for s in family_samples if s["paradigm"] == paradigm]
            complete = [s for s in subset if is_complete(s)]
            by_family[family][paradigm] = {
                "n": len(subset),
                "valid": sum(bool(s["valid"]) for s in subset),
                "evidence_complete": len(complete),
                "evidence_completeness_rate": round(len(complete) / len(subset), 4) if subset else 0.0,
                "accuracy": round(_mean(float(s.get("correct", False)) for s in subset), 4) if subset else 0.0,
                "conditional_accuracy": round(
                    _mean(float(s.get("diagnosis_correct", s.get("correct", False))) for s in complete), 4
                ) if complete else 0.0,
            }

    ranking = sorted(
        paradigms,
        key=lambda p: (
            -by_paradigm[p]["strict_end_to_end_accuracy"],
            -by_paradigm[p]["evidence_completeness_rate"],
            by_paradigm[p]["avg_latency_s"],
        ),
    )
    return {
        "by_paradigm": by_paradigm,
        "pairwise": pairwise,
        "by_mechanism_family": by_family,
        "ranking_by_accuracy_then_latency": ranking,
    }


def _atomic_write(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _legacy_collected_agents(sample: Dict[str, Any]) -> Tuple[List[str], str]:
    """Recover actual collection coverage from the completed v1 replay.

    ReAct stores the exact chosen agent names in evidence_summary.  The other
    five v1 implementations execute all four frozen domain agents before their
    synthesis stage; their architecture plus agent_calls>=4 proves collection.
    No agent is credited when neither condition is available.
    """
    paradigm = str(sample.get("paradigm", ""))
    result = sample.get("result") or {}
    summary = result.get("evidence_summary") or {}
    if paradigm == "react":
        agents = sorted(set(summary) & set(AGENT_NAMES))
        return agents, "react_result_evidence_summary_keys"
    if paradigm in {"chain", "reflection", "plan_and_execute", "debate", "voting"}:
        agent_calls = int((result.get("metrics") or {}).get("agent_calls", 0))
        if agent_calls >= 4:
            return list(AGENT_NAMES), "paradigm_architecture_all_domain_agents"
    return [], "unproven_collection"


def _backfill_contract_sample(sample: Dict[str, Any], case: Dict[str, Any]) -> Dict[str, Any]:
    item = copy.deepcopy(sample)
    collected_agents, inference = _legacy_collected_agents(item)
    routed = item.get("routed_evidence") or {}
    tracker = {
        agent: [{
            "available": bool(
                str(routed.get(agent, "")).strip()
                and not str(routed.get(agent, "")).lower().startswith(
                    "no domain-specific observation"
                )
            )
        }]
        for agent in collected_agents
    }
    evidence = _evidence_contract_status(case, tracker)
    diagnosis_correct = bool(item.get("correct", False))
    item.update({
        "diagnosis_correct": diagnosis_correct,
        "evidence_complete": bool(evidence["complete"]),
        "evidence_status": evidence["status"],
        "evidence_required": evidence["required"],
        "evidence_supporting": evidence["supporting"],
        "evidence_collected": evidence["collected"],
        "evidence_missing": evidence["missing"],
        "evidence_attempts": evidence["attempts"],
        "evidence_collection_inference": inference,
        "correct": bool(evidence["complete"] and diagnosis_correct),
    })
    result = item.get("result") or {}
    result.update({
        "evidence_status": evidence["status"],
        "evidence_contract": copy.deepcopy(case.get("evidence_contract", {})),
        "evidence_collected": evidence["collected"],
        "evidence_missing": evidence["missing"],
    })
    item["result"] = result
    return item


def _rescore_sample(sample: Dict[str, Any], case: Dict[str, Any]) -> Dict[str, Any]:
    """Rescore an immutable model output against the current reachable rubric."""
    item = copy.deepcopy(sample)
    valid = bool(item.get("valid", False))
    diagnosis_correct = valid and _score_artifact(
        item.get("result") or {},
        (case.get("ground_truth") or {}).get("match_groups", []),
    )
    evidence_complete = bool(item.get("evidence_complete", True))
    item["diagnosis_correct"] = bool(diagnosis_correct)
    item["correct"] = bool(evidence_complete and diagnosis_correct)
    item["ground_truth_label"] = (case.get("ground_truth") or {}).get("label", "")
    return item


async def run_benchmark(args: argparse.Namespace) -> Dict[str, Any]:
    raw = yaml.safe_load(args.cases.read_text(encoding="utf-8")) or {}
    cases = list(raw.get("cases", []))
    if args.case:
        wanted = set(args.case.split(","))
        cases = [case for case in cases if case["id"] in wanted]
    paradigms = args.paradigm.split(",") if args.paradigm else list(ALL_PARADIGMS)
    unknown = sorted(set(paradigms) - set(ALL_PARADIGMS))
    if unknown:
        raise SystemExit(f"Unknown paradigms: {', '.join(unknown)}")
    if not cases:
        raise SystemExit("No cases selected")
    for case in cases:
        _validate_evidence_contract(case)
        _validate_ground_truth_reachability(case)

    if args.backfill_from:
        previous = json.loads(args.backfill_from.read_text(encoding="utf-8"))
        case_by_id = {str(case["id"]): case for case in cases}
        samples = [
            _backfill_contract_sample(sample, case_by_id[str(sample["case_id"])])
            for sample in previous.get("samples", [])
            if str(sample.get("case_id")) in case_by_id
            and str(sample.get("paradigm")) in paradigms
        ]
        payload = copy.deepcopy(previous)
        payload.update({
            "schema": "agenticsre.paradigm-real-replay.v2-backfill",
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source_result": str(args.backfill_from.resolve().relative_to(ROOT)),
            "source_result_sha256": _sha256(args.backfill_from),
            "source_cases_sha256": _sha256(args.cases),
            "evidence_contract_backfill": (
                "ReAct uses stored selected-agent keys; other completed v1 paradigms "
                "use their code-defined all-domain collection path and agent_calls>=4."
            ),
            "paradigms": paradigms,
            "execution_profile": args.execution_profile,
            "completed_runs": len(samples),
            "samples": sorted(samples, key=_sample_key),
        })
        payload["aggregate"] = _aggregate(payload["samples"], paradigms)
        _atomic_write(args.output, payload)
        return payload

    if args.rescore_from:
        previous = json.loads(args.rescore_from.read_text(encoding="utf-8"))
        case_by_id = {str(case["id"]): case for case in cases}
        samples = [
            _rescore_sample(sample, case_by_id[str(sample["case_id"])])
            for sample in previous.get("samples", [])
            if str(sample.get("case_id")) in case_by_id
            and str(sample.get("paradigm")) in paradigms
        ]
        payload = copy.deepcopy(previous)
        payload.update({
            "schema": "agenticsre.paradigm-real-replay.v2-rescored",
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source_result": str(args.rescore_from.resolve().relative_to(ROOT)),
            "source_result_sha256": _sha256(args.rescore_from),
            "source_cases_sha256": _sha256(args.cases),
            "scoring_policy": "Every match group must be reachable from incident/evidence.",
            "paradigms": paradigms,
            "completed_runs": len(samples),
            "samples": sorted(samples, key=_sample_key),
        })
        payload["aggregate"] = _aggregate(payload["samples"], paradigms)
        _atomic_write(args.output, payload)
        return payload

    if args.recompute_only:
        if not args.output.exists():
            raise SystemExit(f"Output does not exist: {args.output}")
        payload = json.loads(args.output.read_text(encoding="utf-8"))
        payload["aggregate"] = _aggregate(payload.get("samples", []), paradigms)
        payload["aggregate_recomputed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _atomic_write(args.output, payload)
        return payload

    matrix = [
        (repeat, case, paradigm)
        for repeat in range(1, args.repeats + 1)
        for case in cases
        for paradigm in paradigms
    ]
    random.Random(args.seed).shuffle(matrix)
    if args.dry_run:
        return {
            "dry_run": True,
            "cases": [case["id"] for case in cases],
            "paradigms": paradigms,
            "repeats": args.repeats,
            "scheduled_runs": len(matrix),
        }

    cfg = get_config()
    samples: List[Dict[str, Any]] = []
    if args.resume and args.output.exists():
        previous = json.loads(args.output.read_text(encoding="utf-8"))
        samples = list(previous.get("samples", []))
    completed = {_sample_key(sample) for sample in samples}
    semaphore = asyncio.Semaphore(max(1, args.parallel))
    write_lock = asyncio.Lock()

    async def execute(repeat: int, case: Dict[str, Any], paradigm: str) -> None:
        key = (case["id"], paradigm, repeat)
        if key in completed:
            return
        async with semaphore:
            sample = await asyncio.to_thread(
                _run_one_thread, case, paradigm, repeat, args.execution_profile
            )
        async with write_lock:
            samples.append(sample)
            completed.add(key)
            payload = {
                "schema": "agenticsre.paradigm-real-replay.v2",
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "source_cases": str(args.cases.relative_to(ROOT)),
                "source_artifact": raw.get("source_artifact", ""),
                "truth_isolation": raw.get("source_semantics", ""),
                "model": cfg.llm.model,
                "temperature": cfg.llm.temperature,
                "repeats": args.repeats,
                "seed": args.seed,
                "paradigms": paradigms,
                "execution_profile": args.execution_profile,
                "cases": [case["id"] for case in cases],
                "scheduled_runs": len(matrix),
                "completed_runs": len(samples),
                "samples": sorted(samples, key=_sample_key),
            }
            payload["aggregate"] = _aggregate(payload["samples"], paradigms)
            _atomic_write(args.output, payload)
            status = (
                "PASS" if sample["correct"]
                else "INCOMPLETE_EVIDENCE" if not sample["evidence_complete"]
                else "MISS" if sample["valid"]
                else "INVALID"
            )
            print(
                f"[{len(samples)}/{len(matrix)}] {sample['case_id']} {paradigm} "
                f"repeat={repeat} {status} {sample['latency_s']:.1f}s",
                flush=True,
            )

    await asyncio.gather(*(execute(*item) for item in matrix))
    return json.loads(args.output.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--case", help="Comma-separated case ids")
    parser.add_argument("--paradigm", help="Comma-separated paradigm names")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--seed", type=int, default=20260811)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--recompute-only", action="store_true")
    parser.add_argument(
        "--execution-profile", default="default",
        choices=[
            "default", "plan_fast_contract_v1",
            "plan_accuracy_contract_v2", "plan_accuracy_contract_v3",
        ],
        help="Explicit execution profile; plan profiles apply only to Plan-and-Execute",
    )
    parser.add_argument(
        "--backfill-from", type=Path,
        help="Rebuild evidence-contract metrics from a completed v1 replay without new model calls",
    )
    parser.add_argument(
        "--rescore-from", type=Path,
        help="Rescore immutable outputs against the current evidence-reachable rubric",
    )
    args = parser.parse_args()
    if args.repeats < 1:
        raise SystemExit("--repeats must be >= 1")
    report = asyncio.run(run_benchmark(args))
    if report.get("dry_run"):
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(report.get("aggregate", {}), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
