"""Hermes Agent tools for discovering and invoking AgenticSRE skills."""

from __future__ import annotations

import json
import hashlib
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Iterable, List, Optional


SAFE_SKILL_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
DEFAULT_SKILL_RUNTIME_URL = "http://127.0.0.1:8090"
DEFAULT_STAGES = ["detect", "diagnose"]
VALID_STAGES = {"detect", "diagnose", "heal", "verify"}
HERMES_SKILL_TOOLSET = "agenticsre_skills"
HERMES_SKILL_TOOL_NAMES = (
    "agenticsre_skills_list",
    "agenticsre_skill_detail",
    "agenticsre_skill_run",
)
DYNAMIC_SKILL_TOOL_PREFIX = "agenticsre_skill_"
Transport = Callable[[str, str, Optional[Dict[str, Any]]], Dict[str, Any]]


def _bounded(value: Any, *, depth: int = 0) -> Any:
    """Keep tool responses useful without flooding the model context."""
    if depth >= 5:
        return str(value)[:500]
    if isinstance(value, dict):
        return {
            str(key): _bounded(item, depth=depth + 1)
            for key, item in list(value.items())[:40]
        }
    if isinstance(value, list):
        return [_bounded(item, depth=depth + 1) for item in value[:30]]
    if isinstance(value, tuple):
        return [_bounded(item, depth=depth + 1) for item in value[:30]]
    if isinstance(value, str):
        return value[:4000]
    return value


def _parse_tool_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def _bounded_tool_result(tool_name: str, value: Any) -> Any:
    if tool_name == "agenticsre_skills_list" and isinstance(value, dict):
        bounded = {
            str(key): _bounded(item)
            for key, item in list(value.items())[:40]
            if key != "skills"
        }
        bounded["skills"] = [
            _bounded(item)
            for item in (value.get("skills") or [])[:100]
        ]
        return bounded
    return _bounded(value)


def extract_hermes_tool_activity(messages: Iterable[Any]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Correlate Hermes assistant tool calls with subsequent tool results."""
    tool_calls: List[Dict[str, Any]] = []
    calls_by_id: Dict[str, Dict[str, Any]] = {}
    skill_results: List[Dict[str, Any]] = []

    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role == "assistant":
            for tool_call in message.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
                name = str(function.get("name") or "")
                call_id = str(tool_call.get("id") or "")
                raw_args = function.get("arguments") or ""
                args_text = (
                    json.dumps(raw_args, ensure_ascii=False, default=str)
                    if isinstance(raw_args, (dict, list))
                    else str(raw_args)
                )
                record = {
                    "id": call_id,
                    "tool": name,
                    "args": args_text[:1000],
                    "args_json": _bounded(_parse_tool_value(raw_args)),
                    "is_skill_tool": name in HERMES_SKILL_TOOL_NAMES or name.startswith(DYNAMIC_SKILL_TOOL_PREFIX),
                    "status": "called",
                }
                tool_calls.append(record)
                if call_id:
                    calls_by_id[call_id] = record
            continue

        if role != "tool":
            continue
        call_id = str(message.get("tool_call_id") or "")
        call = calls_by_id.get(call_id)
        if not call:
            continue
        parsed = _parse_tool_value(message.get("content"))
        is_error = isinstance(parsed, str) and any(
            marker in parsed.casefold() for marker in ("error", "failed", "cancelled", "skipped")
        )
        call["status"] = "error" if is_error else "completed"
        if not call.get("is_skill_tool"):
            continue
        result = _bounded_tool_result(str(call.get("tool") or ""), parsed)
        status = "error" if is_error else "completed"
        if isinstance(result, dict) and result.get("status"):
            status = str(result.get("status"))
        skill_results.append({
            "tool_call_id": call_id,
            "tool": call.get("tool") or "",
            "args": call.get("args_json") or {},
            "status": status,
            "result": result,
        })

    return tool_calls, skill_results


def _normalize_stages(stages: Optional[Iterable[str]]) -> List[str]:
    normalized: List[str] = []
    for stage in stages or DEFAULT_STAGES:
        value = str(stage or "").strip().lower()
        if value in VALID_STAGES and value not in normalized:
            normalized.append(value)
    return normalized or list(DEFAULT_STAGES)


def _require_skill_id(skill_id: str) -> str:
    value = str(skill_id or "").strip()
    if not value or not SAFE_SKILL_ID_RE.fullmatch(value):
        raise ValueError("skill_id may only contain letters, numbers, dot, underscore, and hyphen")
    return value


def skill_tool_name(skill_id: str) -> str:
    safe_id = _require_skill_id(skill_id)
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", safe_id).strip("_").lower()[:34] or "skill"
    suffix = hashlib.sha1(safe_id.encode("utf-8")).hexdigest()[:8]
    return f"{DYNAMIC_SKILL_TOOL_PREFIX}{slug}_{suffix}"


def _skill_summary(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": item.get("id") or "",
        "name": item.get("name") or item.get("id") or "",
        "domain": item.get("domain") or "generic",
        "description": str(item.get("description") or "")[:600],
        "stages": list(item.get("stages") or []),
        "risk": item.get("risk") or "medium",
        "requires_approval": bool(item.get("requires_approval", True)),
        "safe_auto_execute": bool(item.get("safe_auto_execute", False)),
        "tags": list(item.get("tags") or [])[:12],
        "maturity": (item.get("contract_status") or {}).get("maturity") or "",
    }


def _build_payload(
    query: str,
    *,
    namespace: str = "",
    domain: str = "",
    fault_type: str = "",
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload = dict(context or {})
    payload.update({
        "source": "hermes_agent",
        "invocation_mode": "hermes_skill_tool",
        "message": str(query or "").strip(),
    })
    if namespace:
        payload["namespace"] = str(namespace).strip()
    if domain:
        payload["domain"] = str(domain).strip()
    if fault_type:
        payload["fault_type"] = str(fault_type).strip()
    return payload


def _compact_direct_result(result: Dict[str, Any]) -> Dict[str, Any]:
    skill = result.get("skill") if isinstance(result.get("skill"), dict) else {}
    stages = result.get("stages") if isinstance(result.get("stages"), dict) else {}
    compact_stages: Dict[str, Any] = {}
    for stage, record in stages.items():
        if not isinstance(record, dict):
            continue
        compact_stages[stage] = {
            "status": record.get("status"),
            "confidence": record.get("confidence"),
            "summary": record.get("summary") or (record.get("output") or {}).get("summary"),
            "evidence": list(record.get("evidence") or [])[:10],
            "output": _bounded(record.get("output") or {}),
        }
    return {
        "status": result.get("status") or "completed",
        "dry_run": bool(result.get("dry_run", True)),
        "skill": _skill_summary(skill),
        "stages": compact_stages,
    }


def _compact_workflow_result(result: Dict[str, Any]) -> Dict[str, Any]:
    runtime = result.get("skill_runtime") if isinstance(result.get("skill_runtime"), dict) else {}
    runs = runtime.get("runs") if isinstance(runtime.get("runs"), list) else []
    matched_skills = []
    for run in runs[:20]:
        skill = run.get("skill") if isinstance(run, dict) and isinstance(run.get("skill"), dict) else {}
        if skill:
            matched_skills.append(_skill_summary(skill))
    diagnosis = result.get("diagnosis") if isinstance(result.get("diagnosis"), dict) else {}
    execution_plan = result.get("execution_plan") if isinstance(result.get("execution_plan"), dict) else {}
    verification_plan = result.get("verification_plan") if isinstance(result.get("verification_plan"), dict) else {}
    return {
        "status": result.get("status") or "completed",
        "domain": result.get("domain") or "generic",
        "dry_run": bool(result.get("dry_run", True)),
        "workflow_run_id": result.get("workflow_run_id") or "",
        "primary_skill_id": runtime.get("primary_skill_id") or "",
        "matched_count": runtime.get("matched_count") or len(matched_skills),
        "matched_skills": matched_skills,
        "diagnosis": _bounded({
            "fault_type": diagnosis.get("fault_type"),
            "root_cause": diagnosis.get("root_cause") or diagnosis.get("summary"),
            "root_cause_component": diagnosis.get("root_cause_component"),
            "confidence": diagnosis.get("confidence"),
            "evidence": list(diagnosis.get("evidence") or [])[:12],
            "source": diagnosis.get("source"),
        }),
        "execution_plan": _bounded(execution_plan),
        "verification_plan": _bounded(verification_plan),
    }


class HermesSkillRuntimeClient:
    """Small HTTP client used by Hermes tool handlers."""

    def __init__(
        self,
        base_url: str = "",
        *,
        timeout_s: int = 30,
        transport: Optional[Transport] = None,
    ) -> None:
        self.base_url = (
            base_url
            or os.environ.get("SKILL_RUNTIME_URL")
            or DEFAULT_SKILL_RUNTIME_URL
        ).rstrip("/")
        self.timeout_s = max(1, int(timeout_s))
        self.transport = transport

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if self.transport is not None:
            return self.transport(method, path, body)
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=raw,
            headers={"Content-Type": "application/json"} if raw is not None else {},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"Skill Runtime HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Skill Runtime unavailable at {self.base_url}: {exc.reason}") from exc

    def is_available(self) -> bool:
        try:
            return self._request("GET", "/api/health").get("status") == "ok"
        except Exception:
            return False

    def list_skills(
        self,
        *,
        query: str = "",
        domain: str = "",
        stage: str = "",
        limit: int = 30,
    ) -> Dict[str, Any]:
        response = self._request("GET", "/api/skills")
        all_skills = [item for item in response.get("skills") or [] if isinstance(item, dict)]
        query_text = str(query or "").strip().casefold()
        domain_text = str(domain or "").strip().casefold()
        stage_text = str(stage or "").strip().casefold()
        matched = []
        for item in all_skills:
            if domain_text and str(item.get("domain") or "").casefold() != domain_text:
                continue
            item_stages = [str(value).casefold() for value in item.get("stages") or []]
            if stage_text and stage_text not in item_stages:
                continue
            searchable = " ".join(str(item.get(key) or "") for key in ("id", "name", "description", "domain"))
            searchable += " " + " ".join(str(value) for value in item.get("tags") or [])
            if query_text and query_text not in searchable.casefold():
                continue
            matched.append(item)
        selected = matched[: max(1, min(int(limit or 30), 100))]
        return {
            "status": "ok",
            "runtime_skill_count": len(all_skills),
            "matched_count": len(matched),
            "returned_count": len(selected),
            "query": query or "",
            "domain": domain or "",
            "stage": stage or "",
            "skills": [_skill_summary(item) for item in selected],
            "hint": "Use agenticsre_skill_detail for instructions, then invoke exact ids with agenticsre_skill_run.",
        }

    def skill_detail(self, skill_id: str) -> Dict[str, Any]:
        safe_id = _require_skill_id(skill_id)
        response = self._request(
            "GET",
            f"/api/skills/{urllib.parse.quote(safe_id, safe='')}/detail",
        )
        metadata = response.get("metadata") if isinstance(response.get("metadata"), dict) else {}
        return {
            "status": "ok",
            "skill": _skill_summary({**metadata, "contract_status": response.get("contract_status") or {}}),
            "instructions": str(response.get("markdown") or "")[:16000],
            "source_status": _bounded(response.get("source_status") or {}),
            "contract_status": _bounded(response.get("contract_status") or {}),
        }

    def run_skill(
        self,
        skill_id: str,
        query: str,
        *,
        namespace: str = "",
        domain: str = "",
        fault_type: str = "",
        stages: Optional[Iterable[str]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        safe_id = _require_skill_id(skill_id)
        body = {
            "payload": _build_payload(
                query,
                namespace=namespace,
                domain=domain,
                fault_type=fault_type,
                context=context,
            ),
            "stages": _normalize_stages(stages),
            "dry_run": True,
        }
        response = self._request(
            "POST",
            f"/api/skills/{urllib.parse.quote(safe_id, safe='')}/run",
            body,
        )
        return _compact_direct_result(response)

    def run_workflow(
        self,
        query: str,
        *,
        namespace: str = "",
        domain: str = "",
        fault_type: str = "",
        stages: Optional[Iterable[str]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = _build_payload(
            query,
            namespace=namespace,
            domain=domain,
            fault_type=fault_type,
            context=context,
        )
        body = {
            "payload": payload,
            "domain": domain or payload.get("domain") or "",
            "stages": _normalize_stages(stages),
            "dry_run": True,
            "allow_legacy_fallback": False,
        }
        response = self._request("POST", "/api/workflows/operate", body)
        return _compact_workflow_result(response)


def _json_handler(callback: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Callable[..., str]:
    def handler(args: Dict[str, Any], **_kwargs: Any) -> str:
        return json.dumps(callback(args or {}), ensure_ascii=False, default=str)

    return handler


def register_hermes_skill_runtime_tools(
    base_url: str = "",
    *,
    client: Optional[HermesSkillRuntimeClient] = None,
    registry_obj: Any = None,
) -> List[str]:
    """Register AgenticSRE tools in Hermes' dynamic tool registry."""
    if registry_obj is None:
        from tools.registry import registry as registry_obj  # type: ignore

    runtime = client or HermesSkillRuntimeClient(base_url)
    toolset = HERMES_SKILL_TOOLSET
    definitions = [
        {
            "name": "agenticsre_skills_list",
            "description": (
                "List the live AgenticSRE Skill catalog. Use this before diagnosis or whenever "
                "the user asks which operational skills are available. Set limit=100 to list all skills."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Optional text filter across id, name, description, and tags."},
                    "domain": {"type": "string", "description": "Optional domain filter such as k8s, llm, generic, or tool."},
                    "stage": {"type": "string", "description": "Optional stage filter: detect, diagnose, heal, or verify."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
                },
            },
            "handler": _json_handler(lambda args: runtime.list_skills(
                query=args.get("query", ""),
                domain=args.get("domain", ""),
                stage=args.get("stage", ""),
                limit=args.get("limit", 30),
            )),
            "max_result_size_chars": 40000,
        },
        {
            "name": "agenticsre_skill_detail",
            "description": "Load the reviewed instructions, contract, risk, and stages for one AgenticSRE skill before invoking it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_id": {"type": "string", "description": "Exact skill id returned by agenticsre_skills_list."},
                },
                "required": ["skill_id"],
            },
            "handler": _json_handler(lambda args: runtime.skill_detail(args.get("skill_id", ""))),
            "max_result_size_chars": 24000,
        },
        {
            "name": "agenticsre_skill_run",
            "description": (
                "Run one exact AgenticSRE skill by id. Execution is always dry-run; heal stages only return guarded plans "
                "and never mutate the target environment."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_id": {"type": "string", "description": "Exact skill id."},
                    "query": {"type": "string", "description": "Incident description and observed symptoms."},
                    "namespace": {"type": "string", "description": "Kubernetes namespace when applicable."},
                    "domain": {"type": "string", "description": "Optional explicit domain."},
                    "fault_type": {"type": "string", "description": "Optional normalized or observed fault type."},
                    "stages": {
                        "type": "array",
                        "items": {"type": "string", "enum": sorted(VALID_STAGES)},
                        "description": "Stages to run; defaults to detect and diagnose.",
                    },
                    "context": {"type": "object", "description": "Optional structured metrics, topology, logs, or prior evidence."},
                },
                "required": ["skill_id", "query"],
            },
            "handler": _json_handler(lambda args: runtime.run_skill(
                args.get("skill_id", ""),
                args.get("query", ""),
                namespace=args.get("namespace", ""),
                domain=args.get("domain", ""),
                fault_type=args.get("fault_type", ""),
                stages=args.get("stages"),
                context=args.get("context") if isinstance(args.get("context"), dict) else None,
            )),
            "max_result_size_chars": 32000,
        },
    ]

    try:
        live_skills = runtime.list_skills(limit=100).get("skills") or []
    except Exception:
        live_skills = []
    for item in live_skills:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        skill_id = _require_skill_id(str(item.get("id")))
        tool_name = skill_tool_name(skill_id)

        def direct_handler(args: Dict[str, Any], *, selected_skill_id: str = skill_id) -> Dict[str, Any]:
            return runtime.run_skill(
                selected_skill_id,
                args.get("query", ""),
                namespace=args.get("namespace", ""),
                domain=args.get("domain", ""),
                fault_type=args.get("fault_type", ""),
                stages=args.get("stages"),
                context=args.get("context") if isinstance(args.get("context"), dict) else None,
            )

        definitions.append({
            "name": tool_name,
            "description": (
                f"Invoke exact AgenticSRE Skill {skill_id} ({item.get('name') or skill_id}). "
                f"{str(item.get('description') or '')[:500]} Execution is dry-run."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Incident description and observed symptoms."},
                    "namespace": {"type": "string", "description": "Kubernetes namespace when applicable."},
                    "domain": {"type": "string", "description": "Optional explicit domain."},
                    "fault_type": {"type": "string", "description": "Optional observed fault type."},
                    "stages": {
                        "type": "array",
                        "items": {"type": "string", "enum": sorted(VALID_STAGES)},
                        "description": "Stages to run; defaults to detect and diagnose.",
                    },
                    "context": {"type": "object", "description": "Optional structured metrics, topology, logs, or prior evidence."},
                },
                "required": ["query"],
            },
            "handler": _json_handler(direct_handler),
            "max_result_size_chars": 32000,
        })

    for definition in definitions:
        registry_obj.register(
            name=definition["name"],
            toolset=toolset,
            schema={
                "name": definition["name"],
                "description": definition["description"],
                "parameters": definition["parameters"],
            },
            handler=definition["handler"],
            check_fn=runtime.is_available,
            description=definition["description"],
            max_result_size_chars=definition["max_result_size_chars"],
        )
    names = [definition["name"] for definition in definitions]
    if tuple(names[:len(HERMES_SKILL_TOOL_NAMES)]) != HERMES_SKILL_TOOL_NAMES:
        raise RuntimeError("Hermes Skill tool definitions do not match the public tool contract")
    return names
