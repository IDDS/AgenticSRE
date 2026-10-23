"""Agent-facing bridge for invoking Skill Runtime during RCA."""

from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Any, Dict, Iterable, Optional

from skill_runtime.executor import SkillExecutor
from skill_runtime.fault_taxonomy import classify_domain, classify_fault_type
from skill_runtime.tool_context import ToolRunner
from skill_runtime.workflow import SkillWorkflow


def infer_skill_domain(query: str, namespace: str = "") -> str:
    return classify_domain(query, namespace)


def infer_skill_fault_type(query: str, domain: str = "") -> str:
    return classify_fault_type(query, domain)


def _structured_payload_from_evidence(evidence: Dict[str, Any]) -> Dict[str, Any]:
    direct = evidence.get("payload")
    if isinstance(direct, dict):
        return direct

    workflow_request = evidence.get("skill_workflow_request")
    if isinstance(workflow_request, dict) and isinstance(workflow_request.get("payload"), dict):
        return workflow_request["payload"]

    context = evidence.get("agent_skill_context")
    if isinstance(context, dict):
        context_payload = context.get("payload")
        if isinstance(context_payload, dict):
            nested = context_payload.get("payload")
            if isinstance(nested, dict):
                return nested
            return context_payload

    return {}


def _agent_evidence_summary(evidence: Dict[str, Any]) -> str:
    """Compact Agent-side reasoning and tool evidence for skill matching."""
    parts: list[str] = []
    direct = _structured_payload_from_evidence(evidence)
    for key in (
        "message", "alertname", "service", "deployment", "object", "target", "metric",
        "candidate_skills", "candidate_faults", "observation_signals", "background_observation_signals",
    ):
        value = direct.get(key) if isinstance(direct, dict) else None
        if value:
            parts.append(str(value))
    for item in (direct.get("evidence") if isinstance(direct, dict) else []) or []:
        if isinstance(item, dict):
            parts.extend(str(item.get(key) or "") for key in ("source", "signal", "value", "summary", "message"))
        elif item:
            parts.append(str(item))
    workflow = evidence.get("agent_workflow") if isinstance(evidence.get("agent_workflow"), dict) else {}
    for item in workflow.get("hypotheses") or []:
        if not isinstance(item, dict):
            continue
        for key in ("description", "fault_type", "service"):
            value = item.get(key)
            if value:
                parts.append(str(value))
        for key in ("expected_evidence", "supporting_evidence", "investigation_plan"):
            values = item.get(key) if isinstance(item.get(key), list) else []
            parts.extend(str(value) for value in values[:3] if value)
    plan = workflow.get("verification_plan") if isinstance(workflow.get("verification_plan"), dict) else {}
    for check in plan.get("checks") or []:
        if isinstance(check, dict):
            parts.append(str(check.get("purpose") or ""))
            parts.extend(str(cmd) for cmd in (check.get("commands") or [])[:3])
    collection = evidence.get("agent_evidence_collection") if isinstance(evidence.get("agent_evidence_collection"), dict) else {}
    for item in collection.get("commands") or []:
        if not isinstance(item, dict):
            continue
        parts.append(str(item.get("command") or ""))
        parts.append(str(item.get("summary") or ""))
        parts.append(str(item.get("sample") or "")[:800])
    summary = " ".join(part.strip() for part in parts if part and str(part).strip())
    return summary[:6000]


def _observed_evidence_summary(evidence: Dict[str, Any]) -> str:
    """Compact only observed facts, excluding LLM hypotheses."""
    parts: list[str] = []
    direct = _structured_payload_from_evidence(evidence)
    for key in (
        "message", "alertname", "service", "deployment", "object", "target", "metric",
        "raw_fault_type", "candidate_skills", "candidate_faults", "observation_signals", "background_observation_signals",
    ):
        value = direct.get(key) if isinstance(direct, dict) else None
        if value:
            parts.append(str(value))
    for item in (direct.get("evidence") if isinstance(direct, dict) else []) or []:
        if isinstance(item, dict):
            parts.extend(str(item.get(key) or "") for key in ("source", "signal", "value", "summary", "message"))
        elif item:
            parts.append(str(item))
    collection = evidence.get("agent_evidence_collection") if isinstance(evidence.get("agent_evidence_collection"), dict) else {}
    for item in collection.get("commands") or []:
        if not isinstance(item, dict):
            continue
        parts.append(str(item.get("command") or ""))
        parts.append(str(item.get("summary") or ""))
        parts.append(str(item.get("sample") or "")[:800])
    return " ".join(part.strip() for part in parts if part and str(part).strip())[:4000]


def build_skill_payload(query: str, namespace: str = "", evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    evidence = evidence or {}
    structured_payload = _structured_payload_from_evidence(evidence)
    if structured_payload.get('source') == 'alert_evidence_dossier':
        structured_payload = dict(structured_payload)
        if (structured_payload.get('namespace') == namespace
                and structured_payload.get('entity_kind') == 'pod'):
            pod = str(structured_payload.get('entity') or '')
            if re.fullmatch(r'[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?', pod):
                structured_payload['pod'] = pod
                structured_payload['object'] = 'pod/' + pod
    agent_summary = _agent_evidence_summary(evidence)
    observed_summary = _observed_evidence_summary(evidence)
    domain = str(structured_payload.get("domain") or infer_skill_domain(query, namespace))
    inferred_fault_type = infer_skill_fault_type(
        " ".join(part for part in (query or "", observed_summary) if part),
        domain,
    )
    structured_fault_type = str(structured_payload.get("fault_type") or "").strip()
    # A Job target is a scope, not proof of a Service failure. Keep all explicit
    # and specific mechanism routes intact; only replace the generic fallback.
    targets = set(re.findall(r'(?<![A-Za-z0-9_.-])job/([a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)(?=$|[\s的，。,:：])', query or '', re.I))
    job = str(structured_payload.get('job') or '')
    if not job and len(targets) == 1:
        job = next(iter(targets))
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', job):
        job = ''
    job_route = (domain == 'k8s' and job and not structured_fault_type
                 and inferred_fault_type == 'service_unavailable'
                 and not structured_payload.get('service') and not structured_payload.get('deployment')
                 and not any(marker in ((query or '') + ' ' + observed_summary).lower() for marker in (
                     'service_unavailable', 'serviceunavailable', 'service unavailable', '503',
                     'no endpoints', 'no ready endpoints', 'scaled to zero')))
    if job_route:
        inferred_fault_type = 'job_failure_investigation'
    payload = {
        "source": "agent_rca",
        "invocation_mode": "agent_tool_bridge",
        "domain": domain,
        "fault_type": structured_fault_type or inferred_fault_type,
        "inferred_fault_type": inferred_fault_type,
        "namespace": namespace or "default",
        "message": query or "",
        "agent_evidence_summary": agent_summary,
        "observed_evidence_summary": observed_summary,
        "agent_evidence": evidence,
    }
    protected_keys = {"source", "invocation_mode", "domain", "message", "namespace", "fault_type", "inferred_fault_type"}
    for key, value in structured_payload.items():
        if key in protected_keys:
            continue
        if value not in (None, "", [], {}):
            payload[key] = value
    payload["source"] = "agent_rca"
    payload["invocation_mode"] = "agent_tool_bridge"
    payload["domain"] = domain
    payload["message"] = query or payload.get("message") or ""
    payload["namespace"] = namespace or payload.get("namespace") or "default"
    if job:
        payload['job'] = job
    return payload


class AgentSkillBridge:
    """Small deterministic tool wrapper that RCA agents can call."""

    def __init__(
        self,
        *,
        skills_dir,
        runtime_url: str = "",
        timeout_s: int = 15,
        tool_runner: Optional[ToolRunner] = None,
    ) -> None:
        self.skills_dir = skills_dir
        self.runtime_url = (runtime_url or os.environ.get("SKILL_RUNTIME_URL", "http://127.0.0.1:8090")).rstrip("/")
        self.timeout_s = timeout_s
        self._local_workflow: Optional[SkillWorkflow] = None
        self.tool_runner = tool_runner

    def run(
        self,
        query: str,
        *,
        namespace: str = "",
        evidence: Optional[Dict[str, Any]] = None,
        stages: Optional[Iterable[str]] = None,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        payload = build_skill_payload(query, namespace, evidence)
        body = {
            "payload": payload,
            "domain": payload["domain"],
            "stages": list(stages or ["detect", "diagnose", "heal", "verify"]),
            "dry_run": dry_run,
            "allow_legacy_fallback": True,
        }
        if self.runtime_url and not self.tool_runner:
            try:
                return self._post(body)
            except Exception as exc:
                if not dry_run:
                    raise RuntimeError(
                        "Skill Runtime write outcome unknown; reconcile remote execution before retrying"
                    ) from exc
                result = self._local(body)
                result["agent_skill_bridge"] = {
                    "mode": "local_fallback",
                    "runtime_url": self.runtime_url,
                    "runtime_error": str(exc),
                }
                return result
        result = self._local(body)
        result["agent_skill_bridge"] = {"mode": "local"}
        return result

    def _post(self, body: Dict[str, Any]) -> Dict[str, Any]:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.runtime_url}/api/workflows/operate",
            data=raw,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        result["agent_skill_bridge"] = {"mode": "runtime_http", "runtime_url": self.runtime_url}
        return result

    def _local(self, body: Dict[str, Any]) -> Dict[str, Any]:
        if self._local_workflow is None:
            self._local_workflow = SkillWorkflow(SkillExecutor.from_dir(self.skills_dir))
        return self._local_workflow.run(
            body.get("payload") or {},
            domain=str(body.get("domain") or ""),
            stages=body.get("stages"),
            dry_run=bool(body.get("dry_run", True)),
            allow_legacy_fallback=bool(body.get("allow_legacy_fallback", True)),
            tool_runner=self.tool_runner,
        )


def summarize_skill_result(result: Dict[str, Any]) -> Dict[str, Any]:
    runtime = result.get("skill_runtime") or {}
    diagnosis = result.get("diagnosis") or {}
    plan = result.get("execution_plan") or {}
    verify = result.get("verification_plan") or {}
    return {
        "summary": (
            f"Agent 调用 Skill Runtime: 主 Skill={runtime.get('primary_skill_id') or '-'}, "
            f"命中 {runtime.get('matched_count', 0)} 个 Skill, "
            f"根因={diagnosis.get('root_cause') or diagnosis.get('summary') or '-'}, "
            f"置信度={diagnosis.get('confidence', 0)}, "
            f"自愈动作={plan.get('command_count', 0)}, 验证项={len(verify.get('checks') or [])}"
        ),
        "primary_skill_id": runtime.get("primary_skill_id") or "",
        "matched_count": runtime.get("matched_count", 0),
        "confidence": diagnosis.get("confidence", 0),
        "root_cause": diagnosis.get("root_cause") or diagnosis.get("summary") or "",
        "command_count": plan.get("command_count", 0),
        "verification_checks": len(verify.get("checks") or []),
        "bridge": result.get("agent_skill_bridge") or {},
    }
