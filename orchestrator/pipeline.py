"""
AgenticSRE 5-Phase Pipeline Manager
Wraps the RCA engine with lifecycle management, tracing, and behaviour validation.
Phases: Detection → Hypothesis → Investigation → Reasoning → Recovery
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from configs.config_loader import get_config
from agents import DetectionAgent, AlertAgent
from tools import build_tool_registry, LLMClient
from observability import AgentTracer, MetricsCollector, BehaviorValidator
from memory.trace_store import TraceStore
from orchestrator.rca_engine import run_rca

logger = logging.getLogger(__name__)


class PipelinePhase(str, Enum):
    DETECTION = "detection"
    HYPOTHESIS = "hypothesis"
    INVESTIGATION = "investigation"
    REASONING = "reasoning"
    RECOVERY = "recovery"


@dataclass
class PipelineResult:
    """Result of a single pipeline execution."""
    pipeline_id: str = ""
    trigger: str = ""  # what triggered the pipeline
    phase: PipelinePhase = PipelinePhase.DETECTION
    status: str = "pending"
    result: Dict = field(default_factory=dict)
    duration_s: float = 0
    alerts_compressed: int = 0
    alert_groups: int = 0
    error: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "pipeline_id": self.pipeline_id,
            "trigger": self.trigger,
            "phase": self.phase.value,
            "status": self.status,
            "result": self.result,
            "duration_s": round(self.duration_s, 2),
            "alerts_compressed": self.alerts_compressed,
            "alert_groups": self.alert_groups,
            "error": self.error,
        }


class Pipeline:
    """
    5-phase pipeline orchestrator.
    
    Manages:
    - Detection: poll for anomalies or accept external triggers
    - Alert compression: group alerts before analysis (SOW core)
    - RCA execution: delegate to rca_engine.run_rca
    - Result validation: behaviour validation on the pipeline
    - Metrics & tracing: full observability
    """

    def __init__(self, config=None):
        self.cfg = config or get_config()
        self.registry = build_tool_registry(
            self.cfg, allow_write=self.cfg.runtime.enable_self_healing, isolated=True
        )
        self.llm = LLMClient(self.cfg.llm)
        self.detection_agent = DetectionAgent(self.llm, self.registry, self.cfg)
        self.alert_agent = AlertAgent(self.llm, self.registry, self.cfg)
        self.metrics = MetricsCollector()
        self.trace_store = TraceStore(self.cfg)
        self.validator = BehaviorValidator(self.trace_store, self.cfg)
        self.tracer = AgentTracer(self.trace_store)
        
        self._running = False
        self._history: List[PipelineResult] = []

    # ── Public API ──

    async def run(
        self,
        trigger: str,
        namespace: str = "",
        log_callback: Optional[Callable] = None,
        initial_evidence: Optional[Dict[str, Any]] = None,
        skill_workflow: Optional[Dict[str, Any]] = None,
        analysis_phases: Optional[List[str]] = None,
        trigger_source: str = "manual",
    ) -> PipelineResult:
        """
        Execute the full 5-phase pipeline for a given trigger (incident description).
        
        Args:
            trigger: Incident description or detection signal.
            namespace: K8s namespace scope.
            log_callback: Optional streaming log function.
        """
        result = PipelineResult(
            pipeline_id=f"pipe-{int(time.time())}",
            trigger=trigger,
            status="running",
        )
        start = time.time()

        def log(msg: str):
            logger.info(msg)
            if log_callback:
                log_callback(msg)

        try:
            # Phase 1: Alert Compression (SOW: ≥80% accuracy)
            result.phase = PipelinePhase.DETECTION
            log("📡 Phase 1: Alert Compression & Signal Triage")
            if log_callback:
                log_callback({"event": "phase_start", "phase": 0, "name": "ALERT_COMPRESSION"})
            payload = (initial_evidence or {}).get("payload", {})
            if payload.get("source") == "alert_evidence_dossier":
                alert_result = {"total_alerts": payload["alert_count"], "num_groups": 1, "groups": []}
                log(f"  Loaded evidence dossier {payload['scan_id']} / {payload['group_id']} revision {payload['revision']}")
            elif trigger_source == "manual" and getattr(self.cfg.pipeline, "skip_alert_compression_for_manual_rca", True):
                alert_result = {
                    "agent": "alert_agent",
                    "total_alerts": 1,
                    "num_groups": 1,
                    "compressed_groups": 1,
                    "groups": [],
                    "summary": "Manual RCA trigger; skipped global alert compression.",
                }
                log("  Manual RCA trigger detected; skipped global alert compression")
            else:
                alert_result = await self._phase_alert_compression(trigger, namespace, log)
            result.alerts_compressed = alert_result.get("total_alerts", 0)
            result.alert_groups = alert_result.get("num_groups", alert_result.get("compressed_groups", 0))
            log(f"  Compressed {result.alerts_compressed} alerts → {result.alert_groups} groups")
            if log_callback:
                log_callback({"event": "phase_complete", "phase": 0, "name": "ALERT_COMPRESSION", "notes": f"{result.alerts_compressed} alerts → {result.alert_groups} groups"})

            # Phase 2-5: Delegate to RCA engine
            result.phase = PipelinePhase.HYPOTHESIS
            log("\n🚀 Phase 2-5: Running RCA Engine...")
            rca_result = await run_rca(
                incident_query=self._enrich_query(trigger, alert_result),
                namespace=namespace,
                config=self.cfg,
                log_callback=log,
                registry=self.registry,
                initial_evidence=initial_evidence,
                skill_workflow=skill_workflow,
                analysis_phases=analysis_phases,
            )
            result.result = rca_result
            result.status = rca_result.get("status", "unknown")
            result.phase = PipelinePhase.RECOVERY  # final phase

            # Behavior validation
            log("\n🔍 Validating pipeline behavior...")
            validation = self.validator.validate_pipeline(result.pipeline_id)
            if validation.get("anomalies"):
                for a in validation["anomalies"]:
                    log(f"  ⚠️ Validation anomaly: {a}")

        except Exception as e:
            result.status = "failed"
            result.error = str(e)
            logger.error(f"Pipeline failed: {e}", exc_info=True)
            log(f"❌ Pipeline failed: {e}")

        result.duration_s = time.time() - start
        self._history.append(result)
        return result

    async def detect_and_run(
        self,
        namespace: str = "",
        log_callback: Optional[Callable] = None,
    ) -> Optional[PipelineResult]:
        """
        Phase 1: Run DetectionAgent; if signals found, trigger full pipeline.
        Used by the daemon for continuous monitoring.
        """
        def log(msg: str):
            logger.info(msg)
            if log_callback:
                log_callback(msg)

        log("🔎 Running detection scan...")
        signals = self.detection_agent.detect(namespace=namespace)
        
        if not signals:
            log("  No anomalies detected.")
            return None

        log(f"  ⚡ {len(signals)} detection signals found!")
        trigger = self._signals_to_trigger(signals)
        return await self.run(trigger, namespace, log_callback, trigger_source="detection")

    def get_history(self, limit: int = 20) -> List[Dict]:
        """Return recent pipeline execution history."""
        return [r.to_dict() for r in self._history[-limit:]]

    def get_stats(self) -> Dict:
        """Aggregate pipeline statistics."""
        if not self._history:
            return {"total": 0}
        
        completed = [r for r in self._history if r.status == "completed"]
        failed = [r for r in self._history if r.status == "failed"]
        durations = [r.duration_s for r in completed]
        
        return {
            "total": len(self._history),
            "completed": len(completed),
            "failed": len(failed),
            "avg_duration_s": round(sum(durations) / len(durations), 2) if durations else 0,
            "max_duration_s": round(max(durations), 2) if durations else 0,
            "total_alerts_compressed": sum(r.alerts_compressed for r in self._history),
        }

    # ── Private Methods ──

    async def _phase_alert_compression(
        self, trigger: str, namespace: str, log: Callable
    ) -> Dict:
        """
        SOW core requirement: compress noisy alerts into actionable groups.
        Target: ≥80% accuracy.
        """
        try:
            result = await self.alert_agent.compress_and_recommend(
                namespace=namespace,
            )
            return result
        except Exception as e:
            log(f"  ⚠️ Alert compression failed: {e}, falling back to raw trigger")
            return {"total_alerts": 0, "num_groups": 0, "groups": []}

    def _enrich_query(self, trigger: str, alert_result: Dict) -> str:
        """Enrich the incident query with compressed alert information."""
        groups = alert_result.get("groups", [])
        if not groups:
            return trigger

        enrichment = []
        for g in groups[:3]:  # top 3 alert groups
            observation = g.get("representative", g.get("common_pattern", ""))
            severity = g.get("severity", "unknown")
            count = g.get("alert_count", 0)
            enrichment.append(f"[{severity}] {count} alerts: {observation}; object={g.get('entity', 'unknown')}; grouping is not a verified root cause")
        
        extra = "\n".join(enrichment)
        return f"{trigger}\n\nAlert context:\n{extra}"

    def _signals_to_trigger(self, signals: List) -> str:
        """Convert DetectionSignals into a human-readable trigger description."""
        parts = []
        for s in signals[:5]:
            if hasattr(s, "description"):
                parts.append(f"- [{s.severity}] {s.source}: {s.description}")
            else:
                parts.append(f"- {s}")
        
        return f"Automated detection found {len(signals)} anomaly signals:\n" + "\n".join(parts)
