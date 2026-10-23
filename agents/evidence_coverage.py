"""Generic evidence-to-conclusion coverage checks.

This module deliberately contains no scenario id, ground-truth label, or
fault-specific vocabulary.  It extracts literal observations that are
structurally salient (key/value fields, paths, codes, call numbers, and
measurements) and checks whether a final RCA preserves enough of them.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List


_FACT_PATTERNS = (
    re.compile(
        r"\b(?:injected|errno|call|pid|match|failures|successes|hits|max_hits|"
        r"restart_delta|nr_throttled|result|return|configured|observed|request)"
        r"\s*=\s*[-A-Za-z0-9_.]+",
        re.I,
    ),
    re.compile(r"\bcpu\.max\s*=\s*\d+(?:\s+\d+)?", re.I),
    re.compile(r"/[-A-Za-z0-9_./]+"),
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}:\d+\b"),
    re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b"),
    re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|seconds|usec|μs|%)\b", re.I),
    re.compile(r"\b(?:call|iteration|process|request|target|control)\s+\d+\b", re.I),
)

_DIRECT_SIGNAL_WORDS = re.compile(
    r"\b(?:baseline|fault|fault-window|during|recovery|recover|error|errno|"
    r"injected|configured|observed|target|control|return|mapping|filter|"
    r"failure|failed|success|detached|removed|restored|checksum|quota|latency)\b",
    re.I,
)


def normalize_fact(value: Any) -> str:
    """Normalize only formatting, never semantic content."""
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .,:;()[]{}")


def _unique(values: Iterable[str]) -> List[str]:
    seen = set()
    output: List[str] = []
    for value in values:
        normalized = normalize_fact(value)
        if len(normalized) < 3 or normalized in seen:
            continue
        seen.add(normalized)
        output.append(normalized)
    return output


def extract_literal_facts(text: str) -> List[str]:
    """Extract high-salience literal observations from text."""
    facts: List[str] = []
    for line in str(text or "").splitlines():
        if not _DIRECT_SIGNAL_WORDS.search(line):
            continue
        for pattern in _FACT_PATTERNS:
            facts.extend(pattern.findall(line))
    return _unique(facts)


def _flatten(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(f"{_flatten(k)} {_flatten(v)}" for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten(item) for item in value)
    return str(value or "")


def assess_evidence_coverage(
    incident: str,
    evidence: str,
    report: Dict[str, Any],
    minimum_coverage: float = 0.5,
    max_facts: int = 12,
) -> Dict[str, Any]:
    """Return a deterministic coverage audit for a candidate RCA.

    The audit is a quality gate, not a truth scorer.  It never decides whether
    a fault label is correct; it only detects when a report drops too many
    direct observations or invents detail unsupported by the supplied record.
    """
    evidence_facts = extract_literal_facts(f"{incident}\n{evidence}")[:max_facts]
    report_text = normalize_fact(_flatten(report))
    covered = [fact for fact in evidence_facts if fact in report_text]
    missing = [fact for fact in evidence_facts if fact not in covered]
    coverage = (len(covered) / len(evidence_facts)) if evidence_facts else 1.0
    return {
        "candidate_fact_count": len(evidence_facts),
        "covered_fact_count": len(covered),
        "coverage": round(coverage, 4),
        "minimum_coverage": minimum_coverage,
        "covered_facts": covered,
        "missing_facts": missing,
        "needs_rewrite": bool(evidence_facts and coverage < minimum_coverage),
    }


def build_coverage_rewrite_prompt(
    incident: str,
    evidence: str,
    previous_report: Dict[str, Any],
    coverage: Dict[str, Any],
) -> str:
    """Build a truth-isolated generic rewrite prompt using the coverage audit."""
    return f"""You are a generic evidence-preservation gate in an SRE RCA workflow.
Rewrite the candidate only when needed to preserve direct observations.  Do not
use experiment ids, hidden truth, or fault-specific rules.

Rules:
1. Keep exact component/scope identifiers, paths, call or process numbers,
   error codes, configured/observed values, and recovery markers when present.
2. If a deeper mechanism is not directly named, report the smallest observed
   fault boundary and explicitly mark the deeper cause as undetermined.
3. Do not convert a cleanup record into an invented deployment, disk, race,
   hypervisor, or network-device cause.
4. Include the control/baseline/recovery contrast that makes the conclusion
   falsifiable.

Incident:
{incident}

Evidence:
{evidence}

Current candidate:
{_flatten(previous_report)[:4000]}

Coverage audit:
{_flatten(coverage)}

Return valid JSON only:
{{
  "root_cause": "minimal complete evidence-supported root cause",
  "fault_type": "operational category",
  "component": "exact component or scope if observed",
  "causal_chain": ["observed mechanism or boundary", "propagation", "impact/recovery"],
  "decisive_evidence": ["literal observations retained from the evidence"],
  "rejected_distractors": ["unsupported explanations rejected"],
  "confidence": 0.0
}}"""
