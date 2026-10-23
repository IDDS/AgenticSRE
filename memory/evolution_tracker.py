"""
AgenticSRE Evolution Tracker
Records system improvement snapshots: knowledge base growth, diagnostic accuracy,
response latency trends, and quality judge scores.
SOW: "利用...系统反馈...实现多智能体运维能力的持续演化"
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_SNAPSHOT_DIR = "./data/evolution"


@dataclass
class EvolutionSnapshot:
    """A point-in-time snapshot of system state."""
    timestamp: float = 0.0
    rule_count: int = 0
    fault_context_count: int = 0
    feedback_count: int = 0
    trace_count: int = 0
    rca_confidence: float = 0.0
    rca_latency_s: float = 0.0
    judge_score: float = 0.0
    paradigm_name: str = ""
    incident_query: str = ""
    # Stage-2 dimensions — surfaced after the recall/feedback loop was wired end-to-end.
    rules_recalled_count: int = 0
    rules_used_count: int = 0
    avg_rule_quality_score: float = 0.0
    fault_type: str = ""


class EvolutionTracker:
    """
    Tracks AgenticSRE system evolution over time.

    Records snapshots after each paradigm/RCA run and provides trend reports.

    Usage:
        tracker = EvolutionTracker.from_config()
        tracker.record_snapshot(fault_store=store, result=result_dict)
        report = tracker.get_evolution_report()
    """

    def __init__(self, snapshot_dir: Optional[str] = None, max_snapshots: int = 1000):
        self._dir = Path(snapshot_dir or _DEFAULT_SNAPSHOT_DIR)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._snapshot_file = self._dir / "snapshots.json"
        self._max = max_snapshots
        self._snapshots: List[EvolutionSnapshot] = []
        self._load()

    @classmethod
    def from_config(cls) -> "EvolutionTracker":
        """Create from global AppConfig."""
        from configs.config_loader import get_config
        cfg = get_config()
        return cls(
            snapshot_dir=cfg.evolution.snapshot_dir or None,
            max_snapshots=cfg.evolution.max_snapshots,
        )

    def _load(self):
        if self._snapshot_file.exists():
            try:
                data = json.loads(self._snapshot_file.read_text(encoding="utf-8"))
                known = {f for f in EvolutionSnapshot.__dataclass_fields__}
                self._snapshots = [
                    EvolutionSnapshot(**{k: v for k, v in s.items() if k in known})
                    for s in data[-self._max:]
                ]
            except Exception as e:
                logger.warning("Failed to load evolution snapshots: %s", e)

    def _save(self):
        data = [asdict(s) for s in self._snapshots[-self._max:]]
        self._snapshot_file.write_text(
            json.dumps(data, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    def record_snapshot(
        self,
        fault_store=None,
        feedback_store=None,
        trace_store=None,
        rca_result: Optional[Dict] = None,
        paradigm_name: str = "",
        incident_query: str = "",
    ) -> Dict:
        """
        Record a new evolution snapshot.

        Parameters
        ----------
        fault_store     : FaultContextStore instance
        feedback_store  : ExpertFeedbackStore instance
        trace_store     : TraceStore instance
        rca_result      : Result dict from RCA / paradigm run
        paradigm_name   : Name of the paradigm used
        incident_query  : The incident description
        """
        snap = EvolutionSnapshot(timestamp=time.time())

        if fault_store is not None:
            try:
                stats = fault_store.stats()
                snap.rule_count = stats.get("rules_count", 0)
                snap.fault_context_count = stats.get("faults_count", 0)
                snap.avg_rule_quality_score = float(stats.get("avg_quality_score", 0) or 0)
            except Exception:
                pass

        if feedback_store is not None:
            try:
                fb_stats = feedback_store.get_feedback_stats()
                snap.feedback_count = fb_stats.get("total", 0)
            except Exception:
                pass

        if trace_store is not None:
            try:
                perf = trace_store.get_performance_stats()
                snap.trace_count = perf.get("total_pipelines", 0)
            except Exception:
                pass

        if rca_result is not None:
            result = rca_result.get("result", rca_result)
            snap.rca_confidence = result.get("confidence", 0)
            snap.rca_latency_s = rca_result.get("metrics", {}).get("latency_s", 0)
            judge = rca_result.get("judge", {})
            snap.judge_score = judge.get("combined_score", 0)
            snap.fault_type = str(result.get("fault_type", "") or "")[:60]
            used_ids = result.get("used_rule_ids") or []
            snap.rules_recalled_count = len(used_ids)
            snap.rules_used_count = len(used_ids)

        snap.paradigm_name = paradigm_name
        snap.incident_query = (incident_query or "")[:200]

        self._snapshots.append(snap)
        self._save()

        logger.info(
            "[Evolution] Snapshot recorded: rules=%d (used=%d avg_q=%.2f) faults=%d conf=%.2f type=%s",
            snap.rule_count, snap.rules_used_count, snap.avg_rule_quality_score,
            snap.fault_context_count, snap.rca_confidence, snap.fault_type or "?",
        )
        return asdict(snap)

    def get_evolution_report(self, fault_store=None) -> Dict:
        """Generate a comprehensive evolution report with trends.

        Pass `fault_store` to enable data-driven recommendations that scan real rule state
        (never-hit rules, low-quality high-traffic rules, conflicting conclusions).
        """
        if not self._snapshots:
            return {
                "total_snapshots": 0,
                "summary": "No evolution data yet.",
                "recommendations": ["Run RCA or paradigm evaluations to create evolution snapshots."],
            }

        first = self._snapshots[0]
        last = self._snapshots[-1]
        span_hours = (last.timestamp - first.timestamp) / 3600 if len(self._snapshots) > 1 else 0

        # Compute trends
        confidences = [s.rca_confidence for s in self._snapshots if s.rca_confidence > 0]
        latencies = [s.rca_latency_s for s in self._snapshots if s.rca_latency_s > 0]
        judge_scores = [s.judge_score for s in self._snapshots if s.judge_score > 0]

        mid = len(confidences) // 2
        first_half_conf = sum(confidences[:mid]) / max(mid, 1) if mid > 0 else 0
        second_half_conf = sum(confidences[mid:]) / max(len(confidences) - mid, 1) if confidences else 0

        if second_half_conf > first_half_conf + 0.05:
            trend = "improving"
        elif second_half_conf < first_half_conf - 0.05:
            trend = "declining"
        else:
            trend = "stable"

        report = {
            "total_snapshots": len(self._snapshots),
            "time_range": {
                "first": time.strftime("%Y-%m-%d %H:%M", time.localtime(first.timestamp)),
                "last": time.strftime("%Y-%m-%d %H:%M", time.localtime(last.timestamp)),
                "span_hours": round(span_hours, 1),
            },
            "trends": {
                "rule_growth": {
                    "initial": first.rule_count,
                    "current": last.rule_count,
                    "net_growth": last.rule_count - first.rule_count,
                },
                "confidence": {
                    "average": sum(confidences) / max(len(confidences), 1),
                    "latest": confidences[-1] if confidences else 0,
                    "trend": trend,
                },
                "latency": {
                    "average_seconds": sum(latencies) / max(len(latencies), 1),
                    "latest_seconds": latencies[-1] if latencies else 0,
                },
                "judge_quality": {
                    "average_score": sum(judge_scores) / max(len(judge_scores), 1),
                    "reviews_needed": sum(1 for s in judge_scores if s < 0.65),
                },
                "rule_usage": {
                    "avg_recalled_per_incident": round(
                        sum(s.rules_recalled_count for s in self._snapshots) / max(len(self._snapshots), 1), 2
                    ),
                    "incidents_with_zero_recall": sum(1 for s in self._snapshots if s.rules_recalled_count == 0),
                    "latest_avg_quality": round(last.avg_rule_quality_score, 3),
                },
            },
            "by_fault_type": self._slice_by_fault_type(),
            "summary": (
                f"System has processed {len(self._snapshots)} incidents over {span_hours:.1f}h. "
                f"Knowledge base: {last.rule_count} rules, {last.fault_context_count} fault contexts. "
                f"Confidence trend: {trend}."
            ),
        }
        report["recommendations"] = self.recommendations(fault_store=fault_store)
        return report

    def _slice_by_fault_type(self) -> Dict[str, Dict]:
        """Group snapshots by fault_type and aggregate per-type metrics."""
        buckets: Dict[str, List[EvolutionSnapshot]] = {}
        for s in self._snapshots:
            key = s.fault_type or "unknown"
            buckets.setdefault(key, []).append(s)

        out: Dict[str, Dict] = {}
        for ftype, items in buckets.items():
            confs = [x.rca_confidence for x in items if x.rca_confidence > 0]
            lats = [x.rca_latency_s for x in items if x.rca_latency_s > 0]
            judges = [x.judge_score for x in items if x.judge_score > 0]
            out[ftype] = {
                "count": len(items),
                "avg_confidence": round(sum(confs) / max(len(confs), 1), 3) if confs else 0,
                "avg_latency_s": round(sum(lats) / max(len(lats), 1), 2) if lats else 0,
                "avg_judge_score": round(sum(judges) / max(len(judges), 1), 3) if judges else 0,
                "avg_rules_recalled": round(sum(x.rules_recalled_count for x in items) / len(items), 2),
            }
        return out

    def recommendations(self, window: int = 20, fault_store=None) -> List[str]:
        """Generate actionable evolution recommendations.

        When `fault_store` is provided, scans the real rule corpus to surface concrete
        rule-level actions (never-hit, high-traffic-low-quality, conflicts) instead of
        only firing on aggregate-threshold heuristics.
        """
        recent = self._snapshots[-window:]
        if not recent:
            return ["Run RCA or paradigm evaluations to create evolution snapshots."]

        recs: List[str] = []

        # ── Rule-level (data-driven, from fault_store) ──
        if fault_store is not None:
            try:
                rules = fault_store.list_rules(limit=1000)
                now = time.time()
                week_ago = now - 7 * 86400

                never_hit = [
                    r for r in rules
                    if int(float(r.get("usage_count", 0) or 0)) == 0
                    and float(r.get("created_at", r.get("timestamp", now)) or now) < week_ago
                    and r.get("status", "active") == "active"
                ]
                if never_hit:
                    ids = [str(r.get("rule_id", ""))[:14] for r in never_hit[:5]]
                    recs.append(
                        f"Retire {len(never_hit)} rules unused for 7+ days: {', '.join(ids)}"
                        + ("…" if len(never_hit) > 5 else "")
                    )

                high_traffic_low_q = [
                    r for r in rules
                    if int(float(r.get("usage_count", 0) or 0)) >= 5
                    and float(r.get("quality_score", r.get("confidence", 1)) or 1) < 0.4
                ]
                if high_traffic_low_q:
                    ids = [str(r.get("rule_id", ""))[:14] for r in high_traffic_low_q[:5]]
                    recs.append(
                        f"Human-review {len(high_traffic_low_q)} high-traffic rules with quality<0.4: "
                        f"{', '.join(ids)}" + ("…" if len(high_traffic_low_q) > 5 else "")
                    )

                if hasattr(fault_store, "governance_report"):
                    gov = fault_store.governance_report()
                    conflicts = gov.get("conflicts", [])
                    if conflicts:
                        recs.append(
                            f"Resolve {len(conflicts)} conflicting rule clusters "
                            f"(same condition, divergent conclusions) — first: "
                            f"{conflicts[0].get('condition', '')[:80]}"
                        )
            except Exception as e:
                logger.warning("recommendations: fault_store scan failed: %s", e)

        # ── Snapshot-level (aggregate signals from recent runs) ──
        avg_judge = sum(s.judge_score for s in recent) / max(len(recent), 1)
        low_quality = sum(1 for s in recent if s.judge_score and s.judge_score < 0.65)
        avg_conf = sum(s.rca_confidence for s in recent) / max(len(recent), 1)
        avg_latency = sum(s.rca_latency_s for s in recent) / max(len(recent), 1)
        zero_recall = sum(1 for s in recent if s.rules_recalled_count == 0)

        if recent[-1].rule_count == 0:
            recs.append("Seed the memory store with verified RCA rules before running enriched mode.")
        if low_quality:
            recs.append(f"Route {low_quality} recent low-quality RCA results through HITL review before auto-learning.")
        if avg_judge and avg_judge < 0.65:
            recs.append("Tighten evidence requirements in prompts; judge quality is below the learning threshold.")
        if avg_conf and avg_conf < 0.55:
            recs.append("Increase cross-signal evidence collection or add scenario-specific rules for low-confidence incidents.")
        if avg_latency > 120:
            recs.append("Reduce max evidence iterations or prefer plan-and-execute for time-sensitive incidents.")
        if zero_recall >= max(3, len(recent) // 2):
            recs.append(
                f"{zero_recall}/{len(recent)} recent incidents had zero rule recall — "
                "memory may be under-populated or condition phrasing diverges from incident queries."
            )
        if len(recent) >= 4 and recent[-1].rule_count <= recent[0].rule_count:
            recs.append("Review expert feedback coverage; the knowledge base is not growing across recent runs.")

        return recs or ["Evolution is stable; continue collecting reviewed incidents to measure accuracy uplift."]

    def get_trend(self, metric_key: str, window: int = 20) -> List[Dict]:
        """Get recent trend data for a specific metric."""
        recent = self._snapshots[-window:]
        return [
            {
                "timestamp": s.timestamp,
                "value": getattr(s, metric_key, 0),
            }
            for s in recent
            if hasattr(s, metric_key)
        ]
