"""通过可复用 SRE Skill 执行 Agent+Skill RCA 的轻量 Agent。

SkillAgent 是面向主系统的调用者。SKILL.md 保持为 Agent 中立的说明文档，
可被 Hermes/龙虾式 Agent 读取；AgenticSRE 特有的执行元数据放在
skill.yaml/skill.py 中，并通过内部执行适配器调用。
"""

from __future__ import annotations

import json
import inspect
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from skill_runtime.executor import SkillExecutor
from skill_runtime.kubectl_safety import readonly_kubectl_args
from skill_runtime.service_evidence import (
    decode_projection, projection_request, evidence_sample, evidence_observed_at, observed_entity_catalog, bind_root_entity,
)
from tools.base_tool import ToolResult
from tools.llm_receipt import JSONCompletion
from orchestrator.confidence_calibrator import calibrate_rca_confidence
from orchestrator.skill_review import review_skill_diagnosis
from orchestrator.skill_scale_evidence import assess_scale_zero_evidence
from diagnostic_strategy import InvestigationController, tool_evidence_catalog


EventSink = Callable[[Dict[str, Any]], None]
KubectlRunner = Callable[[str, str, Dict[str, Any]], Optional[Dict[str, Any]]]


@dataclass
class SkillAgentRequest:
    query: str
    namespace: str = ""
    skill_workflow: Dict[str, Any] = field(default_factory=dict)
    run_id: str = ""


ContextProvider = Callable[[Any], Dict[str, Any]]


@dataclass
class SkillAgentResult:
    status: str
    query: str
    namespace: str
    duration_s: float
    skill_runtime_result: Optional[Dict[str, Any]]
    agent_evidence: Dict[str, Any]
    agent_skill_bridge: Dict[str, Any]
    events: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        diagnosis = (
            self.skill_runtime_result.get("diagnosis")
            if isinstance(self.skill_runtime_result, dict)
            and isinstance(self.skill_runtime_result.get("diagnosis"), dict)
            else {}
        )
        workflow = (self.agent_evidence.get("agent_workflow") or {}) if isinstance(self.agent_evidence, dict) else {}
        return {
            "status": self.status,
            "query": self.query,
            "namespace": self.namespace,
            "duration_s": self.duration_s,
            "root_cause": diagnosis.get("root_cause") or "",
            "fault_type": diagnosis.get("fault_type") or "",
            "confidence": diagnosis.get("confidence"),
            "root_entity": diagnosis.get("root_entity"),
            "root_entity_validation": diagnosis.get("root_entity_validation") or {},
            "judge": diagnosis.get('judge') or {},
            "diagnosis_status": diagnosis.get('diagnosis_status', 'unreviewed'),
            "automation_gate": diagnosis.get('automation_gate') or {},
            "hypotheses": workflow.get("hypotheses") or [],
            "hypothesis_generation": workflow.get("hypothesis_generation") or {},
            "verification_plan": workflow.get("verification_plan") or {},
            "reasoning_chain": workflow.get("reasoning_chain") or diagnosis.get("summary") or diagnosis.get("root_cause") or "",
            "skill_runtime_result": self.skill_runtime_result,
            "agent_skill_bridge": self.agent_skill_bridge,
            "agent_evidence": self.agent_evidence,
            "events": self.events,
            "diagnostic_strategy": self.agent_evidence.get("diagnostic_strategy") or {},
            "history_retrieval": next((event['data'] for event in reversed(self.events)
                if event.get('event') == 'history_retrieval' and isinstance(event.get('data'), dict)),
                (workflow.get('context') or {}).get('history_retrieval', {})),
        }


class SkillAgent:
    """Use LLM decisions to select and invoke exact operational Skills."""

    name = "skill_agent"

    def __init__(
        self,
        *,
        registry: Any,
        skills_dir: str | Path,
        runtime_url: str = "",
        event_sink: Optional[EventSink] = None,
        kubectl_runner: Optional[KubectlRunner] = None,
        context_provider: Optional[ContextProvider] = None,
        llm_client: Any = None,
        llm_timeout_s: Optional[float] = None,
    ) -> None:
        self.registry = registry
        self.skills_dir = Path(skills_dir)
        self.runtime_url = runtime_url
        self.event_sink = event_sink
        self.kubectl_runner = kubectl_runner
        self.context_provider = context_provider
        self.llm_client = llm_client
        self.events: List[Dict[str, Any]] = []
        self._last_context: Dict[str, Any] = {}
        self.llm_hypothesis_timeout_s = float(llm_timeout_s or 90)
        self.require_llm = True
        self._last_decision: Dict[str, Any] = {}
        self._skill_owned_investigation = False
        self.skill_executor = SkillExecutor.from_dir(self.skills_dir)

    def run(self, request: SkillAgentRequest) -> SkillAgentResult:
        started = time.time()
        if self.require_llm and not self.llm_client:
            self._emit({
                "event": "llm_required_missing",
                "source": self.name,
                "success": False,
                "summary": "Agent+Skill 模式必须由大模型参与假设生成和推理；当前未配置 LLM，拒绝用规则结果冒充智能体分析。",
            })
            return SkillAgentResult(
                status="failed",
                query=request.query,
                namespace=request.namespace,
                duration_s=round(time.time() - started, 3),
                skill_runtime_result=None,
                agent_evidence={
                    "agent_workflow": {
                        "agent": self.name,
                        "summary": "LLM 未配置，Agent+Skill 分析未执行。",
                        "llm_required": True,
                        "llm_used": False,
                    }
                },
                agent_skill_bridge={"mode": "blocked", "reason": "llm_required"},
                events=list(self.events),
            )
        workflow = request.skill_workflow if isinstance(request.skill_workflow, dict) else {}
        stages = workflow.get("stages") if isinstance(workflow.get("stages"), list) else ["detect", "diagnose", "heal", "verify"]
        analysis_phases = set(workflow.get("analysis_phases") or ["context", "hypothesis", "plan", "investigation", "reasoning"])
        analysis_phases.update({"investigation", "reasoning"})
        self._skill_owned_investigation = {"hypothesis", "plan"} <= analysis_phases
        dry_run = bool(workflow.get("dry_run", True))
        if "hypothesis" in analysis_phases:
            hypotheses = self.generate_hypotheses(request, include_context="context" in analysis_phases)
        else:
            hypotheses = []
            if "context" in analysis_phases:
                workflow_payload = workflow.get("payload") if isinstance(workflow.get("payload"), dict) else {}
                self._retrieve_context(request, workflow_payload)
        if "hypothesis" in analysis_phases and self.require_llm and not any(item.get("llm_used") for item in hypotheses):
            self._emit({
                "event": "llm_hypothesis_failed",
                "source": self.name,
                "success": False,
                "summary": "模型未生成有效假设，停止诊断；不得用规则候选代替模型推理。",
            })
            return SkillAgentResult("failed", request.query, request.namespace,
                round(time.time() - started, 3), None,
                {"agent_workflow": {"hypotheses": [], "hypothesis_generation": {"llm_used": False}}},
                {"mode": "blocked", "reason": "llm_hypothesis_failed"}, list(self.events))
        verification_plan = self._skill_investigation_plan(hypotheses, stages) if self._skill_owned_investigation else self.plan_verification(request, hypotheses, stages) if "plan" in analysis_phases else {
            "mode": "direct_evidence",
            "summary": "已跳过假设和验证规划，Agent 直接采集当前对象证据并调用 Skill。",
            "hypotheses": hypotheses,
            "stages": list(stages),
            "checks": [],
        }
        evidence = self.collect_evidence(request, verification_plan=verification_plan)
        skill_result = self.select_and_invoke_skills(
            request,
            evidence=evidence,
            hypotheses=hypotheses,
            stages=stages,
            dry_run=dry_run,
        )
        # Skill tool traces are the authoritative record of actual investigation.
        if skill_result:
            commands = evidence["agent_evidence_collection"]["commands"]
            for run in skill_result.get("skill_runtime", {}).get("runs", []):
                collected_scope = (run.get('stages', {}).get('diagnose', {}).get('output') or {}).get('service_scope')
                if isinstance(collected_scope, dict):
                    evidence['agent_evidence_collection']['service_scope'] = collected_scope
                seen = set()
                for trace in run.get("trace", []):
                    for call in trace.get("tool_calls", []):
                        key = json.dumps(call, sort_keys=True, default=str)
                        if key in seen:
                            continue
                        seen.add(key)
                        command = call.get("args", {}).get("command", "")
                        success = call.get("success") is True and call.get("executed") is True and not call.get("outcome_unknown")
                        cluster_id = (workflow.get("payload") or {}).get("cluster_id") or (workflow.get("payload") or {}).get("target_cluster_id") or ""
                        summary = call.get("summary") or f"{call.get('tool') or '工具'} {command}"
                        if not success:
                            summary = f"{command}：{call.get('error') or '执行未确认成功'}"
                        # Publish only the committed trace. A host thread may finish
                        # after a stage deadline; its late return is not this run's result.
                        self._emit({"event": "tool_call", "agent": self.name,
                            "tool": call.get("tool"), "command": command,
                            "namespace": request.namespace, "success": success,
                            "cluster_id": cluster_id,
                            "executed": call.get("executed"), "outcome_unknown": bool(call.get("outcome_unknown")),
                            "error": call.get("error"), "source": "skill_tool",
                            "skill_run_id": run.get("run_id"), "stage": trace.get("stage")})
                        self._emit({"event": "evidence", "agent": self.name, "source": "skill_tool",
                            "success": success, "summary": summary,
                            "cluster_id": cluster_id,
                            "outcome_unknown": bool(call.get("outcome_unknown")),
                            "skill_run_id": run.get("run_id"), "stage": trace.get("stage")})
                        if not self._skill_owned_investigation:
                            continue
                        sample = evidence_sample(call.get("data") or call.get("sample") or "")
                        if any(c.get("command") == command and c.get("sample") == sample for c in commands):
                            continue
                        commands.append({**call, "command": call.get("args", {}).get("command", ""),
                            "sample": sample,
                            "timestamp": evidence_observed_at(call.get('data')) or call.get('timestamp') or '',
                            "cluster_id": (workflow.get("payload") or {}).get("cluster_id") or (workflow.get('payload') or {}).get('target_cluster_id') or '',
                            "namespace": request.namespace})
        if not skill_result:
            return SkillAgentResult("failed", request.query, request.namespace,
                round(time.time() - started, 3), None, evidence,
                {"mode": "blocked", "reason": "skill_execution_failed"}, list(self.events))
        self.reason_about_result(
            request,
            hypotheses=hypotheses,
            verification_plan=verification_plan,
            evidence=evidence,
            skill_result=skill_result,
        )
        status = "completed" if skill_result else "failed"
        if self._skill_owned_investigation and (
            not any(c.get("executed") and c.get("success") and c.get("sample")
                    for c in evidence["agent_evidence_collection"]["commands"])
            or not (skill_result or {}).get("diagnosis", {}).get("agent_llm_reasoning")
        ):
            status = "failed"
            self._emit({"event": "diagnosis_incomplete", "success": False,
                "summary": "缺少 Skill 实际取证或有效模型裁决，诊断不算完成。"})
        skill_event = next(
            (
                event for event in reversed(self.events)
                if event.get("event") == "skill_runtime" and event.get("result")
            ),
            {},
        )
        bridge_info = {
            "mode": "agent_tool_call",
            "agent": self.name,
            "tool": skill_event.get("tool") or "agenticsre_skill_run",
        }
        if not skill_result:
            bridge_info["reason"] = "skill_runtime_failed"
        return SkillAgentResult(
            status=status,
            query=request.query,
            namespace=request.namespace,
            duration_s=round(time.time() - started, 3),
            skill_runtime_result=skill_result,
            agent_evidence=evidence,
            agent_skill_bridge=bridge_info,
            events=list(self.events),
        )

    def _skill_catalog(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        domain = str(payload.get("domain") or "").strip()
        scoped = bool(payload.get("cluster_kind") or payload.get("source") or payload.get("cluster_id") or payload.get("target_cluster_id"))
        domains = {domain, "generic", "tool"} if domain and scoped else set()
        if domain == "llm" and (payload.get("cluster_kind") == "ai" or payload.get("source") == "ai_event_center"):
            domains.add("k8s")
        return [{"id": d.id, "domain": d.metadata.domain, "description": d.metadata.description,
                 "stages": d.metadata.stages, "tags": d.metadata.tags}
                for d in self.skill_executor.registry.list() if not domains or d.metadata.domain in domains]

    def _skill_investigation_plan(self, hypotheses, stages):
        plan = {"mode": "skill_owned_investigation", "skill_owned": True,
                "summary": "Agent 已结合假设选择 Skill；由 Skill 执行专项只读取证。",
                "hypotheses": hypotheses, "hypothesis_generation": self._hypothesis_generation_summary(hypotheses),
                "checks": [{"type": "skill_tool", "tool": "agenticsre_skill_run", "stages": list(stages)}],
                "context": self._compact_context_for_evidence(hypotheses)}
        self._emit({"event": "phase_start", "name": "VERIFICATION_PLANNING", "phase": "agent_skill_plan"})
        self._emit({"event": "plan", "agent": self.name, **plan})
        self._emit({"event": "phase_complete", "name": "VERIFICATION_PLANNING", "phase": "agent_skill_plan", "status": "completed"})
        return plan

    def select_and_invoke_skills(
        self,
        request: SkillAgentRequest,
        *,
        evidence: Dict[str, Any],
        hypotheses: Optional[List[Dict[str, Any]]] = None,
        stages: Iterable[str],
        dry_run: bool,
    ) -> Optional[Dict[str, Any]]:
        hypotheses = hypotheses or []
        workflow = request.skill_workflow if isinstance(request.skill_workflow, dict) else {}
        payload = workflow.get("payload") if isinstance(workflow.get("payload"), dict) else {}
        selection_payload = dict(payload)
        if not hypotheses:
            # A true no-hypothesis ablation must not inherit detector-produced
            # candidate routing hints, which otherwise act as hidden hypotheses.
            for key in ("candidate_skills", "candidate_faults", "observation_signals"):
                selection_payload.pop(key, None)
        domain = str(payload.get("domain") or "").strip()
        cluster_kind = str(payload.get("cluster_kind") or "").strip().lower()
        source = str(payload.get("source") or "").strip().lower()
        has_scope = bool(cluster_kind or source or payload.get("cluster_id") or payload.get("target_cluster_id"))
        allowed_domains = {domain, "generic", "tool"} if domain and has_scope else set()
        if domain == "llm" and (cluster_kind == "ai" or source == "ai_event_center"):
            allowed_domains.add("k8s")
        requested_stages = [stage for stage in stages if stage in {"detect", "diagnose", "heal", "verify"}]
        catalog = []
        for definition in self.skill_executor.registry.list():
            metadata = definition.metadata
            if allowed_domains and metadata.domain not in allowed_domains:
                continue
            catalog.append({
                "id": metadata.id,
                "name": metadata.name,
                "domain": metadata.domain,
                "description": metadata.description,
                "stages": metadata.stages,
                "tags": metadata.tags,
            })
        selection = self._last_decision if self._skill_owned_investigation and self._extract_skill_calls(self._last_decision) else self._call_llm_json(
            "skill_selection",
            [
                {
                    "role": "system",
                    "content": (
                        "你是可调用 Skill 的 SRE Agent。必须仅从给定目录选择具体 skill_id，"
                        "根据事件与已观测证据决定是否调用、调用顺序和阶段。"
                        "对仅包含 detect/diagnose 的分析任务，应优先选择观测、证据采集和诊断类 Skill；"
                        "只读证据若出现 ImagePullBackOff、CrashLoopBackOff、OOMKilled 等明确状态，"
                        "必须选择直接对应该状态的最具体 Skill，不得退化为泛化的 DeploymentUnhealthy；"
                        "除非用户或调查计划明确要求主动探测，不得选择主要用于负载生成、故障注入、压力测试或基准评测的 Skill。"
                        + (
                            "当前已有按置信度排序的根因假设。只验证 Top1，必须调用 1 个能直接判别该假设的专项 Skill；"
                            "即使只读证据已充分，也要用该 Skill 做独立一致性验证，随后立即停止，不得扫描其他候选。"
                            if hypotheses else
                            "当前未启用根因假设。不得使用候选 Skill/故障路由提示，需要从原始观测出发执行广域调查；"
                            "可调用最多 3 个互补 Skill，覆盖观测、专项诊断和交叉验证。"
                        )
                        +
                        "不得创造 skill_id，不得使用实验标签作为选择依据，不得返回 Skill 执行结果。只返回 JSON。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps({
                        "query": request.query,
                        "payload": selection_payload,
                        "hypotheses": hypotheses[:1],
                        "requested_stages": requested_stages,
                        "skill_catalog": catalog,
                        "evidence_summary": {
                            "observation": payload.get("observation") if isinstance(payload.get("observation"), dict) else {},
                            "signals": payload.get("evidence") if isinstance(payload.get("evidence"), list) else [],
                            "readonly_checks": [
                                {
                                    "command": item.get("command"),
                                    "success": item.get("success"),
                                    "summary": item.get("summary"),
                                    "sample": str(item.get("sample") or "")[:600],
                                }
                                for item in (evidence.get("agent_evidence_collection", {}).get("commands", []) if isinstance(evidence.get("agent_evidence_collection"), dict) else [])
                                if isinstance(item, dict)
                            ],
                        },
                        "output_schema": {
                            "skill_calls": [{"skill_id": "exact.id", "stages": ["detect", "diagnose"], "reason": "选择依据"}],
                            "stop_reason": "无需调用时说明原因",
                        },
                    }, ensure_ascii=False, default=str)[:30000],
                },
            ],
            max_tokens=3000,
        )
        initial_selection = selection
        calls = self._extract_skill_calls(selection)
        valid_ids = {item["id"] for item in catalog}
        declared_stages_by_id = {
            item["id"]: set(item.get("stages") or [])
            for item in catalog
        }
        if not any(
            isinstance(item, dict) and str(item.get("skill_id") or "").strip() in valid_ids
            for item in calls or []
        ):
            self._emit({
                "event": "skill_selection_retry",
                "source": self.name,
                "success": True,
                "summary": "首次 Skill 选择未返回有效目录 ID，已要求 LLM 按精简调用协议重试。",
            })
            selection = self._call_llm_json(
                "skill_selection_retry",
                [
                    {
                        "role": "system",
                        "content": (
                            "你是 SRE Agent 的 Skill 调用决策器。仅能从给定列表选择最多 3 个精确 skill_id，"
                            "专项诊断 Skill 优先。Agent+Skill 模式至少选择 1 个 Skill；即使只读证据已经证实 Top1，"
                            "也要选择匹配的专项 Skill 做独立验证。不得输出分析流程、阶段结果或任何额外字段。只返回 JSON。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps({
                            "query": request.query[:2500],
                            "requested_stages": requested_stages,
                            "valid_skills": catalog,
                            "observation_signals": payload.get("observation_signals") or payload.get("evidence") or [],
                            "background_observation_signals": payload.get("background_observation_signals") or [],
                            "readonly_checks": [
                                {
                                    "command": item.get("command"),
                                    "success": item.get("success"),
                                    "sample": str(item.get("sample") or "")[:400],
                                }
                                for item in (evidence.get("agent_evidence_collection", {}).get("commands", []) if isinstance(evidence.get("agent_evidence_collection"), dict) else [])
                                if isinstance(item, dict)
                            ],
                            "required_output": {
                                "skill_calls": [{"skill_id": "exact.id", "stages": requested_stages, "reason": "证据依据"}],
                            },
                        }, ensure_ascii=False, default=str)[:24000],
                    },
                ],
                max_tokens=1200,
            )
            calls = self._extract_skill_calls(selection)
        selection_mode = "agent_llm_exact_skill_calls"
        evidence_call = self._evidence_validated_skill_fallback(
            catalog=catalog,
            top_hypothesis=hypotheses[0] if hypotheses else {},
            evidence=evidence,
            requested_stages=requested_stages,
            query=request.query,
        )
        valid_call_ids = [
            str(item.get("skill_id") or "").strip()
            for item in calls or []
            if isinstance(item, dict) and str(item.get("skill_id") or "").strip() in valid_ids
        ]
        if not valid_call_ids:
            if evidence_call:
                calls = [evidence_call]
                selection_mode = "agent_evidence_validated_fallback"
                selection = {
                    "skill_calls": calls,
                    "selection_mode": selection_mode,
                    "llm_initial_selection": initial_selection,
                    "llm_retry_selection": selection,
                }
                self._emit({
                    "event": "skill_selection_evidence_fallback",
                    "source": self.name,
                    "success": True,
                    "summary": "LLM 已确认 Top1 但省略 Skill 调用，已选择与真实只读证据匹配的专项 Skill 做独立验证。",
                    "skill_id": evidence_call["skill_id"],
                })
        elif evidence_call and valid_call_ids[0] != evidence_call["skill_id"]:
            llm_selection = selection
            calls = [evidence_call]
            selection_mode = "agent_evidence_specificity_override"
            selection = {
                "skill_calls": calls,
                "selection_mode": selection_mode,
                "llm_selection": llm_selection,
            }
            self._emit({
                "event": "skill_selection_specificity_override",
                "source": self.name,
                "success": True,
                "summary": "显式运行态证据要求更具体的专项 Skill，已覆盖泛化 Skill 选择。",
                "llm_skill_id": valid_call_ids[0],
                "skill_id": evidence_call["skill_id"],
            })
        self._emit({
            "event": "skill_selection",
            "source": self.name,
            "success": bool(calls),
            "candidate_skill_ids": [
                str(item.get("skill_id") or "") for item in calls or [] if isinstance(item, dict)
            ][:10],
            "catalog_size": len(catalog),
            "response_keys": sorted(selection.keys()) if isinstance(selection, dict) else [],
            "response_preview": json.dumps(selection, ensure_ascii=False, default=str)[:2000] if selection is not None else "",
        })
        normalized_calls = []
        for item in calls or []:
            if not isinstance(item, dict):
                continue
            skill_id = str(item.get("skill_id") or "").strip()
            if skill_id not in valid_ids:
                continue
            raw_stages = item.get("stages")
            if not isinstance(raw_stages, list):
                raw_stages = [item.get("phase")] if item.get("phase") else requested_stages
            declared_stages = declared_stages_by_id.get(skill_id, set())
            selected_stages = [
                stage for stage in raw_stages
                if stage in requested_stages and stage in declared_stages
            ]
            if not selected_stages:
                selected_stages = [stage for stage in requested_stages if stage in declared_stages]
            if not selected_stages:
                continue
            target = item.get("target") if isinstance(item.get("target"), dict) else {}
            target = {k: v for k, v in target.items() if k in {"pod", "service", "deployment"}
                      and isinstance(v, str) and re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", v)}
            normalized_calls.append({"skill_id": skill_id, "stages": selected_stages,
                "target": target, "commands": item.get("commands") or [], "reason": str(item.get("reason") or "")})
        if not normalized_calls:
            self._emit({
                "event": "skill_selection_failed",
                "source": self.name,
                "success": False,
                "summary": str((selection or {}).get("stop_reason") or "LLM 未选择目录中的有效 Skill。"),
            })
            return None

        run_payload = dict(payload)
        # Skills consume the canonical cluster_id, including their typed
        # evidence predicates. Canonicalize the accepted caller alias once.
        if not run_payload.get('cluster_id') and run_payload.get('target_cluster_id'):
            run_payload['cluster_id'] = run_payload['target_cluster_id']
        run_payload["agent_evidence"] = evidence
        run_payload["message"] = request.query
        run_payload["namespace"] = request.namespace or payload.get("namespace") or "default"
        # Accept standard observation fields without allowing signals to alter
        # identity, scope, routing labels or tool permissions.
        observation_keys = {"endpoint_count", "http_status", "status_code", "node_cpu_usage",
                            "node_memory_usage", "node_rootfs_usage", "ttft_p95_seconds", "e2e_p95_seconds"}
        observations = payload.get("observation") if isinstance(payload.get("observation"), dict) else {}
        for key in observation_keys:
            if key not in run_payload and key in observations:
                run_payload[key] = observations[key]
        for signal in payload.get("observation_signals") or []:
            if isinstance(signal, dict) and signal.get("signal") in observation_keys:
                run_payload.setdefault(signal["signal"], signal.get("value"))
        runs = []
        supplementary_runs = []
        self._emit({
            "event": "phase_start",
            "phase": "agent_skill_tool_calls",
            "name": "AGENT_SKILL_TOOL_CALL",
            "source": self.name,
            "selected_skills": [call["skill_id"] for call in normalized_calls[:3]],
        })
        for call in normalized_calls[:3]:
            call_payload = {**run_payload, **call["target"], "investigation_commands": call["commands"],
                            "collect_service_topology": bool(run_payload.get('service') or call['target'].get('service'))}
            # Existing diagnostic Skills can consume evidence collected by a
            # reusable Skill until they implement their own controlled tools.
            if self._skill_owned_investigation and call["skill_id"] not in {
                "k8s.image_pull_error", "generic.evidence_collection"
            } and not evidence["agent_evidence_collection"]["commands"]:
                self._emit({"event": "tool_call", "agent": self.name, "tool": "agenticsre_skill_run",
                    "skill_id": "generic.evidence_collection", "stages": ["detect", "diagnose"],
                    "reason": "为尚未实现自带工具的专项 Skill 提供共享只读取证。"})
                helper = self.skill_executor.run("generic.evidence_collection", call_payload,
                    stages=["detect", "diagnose"], dry_run=True,
                    tool_runner=self._skill_tool_runner(request.namespace)).to_dict()
                supplementary_runs.append(helper)
                helper_output = helper.get("stages", {}).get("diagnose", {}).get("output", {})
                if isinstance(helper_output.get('service_scope'), dict):
                    evidence['agent_evidence_collection']['service_scope'] = helper_output['service_scope']
                for item in helper_output.get("tool_results", []):
                    evidence["agent_evidence_collection"]["commands"].append({**item,
                        "command": item.get("args", {}).get("command", ""),
                        "sample": evidence_sample(item.get("data") or item.get("sample") or ""),
                        "timestamp": evidence_observed_at(item.get('data')) or item.get('timestamp') or '',
                        "cluster_id": payload.get("cluster_id") or payload.get('target_cluster_id') or '', "namespace": request.namespace})
                call_payload["agent_evidence"] = evidence
            self._emit({
                "event": "tool_call",
                "agent": self.name,
                "tool": "agenticsre_skill_run",
                "skill_id": call["skill_id"],
                "stages": call["stages"],
                "reason": call["reason"],
            })
            run = self.skill_executor.run(
                call["skill_id"],
                call_payload,
                stages=call["stages"],
                dry_run=True,
                tool_runner=self._skill_tool_runner(request.namespace),
            ).to_dict()
            runs.append(run)
        runs.extend(supplementary_runs)
        self._emit({
            "event": "phase_complete",
            "phase": "agent_skill_tool_calls",
            "name": "AGENT_SKILL_TOOL_CALL",
            "status": "completed" if runs else "failed",
            "data": {"skill_count": len(runs)},
        })

        primary = runs[0] if runs else {}
        primary_stages = primary.get("stages") if isinstance(primary.get("stages"), dict) else {}
        diagnose = primary_stages.get("diagnose") if isinstance(primary_stages.get("diagnose"), dict) else {}
        if self._skill_owned_investigation and diagnose.get("status") != "ok":
            self._emit({"event": "skill_not_confirmed", "success": False,
                "summary": "专项 Skill 未匹配或执行失败，不生成该故障的最终结论。"})
            return None
        diagnosis_output = diagnose.get("output") if isinstance(diagnose.get("output"), dict) else {}
        diagnosis = {
            **diagnosis_output,
            "skill_id": (primary.get("skill") or {}).get("id") or normalized_calls[0]["skill_id"],
            "root_cause": diagnosis_output.get("root_cause") or diagnose.get("summary") or "",
            "confidence": diagnosis_output.get("confidence", diagnose.get("confidence")),
            "evidence": diagnose.get("evidence") or diagnosis_output.get("evidence") or [],
        }
        result = {
            "status": "completed",
            "dry_run": True,
            "requested_stages": requested_stages,
            "skill_runtime": {
                "selection_mode": selection_mode,
                "primary_skill_id": diagnosis.get("skill_id") or "",
                "matched_count": len(runs),
                "runs": runs,
            },
            "diagnosis": diagnosis,
            "agent_skill_selection": selection,
        }
        self._emit({
            "event": "skill_runtime",
            "status": "completed",
            "source": "agent_llm_tool_calls",
            "agent": self.name,
            "tool": "agenticsre_skill_run",
            "primary_skill_id": result["skill_runtime"]["primary_skill_id"],
            "matched_count": len(runs),
            "result": result,
        })
        return result

    def _evidence_validated_skill_fallback(
        self,
        *,
        catalog: List[Dict[str, Any]],
        top_hypothesis: Dict[str, Any],
        evidence: Dict[str, Any],
        requested_stages: List[str],
        query: str,
    ) -> Optional[Dict[str, Any]]:
        """Route observed evidence to one diagnostic Skill after empty LLM calls.

        The fallback is enabled only when real read-only commands succeeded and
        returned output.  It uses the observed Top1 mechanism, never experiment
        labels, so Agent+Skill does not fail merely because the LLM said the
        evidence was already sufficient and omitted the mandatory Skill call.
        """
        command_records = (
            evidence.get("agent_evidence_collection", {}).get("commands", [])
            if isinstance(evidence.get("agent_evidence_collection"), dict)
            else []
        )
        observed = [
            item for item in command_records
            if isinstance(item, dict) and item.get("success") and item.get("sample")
        ]
        if not observed:
            return None

        # Specificity overrides must be derived from *current executed output*.
        # Query text and the pre-investigation Top1 may contain recalled history
        # (for example an older ImagePullBackOff on a similarly named workload)
        # and therefore cannot override an otherwise valid LLM Skill choice.
        text = " ".join(
            str(item.get("sample") or "")[:2400]
            for item in observed
        ).casefold()
        routes = [
            (("imagepullbackoff", "errimagepull", "failed to pull image", "镜像拉取"), "k8s.image_pull_error"),
            (("crashloopbackoff", "back-off restarting", "容器反复重启"), "k8s.pod_crashloop"),
            (("oomkilled", "out of memory", "内存溢出"), "k8s.oom_killed"),
            (("failedscheduling", "unschedulable", "调度失败"), "k8s.scheduling_failed"),
            (("failedmount", "volume mount", "挂载失败"), "k8s.volume_mount_failed"),
            ((
                "replicas=0", '"replicas": 0', '"replicas":0',
                "副本数配置为 0", "副本数被设置为 0", "endpoints 为空", "无可用就绪副本",
            ), "k8s.service_unavailable"),
        ]
        by_id = {str(item.get("id") or ""): item for item in catalog}
        for markers, skill_id in routes:
            metadata = by_id.get(skill_id)
            if metadata is None or not any(marker in text for marker in markers):
                continue
            declared = set(metadata.get("stages") or [])
            stages = [stage for stage in requested_stages if stage in declared]
            if not stages:
                continue
            return {
                "skill_id": skill_id,
                "stages": stages,
                "reason": "Top1 已由真实只读命令证实；调用匹配专项 Skill 做独立一致性验证。",
            }
        return None

    @staticmethod
    def _align_fault_type_with_reasoned_root(root_cause: Any, fault_type: Any) -> str:
        """Keep the final fault type consistent with decisive reasoned state."""
        root = str(root_cause or "").casefold()
        current = str(fault_type or "").strip()
        if any(marker in root for marker in (
            "imagepullbackoff", "errimagepull", "image pull", "镜像拉取", "拉取镜像",
        )):
            return "ImagePullError"
        scale_zero = any(marker in root for marker in (
            "replicas=0", "replicas 字段被设置为 0", "spec.replicas 字段被设置为 0",
            "副本数被设置为 0", "期望副本数为 0", "没有任何 pod 运行",
            "没有 pod 被创建",
        ))
        endpoint_empty = any(marker in root for marker in (
            "endpoints 为空", "endpoint 为空", "无可用 endpoint", "无可用就绪副本",
        ))
        if scale_zero and endpoint_empty:
            return "ServiceUnavailable"
        return current or "observable_anomaly"

    def _extract_skill_calls(self, value: Any, depth: int = 0) -> List[Dict[str, Any]]:
        if depth > 5:
            return []
        if isinstance(value, str):
            parsed = self._parse_json_value(value)
            return self._extract_skill_calls(parsed, depth + 1) if parsed is not None else []
        if isinstance(value, list):
            direct = [item for item in value if isinstance(item, dict) and (item.get("skill_id") or item.get("id"))]
            if direct:
                return [
                    {**item, "skill_id": item.get("skill_id") or item.get("id")}
                    for item in direct
                ]
            for item in value:
                found = self._extract_skill_calls(item, depth + 1)
                if found:
                    return found
            return []
        if not isinstance(value, dict):
            return []
        for key in ("skill_calls", "skills_invoked", "calls", "tool_calls", "selected_skills", "skills"):
            found = self._extract_skill_calls(value.get(key), depth + 1)
            if found:
                return found
        function = value.get("function") if isinstance(value.get("function"), dict) else {}
        arguments = function.get("arguments")
        if arguments:
            found = self._extract_skill_calls(arguments, depth + 1)
            if found:
                return found
        for key in ("result", "data", "output", "response", "content", "raw_response"):
            found = self._extract_skill_calls(value.get(key), depth + 1)
            if found:
                return found
        return []

    @staticmethod
    def _parse_json_value(value: str) -> Optional[Any]:
        text = str(value or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
        try:
            return json.loads(text)
        except Exception:
            match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
            if not match:
                return None
            try:
                return json.loads(match.group(1))
            except Exception:
                return None

    def generate_hypotheses(self, request: SkillAgentRequest, *, include_context: bool = True) -> List[Dict[str, Any]]:
        workflow = request.skill_workflow if isinstance(request.skill_workflow, dict) else {}
        payload = workflow.get("payload") if isinstance(workflow.get("payload"), dict) else {}
        fault_type = str(payload.get("fault_type") or payload.get("raw_fault_type") or "").strip()
        event_type = str(payload.get("raw_fault_type") or payload.get("event_type") or fault_type).strip()
        if self._mentions_experiment_metadata(fault_type):
            fault_type = "observable_anomaly"
        if self._mentions_experiment_metadata(event_type):
            event_type = "observable_anomaly"
        target = str(payload.get("service") or payload.get("deployment") or payload.get("object") or "").strip()
        model = str(payload.get("model") or "").strip()
        context = self._retrieve_context(request, payload) if include_context else {
            "rules": [],
            "faults": [],
            "recent_runs": [],
            "current_evidence": payload.get("evidence") if isinstance(payload.get("evidence"), list) else [],
            "observation": payload.get("observation") if isinstance(payload.get("observation"), dict) else {},
        }
        primary = self._hypothesis_for_fault(fault_type, target=target, model=model)
        alternatives = self._alternative_hypotheses(fault_type, event_type, target=target)
        deterministic = [primary] + alternatives
        llm_hypotheses = self._generate_llm_hypotheses(request, payload, context)
        hypotheses = self._merge_and_rank_hypotheses(
            llm_hypotheses if self.require_llm else deterministic,
            payload=payload,
            context=context,
        )
        generation = InvestigationController([],max_iterations=1,confidence_threshold=0.85).record_generation(
            hypotheses,mode="agent_skill")
        self._generation_record = generation
        self._emit(generation)
        self._emit({
            "event": "phase_start",
            "phase": "agent_skill_hypothesis",
            "name": "HYPOTHESIS_GENERATION",
            "source": self.name,
        })
        self._emit({
            "event": "hypotheses",
            "source": self.name,
            "items": [
                {
                    "id": item.get("id"),
                    "rank": item.get("rank"),
                    "confidence": item.get("confidence"),
                    "description": item.get("description"),
                    "expected_evidence": item.get("expected_evidence") or [],
                    "supporting_evidence": item.get("supporting_evidence") or [],
                    "source": item.get("source") or "deterministic",
                    "llm_used": bool(item.get("llm_used")),
                }
                for item in hypotheses
            ],
            "context_summary": {
                "rules_count": len(context.get("rules") or []),
                "faults_count": len(context.get("faults") or []),
                "recent_runs_count": len(context.get("recent_runs") or []),
                "llm_used": any(item.get("llm_used") for item in hypotheses),
            },
        })
        self._emit({
            "event": "phase_complete",
            "phase": "agent_skill_hypothesis",
            "name": "HYPOTHESIS_GENERATION",
            "status": "completed",
            "notes": f"{len(hypotheses)} hypotheses generated",
        })
        return hypotheses

    def plan_verification(
        self,
        request: SkillAgentRequest,
        hypotheses: List[Dict[str, Any]],
        stages: Iterable[str],
    ) -> Dict[str, Any]:
        workflow = request.skill_workflow if isinstance(request.skill_workflow, dict) else {}
        payload = workflow.get("payload") if isinstance(workflow.get("payload"), dict) else {}
        namespace = str(payload.get("namespace") or request.namespace or "default")
        commands = self._k8s_readonly_commands(payload)
        stages_list = list(stages)
        plan = {
            "mode": "agent_driven_skill_tool",
            "summary": "Agent 先提出候选根因，再规划只读调查和 Skill 工具调用，用证据校验结论。",
            "namespace": namespace,
            "hypotheses": hypotheses,
            "hypothesis_count": len(hypotheses),
            "hypothesis_generation": self._hypothesis_generation_summary(hypotheses),
            "context": self._compact_context_for_evidence(hypotheses),
            "stages": stages_list,
            "checks": [
                {
                    "type": "readonly_kubectl",
                    "target": namespace,
                    "commands": commands,
                    "purpose": "核验 Pod、Service、Endpoint、事件和部署状态是否支持候选根因。",
                },
                {
                    "type": "skill_tool",
                    "tool": "agenticsre_skill_run",
                    "stages": stages_list,
                    "purpose": "由 Agent 从目录中选择并调用具体 Skill，不使用 Runtime 自动匹配流水线。",
                },
            ],
        }
        llm_plan = self._call_llm_json(
            "plan",
            [
                {
                    "role": "system",
                    "content": "你是 SRE 调查规划 Agent。只能从给定的只读检查和 Skill 阶段中制定验证顺序，不得生成写操作命令。只返回 JSON。",
                },
                {
                    "role": "user",
                    "content": json.dumps({
                        "query": request.query,
                        "payload": payload,
                        "hypotheses": hypotheses,
                        "readonly_commands": commands,
                        "skill_stages": stages_list,
                        "output_schema": {
                            "summary": "中文规划摘要",
                            "selected_command_indexes": [0],
                            "reasoning": "中文选择依据",
                        },
                    }, ensure_ascii=False, default=str),
                },
            ],
            max_tokens=1800,
        )
        if isinstance(llm_plan, dict):
            selected_indexes = llm_plan.get("selected_command_indexes")
            if isinstance(selected_indexes, list):
                selected_commands = [
                    commands[index] for index in selected_indexes
                    if isinstance(index, int) and 0 <= index < len(commands)
                ]
                if selected_commands:
                    commands = selected_commands
                    plan["checks"][0]["commands"] = commands
            plan["llm_plan"] = llm_plan
            plan["summary"] = str(llm_plan.get("summary") or plan["summary"])
        self._emit({
            "event": "phase_start",
            "phase": "agent_skill_plan",
            "name": "VERIFICATION_PLANNING",
            "source": self.name,
        })
        self._emit({
            "event": "plan",
            "agent": self.name,
            "summary": plan["summary"],
            "checks": plan["checks"],
            "hypotheses": [h.get("id") for h in hypotheses],
            "stages": stages_list,
        })
        self._emit({
            "event": "phase_complete",
            "phase": "agent_skill_plan",
            "name": "VERIFICATION_PLANNING",
            "status": "completed",
            "data": {
                "check_count": len(plan["checks"]),
                "command_count": len(commands),
            },
        })
        return plan

    def collect_evidence(
        self,
        request: SkillAgentRequest,
        *,
        verification_plan: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        workflow = request.skill_workflow if isinstance(request.skill_workflow, dict) else {}
        payload = workflow.get("payload") if isinstance(workflow.get("payload"), dict) else {}
        namespace = str(payload.get("namespace") or request.namespace or "default")
        plan = verification_plan if isinstance(verification_plan, dict) else {}
        commands = [] if plan.get("skill_owned") else self._commands_from_plan(plan) or self._k8s_readonly_commands(payload)
        evidence: Dict[str, Any] = {
            "agent_workflow": {
                "agent": self.name,
                "summary": "Agent+Skill 按假设、规划、调查、推理执行。",
                "hypotheses": [],
                "verification_plan": plan,
                "hypothesis_generation": plan.get("hypothesis_generation") if isinstance(plan.get("hypothesis_generation"), dict) else {},
                "context": plan.get("context") if isinstance(plan.get("context"), dict) else {},
            },
            "agent_skill_context": {
                "agent": self.name,
                "summary": "Skill Agent 已解析告警上下文，准备选择并调用 Skill。",
                "payload": {
                    "payload": payload,
                    "skill_workflow": workflow,
                    "source": "alert_agent_skill_request",
                },
            },
            "agent_evidence_collection": {
                "agent": self.name,
                "summary": "Skill Agent 在调用 Skill 前执行只读证据采集。",
                "namespace": namespace,
                "commands": [],
            },
        }
        if isinstance(plan.get("hypotheses"), list):
            evidence["agent_workflow"]["hypotheses"] = plan.get("hypotheses")
        self._emit({
            "event": "phase_start",
            "phase": "agent_skill_context",
            "name": "AGENT_SKILL_CONTEXT",
            "source": self.name,
        })
        self._emit({
            "event": "structured_evidence",
            "agent": self.name,
            "summary": "结构化告警上下文已进入 Skill Agent 工作流。",
            "payload": payload,
            "success": True,
        })
        self._emit({
            "event": "phase_complete",
            "phase": "agent_skill_context",
            "name": "AGENT_SKILL_CONTEXT",
            "status": "completed",
        })
        self._emit({
            "event": "phase_start",
            "phase": "agent_skill_investigation",
            "name": "INVESTIGATION",
            "source": self.name,
        })

        kubectl_available = bool(self.kubectl_runner) or self._tool_available("kubectl")
        for command in commands:
            record = self._run_kubectl(
                command,
                namespace,
                kubectl_available=kubectl_available,
                payload=payload,
            )
            evidence["agent_evidence_collection"]["commands"].append(record)
            self._emit({
                "event": "tool_call",
                "agent": self.name,
                "tool": "kubectl",
                "command": command,
                "namespace": namespace,
                "cluster_id": record.get("cluster_id") or payload.get("cluster_id") or "",
                "success": record["success"],
                "summary": record["summary"],
            })
            self._emit({
                "event": "evidence",
                "agent": self.name,
                "source": record.get("source") or ("kubectl" if kubectl_available else "planned_kubectl"),
                "success": record["success"] or not kubectl_available,
                "summary": record["summary"],
            })

        self._emit({
            "event": "phase_complete",
            "phase": "agent_skill_investigation",
            "name": "INVESTIGATION",
            "status": "completed",
            "data": {
                "command_count": len(commands),
                "executed": any(item.get("executed") for item in evidence["agent_evidence_collection"]["commands"]),
            },
        })
        return evidence

    def reason_about_result(
        self,
        request: SkillAgentRequest,
        *,
        hypotheses: List[Dict[str, Any]],
        verification_plan: Dict[str, Any],
        evidence: Dict[str, Any],
        skill_result: Optional[Dict[str, Any]],
    ) -> None:
        diagnosis = (
            skill_result.get("diagnosis")
            if isinstance(skill_result, dict) and isinstance(skill_result.get("diagnosis"), dict)
            else {}
        )
        skill_root = str(diagnosis.get("root_cause") or "")
        skill_confidence = diagnosis.get("confidence")
        primary_skill = ""
        if isinstance(skill_result, dict):
            runtime = skill_result.get("skill_runtime") if isinstance(skill_result.get("skill_runtime"), dict) else {}
            primary_skill = runtime.get("primary_skill_id") or diagnosis.get("skill_id") or ""
        top = hypotheses[0] if hypotheses else {}
        root = diagnosis.get("root_cause") or top.get("description") or "证据不足，无法形成可靠根因。"
        confidence = diagnosis.get("confidence", top.get("confidence", 0.5))
        command_records = (
            evidence.get("agent_evidence_collection", {}).get("commands", [])
            if isinstance(evidence.get("agent_evidence_collection"), dict)
            else []
        )
        hypothesis_before = json.loads(json.dumps(hypotheses, default=str))
        hypothesis_catalog = tool_evidence_catalog(command_records)
        success_commands = [item for item in command_records if isinstance(item, dict) and item.get("success")]
        workflow_payload = request.skill_workflow.get('payload') or {}
        entity_catalog = observed_entity_catalog(command_records,
            workflow_payload.get('cluster_id') or workflow_payload.get('target_cluster_id') or '',
            request.namespace or workflow_payload.get('namespace') or 'default')
        service_scope = (evidence.get('agent_evidence_collection') or {}).get('service_scope')
        reasoning_chain = (
            f"Agent 首先形成 {len(hypotheses)} 个候选根因；随后按验证计划执行 "
            f"{len(command_records)} 个只读检查，并调用 {primary_skill or '由 Agent 选择的 Skill'}。"
            f"Skill 返回的根因与首位假设进行一致性校验后，当前结论为：{root}"
        )
        if not success_commands and command_records:
            reasoning_chain += "。本次只读命令未全部成功，置信度需要结合工具失败原因审慎解读"

        # Keep raw observations once, with a per-command budget. Never truncate
        # serialized JSON: doing so can discard the schema or leave invalid JSON.
        sample_limit = min(12000, 24000 // max(1, len(command_records)))

        def compact_output(output):
            if not isinstance(output, dict):
                return {}
            return {key: (value[:2000] if isinstance(value, str) else value)
                    for key, value in output.items()
                    if key in {"root_cause", "root_cause_component", "fault_type", "confidence", "summary"}}

        readonly_checks = [
            {
                "command": item.get("command"),
                "success": bool(item.get("success")),
                "summary": str(item.get("summary") or "")[:400],
                "sample": evidence_sample(item.get('data') or item.get("sample") or "", sample_limit),
            }
            for item in command_records
            if isinstance(item, dict)
        ]
        runtime_runs = (
            (skill_result.get("skill_runtime") or {}).get("runs") or []
            if isinstance(skill_result, dict)
            and isinstance(skill_result.get("skill_runtime"), dict)
            else []
        )
        diagnose_stages = []
        for run in runtime_runs:
            if not isinstance(run, dict):
                continue
            stages = run.get("stages") if isinstance(run.get("stages"), dict) else {}
            diagnose = stages.get("diagnose") if isinstance(stages.get("diagnose"), dict) else {}
            if diagnose:
                diagnose_stages.append({
                    "skill_id": ((run.get("skill") or {}).get("id") if isinstance(run.get("skill"), dict) else ""),
                    "status": diagnose.get("status"),
                    "confidence": diagnose.get("confidence"),
                    "summary": diagnose.get("summary"),
                    "output": compact_output(diagnose.get("output")),
                    "evidence": list(diagnose.get("evidence") or [])[:12],
                })
        reasoning_payload = {
            # Put observed outputs first so prompt size limits cannot leave the
            # model with only hypotheses and an unexecuted-looking plan.
            "observed_entity_catalog": entity_catalog,
            "service_scope": service_scope,
            "readonly_checks": readonly_checks,
            "hypothesis_evidence_catalog": hypothesis_catalog,
            "currently_ready_pods": self._observed_ready_pods(command_records),
            "skill_diagnosis": compact_output(diagnosis),
            "skill_diagnose_stages": diagnose_stages,
            "query": request.query,
            "hypotheses": hypotheses[:5],
            "verification_plan": {
                "summary": verification_plan.get("summary"),
                "checks": verification_plan.get("checks") or [],
            },
            "observation_provenance": {
                "active": (request.skill_workflow.get("payload") or {}).get("observation_signals") or [],
                "background": (request.skill_workflow.get("payload") or {}).get("background_observation_signals") or [],
            },
            "output_schema": {
                "root_cause": "自包含的中文结论：已证实的完整对象名、当前异常状态及因果关系；证据不足则明确说明",
                "confidence": 0.0,
                "root_entity": "从 observed_entity_catalog 原样选择 cluster_id/namespace/kind/name/uid/resource_version；根因对象未证实则为 null",
                "reasoning_chain": "中文证据链",
                "evidence": ["关键证据"],
                "hypothesis_assessments": [{"id":"已有 H 编号", "confidence":0.5,
                    "status":"active|confirmed|rejected", "supporting_evidence_ids":["完整 obs- ID"],
                    "contradicting_evidence_ids":[], "unresolved_questions":["仍缺少的判别证据"],
                    "reasoning":"引用证据为何支持或反驳本候选"}],
            },
        }

        reasoning_messages = [
                {
                    "role": "system",
                    "content": (
                        "你是 SRE 证据裁决 Agent。必须基于当前观测、只读调查和 Skill 返回结果裁决；"
                        "Skill 是工具而不是最终结论权威。background_observation_signals 中的慢变或环境风险"
                        "不能作为当前事件根因，除非它同时出现在故障窗口活动观测中并有时序因果证据。"
                        "不得将实验场景 ID、注入类型、fault_experiment_signal、experiment_fault_type、scenario_id "
                        "或‘实验注入’作为根因或证据。"
                        "没有日志、启动参数、配置差异或对应事件时，不得臆测具体配置错误或内部机制；"
                        "只能把已观测的队列、延迟、资源、进程或服务发现异常作为结论。"
                        "readonly_checks 中 success=true 且 sample 非空的条目是已经执行的真实证据，不得称其未执行；"
                        "skill_diagnose_stages 中 status=ok 的结果必须与对应只读输出一起裁决。"
                        "root_cause 会独立展示，必须保留已证实的实际对象完整名称和导致故障的具体观测状态，"
                        "不能只写‘服务自身故障’等症状概括，也不能把关键定位信息仅放在 reasoning_chain 中。"
                        "currently_ready_pods 来自当前成功执行的 Pod 状态查询；这些 Pod 的旧 Warning 或启动时"
                        "readiness 失败事件不能证明当前仍未就绪。必须优先采用当前状态和故障窗口日志。"
                        "证据不足时必须明确说明，不得伪造根因。只返回 JSON。"
                        "root_entity 只能原样选择已观测目录中的完整身份，不能从名称惯例或根因文字推断 UID。"
                        "matches_workload_template 只是标签匹配，不是运行期所有权或因果证明；"
                        "service_scope 非 collected 时不得选择根因对象。身份绑定也不代表独立复核通过。"
                        "逐项评估 hypotheses，在 hypothesis_assessments 中只引用 hypothesis_evidence_catalog 的完整 ID；"
                        "没有引用不能确认或排除候选，采集失败不等于反驳。历史知识只用于候选，不是当前观测。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        reasoning_payload,
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            ]
        llm_reasoning = self._call_llm_json(
            "reasoning",
            reasoning_messages,
            max_tokens=2600,
        )
        llm_reasoning = self._normalize_reasoning_result(llm_reasoning)
        if self._mentions_experiment_metadata((llm_reasoning or {}).get("root_cause")):
            self._emit({
                "event": "llm_reasoning_retry",
                "source": self.name,
                "success": True,
                "summary": "首次推理引用了实验元数据，已要求 LLM 仅根据运行期证据重新裁决。",
            })
            retry_messages = [
                *reasoning_messages,
                {
                    "role": "assistant",
                    "content": json.dumps(llm_reasoning or {}, ensure_ascii=False, default=str),
                },
                {
                    "role": "user",
                    "content": (
                        "上述结论不合格：引用了实验标签或注入信息。"
                        "请删除这些内容，仅根据指标、日志、K8s 状态、请求表现和 Skill 输出重新裁决。"
                        "如果证据只能证明性能退化而不能证明具体资源根因，必须明确说明。只返回 JSON。"
                    ),
                },
            ]
            retried = self._normalize_reasoning_result(self._call_llm_json(
                "reasoning_retry",
                retry_messages,
                max_tokens=2200,
            ))
            if retried and not self._mentions_experiment_metadata(retried.get("root_cause")):
                llm_reasoning = retried
        payload = request.skill_workflow.get("payload") if isinstance(request.skill_workflow.get("payload"), dict) else {}
        validation_issues = [
            *self._reasoning_contradictions((llm_reasoning or {}).get("root_cause"), payload, command_records),
            *self._unsupported_specificity((llm_reasoning or {}).get("root_cause")),
        ]
        if validation_issues:
            self._emit({
                "event": "llm_reasoning_evidence_retry",
                "source": self.name,
                "success": True,
                "summary": "首次推理与观测矛盾或包含无证据的具体机制，已要求 LLM 重新裁决。",
                "contradictions": validation_issues,
            })
            evidence_retry = self._normalize_reasoning_result(self._call_llm_json(
                "reasoning_evidence_retry",
                [
                    *reasoning_messages,
                    {
                        "role": "assistant",
                        "content": json.dumps(llm_reasoning or {}, ensure_ascii=False, default=str),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"上述结论存在证据一致性问题: {json.dumps(validation_issues, ensure_ascii=False)}。"
                            "请以观测数值为准重新裁决，只报告已证明的症状和资源状态；"
                            "无日志或配置证据时，将具体触发机制标为待调查，不得写入根因。只返回 JSON。"
                        ),
                    },
                ],
                max_tokens=2200,
            ))
            retry_issues = [
                *self._reasoning_contradictions((evidence_retry or {}).get("root_cause"), payload, command_records),
                *self._unsupported_specificity((evidence_retry or {}).get("root_cause")),
            ]
            if evidence_retry and not retry_issues:
                llm_reasoning = evidence_retry
            elif (skill_root and self._has_verified_skill_observation(primary_skill, payload, success_commands)
                  and not self._reasoning_contradictions(skill_root, payload, command_records) and not self._unsupported_specificity(skill_root)):
                llm_reasoning = {
                    "root_cause": skill_root,
                    "confidence": skill_confidence,
                    "reasoning_chain": "LLM 结论与观测矛盾，采用 Agent 已调用专项 Skill 的可验证结论。",
                    "evidence": diagnosis.get("evidence") or [],
                }
            else:
                llm_reasoning = {"root_cause": "证据不足", "confidence": 0.0,
                                 "evidence": [], "reasoning_chain": "推理与观测不一致，且缺少独立验证的 Skill 结论。"}
        reasoning_root = str((llm_reasoning or {}).get("root_cause") or "").strip().casefold()
        evidence_insufficient = (
            reasoning_root in {"evidence_insufficient", "insufficient_evidence", "证据不足"}
            or "证据不足" in reasoning_root
            or "无法形成可靠根因" in reasoning_root
        )
        # A successful command is not evidence for an arbitrary Skill claim.
        # Preserve abstention unless a typed, target-matched observation proves
        # the specific conclusion. Template evidence never satisfies this gate.
        verified_skill_diagnosis = self._has_verified_skill_observation(
            primary_skill, payload, success_commands
        )
        if evidence_insufficient and verified_skill_diagnosis:
            llm_reasoning = {
                "root_cause": skill_root,
                "confidence": skill_confidence,
                "reasoning_chain": (
                    "LLM 将已执行的只读命令误判为缺少证据；采用已成功执行且 diagnose=ok "
                    "的专项 Skill 结论，并保留命令输出用于校准与审计。"
                ),
                "evidence": diagnosis.get("evidence") or [
                    item.get("sample") for item in success_commands if item.get("sample")
                ],
            }
            self._emit({
                "event": "llm_reasoning_evidence_guard",
                "source": self.name,
                "success": True,
                "summary": "LLM 忽略了已执行证据，已恢复经只读命令验证的 Skill 诊断。",
                "successful_commands": len(success_commands),
                "primary_skill_id": primary_skill,
            })
        if isinstance(llm_reasoning, dict) and llm_reasoning.get("root_cause"):
            root = str(llm_reasoning.get("root_cause"))
            parsed_confidence = self._parse_confidence_value(llm_reasoning.get("confidence"))
            if parsed_confidence is not None:
                confidence = parsed_confidence
            reasoning_chain = str(llm_reasoning.get("reasoning_chain") or reasoning_chain)
            if isinstance(skill_result, dict):
                updated_diagnosis = dict(diagnosis)
                updated_diagnosis.update({
                    "root_cause": root,
                    "confidence": confidence,
                    "reasoning_chain": reasoning_chain,
                    "agent_llm_reasoning": True,
                })
                if isinstance(llm_reasoning.get("evidence"), list) and (
                    llm_reasoning.get("evidence") or not updated_diagnosis.get("evidence")
                ):
                    updated_diagnosis["evidence"] = llm_reasoning.get("evidence")
                skill_result["diagnosis"] = updated_diagnosis

        bound_entity, entity_validation = bind_root_entity((llm_reasoning or {}).get('root_entity'),
                                                         entity_catalog, service_scope)
        if isinstance(skill_result, dict):
            skill_result.setdefault('diagnosis', {}).update(root_entity=bound_entity,
                                                         root_entity_validation=entity_validation)

        reasoned_fault_type = self._align_fault_type_with_reasoned_root(
            root,
            diagnosis.get("fault_type") or top.get("fault_type") or "observable_anomaly",
        )
        original_fault_type = str(diagnosis.get("fault_type") or "").strip()
        if isinstance(skill_result, dict) and reasoned_fault_type != original_fault_type:
            updated_diagnosis = dict(
                skill_result.get("diagnosis")
                if isinstance(skill_result.get("diagnosis"), dict)
                else diagnosis
            )
            if original_fault_type:
                updated_diagnosis["fault_type_original"] = original_fault_type
            updated_diagnosis["fault_type"] = reasoned_fault_type
            skill_result["diagnosis"] = updated_diagnosis
            self._emit({
                "event": "fault_type_evidence_alignment",
                "source": self.name,
                "success": True,
                "original_fault_type": original_fault_type,
                "fault_type": reasoned_fault_type,
                "summary": "最终根因已由当前只读证据证实，已校正与根因冲突的故障类型。",
            })

        from agents.hypothesis_agent import Hypothesis, HypothesisAgent
        typed = [Hypothesis(id=str(h.get("id") or f"H{i+1}"), description=str(h.get("description") or ""),
            confidence=float(h.get("confidence") or 0), service=str(h.get("service") or ""),
            fault_type=str(h.get("fault_type") or ""), source=str(h.get("source") or "llm")) for i,h in enumerate(hypotheses)]
        assessor = HypothesisAgent(None)
        assessor.set_evidence_catalog(hypothesis_catalog)
        assessed = assessor._grounded_updates(typed, {"hypotheses": (llm_reasoning or {}).get("hypothesis_assessments", [])}, False, 6)
        review_event_start = len(self.events)
        judge = review_skill_diagnosis(root_cause=root, reasoning=reasoning_chain, confidence=confidence,
            root_entity=bound_entity, commands=command_records,
            cluster_id=workflow_payload.get('cluster_id') or workflow_payload.get('target_cluster_id') or '',
            namespace=request.namespace or workflow_payload.get('namespace') or 'default',
            service_scope=service_scope, hypotheses=[h.to_dict() for h in assessed],
            call_model=lambda messages: self._call_llm_json('independent_review', messages, max_tokens=1800,
                request_timeout_s=self.llm_hypothesis_timeout_s, max_retries=0, require_complete=True))
        judge['skill_review']['provider_receipt'] = next((e.get('provider_receipt') for e in reversed(self.events[review_event_start:])
            if e.get('event') in ('llm_independent_review_result','llm_independent_review_incomplete')), None)
        scale_evidence = assess_scale_zero_evidence(root_entity=bound_entity,
            service=workflow_payload.get('service'), scope=service_scope, judge=judge)
        review_record = {'event': 'hypothesis_independent_review', 'stage': 'agent_skill_final_review',
            **(judge.get('hypothesis_review') or {}), 'needs_review': judge['needs_review'],
            'review_blockers': judge['review_blockers'], 'skill_review': judge['skill_review']}
        self._emit(review_record)
        self._emit({'event': 'judge', 'data': judge})
        if isinstance(skill_result, dict):
            skill_result['diagnosis'].update(judge=judge,
                diagnosis_status='requires_review' if judge['needs_review'] else 'reviewed',
                automation_gate={'allowed': False, 'blockers': list(dict.fromkeys(
                    ['skill_action_contract_required', *judge['review_blockers']]))})

        calibration = calibrate_rca_confidence(
            {
                "root_cause": root,
                "fault_type": reasoned_fault_type,
                "affected_services": [
                    value for value in (
                        payload.get("service"), payload.get("deployment"), payload.get("object"),
                    ) if value
                ],
                "confidence": confidence,
            },
            evidence=self._confidence_evidence(payload, evidence, skill_result),
            hypotheses=hypotheses,
            judge_result=judge,
            history_stats=(self._last_context.get("history_stats") if isinstance(self._last_context, dict) else {}),
        )
        confidence = calibration["confidence"]
        if isinstance(skill_result, dict):
            calibrated_diagnosis = dict(
                skill_result.get("diagnosis") if isinstance(skill_result.get("diagnosis"), dict) else diagnosis
            )
            calibrated_diagnosis.update({
                "root_cause": root,
                "fault_type": reasoned_fault_type,
                "confidence_raw": calibration.get("confidence_raw"),
                "confidence": confidence,
                "confidence_explanation": calibration.get("confidence_explanation") or {},
            })
            skill_result["diagnosis"] = calibrated_diagnosis
        self._emit({
            "event": "confidence_calibration",
            "source": self.name,
            "success": True,
            "raw_confidence": calibration.get("confidence_raw"),
            "confidence": confidence,
            "method": (calibration.get("confidence_explanation") or {}).get("method"),
            "factors": (calibration.get("confidence_explanation") or {}).get("factors") or {},
            "cap": (calibration.get("confidence_explanation") or {}).get("cap"),
        })

        workflow = evidence.setdefault("agent_workflow", {})
        # Proposer updates remain distinct from the independent final review.
        control=InvestigationController([],max_iterations=1,confidence_threshold=0.85)
        generation=getattr(self,"_generation_record",None) or control.record_generation(hypothesis_before,mode="agent_skill")
        strategy_record=control.decide(iteration=1,before=hypothesis_before,after=[h.to_dict() for h in assessed],
            catalog=hypothesis_catalog,novelty=control.observe(hypothesis_catalog),selected_agents=["skill_agent"],
            results={"skill_agent":{"error":not bool(success_commands)}},plan=verification_plan)
        strategy_record["mode"]="skill_owned_single_pass"
        evidence["diagnostic_strategy"]={"generation":generation,"rounds":[strategy_record],
            "independent_reviews": [review_record],
            "scale_zero_evidence": scale_evidence,
            "stop_reason":strategy_record["decision"]["reason"],"cross_incident_policy_learned":False,
            "adaptive_skill_replanning_enabled":False}
        self._emit(strategy_record)
        workflow["hypotheses"] = hypotheses
        workflow["verification_plan"] = verification_plan
        workflow["reasoning_chain"] = reasoning_chain
        workflow["final"] = {
            "root_cause": root,
            "confidence": confidence,
            "primary_skill_id": primary_skill,
            "evidence_commands": len(command_records),
            "successful_commands": len(success_commands),
        }
        self._emit({
            "event": "phase_start",
            "phase": "agent_skill_reasoning",
            "name": "REASONING_DECISION",
            "source": self.name,
        })
        self._emit({
            "event": "reasoning",
            "agent": self.name,
            "summary": reasoning_chain,
            "root_cause": root,
            "confidence": confidence,
            "primary_skill_id": primary_skill,
            "supporting_hypothesis": top.get("id") or "",
        })
        self._emit({
            "event": "phase_complete",
            "phase": "agent_skill_reasoning",
            "name": "REASONING_DECISION",
            "status": "completed" if skill_result else "failed",
            "data": workflow["final"],
        })

    def _skill_tool_runner(self, namespace: str) -> Callable[[str, Dict[str, Any], Dict[str, Any]], Dict[str, Any]]:
        def runner(tool: str, kwargs: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
            if tool == "kubectl":
                command = str(kwargs.get("command") or "").strip()
                ns = str(kwargs.get("namespace") or namespace or payload.get("namespace") or "default")
                result = self._run_kubectl(
                    command,
                    ns,
                    kubectl_available=bool(self.kubectl_runner) or self._tool_available("kubectl"),
                    payload=payload,
                )
                return result
            if self._tool_available(tool):
                tool_result = self.registry.execute(tool, **kwargs)
                data = tool_result.data
                return {
                    "success": bool(tool_result.success),
                    "executed": True,
                    "source": tool,
                    "summary": f"工具 {tool} 执行成功" if tool_result.success else f"工具 {tool} 执行失败: {tool_result.error or 'unknown error'}",
                    "data": data,
                    "sample": str(data or tool_result.error or "")[:1200],
                    "error": tool_result.error,
                    "returncode": 0 if tool_result.success else 1,
                }
            return {
                "success": False,
                "executed": False,
                "error": f"Agent+Skill 当前未配置 {tool} 工具运行器",
                "summary": f"工具 {tool} 未接入 Agent 侧运行器，已跳过真实执行。",
            }

        return runner

    @staticmethod
    def _has_verified_skill_observation(skill_id, payload, commands) -> bool:
        if skill_id != "k8s.service_unavailable":
            return False
        deployment = str(payload.get("deployment") or "")
        namespace = str(payload.get("namespace") or "default")
        cluster_id = str(payload.get("cluster_id") or payload.get("target_cluster_id") or "")
        if not deployment:
            return False
        for item in commands:
            try:
                args = readonly_kubectl_args(str(item.get("command") or ""), namespace)
                value = json.loads(item.get("sample") or "")
            except (ValueError, TypeError):
                continue
            if args[:2] != ["get", f"deployment/{deployment}"] or not isinstance(value, dict):
                continue
            if item.get("executed") is not True or str(item.get("cluster_id") or "") != cluster_id:
                continue
            metadata = value.get("metadata") or {}
            if metadata.get("name") != deployment or metadata.get("namespace") != namespace:
                continue
            if value.get("spec", {}).get("replicas") == 0 and value.get("status", {}).get("readyReplicas", 0) == 0:
                return True
        return False

    def _confidence_evidence(
        self,
        payload: Dict[str, Any],
        evidence: Dict[str, Any],
        skill_result: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        calibrated_evidence = dict(evidence or {})
        signals = payload.get("observation_signals") if isinstance(payload.get("observation_signals"), list) else []
        structured_items = []
        for item in signals:
            if not isinstance(item, dict):
                continue
            structured_items.append({
                "signal_type": item.get("signal") or item.get("metric") or "observation_signal",
                "source": item.get("source") or "observation",
                "observation": str(item.get("label") or item.get("signal") or item.get("metric") or ""),
                "severity": item.get("severity") or "warning",
                "supports": ["root_cause"],
                "raw_ref": item,
            })
        command_records = (
            evidence.get("agent_evidence_collection", {}).get("commands", [])
            if isinstance(evidence.get("agent_evidence_collection"), dict)
            else []
        )
        for item in command_records:
            if not isinstance(item, dict) or not item.get("success") or not item.get("sample"):
                continue
            structured_items.append({
                "signal_type": "readonly_command_result",
                "source": f"kubectl {item.get('command') or ''}".strip(),
                "observation": str(item.get("sample") or "")[:1200],
                "severity": "info",
                "supports": ["root_cause"],
                "raw_ref": {
                    "command": item.get("command"),
                    "cluster_id": item.get("cluster_id"),
                },
            })
        calibrated_evidence["structured_evidence"] = {"items": structured_items}

        # Skill summaries are conclusions, not independent observation sources.
        # Actual runner records already appear in the command/tool evidence.
        return calibrated_evidence

    def _emit(self, event: Dict[str, Any]) -> None:
        event.setdefault("timestamp", time.time())
        self.events.append(event)
        if self.event_sink:
            self.event_sink(event)

    def _call_llm_json(
        self,
        stage: str,
        messages: List[Dict[str, str]],
        *,
        max_tokens: int,
        request_timeout_s: Optional[float] = None,
        max_retries: Optional[int] = None,
        require_complete: bool = False,
    ) -> Optional[Dict[str, Any]]:
        if not self.llm_client:
            return None
        started = time.monotonic()
        self._emit({
            "event": f"llm_{stage}_request",
            "source": self.name,
            "success": True,
            "summary": f"Agent 正在调用 LLM 执行 {stage}。",
        })
        executor = ThreadPoolExecutor(max_workers=1)
        options = {}
        if request_timeout_s is not None: options['request_timeout_s'] = request_timeout_s
        if max_retries is not None: options['max_retries'] = max_retries
        if require_complete: options['with_receipt'] = True
        future = executor.submit(
            self._json_chat_bounded,
            messages,
            temperature=0.1,
            max_tokens=max_tokens,
            **options,
        )
        try:
            result = future.result(timeout=self.llm_hypothesis_timeout_s)
            receipt = None
            if isinstance(result, JSONCompletion) and isinstance(result.provider, dict):
                provider = result.provider
                receipt = {
                    'schema_version': 1 if type(provider.get('schema_version')) is int and provider['schema_version']==1 else None,
                    'source': 'provider_response' if provider.get('source')=='provider_response' else 'unverified',
                    'completion_status': provider.get('completion_status') if provider.get('completion_status') in
                        ('complete','truncated','filtered','incomplete','refused','tool_call','invalid_choices') else 'unknown',
                    'finish_reason': provider.get('finish_reason') if provider.get('finish_reason') in
                        ('stop','length','content_filter','tool_calls','function_call') else 'unknown',
                    'refusal_present': provider.get('refusal_present') if type(provider.get('refusal_present')) is bool else None,
                    'tool_call_present': provider.get('tool_call_present') if type(provider.get('tool_call_present')) is bool else None,
                }
            if isinstance(result, JSONCompletion): result = result.data
            if require_complete and (not isinstance(receipt, dict) or receipt.get('schema_version') != 1 or receipt.get('source') != 'provider_response'
                    or receipt.get('completion_status') != 'complete' or receipt.get('finish_reason') != 'stop'
                    or receipt.get('refusal_present') is not False or receipt.get('tool_call_present') is not False):
                self._emit({'event': f'llm_{stage}_incomplete', 'source': self.name, 'success': False,
                    'provider_receipt': receipt,
                    'summary': '复核响应没有可验证的完整停止收据，保持待审核。'})
                return None
            if isinstance(result, dict) and result and not result.get("parse_error"):
                self._emit({
                    "event": f"llm_{stage}_result",
                    "source": self.name,
                    "success": True,
                    "summary": f"Agent 已完成 LLM {stage}。",
                    "response_keys": sorted(result.keys()),
                    **({'provider_receipt': receipt} if require_complete else {}),
                    "duration_s": round(time.monotonic() - started, 3),
                    "response_preview": json.dumps(result, ensure_ascii=False, default=str)[:2000],
                })
                return result
            self._emit({"event": f"llm_{stage}_invalid", "source": self.name,
                "success": False, "duration_s": round(time.monotonic() - started, 3),
                "summary": "模型返回空内容或不可解析 JSON，未接受为有效推理。"})
        except FutureTimeoutError:
            future.cancel()
            self._emit({
                "event": f"llm_{stage}_timeout",
                "source": self.name,
                "success": False,
                "summary": f"LLM {stage} 超时，未取得有效模型结果。",
            })
        except Exception as exc:
            self._emit({
                "event": f"llm_{stage}_error",
                "source": self.name,
                "success": False,
                "summary": f"LLM {stage} 失败，未取得有效模型结果: {exc}",
            })
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return None

    def _json_chat_bounded(self, messages, **kwargs):
        # Optional controls also support injected clients with older signatures.
        parameters = inspect.signature(self.llm_client.json_chat).parameters
        variadic = any(p.kind == p.VAR_KEYWORD for p in parameters.values())
        if "thinking" in parameters or variadic:
            kwargs["thinking"] = False
        for option in ('request_timeout_s', 'max_retries', 'with_receipt'):
            if option not in parameters and not variadic: kwargs.pop(option, None)
        return self.llm_client.json_chat(messages, **kwargs)

    def _normalize_reasoning_result(self, value: Any, depth: int = 0) -> Optional[Dict[str, Any]]:
        if depth > 5:
            return None
        if isinstance(value, str):
            parsed = self._parse_json_value(value)
            return self._normalize_reasoning_result(parsed, depth + 1) if parsed is not None else None
        if not isinstance(value, dict):
            return None
        root = value.get("root_cause") or value.get("根因") or value.get("conclusion") or value.get("结论")
        if root:
            evidence = value.get("evidence") or value.get("证据") or []
            return {
                "root_cause": str(root),
                "confidence": value.get("confidence_score", value.get("confidence", value.get("置信度"))),
                "reasoning_chain": value.get("reasoning_chain") or value.get("reasoning") or value.get("推理链") or "",
                "evidence": evidence if isinstance(evidence, list) else [str(evidence)],
                **({'root_entity': value['root_entity']} if isinstance(value.get('root_entity'), dict) else {}),
                **({"hypothesis_assessments": value["hypothesis_assessments"]}
                   if isinstance(value.get("hypothesis_assessments"),list) else {}),
            }
        for key in ("diagnosis", "final_diagnosis", "result", "data", "output", "response", "content", "raw_response"):
            normalized = self._normalize_reasoning_result(value.get(key), depth + 1)
            if normalized:
                return normalized
        return None

    @staticmethod
    def _mentions_experiment_metadata(value: Any) -> bool:
        text = str(value or "").casefold()
        return any(marker in text for marker in (
            "fault_experiment_signal",
            "experiment_fault_type",
            "scenario_id",
            "实验注入",
            "注入故障",
        ))

    @staticmethod
    def _parse_confidence_value(value: Any) -> Optional[float]:
        if isinstance(value, (int, float)):
            return max(0.0, min(1.0, float(value)))
        text = str(value or "").strip().casefold()
        if not text:
            return None
        try:
            if text.endswith("%"):
                return max(0.0, min(1.0, float(text[:-1].strip()) / 100.0))
            return max(0.0, min(1.0, float(text)))
        except (TypeError, ValueError):
            pass
        qualitative = {
            "低": 0.35,
            "较低": 0.45,
            "中": 0.6,
            "中等": 0.6,
            "中等置信度": 0.6,
            "中等可能性": 0.6,
            "较高": 0.78,
            "高": 0.88,
            "low": 0.35,
            "medium": 0.6,
            "moderate": 0.6,
            "high": 0.88,
        }
        if text in qualitative:
            return qualitative[text]
        for label, score in sorted(qualitative.items(), key=lambda item: len(item[0]), reverse=True):
            if label in text:
                return score
        return None

    @staticmethod
    def _observed_ready_pods(command_records) -> List[str]:
        names = []
        for record in command_records:
            if not record.get("executed") or not record.get("success"):
                continue
            if not re.match(r"^get\s+(?:pods?|po)(?:\s|$)", record.get("command", "")):
                continue
            for line in str(record.get("sample") or "").splitlines():
                match = re.match(r"(\S+)\s+(\d+)/(\d+)\s+Running\b", line.strip())
                if match and int(match[2]) > 0 and match[2] == match[3]:
                    names.append(match[1])
        return sorted(set(names))

    @staticmethod
    def _reasoning_contradictions(value: Any, payload: Dict[str, Any], command_records=()) -> List[str]:
        root = str(value or "").casefold()
        observation = payload.get("observation") if isinstance(payload.get("observation"), dict) else payload
        contradictions: List[str] = []
        if re.search(r"(?:readiness\s*probe|就绪探针).{0,20}(?:失败|failed)|未就绪|无就绪副本", root):
            for name in SkillAgent._observed_ready_pods(command_records):
                if name.casefold() in root:
                    contradictions.append(f"Pod/{name} 当前 Running 且全部容器 Ready，旧事件不能证明当前未就绪")

        desired = observation.get("deployment_desired_replicas")
        ready = observation.get("deployment_ready_replicas")
        try:
            fully_ready = float(desired) > 0 and float(ready) >= float(desired)
        except (TypeError, ValueError):
            fully_ready = False
        if fully_ready and any(marker in root for marker in (
            "readiness probe", "readiness探针", "就绪探针", "pod未就绪", "未达到就绪",
        )):
            contradictions.append(f"Deployment 已就绪 {ready}/{desired}，不支持 readiness 失败")

        restart_count = observation.get("pod_restart_count")
        if restart_count is not None:
            try:
                no_restart = float(restart_count) == 0
            except (TypeError, ValueError):
                no_restart = False
            if no_restart and any(marker in root for marker in ("进程崩溃", "pod重启", "容器重启", "process crash")):
                contradictions.append("Pod 重启数为 0，不支持进程崩溃或容器重启")

        kv_cache = observation.get("kv_cache_usage")
        if isinstance(kv_cache, (int, float)):
            kv_pct = float(kv_cache) * 100.0 if 0 <= float(kv_cache) <= 1 else float(kv_cache)
            thresholds = payload.get("observation_thresholds") if isinstance(payload.get("observation_thresholds"), dict) else {}
            try:
                kv_threshold = float(thresholds.get("kv_cache"))
            except (TypeError, ValueError):
                kv_threshold = None
            if kv_threshold is not None and kv_pct < kv_threshold and any(marker in root for marker in ("kv cache 耗尽", "kv cache耗尽", "kv cache exhaustion")):
                contradictions.append(f"KV Cache 使用率仅 {kv_pct:.1f}%，不支持耗尽结论")
        return contradictions

    @staticmethod
    def _unsupported_specificity(value: Any) -> List[str]:
        root = str(value or "").casefold()
        speculative = any(marker in root for marker in ("推测", "疑似", "猜测", "speculat", "suspect"))
        concrete_mechanism = any(marker in root for marker in (
            "max_num_", "max_model_", "配置参数", "内部错误", "死锁", "deadlock",
            "超长输入", "计算停滞", "并发槽位",
        ))
        if speculative and concrete_mechanism:
            return ["结论将未经日志或配置证据确认的具体机制写成根因"]
        return []

    def _tool_available(self, name: str) -> bool:
        try:
            return bool(getattr(self.registry, "get", lambda _name: None)(name))
        except Exception:
            return False

    def _retrieve_context(self, request: SkillAgentRequest, payload: Dict[str, Any]) -> Dict[str, Any]:
        context: Dict[str, Any] = {
            "rules": [],
            "faults": [],
            "recent_runs": [],
            "history_stats": {},
            "current_evidence": payload.get("evidence") if isinstance(payload.get("evidence"), list) else [],
            "observation": payload.get("observation") if isinstance(payload.get("observation"), dict) else {},
            "observation_signals": payload.get("observation_signals") if isinstance(payload.get("observation_signals"), list) else [],
            "background_observation_signals": payload.get("background_observation_signals") if isinstance(payload.get("background_observation_signals"), list) else [],
            "candidate_skills": payload.get("candidate_skills") if isinstance(payload.get("candidate_skills"), list) else [],
            "candidate_faults": payload.get("candidate_faults") if isinstance(payload.get("candidate_faults"), list) else [],
        }
        self._emit({
            "event": "phase_start",
            "phase": "agent_skill_context_retrieval",
            "name": "CONTEXT_RETRIEVAL",
            "source": self.name,
        })
        if self.context_provider:
            try:
                provided = self.context_provider(request)
                if isinstance(provided, dict):
                    for key, value in provided.items():
                        if value:
                            context[key] = value
            except Exception:
                context["context_error"] = 'history_provider_error'
        from memory.history_retrieval import empty_history
        retrieval = context.get('history_retrieval') or empty_history(
            'unavailable' if context.get('context_error') or context.get('fault_store_error') else
            ('disabled' if context.get('history_disabled') else ('not_recorded' if self.context_provider else 'not_requested'))
        )['retrieval']
        context['history_retrieval'] = retrieval
        degraded = retrieval.get('status') in ('partial', 'timed_out', 'busy', 'unavailable') or bool(context.get('context_error') or context.get('fault_store_error'))
        self._last_context = dict(context)
        self._emit({'event': 'history_retrieval', 'source': self.name, 'data': retrieval})
        self._emit({
            "event": "context",
            "source": self.name,
            "summary": f"历史检索状态：{retrieval.get('status')}；历史仅用于候选假设，当前根因仍需实时证据核验。",
            "history_retrieval": retrieval,
            "rules_count": len(context.get("rules") or []),
            "faults_count": len(context.get("faults") or []),
            "recent_runs_count": len(context.get("recent_runs") or []),
            "history_stats": context.get("history_stats") or {},
            "success": not degraded,
        })
        self._emit({
            "event": "phase_complete",
            "phase": "agent_skill_context_retrieval",
            "name": "CONTEXT_RETRIEVAL",
            "status": "degraded" if degraded else "completed",
            "notes": f"{len(context.get('rules') or [])} rules, {len(context.get('faults') or [])} faults",
        })
        return context

    def _generate_llm_hypotheses(
        self,
        request: SkillAgentRequest,
        payload: Dict[str, Any],
        context: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        if not self.llm_client:
            self._emit({
                "event": "llm_required_missing",
                "source": self.name,
                "summary": "Agent+Skill 模式必须使用 LLM 生成候选假设，当前未配置 LLM。",
                "success": False,
            })
            return []
        prompt = self._build_llm_hypothesis_prompt(request, payload, context)
        if self._skill_owned_investigation:
            prompt += "\n请在同一个 JSON 中同时返回 hypotheses 和 skill_calls。由 Skill 执行调查，勿在 Agent 侧重复调查。\n"
            prompt += json.dumps({"available_skills": self._skill_catalog(payload),
                "required_schema": {"hypotheses": [{"description": "待验证的具体根因假设", "confidence": 0.5}],
                    "skill_calls": [{"skill_id": "目录中的精确 ID", "stages": ["detect", "diagnose"],
                        "target": {"pod": "实际目标，可省略"}, "commands": ["只读 kubectl 参数，可省略"]}]},
                "constraints": "选择最多两个互补 Skill；镜像状态选专项 Skill，应用层503可用generic.evidence_collection采集当前日志。不得选择负载生成或写操作。只输出紧凑 JSON。"}, ensure_ascii=False)
        self._emit({
            "event": "llm_hypothesis_request",
            "source": self.name,
            "summary": "使用 LLM 结合当前事件、历史规则、相似故障和观测证据生成候选根因假设。",
            "prompt_preview": prompt[:1200],
        })
        messages = [
            {
                "role": "system",
                "content": (
                    "你是资深 SRE/AI Infra 根因分析专家。除命令、资源名和枚举值外，"
                    "所有诊断内容必须使用中文。只能使用观测证据、K8s 状态、指标、日志、Trace 和历史知识推理；"
                    "不得使用实验场景 ID、注入故障类型、experiment_fault_type、scenario_id 或任何测试标签作为根因依据。只返回 JSON。"
                ),
            },
            {"role": "user", "content": prompt},
        ]
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(
            self._json_chat_bounded,
            messages,
            temperature=0.1,
            max_tokens=2400,
        )
        try:
            data = future.result(timeout=self.llm_hypothesis_timeout_s)
        except FutureTimeoutError:
            future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            self._emit({
                "event": "llm_hypothesis_timeout",
                "source": self.name,
                "summary": f"LLM 假设生成超过 {self.llm_hypothesis_timeout_s:g} 秒，未取得有效假设。",
                "success": False,
            })
            return []
        except Exception as exc:
            executor.shutdown(wait=False, cancel_futures=True)
            self._emit({
                "event": "llm_hypothesis_error",
                "source": self.name,
                "summary": f"LLM 假设生成失败，未取得有效假设: {exc}",
                "success": False,
            })
            return []
        finally:
            if future.done():
                executor.shutdown(wait=False, cancel_futures=True)
        data = self._normalize_llm_hypothesis_response(data)
        self._last_decision = data
        items = data.get("hypotheses") if isinstance(data, dict) and isinstance(data.get("hypotheses"), list) else []
        hypotheses: List[Dict[str, Any]] = []
        for item in items[:5]:
            if not isinstance(item, dict):
                continue
            desc = str(
                item.get("description")
                or item.get("root_cause")
                or item.get("hypothesis")
                or item.get("root_cause_hypothesis")
                or item.get("假设")
                or item.get("假设描述")
                or item.get("根因假设")
                or item.get("根因")
                or ""
            ).strip()
            if not desc:
                continue
            hypotheses.append({
                "id": item.get("id") or f"L{len(hypotheses)+1}",
                "description": desc,
                "confidence": self._clamp_float(item.get("confidence") or item.get("置信度"), 0.5),
                "service": item.get("service") or item.get("component") or item.get("服务") or item.get("组件") or "",
                "fault_type": item.get("fault_type") or item.get("故障类型") or payload.get("fault_type") or "",
                "expected_evidence": (
                    item.get("expected_evidence")
                    or item.get("预期证据")
                    or item.get("evidence")
                    or item.get("investigation_plan")
                    or []
                ),
                "investigation_plan": item.get("investigation_plan") or item.get("调查计划") or [],
                "refutes_if": item.get("refutes_if") or [],
                "supporting_evidence": item.get("supporting_evidence") or item.get("支持证据") or [],
                "source": "llm",
                "llm_used": True,
            })
        self._emit({
            "event": "llm_hypotheses",
            "source": self.name,
            "items": [
                {
                    "id": item.get("id"),
                    "confidence": item.get("confidence"),
                    "description": item.get("description"),
                    "fault_type": item.get("fault_type"),
                }
                for item in hypotheses
            ],
            "success": bool(hypotheses),
            "response_preview": self._json_preview(data, 1500) if not hypotheses else "",
        })
        return hypotheses

    def _normalize_llm_hypothesis_response(self, data: Any) -> Dict[str, Any]:
        if isinstance(data, str):
            parsed = self._parse_jsonish_response(data)
            return self._normalize_llm_hypothesis_response(parsed) if parsed is not None else {}
        if not isinstance(data, dict):
            return {}
        if isinstance(data.get("hypotheses"), list):
            return data
        if data.get("description") and "confidence" in data:
            return {**data, "hypotheses": [data]}
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
                content = message.get("content") or choice.get("text")
                parsed = self._parse_jsonish_response(content)
                if parsed is not None:
                    normalized = self._normalize_llm_hypothesis_response(parsed)
                    if normalized.get("hypotheses"):
                        return normalized
        for key in (
            "candidates",
            "candidate_hypotheses",
            "root_causes",
            "root_cause_hypotheses",
            "rootCauseHypotheses",
            "假设列表",
            "候选假设",
            "根因假设",
            "根因假设列表",
        ):
            if isinstance(data.get(key), list):
                return {"hypotheses": data.get(key)}
        nested = self._find_hypothesis_list(data)
        if nested:
                return {"hypotheses": nested}
        raw = data.get("raw_response")
        if isinstance(raw, str) and raw.strip():
            parsed = self._parse_jsonish_response(raw)
            if parsed is not None:
                return self._normalize_llm_hypothesis_response(parsed)
        return data

    def _parse_jsonish_response(self, raw: Any) -> Any:
        """Extract the first valid JSON object/list from an LLM text response."""
        if not isinstance(raw, str):
            return None
        text = raw.strip()
        if not text:
            return None
        fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S | re.I)
        if fence:
            text = fence.group(1).strip()
        candidates = [text]
        for opener in ("{", "["):
            index = text.find(opener)
            if index >= 0 and text[index:] not in candidates:
                candidates.append(text[index:])
        decoder = json.JSONDecoder()
        for candidate in candidates:
            try:
                return json.loads(candidate)
            except Exception:
                pass
            for index, ch in enumerate(candidate):
                if ch not in "{[":
                    continue
                try:
                    parsed, _end = decoder.raw_decode(candidate[index:])
                    return parsed
                except Exception:
                    continue
        return None

    def _find_hypothesis_list(self, value: Any, depth: int = 0) -> List[Dict[str, Any]]:
        if depth > 4:
            return []
        if isinstance(value, list):
            dicts = [item for item in value if isinstance(item, dict)]
            if dicts and any(
                any(key in item for key in (
                    "description", "root_cause", "hypothesis", "root_cause_hypothesis",
                    "假设", "假设描述", "根因假设", "根因"
                ))
                for item in dicts
            ):
                return dicts
            for item in value:
                found = self._find_hypothesis_list(item, depth + 1)
                if found:
                    return found
        if isinstance(value, dict):
            for item in value.values():
                found = self._find_hypothesis_list(item, depth + 1)
                if found:
                    return found
        return []

    def _build_llm_hypothesis_prompt(
        self,
        request: SkillAgentRequest,
        payload: Dict[str, Any],
        context: Dict[str, Any],
    ) -> str:
        rules = context.get("rules") if isinstance(context.get("rules"), list) else []
        faults = context.get("faults") if isinstance(context.get("faults"), list) else []
        recent_runs = context.get("recent_runs") if isinstance(context.get("recent_runs"), list) else []
        evidence = context.get("current_evidence") if isinstance(context.get("current_evidence"), list) else []
        observation_signals = context.get("observation_signals") if isinstance(context.get("observation_signals"), list) else []
        background_signals = context.get("background_observation_signals") if isinstance(context.get("background_observation_signals"), list) else []
        candidate_skills = context.get("candidate_skills") if isinstance(context.get("candidate_skills"), list) else []
        candidate_faults = context.get("candidate_faults") if isinstance(context.get("candidate_faults"), list) else []
        history_stats = context.get("history_stats") if isinstance(context.get("history_stats"), dict) else {}
        return (
            "请根据当前故障信号、历史规则、相似故障和观测证据，生成最多 3 个按置信度排序的根因假设。\n"
            "要求：\n"
            "1. 假设必须具体到组件/服务/资源，不要泛泛而谈。\n"
            "2. 每个假设给出 expected_evidence、investigation_plan、supporting_evidence，每个数组最多 3 条，每条不超过 40 字。\n"
            "3. 如果历史案例支持或反驳该假设，需要写入 supporting_evidence。\n"
            "4. 不要输出恢复动作，只输出诊断假设。\n\n"
            "5. 禁止使用实验场景 ID、注入故障类型、experiment_fault_type、scenario_id 或测试标签；"
            "如果输入中出现这类字段，必须忽略。\n\n"
            "6. observation_signals、candidate_skills、candidate_faults 只是检测层给出的待验证线索，"
            "不能直接当成最终根因；必须说明需要哪些证据验证或反驳。\n\n"
            "7. background_observation_signals 是环境背景风险或慢变指标，只能用于风险提示，"
            "不能作为当前事件根因，除非当前证据显示该指标在故障窗口内突变或直接导致服务异常。\n\n"
            "8. 根因粒度不得超过观测证据；不得猜测未采集到的配置参数、参数值或内部实现细节。"
            "例如证据只支持 KV Cache 压力导致排队时，不得声称某个 gpu_memory_utilization 数值配置错误。\n\n"
            f"当前查询:\n{request.query[:2500]}\n\n"
            f"结构化 payload:\n{self._json_preview(payload, 2500)}\n\n"
            f"当前证据:\n{self._json_preview(evidence, 1800)}\n\n"
            f"观测候选信号:\n{self._json_preview(observation_signals, 1800)}\n\n"
            f"背景观测信号:\n{self._json_preview(background_signals, 1200)}\n\n"
            f"候选 Skill:\n{self._json_preview(candidate_skills, 1000)}\n\n"
            f"候选故障类型:\n{self._json_preview(candidate_faults, 1000)}\n\n"
            f"历史规则 Top5:\n{self._json_preview(rules[:5], 2000)}\n\n"
            f"相似故障 Top5:\n{self._json_preview(faults[:5], 2400)}\n\n"
            f"近期 RCA/Skill 运行 Top5:\n{self._json_preview(recent_runs[:5], 1600)}\n\n"
            f"经确认的历史诊断统计:\n{self._json_preview(history_stats, 800)}\n\n"
            "必须只返回 JSON，不要 markdown，不要解释文字：\n"
            '{"hypotheses":[{"id":"H1","description":"中文根因假设","confidence":0.82,'
            '"service":"组件或服务","fault_type":"枚举值","expected_evidence":["证据"],'
            '"investigation_plan":["能区分前两名的只读调查步骤"],"refutes_if":["可观测的反驳条件"],'
            '"supporting_evidence":["历史或当前证据，注明来源"]}]}'
        )

    def _merge_and_rank_hypotheses(
        self,
        hypotheses: List[Dict[str, Any]],
        *,
        payload: Dict[str, Any],
        context: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        merged: Dict[str, Dict[str, Any]] = {}
        for item in hypotheses:
            if not isinstance(item, dict):
                continue
            desc = str(item.get("description") or "").strip()
            if not desc:
                continue
            fault_type = str(item.get("fault_type") or payload.get("fault_type") or "").strip()
            if self._mentions_experiment_metadata(fault_type):
                fault_type = "observable_anomaly"
            key = f"{fault_type}:{desc[:48]}".lower()
            existing = merged.get(key)
            if existing and self._clamp_float(existing.get("confidence"), 0.0) >= self._clamp_float(item.get("confidence"), 0.0):
                existing.setdefault("supporting_evidence", []).extend(item.get("supporting_evidence") or [])
                continue
            candidate = dict(item)
            candidate["fault_type"] = fault_type
            candidate.setdefault("source", "llm" if candidate.get("llm_used") else "deterministic")
            candidate["llm_used"] = bool(candidate.get("llm_used"))
            candidate["confidence"] = self._score_hypothesis(candidate, payload, context)
            merged[key] = candidate

        ranked = sorted(merged.values(), key=lambda x: self._clamp_float(x.get("confidence"), 0.0), reverse=True)[:5]
        for index, item in enumerate(ranked, start=1):
            item["id"] = f"H{index}"
            item["rank"] = index
            item["confidence"] = round(self._clamp_float(item.get("confidence"), 0.5), 3)
        return ranked

    def _hypothesis_generation_summary(self, hypotheses: List[Dict[str, Any]]) -> Dict[str, Any]:
        llm_count = sum(1 for item in hypotheses if item.get("llm_used"))
        return {
            "method": "llm_with_history_context" if llm_count else "deterministic_with_history_context",
            "llm_used": llm_count > 0,
            "llm_hypothesis_count": llm_count,
            "deterministic_hypothesis_count": max(0, len(hypotheses) - llm_count),
            "history_context_used": any(item.get("supporting_evidence") for item in hypotheses),
            "sources": sorted(set(str(item.get("source") or "deterministic") for item in hypotheses)),
        }

    def _score_hypothesis(self, item: Dict[str, Any], payload: Dict[str, Any], context: Dict[str, Any]) -> float:
        base = self._clamp_float(item.get("confidence"), 0.5)
        text = " ".join([
            str(item.get("description") or ""),
            str(item.get("fault_type") or ""),
            " ".join(str(x) for x in item.get("expected_evidence") or []),
        ]).lower()
        context_text = self._context_text(payload, context, include_background=False)
        supporting = list(item.get("supporting_evidence") or [])
        evidence_bonus = 0.0
        hypothesis_tokens = self._tokens(text)[:24]
        for token in hypothesis_tokens:
            if token and token in context_text:
                evidence_bonus += 0.012
        if evidence_bonus:
            supporting.append("当前告警/观测证据与该假设存在关键词重合")
        history_bonus = 0.0
        fault_type = str(item.get("fault_type") or "").lower()
        for fault in context.get("faults") or []:
            if not isinstance(fault, dict):
                continue
            hist_type = str(fault.get("fault_type") or fault.get("error_category") or "").lower()
            hist_root = str(fault.get("root_cause") or fault.get("description") or "").lower()
            if fault_type and fault_type == hist_type:
                history_bonus += 0.04
                supporting.append(f"相似历史故障类型命中: {hist_type}")
            elif any(token in hist_root for token in self._tokens(text)[:12]):
                history_bonus += 0.02
        stats = context.get("history_stats") if isinstance(context.get("history_stats"), dict) else {}
        if int(float(stats.get("diagnosis_sample_count") or 0)) >= 5 and stats.get("diagnosis_accuracy_lower_bound") is not None:
            lower_bound = self._clamp_float(stats.get("diagnosis_accuracy_lower_bound"), 0.5)
            supporting.append(f"同类历史诊断正确率置信下界 {lower_bound:.0%}")
        llm_bonus = 0.03 if item.get("llm_used") else 0.0
        background_tokens = self._signal_terms(
            context.get("background_observation_signals")
            or payload.get("background_observation_signals")
            or []
        )
        causal_tokens = self._tokens(context_text)
        background_only_tokens = [
            token for token in background_tokens
            if not self._tokens_overlap([token], causal_tokens)
        ]
        background_only_match = self._tokens_overlap(hypothesis_tokens, background_only_tokens)
        active_signal_tokens = self._signal_terms(
            context.get("observation_signals") or payload.get("observation_signals") or []
        )
        background_promoted_to_active = self._tokens_overlap(background_tokens, active_signal_tokens)
        item["supporting_evidence"] = list(dict.fromkeys(str(x) for x in supporting if x))[:8]
        score = min(0.96, max(0.05, base + min(evidence_bonus, 0.12) + min(history_bonus, 0.1) + llm_bonus))
        if background_only_match and not background_promoted_to_active:
            item["supporting_evidence"] = list(dict.fromkeys([
                *item["supporting_evidence"],
                "该假设仅命中背景风险信号，缺少故障窗口内的因果证据",
            ]))[:8]
            score = min(score, 0.55)
        return score

    def _compact_context_for_evidence(self, hypotheses: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "history_retrieval": getattr(self, '_last_context', {}).get('history_retrieval', {}),
            "llm_used": any(item.get("llm_used") for item in hypotheses),
            "sources": sorted(set(str(item.get("source") or "deterministic") for item in hypotheses)),
            "top_supporting_evidence": [
                evidence
                for item in hypotheses[:3]
                for evidence in (item.get("supporting_evidence") or [])[:2]
            ][:8],
        }

    def _context_text(
        self,
        payload: Dict[str, Any],
        context: Dict[str, Any],
        *,
        include_background: bool = True,
    ) -> str:
        background_names = {
            str(item.get("signal") or item.get("metric") or "")
            for item in context.get("background_observation_signals") or payload.get("background_observation_signals") or []
            if isinstance(item, dict)
        }
        payload_view = payload if include_background else self._without_background_signals(payload, background_names)
        parts = [self._json_preview(payload_view, 4000)]
        keys = (
            "current_evidence", "observation", "observation_signals",
            "candidate_skills", "candidate_faults", "rules", "faults", "recent_runs",
        )
        if include_background:
            keys = (*keys, "background_observation_signals")
        for key in keys:
            value = context.get(key)
            if not include_background:
                value = self._without_background_signals(value, background_names)
            parts.append(self._json_preview(value, 4000))
        return "\n".join(parts).lower()

    def _without_background_signals(self, value: Any, background_names: set[str]) -> Any:
        if isinstance(value, dict):
            signal = str(value.get("signal") or value.get("metric") or "")
            if signal and signal in background_names:
                return None
            return {
                key: self._without_background_signals(item, background_names)
                for key, item in value.items()
                if key != "background_observation_signals" and key not in background_names
            }
        if isinstance(value, list):
            return [
                cleaned
                for item in value
                if (cleaned := self._without_background_signals(item, background_names)) is not None
            ]
        return value

    def _tokens_overlap(self, left: List[str], right: List[str]) -> bool:
        for left_token in left:
            if len(left_token) < 3:
                continue
            for right_token in right:
                if len(right_token) < 3:
                    continue
                if left_token in right_token or right_token in left_token:
                    return True
        return False

    def _signal_terms(self, signals: Any) -> List[str]:
        """Return discriminative terms from observation-signal metadata."""
        if not isinstance(signals, list):
            return []
        structural_terms = {
            "signal", "metric", "label", "value", "threshold", "reason",
            "candidate_skill", "candidate_fault", "source", "observation",
            "llm", "vllm", "k8s", "sre",
        }
        terms: List[str] = []
        for item in signals:
            if not isinstance(item, dict):
                continue
            for key in ("signal", "metric", "label", "candidate_skill", "candidate_fault", "reason"):
                value = str(item.get(key) or "").strip().lower()
                if not value:
                    continue
                for token in self._tokens(value):
                    if token not in structural_terms and token not in terms:
                        terms.append(token)
        return terms

    def _tokens(self, text: str) -> List[str]:
        import re
        return re.findall(r"[a-zA-Z0-9_.:/-]{4,}|[\u4e00-\u9fff]{2,}", str(text or "").lower())

    def _json_preview(self, value: Any, limit: int = 2000) -> str:
        try:
            return __import__("json").dumps(value, ensure_ascii=False, default=str)[:limit]
        except Exception:
            return str(value)[:limit]

    def _clamp_float(self, value: Any, default: float = 0.0) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except Exception:
            return default

    def _run_kubectl(
        self,
        command: str,
        namespace: str,
        *,
        kubectl_available: bool,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = payload if isinstance(payload, dict) else {}
        record: Dict[str, Any] = {
            "tool": "kubectl",
            "command": command,
            "namespace": namespace,
            "cluster_id": payload.get("cluster_id") or payload.get("target_cluster_id") or "",
            "success": False,
            "executed": False,
            "source": "planned_kubectl",
            "summary": "",
        }
        try:
            readonly_kubectl_args(command, namespace)
        except ValueError as exc:
            record.update(error="tool_policy_denied", summary=str(exc))
            return record
        def captured_output(stdout):
            try:
                if projection_request(command, namespace) is not None:
                    data = json.dumps(decode_projection(command, namespace, stdout), ensure_ascii=False)
                    return data, evidence_sample(data)
            except (ValueError, TypeError, IndexError):
                record.update(success=False, error='invalid_service_projection',
                              summary='结构化取证输出不完整或身份不匹配，不能作为成功观测。')
                # Keep a useful failure sample without carrying malformed or
                # unprojected stdout into evidence/history.
                from shlex import split
                target = split(command)[1]
                return '', 'invalid_service_projection: '+target
            return stdout[:65536], stdout[:12000]
        if self.kubectl_runner:
            try:
                runner_result = self.kubectl_runner(command, namespace, payload)
            except Exception as exc:
                record["summary"] = f"集群感知 kubectl 执行失败: {exc}"
                record["error"] = str(exc)
                record["source"] = "cluster_kubectl"
                return record
            if isinstance(runner_result, dict):
                ok = (bool(runner_result.get("ok", runner_result.get("success", False)))
                      and runner_result.get('executed') is not False and not runner_result.get('outcome_unknown'))
                stdout = str(runner_result.get("stdout") or runner_result.get("data") or "")
                stderr = str(runner_result.get("stderr") or runner_result.get("error") or "")
                record.update({
                    "success": ok,
                    "executed": runner_result.get('executed') is not False,
                    "outcome_unknown": bool(runner_result.get('outcome_unknown')),
                    "source": "cluster_kubectl",
                    "actual_command": runner_result.get("command") or f"kubectl {command}",
                    "returncode": runner_result.get("returncode"),
                    "summary": (
                        f"kubectl {command} 在集群 {record['cluster_id'] or 'default'} 执行成功"
                        if ok
                        else f"kubectl {command} 在集群 {record['cluster_id'] or 'default'} 执行失败: {stderr or 'unknown error'}"
                    ),
                    "data": stdout[:65536],
                    "sample": (stdout or stderr)[:12000],
                })
                if ok:
                    record['data'], record['sample'] = captured_output(stdout)
                return record
        if not kubectl_available:
            record["summary"] = f"kubectl 工具不可用，已记录计划命令: kubectl {command} -n {namespace}"
            return record
        tool_result = self.registry.execute("kubectl", command=command, namespace=namespace, timeout=8)
        record["success"] = bool(tool_result.success)
        record["executed"] = True
        record["source"] = "kubectl"
        record["summary"] = (
            f"kubectl {command} 执行成功"
            if tool_result.success
            else f"kubectl {command} 执行失败: {tool_result.error or 'unknown error'}"
        )
        text = str(tool_result.data or tool_result.error or "")
        record["data"] = text[:65536]
        record["sample"] = text[:12000]
        if tool_result.success:
            record['data'], record['sample'] = captured_output(text)
        return record

    def _k8s_readonly_commands(self, payload: Dict[str, Any]) -> List[str]:
        service = str(payload.get("service") or payload.get("object") or "").strip()
        deployment = str(payload.get("deployment") or payload.get("root_cause_component") or "").strip()
        pod = str(payload.get("pod") or "").strip()
        commands = [
            "get pods,svc,endpoints -o wide",
            "get events --sort-by=.lastTimestamp",
        ]
        if service:
            commands.extend([
                f"describe svc {service}",
                f"get endpoints {service} -o wide",
            ])
        if deployment:
            commands.extend([
                f"describe deployment/{deployment}",
                f"get deployment/{deployment} -o json",
            ])
        if pod:
            commands.append(f"describe pod/{pod}")
        return commands

    def _commands_from_plan(self, plan: Dict[str, Any]) -> List[str]:
        commands: List[str] = []
        for check in plan.get("checks") or []:
            if not isinstance(check, dict):
                continue
            for command in check.get("commands") or []:
                if isinstance(command, str) and command not in commands:
                    commands.append(command)
        return commands

    def _hypothesis_for_fault(self, fault_type: str, *, target: str = "", model: str = "") -> Dict[str, Any]:
        target_text = target or "目标工作负载"
        model_text = f"模型 {model}" if model else "当前模型服务"
        known = {
            "service_unavailable": (
                f"{target_text} 后端不可用或 Endpoint 为空",
                0.82,
                ["Service endpoints 为空", "请求返回 503", "Pod 未就绪或标签不匹配"],
            ),
            "endpoint_timeout": (
                f"{model_text} 推理链路排队或端点超时",
                0.78,
                ["TTFT/E2E 延迟升高", "等待队列增加", "vLLM Pod 或 GPU 资源存在压力"],
            ),
            "vllm_process_crash": (
                "vLLM 进程或 Pod 重启导致服务不可用",
                0.8,
                ["Pod restart count 增加", "Service endpoint 消失", "请求失败率升高"],
            ),
            "gpu_oom": (
                "GPU 显存或 KV Cache 压力触发 OOM/退化",
                0.78,
                ["显存利用率接近阈值", "OOM 或 CUDA out of memory 日志", "请求排队增加"],
            ),
            "gpu_resource_pressure": (
                "GPU 资源竞争或后台负载导致推理性能下降",
                0.74,
                ["GPU 利用率异常", "运行中请求与 GPU 忙碌不匹配", "背景负载存在"],
            ),
            "resource_pressure": (
                "智算节点 CPU、内存、磁盘或网络资源压力影响推理服务",
                0.74,
                ["节点资源水位超过阈值", "Pod 调度或 IO/网络指标异常", "vLLM 请求时延或队列同步变化"],
            ),
            "observability_unavailable": (
                "Prometheus 或 exporter 采集链路不可用",
                0.76,
                ["Prometheus target down", "/metrics 连接失败", "观测数据长时间缺失"],
            ),
            "teller_request_latency_high": (
                "请求级 trace 显示推理执行路径耗时异常",
                0.83,
                ["request span 超过阈值", "NVTX/CUDA 阶段耗时集中", "同一请求存在端到端延迟"],
            ),
            "teller_kernel_bottleneck": (
                "CUDA Kernel 或算子阶段成为性能瓶颈",
                0.82,
                ["Kernel P95 超过阈值", "top kernels 耗时集中", "GPU 时间线存在长尾"],
            ),
        }
        description, confidence, expected = known.get(
            fault_type,
            (f"{target_text} 出现 {fault_type or '未知'} 类型异常，需要通过 Skill 调查确认", 0.68, ["告警上下文", "K8s 状态", "观测指标"]),
        )
        return {
            "id": "H1",
            "description": description,
            "confidence": confidence,
            "fault_type": fault_type,
            "expected_evidence": expected,
        }

    def _alternative_hypotheses(self, fault_type: str, event_type: str, *, target: str = "") -> List[Dict[str, Any]]:
        common = [
            {
                "id": "H2",
                "description": "观测链路或采集目标异常导致告警表现失真",
                "confidence": 0.42 if fault_type != "observability_unavailable" else 0.62,
                "fault_type": "observability_unavailable",
                "expected_evidence": ["Prometheus target 状态", "exporter /metrics 可达性", "最近采样时间"],
            },
            {
                "id": "H3",
                "description": "配置、调度或依赖变更造成服务侧传播故障",
                "confidence": 0.36,
                "fault_type": "dependency_or_config_change",
                "expected_evidence": ["K8s Event", "Deployment/Service 配置", "最近实验或变更记录"],
            },
        ]
        if str(event_type).startswith("teller_"):
            common[0] = {
                "id": "H2",
                "description": "请求级 trace 上下文缺失或采集不连续，导致瓶颈定位不完整",
                "confidence": 0.48,
                "fault_type": "teller_request_context_missing",
                "expected_evidence": ["request_id 覆盖率", "trace 最新更新时间", "NVTX/CUDA 事件完整性"],
            }
        if fault_type == "service_unavailable" and target:
            common[1]["description"] = f"{target} 的 selector、端口或 NetworkPolicy 配置异常"
            common[1]["confidence"] = 0.46
        return common
