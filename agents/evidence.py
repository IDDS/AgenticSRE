"""Structured evidence helpers shared by RCA agents."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import math
from typing import Any, Dict, List
from operational_ontology import observation_entity


def observed_state_transitions(evidence: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Read timestamped application state records without assigning causality."""
    records, seen = [], set()
    for agent, result in evidence.items():
        if agent == "structured_evidence" or not isinstance(result, dict):
            continue
        for item in result.get("evidence_items", []) or []:
            if not isinstance(item, dict) or item.get("signal_type") != "pod_logs_observed":
                continue
            ref = item.get("raw_ref") or {}
            for line in str(item.get("observation", "")).splitlines():
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict) or row.get("event") != "state_transition":
                        continue
                    if not ref.get("pod") or row.get("pod") != ref["pod"]:
                        continue
                    if "before" not in row or "after" not in row or not row.get("field"):
                        continue
                    timestamp = float(row["timestamp"])
                    if not math.isfinite(timestamp) or timestamp <= 0:
                        continue
                    record = {key: row.get(key) for key in ("timestamp", "pod", "field", "before", "after", "change_id")}
                    record["namespace"] = ref.get("namespace", "")
                    identity = json.dumps(record, sort_keys=True, ensure_ascii=False)
                    if identity not in seen:
                        seen.add(identity)
                        records.append(record)
                except (ValueError, TypeError, KeyError, OverflowError):
                    continue
    return sorted(records, key=lambda row: float(row["timestamp"]))


def ground_state_timeline(report: Dict[str, Any], evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Render observed state changes verbatim instead of model-invented times.

    Scope is deliberately limited to observed application transitions. Other
    incident events and causal ordering are not certified by this function.
    """
    records = observed_state_transitions(evidence)
    if not records:
        return report
    grounded = dict(report)
    grounded["timeline"] = [{"time": str(row["timestamp"]), "event":
        f"{row['namespace']}/{row['pod']}: {row['field']} "
        f"{json.dumps(row['before'], ensure_ascii=False)} → {json.dumps(row['after'], ensure_ascii=False)}",
        "source": "pod_logs_observed", "observation": row} for row in records]
    grounded["timeline_scope"] = "observed_application_state_changes_only"
    return grounded


def format_evidence(evidence: Dict[str, Any], limit: int = 5000) -> str:
    """Deliver observations before lossy LLM summaries to every reasoning stage.

    The structured_evidence entry duplicates agent observations and is omitted.
    Preserve provenance and object scope; a summary is never a substitute for
    a successful measurement, nor does formatting certify an observation.
    """
    facts, summaries, seen = [], [], set()
    fact_groups = []
    for agent, result in evidence.items():
        if agent == "structured_evidence":
            continue
        if not isinstance(result, dict):
            summaries.append(f"[{agent}] {str(result)[:700]}")
            continue
        if result.get("error"):
            summaries.append(f"[{agent}] collection error: {str(result.get('error'))[:300]}")
        agent_facts = []
        for item in result.get("evidence_items", []) or []:
            if not isinstance(item, dict):
                continue
            fact = {k: item[k] for k in (
                "signal_type", "service", "source", "observation", "timestamp", "raw_ref",
            ) if k in item}
            entity = observation_entity(item)
            if entity:
                fact["entity"] = {k: entity[k] for k in ("cluster_id", "namespace", "kind", "name", "uid")}
            encoded = json.dumps(fact, ensure_ascii=False, default=str)
            if encoded not in seen:
                seen.add(encoded)
                agent_facts.append(f"[{agent} observation] {encoded[:1600]}")
        if agent_facts:
            fact_groups.append(agent_facts)
        state = result.get("workload_state")
        if isinstance(state, dict) and state.get("summary"):
            summaries.append(f"[{agent} current state] {str(state['summary'])[:1200]}")
        summary = result.get("summary")
        if summary:
            summaries.append(f"[{agent} summary] {str(summary)[:700]}")
        elif not result.get("evidence_items") and not state:
            summaries.append(f"[{agent}] {json.dumps(result, ensure_ascii=False, default=str)[:700]}")
    # A trace with many spans must not crowd out logs/configuration from another source.
    while any(fact_groups):
        for group in fact_groups:
            if group:
                facts.append(group.pop(0))
    transitions = observed_state_transitions(evidence)
    chronology = []
    if transitions:
        chronology = ["[Observed application state changes, oldest first; not collection times or verified causes] "
                      + json.dumps(transitions[-4:], ensure_ascii=False)]
    return "\n".join(chronology + facts + summaries)[:max(0, limit)]


@dataclass
class EvidenceItem:
    """One normalized observation produced by an evidence agent."""

    signal_type: str
    service: str = ""
    source: str = ""
    observation: str = ""
    severity: str = "info"
    timestamp: str = ""
    supports: List[str] = field(default_factory=list)
    contradicts: List[str] = field(default_factory=list)
    raw_ref: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def make_evidence_item(
    signal_type: str,
    observation: str,
    *,
    service: str = "",
    source: str = "",
    severity: str = "info",
    timestamp: str = "",
    raw_ref: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    return EvidenceItem(
        signal_type=signal_type,
        service=service,
        source=source,
        observation=observation,
        severity=severity,
        timestamp=timestamp,
        raw_ref=raw_ref or {},
    ).to_dict()
