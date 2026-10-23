"""
AgenticSRE Paradigm Base Framework
Provides AgentPool, ParadigmMetrics, ParadigmResult, and ParadigmBase ABC.
All collaboration paradigms inherit from ParadigmBase.
"""

import asyncio
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from configs.config_loader import get_config, AppConfig
from tools import build_tool_registry, LLMClient, ToolRegistry
from agents import (
    MetricAgent, LogAgent, TraceAgent, EventAgent,
    HypothesisAgent, CorrelationAgent, PlanningAgent,
)
from memory import (
    FaultContextStore, ContextLearner, RCAJudge,
    TraceStore, DomainAdapter, ExpertFeedbackStore,
    EvolutionTracker, ContextBuilder, AgentContext,
)

logger = logging.getLogger(__name__)


# ───────────── Shared Agent/Tool Pool ─────────────

class AgentPool:
    """
    Shared container of Agent/Tool/LLM instances.
    All paradigms reuse the same pool so that comparison is fair
    (same LLM client, same tool registry, same agent instances).
    """

    def __init__(self, config: Optional[AppConfig] = None, enrichment_enabled: bool = True):
        self.cfg = config or get_config()
        self.enrichment_enabled = enrichment_enabled
        # Optional per-incident evidence contract.  Evaluation harnesses and
        # online callers may set this before running a paradigm; leaving it
        # unset preserves legacy behavior while allowing Plan-and-Execute to
        # enforce a minimum sufficient evidence set when one is declared.
        self.evidence_contract: Optional[Dict[str, Any]] = None
        self.registry: ToolRegistry = build_tool_registry(self.cfg)
        self.llm = LLMClient(self.cfg.llm)

        # Domain agents
        self.metric_agent = MetricAgent(self.llm, self.registry)
        self.log_agent = LogAgent(self.llm, self.registry)
        self.trace_agent = TraceAgent(self.llm, self.registry)
        self.event_agent = EventAgent(self.llm, self.registry)

        # Reasoning agents
        self.hypothesis_agent = HypothesisAgent(self.llm)
        self.correlation_agent = CorrelationAgent(self.llm)
        self.planning_agent = PlanningAgent(self.llm, self.registry)

        # Generalization & Evolution components
        self._fault_store: Optional[FaultContextStore] = None
        self._feedback_store: Optional[ExpertFeedbackStore] = None
        self._trace_store: Optional[TraceStore] = None
        self._domain_adapter: Optional[DomainAdapter] = None
        self._evolution_tracker: Optional[EvolutionTracker] = None
        self._context_builder: Optional[ContextBuilder] = None
        self._agent_context: Optional[AgentContext] = None

        try:
            if self.cfg.memory.enabled:
                self._fault_store = FaultContextStore(self.cfg)
                self._trace_store = TraceStore(self.cfg)
            self._feedback_store = ExpertFeedbackStore()
            self._domain_adapter = DomainAdapter.from_config()
            if self.cfg.evolution.enabled:
                self._evolution_tracker = EvolutionTracker.from_config()
            self._context_builder = ContextBuilder(
                fault_store=self._fault_store,
                feedback_store=self._feedback_store,
                domain_adapter=self._domain_adapter,
                trace_store=self._trace_store,
            )
            logger.info("[AgentPool] Generalization components initialized")
        except Exception as e:
            logger.warning("[AgentPool] Generalization init failed (non-fatal): %s", e)

    def domain_agents(self) -> Dict[str, Any]:
        """Return dict of domain agents keyed by name."""
        return {
            "metric_agent": self.metric_agent,
            "log_agent": self.log_agent,
            "trace_agent": self.trace_agent,
            "event_agent": self.event_agent,
        }

    async def run_all_domain_agents(self, query: str, namespace: str = "") -> Dict[str, Dict]:
        """Run all 4 domain agents in parallel, return {name: result_dict}."""
        results = await asyncio.gather(
            self.metric_agent.analyze(query, namespace),
            self.log_agent.analyze(query, namespace),
            self.trace_agent.analyze(query, namespace=namespace),
            self.event_agent.analyze(query, namespace),
            return_exceptions=True,
        )
        names = ["metric_agent", "log_agent", "trace_agent", "event_agent"]
        out = {}
        for name, result in zip(names, results):
            if isinstance(result, Exception):
                out[name] = {"summary": f"Error: {result}", "error": True}
            else:
                out[name] = result
        return out

    def build_context(self, incident_query: str, namespace: str = '') -> Optional[AgentContext]:
        """Build unified context from all stores. Returns None if builder unavailable."""
        if not self.enrichment_enabled:
            return None
        if self._context_builder is None:
            return None
        try:
            from memory.memory_scope import MemoryScope
            self._agent_context = self._context_builder.build_context(incident_query,
                memory_scope=MemoryScope(cluster_id=str(getattr(self.cfg.kubernetes, 'cluster_id', '') or ''),
                                         namespace=namespace))
            return self._agent_context
        except Exception as e:
            logger.warning("[AgentPool] build_context failed: %s", e)
            return None

    async def run_all_domain_agents_enriched(
        self, query: str, namespace: str = "", context: Optional[AgentContext] = None,
    ) -> Dict[str, Dict]:
        """
        Run all 4 domain agents with domain-enriched queries.
        Each agent receives a query enriched with its specific domain hints,
        historical rules, and expert feedback.
        Falls back to baseline (non-enriched) if enrichment_enabled is False.
        """
        if not self.enrichment_enabled:
            return await self.run_all_domain_agents(query, namespace)
        if context is None:
            context = self._agent_context
        if context is None or self._context_builder is None:
            return await self.run_all_domain_agents(query, namespace)

        enrich = self._context_builder.enrich_query
        results = await asyncio.gather(
            self.metric_agent.analyze(enrich(query, context, "metric_agent"), namespace),
            self.log_agent.analyze(enrich(query, context, "log_agent"), namespace),
            self.trace_agent.analyze(enrich(query, context, "trace_agent"), namespace=namespace),
            self.event_agent.analyze(enrich(query, context, "event_agent"), namespace),
            return_exceptions=True,
        )
        names = ["metric_agent", "log_agent", "trace_agent", "event_agent"]
        out = {}
        for name, result in zip(names, results):
            if isinstance(result, Exception):
                out[name] = {"summary": f"Error: {result}", "error": True}
            else:
                out[name] = result
        return out


# ───────────── Metrics ─────────────

@dataclass
class ParadigmMetrics:
    """Quantified performance metrics for a single paradigm run."""
    latency_s: float = 0.0
    agent_calls: int = 0
    llm_calls: int = 0
    token_usage: int = 0
    iterations: int = 0

    def to_dict(self) -> Dict:
        return {
            "latency_s": round(self.latency_s, 2),
            "agent_calls": self.agent_calls,
            "llm_calls": self.llm_calls,
            "token_usage": self.token_usage,
            "iterations": self.iterations,
        }


# ───────────── Result ─────────────

@dataclass
class ParadigmResult:
    """
    Standardized output of any paradigm run.
    Compatible with BenchmarkRunner's evaluation logic.
    """
    paradigm_name: str = ""
    root_cause: str = ""
    confidence: float = 0.0
    fault_type: str = ""
    affected_services: List[str] = field(default_factory=list)
    evidence_summary: Dict[str, str] = field(default_factory=dict)
    reasoning_chain: str = ""
    remediation_suggestion: str = ""
    metrics: ParadigmMetrics = field(default_factory=ParadigmMetrics)
    raw_output: Dict = field(default_factory=dict)
    evidence_status: str = "NOT_ASSESSED"
    evidence_contract: Dict[str, Any] = field(default_factory=dict)
    evidence_collected: List[str] = field(default_factory=list)
    evidence_missing: List[str] = field(default_factory=list)
    evidence_coverage: Dict[str, Any] = field(default_factory=dict)
    status: str = "completed"
    error: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "paradigm_name": self.paradigm_name,
            "root_cause": self.root_cause,
            "confidence": self.confidence,
            "fault_type": self.fault_type,
            "affected_services": self.affected_services,
            "evidence_summary": self.evidence_summary,
            "reasoning_chain": self.reasoning_chain,
            "remediation_suggestion": self.remediation_suggestion,
            "evidence_status": self.evidence_status,
            "evidence_contract": self.evidence_contract,
            "evidence_collected": self.evidence_collected,
            "evidence_missing": self.evidence_missing,
            "evidence_coverage": self.evidence_coverage,
            "metrics": self.metrics.to_dict(),
            "status": self.status,
            "error": self.error,
        }

    def to_rca_compatible(self) -> Dict:
        """Convert to the dict format that benchmark_runner._evaluate_result expects."""
        return {
            "status": self.status,
            "result": {
                "root_cause": self.root_cause,
                "confidence": self.confidence,
                "fault_type": self.fault_type,
                "affected_services": self.affected_services,
                "evidence_summary": self.evidence_summary,
                "reasoning_chain": self.reasoning_chain,
                "remediation_suggestion": self.remediation_suggestion,
            },
        }


# ───────────── Base Class ─────────────

class ParadigmBase(ABC):
    """
    Abstract base for all multi-agent collaboration paradigms.

    Subclasses implement _execute(); the public run() method wraps it with
    timing, error handling, and metrics collection.
    """

    name: str = "base"
    description: str = "Abstract base paradigm"

    def __init__(self, pool: AgentPool):
        self.pool = pool
        self._log_callback: Optional[Callable] = None

    def log(self, msg: str):
        logger.info(msg)
        if self._log_callback:
            self._log_callback(msg)

    async def run(
        self,
        incident_query: str,
        namespace: str = "",
        log_callback: Optional[Callable] = None,
    ) -> ParadigmResult:
        """
        Public entry point. Wraps _execute with timing and error handling.
        """
        self._log_callback = log_callback
        self.log(f"[{self.name}] Starting paradigm execution...")

        metrics = ParadigmMetrics()
        start = time.time()

        try:
            result = await self._execute(incident_query, namespace, metrics)
            metrics.latency_s = time.time() - start
            result.metrics = metrics
            result.paradigm_name = self.name
            self.log(f"[{self.name}] Completed in {metrics.latency_s:.1f}s "
                     f"(agents={metrics.agent_calls}, llm={metrics.llm_calls})")

            # Evolution snapshot
            if (self.pool._evolution_tracker is not None
                    and self.pool.cfg.evolution.auto_record):
                try:
                    self.pool._evolution_tracker.record_snapshot(
                        fault_store=self.pool._fault_store,
                        feedback_store=self.pool._feedback_store,
                        trace_store=self.pool._trace_store,
                        rca_result=result.to_dict(),
                        paradigm_name=self.name,
                        incident_query=incident_query,
                    )
                except Exception as evo_err:
                    logger.debug("[%s] Evolution snapshot failed: %s", self.name, evo_err)

            return result

        except Exception as e:
            metrics.latency_s = time.time() - start
            logger.error(f"[{self.name}] Failed: {e}", exc_info=True)
            self.log(f"[{self.name}] Failed: {e}")
            return ParadigmResult(
                paradigm_name=self.name,
                status="failed",
                error=str(e),
                metrics=metrics,
            )

    @abstractmethod
    async def _execute(
        self,
        incident_query: str,
        namespace: str,
        metrics: ParadigmMetrics,
    ) -> ParadigmResult:
        """Subclass implements the paradigm-specific logic here."""
        ...
