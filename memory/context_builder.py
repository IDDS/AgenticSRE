"""
AgenticSRE Context Builder
Unified context assembly: historical rules, similar faults, expert feedback, domain hints.
SOW: "构建智能体上下文，实现多智能体运维能力持续演化"

Usage:
    builder = ContextBuilder(fault_store=store, feedback_store=fb, domain_adapter=da)
    context = builder.build_context("CPU stress on pod frontend-xxx")
    enriched = builder.enrich_query("CPU stress", context, agent_name="metric_agent")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class AgentContext:
    """
    Aggregated agent context assembled from multiple sources:
    - FaultContextStore: historical rules + similar fault fingerprints
    - ExpertFeedbackStore: recent expert-provided diagnoses
    - DomainAdapter: per-agent domain-specific hints
    - TraceStore: recent execution traces + performance stats
    """
    historical_rules: List[Dict] = field(default_factory=list)
    similar_faults: List[Dict] = field(default_factory=list)
    expert_feedback: List[Dict] = field(default_factory=list)
    domain_hints: Dict[str, str] = field(default_factory=dict)
    recent_traces: List[Dict] = field(default_factory=list)
    performance_stats: Dict = field(default_factory=dict)
    history_retrieval: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return asdict(self)

    def to_context_string(self, agent_name: str = "") -> str:
        """
        Serialize context into a text block that can be prepended to a query.

        Parameters
        ----------
        agent_name : Optional agent name to select targeted domain hint.
        """
        parts: List[str] = []

        # Domain hint for the specific agent
        hint = self.domain_hints.get(agent_name, "") if agent_name else ""
        if hint:
            parts.append(f"# Domain Context\n{hint.strip()}")

        # Historical rules
        if self.historical_rules:
            parts.append("# Historical Diagnostic Rules")
            for i, rule in enumerate(self.historical_rules[:5], 1):
                text = rule.get("text", rule.get("condition", str(rule)))
                parts.append(f"{i}. {text}")

        # Similar past incidents
        if self.similar_faults:
            parts.append("\n# Similar Past Incidents")
            for i, fault in enumerate(self.similar_faults[:3], 1):
                desc = fault.get("description", fault.get("text", ""))[:150]
                root_cause = fault.get("root_cause", "")
                parts.append(f"{i}. {desc}\n   Root Cause: {root_cause}")

        # Expert feedback
        if self.expert_feedback:
            parts.append("\n# Recent Expert Feedback")
            for i, fb in enumerate(self.expert_feedback[:3], 1):
                diagnosis = fb.get("expert_diagnosis", "")[:200]
                incident = fb.get("incident_id", "")
                parts.append(f"{i}. [{incident}] {diagnosis}")

        if not parts:
            return ""

        return "\n".join(parts)

    @property
    def is_empty(self) -> bool:
        return (
            not self.historical_rules
            and not self.similar_faults
            and not self.expert_feedback
            and not self.domain_hints
            and not self.recent_traces
        )

    def get_used_rule_ids(self) -> List[str]:
        """rule_id list of historical rules surfaced into the prompt — feeds record_rule_usage()."""
        return [
            r["rule_id"]
            for r in self.historical_rules[:5]
            if isinstance(r, dict) and r.get("rule_id")
        ]

    def get_recalled_rule_refs(self) -> List[Dict]:
        """Content identities for context exposure, not causal contributions."""
        from memory.fault_context_store import FaultContextStore
        return [{"rule_id": rule["rule_id"], "revision": FaultContextStore.rule_revision(rule)}
                for rule in self.historical_rules[:5]
                if isinstance(rule, dict) and rule.get("rule_id")]


class ContextBuilder:
    """
    Unified context builder: queries all available stores and assembles AgentContext.

    Parameters
    ----------
    fault_store    : FaultContextStore instance
    feedback_store : ExpertFeedbackStore instance
    domain_adapter : DomainAdapter instance
    trace_store    : TraceStore instance
    n_rules        : Number of historical rules to retrieve
    n_faults       : Number of similar faults to retrieve
    n_feedback     : Number of recent feedback entries to retrieve
    n_traces       : Number of recent traces to retrieve
    """

    def __init__(
        self,
        fault_store=None,
        feedback_store=None,
        domain_adapter=None,
        trace_store=None,
        n_rules: int = 5,
        n_faults: int = 3,
        n_feedback: int = 5,
        n_traces: int = 5,
    ):
        self._fault_store = fault_store
        self._feedback_store = feedback_store
        self._domain_adapter = domain_adapter
        self._trace_store = trace_store
        self._n_rules = n_rules
        self._n_faults = n_faults
        self._n_feedback = n_feedback
        self._n_traces = n_traces

    def build_context(self, incident_query: str, agent_name: str = "", historical_context=None, memory_scope=None) -> AgentContext:
        """
        Build an AgentContext by querying all available stores.

        Parameters
        ----------
        incident_query : The incident/alert description
        agent_name     : Optional agent name for targeted domain hints
        """
        ctx = AgentContext()

        # A pipeline supplies its one immutable retrieval snapshot for reuse.
        if historical_context is None and self._fault_store is not None:
            from memory.history_retrieval import retrieve_history
            historical_context = retrieve_history(lambda: self._fault_store, incident_query,
                n_rules=self._n_rules, n_faults=self._n_faults, memory_scope=memory_scope).context
        if historical_context is not None:
            from copy import deepcopy
            ctx.historical_rules = deepcopy(historical_context.get('rules', []))[:self._n_rules]
            ctx.similar_faults = deepcopy(historical_context.get('faults', []))[:self._n_faults]
            ctx.history_retrieval = deepcopy(historical_context.get('retrieval', {}))
            if memory_scope is not None:
                from memory.memory_scope import filter_memory
                # Revalidate supplied detached snapshots too; callers cannot
                # inject an unscoped precomputed top-k into the diagnosis.
                ctx.historical_rules, rule_audit = filter_memory(
                    deepcopy(historical_context.get('rules', [])), memory_scope, 'rules')
                ctx.similar_faults, fault_audit = filter_memory(
                    deepcopy(historical_context.get('faults', [])), memory_scope, 'faults')
                ctx.historical_rules = ctx.historical_rules[:self._n_rules]
                ctx.similar_faults = ctx.similar_faults[:self._n_faults]
                ctx.history_retrieval.update(scope=memory_scope.to_dict(),
                    admission={'rules': rule_audit, 'faults': fault_audit})

        # Expert feedback
        if self._feedback_store is not None:
            try:
                options = {'memory_scope': memory_scope} if memory_scope is not None else {}
                ctx.expert_feedback = self._feedback_store.get_recent_feedback(
                    n=self._n_feedback, for_context=True, **options
                )
            except Exception as e:
                logger.debug("[ContextBuilder] Feedback query failed: %s", e)

        # Domain hints
        if self._domain_adapter is not None:
            try:
                profile = self._domain_adapter.get_active_profile()
                ctx.domain_hints = profile.agent_context_hints
            except Exception as e:
                logger.debug("[ContextBuilder] Domain hints failed: %s", e)

        # Execution traces
        if self._trace_store is not None:
            try:
                if memory_scope is None:
                    ctx.recent_traces = self._trace_store.get_recent_traces(n=self._n_traces)
            except Exception as e:
                logger.debug("[ContextBuilder] Trace query failed: %s", e)

            try:
                if memory_scope is None:
                    ctx.performance_stats = self._trace_store.get_performance_stats()
            except Exception as e:
                logger.debug("[ContextBuilder] Performance stats failed: %s", e)

        if not ctx.is_empty:
            logger.info(
                "[ContextBuilder] Context built: rules=%d faults=%d feedback=%d traces=%d",
                len(ctx.historical_rules), len(ctx.similar_faults),
                len(ctx.expert_feedback), len(ctx.recent_traces),
            )

        return ctx

    def enrich_query(
        self,
        query: str,
        context: Optional[AgentContext] = None,
        agent_name: str = "",
    ) -> str:
        """
        Enrich an incident query by prepending context information.

        If no context is provided, build_context() is called automatically.
        """
        if context is None:
            context = self.build_context(query, agent_name=agent_name)

        context_str = context.to_context_string(agent_name=agent_name)
        if not context_str:
            return query

        return f"{context_str}\n\n# Current Incident\n{query}"
