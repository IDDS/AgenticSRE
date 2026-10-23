"""Skill execution engine."""

from __future__ import annotations

import time
import math
import os
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .adapters import LegacyAgenticSREAdapter
from .policy import build_policy
from .registry import SkillDefinition, SkillRegistry
from .schemas import SkillRun, StageResult, normalize_stage_result
from .tool_context import SkillToolContext, ToolRunner
from .stage_worker import StageWorker, StageFailure


def _candidate_skill_ids(payload: Dict[str, Any]) -> list[str]:
    """Read soft skill hints emitted by Agent/observation layers."""
    ids: list[str] = []
    raw = payload.get("candidate_skills")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                value = item.get("id") or item.get("skill_id") or item.get("candidate_skill")
            else:
                value = item
            text = str(value or "").strip()
            if text and text not in ids:
                ids.append(text)
    for item in payload.get("observation_signals") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("candidate_skill") or "").strip()
        if text and text not in ids:
            ids.append(text)
    return ids


class SkillExecutor:
    STAGE_ORDER = ("detect", "diagnose", "heal", "verify")
    STAGE_COMPOSITION_HELPERS = {
        "generic.root_cause_diagnosis",
        "generic.healing_planner",
        "generic.recovery_verifier",
    }

    def __init__(
        self,
        registry: SkillRegistry,
        *,
        adapters: Optional[Dict[str, Any]] = None,
    ):
        self.registry = registry
        self.stage_timeout_s = float(os.environ.get("AGENTICSRE_SKILL_STAGE_TIMEOUT_S", "30"))
        if not math.isfinite(self.stage_timeout_s) or not 0 < self.stage_timeout_s <= 600:
            raise ValueError("Skill stage timeout must be finite and in (0, 600] seconds")
        self.adapters = adapters or {"legacy": LegacyAgenticSREAdapter()}

    @classmethod
    def from_dir(cls, skills_dir: str | Path) -> "SkillExecutor":
        return cls(SkillRegistry(skills_dir).load())

    def run(
        self,
        skill_id: str,
        payload: Dict[str, Any],
        *,
        stages: Optional[Iterable[str]] = None,
        dry_run: bool = True,
        tool_runner: Optional[ToolRunner] = None,
        _worker: Optional[StageWorker] = None,
    ) -> SkillRun:
        definition = self.registry.get(skill_id)
        selected = tuple(stages or self.STAGE_ORDER)
        tools = SkillToolContext(
            runner=tool_runner,
            dry_run=dry_run,
            payload=payload,
        )
        context: Dict[str, Any] = {
            "payload": payload,
            "dry_run": dry_run,
            "adapters": self.adapters,
            "skill": definition.metadata.to_dict(),
            "tools": tools,
        }
        trace = []
        results: Dict[str, StageResult] = {}
        module_path = getattr(definition.module, "__file__", "")
        session = nullcontext(_worker) if _worker is not None else StageWorker(module_path, tools, self.adapters)
        with session as worker:
            worker.module_path = str(Path(module_path).resolve())
            worker.tools = tools
            for stage in self.STAGE_ORDER:
                if stage not in selected or stage not in definition.metadata.stages:
                    continue
                started = time.time()
                if stage == "diagnose" and results.get("detect") and results["detect"].status == "skipped":
                    result = StageResult(stage=stage, status="skipped",
                        summary="检测未匹配，未生成该故障的诊断结论。")
                    results[stage] = result
                    context[stage] = result.to_dict()
                    trace.append({"stage": stage, "status": "skipped", "duration_ms": 0, "tool_calls": []})
                    continue
                fn = getattr(definition.module, stage, None) if definition.module else None
                if not callable(fn):
                    summary = (
                        f"{stage} not implemented"
                        if definition.module
                        else "文档型 Skill 暂未提供可执行适配器"
                    )
                    result = StageResult(stage=stage, status="skipped", summary=summary)
                    results[stage] = result
                    context[stage] = result.to_dict()
                    trace.append({
                        "stage": stage,
                        "status": result.status,
                        "confidence": result.confidence,
                        "duration_ms": int((time.time() - started) * 1000),
                        "reason": summary,
                    })
                    continue
                try:
                    raw = worker.run(stage, context, self.stage_timeout_s)
                    if isinstance(raw, dict) and "__stage_result__" in raw:
                        raw = StageResult(**raw["__stage_result__"])
                    result = normalize_stage_result(stage, raw)
                    result = self._enforce_stage_contract(stage, result, dry_run=dry_run)
                except StageFailure as exc:
                    worker.close()
                    result = StageResult(stage=stage, status="error", error=exc.code,
                        summary=f"{stage} stopped: {exc.code}",
                        output={"error_detail": exc.detail, "requires_manual_review": True,
                                "outcome_unknown": exc.outcome_unknown, "worker_terminated": True,
                                "automatic_retry": False})
                except Exception as exc:  # pragma: no cover - defensive isolation
                    result = StageResult(stage=stage, status="error", error=str(exc), summary=f"{stage} failed")
                results[stage] = result
                context[stage] = result.to_dict()
                trace.append({
                    "stage": stage,
                    "status": result.status,
                    "confidence": result.confidence,
                    "duration_ms": int((time.time() - started) * 1000),
                    "tool_calls": list(tools.calls),
                })
                if result.status == "error":
                    break
        policy = build_policy(
            definition.metadata,
            results.get("detect"),
            results.get("diagnose"),
            results.get("heal"),
        )
        status = "failed" if any(item.status == "error" for item in results.values()) else "completed"
        if status == "failed":
            policy.update(heal_ready=False, auto_heal_ready=False, requires_approval=True)
            policy["heal_blockers"].append("skill_execution_failed")
            policy["auto_heal_blockers"].append("skill_execution_failed")
            policy["heal_blocker_messages"].append("Skill 阶段失败或超时，需要人工核对，不能继续自愈。")
        implemented_stages = [
            stage
            for stage in self.STAGE_ORDER
            if stage in definition.metadata.stages
            and definition.module
            and callable(getattr(definition.module, stage, None))
        ]
        return SkillRun(
            run_id=f"skill-{uuid.uuid4().hex[:10]}",
            skill=definition.metadata,
            status=status,
            stages=results,
            policy=policy,
            trace=trace,
            requested_stages=list(selected),
            implemented_stages=implemented_stages,
        )

    def _enforce_stage_contract(self, stage: str, result: StageResult, *, dry_run: bool) -> StageResult:
        if stage != "heal" or not dry_run:
            return result
        if bool(result.output.get("executed")):
            return StageResult(
                stage=stage,
                status="error",
                confidence=result.confidence,
                evidence=result.evidence,
                summary="dry-run 模式下 heal 阶段不得报告已执行真实变更",
                output=result.output,
                error="dry_run_contract_violation",
            )
        return result

    def run_matching(
        self,
        payload: Dict[str, Any],
        *,
        stages: Optional[Iterable[str]] = None,
        domain: str = "",
        dry_run: bool = True,
        tool_runner: Optional[ToolRunner] = None,
    ) -> Dict[str, Any]:
        with StageWorker("", None, self.adapters) as worker:
            requested = tuple(stages or self.STAGE_ORDER)
            runs = []
            failed_runs = []
            candidates = self.registry.matching(stages=("detect",), domain=domain)
            if not candidates:
                candidates = self.registry.matching(stages=requested, domain=domain)
            seen_candidates = {item.id for item in candidates}
            for skill_id in _candidate_skill_ids(payload):
                if skill_id in seen_candidates:
                    continue
                try:
                    definition = self.registry.get(skill_id)
                except KeyError:
                    continue
                if "detect" in definition.metadata.stages or set(requested).intersection(definition.metadata.stages):
                    candidates.append(definition)
                    seen_candidates.add(skill_id)
            for definition in candidates:
                probe = self.run(definition.id, payload, stages=("detect",), dry_run=dry_run, tool_runner=tool_runner, _worker=worker)
                if probe.status == "failed":
                    failed_runs.append(probe.to_dict())
                    continue
                detect = probe.stages.get("detect")
                if detect and detect.status in {"ok", "matched"}:
                    if requested == ("detect",):
                        runs.append(probe.to_dict())
                    else:
                        run = self.run(definition.id, payload, stages=requested, dry_run=dry_run, tool_runner=tool_runner, _worker=worker)
                        (failed_runs if run.status == "failed" else runs).append(run.to_dict())
                    continue
                if "detect" not in definition.metadata.stages:
                    run = self.run(definition.id, payload, stages=requested, dry_run=dry_run, tool_runner=tool_runner, _worker=worker)
                    if run.status == "failed":
                        failed_runs.append(run.to_dict())
                        continue
                    diagnose = run.stages.get("diagnose")
                    if diagnose and diagnose.status in {"ok", "matched"}:
                        runs.append(run.to_dict())
            executed_ids = {str((run.get("skill") or {}).get("id") or "") for run in runs + failed_runs}
            for definition in self.registry.matching(stages=requested, domain=domain):
                if definition.id in executed_ids or "detect" in definition.metadata.stages:
                    continue
                if definition.metadata.domain != domain and definition.id not in self.STAGE_COMPOSITION_HELPERS:
                    continue
                selected_stages = [stage for stage in requested if stage in definition.metadata.stages]
                if not selected_stages:
                    continue
                run = self.run(
                    definition.id,
                    payload,
                    stages=selected_stages,
                    dry_run=dry_run,
                    tool_runner=tool_runner,
                    _worker=worker,
                )
                if run.status == "failed":
                    failed_runs.append(run.to_dict())
                    continue
                if any(result.status in {"ok", "matched"} for result in run.stages.values()):
                    runs.append(run.to_dict())
                    executed_ids.add(definition.id)
            return {
                "matched_count": len(runs),
                "failed_runs": failed_runs,
                "runs": runs,
            }
