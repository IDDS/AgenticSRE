"""Reusable evidence-grounded adjudication instructions for RCA agents.

The policy is intentionally domain-neutral.  It captures lessons that can be
learned from reviewed incidents without embedding any experiment identifier,
fault-injection action, expected label, or hidden ground truth.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List


CAUSAL_ADJUDICATION_RULES = """Evidence adjudication rules:
1. Reconstruct the active incident timeline. Treat old, already recovered, or
   explicitly stale alerts as distractors unless they overlap the failure window.
2. Rank evidence by causal strength: direct component error/state, correlated
   metrics/logs/traces, and controlled removal or recovery evidence are stronger
   than an alert name or a generic symptom.
3. Separate the initiating mechanism from downstream symptoms and from an
   independent observability failure. Do not call a symptom the root cause.
4. Apply a counterfactual check: the proposed cause must explain both the failure
   and the observed recovery or partial recovery. Explicit contradictions must be
   addressed, not ignored.
5. If the evidence proves a sequence of mechanisms, report the shortest complete
   causal chain needed to explain the incident. Do not collapse a multi-stage
   failure into only its last symptom.
6. Name the exact affected component and mechanism supported by evidence. Avoid a
   broader category when a concrete mechanism is known, and never invent missing
   configuration values, protocols, or implementation details.
7. Choose one primary root cause. Record co-factors or independent failures
   separately and lower confidence when decisive evidence is missing or conflicts.
"""


def build_evidence_adjudication_prompt(incident: str, evidence: str) -> str:
    """Build a truth-isolated final-adjudication prompt for controlled evaluation."""
    return f"""You are the final causal adjudicator in a multi-agent SRE workflow.
The evidence may contain stale alerts, plausible distractors, secondary failures,
or a multi-stage causal chain. Diagnose only from the incident and evidence below.
Do not assume a fault-injection action, experiment identifier, or hidden truth.

{CAUSAL_ADJUDICATION_RULES}

Incident:
{incident}

Evidence:
{evidence}

Return valid JSON only:
{{
  "root_cause": "shortest complete evidence-supported causal conclusion",
  "fault_type": "operational category",
  "component": "exact affected component",
  "causal_chain": ["trigger or mechanism", "propagation", "user-visible impact"],
  "decisive_evidence": ["strongest supporting observation"],
  "rejected_distractors": ["contradicted or stale alternative"],
  "confidence": 0.0
}}"""


def build_hypothesis_refinement_prompt(
    incident: str,
    evidence: str,
    initial_hypotheses: List[Dict[str, Any]],
    reranked_hypotheses: List[Dict[str, Any]],
    selected_id: str,
) -> str:
    """Audit an R0 hypothesis decision and emit an exact final RCA conclusion.

    The R0 choice is explicitly treated as a fallible proposal.  This prevents
    the refinement pass from merely paraphrasing a broad or partially matching
    branch when the evidence proves a narrower or multi-stage mechanism.
    """
    return f"""You are the final Critic in a multi-agent SRE diagnosis workflow.
The previous stage selected one candidate, but that selection is only a proposal.
Audit it against the full evidence and replace it when it is contradicted, too
broad, names the wrong component, or omits a proven stage of the causal chain.
Do not assume a fault-injection action, experiment identifier, expected answer,
or hidden truth.

{CAUSAL_ADJUDICATION_RULES}

Mandatory audit procedure:
1. Extract direct error/state evidence and the exact component it names.
2. Check temporal correlation and any removal, rollback, restart, or recovery.
3. Identify stale alerts, coincident failures, and downstream symptoms.
4. Decide whether selected_id covers the exact mechanism, not merely the same
   component or a broad fault category.
5. Emit the shortest complete causal chain. Preserve the selected branch only
   when it passes all four checks; otherwise synthesize an evidence-supported
   replacement.

Incident:
{incident}

Initial candidates:
{json.dumps(initial_hypotheses, ensure_ascii=False)}

Evidence updates for candidates:
{json.dumps(reranked_hypotheses, ensure_ascii=False)}

Previous selected_id:
{selected_id}

Evidence:
{evidence}

Return valid JSON only:
{{
  "root_cause": "exact evidence-supported root cause or complete causal chain",
  "fault_type": "operational category",
  "component": "exact affected component",
  "decision": "preserved|replaced",
  "causal_chain": ["trigger or mechanism", "propagation", "user-visible impact"],
  "decisive_evidence": ["strongest observation or recovery evidence"],
  "contradictions_checked": ["stale or contradictory signal"],
  "confidence": 0.0
}}"""
