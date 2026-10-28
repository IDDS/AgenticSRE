"""
AgenticSRE Web Dashboard
FastAPI-based single-page application with SSE streaming.
"""

import asyncio
import base64
import copy
import fcntl
import csv
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
import threading
import uuid
import urllib.request
import urllib.error
import urllib.parse
from collections import Counter, defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from orchestrator.confidence_calibrator import calibrate_llm_fault_confidence
from orchestrator.skill_agent import SkillAgent, SkillAgentRequest
from operational_ontology import (build_investigation, query_investigation, result_envelopes,
                                  action_context, command_targets_bound_object, preconditioned_action, ontology_schema)
from operational_lineage import collect_lineage, traceable_object
from skill_runtime.ontology_history import (build_object_history, eligible_object_ids, check_source_budget,
                                            HistoryBindingError, HistoryUnavailable)
from operational_provenance import compare_snapshots, latest_comparisons
from diagnostic_strategy import strategy_view
from integrations.aishperf import AISHPerfCatalog, build_aiops_eval_summary
from integrations.hermes_skill_runtime import HERMES_SKILL_TOOL_NAMES, HERMES_SKILL_TOOLSET, skill_tool_name
from skill_runtime.detail import skill_catalog_item_from_definition, skill_detail_from_definition
from skill_runtime.kubectl_safety import readonly_kubectl_args
from skill_runtime.runtime_evidence import cuda_runtime_evidence, has_abnormal_container_termination
from vllm_fault_injector.k8s_utils import build_label_selector
from confidence_statistics import clopper_pearson_lower_bound
from remediation_history import build_recovery_scope
from recovery_verification import (verify_business_recovery, verification_contract,
                                   valid_verification_fingerprint, VERIFICATION_CONTRACT_VERSION)
from web_app.fault_mode_registry import FaultModeRegistry, FaultModeRegistryError, FAMILY_META

# ── Setup Paths ──
APP_DIR = Path(__file__).parent
ROOT_DIR = APP_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

SCENARIOS_FILE = ROOT_DIR / "eval" / "fault_scenarios.yaml"
PRECISION_FAULT_SCENARIOS_FILE = ROOT_DIR / "configs" / "precision_fault_scenarios.yaml"
LLM_FAULT_SCENARIOS_OVERLAY_FILE = ROOT_DIR / "eval" / "llm_fault_scenarios.yaml"
HEAL_RECIPES_FILE = ROOT_DIR / "configs" / "heal_recipes.yaml"
HEAL_RECIPES_STATE = ROOT_DIR / "data" / "heal_recipe_library.json"
SKILL_RUNTIME_SKILLS_DIR = ROOT_DIR / "skill_runtime" / "skills"
SKILL_RUNTIME_URL = os.environ.get("SKILL_RUNTIME_URL", "http://127.0.0.1:8090").rstrip("/")
_SKILL_WORKFLOW: Optional[Any] = None


def _json_safe_snapshot(value: Any) -> Any:
    """Return a JSON-serializable snapshot of mutable runtime state."""
    try:
        encoded = jsonable_encoder(copy.deepcopy(value))
    except Exception:
        encoded = json.loads(json.dumps(value, ensure_ascii=False, default=str))

    def sanitize(item: Any) -> Any:
        if isinstance(item, float) and not math.isfinite(item):
            return None
        if isinstance(item, dict):
            return {key: sanitize(val) for key, val in item.items()}
        if isinstance(item, (list, tuple, set)):
            return [sanitize(val) for val in item]
        return item

    return sanitize(encoded)


class _SafeJSONResponse(JSONResponse):
    """Default API response that normalizes non-finite runtime metrics."""

    def render(self, content: Any) -> bytes:
        return super().render(_json_safe_snapshot(content))


async def _request_json_or_empty(request: Request) -> Dict[str, Any]:
    """Read optional JSON body; empty POSTs are valid for action endpoints."""
    try:
        body = await request.body()
    except Exception:
        return {}
    if not body:
        return {}
    try:
        parsed = json.loads(body.decode("utf-8"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _skill_workflow() -> Any:
    """Lazy local fallback for development; production should use 8090."""
    global _SKILL_WORKFLOW
    if _SKILL_WORKFLOW is None:
        from skill_runtime.executor import SkillExecutor
        from skill_runtime.workflow import SkillWorkflow

        _SKILL_WORKFLOW = SkillWorkflow(SkillExecutor.from_dir(SKILL_RUNTIME_SKILLS_DIR))
    return _SKILL_WORKFLOW


def _post_skill_runtime_workflow(body: Dict[str, Any]) -> Dict[str, Any]:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{SKILL_RUNTIME_URL}/api/workflows/operate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_skill_runtime_skills() -> Dict[str, Any]:
    request = urllib.request.Request(f"{SKILL_RUNTIME_URL}/api/skills", method="GET")
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_skill_runtime_json(path: str) -> Dict[str, Any]:
    request = urllib.request.Request(f"{SKILL_RUNTIME_URL}{path}", method="GET")
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def _local_skill_detail(skill_id: str) -> Dict[str, Any]:
    workflow = _skill_workflow()
    try:
        definition = workflow.executor.registry.get(skill_id)
    except KeyError as exc:
        raise HTTPException(404, f"Skill not found: {skill_id}") from exc
    try:
        return skill_detail_from_definition(definition)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))


def _default_llm_fault_injector_dir() -> Path:
    configured = os.environ.get("VLLM_FAULT_INJECTOR_DIR")
    if configured:
        return Path(configured)

    candidates = [
        ROOT_DIR / "vllm_fault_injector",
        ROOT_DIR.parent / "vllm_fault_injector",
        ROOT_DIR.parent.parent / "vllm_fault_injector",
        Path.home() / "vllm_fault_injector",
    ]
    for candidate in candidates:
        if (candidate / "scenarios.yaml").exists():
            return candidate
    return candidates[-1]


LLM_FAULT_INJECTOR_DIR = _default_llm_fault_injector_dir()
LLM_FAULT_SCENARIOS_FILE = LLM_FAULT_INJECTOR_DIR / "scenarios.yaml"
_AISHPERF_CATALOG: Optional[AISHPerfCatalog] = None
_AISHPERF_EVAL_RUNS_FILE = ROOT_DIR / "data" / "aishperf_eval_runs.json"
_AISHPERF_EVAL_ARTIFACT_DIR = ROOT_DIR / "data" / "aishperf_eval_runs"


def _aishperf_catalog() -> AISHPerfCatalog:
    global _AISHPERF_CATALOG
    if _AISHPERF_CATALOG is None:
        _AISHPERF_CATALOG = AISHPerfCatalog()
    return _AISHPERF_CATALOG


def _load_aishperf_eval_runs() -> List[Dict[str, Any]]:
    if not _AISHPERF_EVAL_RUNS_FILE.exists():
        return []
    try:
        data = json.loads(_AISHPERF_EVAL_RUNS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"Failed to load AISHPerf eval runs: {e}")
        return []


def _archive_aishperf_eval(summary: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    run_id = str(summary.get("run_id") or f"agenticsre-aiops-{uuid.uuid4().hex[:10]}")
    now = time.time()
    run_json = {
        "run_id": run_id,
        "suite_id": "agenticsre-aiops-eval",
        "source": summary.get("source") or "agenticsre.llm_faultlab",
        "created_at": now,
        "scenario": summary.get("scenario") or {},
        "target": {
            "cluster_id": body.get("cluster_id") or "",
            "namespace": body.get("namespace") or "",
            "deployment": body.get("deployment") or "",
            "host_id": body.get("host_id") or "",
            "target_mode": body.get("target_mode") or "",
        },
        "safety": summary.get("safety") or {},
    }
    artifact = {
        "run.json": run_json,
        "result.json": summary.get("result_json") or {},
        "metric_definitions.json": summary.get("metric_definitions") or {},
        "summary.json": summary,
        "inputs.json": body,
    }
    _AISHPERF_EVAL_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    artifact_dir = _AISHPERF_EVAL_ARTIFACT_DIR / run_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    for filename, payload in artifact.items():
        (artifact_dir / filename).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    record = {
        "id": run_id,
        "created_at": now,
        "status": (summary.get("result_json") or {}).get("status") or "unknown",
        "scenario": summary.get("scenario") or {},
        "metrics": summary.get("metrics") or {},
        "artifact_dir": str(artifact_dir),
    }
    runs = [r for r in _load_aishperf_eval_runs() if r.get("id") != run_id]
    runs.insert(0, record)
    runs = runs[:300]
    _AISHPERF_EVAL_RUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _AISHPERF_EVAL_RUNS_FILE.write_text(
        json.dumps(runs, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return {**record, "artifact": artifact}


_TELLER_EVENT_TYPE_ALIASES = {
    "HTTP": "HTTP",
    "HTTP_SERVER": "HTTP",
    "HTTP_INGRESS": "HTTP",
    "API": "HTTP",
    "API_SERVER": "HTTP",
    "NVTX_MARKER": "NVTX",
    "PYTORCH": "PYTORCH",
    "LIBTORCH": "LIBTORCH",
    "ENGINE": "ENGINE",
    "SCHEDULER": "SCHEDULER",
    "PREFILL": "PREFILL",
    "DECODE": "DECODE",
    "STEP": "STEP",
    "FRONTEND": "FRONTEND",
    "FE": "FRONTEND",
    "BACKEND": "BACKEND",
    "BE": "BACKEND",
    "RUNTIME": "RUNTIME",
    "RT": "RUNTIME",
    "DRIVER": "DRIVER",
    "KERNEL": "KERNEL",
    "K": "KERNEL",
    "HEARTBEAT": "HEARTBEAT",
}

_TELLER_REQUEST_IDS_RE = re.compile(r"request_ids:([^\]\s]+)")
_TELLER_TRACE_SUMMARY_CACHE: Dict[str, Dict[str, Any]] = {}
_TELLER_TRACE_SUMMARY_CACHE_TTL = 30
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _safe_configured_path(value: Any) -> str:
    """Return a configured local path without shell expansion."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    return os.path.expandvars(os.path.expanduser(raw))


def _resolved_path(value: Any) -> Optional[Path]:
    path = _safe_configured_path(value)
    if not path:
        return None
    try:
        return Path(path).resolve()
    except Exception:
        return None


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _allowed_teller_trace_roots(cluster: Optional[Dict[str, Any]] = None) -> List[Path]:
    candidates = [
        ROOT_DIR / "data" / "teller",
        ROOT_DIR / "data" / "teller" / "trace",
        (cluster or {}).get("teller_trace_dir"),
        (cluster or {}).get("teller_processed_dir"),
        os.environ.get("TELLER_TRACE_DIR"),
        os.environ.get("TRUFFLD_DATA_DIR"),
    ]
    roots: List[Path] = []
    seen = set()
    for candidate in candidates:
        resolved = _resolved_path(candidate)
        if not resolved:
            continue
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            roots.append(resolved)
    return roots


def _validate_teller_trace_dir(path: str, cluster: Optional[Dict[str, Any]] = None) -> str:
    resolved = _resolved_path(path)
    if not resolved:
        raise HTTPException(400, "TELLER trace directory is not configured")
    roots = _allowed_teller_trace_roots(cluster)
    if not any(resolved == root or _path_is_relative_to(resolved, root) for root in roots):
        allowed = ", ".join(str(root) for root in roots) or str(ROOT_DIR / "data" / "teller")
        raise HTTPException(400, f"TELLER trace directory must be under configured allowed roots: {allowed}")
    return str(resolved)


def _cluster_teller_trace_dir(cluster: Optional[Dict[str, Any]] = None, explicit: str = "") -> str:
    """Trace root for fine-grained TELLER/NVTX/CUPTI observability."""
    selected = (
        _safe_configured_path(explicit)
        or _safe_configured_path((cluster or {}).get("teller_trace_dir"))
        or _safe_configured_path((cluster or {}).get("teller_processed_dir"))
        or _safe_configured_path(os.environ.get("TELLER_TRACE_DIR"))
        or _safe_configured_path(os.environ.get("TRUFFLD_DATA_DIR"))
        or str(ROOT_DIR / "data" / "teller" / "trace")
    )
    return _validate_teller_trace_dir(selected, cluster)


def _split_teller_request_ids(raw: Any) -> List[str]:
    """Split TELLER request id lists, including compact vLLM batch strings."""
    raw = str(raw or "").strip()
    if not raw or raw.lower() == "unknown":
        return []
    if not any(sep in raw for sep in (";", ",", "|")):
        # TELLER's vLLM hook currently joins multiple request_ids with "_".
        # Keep common single ids such as chatcmpl_abc_123 intact, but split
        # compact synthetic ids used by the hook, e.g. req-a_req-b.
        compact_parts = re.findall(r"(?:req|request|cmpl|chatcmpl)[-_][A-Za-z0-9.-]+", raw)
        if len(compact_parts) > 1 and "_".join(compact_parts) == raw:
            return compact_parts[:64]
    return [
        item.strip()
        for item in re.split(r"[;,|]+", raw)
        if item.strip() and item.strip().lower() != "unknown"
    ][:64]


def _extract_teller_request_ids(name: Any) -> List[str]:
    """Extract request ids embedded by TELLER's vLLM NVTX hook."""
    text = str(name or "")
    match = _TELLER_REQUEST_IDS_RE.search(text)
    if not match:
        return []
    return _split_teller_request_ids(match.group(1))


def _clean_teller_event_name(name: Any) -> str:
    text = str(name or "").strip()
    if "step_execute_model[" in text:
        return "step_execute_model"
    return text


def _normalize_teller_event(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize TELLER compact/raw JSONL events into a common event schema."""
    if not isinstance(raw, dict):
        return {}
    out = dict(raw)
    short_map = {
        "t": "type",
        "n": "name",
        "ts": "timestamp",
        "gs": "gpu_start",
        "ge": "gpu_end",
        "s": "start",
        "e": "end",
        "d": "duration",
        "c": "correlation_id",
        "pid": "process_id",
        "tid": "thread_id",
    }
    for src, dst in short_map.items():
        if src in out and dst not in out:
            out[dst] = out.pop(src)

    raw_type = str(out.get("type") or "").strip().upper()
    event_type = _TELLER_EVENT_TYPE_ALIASES.get(raw_type, raw_type or "UNKNOWN")
    raw_name = str(out.get("name") or event_type.lower()).strip() or event_type.lower()
    request_ids = _extract_teller_request_ids(raw_name)
    explicit_request_ids = out.get("request_ids")
    if isinstance(explicit_request_ids, str):
        request_ids.extend(_split_teller_request_ids(explicit_request_ids))
    elif isinstance(explicit_request_ids, list):
        for item in explicit_request_ids:
            request_ids.extend(_split_teller_request_ids(item))
    explicit_request_id = str(out.get("request_id") or "").strip()
    if explicit_request_id and explicit_request_id.lower() != "unknown":
        request_ids.append(explicit_request_id)
    request_ids = list(dict.fromkeys(request_ids))[:64]
    name = _clean_teller_event_name(raw_name)
    start = out.get("start")
    if start is None:
        start = out.get("start_ns")
    if start is None and out.get("start_us") is not None:
        start = float(out.get("start_us") or 0) * 1000
    if start is None and out.get("start_ms") is not None:
        start = float(out.get("start_ms") or 0) * 1_000_000
    duration = out.get("dur", out.get("duration"))
    if duration is None:
        duration = out.get("duration_ns")
    if duration is None and out.get("duration_us") is not None:
        duration = float(out.get("duration_us") or 0) * 1000
    if duration is None and out.get("duration_ms") is not None:
        duration = float(out.get("duration_ms") or 0) * 1_000_000
    if event_type == "KERNEL":
        start = out.get("gpu_start", start)
        gpu_end = out.get("gpu_end")
        if duration is None and start is not None and gpu_end is not None:
            duration = float(gpu_end) - float(start)
    elif event_type in {"NVTX", "ENGINE", "PYTORCH", "LIBTORCH"}:
        start = out.get("timestamp", start)
    else:
        end = out.get("end")
        if duration is None and start is not None and end is not None:
            duration = float(end) - float(start)
    try:
        start_num = int(float(start or 0))
    except Exception:
        start_num = 0
    try:
        duration_num = max(0, int(float(duration or 0)))
    except Exception:
        duration_num = 0
    normalized = {
        "name": name[:180],
        "type": event_type,
        "start": start_num,
        "duration_ns": duration_num,
        "process_id": out.get("process_id") or 0,
        "thread_id": out.get("thread_id") or 0,
        "correlation_id": out.get("correlation_id"),
    }
    for field in ("method", "route", "path", "status_code", "model", "endpoint"):
        if out.get(field) is not None:
            normalized[field] = out.get(field)
    if request_ids:
        normalized["request_ids"] = request_ids
        normalized["request_id"] = request_ids[0]
        normalized["context_level"] = "request"
    if out.get("rank") is not None:
        normalized["rank"] = out.get("rank")
    return normalized


def _event_sort_time(ev: Dict[str, Any]) -> float:
    if ev.get("timestamp") is not None:
        return float(ev.get("timestamp") or 0)
    if ev.get("gpu_start") is not None:
        return float(ev.get("gpu_start") or 0)
    return float(ev.get("start") or 0)


def _expand_teller_raw_events(raw_events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert raw TELLER marker/API/kernel events into compact range events."""
    normalized = []
    for raw in raw_events:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        for src, dst in {
            "t": "type",
            "n": "name",
            "ts": "timestamp",
            "gs": "gpu_start",
            "ge": "gpu_end",
            "s": "start",
            "e": "end",
            "d": "duration",
            "c": "correlation_id",
            "pid": "process_id",
            "tid": "thread_id",
        }.items():
            if src in item and dst not in item:
                item[dst] = item.pop(src)
        normalized.append(item)

    compact: List[Dict[str, Any]] = []
    nvtx_stacks: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    for ev in sorted(normalized, key=_event_sort_time):
        event_type = str(ev.get("type") or "").strip().upper()
        if event_type == "NVTX_MARKER":
            name = ev.get("name")
            marker_id = ev.get("id")
            process_id = ev.get("process_id") or 0
            timestamp = ev.get("timestamp")
            if timestamp is None:
                continue
            if name in (None, "", "null", "[range_end]"):
                if marker_id is None:
                    continue
                stack = nvtx_stacks[process_id]
                for idx in range(len(stack) - 1, -1, -1):
                    if stack[idx].get("id") == marker_id:
                        start_ev = stack.pop(idx)
                        compact.append(_normalize_teller_event({
                            "type": "NVTX",
                            "name": start_ev.get("name") or "nvtx",
                            "start": start_ev.get("timestamp") or timestamp,
                            "duration": max(0, float(timestamp) - float(start_ev.get("timestamp") or timestamp)),
                            "process_id": process_id,
                            "thread_id": start_ev.get("thread_id") or ev.get("thread_id") or 0,
                            "correlation_id": ev.get("correlation_id"),
                        }))
                        break
                continue
            if marker_id is not None:
                nvtx_stacks[process_id].append(ev)
            else:
                compact.append(_normalize_teller_event(ev))
            continue
        compact.append(_normalize_teller_event(ev))
    return [ev for ev in compact if ev]


def _expand_chrome_trace_events(trace_events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert PyTorch/CUPTI Chrome trace events to TELLER-compatible ranges."""
    compact: List[Dict[str, Any]] = []
    for ev in trace_events:
        if not isinstance(ev, dict) or ev.get("ph") != "X":
            continue
        name = str(ev.get("name") or "").strip()
        cat = str(ev.get("cat") or "").lower()
        if not name:
            continue
        if "kernel" in cat or "gpu" in cat or "cuda_gpu" in cat:
            event_type = "KERNEL"
        elif "runtime" in cat or "cuda_runtime" in cat:
            event_type = "RUNTIME"
        elif "driver" in cat or "cuda_driver" in cat:
            event_type = "DRIVER"
        elif "user_annotation" in cat or "nvtx" in cat or "record_function" in cat:
            event_type = "NVTX"
        else:
            continue
        ts_ns = int(float(ev.get("ts") or 0) * 1000)
        dur_ns = int(float(ev.get("dur") or 0) * 1000)
        compact.append(_normalize_teller_event({
            "type": event_type,
            "name": name,
            "start": ts_ns,
            "duration": dur_ns,
            "process_id": ev.get("pid") or 0,
            "thread_id": ev.get("tid") or 0,
        }))
    return compact


def _teller_node_duration_ns(node: Dict[str, Any]) -> int:
    """Duration in ns following TELLER trace_parser's start/end or gpu_start/duration convention."""
    try:
        if node.get("start") is not None and node.get("end") is not None:
            return max(0, int(float(node.get("end") or 0) - float(node.get("start") or 0)))
        if node.get("gpu_start") is not None and node.get("duration") is not None:
            return max(0, int(float(node.get("duration") or 0)))
        if node.get("duration") is not None:
            return max(0, int(float(node.get("duration") or 0)))
    except Exception:
        return 0
    return 0


def _teller_node_start_ns(node: Dict[str, Any], fallback: int = 0) -> int:
    try:
        value = node.get("start")
        if value is None:
            value = node.get("gpu_start")
        return int(float(value if value is not None else fallback))
    except Exception:
        return fallback


def _append_teller_step_event(
    out: List[Dict[str, Any]],
    *,
    node: Dict[str, Any],
    event_type: str,
    name: str,
    depth: int,
    parent_idx: int,
    fallback_start: int,
    path: str,
    step_index: int,
) -> int:
    raw_name = str(name or event_type.lower()).strip() or event_type.lower()
    normalized = _normalize_teller_event({
        "type": event_type,
        "name": raw_name,
        "start": _teller_node_start_ns(node, fallback_start),
        "duration": _teller_node_duration_ns(node),
        "process_id": node.get("process_id") or node.get("pid") or step_index,
        "thread_id": node.get("thread_id") or node.get("tid") or depth,
        "correlation_id": node.get("correlation_id"),
    })
    if normalized:
        normalized["depth"] = depth
        normalized["parent_idx"] = parent_idx
        normalized["path"] = path
        normalized["step_index"] = step_index
        normalized["teller_hierarchy"] = True
        out.append(normalized)
        return len(out) - 1
    return parent_idx


def _expand_teller_step_tree(steps: List[Dict[str, Any]], max_events: int) -> List[Dict[str, Any]]:
    """Flatten TELLER trace.json steps into a tree-aware event list.

    This mirrors TELLER's trace_parser.py structure:
    step -> torch_frontend_ops -> torch_backend_ops -> runtime/driver_calls -> kernels.
    """
    out: List[Dict[str, Any]] = []
    for step_index, step in enumerate(steps):
        if len(out) >= max_events or not isinstance(step, dict):
            break
        step_name = step.get("step_name") or step.get("name") or f"step_{step_index}"
        step_start = _teller_node_start_ns(step)
        step_idx = _append_teller_step_event(
            out,
            node=step,
            event_type="STEP",
            name=step_name,
            depth=0,
            parent_idx=-1,
            fallback_start=step_start,
            path=str(step_name),
            step_index=step_index,
        )
        for fe in step.get("torch_frontend_ops") or []:
            if len(out) >= max_events or not isinstance(fe, dict):
                break
            fe_name = fe.get("op_name") or fe.get("name") or "frontend_op"
            fe_idx = _append_teller_step_event(
                out,
                node=fe,
                event_type="FRONTEND",
                name=fe_name,
                depth=1,
                parent_idx=step_idx,
                fallback_start=step_start,
                path=f"{step_name} > {fe_name}",
                step_index=step_index,
            )
            for be in fe.get("torch_backend_ops") or []:
                if len(out) >= max_events or not isinstance(be, dict):
                    break
                be_name = be.get("op_name") or be.get("name") or "backend_op"
                be_idx = _append_teller_step_event(
                    out,
                    node=be,
                    event_type="BACKEND",
                    name=be_name,
                    depth=2,
                    parent_idx=fe_idx,
                    fallback_start=_teller_node_start_ns(fe, step_start),
                    path=f"{step_name} > {fe_name} > {be_name}",
                    step_index=step_index,
                )
                for rt in be.get("runtime_calls") or []:
                    if len(out) >= max_events or not isinstance(rt, dict):
                        break
                    rt_name = rt.get("name") or "runtime_call"
                    rt_idx = _append_teller_step_event(
                        out,
                        node=rt,
                        event_type="RUNTIME",
                        name=rt_name,
                        depth=3,
                        parent_idx=be_idx,
                        fallback_start=_teller_node_start_ns(be, step_start),
                        path=f"{step_name} > {fe_name} > {be_name} > {rt_name}",
                        step_index=step_index,
                    )
                    for kernel in rt.get("kernels") or []:
                        if len(out) >= max_events or not isinstance(kernel, dict):
                            break
                        kernel_name = kernel.get("name") or "kernel"
                        _append_teller_step_event(
                            out,
                            node=kernel,
                            event_type="KERNEL",
                            name=kernel_name,
                            depth=4,
                            parent_idx=rt_idx,
                            fallback_start=_teller_node_start_ns(rt, step_start),
                            path=f"{step_name} > {fe_name} > {be_name} > {rt_name} > {kernel_name}",
                            step_index=step_index,
                        )
                for dr in be.get("driver_calls") or []:
                    if len(out) >= max_events or not isinstance(dr, dict):
                        break
                    dr_name = dr.get("name") or "driver_call"
                    dr_idx = _append_teller_step_event(
                        out,
                        node=dr,
                        event_type="DRIVER",
                        name=dr_name,
                        depth=3,
                        parent_idx=be_idx,
                        fallback_start=_teller_node_start_ns(be, step_start),
                        path=f"{step_name} > {fe_name} > {be_name} > {dr_name}",
                        step_index=step_index,
                    )
                    for kernel in dr.get("kernels") or []:
                        if len(out) >= max_events or not isinstance(kernel, dict):
                            break
                        kernel_name = kernel.get("name") or "kernel"
                        _append_teller_step_event(
                            out,
                            node=kernel,
                            event_type="KERNEL",
                            name=kernel_name,
                            depth=4,
                            parent_idx=dr_idx,
                            fallback_start=_teller_node_start_ns(dr, step_start),
                            path=f"{step_name} > {fe_name} > {be_name} > {dr_name} > {kernel_name}",
                            step_index=step_index,
                        )
    return out[:max_events]


def _find_teller_event_files(trace_dir: str, limit: int = 80, *, run_prefix: str = "") -> List[Path]:
    root = Path(trace_dir)
    if not root.exists() or not root.is_dir():
        return []
    search_roots = [root]
    if run_prefix:
        matched_roots = [
            p
            for p in root.glob("**/")
            if p.is_dir()
            and p.name.startswith(run_prefix)
            and any(p.glob("*.jsonl"))
        ]
        if matched_roots:
            search_roots = matched_roots
    candidates: List[Path] = []
    for base in search_roots:
        for pattern in ("**/*.jsonl", "**/*trace*.json", "**/*event*.json", "**/*.csv"):
            candidates.extend(p for p in base.glob(pattern) if p.is_file())
    unique = {str(p.resolve()): p for p in candidates}

    def file_group(path: Path) -> str:
        name = path.name.lower()
        if name.startswith("output_pid") or name.endswith(".tmp.jsonl"):
            return "cupti"
        if name.startswith("request_markers"):
            return "request"
        if name.startswith("agenticsre_pytorch") or name.startswith("agenticsre_http"):
            return "framework"
        if name.startswith("agenticsre_heartbeat"):
            return "heartbeat"
        if name.startswith("agenticsre_"):
            return "agenticsre"
        if "trace" in name or "event" in name:
            return "trace"
        return "other"

    def file_priority(path: Path) -> tuple:
        priority = {
            "request": 0,
            "cupti": 1,
            "framework": 2,
            "trace": 3,
            "agenticsre": 4,
            "other": 5,
            "heartbeat": 6,
        }.get(file_group(path), 9)
        return (priority, -path.stat().st_mtime, str(path))

    grouped: Dict[str, List[Path]] = defaultdict(list)
    for path in sorted(unique.values(), key=file_priority):
        grouped[file_group(path)].append(path)

    selected: List[Path] = []
    group_caps = {
        "request": max(4, limit // 4),
        "cupti": max(4, limit // 4),
        "framework": max(4, limit // 4),
        "trace": max(2, limit // 8),
        "agenticsre": max(2, limit // 10),
        "other": max(2, limit // 10),
        "heartbeat": max(1, limit // 12),
    }
    for group in ("request", "cupti", "framework", "trace", "agenticsre", "other", "heartbeat"):
        selected.extend(grouped.get(group, [])[:group_caps[group]])
    if len(selected) < limit:
        seen = {str(p.resolve()) for p in selected}
        for path in sorted(unique.values(), key=file_priority):
            key = str(path.resolve())
            if key in seen:
                continue
            selected.append(path)
            seen.add(key)
            if len(selected) >= limit:
                break
    return selected[:limit]


def _teller_trace_signature(files: List[Path]) -> str:
    parts: List[str] = []
    for path in files:
        try:
            stat = path.stat()
            parts.append(f"{path.resolve()}:{stat.st_mtime_ns}:{stat.st_size}")
        except OSError:
            parts.append(f"{path}:missing")
    return "|".join(parts)


def _read_teller_events_from_file(path: Path, max_events: int) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    try:
        raw_events: List[Dict[str, Any]] = []
        suffix = path.suffix.lower()
        if suffix == ".csv":
            with path.open("r", encoding="utf-8", errors="ignore", newline="") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    if len(raw_events) >= max_events:
                        break
                    lowered = {str(k or "").strip().lower(): v for k, v in row.items()}
                    name = (
                        lowered.get("name")
                        or lowered.get("kernel")
                        or lowered.get("kernel_name")
                        or lowered.get("event")
                        or lowered.get("operation")
                        or lowered.get("range")
                    )
                    raw_type = lowered.get("type") or lowered.get("category") or lowered.get("cat") or ""
                    if not raw_type:
                        name_l = str(name or "").lower()
                        raw_type = "KERNEL" if "kernel" in name_l or "gemm" in name_l or "cuda" in name_l else "NVTX"
                    item: Dict[str, Any] = {
                        "type": raw_type,
                        "name": name or raw_type,
                        "process_id": lowered.get("pid") or lowered.get("process_id") or 0,
                        "thread_id": lowered.get("tid") or lowered.get("thread_id") or 0,
                    }
                    for key in ("start", "start_ns", "start_us", "start_ms", "duration", "duration_ns", "duration_us", "duration_ms"):
                        if lowered.get(key) not in (None, ""):
                            item[key] = lowered.get(key)
                    raw_events.append(item)
        elif suffix == ".jsonl":
            with path.open("r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if len(raw_events) >= max_events:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw_events.append(json.loads(line))
                    except Exception:
                        continue
        else:
            payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(payload, dict) and isinstance(payload.get("traceEvents"), list):
                return _expand_chrome_trace_events(payload.get("traceEvents") or [])[:max_events]
            if isinstance(payload, dict) and isinstance(payload.get("steps"), list):
                return _expand_teller_step_tree(payload.get("steps") or [], max_events)
            if isinstance(payload, dict) and isinstance(payload.get("trace"), list):
                raw_events = payload.get("trace") or []
            else:
                raw_events = payload.get("events") if isinstance(payload, dict) else payload
            if isinstance(raw_events, list):
                raw_events = raw_events[:max_events]
            else:
                raw_events = []
        events = _expand_teller_raw_events(raw_events)[:max_events]
    except Exception as exc:
        logger.debug("Failed to read TELLER events from %s: %s", path, exc)
    return events


def _attach_teller_request_windows(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Attach request ids to CUDA events that fall inside request-level NVTX windows."""
    windows = []
    for ev in events:
        request_ids = ev.get("request_ids") or []
        duration = int(ev.get("duration_ns") or 0)
        if not request_ids or duration <= 0:
            continue
        start = int(ev.get("start") or 0)
        windows.append({
            "request_ids": [str(r) for r in request_ids if str(r or "").strip()],
            "start": start,
            "end": start + duration,
            "process_id": ev.get("process_id") or 0,
            "thread_id": ev.get("thread_id") or 0,
            "source_file": ev.get("source_file") or "",
            "source_dir": ev.get("source_dir") or "",
        })
    if not windows:
        return events

    for ev in events:
        if ev.get("request_ids"):
            continue
        start = int(ev.get("start") or 0)
        end = start + int(ev.get("duration_ns") or 0)
        if end <= 0:
            continue
        pid = ev.get("process_id") or 0
        source_file = ev.get("source_file") or ""
        source_dir = ev.get("source_dir") or ""
        matched_ids: List[str] = []
        for window in windows:
            same_source_dir = source_dir and window.get("source_dir") and source_dir == window.get("source_dir")
            if not same_source_dir and source_file and window.get("source_file") and source_file != window.get("source_file"):
                continue
            if window.get("process_id") not in (0, pid) and pid not in (0, window.get("process_id")):
                continue
            overlap = start < int(window["end"]) and end > int(window["start"])
            if overlap:
                matched_ids.extend(window["request_ids"])
        if matched_ids:
            unique = sorted(set(matched_ids))
            ev["request_ids"] = unique
            ev["request_id"] = unique[0]
            ev["context_level"] = "request_window"
    return events


def _base_teller_request_id(request_id: Any) -> str:
    text = str(request_id or "").strip()
    if not text:
        return ""
    generated_completion_id = False
    if text.startswith("cmpl-"):
        text = text[5:]
        generated_completion_id = True
    if text.startswith("chatcmpl-"):
        text = text[9:]
        generated_completion_id = True
    if generated_completion_id:
        text = re.sub(r"-(?:\d+)$", "", text)
    return text


def _merge_teller_http_request_aliases(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Map HTTP request ids to the vLLM-generated completion ids for one request path.

    vLLM often transforms a caller-provided x-request-id into a completion id such
    as cmpl-<request-id>-0. Keeping both ids makes the dashboard show two partial
    requests. This pass records HTTP ids as aliases of the richer vLLM id when the
    normalized base id matches, then the dashboard can render one real request path.
    """
    canonical_by_base: Dict[str, str] = {}
    for ev in events:
        if ev.get("type") == "HTTP":
            continue
        for rid in ev.get("request_ids") or []:
            rid_text = str(rid or "").strip()
            base = _base_teller_request_id(rid_text)
            if base and rid_text:
                canonical_by_base.setdefault(base, rid_text)
    if not canonical_by_base:
        return events

    for ev in events:
        if ev.get("type") != "HTTP":
            continue
        ids = [str(rid or "").strip() for rid in ev.get("request_ids") or [] if str(rid or "").strip()]
        merged_ids: List[str] = []
        aliases: List[str] = []
        for rid in ids:
            canonical = canonical_by_base.get(_base_teller_request_id(rid))
            target_id = canonical or rid
            if target_id not in merged_ids:
                merged_ids.append(target_id)
            if canonical and canonical != rid:
                aliases.append(rid)
        if merged_ids:
            ev["request_ids"] = merged_ids[:64]
            ev["request_id"] = merged_ids[0]
            if aliases:
                ev["request_aliases"] = aliases[:64]
                ev["request_alias_of"] = merged_ids[0]
            ev["context_level"] = "request_alias"
    return events


def _build_teller_e2e_trace(
    request_context: Dict[str, Any],
    type_stats: List[Dict[str, Any]],
    summary: Dict[str, Any],
) -> Dict[str, Any]:
    """Normalize TELLER request events into a stable end-to-end trace schema."""
    requests = request_context.get("requests") if isinstance(request_context.get("requests"), list) else []
    type_totals = {
        str(row.get("type") or ""): float(row.get("total_ms") or 0)
        for row in type_stats
        if isinstance(row, dict)
    }

    def stage_status(stage_type: str, request: Dict[str, Any]) -> str:
        if not request:
            return "missing"
        return "observed" if any(stage.get("type") == stage_type for stage in request.get("stages") or []) else "missing"

    def stage_duration(stage_type: str, request: Dict[str, Any]) -> float:
        for stage in request.get("stages") or []:
            if stage.get("type") == stage_type:
                return round(float(stage.get("total_ms") or 0), 3)
        return 0.0

    def stage_duration_any(stage_types: set, request: Dict[str, Any]) -> float:
        return round(sum(stage_duration(stage_type, request) for stage_type in stage_types), 3)

    def has_stage(stage_types: set, request: Dict[str, Any]) -> bool:
        return any(stage.get("type") in stage_types for stage in request.get("stages") or [])

    normalized_requests: List[Dict[str, Any]] = []
    for req in requests[:20]:
        request_id = str(req.get("request_id") or "")
        nodes = [
            {
                "id": "http_ingress",
                "label": "HTTP 入站",
                "layer": "request",
                "status": "observed" if has_stage({"HTTP"}, req) else "missing",
                "duration_ms": stage_duration("HTTP", req) if has_stage({"HTTP"}, req) else None,
                "evidence": (
                    "来自 AgenticSRE TELLER HTTP middleware span，表示请求已进入 vLLM OpenAI API。"
                    if has_stage({"HTTP"}, req) else
                    "当前未接入 HTTP/API server span，需补充 vLLM OpenAI API 入口埋点。"
                ),
            },
            {
                "id": "vllm_engine",
                "label": "vLLM Engine / Scheduler",
                "layer": "vllm",
                "status": "observed" if has_stage({"ENGINE", "SCHEDULER", "PREFILL", "DECODE", "NVTX"}, req) else "missing",
                "duration_ms": stage_duration_any({"ENGINE", "SCHEDULER", "PREFILL", "DECODE", "NVTX"}, req),
                "evidence": "来自 step_execute_model / worker_execute_model NVTX、ENGINE、SCHEDULER、PREFILL 或 DECODE 事件。",
            },
            {
                "id": "pytorch_frontend",
                "label": "PyTorch / 模型前端",
                "layer": "framework",
                "status": "observed" if any(s.get("type") in {"PYTORCH", "LIBTORCH", "FRONTEND", "BACKEND"} for s in req.get("stages") or []) else "missing",
                "duration_ms": round(
                    stage_duration("PYTORCH", req)
                    + stage_duration("LIBTORCH", req)
                    + stage_duration("FRONTEND", req)
                    + stage_duration("BACKEND", req),
                    3,
                ),
                "evidence": "来自 PyTorch / LibTorch / TELLER frontend-backend 事件。",
            },
            {
                "id": "cuda_runtime",
                "label": "CUDA Runtime / Driver",
                "layer": "cuda",
                "status": "observed" if any(s.get("type") in {"RUNTIME", "DRIVER"} for s in req.get("stages") or []) else "missing",
                "duration_ms": round(stage_duration("RUNTIME", req) + stage_duration("DRIVER", req), 3),
                "evidence": "来自 CUDA Runtime / Driver CUPTI 事件。",
            },
            {
                "id": "gpu_kernel",
                "label": "GPU Kernel",
                "layer": "gpu",
                "status": stage_status("KERNEL", req),
                "duration_ms": stage_duration("KERNEL", req),
                "evidence": "来自 CUPTI Kernel 事件。",
            },
        ]
        observed_nodes = [node for node in nodes if node["status"] == "observed"]
        missing_nodes = [node for node in nodes if node["status"] != "observed"]
        request_gaps: List[str] = []
        missing_layers = [node["layer"] for node in missing_nodes]
        if "request" in missing_layers:
            request_gaps.append("缺少 HTTP/API server 入站 span；当前端到端跨度由 TELLER 请求窗口近似。")
        if "framework" in missing_layers:
            request_gaps.append("缺少 PyTorch/LibTorch 前端 span；需要启用 AgenticSRE TELLER PyTorch hook 或 TELLER frontend/backend 解析。")
        if "gpu" in missing_layers:
            request_gaps.append("缺少 GPU Kernel 事件，无法确认请求是否真正下沉到 GPU 执行。")
        normalized_requests.append({
            "request_id": request_id,
            "span_ms": req.get("span_ms"),
            "event_count": req.get("event_count"),
            "coverage": "partial_e2e" if missing_nodes else "full_e2e",
            "nodes": nodes,
            "edges": [
                {"from": nodes[i]["id"], "to": nodes[i + 1]["id"], "status": "inferred"}
                for i in range(len(nodes) - 1)
            ],
            "critical_path": req.get("critical_path") or [],
            "top_events": req.get("top_events") or [],
            "observed_layers": [node["layer"] for node in observed_nodes],
            "missing_layers": missing_layers,
            "gaps": request_gaps,
        })

    gaps = []
    if not normalized_requests:
        gaps.append("未识别请求级 request_id，无法构建端到端请求路径。")
    else:
        if any("request" in item.get("missing_layers", []) for item in normalized_requests):
            gaps.append("缺少 HTTP/API server 入站 span；当前端到端跨度由 TELLER 请求窗口近似。")
        if any("framework" in item.get("missing_layers", []) for item in normalized_requests):
            gaps.append("缺少 PyTorch/LibTorch 前端 span；需要启用 AgenticSRE TELLER PyTorch hook 或 TELLER frontend/backend 解析。")
        if not any((item.get("nodes") or [])[-1].get("status") == "observed" for item in normalized_requests):
            gaps.append("缺少 GPU Kernel 事件，无法确认请求是否真正下沉到 GPU 执行。")

    full_count = sum(1 for item in normalized_requests if item.get("coverage") == "full_e2e")
    partial_count = len(normalized_requests) - full_count
    if not normalized_requests:
        coverage = "none"
    elif full_count == len(normalized_requests):
        coverage = "full_e2e"
    elif full_count > 0:
        coverage = "mixed_e2e"
    else:
        coverage = "partial_e2e"

    return {
        "schema_version": "agenticsre.ai_trace.v1",
        "available": bool(normalized_requests),
        "coverage": coverage,
        "message": (
            "已构建请求级端到端 Trace 归一化视图，HTTP 入站层如未接入会标记为观测缺口。"
            if normalized_requests else
            "缺少请求级上下文，暂无法构建端到端 Trace。"
        ),
        "request_count": len(normalized_requests),
        "full_e2e_count": full_count,
        "partial_e2e_count": partial_count,
        "requests": normalized_requests,
        "aggregate": {
            "request_count": len(normalized_requests),
            "full_e2e_count": full_count,
            "partial_e2e_count": partial_count,
            "total_events": summary.get("total_events") or 0,
            "kernel_events": summary.get("kernel_events") or 0,
            "runtime_driver_ms": round(type_totals.get("RUNTIME", 0) + type_totals.get("DRIVER", 0), 3),
            "kernel_ms": round(type_totals.get("KERNEL", 0), 3),
            "vllm_stage_ms": round(type_totals.get("ENGINE", 0) + type_totals.get("NVTX", 0), 3),
        },
        "gaps": gaps,
    }


def _summarize_teller_trace(
    trace_dir: str,
    max_events: int = 50000,
    *,
    run_prefix: str = "",
    max_files: int = 80,
) -> Dict[str, Any]:
    """Summarize TELLER NVTX/CUPTI trace files for dashboard-level display."""
    configured = bool(trace_dir)
    result: Dict[str, Any] = {
        "configured": configured,
        "available": False,
        "trace_dir": trace_dir,
        "message": "未配置 TELLER trace 目录；可在集群配置中设置 teller_trace_dir，或设置 TELLER_TRACE_DIR。",
        "files": [],
        "summary": {
            "total_events": 0,
            "kernel_events": 0,
            "nvtx_events": 0,
            "step_events": 0,
            "frontend_events": 0,
            "backend_events": 0,
            "runtime_events": 0,
            "driver_events": 0,
            "kernel_total_ms": 0,
            "kernel_p95_ms": 0,
            "time_span_ms": 0,
        },
        "top_kernels": [],
        "top_nvtx": [],
        "top_runtime": [],
        "event_type_stats": [],
        "key_durations": [],
        "request_context": {
            "available": False,
            "request_count": 0,
            "coverage": "none",
            "requests": [],
            "message": "未发现 TELLER 请求级 step_execute_model[request_ids:...] 上下文。",
        },
        "hierarchy": {
            "available": False,
            "event_count": 0,
            "step_count": 0,
            "roots": [],
            "message": "未发现 TELLER 原生 steps 执行树；如需完整请求路径，请启用 TELLER trace_parser 输出。",
        },
        "bottlenecks": [],
        "recent_events": [],
    }
    if not configured:
        return result
    root = Path(trace_dir)
    if not root.exists():
        result["message"] = "TELLER trace 目录不可达；请确认容器挂载或路径配置。"
        return result
    files = _find_teller_event_files(trace_dir, limit=max_files, run_prefix=run_prefix)
    cache_key = f"{root.resolve()}:{max_events}:{max_files}:{run_prefix}"
    signature = _teller_trace_signature(files)
    cached = _TELLER_TRACE_SUMMARY_CACHE.get(cache_key)
    if cached and cached.get("signature") == signature and time.time() - float(cached.get("cached_at") or 0) <= _TELLER_TRACE_SUMMARY_CACHE_TTL:
        return dict(cached.get("result") or result)
    result["files"] = [
        {"path": str(p), "name": p.name, "mtime": p.stat().st_mtime, "size": p.stat().st_size}
        for p in files
    ]
    if not files:
        result["message"] = (
            f"TELLER trace 目录存在，但未发现匹配 {run_prefix} 的 JSONL/JSON 事件文件。"
            if run_prefix
            else "TELLER trace 目录存在，但未发现 JSONL/JSON 事件文件。"
        )
        _TELLER_TRACE_SUMMARY_CACHE[cache_key] = {"signature": signature, "cached_at": time.time(), "result": dict(result)}
        return result

    events: List[Dict[str, Any]] = []
    remaining = max_events
    for path in files:
        if remaining <= 0:
            break
        batch = _read_teller_events_from_file(path, remaining)
        for ev in batch:
            ev["source_file"] = path.name
            ev["source_dir"] = str(path.parent)
            ev["source_path"] = str(path)
        events.extend(batch)
        remaining = max_events - len(events)

    if not events:
        result["message"] = "已发现 trace 文件，但无法解析出 TELLER 事件。"
        _TELLER_TRACE_SUMMARY_CACHE[cache_key] = {"signature": signature, "cached_at": time.time(), "result": dict(result)}
        return result

    events = _attach_teller_request_windows(events)
    events = _merge_teller_http_request_aliases(events)
    by_type = Counter(e.get("type") for e in events)
    kernel_events = [e for e in events if e.get("type") == "KERNEL"]
    nvtx_like_types = {"NVTX", "ENGINE", "SCHEDULER", "PREFILL", "DECODE", "PYTORCH", "LIBTORCH", "HTTP"}
    nvtx_events = [e for e in events if e.get("type") in nvtx_like_types]
    hierarchy_events = [e for e in events if e.get("teller_hierarchy")]
    kernel_durations = sorted([e["duration_ns"] for e in kernel_events if e.get("duration_ns")])
    starts = [e.get("start", 0) for e in events if e.get("start")]
    ends = [e.get("start", 0) + e.get("duration_ns", 0) for e in events if e.get("start")]
    kernel_by_name: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"count": 0, "total_ns": 0, "max_ns": 0, "durations": []})
    nvtx_by_name: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"count": 0, "total_ns": 0, "max_ns": 0, "durations": []})
    runtime_by_name: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"count": 0, "total_ns": 0, "max_ns": 0, "durations": []})
    type_by_name: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"count": 0, "total_ns": 0, "max_ns": 0, "durations": []})
    for ev in kernel_events:
        item = kernel_by_name[ev["name"]]
        item["count"] += 1
        item["total_ns"] += ev.get("duration_ns", 0)
        item["max_ns"] = max(item["max_ns"], ev.get("duration_ns", 0))
        if ev.get("duration_ns"):
            item["durations"].append(ev.get("duration_ns", 0))
    for ev in nvtx_events:
        item = nvtx_by_name[ev["name"]]
        item["count"] += 1
        item["total_ns"] += ev.get("duration_ns", 0)
        item["max_ns"] = max(item["max_ns"], ev.get("duration_ns", 0))
        if ev.get("duration_ns"):
            item["durations"].append(ev.get("duration_ns", 0))
    for ev in events:
        duration_ns = int(ev.get("duration_ns") or 0)
        event_type = str(ev.get("type") or "UNKNOWN")
        if event_type in {"RUNTIME", "DRIVER"}:
            item = runtime_by_name[ev["name"]]
            item["count"] += 1
            item["total_ns"] += duration_ns
            item["max_ns"] = max(item["max_ns"], duration_ns)
            if duration_ns:
                item["durations"].append(duration_ns)
        item = type_by_name[event_type]
        item["count"] += 1
        item["total_ns"] += duration_ns
        item["max_ns"] = max(item["max_ns"], duration_ns)
        if duration_ns:
            item["durations"].append(duration_ns)

    def rank_rows(rows: Dict[str, Dict[str, Any]], limit: int = 10) -> List[Dict[str, Any]]:
        ranked = sorted(rows.items(), key=lambda kv: (kv[1]["total_ns"], kv[1]["count"]), reverse=True)[:limit]
        return [
            {
                "name": name,
                "count": values["count"],
                "total_ms": round(values["total_ns"] / 1_000_000, 3),
                "avg_ms": round((values["total_ns"] / max(values["count"], 1)) / 1_000_000, 3),
                "max_ms": round(values["max_ns"] / 1_000_000, 3),
                "p95_ms": round(p95(sorted(values.get("durations", []))), 3) if values.get("durations") else 0,
            }
            for name, values in ranked
        ]

    def p95(values: List[int]) -> float:
        if not values:
            return 0.0
        idx = min(len(values) - 1, max(0, int(math.ceil(len(values) * 0.95)) - 1))
        return values[idx] / 1_000_000

    def named_rows_with_p95(rows: Dict[str, Dict[str, Any]], limit: int = 10) -> List[Dict[str, Any]]:
        ranked = sorted(rows.items(), key=lambda kv: (kv[1]["total_ns"], kv[1]["max_ns"]), reverse=True)[:limit]
        out_rows = []
        for name, values in ranked:
            durations = sorted(values.get("durations", []))
            out_rows.append({
                "name": name,
                "count": values["count"],
                "total_ms": round(values["total_ns"] / 1_000_000, 3),
                "avg_ms": round((values["total_ns"] / max(values["count"], 1)) / 1_000_000, 3),
                "p95_ms": round(p95(durations), 3),
                "max_ms": round(values["max_ns"] / 1_000_000, 3),
            })
        return out_rows

    def event_type_rows() -> List[Dict[str, Any]]:
        rows = []
        for event_type, values in sorted(type_by_name.items(), key=lambda kv: kv[1]["total_ns"], reverse=True):
            durations = sorted(values.get("durations", []))
            rows.append({
                "type": event_type,
                "label": {
                    "NVTX": "业务阶段 / NVTX",
                    "HTTP": "HTTP / API 入站",
                    "ENGINE": "vLLM 引擎",
                    "SCHEDULER": "vLLM 调度",
                    "PREFILL": "Prefill 阶段",
                    "DECODE": "Decode 阶段",
                    "PYTORCH": "PyTorch 前端",
                    "LIBTORCH": "LibTorch / Aten",
                    "STEP": "TELLER Step",
                    "FRONTEND": "PyTorch Frontend",
                    "BACKEND": "PyTorch Backend",
                    "KERNEL": "GPU Kernel",
                    "RUNTIME": "CUDA Runtime",
                    "DRIVER": "CUDA Driver",
                    "UNKNOWN": "其他事件",
                }.get(event_type, event_type),
                "count": values["count"],
                "total_ms": round(values["total_ns"] / 1_000_000, 3),
                "avg_ms": round((values["total_ns"] / max(values["count"], 1)) / 1_000_000, 3),
                "p95_ms": round(p95(durations), 3),
                "max_ms": round(values["max_ns"] / 1_000_000, 3),
            })
        return rows

    key_duration_candidates = []
    for ev in events:
        duration_ns = int(ev.get("duration_ns") or 0)
        if duration_ns <= 0:
            continue
        key_duration_candidates.append({
            "type": ev.get("type") or "UNKNOWN",
            "name": ev.get("name") or "-",
            "duration_ms": round(duration_ns / 1_000_000, 3),
            "start_ms": round((int(ev.get("start") or 0) - min(starts)) / 1_000_000, 3) if starts else 0,
            "process_id": ev.get("process_id") or 0,
            "thread_id": ev.get("thread_id") or 0,
            "depth": ev.get("depth"),
            "parent_idx": ev.get("parent_idx"),
            "path": ev.get("path") or "",
        })
    key_durations = sorted(key_duration_candidates, key=lambda e: e["duration_ms"], reverse=True)[:20]
    type_stats = event_type_rows()
    bottlenecks = [
        {
            "name": row["label"],
            "reason": "累计耗时最高",
            "total_ms": row["total_ms"],
            "p95_ms": row["p95_ms"],
            "count": row["count"],
        }
        for row in type_stats[:4]
        if row.get("total_ms", 0) > 0
    ]

    request_groups: Dict[str, Dict[str, Any]] = {}
    request_events = [e for e in events if e.get("request_ids")]
    for ev in request_events:
        ids = ev.get("request_ids") or []
        for request_id in ids:
            rid = str(request_id or "").strip()
            if not rid:
                continue
            item = request_groups.setdefault(rid, {
                "request_id": rid,
                "event_count": 0,
                "kernel_events": 0,
                "nvtx_events": 0,
                "runtime_events": 0,
                "driver_events": 0,
                "total_ms": 0.0,
                "start_ns": None,
                "end_ns": None,
                "process_ids": set(),
                "thread_ids": set(),
                "events": [],
            })
            duration_ns = int(ev.get("duration_ns") or 0)
            start_ns = int(ev.get("start") or 0)
            end_ns = start_ns + duration_ns
            event_type = str(ev.get("type") or "UNKNOWN")
            item["event_count"] += 1
            if event_type == "KERNEL":
                item["kernel_events"] += 1
            elif event_type in nvtx_like_types:
                item["nvtx_events"] += 1
            elif event_type == "RUNTIME":
                item["runtime_events"] += 1
            elif event_type == "DRIVER":
                item["driver_events"] += 1
            item["total_ms"] += duration_ns / 1_000_000
            item["start_ns"] = start_ns if item["start_ns"] is None else min(item["start_ns"], start_ns)
            item["end_ns"] = end_ns if item["end_ns"] is None else max(item["end_ns"], end_ns)
            if ev.get("process_id") is not None:
                item["process_ids"].add(str(ev.get("process_id")))
            if ev.get("thread_id") is not None:
                item["thread_ids"].add(str(ev.get("thread_id")))
            item["events"].append({
                "type": event_type,
                "name": ev.get("name") or "-",
                "duration_ms": round(duration_ns / 1_000_000, 3),
                "start_ms": round((start_ns - min(starts)) / 1_000_000, 3) if starts else 0,
                "process_id": ev.get("process_id") or 0,
                "thread_id": ev.get("thread_id") or 0,
                "depth": ev.get("depth"),
                "parent_idx": ev.get("parent_idx"),
                "path": ev.get("path") or "",
            })
    request_rows = []
    for item in request_groups.values():
        raw_item_events = item.pop("events")
        ordered_events = sorted(raw_item_events, key=lambda x: (x.get("start_ms", 0), -x.get("duration_ms", 0)))
        item_events = sorted(raw_item_events, key=lambda x: x["duration_ms"], reverse=True)[:12]
        stage_rows: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"count": 0, "total_ms": 0.0, "max_ms": 0.0})
        for ev in raw_item_events:
            event_type = str(ev.get("type") or "UNKNOWN")
            row = stage_rows[event_type]
            duration_ms = float(ev.get("duration_ms") or 0)
            row["count"] += 1
            row["total_ms"] += duration_ms
            row["max_ms"] = max(row["max_ms"], duration_ms)
        stages = [
            {
                "type": event_type,
                "label": {
                    "NVTX": "业务阶段 / NVTX",
                    "HTTP": "HTTP / API 入站",
                    "ENGINE": "vLLM 引擎",
                    "SCHEDULER": "vLLM 调度",
                    "PREFILL": "Prefill 阶段",
                    "DECODE": "Decode 阶段",
                    "PYTORCH": "PyTorch 前端",
                    "LIBTORCH": "LibTorch / Aten",
                    "STEP": "TELLER Step",
                    "FRONTEND": "PyTorch Frontend",
                    "BACKEND": "PyTorch Backend",
                    "KERNEL": "GPU Kernel",
                    "RUNTIME": "CUDA Runtime",
                    "DRIVER": "CUDA Driver",
                    "UNKNOWN": "其他事件",
                }.get(event_type, event_type),
                "count": values["count"],
                "total_ms": round(values["total_ms"], 3),
                "max_ms": round(values["max_ms"], 3),
            }
            for event_type, values in sorted(stage_rows.items(), key=lambda kv: kv[1]["total_ms"], reverse=True)
        ]
        request_rows.append({
            "request_id": item["request_id"],
            "event_count": item["event_count"],
            "kernel_events": item["kernel_events"],
            "nvtx_events": item["nvtx_events"],
            "runtime_events": item["runtime_events"],
            "driver_events": item["driver_events"],
            "total_ms": round(item["total_ms"], 3),
            "span_ms": round(((item["end_ns"] or 0) - (item["start_ns"] or 0)) / 1_000_000, 3),
            "process_ids": sorted(item["process_ids"])[:8],
            "thread_ids": sorted(item["thread_ids"])[:8],
            "top_events": item_events,
            "path_events": ordered_events[:40],
            "stages": stages,
            "critical_path": [
                {
                    "type": ev.get("type") or "UNKNOWN",
                    "name": ev.get("name") or "-",
                    "start_ms": ev.get("start_ms") or 0,
                    "duration_ms": ev.get("duration_ms") or 0,
                    "process_id": ev.get("process_id") or 0,
                    "thread_id": ev.get("thread_id") or 0,
                    "depth": ev.get("depth"),
                    "path": ev.get("path") or "",
                }
                for ev in ordered_events[:24]
            ],
        })
    request_rows = sorted(request_rows, key=lambda x: (x["total_ms"], x["event_count"]), reverse=True)[:50]
    request_context = {
        "available": bool(request_rows),
        "request_count": len(request_rows),
        "coverage": "request_level" if request_rows else "trace_only",
        "requests": request_rows,
        "message": (
            f"已识别 {len(request_rows)} 个 TELLER 请求级上下文。"
            if request_rows else
            "已解析 trace，但未发现 step_execute_model[request_ids:...]；请确认 vLLM 由 TELLER hook 包裹启动。"
        ),
    }
    def health_item(ok: bool, label: str, count: int, remediation: str = "") -> Dict[str, Any]:
        return {
            "ok": bool(ok),
            "label": label,
            "count": int(count or 0),
            "remediation": remediation,
        }

    heartbeat_events = [e for e in events if e.get("type") == "HEARTBEAT"]
    latest_mtime = max((float(f.get("mtime") or 0) for f in result.get("files", [])), default=0.0)
    latest_heartbeat_ns = max((int(e.get("start") or 0) for e in heartbeat_events), default=0)
    latest_heartbeat_seconds = (latest_heartbeat_ns / 1_000_000_000) if latest_heartbeat_ns else 0.0
    freshness_seconds = max(latest_mtime, latest_heartbeat_seconds)
    age_seconds = max(0.0, time.time() - freshness_seconds) if freshness_seconds else None
    heartbeat_age_seconds = max(0.0, time.time() - latest_heartbeat_seconds) if latest_heartbeat_seconds else None
    trace_health = {
        "status": "ready",
        "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
        "heartbeat_age_seconds": round(heartbeat_age_seconds, 3) if heartbeat_age_seconds is not None else None,
        "checks": [
            health_item(bool(heartbeat_events), "采集心跳", len(heartbeat_events), "启用 AgenticSRE TELLER hooks；空闲时也会写入 heartbeat。"),
            health_item(bool(request_rows), "请求级上下文", len(request_rows), "确认 vLLM 由 TELLER wrapper 启动，并写入 request_ids。"),
            health_item(by_type.get("HTTP", 0) > 0, "HTTP 入站", by_type.get("HTTP", 0), "启用 AgenticSRE vLLM HTTP hook。"),
            health_item(by_type.get("PYTORCH", 0) + by_type.get("LIBTORCH", 0) + by_type.get("FRONTEND", 0) + by_type.get("BACKEND", 0) > 0, "PyTorch/框架层", by_type.get("PYTORCH", 0) + by_type.get("LIBTORCH", 0) + by_type.get("FRONTEND", 0) + by_type.get("BACKEND", 0), "启用 AgenticSRE TELLER PyTorch hook 或 TELLER frontend/backend 解析。"),
            health_item(by_type.get("RUNTIME", 0) + by_type.get("DRIVER", 0) > 0, "CUDA Runtime/Driver", by_type.get("RUNTIME", 0) + by_type.get("DRIVER", 0), "确认 CUPTI 注入库处于采集窗口。"),
            health_item(by_type.get("KERNEL", 0) > 0, "GPU Kernel", by_type.get("KERNEL", 0), "确认 CUPTI Kernel 事件持续写入。"),
        ],
    }
    if any(not item["ok"] for item in trace_health["checks"]):
        trace_health["status"] = "partial"
    if age_seconds is not None and age_seconds > 300:
        trace_health["status"] = "stale" if trace_health["status"] == "ready" else trace_health["status"]
        trace_health["checks"].append(health_item(False, "采集新鲜度", int(age_seconds), "最新 trace/heartbeat 超过 5 分钟未更新，可能不是持续采集。"))

    timeline_events = sorted(
        [e for e in events if e.get("type") in {"HTTP", "STEP", "FRONTEND", "BACKEND", "KERNEL", "NVTX", "ENGINE", "SCHEDULER", "PREFILL", "DECODE", "PYTORCH", "LIBTORCH", "RUNTIME", "DRIVER"}],
        key=lambda e: (e.get("start", 0), -e.get("duration_ns", 0)),
    )
    timeline_limit = 600
    if len(timeline_events) > timeline_limit:
        by_duration = sorted(timeline_events, key=lambda e: e.get("duration_ns", 0), reverse=True)[: timeline_limit // 2]
        early = timeline_events[: timeline_limit // 2]
        keep_keys = {(e.get("type"), e.get("name"), e.get("start"), e.get("duration_ns")) for e in [*by_duration, *early]}
        timeline_events = [
            e for e in timeline_events
            if (e.get("type"), e.get("name"), e.get("start"), e.get("duration_ns")) in keep_keys
        ][:timeline_limit]

    timeline_display_events: List[Dict[str, Any]] = []
    display_cursor_ns = 0
    display_gap_ns = 1_000_000
    by_source: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for ev in timeline_events:
        by_source[str(ev.get("source_file") or "trace")].append(ev)
    for source, source_events in sorted(by_source.items(), key=lambda kv: min((e.get("start", 0) for e in kv[1]), default=0)):
        source_starts = [int(e.get("start") or 0) for e in source_events]
        source_ends = [int(e.get("start") or 0) + int(e.get("duration_ns") or 0) for e in source_events]
        source_min = min(source_starts) if source_starts else 0
        source_max = max(source_ends) if source_ends else source_min
        source_span = max(1, source_max - source_min)
        for ev in source_events:
            item = dict(ev)
            item["display_start_ns"] = display_cursor_ns + max(0, int(ev.get("start") or 0) - source_min)
            item["display_duration_ns"] = int(ev.get("duration_ns") or 0)
            item["display_start_ms"] = round(item["display_start_ns"] / 1_000_000, 3)
            item["display_end_ms"] = round((item["display_start_ns"] + item["display_duration_ns"]) / 1_000_000, 3)
            item["duration_ms"] = round(item["display_duration_ns"] / 1_000_000, 3)
            item["source_file"] = source
            timeline_display_events.append(item)
        display_cursor_ns += source_span + display_gap_ns
    display_range_ns = [0, max(0, display_cursor_ns - display_gap_ns)] if timeline_display_events else [0, 0]
    hierarchy_roots = [
        {
            "name": e.get("name") or "-",
            "duration_ms": round(int(e.get("duration_ns") or 0) / 1_000_000, 3),
            "start_ms": round((int(e.get("start") or 0) - min(starts)) / 1_000_000, 3) if starts else 0,
            "path": e.get("path") or e.get("name") or "-",
            "step_index": e.get("step_index"),
        }
        for e in hierarchy_events
        if e.get("type") == "STEP"
    ][:30]

    result.update({
        "available": True,
        "message": "已解析 TELLER NVTX/CUPTI 细粒度事件。",
        "summary": {
            "total_events": len(events),
            "kernel_events": by_type.get("KERNEL", 0),
            "http_events": by_type.get("HTTP", 0),
            "nvtx_events": by_type.get("NVTX", 0),
            "engine_events": by_type.get("ENGINE", 0),
            "scheduler_events": by_type.get("SCHEDULER", 0),
            "prefill_events": by_type.get("PREFILL", 0),
            "decode_events": by_type.get("DECODE", 0),
            "pytorch_events": by_type.get("PYTORCH", 0),
            "libtorch_events": by_type.get("LIBTORCH", 0),
            "step_events": by_type.get("STEP", 0),
            "frontend_events": by_type.get("FRONTEND", 0),
            "backend_events": by_type.get("BACKEND", 0),
            "runtime_events": by_type.get("RUNTIME", 0),
            "driver_events": by_type.get("DRIVER", 0),
            "hierarchy_events": len(hierarchy_events),
            "request_context_events": len(request_events),
            "request_count": len(request_rows),
            "kernel_total_ms": round(sum(kernel_durations) / 1_000_000, 3),
            "kernel_p95_ms": round(p95(kernel_durations), 3),
            "time_span_ms": round((max(ends) - min(starts)) / 1_000_000, 3) if starts and ends else 0,
        },
        "top_kernels": named_rows_with_p95(kernel_by_name),
        "top_nvtx": named_rows_with_p95(nvtx_by_name),
        "top_runtime": named_rows_with_p95(runtime_by_name),
        "event_type_stats": type_stats,
        "key_durations": key_durations,
        "request_context": request_context,
        "trace_health": trace_health,
        "e2e_trace": _build_teller_e2e_trace(request_context, type_stats, {
            "total_events": len(events),
            "kernel_events": by_type.get("KERNEL", 0),
        }),
        "hierarchy": {
            "available": bool(hierarchy_events),
            "event_count": len(hierarchy_events),
            "step_count": by_type.get("STEP", 0),
            "roots": hierarchy_roots,
            "message": (
                "已解析 TELLER trace_parser 风格的 step/frontend/backend/runtime/kernel 执行树。"
                if hierarchy_events else
                "未发现 TELLER 原生 steps 执行树；如需完整请求路径，请启用 TELLER trace_parser 输出。"
            ),
        },
        "bottlenecks": bottlenecks,
        "timeline": {
            "time_range_ns": [min(starts), max(ends)] if starts and ends else [0, 0],
            "display_time_range_ns": display_range_ns,
            "events": sorted(timeline_display_events, key=lambda e: (e.get("display_start_ns", 0), -e.get("display_duration_ns", 0))),
            "truncated": len(events) > len(timeline_events),
            "event_count": len(timeline_events),
            "display_note": "时间轴已按 trace 文件归一化展示；开始时间为当前可见 trace 片段内的相对时间。",
        },
        "recent_events": sorted(events, key=lambda e: e.get("start", 0), reverse=True)[:80],
    })
    _TELLER_TRACE_SUMMARY_CACHE[cache_key] = {"signature": signature, "cached_at": time.time(), "result": dict(result)}
    return result


def _teller_probe_script() -> str:
    """Python payload executed inside a short-lived GPU profiler pod."""
    return r'''
import json
import os
import time
from pathlib import Path

out = Path(os.environ.get("TELLER_OUTPUT_DIR", "/trace"))
out.mkdir(parents=True, exist_ok=True)
iterations = int(os.environ.get("TELLER_ITERATIONS", "8"))
matrix_size = int(os.environ.get("TELLER_MATRIX_SIZE", "1024"))
hold_seconds = int(os.environ.get("TELLER_HOLD_SECONDS", "300"))
meta = {
    "collector": "agenticsre_teller_probe",
    "iterations": iterations,
    "matrix_size": matrix_size,
    "started_at": time.time(),
}
try:
    import torch
    from torch.profiler import ProfilerActivity, profile, record_function

    cuda = torch.cuda.is_available()
    meta["cuda_available"] = bool(cuda)
    meta["device_count"] = torch.cuda.device_count() if cuda else 0
    if cuda:
        meta["device_name"] = torch.cuda.get_device_name(0)
    device = "cuda:0" if cuda else "cpu"
    dtype = torch.float16 if cuda else torch.float32
    activities = [ProfilerActivity.CPU]
    if cuda:
        activities.append(ProfilerActivity.CUDA)
    with profile(activities=activities, record_shapes=False, with_stack=False) as prof:
        for idx in range(iterations):
            with record_function("agenticsre_teller_probe_step"):
                a = torch.randn((matrix_size, matrix_size), device=device, dtype=dtype)
                b = torch.randn((matrix_size, matrix_size), device=device, dtype=dtype)
                c = torch.matmul(a, b)
                y = torch.nn.functional.silu(c)
                _ = y.sum()
                if cuda:
                    torch.cuda.synchronize()
            prof.step()
    prof.export_chrome_trace(str(out / "torch_trace.json"))
    meta["status"] = "ok"
except Exception as exc:
    meta["status"] = "failed"
    meta["error"] = str(exc)
finally:
    meta["finished_at"] = time.time()
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "_READY").write_text("ok\n", encoding="utf-8")
    time.sleep(hold_seconds)
'''


def _k8s_manifest_to_tempfile(manifest: Dict[str, Any]) -> str:
    tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".yaml", delete=False)
    try:
        yaml.safe_dump(manifest, tmp, sort_keys=False, allow_unicode=True)
        return tmp.name
    finally:
        tmp.close()


def _find_vllm_node_for_cluster(cluster_id: str, namespace: str) -> str:
    result = _shell_sync(
        f"kubectl -n {shlex.quote(namespace)} get pods -l app=vllm-server -o json",
        timeout=30,
        cluster_id=cluster_id,
    )
    if not result.get("ok"):
        return ""
    try:
        payload = json.loads(result.get("stdout") or "{}")
    except Exception:
        return ""
    for pod in payload.get("items") or []:
        node = ((pod.get("spec") or {}).get("nodeName") or "").strip()
        phase = ((pod.get("status") or {}).get("phase") or "").strip()
        if node and phase == "Running":
            return node
    for pod in payload.get("items") or []:
        node = ((pod.get("spec") or {}).get("nodeName") or "").strip()
        if node:
            return node
    return ""


def _build_teller_probe_job_manifest(
    *,
    namespace: str,
    job_name: str,
    image: str,
    node_name: str,
    iterations: int,
    matrix_size: int,
    hold_seconds: int,
    gpu_limit: int,
) -> Dict[str, Any]:
    pod_spec: Dict[str, Any] = {
        "restartPolicy": "Never",
        "containers": [
            {
                "name": "collector",
                "image": image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["/bin/sh", "-lc"],
                "args": [f"python3 - <<'PY'\n{_teller_probe_script()}\nPY"],
                "env": [
                    {"name": "TELLER_OUTPUT_DIR", "value": "/trace"},
                    {"name": "TELLER_ITERATIONS", "value": str(iterations)},
                    {"name": "TELLER_MATRIX_SIZE", "value": str(matrix_size)},
                    {"name": "TELLER_HOLD_SECONDS", "value": str(hold_seconds)},
                ],
                "resources": {
                    "limits": {"nvidia.com/gpu": gpu_limit},
                },
                "volumeMounts": [{"name": "trace", "mountPath": "/trace"}],
            }
        ],
        "volumes": [{"name": "trace", "emptyDir": {}}],
    }
    if node_name:
        pod_spec["nodeSelector"] = {"kubernetes.io/hostname": node_name}
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": namespace,
            "labels": {
                "app": "agenticsre-teller-probe",
                "agenticsre.io/component": "teller-probe",
            },
        },
        "spec": {
            "backoffLimit": 0,
            "template": {
                "metadata": {"labels": {"app": "agenticsre-teller-probe", "job-name": job_name}},
                "spec": pod_spec,
            },
        },
    }


def _run_teller_probe_collection(
    cluster: Dict[str, Any],
    *,
    namespace: str,
    node_name: str,
    image: str,
    iterations: int,
    matrix_size: int,
    timeout_seconds: int,
    hold_seconds: int,
    gpu_limit: int,
    trace_dir: str,
) -> Dict[str, Any]:
    cluster_id = str(cluster.get("id") or "")
    run_id = f"teller-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    job_name = f"teller-probe-{uuid.uuid4().hex[:8]}"
    manifest = _build_teller_probe_job_manifest(
        namespace=namespace,
        job_name=job_name,
        image=image,
        node_name=node_name,
        iterations=iterations,
        matrix_size=matrix_size,
        hold_seconds=hold_seconds,
        gpu_limit=gpu_limit,
    )
    manifest_path = _k8s_manifest_to_tempfile(manifest)
    local_run_dir = Path(trace_dir) / run_id
    local_run_dir.mkdir(parents=True, exist_ok=True)
    events: List[Dict[str, Any]] = []
    try:
        apply_res = _shell_sync(f"kubectl apply -f {shlex.quote(manifest_path)}", timeout=45, cluster_id=cluster_id)
        events.append({"step": "apply", **apply_res})
        if not apply_res.get("ok"):
            return {"status": "failed", "run_id": run_id, "job": job_name, "events": events, "error": apply_res.get("stderr") or apply_res.get("stdout")}

        wait_pod = _shell_sync(
            f"kubectl -n {shlex.quote(namespace)} wait --for=condition=Ready pod -l job-name={shlex.quote(job_name)} --timeout={max(30, min(timeout_seconds, 180))}s",
            timeout=max(45, min(timeout_seconds + 10, 210)),
            cluster_id=cluster_id,
        )
        events.append({"step": "wait_ready", **wait_pod})
        pod_res = _shell_sync(
            f"kubectl -n {shlex.quote(namespace)} get pod -l job-name={shlex.quote(job_name)} -o jsonpath='{{.items[0].metadata.name}}'",
            timeout=30,
            cluster_id=cluster_id,
        )
        pod_name = (pod_res.get("stdout") or "").strip().strip("'")
        events.append({"step": "pod", **pod_res, "pod_name": pod_name})
        if not pod_name:
            return {"status": "failed", "run_id": run_id, "job": job_name, "events": events, "error": "collector pod not found"}

        ready_res = _shell_sync(
            f"kubectl -n {shlex.quote(namespace)} exec {shlex.quote(pod_name)} -- sh -lc 'for i in $(seq 1 {max(10, timeout_seconds // 2)}); do test -f /trace/_READY && exit 0; sleep 2; done; exit 1'",
            timeout=timeout_seconds + 20,
            cluster_id=cluster_id,
        )
        events.append({"step": "wait_trace", **ready_res})
        logs_res = _shell_sync(
            f"kubectl -n {shlex.quote(namespace)} logs {shlex.quote(pod_name)} --tail=80",
            timeout=30,
            cluster_id=cluster_id,
        )
        events.append({"step": "logs", **logs_res})
        if not ready_res.get("ok"):
            return {"status": "failed", "run_id": run_id, "job": job_name, "pod": pod_name, "events": events, "error": "trace file was not produced"}

        cp_res = _shell_sync(
            f"kubectl -n {shlex.quote(namespace)} cp {shlex.quote(pod_name)}:/trace/. {shlex.quote(str(local_run_dir))}",
            timeout=90,
            cluster_id=cluster_id,
        )
        events.append({"step": "copy", **cp_res})
        if not cp_res.get("ok"):
            return {"status": "failed", "run_id": run_id, "job": job_name, "pod": pod_name, "events": events, "error": cp_res.get("stderr") or cp_res.get("stdout")}

        summary = _summarize_teller_trace(str(local_run_dir))
        return {
            "status": "completed" if summary.get("available") else "no_data",
            "run_id": run_id,
            "job": job_name,
            "pod": pod_name,
            "namespace": namespace,
            "node": node_name,
            "image": image,
            "trace_dir": str(local_run_dir),
            "events": events,
            "fine_grained_trace": summary,
        }
    finally:
        _shell_sync(
            f"kubectl -n {shlex.quote(namespace)} delete job {shlex.quote(job_name)} --ignore-not-found=true",
            timeout=45,
            cluster_id=cluster_id,
        )


def _cluster_teller_code_path(cluster: Optional[Dict[str, Any]] = None) -> str:
    return (
        _safe_configured_path((cluster or {}).get("teller_code_path"))
        or _safe_configured_path(os.environ.get("TELLER_CODE_PATH"))
        or "/opt/teller/TELLER_Code"
    )


def _cluster_teller_launcher(cluster: Optional[Dict[str, Any]] = None) -> str:
    launcher = str((cluster or {}).get("teller_launcher") or os.environ.get("TELLER_LAUNCHER") or "python_module").strip()
    if launcher in {"python_module", "uv"}:
        return launcher
    return "python_module"


def _cluster_teller_trace_root(trace_dir: str) -> str:
    return str(Path(trace_dir).parent if trace_dir.endswith("/trace") else trace_dir)


def _cluster_teller_runtime_trace_root(cluster: Optional[Dict[str, Any]], local_trace_root: str) -> str:
    return (
        _safe_configured_path((cluster or {}).get("teller_runtime_trace_root"))
        or _safe_configured_path((cluster or {}).get("teller_trace_container_path"))
        or _safe_configured_path(os.environ.get("TELLER_RUNTIME_TRACE_ROOT"))
        or local_trace_root
    )


def _cluster_teller_code_host_path(cluster: Optional[Dict[str, Any]], container_path: str) -> str:
    return (
        _safe_configured_path((cluster or {}).get("teller_code_host_path"))
        or _safe_configured_path(os.environ.get("TELLER_CODE_HOST_PATH"))
        or container_path
    )


def _cluster_agenticsre_teller_hooks_path(cluster: Optional[Dict[str, Any]] = None) -> str:
    return (
        _safe_configured_path((cluster or {}).get("agenticsre_teller_hooks_path"))
        or _safe_configured_path(os.environ.get("AGENTICSRE_TELLER_HOOKS_PATH"))
        or "/opt/agenticsre/tools/teller"
    )


def _cluster_agenticsre_teller_hooks_host_path(cluster: Optional[Dict[str, Any]], container_path: str) -> str:
    return (
        _safe_configured_path((cluster or {}).get("agenticsre_teller_hooks_host_path"))
        or _safe_configured_path(os.environ.get("AGENTICSRE_TELLER_HOOKS_HOST_PATH"))
        or str(ROOT_DIR / "tools" / "teller")
        or container_path
    )


def _cluster_agenticsre_teller_hooks_source(cluster: Optional[Dict[str, Any]] = None) -> str:
    source = str((cluster or {}).get("agenticsre_teller_hooks_source") or os.environ.get("AGENTICSRE_TELLER_HOOKS_SOURCE") or "configmap").strip().lower()
    return source if source in {"configmap", "hostpath"} else "configmap"


def _cluster_agenticsre_teller_hooks_configmap(cluster: Optional[Dict[str, Any]], deployment: str) -> str:
    raw = str((cluster or {}).get("agenticsre_teller_hooks_configmap") or os.environ.get("AGENTICSRE_TELLER_HOOKS_CONFIGMAP") or "").strip()
    if raw:
        return raw
    clean = re.sub(r"[^a-z0-9.-]+", "-", f"{deployment}-agenticsre-teller-hooks".lower()).strip("-")
    return clean[:63] or "agenticsre-teller-hooks"


def _cluster_teller_trace_host_path(cluster: Optional[Dict[str, Any]], runtime_trace_root: str) -> str:
    return (
        _safe_configured_path((cluster or {}).get("teller_trace_host_path"))
        or _safe_configured_path(os.environ.get("TELLER_TRACE_HOST_PATH"))
        or runtime_trace_root
    )


def _sync_teller_trace_from_k8s(cluster: Dict[str, Any], local_trace_dir: str, *, namespace: str = "") -> Dict[str, Any]:
    """Best-effort copy of TELLER trace files from vLLM pods into AgenticSRE local storage."""
    cluster_id = str(cluster.get("id") or "default")
    ns = namespace or str(cluster.get("default_namespace") or "llm-k8s")
    deployment = str(cluster.get("teller_sync_deployment") or cluster.get("vllm_deployment") or cluster.get("deployment") or "vllm-server")
    container = str(cluster.get("vllm_container") or cluster.get("container") or "vllm")
    local_root = _validate_teller_trace_dir(local_trace_dir, cluster)
    runtime_root = _cluster_teller_runtime_trace_root(cluster, _cluster_teller_trace_root(local_root))
    runtime_trace_dir = str(Path(runtime_root) / "trace")
    if not runtime_trace_dir.startswith("/"):
        return {"enabled": False, "ok": False, "message": "runtime trace dir is not absolute", "events": []}

    Path(local_root).mkdir(parents=True, exist_ok=True)
    events: List[Dict[str, Any]] = []
    pod_res = _shell_sync(
        f"kubectl -n {shlex.quote(ns)} get pods -l app={shlex.quote(deployment)} -o json",
        timeout=30,
        cluster_id=cluster_id,
    )
    events.append({"step": "pods", **pod_res})
    pod_names: List[str] = []
    try:
        payload = json.loads(pod_res.get("stdout") or "{}")
        for item in payload.get("items") or []:
            name = ((item.get("metadata") or {}).get("name") or "").strip()
            phase = ((item.get("status") or {}).get("phase") or "").strip()
            if name and phase == "Running":
                pod_names.append(name)
    except Exception:
        pod_names = []
    if not pod_names:
        return {"enabled": True, "ok": False, "message": f"未找到运行中的 TELLER/vLLM Pod: app={deployment}", "events": events}

    copied = 0
    copied_dirs: List[str] = []
    for pod_name in pod_names[:3]:
        target_dir = Path(local_root) / pod_name
        target_dir.mkdir(parents=True, exist_ok=True)
        probe = _shell_sync(
            f"kubectl -n {shlex.quote(ns)} exec {shlex.quote(pod_name)} -c {shlex.quote(container)} -- sh -lc {shlex.quote(f'test -d {shlex.quote(runtime_trace_dir)} && find {shlex.quote(runtime_trace_dir)} -maxdepth 2 -type f | head -1')}",
            timeout=30,
            cluster_id=cluster_id,
        )
        events.append({"step": "probe", "pod": pod_name, **probe})
        if not (probe.get("ok") and str(probe.get("stdout") or "").strip()):
            continue
        cp_res = _shell_sync(
            f"kubectl -n {shlex.quote(ns)} cp -c {shlex.quote(container)} {shlex.quote(ns)}/{shlex.quote(pod_name)}:{shlex.quote(runtime_trace_dir)}/. {shlex.quote(str(target_dir))}",
            timeout=120,
            cluster_id=cluster_id,
        )
        events.append({"step": "copy", "pod": pod_name, "target_dir": str(target_dir), **cp_res})
        if cp_res.get("ok"):
            copied += 1
            copied_dirs.append(str(target_dir))
    return {
        "enabled": True,
        "ok": copied > 0,
        "pod_count": len(pod_names),
        "copied_pods": copied,
        "copied_dirs": copied_dirs,
        "deployment": deployment,
        "runtime_trace_dir": runtime_trace_dir,
        "local_trace_dir": local_root,
        "message": f"已同步 {copied}/{len(pod_names)} 个 Pod 的 TELLER trace。" if copied else "未发现可同步的 TELLER trace 文件。",
        "events": events[-8:],
    }


def _cluster_teller_cupti_lib_paths(cluster: Optional[Dict[str, Any]] = None) -> List[str]:
    configured = str((cluster or {}).get("teller_cupti_lib_paths") or os.environ.get("TELLER_CUPTI_LIB_PATHS") or "").strip()
    paths = [p for p in re.split(r"[:,]", configured) if p.strip()]
    paths.extend([
        "/usr/local/lib/python3.12/dist-packages/nvidia/cuda_cupti/lib",
        "/usr/local/lib/python3.12/dist-packages/nvidia/nvtx/lib",
        "/usr/local/cuda/extras/CUPTI/lib64",
        "/usr/local/cuda/lib64",
    ])
    result: List[str] = []
    seen = set()
    for path in paths:
        clean = _safe_configured_path(path)
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


def _cluster_teller_injection_so_path(teller_code: str) -> str:
    return str(Path(teller_code) / "src" / "teller" / "lib" / "libcupti_trace_injection_with_nvtx.so")


def _teller_nvtx_bootstrap_command() -> str:
    """Return a POSIX shell snippet that makes NVTX loadable by TELLER hooks."""
    return (
        "teller_nvtx_dirs=$(printf '%s' \"${LD_LIBRARY_PATH:-}\" | tr ':' ' ') && "
        "export TELLER_NVTX_LIB=$(find $teller_nvtx_dirs /usr/local/cuda /usr/local /usr "
        "-name 'libnvToolsExt.so*' 2>/dev/null | head -1) && "
        "if [ -n \"$TELLER_NVTX_LIB\" ]; then "
        "mkdir -p /tmp/teller-nvtx && "
        "ln -sf \"$TELLER_NVTX_LIB\" /tmp/teller-nvtx/libnvToolsExt.so && "
        "export LD_LIBRARY_PATH=/tmp/teller-nvtx:${LD_LIBRARY_PATH:-}; "
        "fi"
    )


def _build_teller_vllm_integration_plan(
    cluster: Dict[str, Any],
    *,
    namespace: str = "",
    include_preflight: bool = True,
) -> Dict[str, Any]:
    """Build a non-mutating plan for running real vLLM under TELLER."""
    cluster_id = str(cluster.get("id") or "default")
    ns = namespace or str(cluster.get("default_namespace") or "llm-k8s")
    deployment = str(cluster.get("vllm_deployment") or cluster.get("deployment") or "vllm-server")
    container = str(cluster.get("vllm_container") or cluster.get("container") or "vllm")
    model = str(cluster.get("model") or "${MODEL_NAME}")
    trace_dir = _cluster_teller_trace_dir(cluster)
    local_trace_root = _cluster_teller_trace_root(trace_dir)
    runtime_trace_root = _cluster_teller_runtime_trace_root(cluster, local_trace_root)
    trace_host_path = _cluster_teller_trace_host_path(cluster, runtime_trace_root)
    teller_code = _cluster_teller_code_path(cluster)
    teller_code_host_path = _cluster_teller_code_host_path(cluster, teller_code)
    teller_src = str(Path(teller_code) / "src")
    hooks_path = _cluster_agenticsre_teller_hooks_path(cluster)
    hooks_host_path = _cluster_agenticsre_teller_hooks_host_path(cluster, hooks_path)
    hooks_source = _cluster_agenticsre_teller_hooks_source(cluster)
    hooks_configmap = _cluster_agenticsre_teller_hooks_configmap(cluster, deployment)
    injection_so = _cluster_teller_injection_so_path(teller_code)
    cupti_lib_paths = _cluster_teller_cupti_lib_paths(cluster)
    ld_library_path_prefix = ":".join(cupti_lib_paths)
    launcher = _cluster_teller_launcher(cluster)
    python_bin = str(cluster.get("teller_python_bin") or os.environ.get("TELLER_PYTHON_BIN") or "python3").strip() or "python3"
    teller_run = "uv run teller run" if launcher == "uv" else f"{python_bin} -m teller.cli run"
    launcher_probe = (
        f"export PYTHONPATH={shlex.quote(teller_src)}:${{PYTHONPATH:-}}; "
        f"cd {shlex.quote(teller_code)} && "
        "python3 -m teller.cli run --help >/dev/null 2>&1 && echo TELLER_LAUNCHER_OK"
    )
    run_id = f"{deployment}-$(date +%Y%m%d-%H%M%S)"
    wrapped_command = (
        "cd {teller_code} && "
        "export PYTHONPATH={teller_src}:${{PYTHONPATH:-}} && "
        "export PYTHONPATH={hooks_path}:${{PYTHONPATH:-}} && "
        "export LD_LIBRARY_PATH={ld_library_path_prefix}:${{LD_LIBRARY_PATH:-}} && "
        "{nvtx_bootstrap} && "
        "export TELLER_DATA_DIR={runtime_trace_root} && "
        "export AGENTICSRE_TELLER_HOOKS=1 && "
        "export AGENTICSRE_TELLER_TRACE_DIR={runtime_trace_root}/trace && "
        "export AGENTICSRE_TELLER_PYTORCH_HOOK=${{AGENTICSRE_TELLER_PYTORCH_HOOK:-1}} && "
        "export AGENTICSRE_TELLER_PYTORCH_SAMPLE=${{AGENTICSRE_TELLER_PYTORCH_SAMPLE:-20}} && "
        "export TELLER_TASK_TYPE=vllm && "
        "export TELLER_TAG={cluster_id}/{deployment} && "
        "{teller_run} --run-id {run_id} --task-type vllm --tag {cluster_id}/{deployment} --so {injection_so} -- "
        "{python_bin} -m vllm.entrypoints.openai.api_server "
        "--model {model} --host 0.0.0.0 --port 8000"
    ).format(
        teller_code=shlex.quote(teller_code),
        teller_src=shlex.quote(teller_src),
        hooks_path=shlex.quote(hooks_path),
        ld_library_path_prefix=shlex.quote(ld_library_path_prefix),
        nvtx_bootstrap=_teller_nvtx_bootstrap_command(),
        runtime_trace_root=shlex.quote(runtime_trace_root),
        cluster_id=shlex.quote(cluster_id),
        deployment=shlex.quote(deployment),
        teller_run=teller_run,
        injection_so=shlex.quote(injection_so),
        python_bin=shlex.quote(python_bin),
        run_id=shlex.quote(run_id),
        model=shlex.quote(model),
    )
    patch = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [{
                        "name": container,
                        "command": ["/bin/sh", "-lc"],
                        "args": [wrapped_command],
                        "env": [
                            {"name": "TELLER_ENABLE", "value": "1"},
                            {"name": "PYTHONPATH", "value": f"{hooks_path}:{teller_src}"},
                            {"name": "LD_LIBRARY_PATH", "value": ld_library_path_prefix},
                            {"name": "TELLER_NVTX_LIB", "value": "auto"},
                            {"name": "TELLER_DATA_DIR", "value": runtime_trace_root},
                            {"name": "AGENTICSRE_TELLER_HOOKS", "value": "1"},
                            {"name": "AGENTICSRE_TELLER_TRACE_DIR", "value": f"{runtime_trace_root}/trace"},
                            {"name": "AGENTICSRE_TELLER_PYTORCH_HOOK", "value": "1"},
                            {"name": "AGENTICSRE_TELLER_PYTORCH_SAMPLE", "value": "20"},
                            {"name": "TELLER_TASK_TYPE", "value": "vllm"},
                            {"name": "TELLER_TAG", "value": f"{cluster_id}/{deployment}"},
                            {"name": "TELLER_CUPTI_SO", "value": injection_so},
                            {"name": "VLLM_WORKER_MULTIPROC_METHOD", "value": "spawn"},
                        ],
                        "volumeMounts": [
                            {"name": "teller-trace", "mountPath": runtime_trace_root},
                            {"name": "teller-code", "mountPath": teller_code},
                            {"name": "agenticsre-teller-hooks", "mountPath": hooks_path},
                        ],
                    }],
                    "volumes": [
                        {"name": "teller-trace", "hostPath": {"path": trace_host_path, "type": "DirectoryOrCreate"}},
                        {"name": "teller-code", "hostPath": {"path": teller_code_host_path, "type": "Directory"}},
                        (
                            {"name": "agenticsre-teller-hooks", "configMap": {"name": hooks_configmap}}
                            if hooks_source == "configmap" else
                            {"name": "agenticsre-teller-hooks", "hostPath": {"path": hooks_host_path, "type": "Directory"}}
                        ),
                    ],
                }
            }
        }
    }
    commands = [
        (
            f"kubectl -n {shlex.quote(ns)} create configmap {shlex.quote(hooks_configmap)} "
            f"--from-file=sitecustomize.py=tools/teller/sitecustomize.py "
            f"--from-file=vllm_http_pytorch_hooks.py=tools/teller/vllm_http_pytorch_hooks.py "
            "--dry-run=client -o yaml | kubectl apply -f -"
        ) if hooks_source == "configmap" else
        f"rsync -a tools/teller/ <every-k8s-node>:{shlex.quote(hooks_host_path)}/",
        f"kubectl -n {shlex.quote(ns)} get deploy {shlex.quote(deployment)} -o yaml > /tmp/{deployment}.before-teller.yaml",
        f"kubectl -n {shlex.quote(ns)} patch deploy {shlex.quote(deployment)} --type merge -p {shlex.quote(json.dumps(patch, ensure_ascii=False))}",
        f"kubectl -n {shlex.quote(ns)} rollout status deploy/{shlex.quote(deployment)} --timeout=300s",
        f"kubectl -n {shlex.quote(ns)} logs deploy/{shlex.quote(deployment)} --tail=80 | grep -E 'teller|nvtx|request_ids' || true",
    ]
    checks = [
        f"kubectl -n {shlex.quote(ns)} exec deploy/{shlex.quote(deployment)} -- sh -lc {shlex.quote('test -d ' + shlex.quote(teller_code) + ' && echo TELLER_CODE_OK')}",
        f"kubectl -n {shlex.quote(ns)} exec deploy/{shlex.quote(deployment)} -- sh -lc {shlex.quote('test -f ' + shlex.quote(hooks_path + '/sitecustomize.py') + ' && echo AGENTICSRE_TELLER_HOOKS_OK')}",
        f"kubectl -n {shlex.quote(ns)} exec deploy/{shlex.quote(deployment)} -- sh -lc {shlex.quote(launcher_probe)}",
        f"kubectl -n {shlex.quote(ns)} exec deploy/{shlex.quote(deployment)} -- sh -lc {shlex.quote('command -v uv && uv run teller --help >/dev/null 2>&1 || true')}",
        f"find {shlex.quote(runtime_trace_root)} -name teller_trace.json -o -name '*.jsonl' | tail -20",
        f"grep -R \"step_execute_model\\[request_ids:\" {shlex.quote(runtime_trace_root)} 2>/dev/null | head",
    ]
    return {
        "mode": "real_teller_vllm_wrapper",
        "cluster_id": cluster_id,
        "namespace": ns,
        "deployment": deployment,
        "container": container,
        "model": model,
        "launcher": launcher,
        "teller_code_path": teller_code,
        "teller_code_host_path": teller_code_host_path,
        "agenticsre_teller_hooks_path": hooks_path,
        "agenticsre_teller_hooks_host_path": hooks_host_path,
        "agenticsre_teller_hooks_source": hooks_source,
        "agenticsre_teller_hooks_configmap": hooks_configmap if hooks_source == "configmap" else "",
        "teller_injection_so_path": injection_so,
        "teller_cupti_lib_paths": cupti_lib_paths,
        "trace_dir": trace_dir,
        "trace_root": local_trace_root,
        "runtime_trace_root": runtime_trace_root,
        "trace_host_path": trace_host_path,
        "wrapped_command": wrapped_command,
        "alternate_commands": [
            wrapped_command.replace(teller_run, "uv run teller run"),
            wrapped_command.replace(teller_run, f"{python_bin} -m teller.cli run"),
        ],
        "commands": commands,
        "verification": checks,
        "preflight": _run_teller_vllm_preflight(cluster, namespace=ns) if include_preflight else {
            "status": "skipped",
            "checks": [],
            "summary": {"required_passed": 0, "required_total": 0},
            "message": "Dashboard 加载时跳过 TELLER 预检；点击“接入计划”或“预检”再执行完整只读检查。",
        },
        "capabilities": [
            "vLLM execute_model 请求级 request_ids NVTX range（依赖 TELLER hook 与注入库同时记录 NVTX 文本）",
            "HTTP/API server 入站 span（AgenticSRE TELLER hooks，写入 agenticsre_http_spans_pid*.jsonl）",
            "PyTorch Module 前端 span（可采样，写入 agenticsre_pytorch_spans_pid*.jsonl）",
            "CUDA Runtime / Driver / Kernel CUPTI 事件",
            "多 PID / 多 rank trace merge",
            "stdout.log 与 trace meta 关联",
        ],
        "limitations": [
            "需要把 TELLER_Code 从 K8s 节点 hostPath 挂载到 vLLM 容器路径 teller_code_path",
            "建议用 ConfigMap 挂载 AgenticSRE tools/teller hook；如使用 hostPath，必须保证每个 K8s 节点上的 hook 文件一致",
            "需要把 vLLM 容器 trace 目录写到可被采集或同步到 AgenticSRE 的 hostPath/PVC",
            "需要 vLLM 容器镜像包含 TELLER 代码源码或包，并能通过 python -m teller.cli 或 uv run teller 启动",
            "需要 CUDA CUPTI runtime 与 TELLER 注入库 libcupti_trace_injection_with_nvtx.so",
            "如果镜像只提供 libnvToolsExt.so.1，启动命令会自动创建 /tmp/teller-nvtx/libnvToolsExt.so 兼容链接",
            "如果 raw JSONL 只有 RUNTIME/DRIVER/KERNEL 而没有 request_ids，说明 CUPTI 注入库尚未把 NVTX range 文本写入 trace",
            "真实启用会改变 vLLM Deployment 启动命令，必须先备份并通过 rollout 验证",
            "多 Pod/多节点建议为每个实例设置独立 run_id/tag 并挂载共享 trace 目录",
        ],
    }


def _teller_preflight_status_from_checks(checks: List[Dict[str, Any]]) -> str:
    if not checks:
        return "unknown"
    required = [c for c in checks if c.get("required") is not False]
    if required and all(c.get("ok") for c in required):
        return "ready"
    if any(c.get("ok") for c in required):
        return "partial"
    return "blocked"


def _run_teller_vllm_preflight(cluster: Dict[str, Any], *, namespace: str = "") -> Dict[str, Any]:
    """Run read-only checks for enabling request-level TELLER on a vLLM Deployment."""
    cluster_id = str(cluster.get("id") or "default")
    ns = namespace or str(cluster.get("default_namespace") or "llm-k8s")
    deployment = str(cluster.get("vllm_deployment") or cluster.get("deployment") or "vllm-server")
    container = str(cluster.get("vllm_container") or cluster.get("container") or "vllm")
    teller_code = _cluster_teller_code_path(cluster)
    trace_dir = _cluster_teller_trace_dir(cluster)
    local_trace_root = _cluster_teller_trace_root(trace_dir)
    runtime_trace_root = _cluster_teller_runtime_trace_root(cluster, local_trace_root)
    trace_host_path = _cluster_teller_trace_host_path(cluster, runtime_trace_root)
    teller_code_host_path = _cluster_teller_code_host_path(cluster, teller_code)
    teller_src = str(Path(teller_code) / "src")
    injection_so = _cluster_teller_injection_so_path(teller_code)
    cupti_lib_paths = _cluster_teller_cupti_lib_paths(cluster)

    checks: List[Dict[str, Any]] = []

    def add_check(
        check_id: str,
        label: str,
        result: Dict[str, Any],
        *,
        required: bool = True,
        ok_when_stdout: Optional[str] = None,
        remediation: str = "",
    ) -> None:
        stdout = str(result.get("stdout") or "")
        stderr = str(result.get("stderr") or "")
        ok = bool(result.get("ok"))
        if ok_when_stdout is not None:
            ok = ok and ok_when_stdout in stdout
        checks.append({
            "id": check_id,
            "label": label,
            "ok": ok,
            "required": required,
            "command": result.get("command") or "",
            "stdout": stdout[-1200:],
            "stderr": stderr[-1200:],
            "remediation": remediation,
        })

    deploy_res = _shell_sync(
        f"kubectl -n {shlex.quote(ns)} get deploy {shlex.quote(deployment)} -o json",
        timeout=30,
        cluster_id=cluster_id,
    )
    add_check("deployment", "vLLM Deployment 存在", deploy_res, remediation=f"确认命名空间 {ns} 中存在 {deployment}。")
    pod_res = _shell_sync(
        f"kubectl -n {shlex.quote(ns)} get pods -l app={shlex.quote(deployment)} -o json",
        timeout=30,
        cluster_id=cluster_id,
    )
    if not pod_res.get("ok") or not (pod_res.get("stdout") or "").strip():
        pod_res = _shell_sync(
            f"kubectl -n {shlex.quote(ns)} get pods -l app=vllm-server -o json",
            timeout=30,
            cluster_id=cluster_id,
        )
    pod_names: List[str] = []
    node_names: List[str] = []
    running_pod = ""
    try:
        payload = json.loads(pod_res.get("stdout") or "{}")
        for item in payload.get("items") or []:
            name = ((item.get("metadata") or {}).get("name") or "").strip()
            phase = ((item.get("status") or {}).get("phase") or "").strip()
            node = ((item.get("spec") or {}).get("nodeName") or "").strip()
            if name:
                pod_names.append(name)
            if node:
                node_names.append(node)
            if not running_pod and name and phase == "Running":
                running_pod = name
    except Exception:
        pass
    add_check(
        "running_pod",
        "存在运行中的 vLLM Pod",
        {**pod_res, "stdout": ", ".join(pod_names) or pod_res.get("stdout") or ""},
        ok_when_stdout=running_pod if running_pod else "__missing__",
        remediation="先恢复 vLLM Pod 到 Running，再接入请求级 trace。",
    )
    exec_target = f"pod/{running_pod}" if running_pod else f"deploy/{deployment}"

    code_res = _shell_sync(
        f"kubectl -n {shlex.quote(ns)} exec {shlex.quote(exec_target)} -c {shlex.quote(container)} -- sh -lc {shlex.quote(f'test -d {shlex.quote(teller_code)} && echo TELLER_CODE_OK || true')}",
        timeout=30,
        cluster_id=cluster_id,
    )
    add_check(
        "teller_code",
        "容器内可访问 TELLER_Code",
        code_res,
        ok_when_stdout="TELLER_CODE_OK",
        remediation=f"将 TELLER_Code 通过 hostPath/镜像挂载到 {teller_code}。",
    )
    launcher_cmd = (
        f"export PYTHONPATH={shlex.quote(teller_src)}:${{PYTHONPATH:-}}; "
        f"cd {shlex.quote(teller_code)} 2>/dev/null || true; "
        "if python3 -m teller.cli run --help >/dev/null 2>&1; then "
        "echo TELLER_LAUNCHER_OK; "
        "elif python -m teller.cli run --help >/dev/null 2>&1; then "
        "echo TELLER_LAUNCHER_OK; "
        "fi"
    )
    launcher_res = _shell_sync(
        f"kubectl -n {shlex.quote(ns)} exec {shlex.quote(exec_target)} -c {shlex.quote(container)} -- sh -lc {shlex.quote(launcher_cmd)}",
        timeout=30,
        cluster_id=cluster_id,
    )
    add_check(
        "launcher",
        "TELLER 启动器可用",
        launcher_res,
        ok_when_stdout="TELLER_LAUNCHER_OK",
        remediation=f"挂载 {teller_code}/src 或安装 TELLER Python 包，使 python -m teller.cli run --help 可执行。",
    )
    uv_res = _shell_sync(
        f"kubectl -n {shlex.quote(ns)} exec {shlex.quote(exec_target)} -c {shlex.quote(container)} -- sh -lc {shlex.quote('command -v uv && echo UV_OK || true')}",
        timeout=30,
        cluster_id=cluster_id,
    )
    add_check(
        "uv",
        "容器内可执行 uv",
        uv_res,
        required=False,
        ok_when_stdout="UV_OK",
        remediation="可选项。若使用 python -m teller.cli 启动，则无需安装 uv。",
    )
    cupti_find_cmd = " ".join(shlex.quote(path) for path in cupti_lib_paths + ["/usr/local/cuda", "/usr"])
    cupti_probe_cmd = f"find {cupti_find_cmd} -name 'libcupti.so*' 2>/dev/null | head -20"
    nvtx_probe_cmd = f"find {cupti_find_cmd} -name 'libnvToolsExt.so*' 2>/dev/null | head -20"
    cupti_runtime_res = _shell_sync(
        f"kubectl -n {shlex.quote(ns)} exec {shlex.quote(exec_target)} -c {shlex.quote(container)} -- sh -lc {shlex.quote(cupti_probe_cmd)}",
        timeout=30,
        cluster_id=cluster_id,
    )
    add_check(
        "cupti_runtime",
        "CUDA CUPTI runtime 可用",
        cupti_runtime_res,
        ok_when_stdout="libcupti.so",
        remediation="安装 CUDA CUPTI runtime，或设置 teller_cupti_lib_paths 指向 libcupti.so* 所在目录。",
    )
    nvtx_runtime_res = _shell_sync(
        f"kubectl -n {shlex.quote(ns)} exec {shlex.quote(exec_target)} -c {shlex.quote(container)} -- sh -lc {shlex.quote(nvtx_probe_cmd)}",
        timeout=30,
        cluster_id=cluster_id,
    )
    add_check(
        "nvtx_runtime",
        "CUDA NVTX runtime 可用",
        nvtx_runtime_res,
        ok_when_stdout="libnvToolsExt.so",
        remediation="安装 CUDA NVTX runtime，或设置 teller_cupti_lib_paths 指向 libnvToolsExt.so* 所在目录。",
    )
    injection_res = _shell_sync(
        f"kubectl -n {shlex.quote(ns)} exec {shlex.quote(exec_target)} -c {shlex.quote(container)} -- sh -lc {shlex.quote(f'test -f {shlex.quote(injection_so)} && echo {shlex.quote(injection_so)} || find {shlex.quote(teller_code)} /opt/teller -name libcupti_trace_injection_with_nvtx.so 2>/dev/null | head -20')}",
        timeout=30,
        cluster_id=cluster_id,
    )
    add_check(
        "teller_injection",
        "TELLER CUPTI 注入库可用",
        injection_res,
        ok_when_stdout="libcupti_trace_injection_with_nvtx.so",
        remediation="在 TELLER_Code 中构建 libcupti_trace_injection_with_nvtx.so，或挂载已有构建产物。",
    )
    trace_res = _shell_sync(
        f"kubectl -n {shlex.quote(ns)} exec {shlex.quote(exec_target)} -c {shlex.quote(container)} -- sh -lc {shlex.quote(f'mkdir -p {shlex.quote(runtime_trace_root)} && test -w {shlex.quote(runtime_trace_root)} && echo TRACE_WRITABLE || true')}",
        timeout=30,
        cluster_id=cluster_id,
    )
    add_check(
        "trace_dir",
        "Trace 目录可写",
        trace_res,
        ok_when_stdout="TRACE_WRITABLE",
        remediation=f"挂载可写 hostPath/PVC 到容器 {runtime_trace_root}，节点路径建议为 {trace_host_path}。",
    )
    request_ctx = _summarize_teller_trace(trace_dir).get("request_context") or {}
    checks.append({
        "id": "request_context",
        "label": "已有请求级 request_id trace",
        "ok": bool(request_ctx.get("available")),
        "required": False,
        "command": f"parse {trace_dir}",
        "stdout": request_ctx.get("message") or "",
        "stderr": "",
        "remediation": "接入 teller run 并产生 vLLM 请求后，该项应变为通过。",
    })

    return {
        "status": _teller_preflight_status_from_checks(checks),
        "cluster_id": cluster_id,
        "namespace": ns,
        "deployment": deployment,
        "container": container,
        "pod": running_pod,
        "nodes": sorted(set(node_names)),
        "teller_code_path": teller_code,
        "teller_code_host_path": teller_code_host_path,
        "teller_injection_so_path": injection_so,
        "teller_cupti_lib_paths": cupti_lib_paths,
        "trace_dir": trace_dir,
        "trace_root": local_trace_root,
        "runtime_trace_root": runtime_trace_root,
        "trace_host_path": trace_host_path,
        "checks": checks,
        "summary": {
            "required_passed": sum(1 for c in checks if c.get("required") is not False and c.get("ok")),
            "required_total": sum(1 for c in checks if c.get("required") is not False),
            "request_context_available": bool(request_ctx.get("available")),
        },
    }


def _llm_fault_environment() -> Dict[str, Any]:
    targets = [
        {
            "name": "T4-127",
            "host": os.environ.get("LLM_FAULT_T4_127_HOST", ""),
            "user": os.environ.get("LLM_FAULT_T4_127_USER", "root"),
            "gpu": "NVIDIA T4 x3",
            "role": "vLLM 推理服务 / 负载节点",
        },
        {
            "name": "T4-128",
            "host": os.environ.get("LLM_FAULT_T4_128_HOST", ""),
            "user": os.environ.get("LLM_FAULT_T4_128_USER", "root"),
            "gpu": "NVIDIA T4 x3",
            "role": "vLLM 推理服务 / 对照节点",
        },
    ]
    extra_host = os.environ.get("LLM_FAULT_EXTRA_HOST", "").strip()
    if extra_host:
        targets.insert(0, {
            "name": os.environ.get("LLM_FAULT_EXTRA_NAME", "GPU validation target"),
            "host": extra_host,
            "user": os.environ.get("LLM_FAULT_EXTRA_USER", "").strip(),
            "gpu": os.environ.get("LLM_FAULT_EXTRA_GPU", "").strip(),
            "role": os.environ.get("LLM_FAULT_EXTRA_ROLE", "Runtime-configured GPU validation target"),
        })
    return {
        "jump_host": {
            "host": os.environ.get("LLM_FAULT_JUMP_HOST", ""),
            "user": os.environ.get("LLM_FAULT_JUMP_USER", ""),
            "network": os.environ.get("LLM_FAULT_JUMP_NETWORK", "校园网直连"),
        },
        "targets": targets,
        "credential_mode": "passwords are runtime-only; do not persist in AgenticSRE",
    }

from configs.config_loader import get_config, reload_config
from tools import build_tool_registry, LLMClient
from agents import DetectionAgent, AlertAgent
from agents.alert_compression import CompressionStore, compress_alerts, propose_relations
from agents.alert_semantics import enrich_retrieval, build_scag, kubernetes_topology
from orchestrator.pipeline import Pipeline
from orchestrator.daemon import Daemon

logger = logging.getLogger(__name__)

# ── FastAPI App ──
app = FastAPI(
    title="AgenticSRE Dashboard",
    version="1.0.0",
    default_response_class=_SafeJSONResponse,
)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if not _auth_enabled() or _is_public_path(request.url.path):
        return await call_next(request)
    if _get_auth_session(request) or _bearer_token_valid(request):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return RedirectResponse(url="/login", status_code=303)

# ── Shared State ──
_state = {
    "config": None,
    "pipeline": None,
    "daemon": None,
    "daemon_thread": None,
    "detection_signals": deque(maxlen=200),
    "rca_runs": {},  # run_id → dict
    "rca_dedup": {},  # caller-provided dedup key → active run_id
    "pipeline_logs": deque(maxlen=500),
    "daemon_logs": deque(maxlen=500),
    "sse_subscribers": [],
    "prometheus_url": None,
    "otel_prometheus_url": None,
    "jaeger_url": None,
    "fault_experiment_tasks": {},
    "fault_experiment_cancel": {},
    "fault_mode_registry": None,
    "ai_event_refresh_tasks": {},
}

_RCA_RUN_DEDUP_LOCK = threading.Lock()
_RCA_HISTORY_LOCK = threading.RLock()
_RCA_HISTORY_LOAD_FAILED = False


def _fault_mode_registry() -> FaultModeRegistry:
    """Return the process-local catalog registry used by fault-lab APIs."""
    registry = _state.get("fault_mode_registry")
    if registry is None:
        registry = FaultModeRegistry(root_dir=ROOT_DIR)
        _state["fault_mode_registry"] = registry
    return registry

AUTH_SESSION_COOKIE = "agenticsre_session"
AUTH_USER_ENV = "AGENTICSRE_AUTH_USERNAME"
AUTH_PASSWORD_ENV = "AGENTICSRE_AUTH_PASSWORD"
AUTH_TOKEN_ENV = "AGENTICSRE_AUTH_TOKEN"
AUTH_ENABLED_ENV = "AGENTICSRE_AUTH_ENABLED"
AUTH_SESSION_TTL_ENV = "AGENTICSRE_AUTH_SESSION_TTL_SECONDS"
_auth_sessions: Dict[str, Dict[str, Any]] = {}


_RCA_HISTORY_FILE = ROOT_DIR / "data" / "rca_history.json"
_PLATFORM_CONFIG_FILE = ROOT_DIR / "data" / "platform_config.json"
_AUTH_CONFIG_FILE = ROOT_DIR / "data" / "auth_config.json"
_DETECTION_CONFIG_FILE = ROOT_DIR / "data" / "detection_config.json"
_FAULT_TARGETS_FILE = ROOT_DIR / "data" / "cluster_profiles.json"
_FAULT_RUNS_FILE = ROOT_DIR / "data" / "fault_runs.json"
_FAULT_RUNS_LOCK = threading.Lock()
_CONFIG_WRITE_LOCK = threading.RLock()
_FAULT_EXPERIMENTS_FILE = ROOT_DIR / "data" / "fault_experiments.json"
_FAULT_EXPERIMENT_ARTIFACT_DIR = ROOT_DIR / "data" / "fault_experiments"
_AI_EVENTS_FILE = ROOT_DIR / "data" / "ai_events.json"
_ALERT_EVENTS_FILE = ROOT_DIR / "data" / "alert_events.json"
_AI_EVENT_DEFAULT_WINDOW_SECONDS = 15 * 60
_HEAL_RUNS_FILE = ROOT_DIR / "data" / "heal_runs.json"
_HEAL_ARTIFACT_DIR = ROOT_DIR / "data" / "heal_runs"
_SKILL_WORKFLOW_RUNS_FILE = ROOT_DIR / "data" / "skill_workflow_runs.json"
_SKILL_WORKFLOW_ARTIFACT_DIR = ROOT_DIR / "data" / "skill_workflow_runs"
_ALERT_LIST_TIMEOUT_SECONDS = float(os.environ.get("AGENTICSRE_ALERT_LIST_TIMEOUT_SECONDS", "8"))
_ALERT_SCAN_TIMEOUT_SECONDS = float(os.environ.get("AGENTICSRE_ALERT_SCAN_TIMEOUT_SECONDS", "12"))
_ALERT_LIST_CACHE_TTL_SECONDS = float(os.environ.get("AGENTICSRE_ALERT_LIST_CACHE_TTL_SECONDS", "120"))
_ALERT_LIST_CACHE: Dict[str, Any] = {"cluster_id": "", "namespace": "", "updated_at": 0.0, "payload": None}


def _alert_cache_payload(namespace: str, *, cluster_id: str = "", error: str = "") -> Optional[Dict[str, Any]]:
    payload = _ALERT_LIST_CACHE.get("payload")
    updated_at = float(_ALERT_LIST_CACHE.get("updated_at") or 0)
    if (
        not payload
        or _ALERT_LIST_CACHE.get("namespace", "") != (namespace or "")
        or _ALERT_LIST_CACHE.get("cluster_id", "") != (cluster_id or "")
    ):
        return None
    if time.time() - updated_at > _ALERT_LIST_CACHE_TTL_SECONDS:
        return None
    cached = dict(payload)
    cached["degraded"] = True
    cached["cached"] = True
    cached["cache_age_seconds"] = round(time.time() - updated_at, 1)
    cached["warning"] = error or "告警采集超时，已返回最近一次成功采集结果。"
    return cached


def _empty_alert_payload(namespace: str, *, cluster_id: str = "", error: str = "") -> Dict[str, Any]:
    return {
        "alerts": [],
        "total": 0,
        "sources": [],
        "namespace": namespace or "",
        "cluster_id": cluster_id or "",
        "degraded": True,
        "cached": False,
        "warning": error or "告警采集超时，当前没有可用缓存。",
    }


def _load_auth_config_file() -> Dict[str, Any]:
    if not _AUTH_CONFIG_FILE.exists():
        return {}
    try:
        _ensure_private_config_permissions(_AUTH_CONFIG_FILE)
        data = json.loads(_AUTH_CONFIG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("Failed to load auth config file: %s", e)
        return {}


def _auth_setting(env_name: str, file_key: str, default: str = "") -> str:
    env_value = os.environ.get(env_name)
    if env_value is not None:
        return env_value
    value = _load_auth_config_file().get(file_key, default)
    return str(value) if value is not None else default


def _auth_enabled() -> bool:
    value = _auth_setting(AUTH_ENABLED_ENV, "enabled", "0").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _auth_username() -> str:
    return _auth_setting(AUTH_USER_ENV, "username", "admin")


def _auth_password() -> str:
    return _auth_setting(AUTH_PASSWORD_ENV, "password", "")


def _auth_token() -> str:
    return _auth_setting(AUTH_TOKEN_ENV, "token", "")


def _auth_session_ttl() -> int:
    try:
        return max(300, int(_auth_setting(AUTH_SESSION_TTL_ENV, "session_ttl_seconds", "28800")))
    except ValueError:
        return 28800


def _hash_auth_session(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_auth_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    _auth_sessions[_hash_auth_session(token)] = {
        "username": username,
        "created_at": time.time(),
        "expires_at": time.time() + _auth_session_ttl(),
    }
    return token


def _get_auth_session(request: Request) -> Optional[Dict[str, Any]]:
    token = request.cookies.get(AUTH_SESSION_COOKIE, "")
    if not token:
        return None
    key = _hash_auth_session(token)
    session = _auth_sessions.get(key)
    if not session:
        return None
    if float(session.get("expires_at", 0)) < time.time():
        _auth_sessions.pop(key, None)
        return None
    return session


def _delete_auth_session(request: Request) -> None:
    token = request.cookies.get(AUTH_SESSION_COOKIE, "")
    if token:
        _auth_sessions.pop(_hash_auth_session(token), None)


def _bearer_token_valid(request: Request) -> bool:
    expected = _auth_token()
    if not expected:
        return False
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return False
    actual = header.split(" ", 1)[1].strip()
    return hmac.compare_digest(actual, expected)


def _is_public_path(path: str) -> bool:
    return (
        path == "/login"
        or path == "/favicon.ico"
        or path.startswith("/static/")
        or path in {
            "/api/auth/login",
            "/api/auth/logout",
            "/api/auth/me",
            "/api/health",
        }
    )

_HIGH_RISK_LLM_FAULT_TYPES = {
    "weight_corruption",
    "tokenizer_error",
    "process_hang",
    "model_permission_error",
    "http_503",
    "gpu_clock_throttle",
    "rdma_failure",
    "cuda_ctx_corruption",
    "disk_pressure",
    "fd_exhaustion",
}


def _llm_fault_requires_risk_ack(fault_type: str = "", severity: str = "") -> bool:
    """Use scenario metadata as the primary risk gate, with legacy type overrides."""
    severity_value = str(severity or "").strip().lower()
    return severity_value in {"critical", "high"} or str(fault_type or "").strip().lower() in _HIGH_RISK_LLM_FAULT_TYPES


def _llm_fault_injector_target(fault_type: str, deployment: str, service: str = "") -> str:
    """Select the concrete K8s object targeted by the injector implementation."""
    if str(fault_type or "") in {"endpoint_timeout", "http_503"} and service:
        return service
    return deployment


def _llm_fault_applicable_targets(scenario: Dict[str, Any]) -> List[str]:
    """Return topology target kinds that can reasonably execute this LLM fault."""
    fault_type = str(scenario.get("fault_type") or "").lower()
    layer = str(scenario.get("layer") or "").lower()
    targets = {"cluster"}

    if layer == "software":
        targets.update({"pod", "container", "model"})
        if fault_type in {
            "request_queue_overflow",
            "continuous_batch_disruption",
            "endpoint_timeout",
            "kv_cache_exhaustion",
            "model_load_failure",
            "tokenizer_error",
            "weight_corruption",
            "model_permission_error",
            "prefill_decode_misclassification",
            "attention_backend_mismatch",
            "state_contamination",
        }:
            targets.add("service")
    elif layer == "network":
        targets.update({"service", "edge", "pod", "model"})
        if fault_type in {"rdma_failure", "tp_communication_loss", "nccl_failure"}:
            targets.update({"node", "gpu"})
    elif layer == "driver":
        targets.update({"gpu", "node", "pod", "container"})
        if fault_type in {"pcie_throttle", "nccl_failure", "rdma_failure"}:
            targets.add("edge")
    elif layer == "os":
        targets.update({"node", "pod", "container"})
        if fault_type in {"gpu_oom", "memory_pressure"}:
            targets.add("gpu")
    else:
        targets.update({"service", "pod", "container", "model", "node", "gpu", "edge"})

    if fault_type in {"http_503", "endpoint_timeout", "packet_loss", "bandwidth_throttle", "grpc_failure"}:
        targets.update({"service", "edge", "model", "target"})
    if fault_type in {"request_queue_overflow", "continuous_batch_disruption", "kv_cache_exhaustion"}:
        targets.add("target")
    if fault_type in {"process_crash", "process_hang"}:
        targets.update({"pod", "container"})
    if fault_type in {"cuda_error", "cuda_ctx_corruption", "gpu_clock_throttle", "gpu_oom", "state_contamination", "cupti_trace_overhead"}:
        targets.update({"gpu", "node"})
    if fault_type in {"disk_pressure", "fd_exhaustion", "cpu_stress", "memory_pressure"}:
        targets.update({"node", "pod", "container"})
    if fault_type in {"model_load_failure", "tokenizer_error", "weight_corruption", "model_permission_error"}:
        targets.update({"model", "pod", "container"})

    return sorted(targets)


def _llm_fault_observation_hints(scenario: Dict[str, Any], target_mode: str) -> List[str]:
    symptoms = [str(x) for x in (scenario.get("expected_symptoms") or []) if x]
    hints = symptoms[:]
    if target_mode == "host":
        hints.extend([
            "重新点击主机探测，检查 GPU、网络工具和 vLLM endpoint 状态",
            "在目标机查看 /tmp/agenticsre-*.log 或 vLLM 服务日志",
            "执行恢复后确认 nvidia-smi、curl /v1/models 与业务请求恢复",
        ])
    else:
        hints.extend([
            "观察 Deployment/Pod 状态、重启次数和事件",
            "观察 vLLM /metrics 中的 waiting requests、TTFT、TPOT、吞吐与错误率",
            "执行恢复后确认 Pod Ready、服务 endpoint 与业务请求恢复",
        ])
    seen = set()
    return [x for x in hints if not (x in seen or seen.add(x))]


def _summarize_llm_fault_effect(payload: Dict[str, Any]) -> Dict[str, Any]:
    scenario = payload.get("scenario") or {}
    status = str(payload.get("status") or "")
    dry_run = status == "dry_run" or bool(payload.get("dry_run"))
    ok = status in {"executed", "dry_run"} and payload.get("returncode", 0) in (0, None)
    if payload.get("result") and isinstance(payload["result"], dict):
        ok = ok and bool(payload["result"].get("success", True))
    if payload.get("results") and isinstance(payload["results"], list):
        ok = ok and all(bool(r.get("success", True)) for r in payload["results"] if isinstance(r, dict))

    target_mode = payload.get("target_mode") or "k8s"
    if target_mode == "host":
        target = payload.get("host_id") or (payload.get("host") or {}).get("id") or (payload.get("host") or {}).get("host") or "-"
    else:
        target = "/".join(str(x) for x in [payload.get("cluster_id") or "default", payload.get("namespace") or "-", payload.get("deployment") or "-"])

    message = payload.get("message") or ""
    result = payload.get("result")
    if isinstance(result, dict):
        message = result.get("message") or message
    elif payload.get("results"):
        messages = [r.get("message") for r in payload["results"] if isinstance(r, dict) and r.get("message")]
        message = "; ".join(messages) or message

    if dry_run:
        outcome = "Dry-run 已生成命令，尚未真实注入。"
    elif ok:
        outcome = "故障命令已执行，需结合业务请求、指标和日志确认传播效果。"
    else:
        outcome = "故障命令执行失败或部分失败，请查看错误输出并恢复现场。"

    return {
        "ok": ok,
        "dry_run": dry_run,
        "outcome": outcome,
        "target": target,
        "status": status or ("dry_run" if dry_run else "unknown"),
        "action": payload.get("action") or "-",
        "fault_type": payload.get("fault_type") or scenario.get("fault_type") or "-",
        "layer": payload.get("layer") or scenario.get("layer") or "-",
        "message": message,
        "expected_detection_time_s": scenario.get("expected_detection_time_s"),
        "observation_hints": _llm_fault_observation_hints(scenario, target_mode),
    }


def _llm_scenario_to_k8s_fault_type(fault_type: str, observation: Optional[Dict[str, Any]] = None) -> str:
    """Map LLM runtime symptoms to existing Kubernetes healing categories."""
    ft = str(fault_type or "").lower()
    obs = observation or {}
    endpoint_status = str(obs.get("endpoint_status") or "").lower()
    if endpoint_status == "unavailable":
        return "ServiceUnavailable"
    if ft in {"kv_cache_exhaustion", "gpu_oom", "cuda_error", "cuda_ctx_corruption"}:
        return "LLMGPUResourcePressure"
    if ft in {"endpoint_timeout", "prefill_decode_misclassification", "attention_backend_mismatch"}:
        return "LLMBusinessSLODegraded"
    if ft in {"request_queue_overflow", "continuous_batch_disruption", "pcie_throttle", "gpu_clock_throttle", "bandwidth_throttle", "disk_pressure", "fd_exhaustion"}:
        return "ResourceExhausted"
    if ft in {"packet_loss", "rdma_failure"}:
        return "HighLatency"
    if ft in {"process_crash", "process_hang", "tokenizer_error", "weight_corruption", "cuda_ctx_corruption", "model_permission_error", "http_503"}:
        return "ServiceUnavailable"
    return "HighLatency"


def _llm_target_mode_label(target_mode: str) -> str:
    return "裸机 GPU 主机" if str(target_mode or "").lower() == "host" else "K8s 集群"


def _build_llm_fault_diagnosis(body: Dict[str, Any], scenario: Dict[str, Any]) -> Dict[str, Any]:
    """Build a deterministic diagnosis from LLM fault result + direct observation."""
    observation = body.get("observation") if isinstance(body.get("observation"), dict) else {}
    effect = body.get("effect_summary") if isinstance(body.get("effect_summary"), dict) else {}
    result = body.get("result") if isinstance(body.get("result"), dict) else {}
    target_mode = str(body.get("target_mode") or result.get("target_mode") or effect.get("target_mode") or "k8s")
    fault_type = str(scenario.get("fault_type") or body.get("fault_type") or result.get("fault_type") or "")
    scenario_name = str(scenario.get("name") or scenario.get("id") or fault_type or "LLM inference fault")
    endpoint_status = str(observation.get("endpoint_status") or "unknown")
    metrics_status = str(observation.get("metrics_status") or "unknown")
    processes = observation.get("processes") if isinstance(observation.get("processes"), list) else []
    gpus = observation.get("gpus") if isinstance(observation.get("gpus"), list) else []
    metrics = observation.get("metrics") if isinstance(observation.get("metrics"), dict) else {}
    extrema = body.get("observation_extrema") if isinstance(body.get("observation_extrema"), dict) else {}
    maxima = extrema.get("max") if isinstance(extrema.get("max"), dict) else {}
    observation_context = body.get("observation_context") if isinstance(body.get("observation_context"), dict) else {}
    context_signals = observation_context.get("signals") if isinstance(observation_context.get("signals"), dict) else {}
    metrics_target = body.get("metrics_target") if isinstance(body.get("metrics_target"), dict) else {}

    def window_max(name: str, value: Any) -> Any:
        candidates = [item for item in (value, maxima.get(name)) if isinstance(item, (int, float))]
        signal_context = context_signals.get(name) if isinstance(context_signals.get(name), dict) else {}
        incident = signal_context.get("incident") if isinstance(signal_context.get("incident"), dict) else {}
        incident_max = incident.get("max")
        if isinstance(incident_max, (int, float)):
            candidates.append(incident_max)
        return max(float(item) for item in candidates) if candidates else value
    process_count = _to_int(observation.get("process_count"), len(processes))
    gpu_count = _to_int(observation.get("gpu_count"), len(gpus))
    metrics_count = _to_int(observation.get("metrics_count"), len(metrics))
    restart_count = _to_int(observation.get("pod_restart_count"), 0)
    terminated_reasons = observation.get("container_terminated_reasons")
    if not isinstance(terminated_reasons, list):
        terminated_reasons = []

    evidence: Dict[str, Any] = {
        "scenario": scenario_name,
        "fault_type": fault_type,
        "target_mode": target_mode,
    }
    evidence_items: List[str] = []
    if effect.get("outcome"):
        evidence["injection_feedback"] = effect.get("outcome")
        evidence_items.append(f"注入反馈: {effect.get('outcome')}")
    if endpoint_status != "unknown":
        evidence["endpoint_status"] = endpoint_status
        evidence_items.append(f"推理接口状态: {endpoint_status}")
    if metrics_status != "unknown":
        evidence["metrics_status"] = metrics_status
        evidence_items.append(f"vLLM 指标接口状态: {metrics_status}")
    if processes or "process_count" in observation:
        evidence["vllm_process_count"] = process_count
        evidence_items.append(f"vLLM 进程数: {process_count}")
    elif observation:
        evidence["vllm_process_count"] = 0
        evidence_items.append("未发现 vLLM 进程")
    if restart_count > 0:
        evidence["pod_restart_count"] = restart_count
        evidence_items.append(f"Pod 重启次数: {restart_count}")
    if terminated_reasons:
        evidence["container_terminated_reasons"] = terminated_reasons
        evidence_items.append(f"容器异常退出: {', '.join(str(x) for x in terminated_reasons)}")
    if gpus:
        avg_util = [
            g.get("utilization_gpu_pct") for g in gpus
            if isinstance(g, dict) and isinstance(g.get("utilization_gpu_pct"), (int, float))
        ]
        if avg_util:
            evidence["avg_gpu_utilization_pct"] = round(sum(avg_util) / len(avg_util), 2)
            evidence_items.append(f"平均 GPU 利用率: {evidence['avg_gpu_utilization_pct']}%")
    waiting = window_max("waiting_requests", metrics.get("vllm:num_requests_waiting", observation.get("waiting_requests")))
    if isinstance(waiting, (int, float)):
        evidence["vllm_num_requests_waiting"] = waiting
        evidence_items.append(f"等待队列请求数: {waiting}")
    running = window_max("running_requests", metrics.get("vllm:num_requests_running", observation.get("running_requests")))
    if isinstance(running, (int, float)):
        evidence["vllm_num_requests_running"] = running
        evidence_items.append(f"运行中请求数: {running}")
    kv_cache_raw = window_max("kv_cache_usage", metrics.get("vllm:gpu_cache_usage_perc", observation.get("kv_cache_usage")))
    kv_cache_pct: Optional[float] = None
    if isinstance(kv_cache_raw, (int, float)):
        kv_cache_pct = float(kv_cache_raw) * 100.0 if 0 <= float(kv_cache_raw) <= 1 else float(kv_cache_raw)
        evidence["kv_cache_usage_pct"] = round(kv_cache_pct, 3)
        evidence_items.append(f"KV Cache 使用率: {kv_cache_pct:.1f}%")
    kv_context = context_signals.get("kv_cache_usage") if isinstance(context_signals.get("kv_cache_usage"), dict) else {}
    kv_baseline = kv_context.get("baseline") if isinstance(kv_context.get("baseline"), dict) else {}
    kv_incident = kv_context.get("incident") if isinstance(kv_context.get("incident"), dict) else {}
    kv_baseline_max = kv_baseline.get("max")
    kv_incident_max = kv_incident.get("max")
    if isinstance(kv_baseline_max, (int, float)):
        evidence["kv_cache_baseline_max_pct"] = round(float(kv_baseline_max), 3)
        evidence_items.append(f"告警前 KV Cache 基线峰值: {float(kv_baseline_max):.1f}%")
    if isinstance(kv_incident_max, (int, float)):
        evidence["kv_cache_incident_max_pct"] = round(float(kv_incident_max), 3)
        evidence_items.append(f"故障窗口 KV Cache 峰值: {float(kv_incident_max):.1f}%")
    if metrics_target:
        evidence["metrics_target"] = metrics_target
    e2e_p95 = window_max("e2e_p95_seconds", metrics.get("vllm:e2e_request_latency_seconds", observation.get("e2e_p95_seconds")))
    if isinstance(e2e_p95, (int, float)):
        evidence["e2e_p95_seconds"] = float(e2e_p95)
        evidence_items.append(f"端到端延迟 P95: {float(e2e_p95):.3f}s")

    confidence = 0.52
    if scenario:
        confidence += 0.12
    if observation:
        confidence += 0.12
    if endpoint_status == "unavailable" or metrics_status == "unavailable":
        confidence += 0.08
    if process_count > 0 or gpu_count > 0 or metrics_count > 0:
        confidence += 0.06
    direct_fault_signal = endpoint_status in {"failed", "unavailable"} or restart_count > 0 or bool(terminated_reasons)
    if direct_fault_signal:
        confidence += 0.1
    if metrics_status == "ok" and isinstance(waiting, (int, float)) and waiting > 0:
        confidence += 0.08
    if isinstance(running, (int, float)) and running > 0:
        confidence += 0.04
    if effect.get("dry_run"):
        confidence = min(confidence, 0.62)
    confidence = min(0.93, round(confidence, 2))
    confidence_raw = confidence
    confidence, confidence_explanation = calibrate_llm_fault_confidence(
        base_confidence=confidence_raw,
        scenario_present=bool(scenario),
        observation_present=bool(observation),
        endpoint_status=endpoint_status,
        metrics_status=metrics_status,
        process_count=process_count,
        gpu_count=gpu_count,
        metrics_count=metrics_count,
        waiting_requests=float(waiting) if isinstance(waiting, (int, float)) else None,
        running_requests=float(running) if isinstance(running, (int, float)) else None,
        dry_run=bool(effect.get("dry_run")),
    )

    k8s_fault_type = _llm_scenario_to_k8s_fault_type(fault_type, observation)
    diagnosis_fault_type = f"LLM::{fault_type or 'unknown'}"
    kv_threshold = float((_ai_event_detection_config().get("rules") or {}).get("kv_cache", 90.0))
    kv_exhaustion_claim = fault_type == "kv_cache_exhaustion"
    kv_exhaustion_confirmed = kv_cache_pct is not None and kv_cache_pct >= kv_threshold
    kv_context_available = bool(metrics_target or kv_context)
    kv_attribution_supported = bool(
        kv_exhaustion_confirmed
        and (
            not kv_context_available
            or metrics_target.get("scope") in {"target", "single_series"}
        )
        and (
            not isinstance(kv_baseline_max, (int, float))
            or float(kv_baseline_max) < kv_threshold
            or (
                isinstance(kv_incident_max, (int, float))
                and float(kv_incident_max) - float(kv_baseline_max) >= 10.0
            )
        )
    )
    if kv_exhaustion_claim and not kv_exhaustion_confirmed:
        diagnosis_fault_type = "LLM::business_performance_degradation"
        k8s_fault_type = "LLMBusinessSLODegraded"
        observed_kv = f"{kv_cache_pct:.1f}%" if kv_cache_pct is not None else "未采集"
        root_cause = (
            f"观测到请求积压或推理延迟退化，但 KV Cache 使用率为 {observed_kv}，"
            f"未达配置门槛 {kv_threshold:g}%，当前证据不能确认 KV Cache 耗尽。"
        )
        confidence = min(confidence, 0.74)
        confidence_explanation = {
            **confidence_explanation,
            "evidence_gate": {
                "claim": "kv_cache_exhaustion",
                "observed_pct": kv_cache_pct,
                "threshold_pct": kv_threshold,
                "confirmed": False,
                "target_scope": metrics_target.get("scope") or "unknown",
                "baseline_max_pct": kv_baseline_max,
                "incident_max_pct": kv_incident_max,
            },
            "caps": [*(confidence_explanation.get("caps") or []), "KV Cache 未达耗尽门槛，置信度上限为 0.74"],
        }
    elif kv_exhaustion_claim and kv_exhaustion_confirmed:
        root_cause = (
            f"{scenario_name} 窗口内目标推理实例 KV Cache 峰值达到 {kv_cache_pct:.1f}%，"
            f"超过配置门槛 {kv_threshold:g}%，并伴随请求排队或调度退化。"
        )
        confidence_explanation = {
            **confidence_explanation,
            "evidence_gate": {
                "claim": "kv_cache_exhaustion",
                "observed_pct": kv_cache_pct,
                "threshold_pct": kv_threshold,
                "confirmed": True,
                "target_scope": metrics_target.get("scope") or "unknown",
                "baseline_max_pct": kv_baseline_max,
                "incident_max_pct": kv_incident_max,
                "attribution_supported": kv_attribution_supported,
            },
        }
        if kv_context_available and not kv_attribution_supported:
            confidence = min(confidence, 0.84)
            confidence_explanation["caps"] = [
                *(confidence_explanation.get("caps") or []),
                "KV Cache 故障已确认，但目标匹配或告警前基线不足以高置信归因本次事件。",
            ]
    elif endpoint_status in {"failed", "unavailable"}:
        root_cause = f"{scenario_name} 后推理 endpoint 不可用，推理服务或依赖资源处于不可服务状态。"
    elif restart_count > 0 or terminated_reasons:
        root_cause = f"{scenario_name} 导致推理容器异常退出或重启，故障已传播到模型服务进程。"
    elif not processes and observation and target_mode == "host":
        root_cause = f"{scenario_name} 后未发现 vLLM 进程，故障可能已传播到推理进程可用性。"
    elif isinstance(waiting, (int, float)) and waiting > 0:
        root_cause = f"{scenario_name} 导致请求排队，推理调度或资源瓶颈正在向业务侧传播。"
    else:
        root_cause = f"{scenario_name} 对 LLM 推理链路造成风险，需结合注入反馈和观测项确认传播范围。"

    if target_mode == "host":
        component = str(body.get("host_id") or result.get("host_id") or (result.get("host") or {}).get("id") or "llm-host")
    else:
        component = str(body.get("deployment") or result.get("deployment") or "vllm-server")

    return {
        "diagnosis_mode": "llm_faultlab",
        "fault_type": diagnosis_fault_type,
        "k8s_fault_type": k8s_fault_type,
        "root_cause": root_cause,
        "root_cause_component": component,
        "affected_services": [component],
        "confidence_raw": confidence_raw,
        "confidence": confidence,
        "confidence_explanation": confidence_explanation,
        "evidence_summary": evidence,
        "evidence": evidence_items,
        "remediation_suggestion": (
            "先执行当前场景恢复动作并复测观测面；K8s 目标可进入自愈计划，裸机目标需通过受控恢复按钮或目标侧服务管理恢复。"
        ),
        "diagnosis_ready": confidence >= _to_float(getattr(_get_config().remediation, "confidence_threshold", 0.85), 0.85),
    }


def _build_llm_host_heal_plan(
    body: Dict[str, Any],
    scenario: Dict[str, Any],
    diagnosis: Dict[str, Any],
    host: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Return guarded host-side recovery guidance without accepting arbitrary shell commands."""
    observation = body.get("observation") if isinstance(body.get("observation"), dict) else {}
    endpoint_status = str(observation.get("endpoint_status") or "unknown")
    metrics_status = str(observation.get("metrics_status") or "unknown")
    processes = observation.get("processes") if isinstance(observation.get("processes"), list) else []
    fault_type = str(scenario.get("fault_type") or "")
    scenario_id = str(scenario.get("id") or "")

    suggestions: List[Dict[str, Any]] = [
        {
            "step": "执行当前智算场景恢复动作",
            "action": "llm_fault_cleanup",
            "scenario_id": scenario_id,
            "risk": "high" if _llm_fault_requires_risk_ack(
                fault_type,
                str(scenario.get("severity") or ""),
            ) else "medium",
            "selection_reason": "复用当前受控的 vLLM 故障注入恢复路径，不接受任意主机命令。",
        },
        {
            "step": "复测 GPU 主机观测面",
            "action": "observe_llm_host",
            "risk": "low",
            "selection_reason": "在同一页面复核 GPU、进程、推理接口和 /metrics 是否恢复。",
        },
    ]
    if endpoint_status == "unavailable" or not processes:
        suggestions.append({
            "step": "恢复目标侧 vLLM 推理服务",
            "action": "manual_or_configured_service_restart",
            "risk": "medium",
            "selection_reason": "推理接口或进程证据显示服务可用性已受影响。",
        })
    if metrics_status == "unavailable":
        suggestions.append({
            "step": "恢复或暴露 vLLM /metrics",
            "action": "metrics_endpoint_check",
            "risk": "low",
            "selection_reason": "需要指标来量化故障向排队、吞吐和错误率症状的传播。",
        })

    return {
        "mode": "host",
        "execution_mode": "guarded_cleanup",
        "target": _redact_host_runtime_fields(host) if host else {"id": body.get("host_id")},
        "diagnosis_gate": {
            "ready": bool(diagnosis.get("diagnosis_ready")),
            "confidence": diagnosis.get("confidence"),
            "confidence_threshold": getattr(_get_config().remediation, "confidence_threshold", 0.85),
            "blockers": [] if diagnosis.get("diagnosis_ready") else ["confidence_below_threshold"],
            "blocker_messages": [] if diagnosis.get("diagnosis_ready") else ["诊断置信度低于配置阈值"],
        },
        "capability": {
            "available": bool(scenario_id),
            "requires_approval": True,
            "requires_host_ack": True,
            "requires_risk_ack": _llm_fault_requires_risk_ack(
                fault_type,
                str(scenario.get("severity") or ""),
            ),
            "real_execution_endpoint": f"/api/llm-faultlab/cleanup/{scenario_id}" if scenario_id else "",
        },
        "suggestions": suggestions,
        "blocked": [],
        "summary": f"故障类型: {diagnosis.get('fault_type')} | 根因: {diagnosis.get('root_cause')} | 目标: {body.get('host_id') or _llm_target_mode_label(body.get('target_mode') or '')}",
    }


def _load_rca_history():
    """Load persisted RCA runs from disk on startup."""
    global _RCA_HISTORY_LOAD_FAILED
    _RCA_HISTORY_LOAD_FAILED = False
    if _RCA_HISTORY_FILE.exists():
        try:
            data = json.loads(_RCA_HISTORY_FILE.read_text(encoding="utf-8"))
            for run in data:
                if run.get("status") in {"queued", "starting", "running"}:
                    run.update(
                        status="failed", requires_manual_review=True,
                        interrupted_at=time.time(),
                        result={"status": "failed", "error_code": "process_interrupted",
                                "error": "服务重启导致诊断中断；已保留过程证据，请复核后重新发起。"},
                    )
                _state["rca_runs"][run["id"]] = run
            _save_rca_history()
            logger.info(f"Loaded {len(data)} RCA runs from disk")
        except Exception as e:
            _RCA_HISTORY_LOAD_FAILED = True
            logger.warning(f"Failed to load RCA history: {e}")


def _save_rca_history(*, required_run_id: Optional[str] = None):
    """Atomically checkpoint active runs and the latest 200 terminal runs."""
    try:
        with _RCA_HISTORY_LOCK:
            runs = list(_state["rca_runs"].values())
            active = [r for r in runs if r.get("status") in {"queued", "starting", "running"}]
            terminal = [r for r in runs if r.get("status") not in {"queued", "starting", "running"}]
            terminal.sort(key=lambda r: r.get("started_at", 0), reverse=True)
            retained = active + terminal[:200]
            if required_run_id and not any(r.get("id") == required_run_id for r in retained):
                return False
            _atomic_write_json(_RCA_HISTORY_FILE, retained)
        return True
    except Exception as e:
        logger.warning(f"Failed to save RCA history: {e}")
        return False


def _atomic_write_json(path: Path, data: Any, *, mode: int = 0o600) -> None:
    """Atomically replace a JSON file and keep configuration data private."""
    serialized = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _CONFIG_WRITE_LOCK:
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        temp_path = Path(temp_name)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                fd = -1
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, path)
            os.chmod(path, mode)
        except Exception:
            if fd >= 0:
                os.close(fd)
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            raise


def _ensure_private_config_permissions(path: Path, *, mode: int = 0o600) -> None:
    """Remove group/other access from an existing local configuration file."""
    if os.name == "nt" or not path.is_file():
        return
    try:
        current_mode = path.stat().st_mode & 0o777
        if current_mode != mode:
            os.chmod(path, mode)
    except OSError as e:
        logger.warning("Failed to harden config file permissions for %s: %s", path, e)


def _config_validation_error(field: str, message: str) -> None:
    raise HTTPException(400, f"{field}: {message}")


def _validate_config_number(
    value: Any,
    field: str,
    *,
    integer: bool = False,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> Any:
    if isinstance(value, bool) or not isinstance(value, int if integer else (int, float)):
        _config_validation_error(field, "must be an integer" if integer else "must be a number")
    numeric = int(value) if integer else float(value)
    if not math.isfinite(float(numeric)):
        _config_validation_error(field, "must be finite")
    if minimum is not None and numeric < minimum:
        _config_validation_error(field, f"must be >= {minimum:g}")
    if maximum is not None and numeric > maximum:
        _config_validation_error(field, f"must be <= {maximum:g}")
    return numeric


def _validate_config_string(value: Any, field: str) -> str:
    if not isinstance(value, str):
        _config_validation_error(field, "must be a string")
    return value


def _validate_config_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        _config_validation_error(field, "must be a boolean")
    return value


def _validate_config_string_list(value: Any, field: str) -> List[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        _config_validation_error(field, "must be an array of strings")
    return list(value)


def _validate_config_bool_map(value: Any, field: str) -> Dict[str, bool]:
    if not isinstance(value, dict) or any(not isinstance(key, str) or not isinstance(item, bool) for key, item in value.items()):
        _config_validation_error(field, "must be an object with boolean values")
    return dict(value)


def _validate_finite_tree(value: Any, field: str) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        _config_validation_error(field, "must not contain non-finite numbers")
    if isinstance(value, dict):
        return {key: _validate_finite_tree(item, f"{field}.{key}") for key, item in value.items()}
    if isinstance(value, list):
        return [_validate_finite_tree(item, f"{field}[{index}]") for index, item in enumerate(value)]
    return copy.deepcopy(value)


def _validate_detection_config_values(raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        _config_validation_error("detection", "must be an object")
    data = copy.deepcopy(raw)
    int_ranges = {
        "default_lookback_m": (1, None),
        "default_ewma_span": (1, None),
        "min_samples": (3, None),
        "confirmation_points": (1, None),
    }
    float_ranges = {
        "default_z_threshold": (0, None),
        "relative_change_threshold": (0, None),
        "spectral_residual_threshold": (0, None),
    }
    for name, (minimum, maximum) in int_ranges.items():
        if name in data:
            data[name] = _validate_config_number(
                data[name], f"detection.{name}", integer=True, minimum=minimum, maximum=maximum
            )
    for name, (minimum, maximum) in float_ranges.items():
        if name in data:
            data[name] = _validate_config_number(
                data[name], f"detection.{name}", minimum=minimum, maximum=maximum
            )
    if "default_algorithm" in data:
        algorithm = _validate_config_string(data["default_algorithm"], "detection.default_algorithm")
        if algorithm not in {"zscore", "iqr", "threshold", "spectral_residual"}:
            _config_validation_error("detection.default_algorithm", "contains an unsupported algorithm")
        data["default_algorithm"] = algorithm
    for name in ("critical_event_reasons", "critical_pod_reasons", "default_detect_methods", "business_services", "db_services"):
        if name in data:
            data[name] = _validate_config_string_list(data[name], f"detection.{name}")
    for name in ("sources_enabled", "categories_enabled"):
        if name in data:
            data[name] = _validate_config_bool_map(data[name], f"detection.{name}")
    if "metric_checks" in data:
        if not isinstance(data["metric_checks"], list) or any(not isinstance(item, dict) for item in data["metric_checks"]):
            _config_validation_error("detection.metric_checks", "must be an array of objects")
        data["metric_checks"] = _validate_finite_tree(data["metric_checks"], "detection.metric_checks")
    if "thresholds" in data:
        if not isinstance(data["thresholds"], dict):
            _config_validation_error("detection.thresholds", "must be an object")
        data["thresholds"] = _validate_finite_tree(data["thresholds"], "detection.thresholds")
    return data


def _validate_platform_config_section(section: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        _config_validation_error(section, "must be an object")
    data = copy.deepcopy(raw)
    if section == "llm":
        for name in ("model", "base_url"):
            if name in data:
                data[name] = _validate_config_string(data[name], f"llm.{name}")
        if "api_key" in data and data["api_key"] is not None:
            data["api_key"] = _validate_config_string(data["api_key"], "llm.api_key")
        if "temperature" in data:
            data["temperature"] = _validate_config_number(data["temperature"], "llm.temperature", minimum=0, maximum=2)
        for name in ("max_tokens", "timeout"):
            if name in data:
                data[name] = _validate_config_number(data[name], f"llm.{name}", integer=True, minimum=1)
        data.pop("api_key_configured", None)
        data.pop("api_key_masked", None)
    elif section == "detection":
        data = _validate_detection_config_values(data)
    elif section == "observability":
        for name in ("prometheus_url", "otel_prometheus_url", "jaeger_url", "grafana_url", "elasticsearch_url"):
            if name in data:
                data[name] = _validate_config_string(data[name], f"observability.{name}")
    elif section == "remediation":
        for name in ("enabled", "dry_run", "require_approval", "canary_enabled", "require_confirmation_for_high_risk", "recommend_only"):
            if name in data:
                data[name] = _validate_config_bool(data[name], f"remediation.{name}")
        for name in ("max_steps", "max_rollback_depth"):
            if name in data:
                data[name] = _validate_config_number(data[name], f"remediation.{name}", integer=True, minimum=1)
        if "confidence_threshold" in data:
            data["confidence_threshold"] = _validate_config_number(
                data["confidence_threshold"], "remediation.confidence_threshold", minimum=0, maximum=1
            )
        for name in ("schedule", "canary_namespace"):
            if name in data:
                data[name] = _validate_config_string(data[name], f"remediation.{name}")
        if "blackout_windows" in data:
            data["blackout_windows"] = _validate_config_string_list(
                data["blackout_windows"], "remediation.blackout_windows"
            )
        if "max_auto_risk_level" in data:
            risk = _validate_config_string(data["max_auto_risk_level"], "remediation.max_auto_risk_level")
            if risk not in {"low", "medium", "high"}:
                _config_validation_error("remediation.max_auto_risk_level", "must be low, medium, or high")
            data["max_auto_risk_level"] = risk
    return data


def _apply_detection_overrides(det: Any, data: Dict[str, Any]) -> None:
    for name in ("sources_enabled", "categories_enabled"):
        value = data.get(name)
        if isinstance(value, dict):
            getattr(det, name).update(value)
    for name in (
        "metric_checks",
        "critical_event_reasons",
        "critical_pod_reasons",
        "default_detect_methods",
        "business_services",
        "db_services",
    ):
        if isinstance(data.get(name), list):
            setattr(det, name, copy.deepcopy(data[name]))
    for name in (
        "default_algorithm",
        "default_lookback_m",
        "default_z_threshold",
        "default_ewma_span",
        "min_samples",
        "confirmation_points",
        "relative_change_threshold",
        "spectral_residual_threshold",
    ):
        if name in data and hasattr(det, name):
            setattr(det, name, data[name])
    if isinstance(data.get("thresholds"), dict):
        det.thresholds.update(copy.deepcopy(data["thresholds"]))


def _save_detection_config():
    """Persist detection config overrides to disk."""
    try:
        cfg = _get_config()
        det = cfg.detection
        data = {
            "sources_enabled": det.sources_enabled,
            "metric_checks": det.metric_checks,
            "critical_event_reasons": det.critical_event_reasons,
            "critical_pod_reasons": det.critical_pod_reasons,
            "default_detect_methods": det.default_detect_methods,
            "default_algorithm": getattr(det, "default_algorithm", "zscore"),
            "default_lookback_m": det.default_lookback_m,
            "default_z_threshold": det.default_z_threshold,
            "default_ewma_span": det.default_ewma_span,
            "min_samples": getattr(det, "min_samples", 12),
            "confirmation_points": getattr(det, "confirmation_points", 1),
            "relative_change_threshold": getattr(det, "relative_change_threshold", 0.0),
            "spectral_residual_threshold": getattr(det, "spectral_residual_threshold", 3.0),
            "categories_enabled": det.categories_enabled,
            "business_services": det.business_services,
            "db_services": det.db_services,
            "thresholds": det.thresholds,
        }
        _atomic_write_json(_DETECTION_CONFIG_FILE, data)
        logger.info("Detection config saved to disk")
    except Exception as e:
        logger.warning(f"Failed to save detection config: {e}")
        raise


def _load_platform_config_file() -> Dict[str, Any]:
    """Load persisted platform configuration-center overrides."""
    if not _PLATFORM_CONFIG_FILE.exists():
        return {}
    try:
        _ensure_private_config_permissions(_PLATFORM_CONFIG_FILE)
        data = json.loads(_PLATFORM_CONFIG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"Failed to load platform config: {e}")
        return {}


def _save_platform_config_file(data: Dict[str, Any]) -> None:
    """Persist platform configuration-center overrides."""
    try:
        _atomic_write_json(_PLATFORM_CONFIG_FILE, data)
    except Exception as e:
        logger.warning(f"Failed to save platform config: {e}")
        raise


def _mask_secret(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:3]}***{text[-4:]}"


def _llm_api_key_configured(value: Any) -> bool:
    """Treat unresolved env placeholders as missing LLM credentials."""
    text = str(value or "").strip()
    if not text:
        return False
    if text.startswith("${") and text.endswith("}"):
        return False
    if text.lower() in {"sk-your-api-key-here", "your-api-key", "change-me"}:
        return False
    return True


def _llm_configured(llm_cfg: Any) -> bool:
    return _llm_api_key_configured(getattr(llm_cfg, "api_key", ""))


def _redact_platform_config(data: Dict[str, Any]) -> Dict[str, Any]:
    """Return persisted config metadata without exposing stored secrets."""
    redacted = json.loads(json.dumps(data, ensure_ascii=False, default=str))
    llm = redacted.get("llm")
    if isinstance(llm, dict) and llm.get("api_key"):
        secret = llm.pop("api_key")
        llm["api_key_configured"] = _llm_api_key_configured(secret)
        llm["api_key_masked"] = _mask_secret(secret)
    return redacted


def _apply_platform_config() -> None:
    """Apply persisted config-center overrides to the in-memory AppConfig."""
    data = _load_platform_config_file()
    if not data:
        return
    cfg = _get_config()

    llm = data.get("llm")
    if isinstance(llm, dict):
        for field_name in ("base_url", "model", "temperature", "max_tokens", "timeout"):
            if field_name in llm and hasattr(cfg.llm, field_name):
                current = getattr(cfg.llm, field_name)
                value = llm[field_name]
                try:
                    if isinstance(current, bool):
                        value = bool(value)
                    elif isinstance(current, int):
                        value = int(value)
                    elif isinstance(current, float):
                        value = float(value)
                except Exception:
                    pass
                setattr(cfg.llm, field_name, value)
        if llm.get("api_key"):
            cfg.llm.api_key = str(llm["api_key"])

    observability = data.get("observability")
    if isinstance(observability, dict):
        obs = cfg.observability
        for field_name in ("prometheus_url", "otel_prometheus_url", "jaeger_url", "grafana_url", "elasticsearch_url"):
            if field_name in observability and hasattr(obs, field_name):
                setattr(obs, field_name, str(observability[field_name] or "").strip())

    detection = data.get("detection")
    if isinstance(detection, dict):
        try:
            _apply_detection_overrides(cfg.detection, _validate_detection_config_values(detection))
        except HTTPException as e:
            logger.warning("Ignoring invalid persisted detection config: %s", e.detail)

    remediation = data.get("remediation")
    if isinstance(remediation, dict):
        rem = cfg.remediation
        for name, value in remediation.items():
            if not hasattr(rem, name):
                continue
            current = getattr(rem, name)
            try:
                if isinstance(current, bool):
                    value = bool(value)
                elif isinstance(current, int):
                    value = int(value)
                elif isinstance(current, float):
                    value = float(value)
                elif isinstance(current, list) and not isinstance(value, list):
                    value = [value]
            except Exception:
                pass
            setattr(rem, name, value)


def _reload_runtime_config() -> None:
    """Reload YAML/env config then re-apply persisted platform overrides."""
    _state["config"] = reload_config()
    if _PLATFORM_CONFIG_FILE.exists():
        _apply_platform_config()
    else:
        _load_detection_config()
    _load_fault_targets()
    _state["pipeline"] = None
    _state["daemon"] = None


def _platform_config_schema() -> Dict[str, Any]:
    """Describe configuration-center parameters for operators and API clients."""
    return {
        "llm": {
            "label": "大模型",
            "description": "RCA、多智能体协作和自愈建议生成使用的大模型连接参数。",
            "fields": {
                "model": {"label": "模型名称", "type": "string", "default": "deepseek-v4-pro", "description": "调用的大模型标识，必须与推理服务实际部署的模型名称一致。"},
                "base_url": {"label": "服务地址", "type": "string", "default": "https://api.deepseek.com/v1", "description": "OpenAI 兼容接口的基础 URL，例如 https://host/v1。"},
                "api_key": {"label": "API Key", "type": "secret", "default": "", "description": "访问大模型服务的凭据。留空保存时保持原值，不会通过接口返回明文。"},
                "temperature": {"label": "采样温度", "type": "number", "default": 0.1, "min": 0, "max": 2, "description": "控制输出随机性。故障诊断建议使用较低值，以提高结果稳定性。"},
                "max_tokens": {"label": "最大输出 Token", "type": "integer", "default": 65536, "min": 1, "description": "单次模型调用允许生成的最大 Token 数，过大会增加延迟和资源消耗。"},
                "timeout": {"label": "请求超时", "type": "integer", "default": 300, "min": 1, "unit": "秒", "description": "单次大模型请求的超时时间。复杂诊断可适当增大。"},
            },
        },
        "detection": {
            "label": "指标异常检测",
            "description": "告警中心指标异常检测的算法和判定参数。",
            "fields": {
                "default_algorithm": {"label": "默认算法", "type": "enum", "default": "zscore", "options": ["zscore", "iqr", "threshold", "spectral_residual"], "description": "未对指标单独指定算法时使用的异常检测算法。"},
                "default_z_threshold": {"label": "Z-Score 阈值", "type": "number", "default": 3.0, "min": 0, "description": "偏离均值超过该标准差倍数时标记异常。值越小越敏感。"},
                "default_lookback_m": {"label": "回看窗口", "type": "integer", "default": 30, "min": 1, "unit": "分钟", "description": "异常检测读取的历史指标窗口长度。"},
                "default_ewma_span": {"label": "EWMA 跨度", "type": "integer", "default": 10, "min": 1, "description": "EWMA 平滑跨度，用于降低短时噪声影响。"},
                "min_samples": {"label": "最少样本数", "type": "integer", "default": 12, "min": 3, "description": "执行统计检测前要求的最少数据点数量。样本不足时不会判定异常。"},
                "confirmation_points": {"label": "连续确认点", "type": "integer", "default": 1, "min": 1, "description": "连续出现多少个异常点后生成告警。增大可减少瞬态抖动告警。"},
                "relative_change_threshold": {"label": "相对变化阈值", "type": "number", "default": 0.0, "min": 0, "description": "相对基线的最小变化比例。0 表示不额外限制。"},
                "spectral_residual_threshold": {"label": "频谱残差阈值", "type": "number", "default": 3.0, "min": 0, "description": "spectral_residual 算法的异常分数阈值。"},
                "sources_enabled": {"label": "检测源开关", "type": "object", "description": "启用或停用 Prometheus、K8s Event、Pod、Node 等检测来源。"},
                "metric_checks": {"label": "指标检查项", "type": "array", "description": "自定义 PromQL、单位、标签和告警阈值列表。"},
                "default_detect_methods": {"label": "默认检测方法", "type": "array", "description": "默认组合执行的检测方法列表。"},
                "thresholds": {"label": "指标阈值覆盖", "type": "object", "description": "按指标名称覆盖默认阈值。"},
            },
        },
        "observability": {
            "label": "可观测源",
            "description": "Prometheus、Jaeger、Grafana 等可观测平台地址。OTel Demo 业务曲线使用独立的 OTel Prometheus 源。",
            "fields": {
                "prometheus_url": {"label": "集群 Prometheus", "type": "string", "default": "", "description": "基础设施、容器、节点和告警指标查询源。"},
                "otel_prometheus_url": {"label": "OTel Demo Prometheus", "type": "string", "default": "", "description": "OpenTelemetry Demo 原生业务指标查询源，例如请求量、延迟和 5xx。"},
                "jaeger_url": {"label": "Jaeger 地址", "type": "string", "default": "", "description": "链路追踪查询 API 地址。"},
                "grafana_url": {"label": "Grafana 地址", "type": "string", "default": "", "description": "Grafana UI 地址。"},
                "elasticsearch_url": {"label": "Elasticsearch 地址", "type": "string", "default": "", "description": "日志检索后端地址。"},
            },
        },
        "remediation": {
            "label": "自愈策略",
            "description": "自愈动作生成、审批和自动执行的安全控制参数。",
            "fields": {
                "enabled": {"label": "启用真实自愈", "type": "boolean", "default": False, "risk": "high", "description": "允许执行真实变更命令。关闭时只能生成建议和预演。"},
                "dry_run": {"label": "默认 Dry-run", "type": "boolean", "default": True, "description": "默认仅预演命令，不修改集群。建议保持开启。"},
                "require_approval": {"label": "需要审批", "type": "boolean", "default": True, "description": "真实执行前要求人工审批。生产环境建议保持开启。"},
                "confidence_threshold": {"label": "置信度阈值", "type": "number", "default": 0.85, "min": 0, "max": 1, "description": "诊断置信度达到该值后才允许进入自愈执行流程。"},
                "max_steps": {"label": "最大动作数", "type": "integer", "default": 5, "min": 1, "description": "单次自愈允许执行的最大命令数量。"},
                "max_rollback_depth": {"label": "最大回滚深度", "type": "integer", "default": 5, "min": 1, "description": "单次自愈保存和执行回滚动作的最大层数。"},
                "max_auto_risk_level": {"label": "自动执行最高风险", "type": "enum", "default": "medium", "options": ["low", "medium", "high"], "risk": "high", "description": "无需升级审批时允许自动执行的最高风险等级。"},
                "schedule": {"label": "执行时间窗", "type": "string", "default": "", "description": "允许自动执行的计划时间表达式。留空表示不限制。"},
                "blackout_windows": {"label": "禁止执行时间窗", "type": "array", "description": "禁止自动执行的时间范围列表，例如业务高峰期。"},
                "canary_enabled": {"label": "启用金丝雀验证", "type": "boolean", "default": False, "description": "先在受控范围验证动作，再决定是否扩大执行范围。"},
                "canary_namespace": {"label": "金丝雀命名空间", "type": "string", "default": "test", "description": "金丝雀验证使用的 Kubernetes 命名空间。"},
                "require_confirmation_for_high_risk": {"label": "高风险二次确认", "type": "boolean", "default": True, "description": "高风险动作执行前强制要求二次确认。"},
                "recommend_only": {"label": "仅生成建议", "type": "boolean", "default": True, "description": "只提供处置建议，不自动执行命令。生产初期建议保持开启。"},
            },
        },
    }


def _platform_config_public() -> Dict[str, Any]:
    cfg = _get_config()
    raw = _load_platform_config_file()
    det = cfg.detection
    rem = cfg.remediation
    return {
        "stored": _redact_platform_config(raw),
        "schema": _platform_config_schema(),
        "effective": {
            "llm": {
                "base_url": cfg.llm.base_url,
                "model": cfg.llm.model,
                "temperature": cfg.llm.temperature,
                "max_tokens": cfg.llm.max_tokens,
                "timeout": cfg.llm.timeout,
                "api_key_configured": _llm_configured(cfg.llm),
                "api_key_masked": _mask_secret(cfg.llm.api_key),
            },
            "detection": {
                "sources_enabled": det.sources_enabled,
                "metric_checks": det.metric_checks,
                "default_detect_methods": det.default_detect_methods,
                "default_algorithm": getattr(det, "default_algorithm", "zscore"),
                "default_lookback_m": det.default_lookback_m,
                "default_z_threshold": det.default_z_threshold,
                "default_ewma_span": det.default_ewma_span,
                "min_samples": getattr(det, "min_samples", 12),
                "confirmation_points": getattr(det, "confirmation_points", 1),
                "relative_change_threshold": getattr(det, "relative_change_threshold", 0.0),
                "spectral_residual_threshold": getattr(det, "spectral_residual_threshold", 3.0),
                "thresholds": det.thresholds,
            },
            "observability": {
                "prometheus_url": cfg.observability.prometheus_url,
                "otel_prometheus_url": getattr(cfg.observability, "otel_prometheus_url", ""),
                "jaeger_url": cfg.observability.jaeger_url,
                "grafana_url": cfg.observability.grafana_url,
                "elasticsearch_url": cfg.observability.elasticsearch_url,
            },
            "remediation": {
                "enabled": rem.enabled,
                "dry_run": getattr(rem, "dry_run", True),
                "max_steps": getattr(rem, "max_steps", 5),
                "confidence_threshold": rem.confidence_threshold,
                "max_rollback_depth": rem.max_rollback_depth,
                "require_approval": rem.require_approval,
                "schedule": getattr(rem, "schedule", ""),
                "blackout_windows": getattr(rem, "blackout_windows", []),
                "max_auto_risk_level": getattr(rem, "max_auto_risk_level", "medium"),
                "canary_enabled": getattr(rem, "canary_enabled", False),
                "canary_namespace": getattr(rem, "canary_namespace", "test"),
                "require_confirmation_for_high_risk": getattr(rem, "require_confirmation_for_high_risk", True),
                "recommend_only": getattr(rem, "recommend_only", True),
            },
        },
    }


def _load_detection_config():
    """Load persisted detection config overrides on startup."""
    if not _DETECTION_CONFIG_FILE.exists():
        return
    try:
        _ensure_private_config_permissions(_DETECTION_CONFIG_FILE)
        data = json.loads(_DETECTION_CONFIG_FILE.read_text(encoding="utf-8"))
        _apply_detection_overrides(_get_config().detection, _validate_detection_config_values(data))
        logger.info("Loaded detection config from disk")
    except Exception as e:
        logger.warning(f"Failed to load detection config: {e}")


def _save_fault_targets():
    """Persist fault-target profiles (k8s_clusters + llm_hosts) to disk."""
    try:
        cfg = _get_config()
        data = {
            "k8s_clusters": list(cfg.fault_targets.k8s_clusters),
            "llm_hosts": list(cfg.fault_targets.llm_hosts),
        }
        _atomic_write_json(_FAULT_TARGETS_FILE, data)
        logger.info("Fault targets saved to disk")
    except Exception as e:
        logger.warning(f"Failed to save fault targets: {e}")
        raise


def _load_fault_runs() -> List[Dict[str, Any]]:
    """Load persisted fault injection run records."""
    if not _FAULT_RUNS_FILE.exists():
        return []
    try:
        data = json.loads(_FAULT_RUNS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"Failed to load fault runs: {e}")
        return []


def _append_fault_run(record: Dict[str, Any]) -> None:
    """Persist one fault injection/cleanup record, keeping the latest 500."""
    try:
        _FAULT_RUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _FAULT_RUNS_LOCK:
            runs = _load_fault_runs()
            runs.insert(0, record)
            runs = runs[:500]
            _FAULT_RUNS_FILE.write_text(
                json.dumps(runs, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
    except Exception as e:
        logger.warning(f"Failed to save fault run: {e}")


def _matching_fault_run_for_recovery(
    *,
    fault_run_id: str,
    cluster_id: str,
    namespace: str,
    deployment: str,
    scenario_id: str,
) -> Optional[Dict[str, Any]]:
    """Find the exact or latest compatible injection run carrying recovery state."""
    for run in _load_fault_runs():
        if str(run.get("action") or "") not in {"inject", "experiment"}:
            continue
        if fault_run_id and str(run.get("id") or "") != fault_run_id:
            continue
        if str(run.get("cluster_id") or "") != str(cluster_id or ""):
            continue
        if str(run.get("namespace") or "") != str(namespace or ""):
            continue
        if str(run.get("deployment") or "") != str(deployment or ""):
            continue
        if str(run.get("scenario_id") or "") != str(scenario_id or ""):
            continue
        return run
    return None


def _fault_run_recovery_command(run: Optional[Dict[str, Any]]) -> str:
    """Read the injector-owned recovery contract from a persisted run."""
    if not isinstance(run, dict):
        return ""
    result = run.get("result") if isinstance(run.get("result"), dict) else {}
    direct = str(result.get("recovery_cmd") or "").strip()
    if direct:
        return direct
    injection = result.get("injection") if isinstance(result.get("injection"), dict) else {}
    return str(injection.get("recovery_cmd") or "").strip()


def _update_fault_run_observation_extrema(run_id: str, observation: Dict[str, Any]) -> None:
    if not run_id or not observation:
        return
    try:
        with _FAULT_RUNS_LOCK:
            runs = _load_fault_runs()
            changed = False
            for run in runs:
                if str(run.get("id") or "") != str(run_id):
                    continue
                extrema = run.get("observation_extrema") if isinstance(run.get("observation_extrema"), dict) else {}
                minima = extrema.get("min") if isinstance(extrema.get("min"), dict) else {}
                maxima = extrema.get("max") if isinstance(extrema.get("max"), dict) else {}
                for key, value in observation.items():
                    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
                        continue
                    number = float(value)
                    minima[key] = number if key not in minima else min(float(minima[key]), number)
                    maxima[key] = number if key not in maxima else max(float(maxima[key]), number)
                run["observation_extrema"] = {"min": minima, "max": maxima}
                for key in ("observation_context", "metrics_target"):
                    value = observation.get(key)
                    if isinstance(value, dict) and value:
                        run[key] = value
                changed = True
                break
            if changed:
                _FAULT_RUNS_FILE.write_text(
                    json.dumps(runs[:500], indent=2, ensure_ascii=False, default=str),
                    encoding="utf-8",
                )
    except Exception:
        logger.debug("Failed to update fault-run observation extrema", exc_info=True)


def _load_alert_events() -> List[Dict[str, Any]]:
    """Load persisted general alert observations."""
    if not _ALERT_EVENTS_FILE.exists():
        return []
    try:
        data = json.loads(_ALERT_EVENTS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning("Failed to load alert events: %s", e)
        return []


def _alert_event_key(alert: Dict[str, Any]) -> str:
    return "|".join([
        str(alert.get("cluster_id") or ""),
        str(alert.get("source") or ""),
        str(alert.get("namespace") or ""),
        str(alert.get("service") or ""),
        str(alert.get("title") or ""),
        str(alert.get("severity") or ""),
    ])


def _save_alert_events(events: List[Dict[str, Any]]) -> None:
    try:
        _ALERT_EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        events = sorted(events, key=lambda item: float(item.get("last_seen_at") or item.get("timestamp") or 0), reverse=True)
        _ALERT_EVENTS_FILE.write_text(
            json.dumps(events[:1000], indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("Failed to save alert events: %s", e)


def _record_alert_events(alerts: List[Dict[str, Any]]) -> None:
    """Persist observed alerts so the UI can query recent non-active events."""
    if not alerts:
        return
    now = time.time()
    existing = _load_alert_events()
    by_key = {_alert_event_key(item): dict(item) for item in existing if isinstance(item, dict)}
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        item = dict(alert)
        ts = float(item.get("timestamp") or now)
        item.setdefault("first_seen_at", ts)
        item["last_seen_at"] = now
        item["timestamp"] = ts
        item["active"] = True
        key = _alert_event_key(item)
        previous = by_key.get(key)
        if previous:
            item["first_seen_at"] = previous.get("first_seen_at") or item.get("first_seen_at")
            item["occurrences"] = int(previous.get("occurrences") or 1) + 1
        else:
            item["occurrences"] = 1
        by_key[key] = item
    _save_alert_events(list(by_key.values()))


def _recent_alert_events(
    duration: str,
    current_alerts: List[Dict[str, Any]],
    *,
    cluster_id: str = "",
) -> List[Dict[str, Any]]:
    """Merge current alerts with persisted observations inside the requested window."""
    window_s = _duration_seconds(duration, 0)
    if window_s <= 0:
        return current_alerts
    cutoff = time.time() - window_s
    merged: Dict[str, Dict[str, Any]] = {}
    current_keys = set()
    for alert in current_alerts:
        item = dict(alert)
        item["active"] = True
        item["last_seen_at"] = time.time()
        key = _alert_event_key(item)
        current_keys.add(key)
        merged[key] = item
    for event in _load_alert_events():
        if not isinstance(event, dict):
            continue
        if cluster_id and str(event.get("cluster_id") or "") != cluster_id:
            continue
        seen = float(event.get("last_seen_at") or event.get("timestamp") or 0)
        if seen < cutoff:
            continue
        key = _alert_event_key(event)
        if key in merged:
            continue
        item = dict(event)
        item["active"] = key in current_keys
        item["timestamp"] = float(item.get("last_seen_at") or item.get("timestamp") or seen)
        merged[key] = item
    return sorted(merged.values(), key=lambda item: float(item.get("last_seen_at") or item.get("timestamp") or 0), reverse=True)


def _merge_platform_section(section: str, values: Dict[str, Any]) -> Dict[str, Any]:
    """Merge a section into platform_config.json and return the full document."""
    data = _load_platform_config_file()
    current = data.get(section)
    if not isinstance(current, dict):
        current = {}
    current.update(values)
    data[section] = current
    data["updated_at"] = time.time()
    _save_platform_config_file(data)
    return data


def _load_fault_experiments() -> List[Dict[str, Any]]:
    """Load persisted live benchmark experiment records."""
    if not _FAULT_EXPERIMENTS_FILE.exists():
        return []
    try:
        data = json.loads(_FAULT_EXPERIMENTS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"Failed to load fault experiments: {e}")
        return []


def _save_fault_experiment(record: Dict[str, Any]) -> None:
    """Upsert one experiment record and keep recent history bounded."""
    try:
        _FAULT_EXPERIMENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        records = _load_fault_experiments()
        records = [r for r in records if r.get("id") != record.get("id")]
        records.insert(0, record)
        records = records[:200]
        _FAULT_EXPERIMENTS_FILE.write_text(
            json.dumps(records, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"Failed to save fault experiment: {e}")


def _write_fault_experiment_artifact(run: Dict[str, Any]) -> Optional[str]:
    """Persist the full benchmark artifact for one run."""
    try:
        run_id = run.get("id") or f"experiment-{uuid.uuid4().hex[:10]}"
        _FAULT_EXPERIMENT_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        path = _FAULT_EXPERIMENT_ARTIFACT_DIR / f"{run_id}.json"
        path.write_text(
            json.dumps(run, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        return str(path)
    except Exception as e:
        logger.warning(f"Failed to save fault experiment artifact: {e}")
        return None


HEAL_RECIPES: List[Dict[str, Any]] = [
    {
        "fault_type": "OOMKilled",
        "category": "资源",
        "risk": "medium",
        "requires_approval": True,
        "description": "容器被 OOMKilled，优先滚动重启或提高 Deployment 内存上限。",
        "signals": ["OOMKilled", "exit code 137", "memory limit"],
        "actions": [
            {
                "step": "滚动重启受影响 Deployment",
                "command": "kubectl rollout restart deployment/<deployment> -n <namespace>",
                "rollback_command": "kubectl rollout undo deployment/<deployment> -n <namespace>",
                "risk": "medium",
            },
            {
                "step": "查看 Pod 资源使用",
                "command": "kubectl top pod <pod> -n <namespace>",
                "rollback_command": "",
                "risk": "low",
            },
        ],
    },
    {
        "fault_type": "PodCrashLoop",
        "category": "应用",
        "risk": "medium",
        "requires_approval": True,
        "description": "Pod 反复崩溃，先采集日志，再重启或回滚 Deployment。",
        "signals": ["CrashLoopBackOff", "Back-off restarting failed container"],
        "actions": [
            {
                "step": "采集最近容器日志",
                "command": "kubectl logs <pod> -n <namespace> --tail=120 --previous",
                "rollback_command": "",
                "risk": "low",
            },
            {
                "step": "回滚 Deployment 到上一版本",
                "command": "kubectl rollout undo deployment/<deployment> -n <namespace>",
                "rollback_command": "",
                "risk": "medium",
            },
        ],
    },
    {
        "fault_type": "ImagePullError",
        "category": "镜像",
        "risk": "medium",
        "requires_approval": True,
        "description": "镜像拉取失败，建议修正镜像 tag 或 imagePullSecret 后触发 rollout。",
        "signals": ["ImagePullBackOff", "ErrImagePull"],
        "actions": [
            {
                "step": "查看镜像拉取事件",
                "command": "kubectl describe pod <pod> -n <namespace>",
                "rollback_command": "",
                "risk": "low",
            },
            {
                "step": "重启 Deployment 重新拉取镜像",
                "command": "kubectl rollout restart deployment/<deployment> -n <namespace>",
                "rollback_command": "kubectl rollout undo deployment/<deployment> -n <namespace>",
                "risk": "medium",
            },
        ],
    },
    {
        "fault_type": "NodeNotReady",
        "category": "节点",
        "risk": "high",
        "requires_approval": True,
        "manual_only": True,
        "auto_recovery_supported": False,
        "automation_class": "manual_only",
        "description": "节点不可用，先 cordon 隔离并检查节点事件，必要时驱逐业务。",
        "signals": ["NodeNotReady", "node is not ready"],
        "actions": [
            {
                "step": "隔离不可用节点",
                "command": "kubectl cordon <node>",
                "rollback_command": "kubectl uncordon <node>",
                "risk": "high",
            },
            {
                "step": "查看节点详情",
                "command": "kubectl describe node <node>",
                "rollback_command": "",
                "risk": "low",
            },
        ],
    },
    {
        "fault_type": "MetricAnomaly",
        "category": "指标",
        "risk": "low",
        "requires_approval": False,
        "description": "指标异常先做只读确认，避免误修复。",
        "signals": ["metric", "prometheus", "anomaly", "指标"],
        "actions": [
            {
                "step": "查看相关 Pod 资源使用",
                "command": "kubectl top pod -n <namespace>",
                "rollback_command": "",
                "risk": "low",
            },
        ],
    },
    {
        "fault_type": "ProbeFailure",
        "category": "应用",
        "risk": "medium",
        "requires_approval": True,
        "description": "Readiness/Liveness 探针失败，先查看事件和 rollout 状态，再执行滚动重启。",
        "signals": ["Unhealthy", "readiness probe failed", "liveness probe failed", "probe"],
        "actions": [
            {
                "step": "查看 Pod 事件与探针失败原因",
                "command": "kubectl describe pod <pod> -n <namespace>",
                "rollback_command": "",
                "risk": "low",
            },
            {
                "step": "滚动重启受影响 Deployment",
                "command": "kubectl rollout restart deployment/<deployment> -n <namespace>",
                "rollback_command": "kubectl rollout undo deployment/<deployment> -n <namespace>",
                "risk": "medium",
            },
        ],
    },
    {
        "fault_type": "PendingScheduling",
        "category": "调度",
        "risk": "low",
        "requires_approval": False,
        "description": "Pod Pending 或 FailedScheduling，优先只读确认资源、污点和调度失败事件。",
        "signals": ["FailedScheduling", "Pending", "Insufficient cpu", "Insufficient memory"],
        "actions": [
            {
                "step": "查看 Pod 调度失败详情",
                "command": "kubectl describe pod <pod> -n <namespace>",
                "rollback_command": "",
                "risk": "low",
            },
            {
                "step": "查看节点资源水位",
                "command": "kubectl top node",
                "rollback_command": "",
                "risk": "low",
            },
        ],
    },
    {
        "fault_type": "HighCpu",
        "category": "资源",
        "risk": "low",
        "requires_approval": False,
        "description": "CPU 使用率异常，默认只读确认热点 Pod，避免误扩缩容。",
        "signals": ["cpu", "CPU", "node_cpu_usage", "container_cpu"],
        "actions": [
            {
                "step": "查看命名空间内 Pod CPU 使用",
                "command": "kubectl top pod -n <namespace>",
                "rollback_command": "",
                "risk": "low",
            },
            {
                "step": "查看 Deployment rollout 状态",
                "command": "kubectl rollout status deployment/<deployment> -n <namespace>",
                "rollback_command": "",
                "risk": "low",
            },
        ],
    },
    {
        "fault_type": "HighMemory",
        "category": "资源",
        "risk": "medium",
        "requires_approval": True,
        "description": "内存使用率异常或接近 OOM，先确认资源水位，再选择滚动重启或扩容。",
        "signals": ["memory", "内存", "node_memory_usage", "container_memory"],
        "actions": [
            {
                "step": "查看命名空间内 Pod 内存使用",
                "command": "kubectl top pod -n <namespace>",
                "rollback_command": "",
                "risk": "low",
            },
            {
                "step": "滚动重启疑似内存泄漏 Deployment",
                "command": "kubectl rollout restart deployment/<deployment> -n <namespace>",
                "rollback_command": "kubectl rollout undo deployment/<deployment> -n <namespace>",
                "risk": "medium",
            },
        ],
    },
]

HEAL_RECIPES.extend([
    {
        "fault_type": "HighCPUUsage",
        "category": "资源",
        "risk": "medium",
        "requires_approval": True,
        "description": "CPU 使用率持续高位，参考 agenticSnail 的热点定位、资源调优、滚动恢复三段式策略。",
        "signals": ["HighCPU", "cpu usage", "container_cpu", "node_cpu_usage"],
        "actions": [
            {"step": "按 CPU 排序定位热点 Pod", "command": "kubectl top pod -n <namespace> --sort-by=cpu", "rollback_command": "", "risk": "low"},
            {"step": "提高 Deployment CPU limit", "command": "kubectl set resources deployment/<deployment> -n <namespace> --requests=cpu=500m --limits=1500m", "rollback_command": "kubectl rollout undo deployment/<deployment> -n <namespace>", "risk": "medium"},
            {"step": "滚动重启使 CPU 配置生效", "command": "kubectl rollout restart deployment/<deployment> -n <namespace>", "rollback_command": "kubectl rollout undo deployment/<deployment> -n <namespace>", "risk": "medium"},
        ],
    },
    {
        "fault_type": "HighMemoryUsage",
        "category": "资源",
        "risk": "medium",
        "requires_approval": True,
        "description": "内存热点或泄漏风险，先定位内存最高 Pod，再提升 limit 或重建异常副本。",
        "signals": ["HighMemory", "memory usage", "container_memory", "node_memory_usage"],
        "actions": [
            {"step": "按内存排序定位热点 Pod", "command": "kubectl top pod -n <namespace> --sort-by=memory", "rollback_command": "", "risk": "low"},
            {"step": "提高 Deployment memory limit", "command": "kubectl set resources deployment/<deployment> -n <namespace> --requests=memory=512Mi --limits=2Gi", "rollback_command": "kubectl rollout undo deployment/<deployment> -n <namespace>", "risk": "medium"},
            {"step": "滚动重启疑似内存泄漏 Deployment", "command": "kubectl rollout restart deployment/<deployment> -n <namespace>", "rollback_command": "kubectl rollout undo deployment/<deployment> -n <namespace>", "risk": "medium"},
        ],
    },
    {
        "fault_type": "DiskPressure",
        "category": "节点",
        "risk": "high",
        "requires_approval": True,
        "description": "节点 DiskPressure 时先收集节点 Conditions，必要时隔离并迁移工作负载。",
        "signals": ["DiskPressure", "disk pressure", "filesystem pressure", "disk full"],
        "actions": [
            {"step": "查看节点磁盘压力和驱逐事件", "command": "kubectl describe node <node>", "rollback_command": "", "risk": "low"},
            {"step": "隔离磁盘压力节点", "command": "kubectl cordon <node>", "rollback_command": "kubectl uncordon <node>", "risk": "medium"},
            {"step": "驱逐节点工作负载释放调度压力", "command": "kubectl drain <node> --ignore-daemonsets --delete-emptydir-data --force --timeout=120s", "rollback_command": "kubectl uncordon <node>", "risk": "high"},
        ],
    },
    {
        "fault_type": "DNSResolution",
        "category": "网络",
        "risk": "medium",
        "requires_approval": True,
        "description": "DNS 解析异常时检查 kube-dns 端点，必要时滚动重启 CoreDNS。",
        "signals": ["dns", "coredns", "kube-dns", "lookup timeout"],
        "actions": [
            {"step": "检查 kube-dns Endpoints", "command": "kubectl get endpoints kube-dns -n kube-system -o wide", "rollback_command": "", "risk": "low"},
            {"step": "滚动重启 CoreDNS", "command": "kubectl rollout restart deployment/coredns -n kube-system", "rollback_command": "", "risk": "medium"},
            {"step": "验证 CoreDNS Pod 状态", "command": "kubectl get pods -n kube-system -l k8s-app=kube-dns -o wide", "rollback_command": "", "risk": "low"},
        ],
    },
    {
        "fault_type": "KubeProxyUnhealthy",
        "category": "网络",
        "risk": "medium",
        "requires_approval": True,
        "description": "kube-proxy 不健康时检查 DaemonSet 与 Pod，必要时触发 DaemonSet 滚动重启。",
        "signals": ["kube-proxy", "KubeProxyDown", "proxy rules"],
        "actions": [
            {"step": "查看 kube-proxy Pod 分布", "command": "kubectl get pods -n kube-system -l k8s-app=kube-proxy -o wide", "rollback_command": "", "risk": "low"},
            {"step": "重启 kube-proxy DaemonSet", "command": "kubectl rollout restart ds/kube-proxy -n kube-system", "rollback_command": "", "risk": "medium"},
            {"step": "等待 kube-proxy 发布完成", "command": "kubectl rollout status ds/kube-proxy -n kube-system", "rollback_command": "", "risk": "low"},
        ],
    },
    {
        "fault_type": "ApiServerUnhealthy",
        "category": "控制面",
        "risk": "high",
        "requires_approval": True,
        "manual_only": True,
        "auto_recovery_supported": False,
        "automation_class": "manual_only",
        "description": "API Server 异常属于控制面高风险故障，只允许在审批后重建异常静态 Pod。",
        "signals": ["apiserver", "kube-apiserver", "TargetDown"],
        "actions": [
            {"step": "查看 API Server 静态 Pod", "command": "kubectl get pods -n kube-system -l component=kube-apiserver -o wide", "rollback_command": "", "risk": "low"},
            {"step": "查看 API Server 事件详情", "command": "kubectl describe pod -n kube-system -l component=kube-apiserver", "rollback_command": "", "risk": "low"},
            {"step": "删除异常 API Server Pod 触发 kubelet 重建", "command": "kubectl delete pod -n kube-system -l component=kube-apiserver", "rollback_command": "", "risk": "high"},
        ],
    },
    {
        "fault_type": "EtcdUnhealthy",
        "category": "控制面",
        "risk": "high",
        "requires_approval": True,
        "manual_only": True,
        "auto_recovery_supported": False,
        "automation_class": "manual_only",
        "description": "etcd 异常属于控制面存储高风险故障，默认只诊断，重建动作需审批和风险确认。",
        "signals": ["etcd", "etcdserver", "leader changed"],
        "actions": [
            {"step": "查看 etcd 静态 Pod", "command": "kubectl get pods -n kube-system -l component=etcd -o wide", "rollback_command": "", "risk": "low"},
            {"step": "查看 etcd Pod 事件", "command": "kubectl describe pod -n kube-system -l component=etcd", "rollback_command": "", "risk": "low"},
            {"step": "重建异常 etcd Pod", "command": "kubectl delete pod -n kube-system -l component=etcd", "rollback_command": "", "risk": "high"},
        ],
    },
    {
        "fault_type": "KubeletUnhealthy",
        "category": "节点",
        "risk": "high",
        "requires_approval": True,
        "description": "kubelet 不健康时先隔离节点，必要时迁移工作负载，主机级 kubelet 重启留给人工执行。",
        "signals": ["kubelet", "KubeletDown", "node agent"],
        "actions": [
            {"step": "查看节点 Conditions", "command": "kubectl describe node <node>", "rollback_command": "", "risk": "low"},
            {"step": "隔离 kubelet 异常节点", "command": "kubectl cordon <node>", "rollback_command": "kubectl uncordon <node>", "risk": "medium"},
            {"step": "驱逐节点工作负载", "command": "kubectl drain <node> --ignore-daemonsets --delete-emptydir-data --force --timeout=120s", "rollback_command": "kubectl uncordon <node>", "risk": "high"},
        ],
    },
    {
        "fault_type": "VolumeMountFailed",
        "category": "存储",
        "risk": "medium",
        "requires_approval": True,
        "manual_only": True,
        "auto_recovery_supported": False,
        "automation_class": "manual_only",
        "description": "挂载失败时检查 PV/PVC/Pod 事件，必要时重建 Pod 触发 kubelet 重新挂载。",
        "signals": ["FailedMount", "MountVolume", "FailedAttachVolume"],
        "actions": [
            {"step": "查看 Pod 挂载失败事件", "command": "kubectl describe pod <pod> -n <namespace>", "rollback_command": "", "risk": "low"},
            {"step": "查看命名空间 PVC 状态", "command": "kubectl get pvc -n <namespace> -o wide", "rollback_command": "", "risk": "low"},
            {"step": "删除挂载异常 Pod 触发重新挂载", "command": "kubectl delete pod <pod> -n <namespace> --grace-period=30", "rollback_command": "", "risk": "medium"},
        ],
    },
    {
        "fault_type": "ConfigMissing",
        "category": "配置",
        "risk": "medium",
        "requires_approval": True,
        "manual_only": True,
        "auto_recovery_supported": False,
        "automation_class": "manual_only",
        "description": "ConfigMap/Secret 缺失时先定位引用关系，不自动生成未知业务配置，仅允许重建 Pod 重新挂载已补齐配置。",
        "signals": ["configmap not found", "secret not found", "not registered"],
        "actions": [
            {"step": "查看 Pod 配置引用和事件", "command": "kubectl describe pod <pod> -n <namespace>", "rollback_command": "", "risk": "low"},
            {"step": "列出命名空间 ConfigMap/Secret", "command": "kubectl get configmap,secret -n <namespace>", "rollback_command": "", "risk": "low"},
            {"step": "配置补齐后重建 Pod", "command": "kubectl delete pod <pod> -n <namespace> --grace-period=30", "rollback_command": "", "risk": "medium"},
        ],
    },
    {
        "fault_type": "ConfigKeyMissing",
        "category": "配置",
        "risk": "low",
        "requires_approval": True,
        "manual_only": True,
        "auto_recovery_supported": False,
        "automation_class": "manual_only",
        "description": "ConfigMap 已存在但 key 缺失，默认只诊断和触发配置补齐后的重建，避免写入错误默认值。",
        "signals": ["non-existent config key", "references non-existent key"],
        "actions": [
            {"step": "查看 Pod 中缺失 key 的引用", "command": "kubectl describe pod <pod> -n <namespace>", "rollback_command": "", "risk": "low"},
            {"step": "导出命名空间 ConfigMap 供人工核对", "command": "kubectl get configmap -n <namespace> -o yaml", "rollback_command": "", "risk": "low"},
            {"step": "配置补齐后滚动重启 Deployment", "command": "kubectl rollout restart deployment/<deployment> -n <namespace>", "rollback_command": "kubectl rollout undo deployment/<deployment> -n <namespace>", "risk": "medium"},
        ],
    },
    {
        "fault_type": "SchedulingFailed",
        "category": "调度",
        "risk": "medium",
        "requires_approval": True,
        "description": "调度失败时检查资源请求、污点、配额，必要时调整副本或资源以恢复可调度性。",
        "signals": ["FailedScheduling", "Unschedulable", "Insufficient cpu", "Insufficient memory"],
        "actions": [
            {"step": "查看 Pod 调度失败事件", "command": "kubectl describe pod <pod> -n <namespace>", "rollback_command": "", "risk": "low"},
            {"step": "查看节点资源水位", "command": "kubectl top node", "rollback_command": "", "risk": "low"},
            {"step": "查看命名空间 ResourceQuota", "command": "kubectl describe resourcequota -n <namespace>", "rollback_command": "", "risk": "low"},
        ],
    },
    {
        "fault_type": "ResourceQuotaExceeded",
        "category": "容量",
        "risk": "medium",
        "requires_approval": True,
        "description": "命名空间配额不足时先输出配额和使用量，配额调整需人工确认后执行。",
        "signals": ["exceeded quota", "ResourceQuota", "forbidden: quota"],
        "actions": [
            {"step": "查看 ResourceQuota 摘要", "command": "kubectl get resourcequota -n <namespace>", "rollback_command": "", "risk": "low"},
            {"step": "查看 ResourceQuota 明细", "command": "kubectl describe resourcequota -n <namespace>", "rollback_command": "", "risk": "low"},
            {"step": "查看命名空间资源使用", "command": "kubectl top pod -n <namespace>", "rollback_command": "", "risk": "low"},
        ],
    },
    {
        "fault_type": "HPAScalingFailed",
        "category": "弹性伸缩",
        "risk": "medium",
        "requires_approval": True,
        "description": "HPA 扩缩容失败时检查 scaleTargetRef、metrics-server 和目标 Deployment，必要时临时固定副本数。",
        "signals": ["FailedGetScale", "FailedGetResourceMetric", "FailedComputeMetricsReplicas"],
        "actions": [
            {"step": "查看 HPA 状态", "command": "kubectl get hpa -n <namespace> -o wide", "rollback_command": "", "risk": "low"},
            {"step": "查看 metrics APIService", "command": "kubectl get apiservice v1beta1.metrics.k8s.io", "rollback_command": "", "risk": "low"},
            {"step": "临时固定 Deployment 副本数", "command": "kubectl scale deployment/<deployment> -n <namespace> --replicas=1", "rollback_command": "", "risk": "medium"},
        ],
    },
    {
        "fault_type": "DeploymentUnhealthy",
        "category": "应用",
        "risk": "medium",
        "requires_approval": True,
        "description": "Deployment 副本不健康或探针失败，先确认 rollout 和 endpoints，再重启或回滚。",
        "signals": ["readiness probe failed", "liveness probe failed", "replicas unavailable"],
        "actions": [
            {"step": "检查 Deployment 发布状态", "command": "kubectl rollout status deployment/<deployment> -n <namespace>", "rollback_command": "", "risk": "low"},
            {"step": "检查服务端点", "command": "kubectl get endpoints <service> -n <namespace> -o wide", "rollback_command": "", "risk": "low"},
            {"step": "滚动重启 Deployment", "command": "kubectl rollout restart deployment/<deployment> -n <namespace>", "rollback_command": "kubectl rollout undo deployment/<deployment> -n <namespace>", "risk": "medium"},
            {"step": "回滚到上一稳定版本", "command": "kubectl rollout undo deployment/<deployment> -n <namespace>", "rollback_command": "", "risk": "medium"},
        ],
    },
    {
        "fault_type": "NetworkPolicyDeny",
        "category": "网络",
        "risk": "low",
        "requires_approval": True,
        "description": "NetworkPolicy 疑似阻断合法流量时默认只审计策略，不自动删除网络策略。",
        "signals": ["NetworkPolicy", "egress denied", "ingress denied", "network deny"],
        "actions": [
            {"step": "查看命名空间 NetworkPolicy", "command": "kubectl get networkpolicy -n <namespace>", "rollback_command": "", "risk": "low"},
            {"step": "查看 NetworkPolicy 明细", "command": "kubectl describe networkpolicy -n <namespace>", "rollback_command": "", "risk": "low"},
            {"step": "查看服务 Endpoints", "command": "kubectl get endpoints -n <namespace> -o wide", "rollback_command": "", "risk": "low"},
        ],
    },
    {
        "fault_type": "HighLatency",
        "category": "性能",
        "risk": "medium",
        "requires_approval": True,
        "description": "P99 延迟或超时升高时先检查 endpoints 和资源热点，必要时扩容或滚动重启。",
        "signals": ["latency", "timeout", "p99", "slow"],
        "actions": [
            {"step": "检查服务 Endpoints", "command": "kubectl get endpoints <service> -n <namespace> -o wide", "rollback_command": "", "risk": "low"},
            {"step": "查看资源热点", "command": "kubectl top pod -n <namespace> --sort-by=cpu", "rollback_command": "", "risk": "low"},
            {"step": "滚动重启高延迟 Deployment", "command": "kubectl rollout restart deployment/<deployment> -n <namespace>", "rollback_command": "kubectl rollout undo deployment/<deployment> -n <namespace>", "risk": "medium"},
            {"step": "临时扩容分摊流量", "command": "kubectl scale deployment/<deployment> -n <namespace> --replicas=2", "rollback_command": "", "risk": "medium"},
        ],
    },
    {
        "fault_type": "ServiceUnavailable",
        "category": "应用",
        "risk": "medium",
        "requires_approval": True,
        "description": "服务 5xx 或不可用时检查 Service/Endpoints/Deployment，优先恢复副本可用性。",
        "signals": ["ServiceUnavailable", "5xx", "no endpoints", "connection refused"],
        "actions": [
            {"step": "检查 Service Endpoints", "command": "kubectl get endpoints <service> -n <namespace> -o wide", "rollback_command": "", "risk": "low"},
            {"step": "检查 Deployment 状态", "command": "kubectl get deployment <deployment> -n <namespace> -o wide", "rollback_command": "", "risk": "low"},
            {"step": "滚动重启 Deployment", "command": "kubectl rollout restart deployment/<deployment> -n <namespace>", "rollback_command": "kubectl rollout undo deployment/<deployment> -n <namespace>", "risk": "medium"},
        ],
    },
    {
        "fault_type": "TargetDown",
        "category": "监控",
        "risk": "low",
        "requires_approval": False,
        "description": "Prometheus TargetDown 先做目标发现和 endpoints 只读确认，避免把监控丢失误判为业务故障。",
        "signals": ["TargetDown", "scrape failed", "target disappeared"],
        "actions": [
            {"step": "查看命名空间 Service 和 Endpoints", "command": "kubectl get svc,endpoints -n <namespace> -o wide", "rollback_command": "", "risk": "low"},
            {"step": "查看相关 Pod 状态", "command": "kubectl get pods -n <namespace> -o wide", "rollback_command": "", "risk": "low"},
        ],
    },
    {
        "fault_type": "LogErrorSpike",
        "category": "应用",
        "risk": "medium",
        "requires_approval": True,
        "description": "错误日志突增时先采样最近日志，若与发布相关再滚动重启或回滚。",
        "signals": ["error spike", "log error", "ERROR rate"],
        "actions": [
            {"step": "采集最近错误日志样本", "command": "kubectl logs <pod> -n <namespace> --tail=500", "rollback_command": "", "risk": "low"},
            {"step": "查看 Deployment 发布历史", "command": "kubectl rollout history deployment/<deployment> -n <namespace>", "rollback_command": "", "risk": "low"},
            {"step": "回滚疑似问题发布", "command": "kubectl rollout undo deployment/<deployment> -n <namespace>", "rollback_command": "", "risk": "medium"},
        ],
    },
    {
        "fault_type": "KubeDeploymentReplicasMismatch",
        "category": "应用",
        "risk": "medium",
        "requires_approval": True,
        "description": "Deployment 期望副本和可用副本不一致，检查 ReplicaSet/Events 后滚动恢复。",
        "signals": ["KubeDeploymentReplicasMismatch", "replicas mismatch"],
        "actions": [
            {"step": "查看 Deployment 副本状态", "command": "kubectl get deployment <deployment> -n <namespace> -o wide", "rollback_command": "", "risk": "low"},
            {"step": "查看 ReplicaSet 状态", "command": "kubectl get rs -n <namespace> -o wide", "rollback_command": "", "risk": "low"},
            {"step": "滚动重启 Deployment 恢复副本", "command": "kubectl rollout restart deployment/<deployment> -n <namespace>", "rollback_command": "kubectl rollout undo deployment/<deployment> -n <namespace>", "risk": "medium"},
        ],
    },
    {
        "fault_type": "NodeClockNotSynchronising",
        "category": "节点",
        "risk": "low",
        "requires_approval": False,
        "description": "节点时钟不同步会影响证书、日志和控制面选主，当前系统只做 K8s 侧诊断并升级人工处理。",
        "signals": ["NodeClockNotSynchronising", "clock skew", "time sync"],
        "actions": [
            {"step": "查看节点 Conditions", "command": "kubectl describe node <node>", "rollback_command": "", "risk": "low"},
            {"step": "查看节点列表和版本", "command": "kubectl get nodes -o wide", "rollback_command": "", "risk": "low"},
        ],
    },
    {
        "fault_type": "AppArmorProfileUnsupported",
        "category": "安全",
        "risk": "low",
        "requires_approval": True,
        "manual_only": True,
        "auto_recovery_supported": False,
        "automation_class": "manual_only",
        "description": "AppArmor/vArmor profile 不兼容导致容器创建失败时，默认只定位安全策略和 Pod 事件，不自动放宽安全边界。",
        "signals": ["AppArmor", "vArmor", "CreateContainerError", "profile unsupported"],
        "actions": [
            {"step": "查看 Pod 创建失败事件", "command": "kubectl describe pod <pod> -n <namespace>", "rollback_command": "", "risk": "low"},
            {"step": "查看命名空间 Pod 状态", "command": "kubectl get pods -n <namespace> -o wide", "rollback_command": "", "risk": "low"},
        ],
    },
])

_HEAL_FORBIDDEN = re.compile(
    r"\b(rm\s+-rf|mkfs|fdisk|dd\s+if|shutdown|reboot|halt|kill\s+-9|pkill|wipefs|format)\b",
    re.IGNORECASE,
)


def _load_heal_recipe_library() -> Dict[str, Any]:
    # The immutable deployment YAML remains the seed. Both services read this
    # shared, atomically replaced state on every request; no stale process cache.
    if HEAL_RECIPES_STATE.exists():
        try:
            state = json.loads(HEAL_RECIPES_STATE.read_text(encoding="utf-8"))
            library = state["library"]
            if not isinstance(library.get("recipes"), list):
                raise ValueError("invalid recipes")
            return {**library, "source": str(HEAL_RECIPES_STATE)}
        except Exception as exc:
            raise HTTPException(503, "自愈知识库读取失败，已停止使用策略，请检查持久化文件") from exc
    return _load_seed_heal_recipe_library()


def _heal_library_revision(library: Dict[str, Any]) -> str:
    content = {k: v for k, v in library.items() if k != "source"}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _load_seed_heal_recipe_library() -> Dict[str, Any]:
    """Load external self-healing recipes, falling back to built-in recipes."""
    if HEAL_RECIPES_FILE.exists():
        try:
            data = yaml.safe_load(HEAL_RECIPES_FILE.read_text(encoding="utf-8")) or {}
            recipes = data.get("recipes") if isinstance(data, dict) else None
            if isinstance(recipes, list):
                return {
                    "version": data.get("version") or "external",
                    "description": data.get("description") or "",
                    "source": str(HEAL_RECIPES_FILE),
                    "recipes": recipes,
                }
        except Exception as e:
            logger.warning("Failed to load heal recipe library: %s", e)
    return {
        "version": "built-in",
        "description": "Built-in fallback self-healing recipes.",
        "source": "built-in",
        "recipes": HEAL_RECIPES,
    }


def _load_heal_recipes() -> List[Dict[str, Any]]:
    return list(_load_heal_recipe_library().get("recipes") or [])


def _load_heal_runs() -> List[Dict[str, Any]]:
    if not _HEAL_RUNS_FILE.exists():
        return []
    try:
        data = json.loads(_HEAL_RUNS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"Failed to load heal runs: {e}")
        return []


@contextmanager
def _heal_run_guard(run_id: str):
    if not _SAFE_ID_RE.fullmatch(run_id) or run_id in {".", ".."}:
        raise HTTPException(400, "Invalid heal run ID")
    _HEAL_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    with (_HEAL_ARTIFACT_DIR / f".{run_id}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HTTPException(409, "Heal run verification or rollback is in progress")
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _save_heal_run(record: Dict[str, Any]) -> None:
    if not _SAFE_ID_RE.fullmatch(str(record.get("id") or "")) or record["id"] in {".", ".."}:
        raise HTTPException(400, "Invalid heal run ID")
    try:
        _HEAL_RUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _HEAL_RUNS_FILE.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            # Do not overwrite a damaged ledger with an empty list.
            runs = json.loads(_HEAL_RUNS_FILE.read_text()) if _HEAL_RUNS_FILE.exists() else []
            if not isinstance(runs, list) or any(not isinstance(r, dict) for r in runs):
                raise ValueError("Invalid heal ledger")
            runs = [r for r in runs if r.get("id") != record["id"]]
            runs.insert(0, record)
            _atomic_write_json(_HEAL_ARTIFACT_DIR / f"{record['id']}.json", record)
            _atomic_write_json(_HEAL_RUNS_FILE, runs[:300])
    except Exception as exc:
        logger.exception("Failed to save heal run")
        raise HTTPException(503, "Heal run persistence failed; inspect execution before retrying") from exc


def _load_skill_workflow_runs() -> List[Dict[str, Any]]:
    if not _SKILL_WORKFLOW_RUNS_FILE.exists():
        return []
    try:
        data = json.loads(_SKILL_WORKFLOW_RUNS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"Failed to load skill workflow runs: {e}")
        return []


def _save_skill_workflow_run(record: Dict[str, Any]) -> None:
    try:
        _SKILL_WORKFLOW_RUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
        runs = [r for r in _load_skill_workflow_runs() if r.get("id") != record.get("id")]
        runs.insert(0, record)
        runs = runs[:300]
        _SKILL_WORKFLOW_RUNS_FILE.write_text(
            json.dumps(runs, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        _SKILL_WORKFLOW_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        (_SKILL_WORKFLOW_ARTIFACT_DIR / f"{record['id']}.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"Failed to save skill workflow run: {e}")


def _load_ai_events() -> List[Dict[str, Any]]:
    if not _AI_EVENTS_FILE.exists():
        return []
    try:
        data = json.loads(_AI_EVENTS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning("Failed to load AI events: %s", e)
        return []


def _save_ai_events(events: List[Dict[str, Any]]) -> None:
    try:
        _AI_EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _AI_EVENTS_FILE.write_text(
            json.dumps(events[:500], indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("Failed to save AI events: %s", e)


def _upsert_ai_event(event: Dict[str, Any]) -> Dict[str, Any]:
    events = [item for item in _load_ai_events() if item.get("id") != event.get("id")]
    event["updated_at"] = time.time()
    events.insert(0, event)
    _save_ai_events(events)
    return event


def _node_resource_diagnosis_from_event(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if str(event.get("event_type") or "") != "node_resource_pressure":
        return None
    values = event.get("value") if isinstance(event.get("value"), dict) else {}
    thresholds = event.get("threshold") if isinstance(event.get("threshold"), dict) else {}
    metric = str(event.get("metric") or "")
    if not values and metric.startswith("node_"):
        values = {metric: event.get("value")}
        thresholds = {metric: event.get("threshold")}
    resource_specs = {
        "node_cpu_usage": ("CPU", "节点 CPU 使用率超过阈值，可能导致预处理、调度和请求排队变慢"),
        "node_memory_usage": ("内存", "节点内存使用率超过阈值，可能影响模型加载、请求调度和容器稳定性"),
        "node_rootfs_usage": ("磁盘/根分区", "节点根分区使用率超过阈值，可能影响镜像、日志、缓存和容器运行时"),
        "node_disk_read": ("磁盘读取 I/O", "节点磁盘读取吞吐超过阈值，可能影响模型或数据加载路径"),
        "node_disk_write": ("磁盘写入 I/O", "节点磁盘写入吞吐超过阈值，可能影响日志、缓存和容器运行时"),
        "node_network_receive": ("网络接收", "节点网络接收吞吐超过阈值，可能存在流量压力或链路拥塞"),
        "node_network_transmit": ("网络发送", "节点网络发送吞吐超过阈值，可能存在流量压力或链路拥塞"),
    }
    candidates: List[Tuple[float, str, float, float]] = []
    for signal, raw_value in values.items():
        if signal not in resource_specs:
            continue
        try:
            value = float(raw_value)
            threshold = float(thresholds.get(signal) or 0)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        if threshold <= 0 or value >= threshold:
            ratio = value / threshold if threshold > 0 else value
            candidates.append((ratio, signal, value, threshold))
    if not candidates:
        return None
    _, signal, value, threshold = max(candidates, key=lambda item: item[0])
    label, root_cause = resource_specs[signal]
    return {
        "signal": signal,
        "label": label,
        "value": value,
        "threshold": threshold,
        "root_cause": root_cause,
    }


def _align_ai_event_diagnosis_with_evidence(
    event: Dict[str, Any],
    diagnosis: Dict[str, Any],
) -> Dict[str, Any]:
    aligned = dict(diagnosis or {})
    resource = _node_resource_diagnosis_from_event(event)
    if not resource:
        return aligned
    original = str(aligned.get("root_cause") or "").strip()
    aligned["root_cause"] = resource["root_cause"]
    aligned["summary"] = f"智算节点{resource['label']}资源压力；诊断结论已与越界指标证据对齐。"
    aligned["evidence_alignment"] = {
        "method": "node_resource_metric_consistency_v1",
        "signal": resource["signal"],
        "value": resource["value"],
        "threshold": resource["threshold"],
        "corrected": bool(original and original != resource["root_cause"]),
    }
    if original and original != resource["root_cause"]:
        aligned["raw_skill_root_cause"] = original
    return aligned


def _compact_ai_event_for_list(event: Dict[str, Any]) -> Dict[str, Any]:
    """Return a small event card payload without dropping stored diagnosis details."""
    compact_keys = [
        "id", "status", "created_at", "updated_at", "last_seen_at", "resolved_at",
        "source", "domain", "event_type", "severity", "title", "description",
        "cluster_id", "cluster_name", "namespace", "deployment", "service",
        "model", "vllm_endpoint", "metric", "value", "threshold", "detector",
        "confirmations", "required_confirmations", "experiment_run_id", "step_id",
        "fault_type", "heal_fault_type", "resolution",
        "k8s_object_kind", "k8s_object_name", "k8s_reason",
    ]
    compact = {key: event.get(key) for key in compact_keys if key in event}
    observation = event.get("observation") if isinstance(event.get("observation"), dict) else {}
    if observation:
        compact["observation"] = {
            key: observation.get(key)
            for key in [
                "loadgen_model",
                "loadgen_requests",
                "loadgen_success",
                "loadgen_errors",
                "loadgen_latency_p95_s",
                "metrics_status",
                "metrics_count",
                "running_requests",
                "waiting_requests",
                "recovery_success",
            ]
            if key in observation
        }
    evidence = _sanitize_ai_event_evidence_for_diagnosis(event.get("evidence"))
    compact["evidence"] = evidence[:8]
    diagnosis = event.get("diagnosis") if isinstance(event.get("diagnosis"), dict) else {}
    diagnosis = _align_ai_event_diagnosis_with_evidence(event, diagnosis)
    if diagnosis:
        compact["diagnosis"] = {
            "status": diagnosis.get("status"),
            "source": diagnosis.get("source"),
            "skill_id": diagnosis.get("skill_id"),
            "fault_type": diagnosis.get("fault_type"),
            "root_cause": diagnosis.get("root_cause"),
            "root_cause_component": diagnosis.get("root_cause_component"),
            "confidence": diagnosis.get("confidence"),
            "summary": diagnosis.get("summary"),
            "confidence_explanation": diagnosis.get("confidence_explanation"),
            "evidence_alignment": diagnosis.get("evidence_alignment"),
            "raw_skill_root_cause": diagnosis.get("raw_skill_root_cause"),
        }
    else:
        compact["diagnosis"] = None
    skill = event.get("skill_result") if isinstance(event.get("skill_result"), dict) else {}
    if skill:
        runtime = skill.get("skill_runtime") if isinstance(skill.get("skill_runtime"), dict) else {}
        plan = skill.get("execution_plan") if isinstance(skill.get("execution_plan"), dict) else {}
        commands = plan.get("commands") if isinstance(plan.get("commands"), list) else []
        compact["skill_result"] = {
            "status": skill.get("status"),
            "domain": skill.get("domain"),
            "dry_run": skill.get("dry_run"),
            "workflow_run_id": skill.get("workflow_run_id"),
            "dashboard_run_id": skill.get("dashboard_run_id"),
            "dashboard_bridge": skill.get("dashboard_bridge") if isinstance(skill.get("dashboard_bridge"), dict) else {},
            "skill_runtime": {
                "matched_count": runtime.get("matched_count", 0),
                "primary_skill_id": runtime.get("primary_skill_id") or "",
            },
            "execution_plan": {
                "dry_run": plan.get("dry_run", True),
                "command_count": plan.get("command_count", len(commands)),
                "commands": commands[:6],
            },
        }
    else:
        compact["skill_result"] = None
    if isinstance(event.get("agent_run"), dict):
        run = event.get("agent_run") or {}
        compact["agent_run"] = {
            "run_id": run.get("run_id"),
            "status": run.get("status") or "running",
            "mode": run.get("mode") or "agent_skill",
            "namespace": run.get("namespace"),
            "query": run.get("query"),
            "requested_stages": run.get("requested_stages") if isinstance(run.get("requested_stages"), list) else [],
            "created_at": run.get("created_at") or event.get("updated_at"),
        }
    else:
        compact["agent_run"] = None
    if isinstance(event.get("agent_runs"), dict):
        compact["agent_runs"] = {}
        for lane, run in (event.get("agent_runs") or {}).items():
            if not isinstance(run, dict):
                continue
            compact["agent_runs"][lane] = {
                "run_id": run.get("run_id"),
                "status": run.get("status") or "running",
                "mode": run.get("mode") or lane,
                "namespace": run.get("namespace"),
                "query": run.get("query"),
                "requested_stages": run.get("requested_stages") if isinstance(run.get("requested_stages"), list) else [],
                "created_at": run.get("created_at") or event.get("updated_at"),
            }
        if compact["agent_run"] is None:
            compact["agent_run"] = compact["agent_runs"].get("agent_skill") or compact["agent_runs"].get("multi_agent")
    else:
        compact["agent_runs"] = {}
    if isinstance(event.get("heal_result"), dict):
        compact["heal_result"] = event.get("heal_result")
    else:
        compact["heal_result"] = None
    return compact


def _compact_ai_events_for_list(events: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    return [_compact_ai_event_for_list(event) for event in events[: max(1, min(limit, 300))]]


def _ensure_ai_event_agent_runs(event: Dict[str, Any]) -> Dict[str, Any]:
    runs = event.get("agent_runs")
    if not isinstance(runs, dict):
        runs = {}
        event["agent_runs"] = runs
    return runs


def _ai_event_summary_value(summary: Dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        value = summary.get(key)
        if value is None:
            return default
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _ai_event_detection_config() -> Dict[str, Any]:
    det = getattr(_get_config(), "detection", None)
    thresholds = getattr(det, "thresholds", {}) if det else {}
    ai_thresholds = thresholds.get("ai", {}) if isinstance(thresholds, dict) else {}

    def number(name: str, default: float) -> float:
        try:
            return float(ai_thresholds.get(name, default))
        except (TypeError, ValueError, AttributeError):
            return default

    try:
        confirmation_points = int(getattr(det, "confirmation_points", 1) or 1)
    except (TypeError, ValueError):
        confirmation_points = 1

    sources_enabled = getattr(det, "sources_enabled", {}) if det else {}
    return {
        "algorithm": "通算同构规则检测",
        "confirmation_mode": "连续确认点",
        "confirmation_points": max(1, confirmation_points),
        "sources_enabled": {
            "prometheus": bool(sources_enabled.get("prometheus", True)) if isinstance(sources_enabled, dict) else True,
            "k8s": bool(sources_enabled.get("k8s", True)) if isinstance(sources_enabled, dict) else True,
            "k8s_events": True,
            "vllm": True,
            "gpu": True,
            "fault_experiment": True,
        },
        "rules": {
            "success_rate_min": number("success_rate_min", 0.99),
            "waiting_requests": number("waiting_requests", 1.0),
            "waiting_requests_critical": number("waiting_requests_critical", 8.0),
            "ttft_p95": number("ttft_p95", 1.0),
            "e2e_latency_p95": number("e2e_latency_p95", 3.0),
            "latency_critical": number("latency_critical", 10.0),
            "gpu_memory_used": number("gpu_memory_used", 90.0),
            "kv_cache": number("kv_cache", 90.0),
            "gpu_util_busy": number("gpu_util_busy", 95.0),
            "node_cpu_usage": number("node_cpu_usage", 85.0),
            "node_memory_usage": number("node_memory_usage", 85.0),
            "node_rootfs_usage": number("node_rootfs_usage", 85.0),
            "node_disk_read": number("node_disk_read", 200.0),
            "node_disk_write": number("node_disk_write", 200.0),
            "node_network_receive": number("node_network_receive", 200.0),
            "node_network_transmit": number("node_network_transmit", 200.0),
            "teller_request_span_ms": number("teller_request_span_ms", 1000.0),
            "teller_kernel_p95_ms": number("teller_kernel_p95_ms", 50.0),
        },
    }


async def _ai_event_detection_config_for_clusters(clusters: List[Dict[str, Any]]) -> Dict[str, Any]:
    detection = _ai_event_detection_config()
    probes: List[Dict[str, Any]] = []
    if detection.get("sources_enabled", {}).get("k8s_events", True):
        for cluster in clusters[:8]:
            if _cluster_kind(cluster) != "ai":
                continue
            probes.append(await _probe_ai_k8s_event_source(cluster))
    detection["k8s_event_sources"] = probes
    return detection


async def _probe_ai_k8s_event_source(cluster: Dict[str, Any], *, limit: int = 20) -> Dict[str, Any]:
    """Return a small read-only probe for the AI cluster K8s event source."""
    cluster_id = str(cluster.get("id") or "")
    namespace = _ai_event_namespaces_for_cluster(cluster)[0]
    kubeconfig = _cluster_kubeconfig_path(cluster)
    context = str(cluster.get("context") or "").strip()
    configured = bool(kubeconfig or context)
    probe: Dict[str, Any] = {
        "cluster_id": cluster_id,
        "namespace": namespace,
        "configured": configured,
        "kubeconfig_configured": bool(kubeconfig),
        "context_configured": bool(context),
        "available": False,
        "nodes": [],
        "event_count": 0,
        "warning_count": 0,
        "recent_reasons": [],
        "message": "",
    }
    try:
        if not configured:
            probe["message"] = "集群档案未配置 kubeconfig/context，无法读取 K8s Events。"
            return probe
        nodes = await _kubectl_json("get nodes", cluster_id=cluster_id)
        if isinstance(nodes, dict) and nodes.get("error"):
            probe["message"] = str(nodes.get("error") or "")
            return probe
        node_items = nodes.get("items") if isinstance(nodes, dict) else []
        probe["nodes"] = [
            item.get("metadata", {}).get("name")
            for item in node_items
            if isinstance(item, dict) and item.get("metadata", {}).get("name")
        ][:20]

        data = await _kubectl_json("get events --sort-by=.lastTimestamp", namespace, cluster_id)
        if isinstance(data, dict) and data.get("error"):
            probe["message"] = str(data.get("error") or "")
            return probe
        items = data.get("items") if isinstance(data, dict) else []
        if not isinstance(items, list):
            items = []
        recent = items[-limit:]
        warnings = [
            item for item in recent
            if isinstance(item, dict)
            and str(item.get("type") or item.get("regarding", {}).get("type") or "").lower() == "warning"
        ]
        probe["available"] = True
        probe["event_count"] = len(recent)
        probe["warning_count"] = len(warnings)
        probe["recent_reasons"] = [
            str(item.get("reason") or item.get("metadata", {}).get("name") or "Warning")
            for item in warnings[-8:]
        ]
        if warnings:
            probe["message"] = f"已连接 K8s Events，最近 {len(warnings)} 条 Warning。"
        else:
            probe["message"] = "已连接 K8s Events，当前命名空间没有近期 Warning。"
        return probe
    except Exception as exc:
        probe["message"] = str(exc)
        return probe


def _parse_k8s_event_timestamp(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return time.time()
    try:
        from datetime import datetime

        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return time.time()


def _ai_event_namespaces_for_cluster(cluster: Dict[str, Any]) -> List[str]:
    namespaces: List[str] = []
    for key in ("default_namespace", "namespace", "vllm_namespace", "monitoring_namespace"):
        value = str(cluster.get(key) or "").strip()
        if value and value not in namespaces:
            namespaces.append(value)
    if not namespaces:
        namespaces.append("llm-k8s")
    return namespaces


def _normalize_k8s_reason(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "k8s_event").strip().lower()).strip("_") or "k8s_event"


def _k8s_event_fault_type(reason: str, message: str, object_name: str) -> str:
    reason_l = reason.lower()
    text = f"{reason_l} {message.lower()} {object_name.lower()}"
    if "failedscheduling" in reason_l or "insufficient" in text:
        return "scheduling_failed"
    if reason_l in {"backoff", "crashloopbackoff", "killing", "unhealthy"} or "crashloopbackoff" in text:
        if any(token in text for token in ("vllm", "llm", "inference")):
            return "vllm_process_crash"
        return "pod_crashloop"
    if reason_l in {"failedpull", "errimagepull", "imagepullbackoff"} or "imagepull" in text:
        return "image_pull_error"
    if "oom" in text or "outofmemory" in text:
        return "gpu_oom" if any(token in text for token in ("gpu", "cuda", "nvidia", "vllm")) else "oom_killed"
    if "failedmount" in reason_l or "failedattachvolume" in reason_l:
        return "volume_mount_failed"
    if "nodenotready" in reason_l or "node not ready" in text:
        return "node_not_ready"
    if any(token in text for token in ("gpu", "cuda", "nvidia", "dcgm")):
        return "gpu_resource_pressure"
    return "k8s_event_warning"


def _k8s_event_severity(reason: str, message: str) -> str:
    text = f"{reason} {message}".lower()
    critical_tokens = (
        "failedscheduling", "crashloopbackoff", "imagepullbackoff", "errimagepull",
        "oom", "outofmemory", "nodenotready", "failedmount", "failedattachvolume",
    )
    return "critical" if any(token in text for token in critical_tokens) else "warning"


def _k8s_event_matches_ai_target(event: Dict[str, Any], cluster: Dict[str, Any], namespace: str) -> bool:
    event_type = str(event.get("type") or event.get("regarding", {}).get("type") or "").strip().lower()
    if event_type and event_type != "warning":
        return False
    involved = event.get("involvedObject") if isinstance(event.get("involvedObject"), dict) else {}
    regarding = event.get("regarding") if isinstance(event.get("regarding"), dict) else {}
    object_name = str(involved.get("name") or regarding.get("name") or "").lower()
    message = str(event.get("message") or event.get("note") or "").lower()
    reason = str(event.get("reason") or "").lower()
    ns = str(involved.get("namespace") or regarding.get("namespace") or namespace or "").strip()
    ai_namespaces = set(_ai_event_namespaces_for_cluster(cluster))
    if ns in ai_namespaces:
        return True
    cluster_tokens = [
        cluster.get("deployment"), cluster.get("service"), cluster.get("model"),
        cluster.get("model_family"), "vllm", "llm", "inference", "gpu", "cuda", "nvidia", "dcgm",
    ]
    haystack = f"{object_name} {message} {reason}"
    return any(str(token or "").strip().lower() and str(token or "").strip().lower() in haystack for token in cluster_tokens)


def _k8s_event_to_ai_event(
    event: Dict[str, Any],
    cluster: Dict[str, Any],
    namespace: str,
    *,
    live_pods: Optional[set[tuple[str, str]]] = None,
    healthy_pods: Optional[set[tuple[str, str]]] = None,
    window_s: int = _AI_EVENT_DEFAULT_WINDOW_SECONDS,
) -> Optional[Dict[str, Any]]:
    if not _k8s_event_matches_ai_target(event, cluster, namespace):
        return None
    involved = event.get("involvedObject") if isinstance(event.get("involvedObject"), dict) else {}
    regarding = event.get("regarding") if isinstance(event.get("regarding"), dict) else {}
    object_kind = str(involved.get("kind") or regarding.get("kind") or "Object")
    object_name = str(involved.get("name") or regarding.get("name") or "unknown")
    event_namespace = str(involved.get("namespace") or regarding.get("namespace") or namespace or cluster.get("default_namespace") or "llm-k8s")
    if object_kind.lower() == "pod" and live_pods is not None and (event_namespace, object_name) not in live_pods:
        return None
    reason = str(event.get("reason") or "Warning")
    if (
        object_kind.lower() == "pod"
        and healthy_pods is not None
        and (event_namespace, object_name) in healthy_pods
        and _normalize_k8s_reason(reason) in {"failedscheduling", "unhealthy", "backoff"}
    ):
        return None
    message = str(event.get("message") or event.get("note") or "")
    count = event.get("count") or event.get("deprecatedCount") or 1
    fault_type = _k8s_event_fault_type(reason, message, object_name)
    severity = _k8s_event_severity(reason, message)
    timestamp = _parse_k8s_event_timestamp(
        event.get("lastTimestamp")
        or event.get("eventTime")
        or event.get("deprecatedLastTimestamp")
        or event.get("metadata", {}).get("creationTimestamp")
    )
    if time.time() - timestamp > max(60, int(window_s or _AI_EVENT_DEFAULT_WINDOW_SECONDS)):
        return None
    cluster_id = str(cluster.get("id") or "default")
    event_id_raw = "|".join([cluster_id, event_namespace, object_kind, object_name, reason, message[:160]])
    base = _ai_event_base(
        cluster=cluster,
        event_type=f"k8s_{_normalize_k8s_reason(reason)}",
        severity=severity,
        title=f"K8s Event: {reason}",
        description=message or f"{object_kind}/{object_name} 产生 Kubernetes Warning 事件。",
        metric="k8s_event",
        value={"reason": reason, "count": count},
        threshold="Warning",
        source="k8s_event",
        evidence=[
            {"source": "k8s_event", "signal": "reason", "value": reason},
            {"source": "k8s_event", "signal": "object", "value": f"{object_kind}/{object_name}"},
            {"source": "k8s_event", "signal": "namespace", "value": event_namespace},
            {"source": "k8s_event", "signal": "count", "value": count},
            {"source": "k8s_event", "signal": "message", "value": message[:240]},
        ],
    )
    base["id"] = "aik8s-" + hashlib.sha1(event_id_raw.encode("utf-8")).hexdigest()[:12]
    base["namespace"] = event_namespace
    base["deployment"] = object_name if object_kind.lower() in {"deployment", "statefulset", "daemonset"} else str(cluster.get("deployment") or "vllm-server")
    base["service"] = object_name if object_kind.lower() == "service" else str(cluster.get("service") or base["deployment"])
    base["k8s_object_kind"] = object_kind
    base["k8s_object_name"] = object_name
    base["k8s_reason"] = reason
    base["fault_type"] = fault_type
    base["heal_fault_type"] = fault_type
    base["last_seen_at"] = timestamp
    base["created_at"] = timestamp
    base["detector"]["rule_id"] = "k8s_event_warning"
    base["detector"]["algorithm"] = "K8s Event 规则检测"
    return base


async def _generate_ai_events_from_k8s_events(
    cluster: Dict[str, Any],
    *,
    limit: int = 80,
    window_s: int = _AI_EVENT_DEFAULT_WINDOW_SECONDS,
) -> List[Dict[str, Any]]:
    detection = _ai_event_detection_config()
    sources = detection.get("sources_enabled") if isinstance(detection.get("sources_enabled"), dict) else {}
    if sources.get("k8s_events") is False or sources.get("k8s") is False:
        return []
    events: List[Dict[str, Any]] = []
    live_pods: set[tuple[str, str]] = set()
    healthy_pods: set[tuple[str, str]] = set()
    try:
        pod_data = await _kubectl_json("get pods --all-namespaces", cluster_id=str(cluster.get("id") or ""))
        for item in pod_data.get("items") if isinstance(pod_data, dict) else []:
            if not isinstance(item, dict):
                continue
            meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            pod_ns = str(meta.get("namespace") or "").strip()
            pod_name = str(meta.get("name") or "").strip()
            if pod_ns and pod_name:
                live_pods.add((pod_ns, pod_name))
                status = item.get("status") if isinstance(item.get("status"), dict) else {}
                phase = str(status.get("phase") or "").lower()
                conditions = status.get("conditions") if isinstance(status.get("conditions"), list) else []
                ready = any(
                    isinstance(cond, dict)
                    and cond.get("type") == "Ready"
                    and str(cond.get("status") or "").lower() == "true"
                    for cond in conditions
                )
                if phase in {"running", "succeeded"} and (ready or phase == "succeeded"):
                    healthy_pods.add((pod_ns, pod_name))
    except Exception as exc:
        logger.warning("Failed to fetch AI live pods for K8s event filtering: %s", exc)
    queries = [(namespace, "get events --sort-by=.lastTimestamp") for namespace in _ai_event_namespaces_for_cluster(cluster)]
    queries.append(("", "get events --sort-by=.lastTimestamp --all-namespaces"))
    seen_ids: set[str] = set()
    for namespace, command in queries:
        try:
            data = await _kubectl_json(command, namespace, str(cluster.get("id") or ""))
        except Exception as exc:
            logger.warning("Failed to fetch AI K8s events for namespace %s: %s", namespace or "all", exc)
            continue
        items = data.get("items") if isinstance(data, dict) else []
        if not isinstance(items, list):
            continue
        for item in items[-limit:]:
            normalized = _k8s_event_to_ai_event(
                item if isinstance(item, dict) else {},
                cluster,
                namespace,
                live_pods=live_pods or None,
                healthy_pods=healthy_pods or None,
                window_s=window_s,
            )
            if normalized:
                if normalized["id"] in seen_ids:
                    continue
                seen_ids.add(normalized["id"])
                events.append(normalized)
    return events


def _k8s_pod_ready(status: Dict[str, Any]) -> bool:
    conditions = status.get("conditions") if isinstance(status.get("conditions"), list) else []
    return any(
        isinstance(cond, dict)
        and str(cond.get("type") or "") == "Ready"
        and str(cond.get("status") or "").lower() == "true"
        for cond in conditions
    )


def _k8s_pod_waiting(status: Dict[str, Any]) -> tuple[str, str, str]:
    statuses: List[Dict[str, Any]] = []
    for key in ("containerStatuses", "initContainerStatuses"):
        values = status.get(key) if isinstance(status.get(key), list) else []
        statuses.extend(item for item in values if isinstance(item, dict))
    for item in statuses:
        state = item.get("state") if isinstance(item.get("state"), dict) else {}
        waiting = state.get("waiting") if isinstance(state.get("waiting"), dict) else {}
        terminated = state.get("terminated") if isinstance(state.get("terminated"), dict) else {}
        if waiting:
            return (
                str(waiting.get("reason") or "ContainerWaiting"),
                str(waiting.get("message") or ""),
                str(item.get("name") or ""),
            )
        if terminated and str(terminated.get("reason") or "").lower() not in {"completed", "succeeded"}:
            return (
                str(terminated.get("reason") or "ContainerTerminated"),
                str(terminated.get("message") or ""),
                str(item.get("name") or ""),
            )
    return "", "", ""


def _k8s_pod_condition_reason(status: Dict[str, Any]) -> tuple[str, str]:
    conditions = status.get("conditions") if isinstance(status.get("conditions"), list) else []
    for cond in conditions:
        if not isinstance(cond, dict):
            continue
        if str(cond.get("status") or "").lower() == "true":
            continue
        reason = str(cond.get("reason") or "").strip()
        message = str(cond.get("message") or "").strip()
        cond_type = str(cond.get("type") or "").strip()
        if reason or message:
            return reason or cond_type or "PodNotReady", message
    return "", ""


def _k8s_pod_matches_ai_target(pod: Dict[str, Any], cluster: Dict[str, Any]) -> bool:
    meta = pod.get("metadata") if isinstance(pod.get("metadata"), dict) else {}
    spec = pod.get("spec") if isinstance(pod.get("spec"), dict) else {}
    namespace = str(meta.get("namespace") or "").strip()
    name = str(meta.get("name") or "").strip().lower()
    labels = meta.get("labels") if isinstance(meta.get("labels"), dict) else {}
    annotations = meta.get("annotations") if isinstance(meta.get("annotations"), dict) else {}
    configured_namespaces = set(_ai_event_namespaces_for_cluster(cluster))
    if namespace in configured_namespaces:
        return True
    containers = spec.get("containers") if isinstance(spec.get("containers"), list) else []
    images = " ".join(
        str(item.get("image") or "")
        for item in containers
        if isinstance(item, dict)
    ).lower()
    label_text = " ".join(
        f"{key}={value}"
        for key, value in {**labels, **annotations}.items()
    ).lower()
    cluster_tokens = [
        cluster.get("deployment"), cluster.get("service"), cluster.get("model"),
        cluster.get("model_family"), cluster.get("namespace"), cluster.get("default_namespace"),
        "vllm", "llm", "inference", "gpu", "cuda", "nvidia", "dcgm", "teller",
    ]
    haystack = f"{namespace.lower()} {name} {images} {label_text}"
    return any(str(token or "").strip().lower() and str(token or "").strip().lower() in haystack for token in cluster_tokens)


def _ai_pod_to_health_event(pod: Dict[str, Any], cluster: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not _k8s_pod_matches_ai_target(pod, cluster):
        return None
    meta = pod.get("metadata") if isinstance(pod.get("metadata"), dict) else {}
    status = pod.get("status") if isinstance(pod.get("status"), dict) else {}
    spec = pod.get("spec") if isinstance(pod.get("spec"), dict) else {}
    namespace = str(meta.get("namespace") or cluster.get("default_namespace") or "llm-k8s").strip()
    pod_name = str(meta.get("name") or "").strip()
    if not namespace or not pod_name:
        return None
    phase = str(status.get("phase") or "Unknown").strip() or "Unknown"
    if phase.lower() in {"succeeded"}:
        return None
    ready = _k8s_pod_ready(status)
    wait_reason, wait_message, container_name = _k8s_pod_waiting(status)
    cond_reason, cond_message = _k8s_pod_condition_reason(status)
    reason = wait_reason or cond_reason or ("PodNotReady" if phase.lower() == "running" and not ready else phase)
    message = wait_message or cond_message or str(status.get("message") or "")
    abnormal = phase.lower() not in {"running"} or not ready or bool(wait_reason)
    if not abnormal:
        return None
    node_name = str(spec.get("nodeName") or "").strip()
    restarts = 0
    for item in status.get("containerStatuses") if isinstance(status.get("containerStatuses"), list) else []:
        if isinstance(item, dict):
            try:
                restarts += int(item.get("restartCount") or 0)
            except (TypeError, ValueError):
                pass
    fault_type = _k8s_event_fault_type(reason, message, pod_name)
    if phase.lower() == "pending" and fault_type == "k8s_event_warning":
        fault_type = "scheduling_failed"
    severity = _k8s_event_severity(reason, message)
    if phase.lower() == "running" and not wait_reason:
        severity = "warning"
    cluster_id = str(cluster.get("id") or "default")
    event_id_raw = "|".join([cluster_id, namespace, pod_name, reason or phase])
    title_reason = reason if reason and reason != phase else phase
    base = _ai_event_base(
        cluster=cluster,
        event_type=f"k8s_pod_{_normalize_k8s_reason(title_reason)}",
        severity=severity,
        title=f"Pod 状态异常: {pod_name}",
        description=f"{namespace}/{pod_name} 当前 {phase}/{title_reason}: {message or 'Pod 未处于 Running/Ready 状态。'}",
        metric="pod_health",
        value={"phase": phase, "reason": title_reason, "ready": ready, "restarts": restarts},
        threshold="Running/Ready",
        source="k8s_pod_health",
        evidence=[
            {"source": "k8s_pod_health", "signal": "pod", "value": pod_name},
            {"source": "k8s_pod_health", "signal": "namespace", "value": namespace},
            {"source": "k8s_pod_health", "signal": "phase", "value": phase},
            {"source": "k8s_pod_health", "signal": "reason", "value": title_reason},
            {"source": "k8s_pod_health", "signal": "ready", "value": ready},
            {"source": "k8s_pod_health", "signal": "container", "value": container_name},
            {"source": "k8s_pod_health", "signal": "node", "value": node_name},
            {"source": "k8s_pod_health", "signal": "message", "value": (message or "")[:240]},
        ],
    )
    base["id"] = "aipod-" + hashlib.sha1(event_id_raw.encode("utf-8")).hexdigest()[:12]
    base["namespace"] = namespace
    base["deployment"] = str(cluster.get("deployment") or "vllm-server")
    base["service"] = str(cluster.get("service") or base["deployment"])
    base["k8s_object_kind"] = "Pod"
    base["k8s_object_name"] = pod_name
    base["k8s_reason"] = title_reason
    base["fault_type"] = fault_type
    base["heal_fault_type"] = fault_type
    base["node"] = node_name
    base["detector"]["rule_id"] = "k8s_pod_health"
    base["detector"]["algorithm"] = "K8s Pod 状态规则检测"
    return base


async def _generate_ai_events_from_pod_health(
    cluster: Dict[str, Any],
    *,
    limit: int = 120,
    window_s: int = _AI_EVENT_DEFAULT_WINDOW_SECONDS,
) -> List[Dict[str, Any]]:
    detection = _ai_event_detection_config()
    sources = detection.get("sources_enabled") if isinstance(detection.get("sources_enabled"), dict) else {}
    if sources.get("k8s_events") is False or sources.get("k8s") is False:
        return []
    events: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    queries = [(namespace, "get pods") for namespace in _ai_event_namespaces_for_cluster(cluster)]
    queries.append(("", "get pods --all-namespaces"))
    for namespace, command in queries:
        try:
            data = await _kubectl_json(command, namespace, str(cluster.get("id") or ""))
        except Exception as exc:
            logger.warning("Failed to fetch AI pod health for namespace %s: %s", namespace or "all", exc)
            continue
        if isinstance(data, dict) and data.get("error"):
            logger.warning(
                "AI pod health query failed for cluster %s namespace %s command %s: %s",
                cluster.get("id") or "default",
                namespace or "all",
                command,
                str(data.get("error") or "")[:500],
            )
            continue
        items = data.get("items") if isinstance(data, dict) else []
        if not isinstance(items, list):
            continue
        for pod in items[:limit]:
            if not isinstance(pod, dict):
                continue
            event = _ai_pod_to_health_event(pod, cluster)
            if event and event["id"] not in seen_ids:
                seen_ids.add(event["id"])
                events.append(event)
    return events


def _ai_event_base(
    *,
    cluster: Dict[str, Any],
    event_type: str,
    severity: str,
    title: str,
    description: str,
    metric: str,
    value: Any,
    threshold: Any,
    source: str = "ai_dashboard",
    experiment_run_id: str = "",
    step_id: str = "",
    evidence: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    cluster_id = str(cluster.get("id") or "default")
    namespace = str(cluster.get("default_namespace") or cluster.get("namespace") or "llm-k8s")
    deployment = str(cluster.get("deployment") or "vllm-server")
    service = str(cluster.get("service") or deployment)
    event_id_raw = "|".join([cluster_id, event_type, experiment_run_id, step_id, metric])
    event_id = "aievt-" + hashlib.sha1(event_id_raw.encode("utf-8")).hexdigest()[:12]
    now = time.time()
    return {
        "id": event_id,
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "last_seen_at": now,
        "source": source,
        "domain": "llm",
        "event_type": event_type,
        "severity": severity,
        "title": title,
        "description": description,
        "cluster_id": cluster_id,
        "cluster_name": cluster.get("name") or cluster_id,
        "namespace": namespace,
        "deployment": deployment,
        "service": service,
        "model": cluster.get("model") or "",
        "vllm_endpoint": cluster.get("vllm_endpoint") or "",
        "metric": metric,
        "value": value,
        "threshold": threshold,
        "evidence": evidence or [],
        "detector": {
            "algorithm": "通算同构规则检测",
            "confirmation_mode": "连续确认点",
            "rule_id": event_type,
        },
        "confirmations": 1,
        "required_confirmations": 1,
        "experiment_run_id": experiment_run_id,
        "step_id": step_id,
        "diagnosis": None,
        "skill_result": None,
        "agent_result": None,
        "resolution": None,
    }


def _generate_ai_events_from_dashboard_payload(
    payload: Dict[str, Any],
    *,
    source: str = "ai_dashboard",
    experiment_run_id: str = "",
    step_id: str = "",
) -> List[Dict[str, Any]]:
    cluster = payload.get("cluster") if isinstance(payload.get("cluster"), dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    peaks = payload.get("peaks") if isinstance(payload.get("peaks"), dict) else {}
    storage = payload.get("storage") if isinstance(payload.get("storage"), dict) else {}
    detection = _ai_event_detection_config()
    rules = detection.get("rules") or {}

    def rule_number(name: str, default: float) -> float:
        try:
            value = rules.get(name, default)
            return float(default if value is None else value)
        except (TypeError, ValueError):
            return default

    def signal_value(name: str, default: float = 0.0) -> float:
        current = _ai_event_summary_value(summary, name, default=default)
        peak = _ai_event_summary_value(peaks, name, default=current)
        return max(current, peak)

    def signal_evidence(source_name: str, signal: str, threshold: Any = None) -> Dict[str, Any]:
        item = {
            "source": source_name,
            "signal": signal,
            "value": signal_value(signal),
            "current": _ai_event_summary_value(summary, signal),
            "peak": _ai_event_summary_value(peaks, signal, default=_ai_event_summary_value(summary, signal)),
        }
        if threshold is not None:
            item["threshold"] = threshold
        return item

    events: List[Dict[str, Any]] = []
    waiting = signal_value("waiting_requests")
    running = signal_value("running_requests")
    running_current = _ai_event_summary_value(summary, "running_requests")
    ttft = signal_value("ttft_p95")
    e2e = signal_value("e2e_latency_p95")
    kv_cache = signal_value("kv_cache")
    gpu_util = signal_value("gpu_util")
    gpu_mem = signal_value("gpu_memory_used")
    node_cpu = signal_value("node_cpu_usage")
    node_memory = signal_value("node_memory_usage")
    node_rootfs = signal_value("node_rootfs_usage")
    node_disk_read = signal_value("node_disk_read")
    node_disk_write = signal_value("node_disk_write")
    node_network_receive = signal_value("node_network_receive")
    node_network_transmit = signal_value("node_network_transmit")
    success_rps = signal_value(
        "success_rps",
        default=_ai_event_summary_value(summary, "success_rate", default=1.0),
    )
    waiting_threshold = rule_number("waiting_requests", 1.0)
    waiting_critical = rule_number("waiting_requests_critical", 8.0)
    ttft_threshold = rule_number("ttft_p95", 1.0)
    e2e_threshold = rule_number("e2e_latency_p95", 3.0)
    latency_critical = rule_number("latency_critical", 10.0)
    kv_threshold = rule_number("kv_cache", 90.0)
    gpu_mem_threshold = rule_number("gpu_memory_used", 90.0)
    gpu_busy_threshold = rule_number("gpu_util_busy", 95.0)
    node_cpu_threshold = rule_number("node_cpu_usage", 85.0)
    node_memory_threshold = rule_number("node_memory_usage", 85.0)
    node_rootfs_threshold = rule_number("node_rootfs_usage", 85.0)
    node_disk_read_threshold = rule_number("node_disk_read", 200.0)
    node_disk_write_threshold = rule_number("node_disk_write", 200.0)
    node_network_receive_threshold = rule_number("node_network_receive", 200.0)
    node_network_transmit_threshold = rule_number("node_network_transmit", 200.0)
    teller = payload.get("fine_grained_trace") if isinstance(payload.get("fine_grained_trace"), dict) else {}
    teller_summary = teller.get("summary") if isinstance(teller.get("summary"), dict) else {}
    teller_request_context = teller.get("request_context") if isinstance(teller.get("request_context"), dict) else {}
    teller_top_kernels = teller.get("top_kernels") if isinstance(teller.get("top_kernels"), list) else []
    teller_event_type_stats = teller.get("event_type_stats") if isinstance(teller.get("event_type_stats"), list) else []
    teller_kernel_p95 = _ai_event_summary_value(teller_summary, "kernel_p95_ms")
    teller_request_span_threshold = rule_number("teller_request_span_ms", 1000.0)
    teller_kernel_p95_threshold = rule_number("teller_kernel_p95_ms", 50.0)

    if storage.get("available") is False:
        events.append(_ai_event_base(
            cluster=cluster,
            event_type="observability_unavailable",
            severity="warning",
            title="智算 Prometheus 不可用",
            description="智算集群缺少稳定时序指标，连续实验期间无法可靠触发指标型诊断。",
            metric="prometheus",
            value="unavailable",
            threshold="available",
            source=source,
            experiment_run_id=experiment_run_id,
            step_id=step_id,
            evidence=[{"source": "prometheus", "signal": "storage.available", "value": False}],
        ))
    if waiting >= waiting_threshold:
        events.append(_ai_event_base(
            cluster=cluster,
            event_type="vllm_queue_backlog",
            severity="warning" if waiting < waiting_critical else "critical",
            title="vLLM 等待队列升高",
            description=(
                f"vLLM 等待队列窗口峰值为 {waiting:g}，当前值为 "
                f"{_ai_event_summary_value(summary, 'waiting_requests'):g}，说明请求曾发生堆积，可能存在业务侧故障传播。"
            ),
            metric="vllm:num_requests_waiting",
            value=waiting,
            threshold=waiting_threshold,
            source=source,
            experiment_run_id=experiment_run_id,
            step_id=step_id,
            evidence=[signal_evidence("vllm", "waiting_requests", waiting_threshold)],
        ))
    if ttft >= ttft_threshold or e2e >= e2e_threshold:
        events.append(_ai_event_base(
            cluster=cluster,
            event_type="inference_latency_high",
            severity="warning" if max(ttft, e2e) < latency_critical else "critical",
            title="推理时延升高",
            description=(
                f"窗口峰值 TTFT P95={ttft:g}s, E2E P95={e2e:g}s；当前 TTFT="
                f"{_ai_event_summary_value(summary, 'ttft_p95'):g}s, E2E="
                f"{_ai_event_summary_value(summary, 'e2e_latency_p95'):g}s。用户侧首 token 或端到端体验曾经恶化。"
            ),
            metric="ttft/e2e",
            value={"ttft_p95": ttft, "e2e_p95": e2e},
            threshold={"ttft_p95": ttft_threshold, "e2e_p95": e2e_threshold},
            source=source,
            experiment_run_id=experiment_run_id,
            step_id=step_id,
            evidence=[
                signal_evidence("vllm", "ttft_p95", ttft_threshold),
                signal_evidence("vllm", "e2e_latency_p95", e2e_threshold),
            ],
        ))
    if kv_cache >= kv_threshold or gpu_mem >= gpu_mem_threshold:
        events.append(_ai_event_base(
            cluster=cluster,
            event_type="gpu_memory_pressure",
            severity="critical",
            title="GPU 显存或 KV Cache 压力过高",
            description=f"窗口峰值 KV Cache={kv_cache:g}%, GPU 显存={gpu_mem:g}%，存在 OOM 或队列堆积风险。",
            metric="gpu_memory/kv_cache",
            value={"kv_cache": kv_cache, "gpu_memory_used": gpu_mem},
            threshold={"kv_cache": kv_threshold, "gpu_memory_used": gpu_mem_threshold},
            source=source,
            experiment_run_id=experiment_run_id,
            step_id=step_id,
            evidence=[
                signal_evidence("gpu", "kv_cache", kv_threshold),
                signal_evidence("gpu", "gpu_memory_used", gpu_mem_threshold),
            ],
        ))
    if gpu_util >= gpu_busy_threshold and running_current <= 0:
        events.append(_ai_event_base(
            cluster=cluster,
            event_type="gpu_busy_without_serving",
            severity="warning",
            title="GPU 高占用但无运行中请求",
            description=f"GPU 利用率窗口峰值={gpu_util:g}% 且当前 vLLM 运行中请求={running_current:g}，可能存在后台进程或异常负载。",
            metric="gpu_util/running_requests",
            value={"gpu_util": gpu_util, "running_requests": running_current},
            threshold={"gpu_util": gpu_busy_threshold, "running_requests": 0},
            source=source,
            experiment_run_id=experiment_run_id,
            step_id=step_id,
            evidence=[
                signal_evidence("gpu", "gpu_util", gpu_busy_threshold),
                {"source": "vllm", "signal": "running_requests", "value": running_current, "threshold": 0},
            ],
        ))
    node_pressure_signals = [
        ("node_cpu_usage", "CPU 使用率", node_cpu, node_cpu_threshold, "%"),
        ("node_memory_usage", "内存使用率", node_memory, node_memory_threshold, "%"),
        ("node_rootfs_usage", "根分区使用率", node_rootfs, node_rootfs_threshold, "%"),
        ("node_disk_read", "磁盘读取", node_disk_read, node_disk_read_threshold, "MB/s"),
        ("node_disk_write", "磁盘写入", node_disk_write, node_disk_write_threshold, "MB/s"),
        ("node_network_receive", "网络接收", node_network_receive, node_network_receive_threshold, "MB/s"),
        ("node_network_transmit", "网络发送", node_network_transmit, node_network_transmit_threshold, "MB/s"),
    ]
    breached_node_signals = [
        {"source": "node", "signal": key, "label": label, "value": value, "threshold": threshold, "unit": unit}
        for key, label, value, threshold, unit in node_pressure_signals
        if value >= threshold
    ]
    if breached_node_signals:
        worst = max(breached_node_signals, key=lambda item: float(item.get("value") or 0) / max(float(item.get("threshold") or 1), 0.0001))
        events.append(_ai_event_base(
            cluster=cluster,
            event_type="node_resource_pressure",
            severity="warning" if len(breached_node_signals) == 1 else "critical",
            title="智算节点基础资源压力过高",
            description=f"{worst['label']}={worst['value']:g}{worst['unit']} 超过阈值 {worst['threshold']:g}{worst['unit']}，可能影响 vLLM 推理时延或请求吞吐。",
            metric="node_resources",
            value={item["signal"]: item["value"] for item in breached_node_signals},
            threshold={item["signal"]: item["threshold"] for item in breached_node_signals},
            source=source,
            experiment_run_id=experiment_run_id,
            step_id=step_id,
            evidence=breached_node_signals,
        ))
    if success_rps == 0 and (running > 0 or waiting > 0):
        events.append(_ai_event_base(
            cluster=cluster,
            event_type="inference_success_rate_zero",
            severity="critical",
            title="推理成功速率为 0",
            description="存在运行中或等待请求，但成功请求速率为 0，可能是 vLLM 服务不可用或请求持续失败。",
            metric="vllm:request_success_total",
            value=success_rps,
            threshold=">0",
            source=source,
            experiment_run_id=experiment_run_id,
            step_id=step_id,
            evidence=[
                {"source": "vllm", "signal": "success_rps", "value": success_rps, "threshold": ">0"},
                {"source": "vllm", "signal": "running_requests", "value": running},
                {"source": "vllm", "signal": "waiting_requests", "value": waiting},
            ],
        ))
    if teller.get("available") and not teller_request_context.get("available"):
        event = _ai_event_base(
            cluster=cluster,
            event_type="teller_request_context_missing",
            severity="warning",
            title="TELLER 缺少请求级上下文",
            description="已采集到 CUDA/NVTX 细粒度 trace，但没有 step_execute_model[request_ids:...]，无法还原完整请求执行路径。",
            metric="teller.request_context",
            value="missing",
            threshold="request_id",
            source=source,
            experiment_run_id=experiment_run_id,
            step_id=step_id,
            evidence=[
                {"source": "teller", "signal": "total_events", "value": teller_summary.get("total_events", 0)},
                {"source": "teller", "signal": "hierarchy_events", "value": teller_summary.get("hierarchy_events", 0)},
                {"source": "teller", "signal": "request_context", "value": "missing"},
            ],
        )
        event["fine_grained_trace"] = {
            "available": teller.get("available"),
            "trace_dir": teller.get("trace_dir"),
            "summary": teller_summary,
            "request_context": teller_request_context,
            "top_kernels": teller_top_kernels[:8],
            "event_type_stats": teller_event_type_stats[:12],
        }
        events.append(event)
    if teller_request_context.get("available"):
        requests = teller_request_context.get("requests") if isinstance(teller_request_context.get("requests"), list) else []
        max_span = max([float(r.get("span_ms") or 0) for r in requests] or [0.0])
        if max_span >= teller_request_span_threshold:
            event = _ai_event_base(
                cluster=cluster,
                event_type="teller_request_latency_high",
                severity="warning",
                title="TELLER 请求级执行耗时升高",
                description=f"TELLER 识别到请求执行跨度 {max_span:g}ms，超过阈值 {teller_request_span_threshold:g}ms，可触发请求级根因分析。",
                metric="teller.request_span_ms",
                value=max_span,
                threshold=teller_request_span_threshold,
                source=source,
                experiment_run_id=experiment_run_id,
                step_id=step_id,
                evidence=[
                    {"source": "teller", "signal": "request_count", "value": teller_request_context.get("request_count", 0)},
                    {"source": "teller", "signal": "max_request_span_ms", "value": max_span, "threshold": teller_request_span_threshold},
                ],
            )
            event["fine_grained_trace"] = {
                "available": teller.get("available"),
                "trace_dir": teller.get("trace_dir"),
                "summary": teller_summary,
                "request_context": {
                    **teller_request_context,
                    "requests": requests[:20],
                },
                "top_kernels": teller_top_kernels[:8],
                "event_type_stats": teller_event_type_stats[:12],
            }
            events.append(event)
    if teller_kernel_p95 >= teller_kernel_p95_threshold:
        event = _ai_event_base(
            cluster=cluster,
            event_type="teller_kernel_bottleneck",
            severity="warning",
            title="TELLER Kernel P95 耗时异常",
            description=f"CUDA Kernel P95={teller_kernel_p95:g}ms，超过阈值 {teller_kernel_p95_threshold:g}ms，可能存在算子、显存或调度瓶颈。",
            metric="teller.kernel_p95_ms",
            value=teller_kernel_p95,
            threshold=teller_kernel_p95_threshold,
            source=source,
            experiment_run_id=experiment_run_id,
            step_id=step_id,
            evidence=[
                {"source": "teller", "signal": "kernel_p95_ms", "value": teller_kernel_p95, "threshold": teller_kernel_p95_threshold},
                {"source": "teller", "signal": "kernel_events", "value": teller_summary.get("kernel_events", 0)},
            ],
        )
        event["fine_grained_trace"] = {
            "available": teller.get("available"),
            "trace_dir": teller.get("trace_dir"),
            "summary": teller_summary,
            "request_context": {
                **teller_request_context,
                "requests": (teller_request_context.get("requests") if isinstance(teller_request_context.get("requests"), list) else [])[:20],
            },
            "top_kernels": teller_top_kernels[:8],
            "event_type_stats": teller_event_type_stats[:12],
        }
        events.append(event)
    return events


def _recent_ai_fault_run(
    cluster_id: str,
    *,
    action: str = "",
    lookback_s: int = _AI_EVENT_DEFAULT_WINDOW_SECONDS,
) -> Optional[Dict[str, Any]]:
    now = time.time()
    for run in _load_fault_runs():
        if str(run.get("target_mode") or "") != "k8s":
            continue
        if str(run.get("cluster_id") or "") != str(cluster_id or ""):
            continue
        if action and str(run.get("action") or "") != action:
            continue
        finished = float(run.get("finished_at") or run.get("started_at") or 0)
        if finished and now - finished <= lookback_s:
            return run
    return None


def _active_ai_fault_runs(
    cluster_id: str,
    *,
    lookback_s: int = _AI_EVENT_DEFAULT_WINDOW_SECONDS,
) -> List[Dict[str, Any]]:
    """Return recent real LLM fault runs that should trigger short-lived AI diagnosis."""
    now = time.time()
    runs = [
        run for run in _load_fault_runs()
        if str(run.get("target_mode") or "") == "k8s"
        and str(run.get("cluster_id") or "") == str(cluster_id or "")
    ]
    latest_cleanup = 0.0
    for run in runs:
        if str(run.get("action") or "") != "cleanup":
            continue
        latest_cleanup = max(latest_cleanup, float(run.get("finished_at") or run.get("started_at") or 0))

    active: List[Dict[str, Any]] = []
    for run in runs:
        if str(run.get("action") or "") not in {"inject", "experiment"}:
            continue
        started = float(run.get("started_at") or 0)
        finished = float(run.get("finished_at") or started or 0)
        if not finished or now - finished > lookback_s:
            continue
        if latest_cleanup and latest_cleanup >= started:
            continue
        active.append(run)
    return active


def _fault_run_observation_payload(run: Dict[str, Any]) -> Dict[str, Any]:
    """Extract compact real-experiment observation signals for Skill routing."""
    observation: Dict[str, Any] = {}
    stored_observation = run.get("observation") if isinstance(run.get("observation"), dict) else {}
    if stored_observation:
        observation.update({
            key: value
            for key, value in stored_observation.items()
            if value not in (None, "", [], {})
        })
    loadgen = run.get("loadgen") if isinstance(run.get("loadgen"), dict) else {}
    if loadgen:
        observation["loadgen_model"] = loadgen.get("model")
        log_summary: Dict[str, Any] = {}
        for result in loadgen.get("results") or []:
            if not isinstance(result, dict):
                continue
            stdout = str(result.get("stdout") or "").strip()
            if not stdout or not stdout.startswith("{"):
                continue
            try:
                parsed = json.loads(stdout.splitlines()[-1])
            except Exception:
                continue
            if isinstance(parsed, dict) and ("requests" in parsed or "success" in parsed):
                log_summary = parsed
        for key in ("requests", "success", "errors", "rps", "latency_p50_s", "latency_p95_s"):
            if key in log_summary:
                observation[f"loadgen_{key}"] = log_summary.get(key)
        try:
            loadgen_requests = int(log_summary.get("requests") or 0)
            loadgen_success = int(log_summary.get("success") or 0)
        except (TypeError, ValueError):
            loadgen_requests = 0
            loadgen_success = 0
        if loadgen_requests > 0:
            observation["loadgen_success_rate"] = loadgen_success / loadgen_requests
    result = run.get("result") if isinstance(run.get("result"), dict) else {}
    comparison = result.get("metrics_comparison") if isinstance(result.get("metrics_comparison"), dict) else {}
    snapshots = result.get("all_snapshots") if isinstance(result.get("all_snapshots"), list) else []
    metric_names: set[str] = set()
    max_running = 0.0
    max_waiting = 0.0
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            continue
        metrics = snapshot.get("metrics") if isinstance(snapshot.get("metrics"), dict) else {}
        metric_items = list(metrics.items())
        metric_items.extend(
            (key, value)
            for key, value in snapshot.items()
            if key in {
                "throughput_rps",
                "avg_latency_ms",
                "p99_latency_ms",
                "gpu_utilization",
                "error_rate",
                "ttft_ms",
                "tpot_ms",
                "itl_ms",
                "tokens_per_second",
                "kv_cache_usage_pct",
                "num_preemptions",
                "gpu_power_watts",
                "gpu_temperature_c",
                "active_requests",
            }
        )
        for name, value in metric_items:
            metric_names.add(str(name))
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if "num_requests_running" in str(name) or str(name) == "active_requests":
                max_running = max(max_running, number)
            if "num_requests_waiting" in str(name):
                max_waiting = max(max_waiting, number)
    if metric_names:
        observation["metrics_status"] = "ok"
        observation["metrics_count"] = len(metric_names)
    if max_running:
        observation["running_requests"] = max_running
    if max_waiting:
        observation["waiting_requests"] = max_waiting
    if comparison:
        observation["metrics_comparison_keys"] = sorted(comparison.keys())
        compact_comparison: Dict[str, Dict[str, float]] = {}
        for phase in ("baseline", "fault", "delta"):
            values = comparison.get(phase) if isinstance(comparison.get(phase), dict) else {}
            compact_values: Dict[str, float] = {}
            for name, value in values.items():
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(number):
                    compact_values[str(name)] = number
            if compact_values:
                compact_comparison[phase] = compact_values
        if compact_comparison:
            observation["metric_baseline"] = compact_comparison.get("baseline", {})
            observation["metric_fault"] = compact_comparison.get("fault", {})
            observation["metric_deltas"] = compact_comparison.get("delta", {})
            for signal, value in compact_comparison.get("fault", {}).items():
                if str(signal).startswith("node_"):
                    observation[str(signal)] = value
    if result.get("injection"):
        observation["injection_success"] = bool((result.get("injection") or {}).get("success"))
    recovery = result.get("recovery") if isinstance(result.get("recovery"), list) else []
    if recovery:
        observation["recovery_success"] = all(bool(item.get("success")) for item in recovery if isinstance(item, dict))
        observation["recovery_layers"] = [item.get("layer") for item in recovery if isinstance(item, dict)]
    return {key: value for key, value in observation.items() if value not in (None, "", [], {})}


async def _collect_llm_fault_live_observation(
    *,
    cluster_id: Optional[str],
    namespace: str,
    deployment: str,
    service: str,
    delay_s: float = 0.0,
) -> Dict[str, Any]:
    """Capture a lightweight post-action observation for non-experiment fault runs."""
    if delay_s > 0:
        await asyncio.sleep(min(max(delay_s, 0.0), 15.0))
    payload = {
        "cluster_id": cluster_id or "",
        "namespace": namespace,
        "deployment": deployment,
        "service": service or deployment,
    }
    try:
        return await asyncio.to_thread(_ai_event_live_observation, payload)
    except Exception:
        logger.debug("failed to collect LLM fault live observation", exc_info=True)
        return {}


def _merge_observation(target: Dict[str, Any], source: Dict[str, Any]) -> Dict[str, Any]:
    """Merge observed facts without overwriting already captured non-empty values."""
    for key, value in (source or {}).items():
        if value in (None, "", [], {}):
            continue
        if target.get(key) in (None, "", [], {}):
            target[key] = value
    return target


def _merge_live_observation(
    stored: Dict[str, Any],
    live: Dict[str, Any],
) -> Dict[str, Any]:
    """Prefer fresh values without replacing a successful status with a transient failure."""
    merged = dict(stored or {})
    for key, value in (live or {}).items():
        if value in (None, "", [], {}):
            continue
        previous = merged.get(key)
        if (
            str(key).endswith("_status")
            and str(previous or "").lower() == "ok"
            and str(value or "").lower() in {"failed", "unknown", "unavailable"}
        ):
            continue
        merged[key] = value
    return merged


def _generate_ai_events_from_fault_runs(
    cluster: Dict[str, Any],
    *,
    window_s: int = _AI_EVENT_DEFAULT_WINDOW_SECONDS,
) -> List[Dict[str, Any]]:
    cluster_id = str(cluster.get("id") or "")
    events: List[Dict[str, Any]] = []
    for run in _active_ai_fault_runs(cluster_id, lookback_s=window_s):
        scenario_id = str(run.get("scenario_id") or "")
        fault_type = str(run.get("fault_type") or "fault_experiment_signal")
        layer = str(run.get("layer") or "unknown")
        family = ""
        normalized_fault_type = fault_type or "fault_experiment_signal"
        try:
            from skill_runtime.fault_taxonomy import (
                llm_experiment_family,
                normalize_llm_experiment_fault_type,
            )
            family = llm_experiment_family(fault_type)
            normalized_fault_type = normalize_llm_experiment_fault_type(fault_type)
        except Exception:
            logger.debug("LLM fault taxonomy unavailable; preserving raw fault_type", exc_info=True)
        severity = "critical" if family in {"process", "gpu_memory", "driver_cuda", "distributed_comm"} else "warning"
        if fault_type in {"process_crash", "process_hang", "gpu_oom", "cuda_error", "cuda_ctx_corruption"}:
            severity = "critical"
        mapped_fault_type = _llm_scenario_to_k8s_fault_type(fault_type)
        observable_title = "智算运行异常：推理服务观测信号异常"
        observable_description = "检测到真实运行窗口内的智算服务健康、请求或指标异常，需要基于观测证据触发检测、诊断和恢复闭环。"
        event = _ai_event_base(
            cluster=cluster,
            event_type="fault_experiment_signal",
            severity=severity,
            title=observable_title,
            description=observable_description,
            metric="fault_run",
            value="observable_anomaly",
            threshold="diagnose",
            source="fault_experiment",
            experiment_run_id=str(run.get("id") or ""),
            evidence=[],
        )
        observation = _fault_run_observation_payload(run)
        live_payload = {
            "cluster_id": cluster_id,
            "namespace": event.get("namespace") or cluster.get("default_namespace") or "llm-k8s",
            "deployment": event.get("deployment") or cluster.get("deployment") or "vllm-server",
            "service": event.get("service") or cluster.get("service") or cluster.get("deployment") or "vllm-server",
            "observation_window_started_at": run.get("started_at") or 0,
        }
        live_observation = _ai_event_live_observation(live_payload)
        observation = _merge_live_observation(observation, live_observation)
        if observation:
            _update_fault_run_observation_extrema(str(run.get("id") or ""), observation)
            event["observation"] = observation
            event["evidence"].extend(
                {"source": "observation", "signal": key, "value": value}
                for key, value in observation.items()
            )
        event["fault_type"] = "fault_experiment_signal"
        event["normalized_fault_type"] = "fault_experiment_signal"
        event["heal_fault_type"] = mapped_fault_type
        event["evaluation_context"] = {
            "id": run.get("id"),
            "action": run.get("action"),
            "scenario_id": scenario_id,
            "fault_type": fault_type,
            "normalized_fault_type": normalized_fault_type,
            "fault_family": family,
            "layer": layer,
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
        }
        events.append(event)
    return events


def _ai_fault_type_for_skill(raw_fault_type: str, normalized_fault_type: str = "") -> str:
    """Map AI fault experiment taxonomy to the skill-runtime fault vocabulary."""
    raw = str(raw_fault_type or "").strip()
    normalized = str(normalized_fault_type or "").strip()
    try:
        from skill_runtime.fault_taxonomy import llm_experiment_spec
        spec = llm_experiment_spec(raw)
        diagnosis_fault_type = str(spec.get("diagnosis_fault_type") or "").strip()
        if diagnosis_fault_type:
            return diagnosis_fault_type
    except Exception:
        logger.debug("LLM fault taxonomy unavailable while mapping AI event fault type", exc_info=True)
    if raw in {"process_crash", "process_hang"}:
        return "vllm_process_crash"
    if raw in {"gpu_oom", "cuda_ctx_corruption", "cuda_error"}:
        return "gpu_oom"
    if raw in {"pcie_throttle", "gpu_clock_throttle", "bandwidth_throttle", "rdma_failure"}:
        return "gpu_resource_pressure"
    if raw in {"request_queue_overflow", "continuous_batch_disruption", "tokenizer_error", "weight_corruption"}:
        return raw
    if raw in {"packet_loss", "dns_error"}:
        return "endpoint_timeout"
    if normalized.startswith("LLM::"):
        tail = normalized.split("::", 1)[1]
        if "process" in tail or "availability" in tail:
            return "vllm_process_crash"
        if "memory" in tail or "oom" in tail:
            return "gpu_oom"
        if "queue" in tail or "latency" in tail or "scheduler" in tail:
            return raw or "endpoint_timeout"
    return raw or normalized or "fault_experiment_signal"


def _attach_ai_fault_context(event: Dict[str, Any]) -> Dict[str, Any]:
    cluster_id = str(event.get("cluster_id") or "")
    if event.get("experiment_run_id"):
        return event
    run = _recent_ai_fault_run(cluster_id, action="experiment") or _recent_ai_fault_run(cluster_id, action="inject")
    if not run:
        return event
    event["experiment_run_id"] = run.get("id") or ""
    event["evaluation_context"] = {
        "id": run.get("id"),
        "action": run.get("action"),
        "scenario_id": run.get("scenario_id"),
        "fault_type": run.get("fault_type"),
        "layer": run.get("layer"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
    }
    return event


def _merge_ai_events_refresh(
    generated: List[Dict[str, Any]],
    *,
    cluster_ids: List[str],
) -> List[Dict[str, Any]]:
    existing_events = _load_ai_events()
    existing = {item.get("id"): item for item in existing_events}
    generated_ids = {event.get("id") for event in generated}
    detection = _ai_event_detection_config()
    required = int(detection.get("confirmation_points") or 1)

    for event in generated:
        event = _attach_ai_fault_context(event)
        old = existing.get(event.get("id")) or {}
        source = str(event.get("source") or "")
        required_confirmations = 1 if source == "fault_experiment" else required
        old_confirmations = int(old.get("confirmations") or 0) if old.get("status") in {"active", "observing"} else 0
        confirmations = max(1, old_confirmations + 1)
        preserved_status = old.get("status") if old.get("status") in {"acknowledged", "diagnosed", "healing"} else ""
        event["status"] = preserved_status or ("active" if confirmations >= required_confirmations else "observing")
        event["created_at"] = old.get("created_at") or event.get("created_at") or time.time()
        event["last_seen_at"] = time.time()
        event["confirmations"] = confirmations
        event["required_confirmations"] = required_confirmations
        event["detector"] = {
            **(event.get("detector") or {}),
            "algorithm": detection.get("algorithm"),
            "confirmation_mode": detection.get("confirmation_mode"),
        }
        event["diagnosis"] = old.get("diagnosis")
        event["skill_result"] = old.get("skill_result")
        event["agent_result"] = old.get("agent_result")
        event["agent_run"] = old.get("agent_run")
        event["agent_runs"] = old.get("agent_runs")
        event["heal_result"] = old.get("heal_result")
        event["resolution"] = old.get("resolution")
        _upsert_ai_event(event)

    if cluster_ids:
        now = time.time()
        for old in existing_events:
            if old.get("id") in generated_ids:
                continue
            if str(old.get("cluster_id") or "") not in cluster_ids:
                continue
            if old.get("source") not in {"ai_dashboard", "fault_experiment", "k8s_event", "k8s_pod_health"}:
                continue
            if old.get("status") not in {"active", "observing", "diagnosed", "healing"}:
                continue
            cleanup_run = _recent_ai_fault_run(str(old.get("cluster_id") or ""), action="cleanup")
            old["status"] = "resolved"
            old["resolved_at"] = now
            old["resolution"] = old.get("resolution") or (
                f"刷新时未再命中智算检测规则，且最近恢复动作 {cleanup_run.get('id')} 已执行，事件已自动恢复。"
                if cleanup_run else "刷新时未再命中智算检测规则，事件已自动恢复。"
            )
            _upsert_ai_event(old)

    return _load_ai_events()


async def _refresh_ai_events_after_fault_action(
    cluster: Optional[Dict[str, Any]],
    *,
    duration: str = "5m",
) -> Dict[str, Any]:
    """Run the normal metric detector after a real fault action.

    This keeps event creation tied to observed cluster signals and configured
    thresholds, while removing the requirement for an open browser timer.
    """
    if not isinstance(cluster, dict) or _cluster_kind(cluster) != "ai":
        return {"triggered": False, "count": 0, "active_count": 0, "events": []}
    try:
        payload = await _ai_event_light_dashboard_payload(cluster, duration=duration)
        generated = _generate_ai_events_from_dashboard_payload(payload, source="ai_dashboard")
        fault_events = await asyncio.to_thread(
            _generate_ai_events_from_fault_runs,
            cluster,
            window_s=_duration_seconds(duration, _AI_EVENT_DEFAULT_WINDOW_SECONDS),
        )
        generated.extend(fault_events)
        events = _merge_ai_events_refresh(generated, cluster_ids=[str(cluster.get("id") or "default")])
        cluster_events = [
            event for event in events
            if str(event.get("cluster_id") or "") == str(cluster.get("id") or "")
        ]
        active_events = [
            event for event in cluster_events
            if str(event.get("status") or "") in {"active", "observing", "diagnosed", "healing"}
        ]
        return {
            "triggered": True,
            "count": len(generated),
            "active_count": len(active_events),
            "events": _compact_ai_events_for_list(active_events, 20),
        }
    except Exception as exc:
        logger.warning("AI event refresh after fault action failed: %s", exc, exc_info=True)
        return {
            "triggered": False,
            "count": 0,
            "active_count": 0,
            "events": [],
            "message": str(exc),
        }


async def _delayed_ai_event_refresh(
    task_id: str,
    cluster: Dict[str, Any],
    delay_s: float,
) -> None:
    try:
        await asyncio.sleep(delay_s)
        await _refresh_ai_events_after_fault_action(cluster)
    finally:
        _state["ai_event_refresh_tasks"].pop(task_id, None)


def _schedule_ai_event_follow_up(
    cluster: Optional[Dict[str, Any]],
    *,
    fault_run_id: str,
    expected_detection_time_s: Any,
) -> Dict[str, Any]:
    if not isinstance(cluster, dict) or _cluster_kind(cluster) != "ai" or not fault_run_id:
        return {"scheduled": False}
    try:
        configured_delay = float(expected_detection_time_s or 15)
    except (TypeError, ValueError):
        configured_delay = 15.0
    delay_s = min(120.0, max(5.0, configured_delay))
    task_id = f"{fault_run_id}:follow-up"
    previous = _state["ai_event_refresh_tasks"].pop(task_id, None)
    if previous and not previous.done():
        previous.cancel()
    task = asyncio.create_task(_delayed_ai_event_refresh(task_id, dict(cluster), delay_s))
    _state["ai_event_refresh_tasks"][task_id] = task
    return {"scheduled": True, "delay_s": delay_s, "task_id": task_id}


_AI_EVENT_DIAGNOSIS_EVIDENCE_BLOCKLIST = {
    "scenario_id",
    "scenario",
    "fault_type",
    "raw_fault_type",
    "experiment_fault_type",
    "normalized_fault_type",
    "fault_family",
    "layer",
    "mapped_fault_type",
    "injection_success",
    "recovery_layers",
}


def _sanitize_ai_event_evidence_for_diagnosis(evidence: Any) -> List[Dict[str, Any]]:
    """Return only observable evidence for RCA input.

    Fault-lab truth labels are useful for offline evaluation, but they must not
    enter hypothesis generation, skill routing, or confidence calibration.
    """
    if not isinstance(evidence, list):
        return []
    sanitized: List[Dict[str, Any]] = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        signal = str(item.get("signal") or item.get("metric") or "").strip()
        source = str(item.get("source") or "").strip()
        if signal in _AI_EVENT_DIAGNOSIS_EVIDENCE_BLOCKLIST:
            continue
        if source in {"fault_run", "fault_experiment"} and signal in _AI_EVENT_DIAGNOSIS_EVIDENCE_BLOCKLIST:
            continue
        value = item.get("value")
        if value in (None, ""):
            continue
        sanitized.append(dict(item))
    return sanitized


def _sanitize_ai_event_observation_for_diagnosis(observation: Any) -> Dict[str, Any]:
    if not isinstance(observation, dict):
        return {}
    return {
        key: value
        for key, value in observation.items()
        if key not in _AI_EVENT_DIAGNOSIS_EVIDENCE_BLOCKLIST and value not in (None, "", [], {})
    }


def _ai_event_number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _append_candidate_signal(
    signals: List[Dict[str, Any]],
    *,
    signal: str,
    label: str,
    value: Any,
    threshold: Any,
    candidate_skill: str,
    candidate_fault: str,
    reason: str,
    severity: str = "warning",
) -> None:
    signals.append({
        "signal": signal,
        "label": label,
        "value": value,
        "threshold": threshold,
        "candidate_skill": candidate_skill,
        "candidate_fault": candidate_fault,
        "reason": reason,
        "severity": severity,
        "source": "observation",
    })


def _ai_event_observation_signals(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Convert observations into soft hypothesis candidates, not root-cause labels."""
    rules = (_ai_event_detection_config().get("rules") or {})
    signals: List[Dict[str, Any]] = []

    resource_specs = [
        ("node_cpu_usage", "节点 CPU 使用率", "node_cpu_usage", "k8s.resource_pressure", "HighCPUUsage"),
        ("node_memory_usage", "节点内存使用率", "node_memory_usage", "k8s.resource_pressure", "HighMemoryUsage"),
        ("node_rootfs_usage", "节点根分区使用率", "node_rootfs_usage", "k8s.resource_pressure", "DiskPressure"),
    ]
    for key, label, threshold_key, skill_id, fault in resource_specs:
        value = _ai_event_number(payload.get(key), 0.0)
        threshold = _ai_event_number(rules.get(threshold_key), 85.0)
        if value >= threshold:
            _append_candidate_signal(
                signals,
                signal=key,
                label=label,
                value=round(value, 3),
                threshold=threshold,
                candidate_skill=skill_id,
                candidate_fault=fault,
                reason=f"{label}达到 {value:.1f}%，超过阈值 {threshold:.1f}%",
            )

    gpu_specs = [
        ("gpu_memory_used_pct", "GPU 显存使用率", "gpu_memory_used", "llm.gpu_resource_pressure", "gpu_memory_pressure"),
        ("gpu_memory_usage", "GPU 显存使用率", "gpu_memory_used", "llm.gpu_resource_pressure", "gpu_memory_pressure"),
        ("kv_cache_usage", "KV Cache 使用率", "kv_cache", "llm.gpu_resource_pressure", "kv_cache_pressure"),
    ]
    seen_gpu_signal = set()
    for key, label, threshold_key, skill_id, fault in gpu_specs:
        if key in seen_gpu_signal:
            continue
        value = _ai_event_number(payload.get(key), 0.0)
        threshold = _ai_event_number(rules.get(threshold_key), 90.0)
        if value >= threshold:
            seen_gpu_signal.add(key)
            _append_candidate_signal(
                signals,
                signal=key,
                label=label,
                value=round(value, 3),
                threshold=threshold,
                candidate_skill=skill_id,
                candidate_fault=fault,
                reason=f"{label}达到 {value:.1f}%，超过阈值 {threshold:.1f}%",
            )

    success_rate_key = next(
        (
            key for key in (
                "request_success_ratio", "loadgen_success_rate", "success_rate",
                "request_success_rate", "request_success_rate_5m",
            )
            if key in payload
        ),
        "",
    )
    if success_rate_key:
        success_rate = _ai_event_number(payload.get(success_rate_key), 1.0)
        if 1.0 < success_rate <= 100.0:
            success_rate /= 100.0
        success_rate_min = _ai_event_number(rules.get("success_rate_min"), 0.99)
        if success_rate < success_rate_min:
            _append_candidate_signal(
                signals,
                signal="success_rate",
                label="vLLM 请求成功率",
                value=round(success_rate, 4),
                threshold=success_rate_min,
                candidate_skill="llm.business_slo",
                candidate_fault="business_success_rate_drop",
                reason=f"vLLM 请求成功率为 {success_rate:.1%}，低于阈值 {success_rate_min:.1%}",
                severity="critical" if success_rate <= 0.5 else "warning",
            )

    waiting_value = _ai_event_number(payload.get("waiting_requests"), 0.0)
    waiting_threshold = _ai_event_number(rules.get("waiting_requests"), 1.0)
    queue_backlog_observed = waiting_value >= waiting_threshold
    queue_specs = [
        ("waiting_requests", "vLLM 等待队列", "waiting_requests", "request_queue_overflow"),
        ("ttft_p95_seconds", "TTFT P95", "ttft_p95", "first_token_latency_high"),
        ("e2e_p95_seconds", "端到端 P95", "e2e_latency_p95", "e2e_latency_high"),
    ]
    for key, label, threshold_key, fault in queue_specs:
        value = _ai_event_number(payload.get(key), 0.0)
        threshold = _ai_event_number(rules.get(threshold_key), 1.0)
        if value >= threshold:
            scheduler_causal = key == "waiting_requests" or queue_backlog_observed
            _append_candidate_signal(
                signals,
                signal=key,
                label=label,
                value=round(value, 3),
                threshold=threshold,
                candidate_skill="llm.vllm_scheduler_queue" if scheduler_causal else "llm.business_slo",
                candidate_fault=fault,
                reason=f"{label}为 {value:g}，达到或超过阈值 {threshold:g}",
            )

    endpoint_status = str(payload.get("endpoint_status") or "").lower()
    endpoint_count = int(_ai_event_number(payload.get("endpoint_count"), -1.0))
    if endpoint_status in {"failed", "down", "unavailable"} or endpoint_count == 0:
        _append_candidate_signal(
            signals,
            signal="endpoint_status",
            label="模型服务入口",
            value=endpoint_status or endpoint_count,
            threshold="available endpoints > 0",
            candidate_skill="llm.model_serving_endpoint",
            candidate_fault="model_serving_endpoint_unavailable",
            reason="Service Endpoint 为空或推理端点不可用，需要验证 selector、Pod Ready 和端口",
            severity="critical",
        )

    cuda_evidence = cuda_runtime_evidence(payload)
    if cuda_evidence:
        _append_candidate_signal(
            signals,
            signal="cuda_runtime_error",
            label="CUDA/GPU 运行时",
            value=cuda_evidence[0],
            threshold="no CUDA or GPU driver runtime errors",
            candidate_skill="llm.cuda_driver_failure",
            candidate_fault="cuda_runtime_error",
            reason="；".join(cuda_evidence[:3]),
            severity="critical",
        )

    if has_abnormal_container_termination(payload):
        _append_candidate_signal(
            signals,
            signal="container_terminated_in_window",
            label="vLLM 工作负载生命周期",
            value=True,
            threshold="no abnormal container termination in observation window",
            candidate_skill="llm.vllm_process_crash",
            candidate_fault="vllm_process_restart",
            reason="观测窗口内 vLLM 容器发生异常终止或重启，需要核查进程退出和 K8s Event",
            severity="critical",
        )

    return signals


def _ai_event_background_observation_signals(event: Dict[str, Any], signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Separate slow-moving environment risks from experiment-local root-cause candidates."""
    if event.get("source") != "fault_experiment" or event.get("event_type") != "fault_experiment_signal":
        return []
    return [
        item for item in signals
        if str(item.get("signal") or item.get("metric") or "").startswith("node_")
        and not _ai_event_signal_has_window_causality(event, item)
    ]


def _ai_event_signal_has_window_causality(event: Dict[str, Any], signal: Dict[str, Any]) -> bool:
    """Return whether a candidate changed materially inside the observation window."""
    signal_name = str(signal.get("signal") or signal.get("metric") or "").strip()
    if not signal_name:
        return False
    if str(event.get("metric") or "").strip() == signal_name:
        return True

    observation = event.get("observation") if isinstance(event.get("observation"), dict) else {}
    baseline_values = observation.get("metric_baseline") if isinstance(observation.get("metric_baseline"), dict) else {}
    fault_values = observation.get("metric_fault") if isinstance(observation.get("metric_fault"), dict) else {}
    delta_values = observation.get("metric_deltas") if isinstance(observation.get("metric_deltas"), dict) else {}
    if signal_name in fault_values or signal_name in delta_values:
        baseline = _ai_event_number(baseline_values.get(signal_name), 0.0)
        fault = _ai_event_number(fault_values.get(signal_name), _ai_event_number(signal.get("value"), 0.0))
        delta = _ai_event_number(delta_values.get(signal_name), fault - baseline)
        threshold = _ai_event_number(signal.get("threshold"), 0.0)
        crossed_threshold = threshold > 0 and baseline < threshold <= fault
        material_delta = abs(delta) >= max(0.01, abs(baseline) * 0.2, abs(threshold) * 0.05)
        if crossed_threshold or material_delta:
            return True

    for item in event.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        item_signal = str(item.get("signal") or item.get("metric") or "").strip()
        if item_signal != signal_name:
            continue
        if item.get("changed_in_window") is True or any(
            key in item for key in ("delta", "baseline", "fault", "window_start", "window_end")
        ):
            return True
    return False


def _ai_event_candidate_observation_signals(event: Dict[str, Any], signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not signals:
        return []
    background = {
        id(item)
        for item in _ai_event_background_observation_signals(event, signals)
    }
    return [item for item in signals if id(item) not in background]


def _ai_event_to_skill_payload(
    event: Dict[str, Any],
    *,
    include_live_observation: bool = True,
) -> Dict[str, Any]:
    event_type = str(event.get("event_type") or "")
    raw_evidence = event.get("evidence") if isinstance(event.get("evidence"), list) else []
    evidence = _sanitize_ai_event_evidence_for_diagnosis(raw_evidence)
    observation = _sanitize_ai_event_observation_for_diagnosis(event.get("observation"))
    fault_status = ""
    if not fault_status:
        fault_status = str(next((item.get("value") for item in evidence if isinstance(item, dict) and item.get("signal") == "status"), "")).lower()
    fault_type = {
        "vllm_queue_backlog": "endpoint_timeout",
        "inference_latency_high": "endpoint_timeout",
        "gpu_memory_pressure": "gpu_oom",
        "gpu_busy_without_serving": "gpu_resource_pressure",
        "node_resource_pressure": "resource_pressure",
        "inference_success_rate_zero": "vllm_process_crash",
        "observability_unavailable": "observability_unavailable",
        "teller_request_context_missing": "observability_unavailable",
        "teller_request_latency_high": "teller_request_latency_high",
        "teller_kernel_bottleneck": "teller_kernel_bottleneck",
        "fault_experiment_signal": "fault_experiment_signal",
        "k8s_event_warning": event.get("fault_type") or "k8s_event_warning",
    }.get(event_type, event_type or "endpoint_timeout")
    if event.get("source") in {"k8s_event", "k8s_pod_health"}:
        fault_type = event.get("fault_type") or fault_type or "k8s_event_warning"
    k8s_object_kind = str(event.get("k8s_object_kind") or "").strip()
    k8s_object_name = str(event.get("k8s_object_name") or "").strip()
    k8s_reason = str(event.get("k8s_reason") or "").strip()
    k8s_orchestration_faults = {
        "scheduling_failed",
        "image_pull_error",
        "pod_crashloop",
        "oom_killed",
        "config_missing",
        "volume_mount_failed",
        "node_not_ready",
        "dns_resolution",
        "network_policy_deny",
        "hpa_scaling_failed",
        "resource_quota_exceeded",
        "service_unavailable",
    }
    skill_domain = "k8s" if event.get("source") in {"k8s_event", "k8s_pod_health"} and fault_type in k8s_orchestration_faults else "llm"
    k8s_object_ref = f"{k8s_object_kind}/{k8s_object_name}" if k8s_object_kind and k8s_object_name else k8s_object_name
    pod_name = k8s_object_name if k8s_object_kind.lower() == "pod" else ""
    if event_type == "fault_experiment_signal":
        fault_type = "fault_experiment_signal"
    payload = {
        "source": "ai_event_center",
        "domain": skill_domain,
        "event_id": event.get("id"),
        "cluster_id": event.get("cluster_id") or "",
        "namespace": event.get("namespace") or "llm-k8s",
        "deployment": event.get("deployment") or "vllm-server",
        "service": event.get("service") or event.get("deployment") or "vllm-server",
        "pod": pod_name,
        "object": k8s_object_ref,
        "model": event.get("model") or "",
        "endpoint": event.get("vllm_endpoint") or "",
        "fault_type": fault_type,
        "raw_fault_type": k8s_reason or event_type,
        "severity": event.get("severity") or "warning",
        "message": " | ".join(str(item) for item in [
            event.get("title") or "",
            k8s_object_ref,
            k8s_reason,
            event.get("description") or "",
        ] if str(item or "").strip()),
        "root_cause_component": k8s_object_name or event.get("deployment") or event.get("service") or "vllm-server",
        "evidence": evidence,
        "fault_execution_dry_run": fault_status in {"dry_run", "planned"},
        "metrics": {
            "metric": event.get("metric"),
            "value": event.get("value"),
            "threshold": event.get("threshold"),
        },
        "observation_thresholds": dict((_ai_event_detection_config().get("rules") or {})),
        "k8s_event": {
            "reason": k8s_reason,
            "object_kind": k8s_object_kind,
            "object_name": k8s_object_name,
            "namespace": event.get("namespace") or "",
        },
    }
    if isinstance(event.get("fine_grained_trace"), dict):
        payload["fine_grained_trace"] = event.get("fine_grained_trace")
        trace = payload["fine_grained_trace"]
        summary = trace.get("summary") if isinstance(trace.get("summary"), dict) else {}
        request_context = trace.get("request_context") if isinstance(trace.get("request_context"), dict) else {}
        payload["fine_grained_trace_summary"] = summary
        payload["request_context"] = request_context
        payload["top_kernels"] = trace.get("top_kernels") if isinstance(trace.get("top_kernels"), list) else []
        payload["teller_kernel_p95_ms"] = summary.get("kernel_p95_ms")
        requests = request_context.get("requests") if isinstance(request_context.get("requests"), list) else []
        payload["teller_request_count"] = request_context.get("request_count") or len(requests)
        payload["teller_max_request_span_ms"] = max([float(item.get("span_ms") or 0) for item in requests if isinstance(item, dict)] or [0.0])
        payload["thresholds"] = {
            "teller_request_span_ms": event.get("threshold") if event_type == "teller_request_latency_high" else 1000.0,
            "teller_kernel_p95_ms": event.get("threshold") if event_type == "teller_kernel_bottleneck" else 50.0,
        }
    if observation:
        payload["observation"] = observation
        payload.update(observation)
    live_observation = _ai_event_live_observation(payload) if include_live_observation else {}
    if live_observation:
        payload["live_observation"] = live_observation
        for key, value in live_observation.items():
            payload.setdefault(key, value)
        payload.setdefault("observation", {}).update(
            {key: value for key, value in live_observation.items() if value not in (None, "", [], {})}
        )
    all_observation_signals = _ai_event_observation_signals(payload)
    background_signals = _ai_event_background_observation_signals(event, all_observation_signals)
    observation_signals = _ai_event_candidate_observation_signals(event, all_observation_signals)
    if background_signals:
        payload["background_observation_signals"] = background_signals
    if observation_signals:
        payload["observation_signals"] = observation_signals
        payload["candidate_skills"] = sorted({
            str(item.get("candidate_skill") or "")
            for item in observation_signals
            if str(item.get("candidate_skill") or "").strip()
        })
        payload["candidate_faults"] = sorted({
            str(item.get("candidate_fault") or "")
            for item in observation_signals
            if str(item.get("candidate_fault") or "").strip()
        })
        signal_summary = "；".join(str(item.get("reason") or "") for item in observation_signals[:4] if item.get("reason"))
        if signal_summary:
            payload["message"] = f"{payload.get('message') or ''} | 观测候选信号：{signal_summary}".strip(" |")
    return payload


def _prometheus_vllm_target_selector(
    base_url: str,
    *,
    deployment: str,
    service: str,
    model: str = "",
) -> tuple[str, Dict[str, Any]]:
    """Discover a target-specific Prometheus matcher from live vLLM labels."""
    identities = {
        value
        for value in (str(deployment or "").strip(), str(service or "").strip(), str(model or "").strip())
        if value
    }
    label_priority = ("instance", "deployment", "pod", "service", "job", "model_name", "model")
    candidates: List[tuple[int, Dict[str, Any], str]] = []
    for metric_name in ("vllm:gpu_cache_usage_perc", "vllm:num_requests_running"):
        query = f"last_over_time({metric_name}[15m])"
        for item in _prom_instant_values(base_url, query):
            labels = item.get("metric") if isinstance(item.get("metric"), dict) else {}
            score = 0
            matched_label = ""
            for label in label_priority:
                label_value = str(labels.get(label) or "").strip()
                if not label_value:
                    continue
                if label_value in identities:
                    score = 100 - label_priority.index(label)
                    matched_label = label
                    break
                if label == "pod" and any(label_value.startswith(f"{identity}-") for identity in identities):
                    score = 80
                    matched_label = label
                    break
            candidates.append((score, labels, matched_label))

    candidates.sort(key=lambda item: item[0], reverse=True)
    if candidates and candidates[0][0] > 0:
        score, labels, matched_label = candidates[0]
        value = str(labels.get(matched_label) or "")
        selector = f"{matched_label}={json.dumps(value, ensure_ascii=True)}"
        return selector, {
            "scope": "target",
            "selector": selector,
            "matched_label": matched_label,
            "matched_value": value,
            "score": score,
        }
    if len(candidates) == 1:
        return "", {"scope": "single_series", "selector": ""}
    if not candidates and deployment:
        selector = f"instance={json.dumps(deployment, ensure_ascii=True)}"
        return selector, {
            "scope": "target_candidate",
            "selector": selector,
            "matched_label": "instance",
            "matched_value": deployment,
            "reason": "no recent series available for label discovery",
        }
    return "", {
        "scope": "cluster",
        "selector": "",
        "reason": "target labels unavailable or ambiguous",
        "series_count": len(candidates),
    }


def _ai_event_live_observation(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Collect lightweight read-only signals for Agent+Skill confidence calibration."""
    cluster_id = str(payload.get("cluster_id") or "").strip() or None
    cluster = _find_k8s_cluster(cluster_id) if cluster_id else None
    namespace = str(payload.get("namespace") or "llm-k8s").strip() or "llm-k8s"
    deployment = str(payload.get("deployment") or "vllm-server").strip() or "vllm-server"
    service = str(payload.get("service") or deployment).strip() or deployment
    observation: Dict[str, Any] = {}

    deploy_res = _kubectl_heal_check(
        f"-n {shlex.quote(namespace)} get deploy/{shlex.quote(deployment)} -o json",
        cluster_id=cluster_id,
        timeout=8,
        parse_json=True,
    )
    if deploy_res.get("ok") and isinstance(deploy_res.get("json"), dict):
        deploy = deploy_res["json"]
        spec = deploy.get("spec") if isinstance(deploy.get("spec"), dict) else {}
        status = deploy.get("status") if isinstance(deploy.get("status"), dict) else {}
        desired = int(spec.get("replicas") or 0)
        ready = int(status.get("readyReplicas") or 0)
        available = int(status.get("availableReplicas") or 0)
        observation.update({
            "deployment_desired_replicas": desired,
            "deployment_ready_replicas": ready,
            "deployment_available_replicas": available,
            "process_count": 1 if ready > 0 else 0,
        })
        accelerator_visibility: Dict[str, str] = {}
        accelerator_requests: Dict[str, float] = {}
        accelerator_limits: Dict[str, float] = {}
        template = spec.get("template") if isinstance(spec.get("template"), dict) else {}
        pod_spec = template.get("spec") if isinstance(template.get("spec"), dict) else {}
        for container in pod_spec.get("containers") or []:
            if not isinstance(container, dict):
                continue
            container_name = str(container.get("name") or "container")
            for env in container.get("env") or []:
                if not isinstance(env, dict):
                    continue
                name = str(env.get("name") or "")
                if name in {"CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES"} and "value" in env:
                    accelerator_visibility[f"{container_name}:{name}"] = str(env.get("value") or "")
            resources = container.get("resources") if isinstance(container.get("resources"), dict) else {}
            for field, target in (("requests", accelerator_requests), ("limits", accelerator_limits)):
                values = resources.get(field) if isinstance(resources.get(field), dict) else {}
                for name, value in values.items():
                    resource_name = str(name)
                    if "gpu" not in resource_name.lower() and "accelerator" not in resource_name.lower():
                        continue
                    try:
                        target[resource_name] = target.get(resource_name, 0.0) + float(value)
                    except (TypeError, ValueError):
                        continue
        if accelerator_visibility:
            observation["accelerator_visibility"] = accelerator_visibility
        if accelerator_requests:
            observation["accelerator_resource_requests"] = accelerator_requests
        if accelerator_limits:
            observation["accelerator_resource_limits"] = accelerator_limits
        selector = build_label_selector(spec.get("selector"))
        pods_res = (
            _kubectl_heal_check(
                f"-n {shlex.quote(namespace)} get pods -l {shlex.quote(selector)} -o json",
                cluster_id=cluster_id,
                timeout=8,
                parse_json=True,
            )
            if selector
            else {"ok": False, "returncode": None}
        )
        if pods_res.get("ok") and isinstance(pods_res.get("json"), dict):
            pods = pods_res["json"].get("items") if isinstance(pods_res["json"].get("items"), list) else []
            observation["pod_count"] = len(pods)
            now = time.time()
            try:
                window_started_at = float(payload.get("observation_window_started_at") or 0)
            except (TypeError, ValueError):
                window_started_at = 0.0
            newest_created_at = 0.0
            restart_count = 0
            recreated_in_window = False
            restarted_in_window = False
            terminated_in_window = False
            last_terminated_reason = ""
            last_exit_code: Optional[int] = None
            phase_counts: Counter[str] = Counter()
            waiting_reasons = set()
            waiting_messages = set()
            terminated_reasons = set()
            terminated_messages = set()
            log_targets: List[tuple] = []
            for pod in pods:
                if not isinstance(pod, dict):
                    continue
                metadata = pod.get("metadata") if isinstance(pod.get("metadata"), dict) else {}
                status = pod.get("status") if isinstance(pod.get("status"), dict) else {}
                pod_name = str(metadata.get("name") or "")
                phase = str(status.get("phase") or "Unknown")
                phase_counts[phase] += 1
                created_at = _parse_k8s_event_timestamp(metadata.get("creationTimestamp"))
                newest_created_at = max(newest_created_at, created_at)
                if window_started_at and created_at >= window_started_at - 5:
                    recreated_in_window = True
                for container in status.get("containerStatuses") or []:
                    if not isinstance(container, dict):
                        continue
                    container_name = str(container.get("name") or "")
                    container_restart_count = int(container.get("restartCount") or 0)
                    restart_count += container_restart_count
                    state = container.get("state") if isinstance(container.get("state"), dict) else {}
                    waiting = state.get("waiting") if isinstance(state.get("waiting"), dict) else {}
                    current_terminated = state.get("terminated") if isinstance(state.get("terminated"), dict) else {}
                    previous_terminated = (
                        ((container.get("lastState") or {}).get("terminated") or {})
                        if isinstance(container.get("lastState"), dict)
                        else {}
                    )
                    if waiting:
                        reason = str(waiting.get("reason") or "")
                        message = str(waiting.get("message") or "")
                        if reason:
                            waiting_reasons.add(reason)
                        if message:
                            waiting_messages.add(message[:500])
                    for terminated, previous in ((current_terminated, False), (previous_terminated, True)):
                        if not isinstance(terminated, dict) or not terminated:
                            continue
                        reason = str(terminated.get("reason") or "")
                        message = str(terminated.get("message") or "")
                        if reason:
                            terminated_reasons.add(reason)
                            last_terminated_reason = reason
                        if message:
                            terminated_messages.add(message[:500])
                        try:
                            last_exit_code = int(terminated.get("exitCode"))
                        except (TypeError, ValueError):
                            pass
                        finished_at = _parse_k8s_event_timestamp(terminated.get("finishedAt"))
                        if window_started_at and finished_at >= window_started_at - 5:
                            terminated_in_window = True
                            if previous:
                                restarted_in_window = True
                    waiting_reason = str(waiting.get("reason") or "").lower()
                    should_collect_logs = bool(
                        previous_terminated
                        or current_terminated
                        or container_restart_count > 0
                        or waiting_reason not in {"", "containercreating", "podinitializing"}
                    )
                    if should_collect_logs and pod_name and container_name:
                        log_targets.append((created_at, pod_name, container_name, bool(previous_terminated)))
            observation["pod_restart_count"] = restart_count
            if phase_counts:
                observation["pod_phase_counts"] = dict(sorted(phase_counts.items()))
            if waiting_reasons:
                observation["container_waiting_reasons"] = sorted(waiting_reasons)
            if waiting_messages:
                observation["container_waiting_messages"] = sorted(waiting_messages)
            if terminated_reasons:
                observation["container_terminated_reasons"] = sorted(terminated_reasons)
            if terminated_messages:
                observation["container_terminated_messages"] = sorted(terminated_messages)
            if newest_created_at:
                observation["pod_age_seconds"] = round(max(0.0, now - newest_created_at), 3)
            if recreated_in_window:
                observation["pod_recreated_in_window"] = True
            if restarted_in_window:
                observation["container_restarted_in_window"] = True
            if terminated_in_window:
                observation["container_terminated_in_window"] = True
            if last_terminated_reason:
                observation["last_terminated_reason"] = last_terminated_reason
            if last_exit_code is not None:
                observation["last_exit_code"] = last_exit_code
            log_samples = []
            seen_log_targets = set()
            for _, pod_name, container_name, previous in sorted(log_targets, reverse=True):
                target_key = (pod_name, container_name, previous)
                if target_key in seen_log_targets or len(log_samples) >= 2:
                    continue
                seen_log_targets.add(target_key)
                previous_flag = " --previous" if previous else ""
                log_res = _kubectl_heal_check(
                    f"-n {shlex.quote(namespace)} logs {shlex.quote(pod_name)} "
                    f"-c {shlex.quote(container_name)} --tail=80{previous_flag}",
                    cluster_id=cluster_id,
                    timeout=8,
                )
                if not log_res.get("ok") and previous:
                    log_res = _kubectl_heal_check(
                        f"-n {shlex.quote(namespace)} logs {shlex.quote(pod_name)} "
                        f"-c {shlex.quote(container_name)} --tail=80",
                        cluster_id=cluster_id,
                        timeout=8,
                    )
                    previous = False
                raw_log = str(log_res.get("stdout") or "") if log_res.get("ok") else ""
                excerpt = re.sub(r"\x1b\[[0-9;]*m", "", raw_log).strip()[-4000:]
                if excerpt:
                    log_samples.append({
                        "pod": pod_name,
                        "container": container_name,
                        "previous": previous,
                        "text": excerpt,
                    })
            if log_samples:
                observation["container_log_samples"] = log_samples
                observation["container_log_excerpt"] = "\n".join(
                    sample["text"] for sample in log_samples
                )[-6000:]
    else:
        observation.setdefault("process_count", 0)

    ep_res = _kubectl_heal_check(
        f"-n {shlex.quote(namespace)} get endpoints/{shlex.quote(service)} -o json",
        cluster_id=cluster_id,
        timeout=8,
        parse_json=True,
    )
    endpoint_count = 0
    if ep_res.get("ok") and isinstance(ep_res.get("json"), dict):
        subsets = ep_res["json"].get("subsets") if isinstance(ep_res["json"].get("subsets"), list) else []
        for subset in subsets:
            if not isinstance(subset, dict):
                continue
            endpoint_count += len(subset.get("addresses") or [])
        observation["endpoint_count"] = endpoint_count
        observation["endpoint_status"] = "ok" if endpoint_count > 0 else "failed"
    elif ep_res.get("returncode") is not None:
        observation["endpoint_status"] = "failed"

    base_url = _reachable_cluster_prometheus_url(cluster or {}, "")
    if base_url and _prometheus_api_ok(base_url):
        observation["metrics_status"] = "ok"
        selector, selector_meta = _prometheus_vllm_target_selector(
            base_url,
            deployment=deployment,
            service=service,
            model=str((cluster or {}).get("model") or ""),
        )
        if selector_meta:
            observation["metrics_target"] = selector_meta

        def metric(name: str) -> str:
            return f"{name}{{{selector}}}" if selector else name

        metric_queries = {
            "gpu_utilization": "avg(DCGM_FI_DEV_GPU_UTIL)",
            "gpu_memory_used_pct": "sum(DCGM_FI_DEV_FB_USED) / sum(DCGM_FI_DEV_FB_TOTAL) * 100",
            "gpu_count": "count(DCGM_FI_DEV_GPU_UTIL)",
            "node_cpu_usage": 'avg(1 - rate(node_cpu_seconds_total{mode="idle"}[1m])) * 100',
            "node_memory_usage": "avg(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100",
            "node_rootfs_usage": 'max(100 * (1 - node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay"} / node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay"}))',
            "node_disk_read": "sum(rate(node_disk_read_bytes_total[1m])) / 1024 / 1024",
            "node_disk_write": "sum(rate(node_disk_written_bytes_total[1m])) / 1024 / 1024",
            "node_network_receive": 'sum(rate(node_network_receive_bytes_total{device!="lo"}[1m])) / 1024 / 1024',
            "node_network_transmit": 'sum(rate(node_network_transmit_bytes_total{device!="lo"}[1m])) / 1024 / 1024',
        }
        if selector or selector_meta.get("scope") == "single_series":
            metric_queries.update({
                "running_requests": f"sum({metric('vllm:num_requests_running')})",
                "waiting_requests": f"sum({metric('vllm:num_requests_waiting')})",
                "success_rps": f"sum(rate({metric('vllm:request_success_total')}[1m]))",
                "ttft_p95_seconds": f"histogram_quantile(0.95, sum by(le)(rate({metric('vllm:time_to_first_token_seconds_bucket')}[1m])))",
                "tpot_p95_seconds": f"histogram_quantile(0.95, sum by(le)(rate({metric('vllm:time_per_output_token_seconds_bucket')}[1m])))",
                "e2e_p95_seconds": f"histogram_quantile(0.95, sum by(le)(rate({metric('vllm:e2e_request_latency_seconds_bucket')}[1m])))",
                "kv_cache_usage": f"max({metric('vllm:gpu_cache_usage_perc')}) * 100",
            })
        metrics_count = 0
        for key, query in metric_queries.items():
            value = _prom_instant_first_value(base_url, query)
            if value is not None:
                metrics_count += 1
                observation[key] = value

        try:
            window_started_at = float(payload.get("observation_window_started_at") or 0)
        except (TypeError, ValueError):
            window_started_at = 0.0
        if window_started_at > 0 and selector:
            now = time.time()
            configured_lookback_m = max(
                1,
                int(getattr(_get_config().detection, "default_lookback_m", 15) or 15),
            )
            requested_lookback_s = _to_float(payload.get("observation_lookback_seconds"), 0.0)
            baseline_s = min(86400, max(60, int(requested_lookback_s or configured_lookback_m * 60)))
            incident_s = min(3600, max(30, int(now - window_started_at) + 15))
            baseline_start = max(1.0, window_started_at - baseline_s)
            baseline_end = max(baseline_start, window_started_at - 1)
            incident_start = max(1.0, window_started_at)
            step_s = max(5, min(30, incident_s // 60 or 5))
            scoped_queries = {
                "running_requests": f"sum({metric('vllm:num_requests_running')})",
                "waiting_requests": f"sum({metric('vllm:num_requests_waiting')})",
                "kv_cache_usage": f"max({metric('vllm:gpu_cache_usage_perc')}) * 100",
            }
            context_windows: Dict[str, Dict[str, Any]] = {}
            for key, query in scoped_queries.items():
                baseline = _prom_range_numeric_summary(
                    base_url, query, start=baseline_start, end=baseline_end, step_s=step_s,
                )
                incident = _prom_range_numeric_summary(
                    base_url, query, start=incident_start, end=now, step_s=step_s,
                )
                current = observation.get(key)
                incident_max = incident.get("max")
                if isinstance(current, (int, float)):
                    observation[f"{key}_current"] = current
                if isinstance(incident_max, (int, float)):
                    observation[key] = max(float(current), incident_max) if isinstance(current, (int, float)) else incident_max
                baseline_max = baseline.get("max")
                context_windows[key] = {
                    "baseline": baseline,
                    "incident": incident,
                    "current": current,
                    "max_delta": (
                        round(float(incident_max) - float(baseline_max), 6)
                        if isinstance(incident_max, (int, float)) and isinstance(baseline_max, (int, float))
                        else None
                    ),
                }
            if context_windows:
                observation["observation_context"] = {
                    "event_started_at": window_started_at,
                    "baseline_window_seconds": baseline_s,
                    "incident_window_seconds": incident_s,
                    "step_seconds": step_s,
                    "signals": context_windows,
                }
        observation["metrics_count"] = max(int(observation.get("metrics_count") or 0), metrics_count)
    elif base_url:
        observation["metrics_status"] = "failed"

    return {key: value for key, value in observation.items() if value not in (None, "", [], {})}


def _normalize_ai_event_skill_result(
    event: Dict[str, Any],
    payload: Dict[str, Any],
    result: Dict[str, Any],
) -> Dict[str, Any]:
    diagnosis = result.get("diagnosis") if isinstance(result.get("diagnosis"), dict) else {}
    preferred_fault_type = str(payload.get("fault_type") or event.get("event_type") or "").strip()
    if event.get("source") == "fault_experiment":
        preferred_fault_type = ""
    raw_skill_fault_type = diagnosis.get("fault_type")
    keep_skill_fault_type = (
        isinstance(raw_skill_fault_type, str)
        and raw_skill_fault_type.startswith("LLM::")
        and preferred_fault_type
        and not preferred_fault_type.startswith("LLM::")
        and (
            event.get("source") == "fault_experiment"
            or str(event.get("event_type") or "").startswith("teller_")
            or str(diagnosis.get("skill_id") or result.get("primary_skill_id") or "").startswith("llm.teller")
        )
    )
    root_map = {
        "endpoint_timeout": "vLLM 推理链路出现排队或时延升高，优先检查服务端点、Pod 就绪状态、GPU 资源压力和背景负载。",
        "gpu_oom": "GPU 显存或 KV Cache 压力过高，可能导致请求排队、OOM 或解码吞吐下降。",
        "gpu_resource_pressure": "GPU 利用率与 vLLM 请求状态不匹配，可能存在异常后台负载或资源竞争。",
        "vllm_process_crash": "vLLM 服务存在不可用或请求失败信号，需要检查进程、Pod 重启和 Service Endpoint。",
        "observability_unavailable": "智算 Prometheus 或 /metrics 采集不可用，需要先恢复观测链路后再执行指标型闭环。",
        "teller_request_context_missing": "TELLER 已采集到细粒度 trace，但缺少请求级 request_id，上下文链路不完整。",
        "teller_request_latency_high": "TELLER 请求级执行跨度升高，应沿请求执行路径检查 runtime、driver 和 kernel 瓶颈。",
        "teller_kernel_bottleneck": "TELLER 检测到 CUDA Kernel P95 耗时异常，可能存在算子、GPU 调度或资源竞争瓶颈。",
        "fault_experiment_signal": "智算运行窗口出现可观测异常，应结合端点、Pod、指标、请求和 Trace 证据分析。",
        "image_pull_error": "镜像拉取失败，常见原因是镜像 tag 不存在、镜像仓库不可达或 imagePullSecret 配置异常。",
        "scheduling_failed": "Pod 无法调度，常见原因是节点选择器、亲和性、污点容忍或资源配额不满足。",
    }
    root_phrase_map = {
        "image pull failed": "镜像拉取失败，常见原因是镜像 tag 不存在、镜像仓库不可达或 imagePullSecret 配置异常。",
        "scheduling failed": "Pod 无法调度，常见原因是节点选择器、亲和性、污点容忍或资源配额不满足。",
    }
    if preferred_fault_type:
        diagnosis["raw_skill_fault_type"] = raw_skill_fault_type
        if keep_skill_fault_type:
            diagnosis["fault_type"] = raw_skill_fault_type
            diagnosis["root_cause"] = diagnosis.get("root_cause") or root_map.get(preferred_fault_type) or event.get("description") or "智算事件中心已命中检测规则，需要结合证据确认根因。"
            diagnosis["summary"] = f"智算事件中心识别实验类型 {preferred_fault_type}；Skill 诊断根因为 {raw_skill_fault_type}。"
        else:
            diagnosis["fault_type"] = preferred_fault_type
            root_cause = diagnosis.get("root_cause")
            if isinstance(root_cause, str) and root_cause.strip().lower() in root_phrase_map:
                root_cause = root_phrase_map[root_cause.strip().lower()]
            diagnosis["root_cause"] = root_map.get(preferred_fault_type) or root_cause or event.get("description") or "智算事件中心已命中检测规则，需要结合证据确认根因。"
            diagnosis["summary"] = f"智算事件中心归一化诊断为 {preferred_fault_type}；原始 Skill 类型为 {raw_skill_fault_type or '-'}。"
        diagnosis["evidence"] = diagnosis.get("evidence") or event.get("evidence") or []
        diagnosis["confidence"] = diagnosis.get("confidence", 0.78)
    elif event.get("source") == "fault_experiment":
        diagnosis.setdefault("fault_type", raw_skill_fault_type or "LLM::observed_runtime_anomaly")
        diagnosis.setdefault("root_cause", "智算运行窗口出现可观测异常，需要基于端点、Pod、指标、请求和 Trace 证据判断根因。")
        diagnosis.setdefault("evidence", payload.get("evidence") or [])
        evidence = diagnosis.get("evidence") if isinstance(diagnosis.get("evidence"), list) else []
        metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
        base_confidence = float(diagnosis.get("confidence") or 0.72)
        confidence, explanation = calibrate_llm_fault_confidence(
            base_confidence=base_confidence,
            scenario_present=False,
            observation_present=bool(evidence or payload.get("observation") or payload.get("live_observation")),
            endpoint_status=str(payload.get("endpoint_status") or "unknown"),
            metrics_status=str(payload.get("metrics_status") or "unknown"),
            process_count=int(payload.get("process_count") or 0),
            gpu_count=int(payload.get("gpu_count") or 0),
            metrics_count=len(evidence) + len(metrics),
            waiting_requests=payload.get("waiting_requests"),
            running_requests=payload.get("running_requests"),
            dry_run=False,
        )
        existing_explanation = diagnosis.get("confidence_explanation") if isinstance(diagnosis.get("confidence_explanation"), dict) else {}
        runtime = result.get("skill_runtime") if isinstance(result.get("skill_runtime"), dict) else {}
        if runtime.get("selection_mode") == "agent_llm_exact_skill_calls" and existing_explanation.get("method") in {"evidence_calibrated_v1", "evidence_calibrated_v2"}:
            diagnosis["confidence_explanation"] = {
                **existing_explanation,
                "observation_validation": explanation,
                "notes": [
                    *(existing_explanation.get("notes") or []),
                    "AI 事件适配层只附加观测校验，未改写 Agent+Skill 统一公式的最终置信度。",
                ],
            }
        else:
            diagnosis["confidence"] = min(0.95, max(float(diagnosis.get("confidence") or 0.0), confidence))
            diagnosis["confidence_explanation"] = {
                **explanation,
                "method": "ai_event_blind_observable_signals_v1",
                "previous": existing_explanation,
                "notes": ["未使用实验场景 ID、注入故障类型或实验标签；仅使用观测证据校准置信度。"],
            }
    diagnosis = _align_ai_event_diagnosis_with_evidence(event, diagnosis)
    result["diagnosis"] = diagnosis
    heal_payload = result.get("heal_compat_payload")
    if preferred_fault_type and isinstance(heal_payload, dict):
        heal_payload["fault_type"] = diagnosis.get("fault_type") or preferred_fault_type
        heal_payload["root_cause"] = diagnosis.get("root_cause") or heal_payload.get("root_cause")
        heal_payload["root_cause_component"] = diagnosis.get("root_cause_component") or heal_payload.get("root_cause_component")
        compat_diagnosis = heal_payload.get("diagnosis") if isinstance(heal_payload.get("diagnosis"), dict) else {}
        compat_diagnosis["fault_type"] = diagnosis.get("fault_type") or preferred_fault_type
        compat_diagnosis["root_cause"] = diagnosis.get("root_cause") or compat_diagnosis.get("root_cause")
        compat_diagnosis["root_cause_component"] = diagnosis.get("root_cause_component") or compat_diagnosis.get("root_cause_component")
        compat_diagnosis["confidence"] = diagnosis.get("confidence", compat_diagnosis.get("confidence"))
        heal_payload["diagnosis"] = compat_diagnosis
        result["heal_compat_payload"] = heal_payload
    result["ai_event"] = {
        "id": event.get("id"),
        "event_type": event.get("event_type"),
        "title": event.get("title"),
        "confirmations": event.get("confirmations"),
        "required_confirmations": event.get("required_confirmations"),
    }
    return result


def _skill_workflow_run_summary(record: Dict[str, Any]) -> Dict[str, Any]:
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    diagnosis = result.get("diagnosis") if isinstance(result.get("diagnosis"), dict) else {}
    migration = result.get("migration") if isinstance(result.get("migration"), dict) else {}
    execution_plan = result.get("execution_plan") if isinstance(result.get("execution_plan"), dict) else {}
    runtime = result.get("skill_runtime") if isinstance(result.get("skill_runtime"), dict) else {}
    request_payload = (record.get("request") or {}).get("payload") if isinstance(record.get("request"), dict) else {}
    return {
        "id": record.get("id"),
        "runtime_run_id": record.get("runtime_run_id") or result.get("workflow_run_id"),
        "status": record.get("status"),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "duration_ms": record.get("duration_ms"),
        "domain": result.get("domain") or record.get("domain"),
        "dry_run": result.get("dry_run", record.get("dry_run")),
        "fault_type": diagnosis.get("fault_type") or (request_payload or {}).get("fault_type"),
        "primary_skill_id": runtime.get("primary_skill_id") or migration.get("primary_skill_id"),
        "matched_count": runtime.get("matched_count") or migration.get("skill_matched_count") or 0,
        "migration_mode": migration.get("mode"),
        "confidence": diagnosis.get("confidence"),
        "command_count": execution_plan.get("command_count") or 0,
        "bridge_mode": (result.get("dashboard_bridge") or {}).get("mode") or (record.get("dashboard_bridge") or {}).get("mode"),
    }


def _compact_skill_result_for_event(result: Dict[str, Any]) -> Dict[str, Any]:
    runtime = result.get("skill_runtime") if isinstance(result.get("skill_runtime"), dict) else {}
    plan = result.get("execution_plan") if isinstance(result.get("execution_plan"), dict) else {}
    commands = plan.get("commands") if isinstance(plan.get("commands"), list) else []
    diagnosis = result.get("diagnosis") if isinstance(result.get("diagnosis"), dict) else {}
    return {
        "status": result.get("status"),
        "domain": result.get("domain"),
        "dry_run": result.get("dry_run"),
        "workflow_run_id": result.get("workflow_run_id"),
        "dashboard_run_id": result.get("dashboard_run_id"),
        "dashboard_bridge": result.get("dashboard_bridge") if isinstance(result.get("dashboard_bridge"), dict) else {},
        "diagnosis": {
            "status": diagnosis.get("status"),
            "source": diagnosis.get("source"),
            "skill_id": diagnosis.get("skill_id"),
            "fault_type": diagnosis.get("fault_type"),
            "root_cause": diagnosis.get("root_cause"),
            "root_cause_component": diagnosis.get("root_cause_component"),
            "confidence": diagnosis.get("confidence"),
            "summary": diagnosis.get("summary"),
            "confidence_explanation": diagnosis.get("confidence_explanation"),
        },
        "skill_runtime": {
            "matched_count": runtime.get("matched_count", 0),
            "primary_skill_id": runtime.get("primary_skill_id") or diagnosis.get("skill_id") or "",
        },
        "execution_plan": {
            "dry_run": plan.get("dry_run", True),
            "command_count": plan.get("command_count", len(commands)),
            "commands": commands[:6],
        },
    }


def _finalize_ai_event_agent_skill_run(
    *,
    run_id: str,
    skill_workflow: Optional[Dict[str, Any]],
    result_dict: Dict[str, Any],
    status: str,
) -> None:
    """Persist Agent+Skill RCA results back to the AI event center."""
    if not isinstance(skill_workflow, dict):
        return
    payload = skill_workflow.get("payload") if isinstance(skill_workflow.get("payload"), dict) else {}
    if payload.get("source") != "ai_event_center":
        return
    event_id = str(payload.get("event_id") or "")
    if not event_id:
        return
    events = _load_ai_events()
    event = next((item for item in events if item.get("id") == event_id), None)
    if not event:
        return
    skill_result = result_dict.get("skill_runtime_result")
    if not isinstance(skill_result, dict):
        skill_result = result_dict.get("skill_runtime_result") if isinstance(result_dict.get("skill_runtime_result"), dict) else None
    if isinstance(skill_result, dict):
        normalized = _normalize_ai_event_skill_result(event, payload, skill_result)
        event["skill_result"] = _compact_skill_result_for_event(normalized)
        event["diagnosis"] = normalized.get("diagnosis") if isinstance(normalized.get("diagnosis"), dict) else event.get("diagnosis")
    runs = _ensure_ai_event_agent_runs(event)
    run_record = runs.get("agent_skill") if isinstance(runs.get("agent_skill"), dict) else {}
    run_record.update({
        "run_id": run_id,
        "mode": "agent_skill",
        "status": status,
        "finished_at": time.time() if status in {"completed", "failed"} else run_record.get("finished_at"),
    })
    runs["agent_skill"] = run_record
    event["agent_runs"] = runs
    event["agent_run"] = run_record
    event["status"] = "diagnosed" if status == "completed" else event.get("status", "active")
    if status == "completed":
        event["resolution"] = "Agent+Skill 已按假设、规划、调查、推理完成诊断。"
    _upsert_ai_event(event)


def _normalize_heal_fault_type(text: str) -> str:
    raw = str(text or "")
    lower = raw.lower()
    if HEAL_RECIPES_STATE.exists():
        matches = {r["fault_type"] for r in _load_heal_recipes()
                   if r.get("updated_at") and any(str(s).lower() in lower for s in r.get("signals", []) if s)}
        if len(matches) == 1:
            return matches.pop()
        # Ambiguous learned signals must not silently select the first strategy.
        if len(matches) > 1:
            return "UnknownFault"
    bracket_alert = re.search(r"\[([A-Za-z0-9_.:-]+)\]", raw)
    if bracket_alert:
        alert_name = bracket_alert.group(1).lower()
        if "kubeproxydown" in alert_name or "kube-proxy" in alert_name or "kubeproxy" in alert_name:
            return "KubeProxyUnhealthy"
        if "targetdown" in alert_name:
            if "kube-proxy" in lower or "kubeproxy" in lower:
                return "KubeProxyUnhealthy"
            if "apiserver" in lower or "api server" in lower or "kube-apiserver" in lower:
                return "ApiServerUnhealthy"
            if "kubelet" in lower:
                return "KubeletUnhealthy"
            if "etcd" in lower:
                return "EtcdUnhealthy"
            return "TargetDown"
        if "kubeapiserver" in alert_name or "apiserver" in alert_name:
            return "ApiServerUnhealthy"
        if "etcd" in alert_name:
            return "EtcdUnhealthy"
        if "kubelet" in alert_name:
            return "KubeletUnhealthy"
        if "nodeclocknotsynchronising" in alert_name or "nodeclocknotsynchronizing" in alert_name:
            return "NodeClockNotSynchronising"
    if any(x in lower for x in ("non-existent config key", "non-existent configmap key", "references non-existent key")):
        return "ConfigKeyMissing"
    if any(x in lower for x in ("oomkilled", "out of memory", "exit code 137", "oom")):
        return "OOMKilled"
    if any(x in lower for x in ("apparmor", "varmor", "sandbox.varmor.org")) and any(x in lower for x in ("createcontainererror", "blocked", "denied", "forbidden")):
        return "AppArmorProfileUnsupported"
    if any(x in lower for x in ("diskpressure", "disk pressure", "filesystem pressure", "disk full")):
        return "DiskPressure"
    if any(x in lower for x in ("dns", "coredns", "kube-dns")):
        return "DNSResolution"
    if any(x in lower for x in ("apiserver", "api server", "kube-apiserver")):
        return "ApiServerUnhealthy"
    if "etcd" in lower:
        return "EtcdUnhealthy"
    if "kubelet" in lower:
        return "KubeletUnhealthy"
    if any(x in lower for x in ("kube-proxy", "kubeproxy")):
        return "KubeProxyUnhealthy"
    if any(x in lower for x in ("exceeded quota", "forbidden: quota", "resourcequota", "limitrange", "denied the request")):
        return "ResourceQuotaExceeded"
    if any(x in lower for x in ("failedgetscale", "failedgetresourcemetric", "failedcomputemetricsreplicas", "unable to get target")):
        return "HPAScalingFailed"
    if any(x in lower for x in ("crashloopbackoff", "crash loop", "back-off")):
        return "PodCrashLoop"
    if any(x in lower for x in ("kubepodnotready", "pod not ready", "non-ready state")):
        return "KubePodNotReady"
    if any(x in lower for x in ("kubedeploymentreplicasmismatch", "replicas mismatch", "expected number of replicas")):
        return "KubeDeploymentReplicasMismatch"
    if any(x in lower for x in ("kubepodcrashlooping", "container waiting", "kubecontainerwaiting")):
        return "KubeContainerWaiting"
    if any(x in lower for x in ("targetdown", "target disappeared from prometheus target discovery")):
        return "TargetDown"
    if any(x in lower for x in ("nodeclocknotsynchronising", "clock not synchronising", "time sync")):
        return "NodeClockNotSynchronising"
    if any(x in lower for x in ("imagepullbackoff", "errimagepull", "image pull")):
        return "ImagePullError"
    if any(x in lower for x in ("nodenotready", "node not ready", "node is not ready", "not ready")):
        return "NodeNotReady"
    if any(x in lower for x in ("failedmount", "mountvolume", "failedattachvolume", "volumefailed")):
        return "VolumeMountFailed"
    if any(x in lower for x in ("configmap", "secret \"", "not registered", "non-existent config")):
        return "ConfigMissing"
    if any(x in lower for x in ("failedscheduling", "failed scheduling", "pending", "unschedulable", "insufficient cpu", "insufficient memory", "failedcreate")):
        return "SchedulingFailed"
    if any(x in lower for x in ("highcpu", "high cpu", "cpu throttle", "cpu throttl", "cpu usage", "container_cpu", "node_cpu", "cpu使用", " cpu ")):
        return "HighCPUUsage"
    if any(x in lower for x in ("highmemory", "high memory", "memory usage", "memory pressure", "container_memory", "node_memory", "内存")):
        return "HighMemoryUsage"
    if any(x in lower for x in ("readiness probe", "liveness probe", "probe failed", "unhealthy")):
        return "DeploymentUnhealthy"
    if any(x in lower for x in ("networkpolicy", "network policy", "network_deny")):
        return "NetworkPolicyDeny"
    if any(x in lower for x in ("resource_exhausted", "memory_pressure", "cpu_pressure")):
        return "ResourceExhausted"
    if any(x in lower for x in ("log error spike", "error spike", "log_spike")):
        return "LogErrorSpike"
    if any(x in lower for x in ("metric", "prometheus", "anomaly", "指标")):
        return "MetricAnomaly"
    if any(x in lower for x in ("serviceunavailable", "service unavailable", "5xx")):
        return "ServiceUnavailable"
    if any(x in lower for x in ("latency", "slow", "timeout", "connection refused")):
        return "HighLatency"
    return raw or "UnknownFault"


def _is_safe_k8s_name(value: Any) -> bool:
    return isinstance(value, str) and bool(re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,252}$", value.strip()))


def _extract_k8s_name(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ("pod", "node", "deployment", "network_policy", "networkpolicy", "network_chaos", "networkchaos", "object", "root_cause_component", "name"):
            found = _extract_k8s_name(value.get(key))
            if found:
                return found
        return ""
    text = str(value).strip()
    typed = re.search(r"\b(?:pod|deployment|deploy|node|service|svc)[/:=\s]+([a-zA-Z0-9][a-zA-Z0-9_.-]{0,252})\b", text, re.I)
    if typed and _is_safe_k8s_name(typed.group(1)):
        return typed.group(1)
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    text = re.sub(r"^(pod|deployment|deploy|node|service|svc)[/:]\s*", "", text, flags=re.I)
    return text if _is_safe_k8s_name(text) else ""


def _guess_deployment_from_pod(pod: str) -> str:
    parts = str(pod or "").split("-")
    if len(parts) >= 3 and re.match(r"^[a-z0-9]{5,12}$", parts[-2]) and re.match(r"^[a-z0-9]{4,10}$", parts[-1]):
        return "-".join(parts[:-2])
    return ""


def _materialize_heal_command(
    command: str,
    *,
    namespace: str,
    pod: str = "",
    deployment: str = "",
    node: str = "",
    service: str = "",
    network_policy: str = "",
    network_chaos: str = "",
) -> str:
    cmd = str(command or "").strip()
    replacements = {
        "<namespace>": namespace,
        "<ns>": namespace,
        "<dst-ns>": namespace,
        "<pod>": pod,
        "<deployment>": deployment,
        "<name>": deployment or pod,
        "<node>": node,
        "<svc>": service or deployment,
        "<service>": service or deployment,
        "<network_policy>": network_policy,
        "<networkpolicy>": network_policy,
        "<network_chaos>": network_chaos,
        "<networkchaos>": network_chaos,
    }
    for key, value in replacements.items():
        if value:
            cmd = cmd.replace(key, value)
    return cmd


def _has_heal_placeholder(command: Any) -> bool:
    return bool(re.search(r"<[^>\s]+>", str(command or "")))


def _validate_heal_command(command: str) -> Optional[str]:
    cmd = str(command or "").strip()
    if not cmd:
        return "命令为空"
    if _has_heal_placeholder(cmd):
        return "命令仍包含未解析占位符"
    if _HEAL_FORBIDDEN.search(cmd):
        return "命令包含被禁止的破坏性模式"
    if not cmd.startswith("kubectl "):
        return "只允许 kubectl 命令"
    return None


def _sample_materialize_heal_command(command: str) -> str:
    return _materialize_heal_command(
        command,
        namespace="default",
        pod="sample-pod-7d8c9f6b5d-x9q2p",
        deployment="sample-deploy",
        node="sample-node",
        service="sample-service",
    )


def _validate_heal_recipe_library(library: Dict[str, Any]) -> Dict[str, Any]:
    recipes = library.get("recipes") or []
    issues: List[Dict[str, Any]] = []
    seen_fault_types: set[str] = set()
    required = ("fault_type", "category", "risk", "description", "actions")

    for idx, recipe in enumerate(recipes):
        fault_type = str(recipe.get("fault_type") or "").strip()
        for field in required:
            if not recipe.get(field):
                issues.append({"severity": "error", "recipe": fault_type or f"index:{idx}", "field": field, "message": "missing required field"})
        if fault_type in seen_fault_types:
            issues.append({"severity": "error", "recipe": fault_type, "field": "fault_type", "message": "duplicate fault_type"})
        if fault_type:
            seen_fault_types.add(fault_type)

        actions = recipe.get("actions")
        if not isinstance(actions, list) or not actions:
            issues.append({"severity": "error", "recipe": fault_type or f"index:{idx}", "field": "actions", "message": "actions must be a non-empty list"})
            continue
        for action_idx, action in enumerate(actions):
            if not isinstance(action, dict):
                issues.append({"severity": "error", "recipe": fault_type, "field": f"actions[{action_idx}]", "message": "action must be an object"})
                continue
            command = str(action.get("command") or "").strip()
            if not action.get("step"):
                issues.append({"severity": "warning", "recipe": fault_type, "field": f"actions[{action_idx}].step", "message": "missing action step"})
            reason = _validate_heal_command(_sample_materialize_heal_command(command))
            if reason:
                issues.append({"severity": "error", "recipe": fault_type, "field": f"actions[{action_idx}].command", "message": reason, "command": command})
            rollback = str(action.get("rollback_command") or "").strip()
            if rollback:
                rollback_reason = _validate_heal_command(_sample_materialize_heal_command(rollback))
                if rollback_reason:
                    issues.append({"severity": "error", "recipe": fault_type, "field": f"actions[{action_idx}].rollback_command", "message": rollback_reason, "command": rollback})

    error_count = sum(1 for item in issues if item.get("severity") == "error")
    warning_count = sum(1 for item in issues if item.get("severity") == "warning")
    return {
        "ok": error_count == 0,
        "source": library.get("source"),
        "version": library.get("version"),
        "total": len(recipes),
        "validated_fault_types": len(seen_fault_types),
        "error_count": error_count,
        "warning_count": warning_count,
        "issues": issues,
    }


def _build_heal_verification_plan(namespace: str, target: Dict[str, Any], commands: List[str]) -> Dict[str, Any]:
    target = target if isinstance(target, dict) else {}
    deployment = _extract_k8s_name(target.get("deployment"))
    pod = _extract_k8s_name(target.get("pod"))
    service = _extract_k8s_name(target.get("service"))
    node = _extract_k8s_name(target.get("node"))
    network_policy = _extract_k8s_name(target.get("network_policy"))
    network_chaos = _extract_k8s_name(target.get("network_chaos"))
    commands_text = "\n".join(commands or [])
    checks: List[Dict[str, str]] = []

    if network_chaos and any(" delete networkchaos " in f" {cmd} " for cmd in commands or []):
        checks.append({
            "type": "networkchaos_removed",
            "command": f"kubectl get networkchaos {network_chaos} -n {namespace} --ignore-not-found -o name",
        })
        return {"mode": "network_chaos", "target": {"namespace": namespace, "network_chaos": network_chaos}, "checks": checks}
    if network_policy and any(" delete networkpolicy " in f" {cmd} " for cmd in commands or []):
        checks.append({
            "type": "networkpolicy_removed",
            "command": f"kubectl get networkpolicy {network_policy} -n {namespace} --ignore-not-found -o name",
        })
        return {"mode": "network_policy", "target": {"namespace": namespace, "network_policy": network_policy}, "checks": checks}
    if pod and any(" delete pod " in f" {cmd} " for cmd in commands):
        checks.append({
            "type": "pod_removed",
            "command": f"kubectl get pod {pod} -n {namespace} --ignore-not-found -o name",
        })
        return {"mode": "pod_removed", "target": {"namespace": namespace, "pod": pod}, "checks": checks}

    if deployment or re.search(r"\bdeployment/", commands_text):
        if not deployment:
            match = re.search(r"\bdeployment/([a-zA-Z0-9][a-zA-Z0-9_.-]{0,252})", commands_text)
            deployment = _extract_k8s_name(match.group(1) if match else "")
        checks.extend([
            {"type": "deployment_rollout", "command": f"kubectl rollout status deployment/{deployment or '<deployment>'} -n {namespace} --timeout=60s"},
            {"type": "deployment_status", "command": f"kubectl get deployment {deployment or '<deployment>'} -n {namespace} -o json"},
        ])
        return {"mode": "deployment", "target": {"namespace": namespace, "deployment": deployment}, "checks": checks}
    if pod:
        checks.append({"type": "pod_status", "command": f"kubectl get pod {pod} -n {namespace} -o json"})
        if any(" delete pod " in f" {cmd} " for cmd in commands or []):
            checks.append({"type": "namespace_pods_after_delete", "command": f"kubectl get pods -n {namespace} --no-headers"})
        return {"mode": "pod", "target": {"namespace": namespace, "pod": pod}, "checks": checks}
    if service:
        checks.append({"type": "service_endpoints", "command": f"kubectl get endpoints {service} -n {namespace} -o json"})
        return {"mode": "service", "target": {"namespace": namespace, "service": service}, "checks": checks}
    if node:
        checks.append({"type": "node_status", "command": f"kubectl get node {node} -o json"})
        return {"mode": "node", "target": {"node": node}, "checks": checks}
    checks.append({"type": "namespace_pods", "command": f"kubectl get pods -n {namespace} --no-headers"})
    return {"mode": "namespace", "target": {"namespace": namespace}, "checks": checks}


def _compact_heal_text(value: Any, limit: int = 1200) -> str:
    """Convert alert/RCA fragments to bounded text for deterministic matching."""
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)[:limit]
    try:
        return json.dumps(value, ensure_ascii=False, default=str)[:limit]
    except Exception:
        return str(value)[:limit]


def _extract_alert_object(alert: Dict[str, Any]) -> Dict[str, str]:
    raw = alert.get("raw_data") if isinstance(alert.get("raw_data"), dict) else {}
    involved = raw.get("involvedObject") if isinstance(raw.get("involvedObject"), dict) else {}
    labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
    kind = str(involved.get("kind") or labels.get("kind") or "").lower()
    name = involved.get("name") or alert.get("service") or labels.get("pod") or labels.get("deployment") or labels.get("node") or ""
    obj = {"pod": "", "deployment": "", "node": ""}
    if kind == "pod":
        obj["pod"] = _extract_k8s_name(name)
    elif kind in ("deployment", "replicaset", "statefulset", "daemonset"):
        obj["deployment"] = _extract_k8s_name(name)
    elif kind == "node":
        obj["node"] = _extract_k8s_name(name)
    service = _extract_k8s_name(alert.get("service") or "")
    title_name = _extract_k8s_name(alert.get("title") or "")
    if not obj["pod"] and service:
        obj["pod"] = service
    if not obj["pod"] and title_name and any(x in str(alert.get("title", "")).lower() for x in ("pod", "unhealthy pod")):
        obj["pod"] = title_name
    if not obj["node"] and (labels.get("node") or labels.get("instance")):
        obj["node"] = _extract_k8s_name(labels.get("node") or labels.get("instance"))
    return obj


def _heal_capability(
    *,
    recipe: Optional[Dict[str, Any]],
    suggestions: List[Dict[str, Any]],
    blocked: List[Dict[str, Any]],
) -> Dict[str, Any]:
    cfg = _get_config()
    risks = [str(s.get("risk", "low")).lower() for s in suggestions + blocked]
    high_risk = "high" in risks
    return {
        "available": bool(suggestions),
        "mode": "dry_run" if getattr(cfg.remediation, "dry_run", True) else "execution_ready",
        "execution_enabled": bool(cfg.remediation.enabled),
        "requires_approval": bool((recipe or {}).get("requires_approval", True) or cfg.remediation.require_approval),
        "requires_risk_ack": bool(high_risk and getattr(cfg.remediation, "require_confirmation_for_high_risk", True)),
        "supports_rollback": any(bool(s.get("rollback_command")) for s in suggestions),
        "max_auto_risk_level": getattr(cfg.remediation, "max_auto_risk_level", "medium"),
        "policy": "recommend_only" if getattr(cfg.remediation, "recommend_only", False) else "guarded_execute",
    }


def _recipe_min_confidence(recipe: Optional[Dict[str, Any]], cfg: Any) -> Optional[float]:
    if not recipe:
        return None
    raw = recipe.get("min_confidence", recipe.get("confidence_threshold"))
    if raw is None:
        return None
    return max(0.0, min(1.0, _to_float(raw, _to_float(getattr(cfg.remediation, "confidence_threshold", 0.85), 0.85))))


def _risk_rank(value: str) -> int:
    return {"low": 1, "medium": 2, "high": 3, "critical": 4}.get(str(value or "low").lower(), 2)


def _max_risk_label(suggestions: List[Dict[str, Any]]) -> str:
    best = "low"
    for item in suggestions:
        risk = str(item.get("risk", "low")).lower()
        if _risk_rank(risk) > _risk_rank(best):
            best = risk
    return best


def _heal_risk_impact(risk: str, step: str) -> str:
    if risk == "high":
        return "高风险动作，可能导致服务短暂中断或节点工作负载迁移，需审批和风险确认。"
    if risk == "medium":
        return "中风险动作，可能触发 Pod 重建、滚动发布或副本调整。"
    return "低风险动作，主要用于只读确认或轻量恢复。"


def _public_heal_recipe(recipe: Dict[str, Any]) -> Dict[str, Any]:
    """Expose recipes in an agenticSnail-compatible shape while keeping actions."""
    actions = list(recipe.get("actions") or recipe.get("example_commands") or [])
    tier = recipe.get("tier")
    if not tier:
        tier = "diagnostic" if all(str(a.get("risk", "low")).lower() == "low" for a in actions) else "kubectl"
    return {
        **recipe,
        "tier": tier,
        "coverage": recipe.get("coverage") or "tested",
        "how_fixed": recipe.get("how_fixed") or recipe.get("description") or "",
        "actions": actions,
        "example_commands": actions,
    }


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    return max(low, min(high, _to_int(value, default)))


def _diagnosis_evidence_count(diagnosis: Dict[str, Any]) -> int:
    evidence = diagnosis.get("evidence_summary")
    if isinstance(evidence, dict):
        return sum(1 for v in evidence.values() if v)
    if isinstance(evidence, list):
        return sum(1 for v in evidence if v)
    return 0


def _build_diagnosis_gate(
    diagnosis: Dict[str, Any],
    cfg: Any,
    recipe: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    config_threshold = _to_float(getattr(cfg.remediation, "confidence_threshold", 0.85), 0.85)
    recipe_threshold = _recipe_min_confidence(recipe, cfg)
    threshold = max(config_threshold, recipe_threshold or 0.0)
    confidence = _to_float(diagnosis.get("confidence"), 0.0) if diagnosis else 0.0
    root_cause = str((diagnosis or {}).get("root_cause") or "").strip()
    fault_type = str((diagnosis or {}).get("fault_type") or "").strip()
    evidence_count = _diagnosis_evidence_count(diagnosis or {})
    blockers = []
    blocker_messages = []
    if not diagnosis:
        blockers.append("missing_diagnosis")
        blocker_messages.append("缺少 RCA 诊断结果，真实自愈只能在 dry-run 下预演。")
    if diagnosis and confidence < threshold:
        key = "rule_min_confidence" if recipe_threshold is not None and confidence < recipe_threshold else "low_confidence"
        blockers.append(key)
        blocker_messages.append(f"诊断置信度 {confidence:.0%} 低于准入阈值 {threshold:.0%}。")
    if diagnosis and not root_cause:
        blockers.append("missing_root_cause")
        blocker_messages.append("诊断结果缺少明确根因。")
    if diagnosis and not fault_type:
        blockers.append("missing_fault_type")
        blocker_messages.append("诊断结果缺少故障类型。")
    return {
        "required_for_real_execution": True,
        "ready": not blockers,
        "blockers": blockers,
        "blocker_messages": blocker_messages,
        "confidence": confidence,
        "confidence_threshold": threshold,
        "config_confidence_threshold": config_threshold,
        "rule_min_confidence": recipe_threshold,
        "root_cause": root_cause,
        "fault_type": fault_type,
        "evidence_count": evidence_count,
    }


def _diagnosis_has_recent_code_change(diagnosis: Dict[str, Any]) -> bool:
    keys = (
        "recent_code_change",
        "code_changed",
        "has_code_change",
        "deployment_changed",
        "recent_deployment",
    )
    for key in keys:
        if key in diagnosis:
            return bool(diagnosis.get(key))
    context = diagnosis.get("environment_context") if isinstance(diagnosis.get("environment_context"), dict) else {}
    change_keys = keys + ("recent_rollout", "new_release")
    return any(bool(context.get(key)) for key in change_keys)


def _build_auto_execution_gate(
    *,
    recipe: Optional[Dict[str, Any]],
    suggestions: List[Dict[str, Any]],
    diagnosis: Dict[str, Any],
    cfg: Any,
    recovery_history_stats: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    confidence_threshold = _to_float(getattr(cfg.remediation, "auto_confidence_threshold", 0.9), 0.9)
    history_threshold = _to_float(getattr(cfg.remediation, "auto_history_success_rate_threshold", 0.8), 0.8)
    history_confidence_level = _to_float(getattr(cfg.remediation, "auto_history_confidence_level", 0.95), 0.95)
    history_min_samples = _to_int(getattr(cfg.remediation, "auto_history_min_samples", 14), 14)
    confidence = _to_float((diagnosis or {}).get("confidence"), 0.0)
    history = dict(recovery_history_stats or {})
    history_rate = _to_float(history.get("success_rate"), 0.0)
    history_samples = _to_int(history.get("sample_count"), 0)
    history_successes = _to_int(history.get("success_count"), -1)
    history_failures = _to_int(history.get("failure_count"), -1)
    counts_present = history_successes >= 0 and history_failures >= 0
    scope_required = True
    scope_complete = (history.get('scope_required') is True and history.get('scope_complete') is True and
                      bool(history.get('action_fingerprint') and history.get('environment_fingerprint')))
    if counts_present and history_samples <= 0:
        history_samples = history_successes + history_failures
    history_lower_bound = (
        clopper_pearson_lower_bound(
            history_successes,
            history_samples,
            confidence_level=history_confidence_level,
        )
        if counts_present and history_samples > 0
        else 0.0
    )
    sufficient_history = counts_present and history_samples >= history_min_samples
    safe_marked = bool((recipe or {}).get("safe_auto_execute") or (recipe or {}).get("safe_rerun") or (recipe or {}).get("allow_auto_execute"))
    automation_class = str((recipe or {}).get("automation_class") or "").strip().lower()
    manual_only = bool(
        (recipe or {}).get("manual_only")
        or (recipe or {}).get("auto_recovery_supported") is False
        or automation_class in {"manual", "manual_only", "unsupported"}
    )
    max_action_risk = max((_risk_rank(s.get("risk", "low")) for s in suggestions), default=1)
    max_allowed_risk = _risk_rank(getattr(cfg.remediation, "max_auto_risk_level", "medium"))
    rollback_ready = all(
        _risk_rank(item.get("risk", "low")) < _risk_rank("medium")
        or bool(str(item.get("rollback_command") or item.get("rollback") or "").strip())
        for item in suggestions
    )
    recent_code_change = _diagnosis_has_recent_code_change(diagnosis or {})
    blockers = []
    messages = []
    verification_complete = (history.get('verification_required') is True and
        history.get('verification_scope_complete') is True and
        valid_verification_fingerprint(history.get('verification_fingerprint')))
    if not verification_complete:
        blockers.append('verification_contract_missing')
        messages.append('历史恢复样本没有绑定当前恢复验证标准，不能支持全自动执行。')
    if not suggestions:
        blockers.append("no_executable_actions")
        messages.append("当前没有可执行自愈动作，不能进入全自动执行。")
    if confidence < confidence_threshold:
        blockers.append("confidence_below_auto_threshold")
        messages.append(f"诊断置信度 {confidence:.1%} 未达到全自动阈值 {confidence_threshold:.1%}。")
    if scope_required and not scope_complete:
        blockers.append("recovery_scope_incomplete")
        messages.append("恢复历史缺少动作或环境指纹，不能用宽泛的同故障类型记录支持全自动执行。")
    if not counts_present:
        blockers.append("history_outcome_counts_missing")
        messages.append("历史恢复记录缺少成功/失败计数，不能计算统计置信下界。")
    elif not sufficient_history:
        blockers.append("insufficient_history")
        messages.append(f"历史恢复样本 {history_samples} 条，未达到至少 {history_min_samples} 条的门槛。")
        if history_samples > 0 and history_lower_bound < history_threshold:
            blockers.append("history_success_lower_bound_low")
            messages.append(
                f"历史恢复成功率的 {history_confidence_level:.0%} 单侧置信下界 "
                f"{history_lower_bound:.0%} 低于 {history_threshold:.0%}。"
            )
    elif history_lower_bound < history_threshold:
        blockers.append("history_success_lower_bound_low")
        messages.append(
            f"历史恢复成功率的 {history_confidence_level:.0%} 单侧置信下界 "
            f"{history_lower_bound:.0%} 低于 {history_threshold:.0%}。"
        )
    if recent_code_change:
        blockers.append("recent_code_change")
        messages.append("检测到近期代码或发布变更，不允许全自动执行。")
    if not safe_marked:
        blockers.append("recipe_not_marked_safe")
        messages.append("命中的自愈规则未显式标记为可全自动执行。")
    if manual_only:
        blockers.append("manual_recovery_only")
        messages.append("该故障或恢复规则被标记为仅允许人工恢复。")
    if suggestions and not rollback_ready:
        blockers.append("rollback_not_available")
        messages.append("至少一个中高风险恢复动作没有可验证的回滚操作。")
    if max_action_risk > max_allowed_risk:
        blockers.append("action_risk_too_high")
        messages.append(f"动作风险超过配置中心允许的 {getattr(cfg.remediation, 'max_auto_risk_level', 'medium')}。")
    return {
        "ready": not blockers,
        "blockers": blockers,
        "blocker_messages": messages,
        "confidence": confidence,
        "confidence_threshold": confidence_threshold,
        "history_success_rate": history_rate if history else None,
        "history_success_rate_threshold": history_threshold,
        "history_success_lower_bound": round(history_lower_bound, 3) if counts_present else None,
        "history_confidence_level": history_confidence_level,
        "history_sample_count": history_samples,
        "history_success_count": history_successes if counts_present else None,
        "history_failure_count": history_failures if counts_present else None,
        "history_min_samples": history_min_samples,
        "history_scope_required": scope_required,
        "history_scope_complete": scope_complete,
        "history_scope_version": history.get("scope_version"),
        "history_action_fingerprint": history.get("action_fingerprint"),
        "history_environment_fingerprint": history.get("environment_fingerprint"),
        'history_verification_fingerprint': history.get('verification_fingerprint'),
        'history_verification_contract_complete': verification_complete,
        'history_verification_missing_count': history.get('verification_missing_count', 0),
        'history_verification_mismatch_count': history.get('verification_mismatch_count', 0),
        "history_legacy_unscoped_count": _to_int(history.get("legacy_unscoped_count"), 0),
        "safe_rule": safe_marked,
        "manual_only": manual_only,
        "automation_class": automation_class or ("manual_only" if manual_only else "unspecified"),
        "rollback_ready": rollback_ready,
        "recent_code_change": recent_code_change,
        "max_action_risk": _max_risk_label(suggestions),
        "max_auto_risk_level": getattr(cfg.remediation, "max_auto_risk_level", "medium"),
        "requirements": [
            "诊断置信度 >= 90%",
            "同动作、同环境、同恢复验证标准的历史成功率统计置信下界达到配置阈值",
            "未检测到近期代码或发布变更",
            "自愈规则显式标记 safe_auto_execute/safe_rerun",
            "故障未标记 manual_only，且中高风险动作具备回滚操作",
            "动作风险不超过配置中心自动执行上限",
        ],
    }


def _recovery_scope_for_plan(
    *,
    body: Dict[str, Any],
    target: Dict[str, Any],
    commands: List[str],
    namespace: str,
    cfg: Any = None,
) -> Dict[str, Any]:
    scope = build_recovery_scope(
        commands=commands,
        cluster_id=body.get("cluster_id"),
        namespace=namespace,
        target=target,
        environment=body.get("environment") or body.get("stage"),
        workload_version=(
            body.get("workload_version")
            or body.get("application_version")
            or body.get("release")
            or body.get("image")
        ),
    )
    config = cfg if cfg is not None else _get_config()
    policy = verification_contract(profiles=getattr(config.remediation, 'business_recovery_probes', []),
        cluster_id=body.get('cluster_id'), namespace=namespace, service=str(target.get('service') or ''))
    return {**scope, **policy, 'scope_version': 'recovery_scope_v2',
            'scope_complete': bool(scope['scope_complete'] and policy['verification_contract_complete'])}


def _scoped_recovery_history_stats(
    *,
    body: Dict[str, Any],
    diagnosis: Dict[str, Any],
    fault_type: str,
    target: Dict[str, Any],
    commands: List[str],
    namespace: str,
    cfg: Any,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    scope = _recovery_scope_for_plan(
        body=body,
        target=target,
        commands=commands,
        namespace=namespace,
        cfg=cfg,
    )
    min_samples = _to_int(getattr(cfg.remediation, "auto_history_min_samples", 14), 14)
    try:
        if not scope['scope_complete']:
            raise ValueError('Recovery verification contract or action scope unavailable')
        stats = _get_fault_store().resolution_stats(
            fault_type=str(diagnosis.get("fault_type") or fault_type or ""),
            error_pattern=str(diagnosis.get("error_pattern") or ""),
            action_fingerprint=str(scope.get("action_fingerprint") or ""),
            environment_fingerprint=str(scope.get("environment_fingerprint") or ""),
            verification_fingerprint=scope['verification_fingerprint'],
            require_verification_contract=True,
            min_samples=min_samples,
        )
        if (stats.get('verification_required') is not True or stats.get('verification_scope_complete') is not True
                or stats.get('verification_fingerprint') != scope['verification_fingerprint']
                or stats.get('action_fingerprint') != scope['action_fingerprint']
                or stats.get('environment_fingerprint') != scope['environment_fingerprint']
                or stats.get('scope_required') is not True or stats.get('scope_complete') is not True):
            raise ValueError('History backend cannot provide verification-scoped statistics')
    except Exception:
        stats = {
            "history_kind": "recovery_outcome",
            "scope_required": True,
            "scope_complete": bool(scope.get("scope_complete")),
            "scope_version": scope.get("scope_version"),
            "action_fingerprint": scope.get("action_fingerprint"),
            "environment_fingerprint": scope.get("environment_fingerprint"),
            'verification_required': True,
            'verification_scope_complete': False,
            'verification_fingerprint': scope.get('verification_fingerprint'),
            'sample_count': 0, 'success_count': 0, 'failure_count': 0,
            'sufficient_samples': False, 'verification_contract_unavailable': True,
        }
    return stats, scope


def _score_heal_action(action: Dict[str, Any], context_text: str) -> Dict[str, Any]:
    text = str(context_text or "").lower()
    step = str(action.get("step") or "").lower()
    command = str(action.get("command") or "").lower()
    joined = f"{step} {command}"
    score = 0
    reasons: List[str] = []
    if action.get("source") == "kb":
        score += 20
        reasons.append("命中自愈知识库策略")
    elif action.get("source") == "diagnosis_dynamic":
        score += 16
        reasons.append("根据诊断上下文动态生成")
    else:
        score += 10
        reasons.append("命中内置安全规则")
    if any(x in text for x in ("scaled to zero", "scale to zero", "replicas=0", "0 replicas", "no ready endpoints", "no endpoints")) and "scale deployment" in joined and "--replicas=1" in joined:
        score += 40
        reasons.append("RCA 指向副本为 0 或无可用端点，优先恢复副本")
    if "no endpoints" in text and "endpoints" in joined:
        score += 12
        reasons.append("检查诊断中提到的 Service Endpoints")
    if "deployment" in text and "deployment" in joined:
        score += 8
        reasons.append("命中诊断定位的 Deployment")
    if any(x in text for x in ("imagepullbackoff", "errimagepull", "image pull", "bad image", "non-existing image")) and "rollout undo" in command:
        score += 35
        reasons.append("RCA 指向镜像拉取失败，回滚是直接恢复动作")
    if "rollback" in step or "rollout undo" in command:
        score -= 3
        reasons.append("回滚动作需排在直接恢复检查之后")
    ranked = dict(action)
    ranked["selection_score"] = score
    ranked["selection_reason"] = "; ".join(reasons)
    return ranked


def _append_heal_action(
    suggestions: List[Dict[str, Any]],
    *,
    step: str,
    command: str,
    risk: str = "medium",
    rollback_command: str = "",
    source: str = "deterministic",
    impact: str = "",
    selection_reason: str = "",
) -> None:
    if not command:
        return
    suggestions.append({
        "step": step,
        "command": command,
        "rollback_command": rollback_command,
        "risk": risk,
        "impact": impact or _heal_risk_impact(risk, step),
        "source": source,
        "selection_reason": selection_reason or f"根据 {source} 策略匹配动作: {step}",
    })


def _build_dynamic_heal_actions(
    fault_type: str,
    *,
    namespace: str,
    pod: str = "",
    deployment: str = "",
    node: str = "",
    service: str = "",
    network_policy: str = "",
    network_chaos: str = "",
    diagnosis_text: str = "",
) -> List[Dict[str, Any]]:
    suggestions: List[Dict[str, Any]] = []
    deploy = deployment or _guess_deployment_from_pod(pod)
    svc = service or deploy
    diag_lower = str(diagnosis_text or "").lower()

    if pod:
        _append_heal_action(
            suggestions,
            step=f"查看 Pod {pod} 的事件和状态",
            command=f"kubectl describe pod {pod} -n {namespace}",
            risk="low",
        )

    if fault_type in {"PodCrashLoop", "KubeContainerWaiting"}:
        if pod:
            _append_heal_action(suggestions, step=f"采集 {pod} 上一次容器日志", command=f"kubectl logs {pod} -n {namespace} --tail=200 --previous", risk="low")
            _append_heal_action(suggestions, step=f"删除异常 Pod {pod} 触发控制器重建", command=f"kubectl delete pod {pod} -n {namespace} --grace-period=30", risk="medium")
        if deploy:
            _append_heal_action(suggestions, step=f"滚动重启 Deployment {deploy}", command=f"kubectl rollout restart deployment/{deploy} -n {namespace}", rollback_command=f"kubectl rollout undo deployment/{deploy} -n {namespace}", risk="medium")
            _append_heal_action(suggestions, step=f"回滚 Deployment {deploy} 到上一版本", command=f"kubectl rollout undo deployment/{deploy} -n {namespace}", risk="medium")

    elif fault_type == "OOMKilled":
        if pod:
            _append_heal_action(suggestions, step=f"确认 {pod} OOMKilled 证据", command=f"kubectl describe pod {pod} -n {namespace}", risk="low")
            _append_heal_action(suggestions, step=f"删除 OOM Pod {pod} 触发重建", command=f"kubectl delete pod {pod} -n {namespace} --grace-period=30", risk="medium")
        if deploy:
            _append_heal_action(suggestions, step=f"提高 Deployment {deploy} 内存限制", command=f"kubectl set resources deployment/{deploy} -n {namespace} --requests=memory=512Mi --limits=1Gi", rollback_command=f"kubectl rollout undo deployment/{deploy} -n {namespace}", risk="medium")
            _append_heal_action(suggestions, step=f"滚动重启 Deployment {deploy} 使资源配置生效", command=f"kubectl rollout restart deployment/{deploy} -n {namespace}", rollback_command=f"kubectl rollout undo deployment/{deploy} -n {namespace}", risk="medium")

    elif fault_type in {"HighCPUUsage", "HighMemoryUsage", "MetricAnomaly"}:
        sort_key = "memory" if fault_type == "HighMemoryUsage" else "cpu"
        _append_heal_action(suggestions, step=f"定位 {namespace} 中资源热点 Pod", command=f"kubectl top pod -n {namespace} --sort-by={sort_key}", risk="low")
        if pod:
            _append_heal_action(suggestions, step=f"查看 {pod} 资源使用", command=f"kubectl top pod {pod} -n {namespace}", risk="low")
            _append_heal_action(suggestions, step=f"重建资源异常 Pod {pod}", command=f"kubectl delete pod {pod} -n {namespace} --grace-period=30", risk="medium")
        if deploy:
            if fault_type == "HighCPUUsage":
                _append_heal_action(suggestions, step=f"提高 Deployment {deploy} CPU limit", command=f"kubectl set resources deployment/{deploy} -n {namespace} --requests=cpu=500m --limits=1500m", rollback_command=f"kubectl rollout undo deployment/{deploy} -n {namespace}", risk="medium")
            if fault_type == "HighMemoryUsage":
                _append_heal_action(suggestions, step=f"提高 Deployment {deploy} memory limit", command=f"kubectl set resources deployment/{deploy} -n {namespace} --requests=memory=512Mi --limits=2Gi", rollback_command=f"kubectl rollout undo deployment/{deploy} -n {namespace}", risk="medium")
            _append_heal_action(suggestions, step=f"滚动重启 Deployment {deploy}", command=f"kubectl rollout restart deployment/{deploy} -n {namespace}", rollback_command=f"kubectl rollout undo deployment/{deploy} -n {namespace}", risk="medium")
            _append_heal_action(suggestions, step=f"临时扩容 Deployment {deploy}", command=f"kubectl scale deployment/{deploy} -n {namespace} --replicas=2", risk="medium")

    elif fault_type in {"DeploymentUnhealthy", "KubePodNotReady", "ServiceUnavailable", "HighLatency"}:
        if svc:
            _append_heal_action(suggestions, step=f"检查 Service/Endpoints {svc}", command=f"kubectl get endpoints {svc} -n {namespace} -o wide", risk="low")
        if deploy:
            _append_heal_action(suggestions, step=f"检查 Deployment {deploy} 发布状态", command=f"kubectl rollout status deployment/{deploy} -n {namespace}", risk="low")
            if any(x in diag_lower for x in ("scaled to zero", "scale to zero", "replicas=0", "0 replicas", "no ready endpoints", "no endpoints")):
                _append_heal_action(suggestions, step=f"恢复 Deployment {deploy} 至 1 个副本", command=f"kubectl scale deployment/{deploy} -n {namespace} --replicas=1", rollback_command=f"kubectl scale deployment/{deploy} -n {namespace} --replicas=0", risk="medium")
            _append_heal_action(suggestions, step=f"滚动重启 Deployment {deploy}", command=f"kubectl rollout restart deployment/{deploy} -n {namespace}", rollback_command=f"kubectl rollout undo deployment/{deploy} -n {namespace}", risk="medium")
            _append_heal_action(suggestions, step=f"回滚 Deployment {deploy}", command=f"kubectl rollout undo deployment/{deploy} -n {namespace}", risk="medium")
        elif pod:
            _append_heal_action(suggestions, step=f"删除异常 Pod {pod} 触发重建", command=f"kubectl delete pod {pod} -n {namespace} --grace-period=30", risk="medium")
        else:
            _append_heal_action(suggestions, step="检查命名空间服务端点和最近事件", command=f"kubectl get endpoints -n {namespace} -o wide", risk="low")
            _append_heal_action(suggestions, step="检查命名空间 Deployment 状态", command=f"kubectl get deploy -n {namespace} -o wide", risk="low")

    elif fault_type == "ImagePullError":
        if pod:
            _append_heal_action(suggestions, step=f"查看 {pod} 镜像拉取事件", command=f"kubectl describe pod {pod} -n {namespace}", risk="low")
            _append_heal_action(suggestions, step=f"删除镜像拉取失败 Pod {pod} 重新拉取", command=f"kubectl delete pod {pod} -n {namespace} --ignore-not-found=true", risk="medium")
        if deploy:
            _append_heal_action(suggestions, step=f"回滚 Deployment {deploy} 到上一镜像版本", command=f"kubectl rollout undo deployment/{deploy} -n {namespace}", risk="medium")

    elif fault_type in {"SchedulingFailed", "PendingScheduling", "ResourceQuotaExceeded", "ResourceExhausted"}:
        if pod:
            _append_heal_action(suggestions, step=f"查看 {pod} 调度失败原因", command=f"kubectl describe pod {pod} -n {namespace}", risk="low")
        _append_heal_action(suggestions, step="查看节点资源水位", command="kubectl top node", risk="low")
        _append_heal_action(suggestions, step=f"查看 {namespace} 配额状态", command=f"kubectl describe resourcequota -n {namespace}", risk="low")
        if deploy:
            _append_heal_action(suggestions, step=f"临时扩容 Deployment {deploy} 保持可用副本", command=f"kubectl scale deployment/{deploy} -n {namespace} --replicas=2", risk="medium")

    elif fault_type in {"ConfigMissing", "ConfigKeyMissing", "VolumeMountFailed"}:
        if pod:
            _append_heal_action(suggestions, step=f"定位 {pod} 配置/挂载失败原因", command=f"kubectl describe pod {pod} -n {namespace}", risk="low")
            _append_heal_action(suggestions, step=f"删除挂载异常 Pod {pod} 触发重新挂载", command=f"kubectl delete pod {pod} -n {namespace} --grace-period=30", risk="medium")
        if deploy:
            _append_heal_action(suggestions, step=f"滚动重启 Deployment {deploy} 重新加载配置", command=f"kubectl rollout restart deployment/{deploy} -n {namespace}", rollback_command=f"kubectl rollout undo deployment/{deploy} -n {namespace}", risk="medium")

    elif fault_type in {"NodeNotReady", "KubeletUnhealthy", "DiskPressure", "NodeUnschedulable"}:
        if node:
            _append_heal_action(suggestions, step=f"查看节点 {node} Conditions 与事件", command=f"kubectl describe node {node}", risk="low")
            if fault_type != "NodeUnschedulable":
                _append_heal_action(suggestions, step=f"隔离节点 {node}", command=f"kubectl cordon {node}", rollback_command=f"kubectl uncordon {node}", risk="medium")
                _append_heal_action(suggestions, step=f"驱逐节点 {node} 上工作负载", command=f"kubectl drain {node} --ignore-daemonsets --delete-emptydir-data --force --timeout=120s", rollback_command=f"kubectl uncordon {node}", risk="high")
            _append_heal_action(suggestions, step=f"恢复节点 {node} 调度", command=f"kubectl uncordon {node}", risk="medium")
        else:
            _append_heal_action(suggestions, step="查看所有节点状态", command="kubectl get nodes -o wide", risk="low")

    elif fault_type == "DNSResolution":
        _append_heal_action(suggestions, step="检查 kube-dns/CoreDNS Endpoints", command="kubectl get endpoints kube-dns -n kube-system -o wide", risk="low")
        _append_heal_action(suggestions, step="滚动重启 CoreDNS", command="kubectl rollout restart deployment/coredns -n kube-system", risk="medium")
        _append_heal_action(suggestions, step="查看 CoreDNS Pod 状态", command="kubectl get pods -n kube-system -l k8s-app=kube-dns -o wide", risk="low")

    elif fault_type == "KubeProxyUnhealthy":
        _append_heal_action(suggestions, step="查看 kube-proxy Pod 状态", command="kubectl get pods -n kube-system -l k8s-app=kube-proxy -o wide", risk="low")
        _append_heal_action(suggestions, step="重启 kube-proxy DaemonSet", command="kubectl rollout restart ds/kube-proxy -n kube-system", risk="medium")

    elif fault_type == "ApiServerUnhealthy":
        _append_heal_action(suggestions, step="查看 API Server 静态 Pod 状态", command="kubectl get pods -n kube-system -l component=kube-apiserver -o wide", risk="low")
        _append_heal_action(suggestions, step="删除异常 API Server Pod 触发 kubelet 重建", command="kubectl delete pod -n kube-system -l component=kube-apiserver", risk="high")

    elif fault_type == "EtcdUnhealthy":
        _append_heal_action(suggestions, step="查看 etcd 静态 Pod 状态", command="kubectl get pods -n kube-system -l component=etcd -o wide", risk="low")
        _append_heal_action(suggestions, step="重建异常 etcd Pod", command="kubectl delete pod -n kube-system -l component=etcd", risk="high")

    elif fault_type == "NetworkPolicyDeny":
        _append_heal_action(suggestions, step=f"查看 {namespace} NetworkPolicy", command=f"kubectl get networkpolicy -n {namespace}", risk="low")
        _append_heal_action(suggestions, step=f"查看 {namespace} NetworkPolicy 详情", command=f"kubectl describe networkpolicy -n {namespace}", risk="low")
        _append_heal_action(suggestions, step=f"查看 {namespace} NetworkChaos", command=f"kubectl get networkchaos -n {namespace}", risk="low")
        if network_policy and "agenticsre-demo" in diag_lower:
            _append_heal_action(
                suggestions,
                step=f"删除 AgenticSRE demo 网络策略 {network_policy}",
                command=f"kubectl delete networkpolicy {network_policy} -n {namespace}",
                rollback_command="",
                risk="high",
                selection_reason="RCA 识别出临时 AgenticSRE demo NetworkPolicy 是阻断配置",
            )
        if network_chaos and "agenticsre-demo" in diag_lower:
            _append_heal_action(
                suggestions,
                step=f"删除 AgenticSRE demo 网络 Chaos {network_chaos}",
                command=f"kubectl delete networkchaos {network_chaos} -n {namespace}",
                rollback_command="",
                risk="high",
                selection_reason="RCA 识别出临时 AgenticSRE demo ChaosMesh NetworkChaos 是阻断配置",
            )

    elif fault_type == "HPAScalingFailed":
        _append_heal_action(suggestions, step=f"查看 {namespace} HPA 状态", command=f"kubectl get hpa -n {namespace} -o wide", risk="low")
        if deploy:
            _append_heal_action(suggestions, step=f"检查 HPA {deploy}", command=f"kubectl describe hpa {deploy} -n {namespace}", risk="low")
            _append_heal_action(suggestions, step=f"临时固定 Deployment {deploy} 副本", command=f"kubectl scale deployment/{deploy} -n {namespace} --replicas=1", risk="medium")

    elif fault_type in {"TargetDown", "LogErrorSpike"}:
        _append_heal_action(suggestions, step=f"检查 {namespace} Endpoints", command=f"kubectl get endpoints -n {namespace} -o wide", risk="low")
        if pod:
            _append_heal_action(suggestions, step=f"查看 {pod} 错误日志", command=f"kubectl logs {pod} -n {namespace} --tail=500", risk="low")
        if deploy:
            _append_heal_action(suggestions, step=f"滚动重启 Deployment {deploy}", command=f"kubectl rollout restart deployment/{deploy} -n {namespace}", rollback_command=f"kubectl rollout undo deployment/{deploy} -n {namespace}", risk="medium")

    return suggestions


def _build_heal_suggestions(body: Dict[str, Any]) -> Dict[str, Any]:
    alert = body.get("alert") if isinstance(body.get("alert"), dict) else {}
    diagnosis = body.get("diagnosis") if isinstance(body.get("diagnosis"), dict) else {}
    cfg = _get_config()
    raw = alert.get("raw_data") if isinstance(alert.get("raw_data"), dict) else {}
    involved = raw.get("involvedObject") if isinstance(raw.get("involvedObject"), dict) else {}
    labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
    namespace = body.get("namespace") or alert.get("namespace") or raw.get("metadata", {}).get("namespace") or cfg.kubernetes.namespace or "default"
    recipes = _load_heal_recipes()
    known_fault_types = {str(r.get("fault_type")) for r in recipes if r.get("fault_type")}
    explicit_fault_type = str(body.get("fault_type") or diagnosis.get("fault_type") or "").strip()
    diagnosis_evidence = diagnosis.get("evidence_summary") if isinstance(diagnosis.get("evidence_summary"), dict) else {}
    diagnosis_services = diagnosis.get("affected_services") if isinstance(diagnosis.get("affected_services"), list) else []
    text = " ".join(filter(None, [
        str(body.get("fault_type") or diagnosis.get("fault_type") or ""),
        str(body.get("root_cause") or diagnosis.get("root_cause") or ""),
        str(body.get("root_cause_component") or diagnosis.get("root_cause_component") or ""),
        str(body.get("remediation_hint") or diagnosis.get("remediation_suggestion") or ""),
        str(body.get("message", "")),
        str(body.get("object", "")),
        " ".join(map(str, diagnosis_services)),
        " ".join(map(str, diagnosis_evidence.values())),
        " ".join(map(str, body.get("evidence", []) or [])),
        str(alert.get("source", "")),
        str(alert.get("severity", "")),
        str(alert.get("title", "")),
        str(alert.get("description", "")),
        str(alert.get("service", "")),
        str(raw.get("reason", "")),
        str(raw.get("message", "")),
        str(involved.get("kind", "")),
        str(involved.get("name", "")),
        " ".join(str(labels.get(k, "")) for k in ("alertname", "reason", "pod", "deployment", "node", "instance")),
    ]))
    # An explicit diagnosis category takes precedence over incidental resource
    # words in supporting evidence (for example a healthy CPU observation).
    normalized_type = re.sub(r"[^a-z0-9]", "", explicit_fault_type.lower())
    canonical_type = next((name for name in known_fault_types
                           if re.sub(r"[^a-z0-9]", "", name.lower()) == normalized_type), "") if normalized_type else ""
    if normalized_type == "servicedisruption":
        canonical_type = "ServiceUnavailable"
    fault_type = canonical_type or _normalize_heal_fault_type(text)
    alert_object = _extract_alert_object(alert)
    primary_diagnosis_service = next((str(s) for s in diagnosis_services if s), "")
    involved_kind = str(involved.get("kind") or "").lower()
    alert_pod = alert_object.get("pod") if involved_kind == "pod" or str(alert.get("source") or "") == "pod_health" else ""
    pod = _extract_k8s_name(body.get("pod") or alert_pod or "")
    component = _extract_k8s_name(diagnosis.get("root_cause_component") or body.get("root_cause_component") or "")
    deployment = _extract_k8s_name(body.get("deployment") or alert_object.get("deployment") or component or primary_diagnosis_service or "") or _guess_deployment_from_pod(pod)
    node = _extract_k8s_name(body.get("node") or alert_object.get("node") or body.get("instance") or diagnosis.get("node") or "")
    service = _extract_k8s_name(body.get("service") or body.get("svc") or alert.get("service") or body.get("object") or primary_diagnosis_service or component or "")
    network_policy = _extract_k8s_name(body.get("network_policy") or diagnosis.get("network_policy") or body.get("networkpolicy") or diagnosis_evidence.get("network_policy") or "")
    network_chaos = _extract_k8s_name(body.get("network_chaos") or diagnosis.get("network_chaos") or body.get("networkchaos") or diagnosis_evidence.get("network_chaos") or "")
    recipe = next((r for r in recipes if r.get("fault_type") == fault_type), None)
    suggestions: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []
    seen_commands: set[str] = set()
    candidate_actions: List[Dict[str, Any]] = []
    plan_sources: set[str] = set()

    for action in _build_dynamic_heal_actions(
        fault_type,
        namespace=namespace,
        pod=pod,
        deployment=deployment,
        node=node,
        service=service,
        network_policy=network_policy,
        network_chaos=network_chaos,
        diagnosis_text=text,
    ):
        action["source"] = action.get("source") or "diagnosis_dynamic"
        action["selection_reason"] = action.get("selection_reason") or "根据 RCA 目标对象和故障类型生成"
        candidate_actions.append(action)

    if recipe:
        for action in recipe.get("actions", []):
            cmd = _materialize_heal_command(
                action.get("command", ""),
                namespace=namespace,
                pod=pod,
                deployment=deployment,
                node=node,
                service=service,
                network_policy=network_policy,
                network_chaos=network_chaos,
            )
            rollback = _materialize_heal_command(
                action.get("rollback_command", ""),
                namespace=namespace,
                pod=pod,
                deployment=deployment,
                node=node,
                service=service,
                network_policy=network_policy,
                network_chaos=network_chaos,
            )
            item = {
                "step": action.get("step"),
                "command": cmd,
                "rollback_command": rollback,
                "risk": action.get("risk") or recipe.get("risk", "medium"),
                "source": "kb",
                "selection_reason": f"来自自愈知识库策略 {fault_type}",
            }
            candidate_actions.append(item)

    for action in sorted(
        (_score_heal_action(item, text) for item in candidate_actions),
        key=lambda x: x.get("selection_score", 0),
        reverse=True,
    ):
        cmd_key = action.get("command", "")
        if cmd_key and cmd_key not in seen_commands:
            seen_commands.add(cmd_key)
            plan_sources.add(str(action.get("source") or "unknown"))
            suggestions.append(action)

    executable: List[Dict[str, Any]] = []
    for item in suggestions:
        reason = _validate_heal_command(item.get("command", ""))
        if reason:
            item["executable"] = False
            item["blocked_reason"] = reason
            blocked.append(item)
        else:
            item["executable"] = True
            executable.append(item)
    suggestions = executable

    recipe_public = None
    if recipe:
        recipe_public = {
            "fault_type": recipe.get("fault_type"),
            "category": recipe.get("category"),
            "risk": recipe.get("risk"),
            "requires_approval": recipe.get("requires_approval"),
            "min_confidence": recipe.get("min_confidence", recipe.get("confidence_threshold")),
            "safe_auto_execute": bool(recipe.get("safe_auto_execute") or recipe.get("safe_rerun") or recipe.get("allow_auto_execute")),
            "manual_only": bool(recipe.get("manual_only") or recipe.get("auto_recovery_supported") is False),
            "automation_class": recipe.get("automation_class", ""),
            "description": recipe.get("description"),
            "signals": recipe.get("signals", []),
        }
    diagnosis_gate = _build_diagnosis_gate(diagnosis, cfg, recipe)
    capability = _heal_capability(recipe=recipe, suggestions=suggestions, blocked=blocked)
    capability["diagnosis_ready"] = bool(diagnosis_gate.get("ready"))
    capability["diagnosis_required_for_real_execution"] = True
    recovery_target = {
        "pod": pod,
        "deployment": deployment,
        "node": node,
        "service": service,
        "network_policy": network_policy,
        "network_chaos": network_chaos,
    }
    recovery_history_stats, recovery_scope = _scoped_recovery_history_stats(
        body=body,
        diagnosis=diagnosis,
        fault_type=fault_type,
        target=recovery_target,
        commands=[str(item.get("command") or "") for item in suggestions],
        namespace=namespace,
        cfg=cfg,
    )
    capability["auto_execution_gate"] = _build_auto_execution_gate(
        recipe=recipe,
        suggestions=suggestions,
        diagnosis=diagnosis,
        cfg=cfg,
        recovery_history_stats=recovery_history_stats,
    )
    capability["recovery_history_scope"] = recovery_scope
    capability["auto_execution_eligible"] = bool(capability["auto_execution_gate"].get("ready"))

    return {
        "fault_type": fault_type,
        "namespace": namespace,
        "target": recovery_target,
        "recipe": recipe_public,
        "suggestions": suggestions,
        "blocked_templates": blocked,
        "capability": capability,
        "diagnosis_gate": diagnosis_gate,
        "verification_plan": _build_heal_verification_plan(
            namespace,
            {"pod": pod, "deployment": deployment, "node": node, "service": service, "network_policy": network_policy, "network_chaos": network_chaos},
            [s.get("command", "") for s in suggestions],
        ),
        "recommended_strategy": "kb" if suggestions else "none",
        "plan_source": "+".join(sorted(plan_sources)) if plan_sources else "none",
        "strategy_trace": {
            "recipe_matched": bool(recipe),
            "candidate_count": len(candidate_actions),
            "selected_count": len(suggestions),
            "sources": sorted(plan_sources),
            "ranking": [
                {
                    "source": item.get("source"),
                    "step": item.get("step"),
                    "command": item.get("command"),
                    "score": item.get("selection_score"),
                    "reason": item.get("selection_reason"),
                }
                for item in suggestions[:12]
            ],
        },
        "diagnosis_mode": "rca_result" if diagnosis else "alert_only",
        "diagnosis_used": {
            "fault_type": diagnosis.get("fault_type"),
            "root_cause": diagnosis.get("root_cause"),
            "root_cause_component": diagnosis.get("root_cause_component"),
            "remediation_suggestion": diagnosis.get("remediation_suggestion"),
            "affected_services": diagnosis_services,
            "confidence": diagnosis.get("confidence"),
            "evidence_count": diagnosis_gate.get("evidence_count", 0),
        } if diagnosis else {},
        "diagnosis_summary": f"故障类型: {fault_type} | 根因: {diagnosis.get('root_cause') or body.get('root_cause') or '-'} | namespace: {namespace} | pod: {pod or '-'} | deployment: {deployment or '-'} | node: {node or '-'} | network_policy: {network_policy or '-'} | network_chaos: {network_chaos or '-'}",
    }


def _require_fault_execution_approval(
    *,
    body: Dict[str, Any],
    target_mode: str,
    action: str,
    fault_type: str,
    severity: str = "",
) -> None:
    """Enforce explicit confirmations before any real fault action."""
    if bool(body.get("dry_run", True)):
        return
    if not bool(body.get("confirm")):
        raise HTTPException(
            400,
            "Real fault execution requires confirm=true. Re-run as dry_run first and review the command.",
        )
    if target_mode == "host" and not bool(body.get("host_ack")):
        raise HTTPException(
            400,
            "Bare-metal host execution requires host_ack=true because it modifies the target GPU host.",
        )
    if _llm_fault_requires_risk_ack(fault_type, severity) and not bool(body.get("risk_ack")):
        raise HTTPException(
            400,
            f"Fault type '{fault_type}' is high risk and requires risk_ack=true.",
        )
    if action == "experiment" and not bool(body.get("experiment_ack")):
        raise HTTPException(
            400,
            "Real experiment execution requires experiment_ack=true because it injects and recovers faults.",
        )


def _load_fault_targets():
    """Load persisted fault-target overrides on startup (replaces yaml defaults)."""
    if not _FAULT_TARGETS_FILE.exists():
        return
    try:
        _ensure_private_config_permissions(_FAULT_TARGETS_FILE)
        data = json.loads(_FAULT_TARGETS_FILE.read_text(encoding="utf-8"))
        cfg = _get_config()
        if isinstance(data.get("k8s_clusters"), list):
            cfg.fault_targets.k8s_clusters = list(data["k8s_clusters"])
        if isinstance(data.get("llm_hosts"), list):
            cfg.fault_targets.llm_hosts = list(data["llm_hosts"])
        logger.info("Loaded fault targets from disk")
    except Exception as e:
        logger.warning(f"Failed to load fault targets: {e}")


def _find_k8s_cluster(cluster_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Look up a K8s cluster profile by id. Returns None if not found."""
    if not cluster_id:
        return None
    cfg = _get_config()
    for c in cfg.fault_targets.k8s_clusters:
        if isinstance(c, dict) and c.get("id") == cluster_id:
            return c
    return None


def _find_llm_host(host_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Look up an LLM GPU host profile by id. Returns None if not found."""
    if not host_id:
        return None
    cfg = _get_config()
    for h in cfg.fault_targets.llm_hosts:
        if isinstance(h, dict) and h.get("id") == host_id:
            return h
    return None


def _llm_host_address_configured(host: Optional[Dict[str, Any]]) -> bool:
    return bool(str((host or {}).get("host") or "").strip())


def _require_llm_host_address(host: Dict[str, Any], host_id: str = "") -> Dict[str, Any]:
    if not _llm_host_address_configured(host):
        label = host_id or str(host.get("id") or "")
        raise HTTPException(400, f"llm host '{label}' is not configured; set its host address first")
    return host


def _cluster_kind(cluster: Optional[Dict[str, Any]]) -> str:
    if not isinstance(cluster, dict):
        return "general"
    raw = str(cluster.get("kind") or cluster.get("type") or cluster.get("cluster_kind") or "").strip().lower()
    if raw in {"ai", "llm", "gpu", "inference", "智算"}:
        return "ai"
    return "general"


def _general_cluster_profiles() -> List[Dict[str, Any]]:
    return [
        cluster for cluster in _get_config().fault_targets.k8s_clusters
        if isinstance(cluster, dict) and _cluster_kind(cluster) == "general"
    ]


def _resolve_general_cluster_id(cluster_id: str = "") -> str:
    requested = str(cluster_id or "").strip()
    if requested:
        cluster = _find_k8s_cluster(requested)
        if not cluster:
            raise HTTPException(400, f"cluster profile not found: {requested}")
        if _cluster_kind(cluster) != "general":
            raise HTTPException(409, f"cluster {requested} is not a general-compute cluster")
        return requested
    profiles = _general_cluster_profiles()
    if len(profiles) == 1:
        return str(profiles[0].get("id") or "")
    if not profiles:
        return ""
    raise HTTPException(400, "multiple general-compute clusters are configured; cluster_id is required")


def _cluster_scoped_config(cluster_id: str = ""):
    """Clone runtime config and bind tools to one cluster profile."""
    cfg = copy.deepcopy(_get_config())
    cluster = _find_k8s_cluster(cluster_id) if cluster_id else None
    if not cluster:
        return cfg
    cfg.kubernetes.cluster_id = cluster_id
    if cluster.get("kubeconfig") is not None:
        cfg.kubernetes.kubeconfig = str(cluster.get("kubeconfig") or "")
    if cluster.get("context") is not None:
        setattr(cfg.kubernetes, "context", str(cluster.get("context") or ""))
    if cluster.get("default_namespace"):
        cfg.kubernetes.namespace = str(cluster.get("default_namespace") or "")
    if cluster.get("jaeger_url"):
        cfg.observability.jaeger_url = str(cluster.get("jaeger_url"))
    if cluster.get("prometheus_url"):
        cfg.observability.prometheus_url = str(cluster.get("prometheus_url") or "")
    return cfg


def _cluster_prometheus_url(cluster: Optional[Dict[str, Any]] = None, explicit: str = "") -> str:
    configured = (
        str(explicit or "").strip().rstrip("/")
        or str((cluster or {}).get("prometheus_url") or "").strip().rstrip("/")
    )
    if configured:
        return configured
    if cluster:
        return ""
    return _discover_prometheus_url()


def _reachable_cluster_prometheus_url(cluster: Optional[Dict[str, Any]] = None, explicit: str = "") -> str:
    """Keep scoped metrics on their configured backend, even when unavailable."""
    configured = _cluster_prometheus_url(cluster, explicit)
    if cluster or str(explicit or "").strip():
        # Reachability cannot establish cluster identity. A healthy global
        # endpoint or local tunnel may belong to an entirely different cluster.
        return configured
    if configured and _prometheus_api_ok(configured):
        return configured
    candidates = []
    for url in (
        str((cluster or {}).get("prometheus_url") or "").strip().rstrip("/"),
        str(explicit or "").strip().rstrip("/"),
        str(getattr(_get_config().observability, "prometheus_url", "") or "").strip().rstrip("/"),
        "http://127.0.0.1:19090",
        "http://127.0.0.1:30090",
    ):
        if url and url not in candidates:
            candidates.append(url)
    for url in candidates:
        if _prometheus_api_ok(url):
            logger.info("Cluster Prometheus endpoint selected: %s", url)
            return url
    discovered = _discover_prometheus_url()
    if discovered and _prometheus_api_ok(discovered):
        return discovered
    return configured


def _redact_cluster_runtime_fields(cluster: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(cluster or {})
    for key in ("kubeconfig",):
        if item.get(key):
            item[f"{key}_configured"] = True
            item[key] = _mask_secret(str(item[key]))
    item["kind"] = _cluster_kind(item)
    return item


def _cluster_kubectl_path(cluster: Optional[Dict[str, Any]]) -> str:
    raw = str((cluster or {}).get("kubectl_path") or "kubectl").strip()
    if not raw:
        return "kubectl"
    # Only allow a single executable path/name here. Arguments are added by the
    # command builder so a configured path cannot smuggle extra shell syntax.
    if not re.fullmatch(r"[A-Za-z0-9_./:-]+", raw) or raw.endswith("/"):
        return "kubectl"
    return raw


def _cluster_kubeconfig_path(cluster: Optional[Dict[str, Any]]) -> str:
    raw = str((cluster or {}).get("kubeconfig") or "").strip()
    if not raw:
        return ""
    if raw.startswith("/app/"):
        mapped = ROOT_DIR / raw.removeprefix("/app/")
        if mapped.exists():
            return str(mapped)
    return raw


def _is_valid_kubectl_path(value: Any) -> bool:
    raw = str(value or "").strip()
    return not raw or (
        bool(re.fullmatch(r"[A-Za-z0-9_./:-]+", raw))
        and not raw.endswith("/")
    )


def _get_config():
    if _state["config"] is None:
        _state["config"] = get_config()
    return _state["config"]


def _get_pipeline():
    if _state["pipeline"] is None:
        _state["pipeline"] = Pipeline(_get_config())
    return _state["pipeline"]


# ── Kubectl Helpers ──

def _kubectl_sync(cmd: str, namespace: str = "") -> str:
    """Execute kubectl command via SSH jump host (synchronous)."""
    import subprocess
    cfg = _get_config()
    ns_flag = f"-n {namespace}" if namespace else ""

    if cfg.kubernetes.use_ssh and cfg.kubernetes.ssh_jump_host:
        ssh_target = cfg.kubernetes.ssh_target or cfg.kubernetes.target_host
        ssh_cmd = f"ssh -J {cfg.kubernetes.ssh_jump_host} {ssh_target} 'kubectl {cmd} {ns_flag}'"
    else:
        ssh_cmd = f"kubectl {cmd} {ns_flag}"

    try:
        result = subprocess.run(
            ssh_cmd, shell=True, capture_output=True, text=True, timeout=30
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return "Error: command timed out"
    except Exception as e:
        return f"Error: {e}"


async def _kubectl(cmd: str, namespace: str = "") -> str:
    """Execute kubectl command without blocking the event loop."""
    return await asyncio.to_thread(_kubectl_sync, cmd, namespace)


async def _kubectl_json(cmd: str, namespace: str = "", cluster_id: Optional[str] = None) -> Any:
    """Execute kubectl -o json and parse without blocking."""
    ns_flag = f"-n {namespace}" if namespace else ""
    result = await asyncio.to_thread(_shell_sync, f"kubectl {cmd} {ns_flag} -o json", 30, cluster_id)
    raw = result.get("stdout") or result.get("stderr") or ""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"error": raw}


def _apply_cluster_to_cmd(cmd: str, cluster_id: Optional[str]) -> str:
    """Scope the whole shell command to a cluster, including pipelines."""
    if not cluster_id:
        return cmd
    cluster = _find_k8s_cluster(cluster_id)
    if not cluster:
        return cmd
    ctx = cluster.get("context") or ""
    kubeconfig = _cluster_kubeconfig_path(cluster)
    kubectl_path = _cluster_kubectl_path(cluster)
    new_cmd = cmd
    if kubectl_path != "kubectl":
        new_cmd = re.sub(r"\bkubectl\b", shlex.quote(kubectl_path), new_cmd)
    if ctx:
        new_cmd = re.sub(
            rf'(?<![\w/.-]){re.escape(shlex.quote(kubectl_path))}\b(?!\s+--context)',
            f'{shlex.quote(kubectl_path)} --context={shlex.quote(ctx)}',
            new_cmd,
        )
    if kubeconfig:
        # An assignment before a pipeline applies only to its first process.
        # Give the complete compound command a shell with the selected config.
        new_cmd = f"KUBECONFIG={shlex.quote(kubeconfig)} /bin/sh -c {shlex.quote(new_cmd)}"
    return new_cmd


def _k8s_name_fragment(value: Any, fallback: str = "target", max_len: int = 42) -> str:
    text = str(value or fallback).strip().lower()
    text = re.sub(r"[^a-z0-9.-]+", "-", text).strip(".-")
    text = re.sub(r"-{2,}", "-", text)
    return (text or fallback)[:max_len].strip(".-") or fallback


def _k8s_label_value(value: Any, fallback: str = "target") -> str:
    text = str(value or fallback).strip()
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("_.-")
    return (text or fallback)[:63].strip("_.-") or fallback


def _shell_sync(
    cmd: str,
    timeout: int = 90,
    cluster_id: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Execute a bounded shell command locally or through the configured jump host.

    When cluster_id is provided, kubectl invocations are rewritten to target that
    cluster profile (KUBECONFIG + --context flags).
    """
    cfg = _get_config()
    effective_cmd = _apply_cluster_to_cmd(cmd, cluster_id)
    shell_cmd = effective_cmd
    if cfg.kubernetes.use_ssh and cfg.kubernetes.ssh_jump_host:
        ssh_target = cfg.kubernetes.ssh_target or cfg.kubernetes.target_host
        escaped = effective_cmd.replace("'", "'\"'\"'")
        shell_cmd = f"ssh -J {cfg.kubernetes.ssh_jump_host} {ssh_target} '{escaped}'"

    try:
        result = subprocess.run(
            shell_cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, **(env or {})},
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        keep_full_stdout = "-o json" in effective_cmd or "--output=json" in effective_cmd or "get --raw" in effective_cmd
        return {
            "command": effective_cmd,
            "returncode": result.returncode,
            "stdout": stdout if keep_full_stdout else stdout[-4000:],
            "stderr": stderr[-4000:],
            "ok": result.returncode == 0,
            # Only an observed normal shell exit supplies terminal failure
            # evidence. Timeout/transport/signal conventions remain ambiguous;
            # never infer dispatch certainty from the legacy ok/returncode pair.
            "executed": True,
            "execution_failure_confirmed": 0 < result.returncode < 124,
            "outcome_unknown": result.returncode != 0 and not 0 < result.returncode < 124,
        }
    except subprocess.TimeoutExpired:
        return {"command": effective_cmd, "returncode": 124, "stdout": "", "stderr": "timeout", "ok": False,
                "outcome_unknown": True}
    except OSError as e:
        return {"command": effective_cmd, "returncode": 1, "stdout": "", "stderr": str(e), "ok": False,
                "executed": False}
    except Exception as e:
        return {"command": effective_cmd, "returncode": 1, "stdout": "", "stderr": str(e), "ok": False,
                "outcome_unknown": True}


def _agent_skill_kubectl_runner(command: str, namespace: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Run Agent+Skill investigation commands against the requested cluster."""
    payload = payload if isinstance(payload, dict) else {}
    cluster_id = str(payload.get("cluster_id") or payload.get("target_cluster_id") or "").strip() or None
    ns = str(namespace or payload.get("namespace") or "").strip()
    try:
        args = readonly_kubectl_args(str(command or ""), ns)
        cluster = _find_k8s_cluster(cluster_id) if cluster_id else None
        if cluster_id and not cluster:
            raise ValueError("Unknown Kubernetes cluster")
        argv = [_cluster_kubectl_path(cluster) if cluster else "kubectl"]
        if cluster:
            if _cluster_kubeconfig_path(cluster):
                argv.append(f"--kubeconfig={_cluster_kubeconfig_path(cluster)}")
            if cluster.get("context"):
                argv.append(f"--context={cluster['context']}")
        argv.extend(args)
        cfg = _get_config()
        if cfg.kubernetes.use_ssh and cfg.kubernetes.ssh_jump_host:
            argv = ["ssh", "-J", cfg.kubernetes.ssh_jump_host,
                    cfg.kubernetes.ssh_target or cfg.kubernetes.target_host, shlex.join(argv)]
        result = subprocess.run(argv, shell=False, capture_output=True, text=True,
                                timeout=15 if args[0] in {"logs", "top"} else 10)
        return {"ok": result.returncode == 0, "returncode": result.returncode,
                "stdout": result.stdout.strip(), "stderr": result.stderr.strip(),
                "command": shlex.join(argv)}
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "executed": False, "returncode": 126, "stdout": "", "stderr": str(exc)}


def _agent_skill_context_provider(request: SkillAgentRequest) -> Dict[str, Any]:
    workflow = request.skill_workflow if isinstance(request.skill_workflow, dict) else {}
    payload = workflow.get("payload") if isinstance(workflow.get("payload"), dict) else {}
    query = str(request.query or payload.get("message") or "")
    target_cluster_id = str(payload.get("cluster_id") or payload.get("target_cluster_id") or "").strip()
    target_namespace = str(payload.get("namespace") or request.namespace or "").strip()
    namespace_conflict = bool(payload.get('namespace') and request.namespace
        and str(payload['namespace']).strip() != str(request.namespace).strip())
    cluster_conflict = bool(payload.get('cluster_id') and payload.get('target_cluster_id')
        and str(payload['cluster_id']).strip() != str(payload['target_cluster_id']).strip())
    from memory.memory_scope import MemoryScope, admission_reason
    memory_scope = MemoryScope(cluster_id=target_cluster_id, namespace=target_namespace,
        workload_version=str(payload.get('workload_version') or ''),
        entity_uid=str(payload.get('entity_uid') or ''), conflicted=namespace_conflict or cluster_conflict)
    context: Dict[str, Any] = {
        "rules": [],
        "faults": [],
        "recent_runs": [],
        "history_stats": {},
        "context_scope": {
            "cluster_id": target_cluster_id,
            "namespace": target_namespace,
            "as_of": memory_scope.as_of,
            "cluster_kind": payload.get("cluster_kind") or "",
            "strict": True,
        },
    }
    if not _get_config().memory.enabled:
        from memory.history_retrieval import empty_history
        context['history_retrieval'] = empty_history('disabled')['retrieval']
        context["history_disabled"] = True
        return context

    try:
        from memory.history_retrieval import retrieve_history, empty_history
        historical = retrieve_history(_get_fault_store, query,
            n_rules=_get_config().memory.max_similar_results,
            n_faults=_get_config().memory.max_similar_results,
            memory_scope=memory_scope,
            timeout_s=getattr(_get_config().memory, 'retrieval_timeout_s', 5.0)).context if query else empty_history('not_requested')
        context['history_retrieval'] = historical['retrieval']
        rules = historical.get("rules") if isinstance(historical.get("rules"), list) else []
        faults = historical.get("faults") if isinstance(historical.get("faults"), list) else []
        context["rules"] = rules
        context["faults"] = faults
        context["history_stats"] = _get_feedback_store().diagnosis_stats(
            fault_type=str(payload.get("fault_type") or ""),
            error_pattern=str(payload.get("raw_fault_type") or payload.get("event_type") or ""),
            cluster_id=target_cluster_id,
            memory_scope=memory_scope,
        ) if memory_scope.complete else {}
    except Exception:
        context["fault_store_error"] = 'history_context_error'
        if 'history_retrieval' not in context:
            from memory.history_retrieval import empty_history
            context['history_retrieval'] = empty_history('unavailable')['retrieval']
    recent_runs: List[Dict[str, Any]] = []
    payload_fault = str(payload.get("fault_type") or "").lower()
    payload_domain = str(payload.get("domain") or workflow.get("domain") or "").lower()
    for run in sorted(_state.get("rca_runs", {}).values(), key=lambda item: item.get("started_at", 0), reverse=True):
        if len(recent_runs) >= 8:
            break
        if str(run.get("id") or "") == str(request.run_id or ""):
            continue
        result = run.get("result") if isinstance(run.get("result"), dict) else {}
        run_cluster_id = str(run.get("cluster_id") or result.get("cluster_id") or "").strip()
        run_namespace = str(run.get("namespace") or result.get("namespace") or "").strip()
        candidate = dict(cluster_id=run_cluster_id, namespace=run_namespace,
            timestamp=run.get('finished_at'), status=run.get('status'),
            entity_uid=result.get('entity_uid', ''), workload_version=result.get('workload_version', ''))
        if admission_reason(candidate, memory_scope, 'runs') != 'admitted':
            continue
        skill_result = result.get("skill_runtime_result") if isinstance(result.get("skill_runtime_result"), dict) else {}
        diagnosis = skill_result.get("diagnosis") if isinstance(skill_result.get("diagnosis"), dict) else {}
        run_fault = str(diagnosis.get("fault_type") or result.get("fault_type") or "").lower()
        run_domain = str(skill_result.get("domain") or "").lower()
        text = f"{run.get('query','')} {diagnosis.get('root_cause','')} {run_fault}".lower()
        if (
            (payload_fault and (payload_fault == run_fault or payload_fault in text))
            or (payload_domain and payload_domain == run_domain)
            or any(token and token in text for token in [str(payload.get("service") or "").lower(), str(payload.get("deployment") or "").lower()])
        ):
            recent_runs.append({
                "id": run.get("id"),
                "cluster_id": run_cluster_id,
                "status": run.get("status"),
                "query": str(run.get("query") or "")[:300],
                "fault_type": run_fault,
                "root_cause": diagnosis.get("root_cause") or result.get("root_cause") or "",
                "confidence": diagnosis.get("confidence") or result.get("confidence"),
            })
    context["recent_runs"] = recent_runs
    return context


def _kubectl_heal_check(
    args: str,
    *,
    cluster_id: Optional[str],
    timeout: int = 45,
    parse_json: bool = False,
) -> Dict[str, Any]:
    cmd = f"kubectl {args}"
    result = _shell_sync(cmd, timeout, cluster_id)
    check = {
        "command": result.get("command") or cmd,
        "ok": bool(result.get("ok")),
        "returncode": result.get("returncode"),
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
    }
    if parse_json and check["ok"]:
        try:
            check["json"] = json.loads(check["stdout"] or "{}")
        except Exception as e:
            check["ok"] = False
            check["parse_error"] = str(e)
    return check


def _deployment_available(obj: Dict[str, Any]) -> bool:
    spec = obj.get("spec") if isinstance(obj, dict) else {}
    status = obj.get("status") if isinstance(obj, dict) else {}
    desired = int(spec.get("replicas") or 1)
    available = int(status.get("availableReplicas") or 0)
    ready = int(status.get("readyReplicas") or 0)
    unavailable = int(status.get("unavailableReplicas") or 0)
    conditions = [c for c in status.get("conditions", []) or [] if isinstance(c, dict)]
    condition_ok = not conditions or any(c.get("type") == "Available" and c.get("status") == "True" for c in conditions)
    return available >= desired and ready >= desired and unavailable == 0 and condition_ok


def _pod_ready(obj: Dict[str, Any]) -> bool:
    if not isinstance(obj, dict) or (obj.get("metadata") or {}).get("deletionTimestamp"):
        return False
    status = obj.get("status") or {}
    phase = status.get("phase")
    if phase == "Succeeded":
        return True
    if phase != "Running":
        return False
    statuses = status.get("containerStatuses") or []
    if not isinstance(statuses, list) or not statuses:
        return False
    conditions = status.get("conditions") or []
    ready_conditions = [c for c in conditions if isinstance(c, dict) and c.get("type") == "Ready"]
    if ready_conditions and not all(c.get("status") == "True" for c in ready_conditions):
        return False
    return all(isinstance(c, dict) and c.get("ready") is True for c in statuses)


def _node_ready(obj: Dict[str, Any]) -> bool:
    conditions = ((obj.get("status") or {}).get("conditions") or []) if isinstance(obj, dict) else []
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions if isinstance(c, dict))


def _endpoints_ready(obj: Dict[str, Any]) -> bool:
    subsets = obj.get("subsets") if isinstance(obj, dict) else []
    return any((s.get("addresses") or []) for s in subsets or [] if isinstance(s, dict))


def _namespace_health_from_pods(stdout: str) -> Dict[str, Any]:
    rows = [line for line in (stdout or "").splitlines() if line.strip()]
    bad_rows = []
    for row in rows:
        fields = row.split()
        ready = re.fullmatch(r"(\d+)/(\d+)", fields[1]) if len(fields) >= 3 else None
        # Completed Jobs are terminal successes; serving Pods must be fully ready.
        healthy = bool(ready) and (fields[2] == "Completed" or (
            fields[2] == "Running" and int(ready[2]) > 0 and ready[1] == ready[2]))
        if not healthy:
            bad_rows.append(row)
    return {"total_pods_seen": len(rows), "unhealthy_rows": bad_rows[:20], "healthy": bool(rows) and not bad_rows}


def _verify_heal_recovery(
    *,
    namespace: str,
    suggestion: Dict[str, Any],
    commands: List[str],
    cluster_id: Optional[str],
    dry_run: bool,
    executed_success: bool,
) -> Dict[str, Any]:
    """Best-effort post-action recovery verification for self-healing runs."""
    if dry_run:
        return {"status": "skipped", "reason": "dry_run", "recovered": None, "checks": []}
    if not executed_success:
        return {"status": "skipped", "reason": "execution_failed", "recovered": False, "checks": []}
    if not _is_safe_k8s_name(namespace):
        return {"status": "unknown", "reason": "unsafe_namespace", "recovered": None, "checks": []}

    target = suggestion.get("target") if isinstance(suggestion.get("target"), dict) else {}
    deployment = _extract_k8s_name(target.get("deployment"))
    pod = _extract_k8s_name(target.get("pod"))
    service = _extract_k8s_name(target.get("service"))
    node = _extract_k8s_name(target.get("node"))
    network_policy = _extract_k8s_name(target.get("network_policy"))
    network_chaos = _extract_k8s_name(target.get("network_chaos"))
    commands_text = "\n".join(commands)
    checks: List[Dict[str, Any]] = []

    if network_chaos and any(" delete networkchaos " in f" {cmd} " for cmd in commands):
        chaos = _kubectl_heal_check(
            f"get networkchaos {shlex.quote(network_chaos)} -n {shlex.quote(namespace)} --ignore-not-found -o name",
            cluster_id=cluster_id,
        )
        checks.append({"type": "networkchaos_removed", **chaos})
        recovered = bool(chaos.get("ok")) and not str(chaos.get("stdout") or "").strip()
        return {"status": "unknown" if recovered else "failed", "reason": "business_probe_required" if recovered else "cleanup_failed", "target": {"network_chaos": network_chaos, "namespace": namespace}, "recovered": None if recovered else False, "action_completed": recovered, "checks": checks}

    if network_policy and any(" delete networkpolicy " in f" {cmd} " for cmd in commands):
        policy = _kubectl_heal_check(
            f"get networkpolicy {shlex.quote(network_policy)} -n {shlex.quote(namespace)} --ignore-not-found -o name",
            cluster_id=cluster_id,
        )
        checks.append({"type": "networkpolicy_removed", **policy})
        recovered = bool(policy.get("ok")) and not str(policy.get("stdout") or "").strip()
        return {"status": "unknown" if recovered else "failed", "reason": "business_probe_required" if recovered else "cleanup_failed", "target": {"network_policy": network_policy, "namespace": namespace}, "recovered": None if recovered else False, "action_completed": recovered, "checks": checks}

    if pod and any(" delete pod " in f" {cmd} " for cmd in commands):
        pod_after_delete = _kubectl_heal_check(
            f"get pod {shlex.quote(pod)} -n {shlex.quote(namespace)} --ignore-not-found -o name",
            cluster_id=cluster_id,
        )
        checks.append({"type": "pod_removed", **pod_after_delete})
        removed = bool(pod_after_delete.get("ok")) and not str(pod_after_delete.get("stdout") or "").strip()
        if not removed:
            return {"status": "failed", "recovered": False, "checks": checks}
        if not deployment:
            return {"status": "unknown", "reason": "replacement_controller_required",
                    "action_completed": True, "recovered": None, "checks": checks}
        # The deleted object is not recovery evidence. Verify its controller below.
        pod = ""

    if deployment or re.search(r"\bdeployment/", commands_text):
        if not deployment:
            match = re.search(r"\bdeployment/([a-zA-Z0-9][a-zA-Z0-9_.-]{0,252})", commands_text)
            deployment = _extract_k8s_name(match.group(1) if match else "")
        if deployment:
            rollout = _kubectl_heal_check(
                f"rollout status deployment/{shlex.quote(deployment)} -n {shlex.quote(namespace)} --timeout=60s",
                cluster_id=cluster_id,
                timeout=75,
            )
            checks.append({"type": "deployment_rollout", **rollout})
            dep = _kubectl_heal_check(
                f"get deployment {shlex.quote(deployment)} -n {shlex.quote(namespace)} -o json",
                cluster_id=cluster_id,
                parse_json=True,
            )
            checks.append({"type": "deployment_status", **dep})
            recovered = bool(rollout.get("ok")) and bool(dep.get("ok")) and _deployment_available(dep.get("json") or {})
            return {"status": "verified", "target": {"deployment": deployment, "namespace": namespace}, "recovered": recovered, "checks": checks}

    if pod:
        pod_check = _kubectl_heal_check(
            f"get pod {shlex.quote(pod)} -n {shlex.quote(namespace)} -o json",
            cluster_id=cluster_id,
            parse_json=True,
        )
        checks.append({"type": "pod_status", **pod_check})
        if pod_check.get("ok"):
            recovered = _pod_ready(pod_check.get("json") or {})
            return {"status": "verified", "target": {"pod": pod, "namespace": namespace}, "recovered": recovered, "checks": checks}
        if any(" delete pod " in f" {cmd} " for cmd in commands):
            pods = _kubectl_heal_check(
                f"get pods -n {shlex.quote(namespace)} --no-headers",
                cluster_id=cluster_id,
            )
            checks.append({"type": "namespace_pods_after_delete", **pods})
            health = _namespace_health_from_pods(pods.get("stdout", ""))
            return {
                "status": "verified" if pods.get("ok") else "unknown",
                "target": {"pod": pod, "namespace": namespace},
                "recovered": health["healthy"] if pods.get("ok") else None,
                "checks": checks,
                "summary": health,
            }

    if service:
        endpoints = _kubectl_heal_check(
            f"get endpoints {shlex.quote(service)} -n {shlex.quote(namespace)} -o json",
            cluster_id=cluster_id,
            parse_json=True,
        )
        checks.append({"type": "service_endpoints", **endpoints})
        recovered = bool(endpoints.get("ok")) and _endpoints_ready(endpoints.get("json") or {})
        return {"status": "verified", "target": {"service": service, "namespace": namespace}, "recovered": recovered, "checks": checks}

    if node:
        node_check = _kubectl_heal_check(
            f"get node {shlex.quote(node)} -o json",
            cluster_id=cluster_id,
            parse_json=True,
        )
        checks.append({"type": "node_status", **node_check})
        recovered = bool(node_check.get("ok")) and _node_ready(node_check.get("json") or {})
        return {"status": "verified", "target": {"node": node}, "recovered": recovered, "checks": checks}

    pods = _kubectl_heal_check(
        f"get pods -n {shlex.quote(namespace)} --no-headers",
        cluster_id=cluster_id,
    )
    checks.append({"type": "namespace_pods", **pods})
    health = _namespace_health_from_pods(pods.get("stdout", ""))
    return {
        "status": "verified" if pods.get("ok") else "unknown",
        "target": {"namespace": namespace},
        "recovered": health["healthy"] if pods.get("ok") else None,
        "checks": checks,
        "summary": health,
    }


def _prometheus_api_ok(base_url: str) -> bool:
    """Return True if the URL looks like a working Prometheus API endpoint."""
    if not base_url:
        return False
    import requests as req
    try:
        resp = req.get(
            f"{base_url.rstrip('/')}/api/v1/query",
            params={"query": "up"},
            timeout=4,
        )
        if resp.status_code != 200:
            return False
        data = resp.json()
        return data.get("status") == "success" and "data" in data
    except Exception:
        return False


def _discover_prometheus_url() -> str:
    """
    Discover a reachable Prometheus URL.

    The validation host is not always a K8S node, so ClusterIP and localhost can
    fail. Prefer configured URL, then discover the Prometheus NodePort and test
    all node InternalIPs.
    """
    cached = _state.get("prometheus_url")
    if cached and _prometheus_api_ok(cached):
        return cached

    cfg = _get_config()
    candidates: List[str] = []
    if cfg.observability.prometheus_url:
        candidates.append(cfg.observability.prometheus_url.rstrip("/"))

    try:
        svc_data = json.loads(_kubectl_sync("get svc -A -o json"))
    except Exception:
        svc_data = {}
    try:
        node_data = json.loads(_kubectl_sync("get nodes -o json"))
    except Exception:
        node_data = {}

    nodes: List[str] = []
    for node in node_data.get("items", []):
        for addr in node.get("status", {}).get("addresses", []):
            if addr.get("type") == "InternalIP" and addr.get("address"):
                nodes.append(addr["address"])

    for svc in svc_data.get("items", []):
        meta = svc.get("metadata", {})
        spec = svc.get("spec", {})
        ns_name = f"{meta.get('namespace', '')}/{meta.get('name', '')}".lower()
        if "prom" not in ns_name:
            continue
        for port in spec.get("ports", []):
            port_num = port.get("port")
            target_name = str(port.get("name", "")).lower()
            if port_num != 9090 and "web" not in target_name and "prom" not in target_name:
                continue

            cluster_ip = spec.get("clusterIP")
            if cluster_ip and cluster_ip != "None":
                candidates.append(f"http://{cluster_ip}:{port_num}")

            node_port = port.get("nodePort")
            if node_port:
                for node_ip in nodes:
                    candidates.append(f"http://{node_ip}:{node_port}")

    seen = set()
    for url in candidates:
        if not url or url in seen:
            continue
        seen.add(url)
        if _prometheus_api_ok(url):
            _state["prometheus_url"] = url
            logger.info("Prometheus endpoint selected: %s", url)
            return url

    return cfg.observability.prometheus_url.rstrip("/") if cfg.observability.prometheus_url else ""


def _discover_otel_prometheus_url() -> str:
    """Discover the OpenTelemetry Demo Prometheus source for service metrics."""
    cached = _state.get("otel_prometheus_url")
    if cached and _prometheus_api_ok(cached):
        return cached

    cfg = _get_config()
    configured = getattr(cfg.observability, "otel_prometheus_url", "")
    candidates: List[str] = []
    if configured:
        candidates.append(configured.rstrip("/"))

    try:
        svc_data = json.loads(_kubectl_sync("get svc -n otel-demo prometheus -o json"))
    except Exception:
        svc_data = {}
    try:
        node_data = json.loads(_kubectl_sync("get nodes -o json"))
    except Exception:
        node_data = {}

    nodes: List[str] = []
    for node in node_data.get("items", []):
        for addr in node.get("status", {}).get("addresses", []):
            if addr.get("type") == "InternalIP" and addr.get("address"):
                nodes.append(addr["address"])

    spec = svc_data.get("spec") if isinstance(svc_data, dict) else {}
    for port in spec.get("ports", []) or []:
        port_num = port.get("port")
        if port_num != 9090:
            continue
        cluster_ip = spec.get("clusterIP")
        if cluster_ip and cluster_ip != "None":
            candidates.append(f"http://{cluster_ip}:{port_num}")
        node_port = port.get("nodePort")
        if node_port:
            for node_ip in nodes:
                candidates.append(f"http://{node_ip}:{node_port}")

    seen = set()
    for url in candidates:
        if not url or url in seen:
            continue
        seen.add(url)
        if _prometheus_api_ok(url):
            _state["otel_prometheus_url"] = url
            logger.info("OTel Prometheus endpoint selected: %s", url)
            return url
    return configured.rstrip("/") if configured else ""


# ─────────────────────────────────────────
# Page Routes
# ─────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _auth_enabled() and _get_auth_session(request):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "auth_enabled": _auth_enabled(),
            "default_username": _auth_username(),
        },
    )


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return Response(status_code=204)


@app.post("/api/auth/login")
async def auth_login(request: Request):
    body = await request.json()
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    if not _auth_enabled():
        return {"status": "ok", "auth_enabled": False, "username": username or _auth_username()}
    expected_password = _auth_password()
    if not expected_password:
        raise HTTPException(503, "auth_password_not_configured")
    if not username or not password:
        raise HTTPException(400, "Missing username or password")
    if not (
        hmac.compare_digest(username, _auth_username())
        and hmac.compare_digest(password, expected_password)
    ):
        raise HTTPException(401, "用户名或密码错误")
    session_token = _new_auth_session(username)
    resp = JSONResponse({
        "status": "ok",
        "auth_enabled": True,
        "username": username,
        "expires_in": _auth_session_ttl(),
    })
    resp.set_cookie(
        AUTH_SESSION_COOKIE,
        session_token,
        max_age=_auth_session_ttl(),
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        path="/",
    )
    return resp


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    _delete_auth_session(request)
    resp = JSONResponse({"status": "ok"})
    resp.delete_cookie(AUTH_SESSION_COOKIE, path="/")
    return resp


@app.get("/api/auth/me")
async def auth_me(request: Request):
    session = _get_auth_session(request)
    bearer = _bearer_token_valid(request)
    return {
        "auth_enabled": _auth_enabled(),
        "authenticated": (not _auth_enabled()) or bool(session) or bearer,
        "username": (session or {}).get("username") or ("token" if bearer else ""),
        "expires_at": (session or {}).get("expires_at"),
    }


# ─────────────────────────────────────────
# Cluster Info APIs
# ─────────────────────────────────────────

@app.get("/api/cluster/overview")
async def cluster_overview():
    """Cluster health summary."""
    nodes_raw, pods_raw = await asyncio.gather(
        _kubectl_json("get nodes"),
        _kubectl_json("get pods --all-namespaces"),
    )
    
    nodes = nodes_raw.get("items", [])
    pods = pods_raw.get("items", [])
    
    # Count pod phases
    phases = {}
    total_restarts = 0
    for pod in pods:
        phase = pod.get("status", {}).get("phase", "Unknown")
        phases[phase] = phases.get(phase, 0) + 1
        for cs in pod.get("status", {}).get("containerStatuses", []):
            total_restarts += cs.get("restartCount", 0)
    
    return {
        "nodes": len(nodes),
        "pods_total": len(pods),
        "pod_phases": phases,
        "total_restarts": total_restarts,
        "namespaces": len(set(p.get("metadata", {}).get("namespace", "") for p in pods)),
    }


@app.get("/api/cluster/nodes")
async def cluster_nodes():
    """Detailed node info."""
    data = await _kubectl_json("get nodes")
    nodes = []
    for n in data.get("items", []):
        meta = n.get("metadata", {})
        status = n.get("status", {})
        conditions = {c["type"]: c["status"] for c in status.get("conditions", [])}
        nodes.append({
            "name": meta.get("name", ""),
            "roles": [l.split("/")[-1] for l in meta.get("labels", {}) if "node-role" in l],
            "ready": conditions.get("Ready", "Unknown"),
            "version": status.get("nodeInfo", {}).get("kubeletVersion", ""),
            "os": status.get("nodeInfo", {}).get("osImage", ""),
            "cpu": status.get("capacity", {}).get("cpu", ""),
            "memory": status.get("capacity", {}).get("memory", ""),
        })
    return {"nodes": nodes}


@app.get("/api/cluster/namespaces")
async def cluster_namespaces():
    raw = await _kubectl("get namespaces -o jsonpath='{.items[*].metadata.name}'")
    return {"namespaces": raw.replace("'", "").split()}


@app.get("/api/cluster/pods")
async def cluster_pods(namespace: str = ""):
    """List pods with status info."""
    if namespace:
        data = await _kubectl_json("get pods", namespace)
    else:
        data = await _kubectl_json("get pods --all-namespaces")
    pods = []
    for p in data.get("items", []):
        meta = p.get("metadata", {})
        status = p.get("status", {})
        containers = status.get("containerStatuses", [])
        ready = sum(1 for c in containers if c.get("ready"))
        restarts = sum(c.get("restartCount", 0) for c in containers)
        pods.append({
            "name": meta.get("name", ""),
            "namespace": meta.get("namespace", ""),
            "phase": status.get("phase", "Unknown"),
            "ready": f"{ready}/{len(containers)}",
            "restarts": restarts,
            "node": p.get("spec", {}).get("nodeName", ""),
            "age": meta.get("creationTimestamp", ""),
        })
    return {"pods": pods}


@app.get("/api/cluster/events")
async def cluster_events(namespace: str = "", limit: int = 50):
    """Recent K8s events."""
    if namespace:
        data = await _kubectl_json("get events --sort-by=.lastTimestamp", namespace)
    else:
        data = await _kubectl_json("get events --sort-by=.lastTimestamp --all-namespaces")
    events = []
    for e in data.get("items", [])[-limit:]:
        events.append({
            "type": e.get("type", ""),
            "reason": e.get("reason", ""),
            "message": e.get("message", ""),
            "source": e.get("source", {}).get("component", ""),
            "object": e.get("involvedObject", {}).get("name", ""),
            "namespace": e.get("involvedObject", {}).get("namespace", ""),
            "count": e.get("count", 1),
            "last_seen": e.get("lastTimestamp", ""),
        })
    return {"events": events}


@app.get("/api/cluster/services")
async def cluster_services(namespace: str = ""):
    if namespace:
        data = await _kubectl_json("get services", namespace)
    else:
        data = await _kubectl_json("get services --all-namespaces")
    services = []
    for s in data.get("items", []):
        meta = s.get("metadata", {})
        spec = s.get("spec", {})
        ports = [f"{p.get('port')}/{p.get('protocol','TCP')}" for p in spec.get("ports", [])]
        services.append({
            "name": meta.get("name", ""),
            "namespace": meta.get("namespace", ""),
            "type": spec.get("type", ""),
            "cluster_ip": spec.get("clusterIP", ""),
            "ports": ", ".join(ports),
        })
    return {"services": services}


@app.get("/api/cluster/topology")
async def cluster_topology(cluster_id: str = ""):
    """完整拓扑数据：nodes + pods + deployments + services + 故障标记"""
    cluster = _find_k8s_cluster(cluster_id) if cluster_id else None
    effective_cluster_id = cluster.get("id") if cluster else None
    nodes_raw, pods_raw, deploy_raw, svc_raw = await asyncio.gather(
        _kubectl_json("get nodes", cluster_id=effective_cluster_id),
        _kubectl_json("get pods --all-namespaces", cluster_id=effective_cluster_id),
        _kubectl_json("get deployments --all-namespaces", cluster_id=effective_cluster_id),
        _kubectl_json("get services --all-namespaces", cluster_id=effective_cluster_id),
    )

    nodes = []
    for n in nodes_raw.get("items", []):
        meta = n.get("metadata", {})
        status = n.get("status", {})
        conditions = {c["type"]: c["status"] for c in status.get("conditions", [])}
        ready = conditions.get("Ready", "Unknown") == "True"
        roles = [l.split("/")[-1] for l in meta.get("labels", {}) if "node-role" in l]
        nodes.append({
            "name": meta.get("name", ""),
            "roles": roles,
            "ready": ready,
            "is_faulty": not ready,
            "cpu": status.get("capacity", {}).get("cpu", ""),
            "memory": status.get("capacity", {}).get("memory", ""),
        })

    pods = []
    for p in pods_raw.get("items", []):
        meta = p.get("metadata", {})
        pstatus = p.get("status", {})
        containers = pstatus.get("containerStatuses", [])
        restarts = sum(c.get("restartCount", 0) for c in containers)
        phase = pstatus.get("phase", "Unknown")
        labels = meta.get("labels", {})
        owner_refs = meta.get("ownerReferences", [])
        owner = owner_refs[0].get("name", "") if owner_refs else ""
        owner_kind = owner_refs[0].get("kind", "") if owner_refs else ""

        # Detect faults: only based on CURRENT state, not historical restarts
        is_faulty = False
        fault_reason = ""
        all_ready = True
        for cs in containers:
            if not cs.get("ready", False):
                all_ready = False
            waiting = cs.get("state", {}).get("waiting", {})
            reason = waiting.get("reason", "")
            if reason in ("CrashLoopBackOff", "ImagePullBackOff", "OOMKilled", "Error", "CreateContainerError", "RunContainerError"):
                is_faulty = True
                fault_reason = reason
            terminated = cs.get("state", {}).get("terminated", {})
            t_reason = terminated.get("reason", "")
            if t_reason in ("OOMKilled", "Error"):
                is_faulty = True
                fault_reason = t_reason
        if phase not in ("Running", "Succeeded"):
            is_faulty = True
            if not fault_reason:
                fault_reason = phase
        if phase == "Running" and not all_ready and not is_faulty:
            is_faulty = True
            fault_reason = "容器未就绪"

        pods.append({
            "name": meta.get("name", ""),
            "uid": meta.get("uid", ""),
            "namespace": meta.get("namespace", ""),
            "owner_uid": owner_refs[0].get("uid", "") if owner_refs else "",
            "node": p.get("spec", {}).get("nodeName", ""),
            "phase": phase,
            "restarts": restarts,
            "is_faulty": is_faulty,
            "fault_reason": fault_reason,
            "ready": f"{sum(1 for c in containers if c.get('ready'))}/{len(containers)}",
            "containers": len(containers),
            "labels": labels,
            "owner": owner,
            "owner_kind": owner_kind,
        })

    # Deployments
    deployments = []
    for d in deploy_raw.get("items", []):
        meta = d.get("metadata", {})
        spec = d.get("spec", {})
        status = d.get("status", {})
        desired = spec.get("replicas", 0)
        available = status.get("availableReplicas", 0) or 0
        ready_r = status.get("readyReplicas", 0) or 0
        selector_labels = spec.get("selector", {}).get("matchLabels", {})
        is_faulty = available < desired
        deployments.append({
            "name": meta.get("name", ""),
            "namespace": meta.get("namespace", ""),
            "replicas": desired,
            "available": available,
            "ready": ready_r,
            "is_faulty": is_faulty,
            "selector": selector_labels,
        })

    # Services
    services = []
    for s in svc_raw.get("items", []):
        meta = s.get("metadata", {})
        spec = s.get("spec", {})
        selector = spec.get("selector", {})
        ports = [f"{p.get('port')}" for p in spec.get("ports", [])]
        services.append({
            "name": meta.get("name", ""),
            "namespace": meta.get("namespace", ""),
            "type": spec.get("type", ""),
            "cluster_ip": spec.get("clusterIP", ""),
            "ports": ports,
            "selector": selector,
        })

    namespaces = sorted(set(p["namespace"] for p in pods))

    return {
        "cluster": _redact_cluster_runtime_fields(cluster) if cluster else None,
        "cluster_id": effective_cluster_id,
        "nodes": nodes,
        "pods": pods,
        "deployments": deployments,
        "services": services,
        "namespaces": namespaces,
        "summary": {
            "total_nodes": len(nodes),
            "total_pods": len(pods),
            "total_deployments": len(deployments),
            "total_services": len(services),
            "faulty_nodes": sum(1 for n in nodes if n["is_faulty"]),
            "faulty_pods": sum(1 for p in pods if p["is_faulty"]),
            "faulty_deployments": sum(1 for d in deployments if d["is_faulty"]),
        },
    }


@app.get("/api/logs/{namespace}/{pod}")
async def pod_logs(namespace: str, pod: str, lines: int = 200, container: str = ""):
    c_flag = f"-c {container}" if container else ""
    raw = await _kubectl(f"logs {pod} {c_flag} --tail={lines}", namespace)
    return {"logs": raw}


# ─────────────────────────────────────────
# Prometheus Query APIs
# ─────────────────────────────────────────

def _prom_query_sync(query: str, query_type: str = "instant",
                     start: str = "", end: str = "", step: str = "60s",
                     source: str = "cluster") -> dict:
    """Execute Prometheus query synchronously."""
    base_url = _discover_otel_prometheus_url() if source == "otel" else _discover_prometheus_url()
    if not base_url:
        return {"error": f"Prometheus URL not configured for source={source}"}

    import requests as req
    try:
        if query_type == "range":
            url = f"{base_url}/api/v1/query_range"
            params = {"query": query, "step": step}
            params["start"] = start or str(int(time.time()) - 3600)
            params["end"] = end or str(int(time.time()))
        else:
            url = f"{base_url}/api/v1/query"
            params = {"query": query}

        resp = req.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "success":
            return {"results": data.get("data", {}).get("result", []),
                    "resultType": data.get("data", {}).get("resultType", ""),
                    "source": source,
                    "base_url": base_url}
        return {"error": data.get("error", "Unknown error")}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/prometheus/query")
async def prometheus_query(query: str = "", query_type: str = "instant",
                           start: str = "", end: str = "", step: str = "60s",
                           source: str = "cluster"):
    """Execute arbitrary PromQL queries."""
    if not query:
        raise HTTPException(400, "Missing 'query' parameter")
    source = "otel" if source == "otel" else "cluster"
    result = await asyncio.to_thread(_prom_query_sync, query, query_type, start, end, step, source)
    return result


def _duration_seconds(duration: str, default: int = 900) -> int:
    dur_map = {
        "5m": 300,
        "10m": 600,
        "15m": 900,
        "30m": 1800,
        "1h": 3600,
        "3h": 10800,
        "6h": 21600,
        "12h": 43200,
        "24h": 86400,
    }
    return dur_map.get(duration, default)


def _parse_epoch_seconds(value: Any) -> Optional[int]:
    """Parse a browser-provided epoch-seconds timestamp for Prometheus range queries."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    if not (0 < parsed < 4_102_444_800):
        return None
    return int(parsed)


def _prom_query_url_sync(
    base_url: str,
    query: str,
    *,
    query_type: str = "instant",
    start: str = "",
    end: str = "",
    step: str = "60s",
) -> Dict[str, Any]:
    """Execute a Prometheus query against an explicit base URL."""
    import requests as req

    try:
        if query_type == "range":
            url = f"{base_url.rstrip('/')}/api/v1/query_range"
            params = {
                "query": query,
                "start": start or str(int(time.time()) - 900),
                "end": end or str(int(time.time())),
                "step": step,
            }
        else:
            url = f"{base_url.rstrip('/')}/api/v1/query"
            params = {"query": query}
        resp = req.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            return {"error": data.get("error") or "Prometheus query failed"}
        return {
            "results": data.get("data", {}).get("result", []),
            "resultType": data.get("data", {}).get("resultType", ""),
            "base_url": base_url.rstrip("/"),
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/prometheus/metrics_summary")
async def prometheus_metrics_summary(namespace: str = ""):
    """Pre-built metrics summary for the dashboard: node CPU/mem/disk + container top."""
    ns_filter = f'namespace="{namespace}"' if namespace else ''

    queries = {
        "node_cpu": 'avg by(instance)(1 - rate(node_cpu_seconds_total{mode="idle"}[1m])) * 100',
        "node_memory": '(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100',
        "node_disk": '(1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}) * 100',
    }

    if ns_filter:
        queries["container_cpu_top"] = (
            f'topk(10, sum by(pod)(rate(container_cpu_usage_seconds_total{{{ns_filter}}}[1m])) * 100)'
        )
        queries["container_mem_top"] = (
            f'topk(10, sum by(pod)(container_memory_working_set_bytes{{{ns_filter}}}) / 1024 / 1024)'
        )
    else:
        queries["container_cpu_top"] = (
            'topk(10, sum by(pod, namespace)(rate(container_cpu_usage_seconds_total[1m])) * 100)'
        )
        queries["container_mem_top"] = (
            'topk(10, sum by(pod, namespace)(container_memory_working_set_bytes) / 1024 / 1024)'
        )

    import concurrent.futures
    results = {}

    def _query_one(key, q):
        return key, _prom_query_sync(q)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(_query_one, k, q) for k, q in queries.items()]
        for f in concurrent.futures.as_completed(futures):
            k, v = f.result()
            results[k] = v

    return results


# ─────────────────────────────────────────
# Metric History APIs (time-series charts)
# ─────────────────────────────────────────

def _detect_anomalies_zscore(values: list, threshold: float = 3.0) -> list:
    """Z-score anomaly detection on a list of float values."""
    import math
    n = len(values)
    if n < 5:
        return []
    mean = sum(values) / n
    std = math.sqrt(sum((v - mean) ** 2 for v in values) / n)
    if std == 0:
        return []
    anomalies = []
    for i, v in enumerate(values):
        z = abs(v - mean) / std
        if z > threshold:
            anomalies.append({
                "index": i, "value": round(v, 4),
                "zscore": round(z, 2),
                "severity": "critical" if z > threshold * 1.5 else "warning",
                "method": "zscore",
            })
    return anomalies


def _detect_anomalies_threshold(values: list, warn: float = None, crit: float = None) -> list:
    """Static threshold anomaly detection."""
    anomalies = []
    for i, v in enumerate(values):
        if crit is not None and v >= crit:
            anomalies.append({"index": i, "value": round(v, 4), "severity": "critical", "method": "threshold"})
        elif warn is not None and v >= warn:
            anomalies.append({"index": i, "value": round(v, 4), "severity": "warning", "method": "threshold"})
    return anomalies


def _detect_anomalies_ewma(values: list, span: int = 10, threshold: float = 3.0) -> list:
    """EWMA (Exponentially Weighted Moving Average) anomaly detection."""
    import math
    n = len(values)
    if n < span:
        return []
    alpha = 2.0 / (span + 1)
    ewma = values[0]
    ewma_var = 0.0
    anomalies = []
    for i, v in enumerate(values):
        if i == 0:
            continue
        diff = v - ewma
        ewma = alpha * v + (1 - alpha) * ewma
        ewma_var = alpha * diff * diff + (1 - alpha) * ewma_var
        std = math.sqrt(ewma_var) if ewma_var > 0 else 0
        if std > 0 and abs(diff) / std > threshold:
            anomalies.append({
                "index": i, "value": round(v, 4),
                "zscore": round(abs(diff) / std, 2),
                "severity": "critical" if abs(diff) / std > threshold * 1.5 else "warning",
                "method": "ewma",
            })
    return anomalies


def _run_anomaly_detection(values: list, methods: list, z_threshold: float,
                           ewma_span: int, warn: float = None, crit: float = None) -> list:
    """Run configured anomaly detection methods and merge results."""
    all_anomalies = []
    seen_indices = set()
    if "zscore" in methods:
        for a in _detect_anomalies_zscore(values, z_threshold):
            if a["index"] not in seen_indices:
                seen_indices.add(a["index"])
                all_anomalies.append(a)
    if "threshold" in methods:
        for a in _detect_anomalies_threshold(values, warn, crit):
            if a["index"] not in seen_indices:
                seen_indices.add(a["index"])
                all_anomalies.append(a)
    if "ewma" in methods:
        for a in _detect_anomalies_ewma(values, ewma_span, z_threshold):
            if a["index"] not in seen_indices:
                seen_indices.add(a["index"])
                all_anomalies.append(a)
    return all_anomalies


@app.get("/api/prometheus/metric_history")
async def prometheus_metric_history(
    metric_name: str = "",
    duration: str = "1h",
    step: str = "",
    custom_query: str = "",
    max_series: int = 10,
    source: str = "cluster",
):
    """Range query for a configured metric, with anomaly detection."""
    cfg = _get_config()

    # Resolve query + thresholds + detection methods from config metric_checks
    query = custom_query
    warn_threshold = None
    crit_threshold = None
    unit = "%"
    label_key = "instance"
    detect_methods = cfg.detection.default_detect_methods or ["zscore"]
    z_threshold = cfg.detection.default_z_threshold or 3.0
    ewma_span = cfg.detection.default_ewma_span or 10

    if metric_name and not custom_query:
        for mc in cfg.detection.metric_checks:
            if mc.get("name") == metric_name:
                query = mc.get("query", "")
                warn_threshold = mc.get("warn")
                crit_threshold = mc.get("crit")
                unit = mc.get("unit", "%")
                label_key = mc.get("label_key", "instance")
                detect_methods = mc.get("detect_methods") or detect_methods
                break

    if not query:
        raise HTTPException(400, "No query found for this metric")

    # Calculate time range
    dur_map = {
        "5m": 300,
        "10m": 600,
        "15m": 900,
        "30m": 1800,
        "1h": 3600,
        "3h": 10800,
        "6h": 21600,
        "12h": 43200,
        "24h": 86400,
    }
    dur_seconds = dur_map.get(duration, 3600)
    now = int(time.time())
    start_ts = str(now - dur_seconds)
    end_ts = str(now)

    if not step:
        if dur_seconds <= 1800:
            step = "15s"
        elif dur_seconds <= 3600:
            step = "30s"
        elif dur_seconds <= 21600:
            step = "120s"
        else:
            step = "300s"

    result = await asyncio.to_thread(
        _prom_query_sync, query, "range", start_ts, end_ts, step, "otel" if source == "otel" else "cluster"
    )

    if result.get("error"):
        return result

    # Build series + detect anomalies
    series = []
    all_anomalies = []

    for r in result.get("results", []):
        metric_labels = r.get("metric", {})
        label = metric_labels.get(label_key) or metric_labels.get("pod") or \
                metric_labels.get("instance") or str(metric_labels)
        # Shorten label
        label = label.replace(":9100", "").replace(":10250", "")
        # Append namespace for container-level if multiple namespaces
        ns_val = metric_labels.get("namespace", "")
        if ns_val and label_key in ("pod",) and not custom_query:
            label = f"{label} ({ns_val})"

        values_raw = r.get("values", [])
        timestamps = [v[0] for v in values_raw]
        values = [float(v[1]) if v[1] != "NaN" else 0.0 for v in values_raw]

        series.append({
            "label": label,
            "timestamps": timestamps,
            "values": [round(v, 4) for v in values],
        })

        # Anomaly detection per series using configured methods
        anoms = _run_anomaly_detection(
            values, detect_methods, z_threshold, ewma_span,
            warn_threshold, crit_threshold
        )
        for a in anoms:
            a["series_label"] = label
            if a["index"] < len(timestamps):
                a["timestamp"] = timestamps[a["index"]]
            all_anomalies.append(a)

    # Limit series count: sort by average value descending, keep top max_series
    if len(series) > max_series:
        series.sort(key=lambda s: sum(s["values"]) / max(len(s["values"]), 1), reverse=True)
        kept_labels = {s["label"] for s in series[:max_series]}
        series = series[:max_series]
        all_anomalies = [a for a in all_anomalies if a.get("series_label") in kept_labels]

    return {
        "metric_name": metric_name,
        "unit": unit,
        "duration": duration,
        "source": "otel" if source == "otel" else "cluster",
        "series": series,
        "anomalies": all_anomalies,
        "thresholds": {"warn": warn_threshold, "crit": crit_threshold},
        "detection": {
            "methods": detect_methods,
            "z_threshold": z_threshold,
            "ewma_span": ewma_span,
            "anomaly_count": len(all_anomalies),
        },
    }


@app.get("/api/prometheus/business_metrics")
async def prometheus_business_metrics(duration: str = "1h", step: str = "",
                                      namespace: str = "", source: str = "cluster"):
    """Business-level metrics: service request rate, latency P99, error rate."""
    cfg = _get_config()
    services = cfg.detection.business_services or []

    dur_map = {
        "5m": 300,
        "10m": 600,
        "15m": 900,
        "30m": 1800,
        "1h": 3600,
        "3h": 10800,
        "6h": 21600,
        "12h": 43200,
        "24h": 86400,
    }
    dur_seconds = dur_map.get(duration, 3600)
    now = int(time.time())
    start_ts = str(now - dur_seconds)
    end_ts = str(now)

    if not step:
        step = "15s" if dur_seconds <= 1800 else "30s" if dur_seconds <= 3600 else "120s" if dur_seconds <= 21600 else "300s"

    prom_source = "otel" if source == "otel" or namespace == "otel-demo" else "cluster"
    rate_window = "2m" if dur_seconds <= 1800 else "5m"
    if prom_source == "otel":
        queries = {
            "request_rate": f'sum by(service_name)(rate(app_frontend_requests_total{{service_name="frontend"}}[{rate_window}]))',
            "latency_p99": f'histogram_quantile(0.99, sum by(le, service_name)(rate(http_server_duration_milliseconds_bucket{{service_name="frontend"}}[{rate_window}])))',
            "error_rate": f'(sum(rate(app_frontend_requests_total{{service_name="frontend",status=~"5.."}}[{rate_window}])) or vector(0)) + (max(httpcheck_status{{service_name="frontend-proxy",http_status_class="5xx"}}) or vector(0))',
        }
    else:
        svc_filter = "|".join(services) if services else ".*"
        ns_filter = f',namespace="{namespace}"' if namespace else ''
        queries = {
            "request_rate": f'sum by(service)(rate(istio_requests_total{{reporter="source",destination_service_name=~"{svc_filter}"{ns_filter}}}[5m])) or sum by(service)(rate(http_server_requests_seconds_count{{service=~"{svc_filter}"{ns_filter}}}[5m]))',
            "latency_p99": f'histogram_quantile(0.99, sum by(le, service)(rate(istio_request_duration_milliseconds_bucket{{reporter="source",destination_service_name=~"{svc_filter}"{ns_filter}}}[5m]))) or histogram_quantile(0.99, sum by(le, service)(rate(http_server_requests_seconds_bucket{{service=~"{svc_filter}"{ns_filter}}}[5m]))) * 1000',
            "error_rate": f'sum by(service)(rate(istio_requests_total{{reporter="source",response_code=~"5.*",destination_service_name=~"{svc_filter}"{ns_filter}}}[5m])) / sum by(service)(rate(istio_requests_total{{reporter="source",destination_service_name=~"{svc_filter}"{ns_filter}}}[5m])) * 100',
        }

    import concurrent.futures
    results = {}

    def _query_one(key, q):
        return key, _prom_query_sync(q, "range", start_ts, end_ts, step, prom_source)

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(_query_one, k, q) for k, q in queries.items()]
        for f in concurrent.futures.as_completed(futures):
            k, v = f.result()
            results[k] = v

    return {"duration": duration, "source": prom_source, "metrics": results}


# ─────────────────────────────────────────
# Jaeger Trace APIs
# ─────────────────────────────────────────

def _jaeger_api_ok(base_url: str) -> tuple[bool, List[str]]:
    import requests as req
    if not base_url:
        return False, []
    try:
        resp = req.get(f"{base_url.rstrip('/')}/api/services", timeout=5)
        if resp.status_code != 200:
            return False, []
        data = resp.json()
        services = data.get("data", [])
        return isinstance(services, list), services if isinstance(services, list) else []
    except Exception:
        return False, []


def _discover_jaeger_url(cluster_id: str = "") -> str:
    """Discover a reachable Jaeger query API endpoint."""
    cached_urls = _state.setdefault("jaeger_urls", {})
    cached = cached_urls.get(cluster_id or "default")
    ok, services = _jaeger_api_ok(cached or "")
    if ok and services:
        return cached

    cfg = _get_config()
    candidates: List[str] = []
    cluster = _find_k8s_cluster(cluster_id) if cluster_id else None
    if cluster and cluster.get("jaeger_url"):
        candidates.append(str(cluster.get("jaeger_url")).rstrip("/"))
    if cfg.observability.jaeger_url:
        candidates.append(cfg.observability.jaeger_url.rstrip("/"))

    svc_result = _shell_sync("kubectl get svc -A -o json", 30, cluster_id or None)
    node_result = _shell_sync("kubectl get nodes -o json", 30, cluster_id or None)
    try:
        svc_data = json.loads(svc_result.get("stdout") or "{}")
    except json.JSONDecodeError:
        svc_data = {}
    try:
        node_data = json.loads(node_result.get("stdout") or "{}")
    except json.JSONDecodeError:
        node_data = {}

    nodes: List[str] = []
    for node in node_data.get("items", []):
        for addr in node.get("status", {}).get("addresses", []):
            if addr.get("type") == "InternalIP" and addr.get("address"):
                nodes.append(addr["address"])

    for svc in svc_data.get("items", []):
        meta = svc.get("metadata", {})
        spec = svc.get("spec", {})
        ns_name = f"{meta.get('namespace', '')}/{meta.get('name', '')}".lower()
        if not any(token in ns_name for token in ("jaeger", "trace")):
            continue
        for port in spec.get("ports", []):
            port_num = port.get("port")
            target_name = str(port.get("name", "")).lower()
            if port_num != 16686 and "query" not in target_name and "ui" not in target_name:
                continue

            cluster_ip = spec.get("clusterIP")
            if cluster_ip and cluster_ip != "None":
                candidates.append(f"http://{cluster_ip}:{port_num}")

            node_port = port.get("nodePort")
            if node_port:
                for node_ip in nodes:
                    candidates.append(f"http://{node_ip}:{node_port}")

    preferred_markers = {
        "nginx-web-server", "compose-post-service", "home-timeline-service",
        "user-timeline-service", "post-storage-service",
    }
    first_reachable = ""
    first_with_services = ""
    seen = set()
    for url in candidates:
        if not url or url in seen:
            continue
        seen.add(url)
        ok, services = _jaeger_api_ok(url)
        if ok and services:
            if preferred_markers.intersection(set(services)):
                cached_urls[cluster_id or "default"] = url
                logger.info("Jaeger endpoint selected: %s (%s services, preferred workload)", url, len(services))
                return url
            if not first_with_services:
                first_with_services = url
        if ok and not first_reachable:
            first_reachable = url

    if first_with_services:
        cached_urls[cluster_id or "default"] = first_with_services
        logger.info("Jaeger endpoint selected: %s (first endpoint with services)", first_with_services)
        return first_with_services
    if first_reachable:
        cached_urls[cluster_id or "default"] = first_reachable
        return first_reachable
    return cfg.observability.jaeger_url.rstrip("/") if cfg.observability.jaeger_url else ""


def _jaeger_request(path: str, params: dict = None, *, cluster_id: str = "") -> dict:
    """Execute Jaeger API request synchronously."""
    base_url = _discover_jaeger_url(cluster_id)
    if not base_url:
        return {"error": "Jaeger URL not configured or discovered"}

    import requests as req
    try:
        url = f"{base_url.rstrip('/')}{path}"
        resp = req.get(url, params=params or {}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            data["_selected_url"] = base_url
        return data
    except Exception as e:
        return {"error": str(e), "selected_url": base_url}


@app.get("/api/jaeger/services")
async def jaeger_services(cluster_id: str = ""):
    """List available services in Jaeger."""
    data = await asyncio.to_thread(_jaeger_request, "/api/services", cluster_id=cluster_id)
    services = data.get("data", []) if not data.get("error") else []
    return {
        "services": services,
        "error": data.get("error"),
        "selected_url": data.get("_selected_url") or data.get("selected_url"),
    }


@app.get("/api/jaeger/traces")
async def jaeger_traces(service: str = "", operation: str = "",
                        min_duration: str = "", max_duration: str = "",
                        limit: int = 20, lookback: str = "1h", cluster_id: str = ""):
    """Search traces by service and filters."""
    if not service:
        raise HTTPException(400, "Missing 'service' parameter")

    params = {"service": service, "limit": limit, "lookback": lookback}
    if operation:
        params["operation"] = operation
    if min_duration:
        params["minDuration"] = min_duration
    if max_duration:
        params["maxDuration"] = max_duration

    data = await asyncio.to_thread(_jaeger_request, "/api/traces", params, cluster_id=cluster_id)
    if data.get("error"):
        return {"traces": [], "error": data["error"]}

    traces = data.get("data", [])
    summaries = []
    for trace in traces[:limit]:
        spans = trace.get("spans", [])
        processes = trace.get("processes", {})
        services_in_trace = sorted(set(
            processes.get(s.get("processID", ""), {}).get("serviceName", "")
            for s in spans
            if processes.get(s.get("processID", ""), {}).get("serviceName", "")
        ))
        durations = [s.get("duration", 0) for s in spans]
        root_span = next((s for s in spans if not s.get("references")), spans[0] if spans else {})
        root_proc = processes.get(root_span.get("processID", ""), {})
        summaries.append({
            "traceID": trace.get("traceID", ""),
            "root_service": root_proc.get("serviceName", ""),
            "root_operation": root_span.get("operationName", ""),
            "span_count": len(spans),
            "services": services_in_trace,
            "total_duration_us": max(durations) if durations else 0,
            "avg_duration_us": sum(durations) // max(len(durations), 1),
            "start_time": root_span.get("startTime", 0),
        })

    return {
        "traces": summaries,
        "total": len(summaries),
        "selected_url": data.get("_selected_url"),
    }


@app.get("/api/jaeger/trace/{trace_id}")
async def jaeger_trace_detail(trace_id: str, cluster_id: str = ""):
    """Get full trace detail by trace ID."""
    data = await asyncio.to_thread(_jaeger_request, f"/api/traces/{trace_id}", cluster_id=cluster_id)
    if data.get("error"):
        return {"error": data["error"]}

    traces = data.get("data", [])
    if not traces:
        raise HTTPException(404, "Trace not found")

    trace = traces[0]
    spans = trace.get("spans", [])
    processes = trace.get("processes", {})

    span_list = []
    for s in spans:
        pid = s.get("processID", "")
        proc = processes.get(pid, {})
        span_list.append({
            "spanID": s.get("spanID", ""),
            "operationName": s.get("operationName", ""),
            "serviceName": proc.get("serviceName", ""),
            "duration_us": s.get("duration", 0),
            "startTime": s.get("startTime", 0),
            "tags": {t["key"]: t["value"] for t in s.get("tags", [])},
            "logs": [{"ts": l.get("timestamp"), "fields": l.get("fields")} for l in s.get("logs", [])],
            "references": s.get("references", []),
        })

    return {
        "traceID": trace.get("traceID"),
        "spans": span_list,
        "span_count": len(span_list),
        "processes": processes,
    }


@app.get("/api/jaeger/operations")
async def jaeger_operations(service: str = "", cluster_id: str = ""):
    """List operations for a Jaeger service."""
    if not service:
        return {"operations": []}
    data = await asyncio.to_thread(_jaeger_request, f"/api/services/{service}/operations", cluster_id=cluster_id)
    operations = data.get("data", []) if not data.get("error") else []
    return {
        "operations": operations,
        "error": data.get("error"),
        "selected_url": data.get("_selected_url") or data.get("selected_url"),
    }


def _skywalking_api_ok(url: str) -> bool:
    if not url:
        return False
    import requests as req
    try:
        response = req.post(
            f"{url.rstrip('/')}/graphql",
            json={"query": "{ version }"},
            timeout=5,
        )
        return response.ok and isinstance(response.json().get("data", {}).get("version"), str)
    except Exception:
        return False


def _discover_skywalking_url(cluster_id: str = "") -> str:
    cached_urls = _state.setdefault("skywalking_urls", {})
    cached = str(cached_urls.get(cluster_id or "default") or "")
    if _skywalking_api_ok(cached):
        return cached
    candidates = [str(os.environ.get("SKYWALKING_URL") or "").rstrip("/")]
    svc_result = _shell_sync("kubectl get svc -A -o json", 30, cluster_id or None)
    node_result = _shell_sync("kubectl get nodes -o json", 30, cluster_id or None)
    try:
        services = json.loads(svc_result.get("stdout") or "{}").get("items") or []
    except json.JSONDecodeError:
        services = []
    try:
        nodes_payload = json.loads(node_result.get("stdout") or "{}").get("items") or []
    except json.JSONDecodeError:
        nodes_payload = []
    node_ips = [
        address.get("address")
        for node in nodes_payload
        for address in node.get("status", {}).get("addresses", [])
        if address.get("type") == "InternalIP" and address.get("address")
    ]
    for service in services:
        metadata = service.get("metadata") or {}
        spec = service.get("spec") or {}
        name = str(metadata.get("name") or "").lower()
        if "skywalking-ui" not in name:
            continue
        for port in spec.get("ports") or []:
            service_port = _to_int(port.get("port"), 0)
            cluster_ip = str(spec.get("clusterIP") or "")
            if cluster_ip and cluster_ip != "None" and service_port:
                candidates.append(f"http://{cluster_ip}:{service_port}")
            node_port = _to_int(port.get("nodePort"), 0)
            if node_port:
                candidates.extend(f"http://{node_ip}:{node_port}" for node_ip in node_ips)
    for candidate in dict.fromkeys(url for url in candidates if url):
        if _skywalking_api_ok(candidate):
            cached_urls[cluster_id or "default"] = candidate
            return candidate
    return ""


def _skywalking_graphql(
    query: str,
    variables: Optional[Dict[str, Any]] = None,
    *,
    cluster_id: str = "",
) -> Dict[str, Any]:
    url = _discover_skywalking_url(cluster_id)
    if not url:
        return {"error": "SkyWalking URL not configured or discovered"}
    import requests as req
    try:
        response = req.post(
            f"{url.rstrip('/')}/graphql",
            json={"query": query, "variables": variables or {}},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            return {"error": payload["errors"][0].get("message") or str(payload["errors"]), "selected_url": url}
        payload["_selected_url"] = url
        return payload
    except Exception as exc:
        return {"error": str(exc), "selected_url": url}


@app.get("/api/skywalking/services")
async def skywalking_services(cluster_id: str = ""):
    data = await asyncio.to_thread(
        _skywalking_graphql,
        '{ listServices(layer: "GENERAL") { id name } }',
        None,
        cluster_id=cluster_id,
    )
    services = data.get("data", {}).get("listServices") or [] if not data.get("error") else []
    return {
        "services": services,
        "error": data.get("error"),
        "selected_url": data.get("_selected_url") or data.get("selected_url"),
    }


def _skywalking_duration(lookback: str) -> Dict[str, str]:
    seconds = _duration_seconds(lookback, 3600)
    end = datetime.now(timezone.utc)
    start = end - timedelta(seconds=max(60, seconds))
    return {
        "start": start.strftime("%Y-%m-%d %H%M"),
        "end": end.strftime("%Y-%m-%d %H%M"),
        "step": "MINUTE",
    }


@app.get("/api/skywalking/traces")
async def skywalking_traces(
    service_id: str,
    service_name: str = "",
    cluster_id: str = "",
    limit: int = 20,
    lookback: str = "1h",
):
    query = """
    query QueryTraces($condition: TraceQueryCondition) {
      queryBasicTraces(condition: $condition) {
        traces { segmentId endpointNames duration start isError traceIds }
      }
    }
    """
    variables = {
        "condition": {
            "serviceId": service_id,
            "queryDuration": _skywalking_duration(lookback),
            "traceState": "ALL",
            "queryOrder": "BY_START_TIME",
            "paging": {"pageNum": 1, "pageSize": _clamp_int(limit, 20, 1, 100)},
        }
    }
    data = await asyncio.to_thread(_skywalking_graphql, query, variables, cluster_id=cluster_id)
    if data.get("error"):
        return {"traces": [], "error": data["error"], "selected_url": data.get("selected_url")}
    traces = []
    for item in data.get("data", {}).get("queryBasicTraces", {}).get("traces") or []:
        trace_ids = item.get("traceIds") or []
        traces.append({
            "traceID": trace_ids[0] if trace_ids else item.get("segmentId", ""),
            "root_service": service_name,
            "root_operation": (item.get("endpointNames") or [""])[0],
            "span_count": 0,
            "services": [service_name] if service_name else [],
            "total_duration_us": _to_int(item.get("duration"), 0) * 1000,
            "start_time": _to_int(item.get("start"), 0) * 1000,
            "is_error": bool(item.get("isError")),
        })
    return {
        "traces": traces,
        "total": len(traces),
        "selected_url": data.get("_selected_url"),
        "source": "skywalking",
    }


@app.get("/api/skywalking/trace/{trace_id}")
async def skywalking_trace_detail(trace_id: str, cluster_id: str = ""):
    query = """
    query TraceDetail($traceId: ID!) {
      queryTrace(traceId: $traceId) {
        spans { traceId segmentId spanId parentSpanId serviceCode startTime endTime endpointName isError }
      }
    }
    """
    data = await asyncio.to_thread(
        _skywalking_graphql,
        query,
        {"traceId": trace_id},
        cluster_id=cluster_id,
    )
    if data.get("error"):
        return {"error": data["error"]}
    spans = []
    for item in data.get("data", {}).get("queryTrace", {}).get("spans") or []:
        start_ms = _to_int(item.get("startTime"), 0)
        end_ms = _to_int(item.get("endTime"), start_ms)
        spans.append({
            "spanID": str(item.get("spanId") or ""),
            "operationName": item.get("endpointName") or "",
            "serviceName": item.get("serviceCode") or "",
            "duration_us": max(0, end_ms - start_ms) * 1000,
            "startTime": start_ms * 1000,
            "tags": {"error": bool(item.get("isError"))},
            "logs": [],
            "references": [],
            "parentSpanID": str(item.get("parentSpanId") or ""),
        })
    return {"traceID": trace_id, "spans": spans, "span_count": len(spans), "source": "skywalking"}


# ─────────────────────────────────────────
# Detection & Alert APIs
# ─────────────────────────────────────────

@app.get("/api/detection/signals")
async def get_detection_signals():
    return {"signals": list(_state["detection_signals"])}


@app.delete("/api/detection/signals")
async def clear_detection_signals():
    _state["detection_signals"].clear()
    return {"status": "cleared"}


@app.get("/api/detection/stream")
async def detection_stream():
    """SSE stream for detection signals."""
    async def event_gen():
        last_count = 0
        while True:
            current = len(_state["detection_signals"])
            if current > last_count:
                new = list(_state["detection_signals"])[last_count:]
                for s in new:
                    yield f"data: {json.dumps(s)}\n\n"
                last_count = current
            else:
                yield f": heartbeat {int(time.time())}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.get("/api/detection/config")
async def get_detection_config():
    """返回当前检测配置"""
    cfg = _get_config()
    det = cfg.detection
    return {
        "sources_enabled": det.sources_enabled,
        "metric_checks": det.metric_checks,
        "critical_event_reasons": det.critical_event_reasons,
        "critical_pod_reasons": det.critical_pod_reasons,
        "default_detect_methods": det.default_detect_methods,
        "default_lookback_m": det.default_lookback_m,
        "default_z_threshold": det.default_z_threshold,
        "default_ewma_span": det.default_ewma_span,
        "categories_enabled": det.categories_enabled,
        "business_services": det.business_services,
        "db_services": det.db_services,
        "thresholds": det.thresholds,
    }


@app.put("/api/detection/config")
async def update_detection_config(request: Request):
    """运行时更新检测配置（内存生效，不写 YAML）"""
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "detection config must be an object")
    body = _validate_detection_config_values(body)
    cfg = _get_config()
    det = cfg.detection
    previous_detection = copy.deepcopy(det)
    try:
        _apply_detection_overrides(det, body)
        # 重建 pipeline 使配置生效
        _state["pipeline"] = None
        # 持久化到磁盘
        _save_detection_config()
        _merge_platform_section("detection", {
            "sources_enabled": det.sources_enabled,
            "metric_checks": det.metric_checks,
            "critical_event_reasons": det.critical_event_reasons,
            "critical_pod_reasons": det.critical_pod_reasons,
            "default_detect_methods": det.default_detect_methods,
            "default_algorithm": getattr(det, "default_algorithm", "zscore"),
            "default_lookback_m": det.default_lookback_m,
            "default_z_threshold": det.default_z_threshold,
            "default_ewma_span": det.default_ewma_span,
            "min_samples": getattr(det, "min_samples", 12),
            "confirmation_points": getattr(det, "confirmation_points", 1),
            "relative_change_threshold": getattr(det, "relative_change_threshold", 0.0),
            "spectral_residual_threshold": getattr(det, "spectral_residual_threshold", 3.0),
            "categories_enabled": det.categories_enabled,
            "business_services": det.business_services,
            "db_services": det.db_services,
            "thresholds": det.thresholds,
        })
    except Exception:
        cfg.detection = previous_detection
        _state["pipeline"] = None
        raise
    return {
        "status": "ok",
        "detection": {
            "sources_enabled": det.sources_enabled,
            "metric_checks": det.metric_checks,
            "critical_event_reasons": det.critical_event_reasons,
            "critical_pod_reasons": det.critical_pod_reasons,
            "default_detect_methods": det.default_detect_methods,
            "default_lookback_m": det.default_lookback_m,
            "default_z_threshold": det.default_z_threshold,
            "default_ewma_span": det.default_ewma_span,
            "categories_enabled": det.categories_enabled,
            "business_services": det.business_services,
            "db_services": det.db_services,
            "thresholds": det.thresholds,
        },
    }


@app.get("/api/platform/config")
async def platform_config_get():
    """Return configuration-center stored overrides and effective runtime values."""
    return _platform_config_public()


@app.put("/api/platform/config")
async def platform_config_update(request: Request):
    """Update configuration-center overrides for llm/detection/observability/remediation."""
    body = await request.json()
    allowed = {"llm", "detection", "observability", "remediation"}
    data = _load_platform_config_file()
    for section in allowed:
        if section not in body:
            continue
        value = body.get(section)
        if value is None:
            data.pop(section, None)
        elif isinstance(value, dict):
            value = _validate_platform_config_section(section, value)
            current = data.get(section)
            if not isinstance(current, dict):
                current = {}
            if section == "llm" and value.get("api_key") in ("", None, "********"):
                value = {k: v for k, v in value.items() if k != "api_key"}
            current.update(value)
            data[section] = current
        else:
            raise HTTPException(400, f"{section} must be an object")
    data["updated_at"] = time.time()
    _save_platform_config_file(data)
    _reload_runtime_config()
    return {"status": "ok", **_platform_config_public()}


@app.post("/api/platform/config/reload")
async def platform_config_reload():
    """Reload YAML/env config and re-apply config-center overrides."""
    _reload_runtime_config()
    return {"status": "ok", **_platform_config_public()}


@app.post("/api/platform/config/test-llm")
async def platform_config_test_llm():
    """Issue one real request with the effective LLM configuration."""
    cfg = _get_config()
    if not _llm_configured(cfg.llm):
        raise HTTPException(400, "LLM API Key 未配置；请先保存配置或设置 LLM_API_KEY")

    model = str(getattr(cfg.llm, "model", "") or "").strip()
    base_url = str(getattr(cfg.llm, "base_url", "") or "").strip()
    if not model or not base_url:
        raise HTTPException(400, "LLM model/base_url 未完整配置")

    started_at = time.perf_counter()
    try:
        content = await asyncio.wait_for(
            LLMClient(cfg.llm).async_chat(
                [
                    {
                        "role": "system",
                        "content": "你是 AgenticSRE 连接测试助手。只返回 AGENTICSRE_LLM_OK，不要解释。",
                    },
                    {"role": "user", "content": "执行连接测试。"},
                ],
                temperature=0.0,
                max_tokens=min(8192, max(2048, int(getattr(cfg.llm, "max_tokens", 8192) or 8192))),
                model=model,
            ),
            timeout=min(120.0, max(10.0, float(getattr(cfg.llm, "timeout", 300) or 300))),
        )
    except asyncio.TimeoutError as exc:
        raise HTTPException(504, f"大模型连接测试超时：{model}") from exc
    except Exception as exc:
        logger.warning("LLM configuration test failed for model=%s: %s", model, exc)
        raise HTTPException(502, f"大模型连接测试失败：{exc}") from exc

    elapsed_ms = round((time.perf_counter() - started_at) * 1000)
    preview = str(content or "").strip()[:240]
    if not preview:
        raise HTTPException(502, f"模型 {model} 已响应但未返回可用文本")
    return {
        "status": "ok",
        "model": model,
        "base_url": base_url,
        "latency_ms": elapsed_ms,
        "response_preview": preview,
        "runtime_scope": [
            "RCA/多智能体诊断",
            "告警分析与压缩",
            "故障模式总结与分类",
            "自愈建议生成",
        ],
        "note": "测试使用当前有效配置发起真实推理；后续新建 Agent/Pipeline 调用使用同一配置。",
    }


@app.delete("/api/platform/config")
async def platform_config_reset():
    """Clear configuration-center overrides and fall back to YAML/env config."""
    if _PLATFORM_CONFIG_FILE.exists():
        _PLATFORM_CONFIG_FILE.unlink()
    _reload_runtime_config()
    return {"status": "ok", **_platform_config_public()}


@app.get("/api/alerts/list")
async def alert_list(namespace: str = "", duration: str = "current", cluster_id: str = ""):
    """Fetch all current alerts from all sources with details."""
    resolved_cluster_id = _resolve_general_cluster_id(cluster_id)
    cfg = _cluster_scoped_config(resolved_cluster_id)
    registry = build_tool_registry(cfg, isolated=True)
    llm = LLMClient(cfg.llm) if _llm_configured(cfg.llm) else None
    agent = DetectionAgent(llm, registry, cfg)
    try:
        signals = await asyncio.wait_for(
            asyncio.to_thread(agent.detect, namespace),
            timeout=_ALERT_LIST_TIMEOUT_SECONDS,
        )
        current_alerts = [
            {
                **s.to_dict(),
                "cluster_id": resolved_cluster_id,
                "cluster_kind": "general",
            }
            for s in signals
        ]
        _record_alert_events(current_alerts)
        alerts = (
            _recent_alert_events(duration, current_alerts, cluster_id=resolved_cluster_id)
            if duration and duration != "current"
            else current_alerts
        )
        payload = {
            "alerts": alerts,
            "total": len(alerts),
            "active_total": len(current_alerts),
            "sources": sorted(set(a.get("source") for a in alerts if a.get("source"))),
            "namespace": namespace or "",
            "cluster_id": resolved_cluster_id,
            "cluster_kind": "general",
            "duration": duration,
            "window_seconds": _duration_seconds(duration, 0),
            "degraded": False,
            "cached": False,
        }
        _ALERT_LIST_CACHE.update({
            "cluster_id": resolved_cluster_id,
            "namespace": namespace or "",
            "updated_at": time.time(),
            "payload": payload,
        })
        return payload
    except asyncio.TimeoutError:
        logger.warning("Alert list collection timed out after %.1fs", _ALERT_LIST_TIMEOUT_SECONDS)
        cached = _alert_cache_payload(namespace, cluster_id=resolved_cluster_id, error="告警采集超时，已返回最近一次成功采集结果。")
        return cached or _empty_alert_payload(namespace, cluster_id=resolved_cluster_id)
    except Exception as exc:
        logger.warning("Alert list collection failed: %s", exc, exc_info=True)
        cached = _alert_cache_payload(namespace, cluster_id=resolved_cluster_id, error=f"告警采集失败，已返回缓存：{exc}")
        return cached or _empty_alert_payload(namespace, cluster_id=resolved_cluster_id, error=f"告警采集失败：{exc}")


@app.get("/api/alerts/scan")
async def alert_scan(namespace: str = "", cluster_id: str = ""):
    """Run alert compression scan (SOW core)."""
    result = await _prepare_live_alert_scan(namespace, cluster_id)
    from agents.alert_lifecycle import AlertLifecycle
    from agents.alert_history import AlertHistory
    stored = await asyncio.to_thread(_compression_store().create, result,
        lifecycle=AlertLifecycle(ROOT_DIR / 'data' / 'alert_compression' / 'lifecycle.sqlite3'))
    try:
        history = AlertHistory(ROOT_DIR / 'data' / 'alert_compression' / 'history.sqlite3')
        await asyncio.to_thread(history.record, stored, namespace)
    except Exception as exc:
        logger.warning("Alert snapshot history persistence failed: %s", type(exc).__name__)
    return stored


async def _prepare_live_alert_scan(namespace: str, cluster_id: str, *, for_job=False):
    collection_started_at = time.time()
    resolved_cluster_id = _resolve_general_cluster_id(cluster_id)
    cfg = _cluster_scoped_config(resolved_cluster_id)
    llm = LLMClient(cfg.llm) if _llm_configured(cfg.llm) else None
    registry = build_tool_registry(cfg, isolated=True)
    agent = AlertAgent(llm, registry, cfg)

    try:
        raw_alerts = await asyncio.wait_for(
            asyncio.to_thread(lambda: asyncio.run(agent._collect_alerts(namespace))),
            timeout=max(_ALERT_LIST_TIMEOUT_SECONDS, agent.collection_timeout_s + 5),
        )
        for alert in raw_alerts:
            alert.cluster_id = resolved_cluster_id
        result = await asyncio.wait_for(
            asyncio.to_thread(lambda: asyncio.run(agent.compress_and_recommend(alerts=raw_alerts, namespace=namespace, enrich=False))),
            timeout=max(_ALERT_SCAN_TIMEOUT_SECONDS, agent.semantic_timeout_s + 5),
        )
    except asyncio.TimeoutError:
        logger.warning("Alert scan timed out")
        cached = _alert_cache_payload(namespace, cluster_id=resolved_cluster_id, error="告警压缩扫描超时，已返回最近一次告警列表，暂未进行压缩。")
        raw_alerts = []
        if cached:
            for item in cached.get("alerts", []):
                raw_alerts.append(type("AlertView", (), {
                    "name": item.get("title", ""),
                    "severity": item.get("severity", "warning"),
                    "source": item.get("source", ""),
                    "timestamp": item.get("timestamp", time.time()),
                    "labels": {"namespace": item.get("namespace", ""), "service": item.get("service", "")},
                    "message": item.get("description", ""),
                })())
        result = {
            "agent": "alert_agent",
            "total_alerts": len(raw_alerts),
            "compressed_groups": 0,
            "compression_ratio": 0,
            "groups": [],
            "summary": "告警压缩扫描超时，已降级返回原始告警。",
            "degraded": True,
            "warning": "告警压缩扫描超时，请检查 Prometheus/Kubernetes 采集链路。",
        }

    # Attach raw alert details for frontend display
    if "raw_alerts" not in result:
        warning = result.get("warning", "")
        result = compress_alerts([
        {"name": a.name, "severity": a.severity, "source": a.source,
         "timestamp": a.timestamp, "labels": a.labels, "message": a.message,
         "cluster_id": resolved_cluster_id, "cluster_kind": "general"}
        for a in raw_alerts
        ], cluster_id=resolved_cluster_id, namespace=namespace, time_window=cfg.alert.time_window)
        result["degraded"] = True
        result["warning"] = warning
    result["cluster_id"] = resolved_cluster_id
    result["cluster_kind"] = "general"
    from agents.alert_history import AlertHistory
    result["origin"] = "live_scan"
    result["scan_scope"] = namespace
    result['collection_started_at'] = collection_started_at
    history = AlertHistory(ROOT_DIR / "data" / "alert_compression" / "history.sqlite3")
    try:
        await asyncio.to_thread(history.bootstrap, _compression_store().directory, resolved_cluster_id, namespace)
        await asyncio.to_thread(history.apply, result, namespace)
    except Exception as exc:
        result["automatic_baseline"] = {"status": "unavailable", "error": type(exc).__name__}
    # Use the same endpoint that actually supplied ALERTS, including tool discovery.
    prometheus_tool = registry.get('prometheus')
    observed_backend = getattr(prometheus_tool, 'base_url', '')
    if observed_backend:
        cfg.observability.prometheus_url = observed_backend
    topology = await _enrich_alert_groups(result, cfg, llm=None if for_job else llm,
                                          live_cluster=resolved_cluster_id, build_graph=not for_job)
    if any(result.get(k, {}).get('status') == 'applied' for k in ('threshold_compression', 'trace_compression', 'window_compression')):
        try:
            await asyncio.to_thread(history.apply, result, namespace)
        except Exception as exc:
            result['automatic_baseline'] = {'status':'unavailable','error':type(exc).__name__}
    if for_job:
        result['job_topology'] = topology
    return result


def _compression_store():
    return CompressionStore(ROOT_DIR / "data" / "alert_compression")


@app.get('/api/alerts/lifecycle')
async def alert_lifecycle(namespace: str = '', cluster_id: str = ''):
    from agents.alert_lifecycle import AlertLifecycle
    cluster = _resolve_general_cluster_id(cluster_id)
    store = AlertLifecycle(ROOT_DIR / 'data' / 'alert_compression' / 'lifecycle.sqlite3')
    return {'cluster_id': cluster, 'namespace': namespace, 'limit': 200,
            'incidents': await asyncio.to_thread(store.list, cluster, namespace)}


async def _enrich_alert_groups(result, cfg, llm=None, topology=None, live_cluster=None, build_graph=True):
    # Caller-supplied snapshots never inherit live-observation provenance.
    if topology is not None:
        if not isinstance(topology, dict) or not isinstance(topology.get("edges", []), list):
            raise HTTPException(400, "topology must be an object with edges")
        if len(topology.get("edges", [])) > 10000 or any(not isinstance(e, dict) or not isinstance(e.get("source"), str) or not isinstance(e.get("target"), str) for e in topology.get("edges", [])):
            raise HTTPException(400, "invalid topology edges")
        topology = dict(topology, provenance="client_supplied")
    await enrich_retrieval(result)
    if live_cluster is not None:
        from agents.alert_metrics import collect_metric_evidence
        from tools.llm_client import run_sync_in_daemon_thread
        try:
            result['metric_evidence'] = await asyncio.wait_for(run_sync_in_daemon_thread(
                collect_metric_evidence, cfg.observability.prometheus_url, result), 30)
        except Exception as exc:
            result['metric_evidence'] = {'status':'unavailable','error':type(exc).__name__,'groups':{}}
        from agents.alert_thresholds import merge_nested_thresholds
        if merge_nested_thresholds(result):
            await enrich_retrieval(result)
        observed = {}
        try:
            observed = await asyncio.wait_for(cluster_topology(cluster_id=live_cluster), timeout=25)
            topology = kubernetes_topology(observed, live_cluster)
            from agents.alert_context import collect_scoped_traces, rollout_evidence, namespace_trace_query
            from tools.llm_client import run_sync_in_daemon_thread
            proxy_prefixes = {}
            def query_proxy(namespace, path, params):
                return namespace_trace_query(namespace, path, params,
                    lambda command, timeout: _shell_sync(command, timeout, live_cluster), proxy_prefixes)
            async def traces():
                try:
                    return await asyncio.wait_for(collect_scoped_traces(cfg.observability.jaeger_url, result, observed, proxy_fetch=query_proxy), 43)
                except Exception as exc:
                    return {"status": "unavailable", "error": type(exc).__name__, "edges": []}
            async def rollouts():
                try:
                    raw = await asyncio.wait_for(_kubectl_json("get replicasets --all-namespaces", cluster_id=live_cluster), 22)
                    if "items" not in raw:
                        return {"status": "unavailable", "changes": []}
                    from agents.alert_semantics import entity_ref
                    entities = {entity_ref(g["cluster_id"], g["namespace"], g["entity_kind"], g["entity"]) for g in result["groups"]}
                    return rollout_evidence(raw, live_cluster, observed, "live_kubernetes", entities=entities)
                except Exception as exc:
                    return {"status": "unavailable", "error": type(exc).__name__, "changes": []}
            trace_data, change_data = await asyncio.gather(traces(), rollouts())
            result["context_evidence"] = {"traces": trace_data, "rollouts": change_data}
        except Exception as exc:
            topology = {"provenance": "live_kubernetes", "status": "unavailable", "error": type(exc).__name__}
            result["context_evidence"] = {"traces": {"status": "inventory_unavailable", "edges": []}, "rollouts": {"status": "inventory_unavailable", "changes": []}}
    if live_cluster is not None:
        from agents.alert_trace_merge import merge_trace_observations
        if merge_trace_observations(result):
            await enrich_retrieval(result)
        from agents.alert_window_merge import collect_window_proofs, merge_window_observations
        try:
            result['window_evidence'] = await asyncio.wait_for(run_sync_in_daemon_thread(
                collect_window_proofs, result, cfg.observability.prometheus_url,
                cfg.observability.jaeger_url, observed), 30)
            if merge_window_observations(result, result['window_evidence']):
                await enrich_retrieval(result)
        except Exception as exc:
            result['window_evidence'] = {'status':'unavailable','error':type(exc).__name__,'proofs':[]}
    from agents.alert_context import recall_context
    await asyncio.to_thread(recall_context, result)
    if llm is not None:
        await propose_relations(result, llm, timeout_s=max(1, min(180, float(cfg.alert.semantic_timeout_s))))
    if build_graph:
        await asyncio.to_thread(build_scag, result, topology)
    return topology


_alert_job_worker = None


def _alert_jobs():
    from agents.alert_jobs import AlertJobs
    return AlertJobs(ROOT_DIR / 'data' / 'alert_compression' / 'jobs', output=_compression_store())


async def _prepare_alert_job(payload):
    if payload.get('source') == 'live_scan':
        return await _prepare_live_alert_scan(payload.get('namespace', ''), payload['cluster_id'], for_job=True)
    cluster = payload['cluster_id']
    cfg = _cluster_scoped_config(cluster)
    result = await asyncio.to_thread(compress_alerts, payload['alerts'], cluster_id=cluster,
        namespace=payload.get('namespace', ''), time_window=payload.get('time_window', cfg.alert.time_window),
        baseline=payload.get('baseline'))
    await _enrich_alert_groups(result, cfg, topology=payload.get('topology'), build_graph=False)
    result['cluster_id'] = cluster
    if payload.get('topology') is not None:
        result['job_topology'] = dict(payload['topology'], provenance='client_supplied')
    return result


async def _finalize_alert_job(payload, result):
    if payload.get('source') != 'live_scan':
        return
    from agents.alert_lifecycle import AlertLifecycle
    from agents.alert_history import AlertHistory
    lifecycle = AlertLifecycle(ROOT_DIR / 'data' / 'alert_compression' / 'lifecycle.sqlite3')
    await asyncio.to_thread(lifecycle.observe, result)
    history = AlertHistory(ROOT_DIR / 'data' / 'alert_compression' / 'history.sqlite3')
    await asyncio.to_thread(history.record, result, payload.get('namespace', ''),
                            now=result['collection_started_at'])


def _alert_job_model(payload):
    cfg = _cluster_scoped_config(payload['cluster_id'])
    return (LLMClient(cfg.llm) if _llm_configured(cfg.llm) else None,
            max(1, min(180, float(cfg.alert.semantic_timeout_s))))


async def _ensure_alert_job_worker():
    global _alert_job_worker
    if _alert_job_worker is None or _alert_job_worker.done():
        from agents.alert_review_context import ReviewContextGuard
        store = await asyncio.to_thread(_alert_jobs)
        _alert_job_worker = asyncio.create_task(store.run(_prepare_alert_job, _alert_job_model,
            _compression_store(), _finalize_alert_job,
            review_guard=ReviewContextGuard(ROOT_DIR / 'data' / 'alert_compression' / 'lifecycle.sqlite3')))


@app.on_event('shutdown')
async def _stop_alert_job_worker():
    global _alert_job_worker
    if _alert_job_worker is not None:
        _alert_job_worker.cancel()
        await asyncio.gather(_alert_job_worker, return_exceptions=True)
        _alert_job_worker = None


@app.post('/api/alerts/compression-jobs', status_code=202)
async def create_alert_compression_job(request: Request):
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, 'request must be an object')
    cluster = _resolve_general_cluster_id(str(payload.get('cluster_id') or ''))
    cfg = _cluster_scoped_config(cluster)
    # Caller-supplied alerts must never impersonate a live observation.
    payload = dict(payload, cluster_id=cluster, namespace=str(payload.get('namespace') or ''), source='import')
    try:
        # Validate before acknowledging the durable job; no model calls here.
        await asyncio.to_thread(compress_alerts, payload.get('alerts'), cluster_id=cluster,
            namespace=payload['namespace'], time_window=payload.get('time_window', cfg.alert.time_window), baseline=payload.get('baseline'))
        if payload.get('topology') is not None:
            topology = payload['topology']
            if not isinstance(topology, dict) or not isinstance(topology.get('edges', []), list):
                raise ValueError('invalid topology')
            if len(topology.get('edges', [])) > 10000 or any(not isinstance(e, dict) or not isinstance(e.get('source'), str) or not isinstance(e.get('target'), str) for e in topology.get('edges', [])):
                raise ValueError('invalid topology edges')
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    store = await asyncio.to_thread(_alert_jobs)
    try:
        job = await asyncio.to_thread(store.create, payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await _ensure_alert_job_worker()
    return JSONResponse(job, status_code=202)


@app.post('/api/alerts/scan-jobs', status_code=202)
async def create_alert_scan_job(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, 'request must be an object')
    cluster = _resolve_general_cluster_id(str(body.get('cluster_id') or ''))
    cfg = _cluster_scoped_config(cluster)
    payload = {'source': 'live_scan', 'cluster_id': cluster,
               'namespace': str(body.get('namespace') or ''), 'semantic': _llm_configured(cfg.llm)}
    if 'review_budget' in body:
        from agents.alert_review import limits
        try:
            limits(body['review_budget'])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        payload['review_budget'] = body['review_budget']
    store = await asyncio.to_thread(_alert_jobs)
    try:
        job = await asyncio.to_thread(store.create, payload, key=body.get('job_id'))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    await _ensure_alert_job_worker()
    return JSONResponse(job, status_code=202)


@app.get('/api/alerts/compression-jobs/{job_id}')
async def get_alert_compression_job(job_id: str):
    try:
        store = await asyncio.to_thread(_alert_jobs)
        return await asyncio.to_thread(store.get, job_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, 'job not found') from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post('/api/alerts/compression-jobs/{job_id}/{action}')
async def control_alert_compression_job(job_id: str, action: str):
    try:
        store = await asyncio.to_thread(_alert_jobs)
        job = await asyncio.to_thread(store.control, job_id, action)
        if action == 'resume':
            await _ensure_alert_job_worker()
        return job
    except FileNotFoundError as exc:
        raise HTTPException(404, 'job not found') from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get('/api/alerts/compression/{scan_id}/summary')
async def get_alert_compression_summary(scan_id: str):
    from agents.alert_jobs import scan_summary
    try:
        result = await asyncio.to_thread(_compression_store().get, scan_id)
        return await asyncio.to_thread(scan_summary, result)
    except FileNotFoundError as exc:
        raise HTTPException(404, 'compression record not found') from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get('/api/alerts/compression/{scan_id}/page')
async def get_alert_compression_page(scan_id: str, section: str = 'groups', offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
    from agents.alert_jobs import scan_page
    try:
        result = await asyncio.to_thread(_compression_store().get, scan_id)
        page = await asyncio.to_thread(scan_page, result, section, offset, limit)
        return await asyncio.to_thread(JSONResponse, page)
    except FileNotFoundError as exc:
        raise HTTPException(404, 'compression record not found') from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get('/api/alerts/compression/{scan_id}/groups/{group_id}/evidence')
async def get_alert_group_evidence(scan_id: str, group_id: str, revision: int = Query(..., ge=1),
                                   offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    try:
        page = await asyncio.to_thread(_compression_store().evidence_page, scan_id, group_id, revision,
                                       offset=offset, limit=limit)
        return await asyncio.to_thread(JSONResponse, page)
    except FileNotFoundError as exc:
        raise HTTPException(404, 'compression record not found') from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/alerts/compress")
async def compress_alert_batch(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "request must be an object")
    cluster_id = _resolve_general_cluster_id(str(body.get("cluster_id") or ""))
    cfg = _cluster_scoped_config(cluster_id)
    try:
        result = compress_alerts(body.get("alerts"), cluster_id=cluster_id,
            namespace=str(body.get("namespace") or ""), time_window=body.get("time_window", cfg.alert.time_window), baseline=body.get("baseline"))
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    llm = LLMClient(cfg.llm) if body.get("semantic") is True and _llm_configured(cfg.llm) else None
    await _enrich_alert_groups(result, cfg, llm=llm, topology=body.get("topology"))
    result["cluster_id"] = cluster_id
    stored = await asyncio.to_thread(_compression_store().create, result)
    return await asyncio.to_thread(JSONResponse, stored)


@app.get("/api/alerts/compression/{scan_id}")
async def get_alert_compression(scan_id: str):
    try:
        stored = await asyncio.to_thread(_compression_store().get, scan_id)
        return await asyncio.to_thread(JSONResponse, stored)
    except FileNotFoundError as exc:
        raise HTTPException(404, "compression record not found") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/alerts/compression/{scan_id}/edit")
async def edit_alert_compression(scan_id: str, request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "request must be an object")
    try:
        return _compression_store().edit(scan_id, revision=body.get("revision"), action=body.get("action"),
            group_ids=body.get("group_ids"), alert_ids=body.get("alert_ids"), reason=body.get("reason", ""))
    except FileNotFoundError as exc:
        raise HTTPException(404, "compression record not found") from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(409, str(exc)) from exc


# ─────────────────────────────────────────
# RCA APIs
# ─────────────────────────────────────────

def _resolve_rca_cluster_scope(
    body: Dict[str, Any],
    skill_workflow: Optional[Dict[str, Any]],
) -> tuple[str, Optional[Dict[str, Any]]]:
    workflow = skill_workflow if isinstance(skill_workflow, dict) else {}
    payload = workflow.get("payload") if isinstance(workflow.get("payload"), dict) else {}
    source = str(payload.get("source") or "").strip().lower()
    cluster_id = str(payload.get("cluster_id") or body.get("cluster_id") or "").strip()
    if source == "alert_center":
        cluster_id = _resolve_general_cluster_id(cluster_id)
    cluster = _find_k8s_cluster(cluster_id) if cluster_id else None
    if cluster_id and not cluster:
        raise HTTPException(400, f"cluster profile not found: {cluster_id}")
    cluster_kind = _cluster_kind(cluster) if cluster else ""
    if source == "alert_center" and cluster_kind != "general":
        raise HTTPException(409, "general alert Agent+Skill requests must target a general-compute cluster")
    if source == "ai_event_center" and cluster_kind != "ai":
        raise HTTPException(409, "AI event Agent+Skill requests must target an AI cluster")
    if payload is not None and cluster_id:
        payload["cluster_id"] = cluster_id
        payload["cluster_kind"] = cluster_kind
        workflow["payload"] = payload
    return cluster_id, cluster


def _resolve_agent_skill_llm_timeout(configured_timeout: Any, requested_timeout: Any = None) -> float:
    """Resolve model timeout independently from remediation dry-run mode."""
    try:
        configured = float(configured_timeout or 90)
    except (TypeError, ValueError):
        configured = 90.0
    if requested_timeout is None:
        return min(max(configured, 1.0), 300.0)
    try:
        requested = float(requested_timeout)
    except (TypeError, ValueError):
        requested = 60.0
    return min(max(requested, 1.0), max(configured, 1.0), 300.0)

@app.post("/api/rca/run")
async def rca_run(request: Request):
    """Trigger an RCA pipeline."""
    body = await request.json()
    group_ref = body.get("alert_group")
    if group_ref is not None:
        if not isinstance(group_ref, dict) or body.get("skill_workflow"):
            raise HTTPException(400, "event groups use the multi-agent diagnosis entry")
        try:
            dossier = await asyncio.to_thread(_compression_store().dossier, group_ref.get("scan_id"),
                group_ref.get("group_id"), group_ref.get("revision"), for_diagnosis=True)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(409, str(exc)) from exc
        if any(body.get(key) and body[key] != dossier[key] for key in ("cluster_id", "namespace")):
            raise HTTPException(409, "RCA scope differs from the stored event group")
        body["cluster_id"], body["namespace"] = dossier["cluster_id"], dossier["namespace"]
        event_name = dossier['alert_samples'][0]['name'] if dossier['alert_samples'] else '原始告警超出读取预算'
        body["query"] = f"请调查 {dossier['namespace']} 中 {dossier['entity_kind']}/{dossier['entity']} 的告警事件：{event_name}。根据证据包及实时只读观测验证根因。"
        body["initial_evidence"] = {"payload": dossier}
    query = body.get("query", "")
    namespace = body.get("namespace", "")
    if namespace and not _is_safe_k8s_name(namespace):
        raise HTTPException(400, "Invalid Kubernetes namespace")
    skill_workflow = body.get("skill_workflow") if isinstance(body.get("skill_workflow"), dict) else None
    cluster_id, cluster = _resolve_rca_cluster_scope(body, skill_workflow)
    analysis_phases = _normalize_ai_analysis_phases(
        (skill_workflow or {}).get("analysis_phases") if skill_workflow else body.get("analysis_phases")
    )

    if not query:
        raise HTTPException(400, "Missing 'query' field")

    cfg = _get_config()
    guarded_auto_recovery = getattr(cfg.remediation, "auto_loop_enabled", False) is True
    if skill_workflow and guarded_auto_recovery:
        skill_workflow = copy.deepcopy(skill_workflow)
        skill_workflow.update(dry_run=True, stages=["detect", "diagnose"])
    if not _llm_configured(cfg.llm):
        raise HTTPException(
            503,
            "LLM API Key 未配置。多智能体和 Agent+Skill 都必须由大模型参与推理；请在 .env 设置 LLM_API_KEY 或在 config_cluster.yaml 配置 llm.api_key 后重启服务。"
        )

    dedup_key = str(body.get("dedup_key") or "").strip()
    with _RCA_RUN_DEDUP_LOCK:
        existing_run_id = _state["rca_dedup"].get(dedup_key) if dedup_key else None
        existing_run = _state["rca_runs"].get(existing_run_id) if existing_run_id else None
        if existing_run and existing_run.get("status") in {"queued", "starting", "running"}:
            return {"run_id": existing_run_id, "status": existing_run.get("status"), "reused": True}

        run_id = f"rca-{uuid.uuid4().hex[:8]}"
        _state["rca_runs"][run_id] = {
            "id": run_id,
            "query": query,
            "namespace": namespace,
            "cluster_id": cluster_id,
            "cluster_kind": _cluster_kind(cluster) if cluster else "",
            "guarded_auto_recovery": guarded_auto_recovery,
            "dedup_key": dedup_key,
            "status": "running",
            "logs": [],
            "events": [],
            "result": None,
            "started_at": time.time(),
        }
        if dedup_key:
            _state["rca_dedup"][dedup_key] = run_id
        if not _save_rca_history():
            _state["rca_runs"].pop(run_id, None)
            if dedup_key:
                _state["rca_dedup"].pop(dedup_key, None)
            raise HTTPException(503, "无法保存诊断任务，请检查数据目录后重试。")

    def log_cb(msg):
        if isinstance(msg, dict):
            _state["rca_runs"][run_id]["events"].append(msg)
        else:
            _state["rca_runs"][run_id]["logs"].append(msg)
        if not _save_rca_history():
            _state["rca_runs"][run_id]["persistence_error"] = True

    def _run_agent_skill_sync():
        """Run the Agent+Skill branch through a real host-side SkillAgent."""
        try:
            workflow_payload = (
                skill_workflow.get("payload")
                if isinstance(skill_workflow, dict) and isinstance(skill_workflow.get("payload"), dict)
                else {}
            )
            if workflow_payload.get("source") == "ai_event_center":
                live_observation = _ai_event_live_observation(workflow_payload)
                if live_observation:
                    workflow_payload["live_observation"] = live_observation
                    workflow_payload.setdefault("observation", {}).update(
                        {
                            key: value
                            for key, value in live_observation.items()
                            if value not in (None, "", [], {})
                        }
                    )
            agent_cfg = _cluster_scoped_config(cluster_id)
            if guarded_auto_recovery:
                agent_cfg = copy.deepcopy(agent_cfg)
                agent_cfg.runtime.enable_self_healing = False
            registry = build_tool_registry(agent_cfg, isolated=True)
            configured_llm_timeout = float(getattr(cfg.llm, "timeout", 90) or 90)
            requested_llm_timeout = (skill_workflow or {}).get("llm_timeout_s")
            agent_llm_timeout_s = _resolve_agent_skill_llm_timeout(
                configured_llm_timeout,
                requested_llm_timeout,
            )
            llm_cfg = agent_cfg.llm
            if configured_llm_timeout != agent_llm_timeout_s:
                llm_cfg = copy.copy(agent_cfg.llm)
                llm_cfg.timeout = agent_llm_timeout_s
            llm_client = LLMClient(llm_cfg)
            agent = SkillAgent(
                registry=registry,
                skills_dir=SKILL_RUNTIME_SKILLS_DIR,
                runtime_url=SKILL_RUNTIME_URL,
                event_sink=log_cb,
                kubectl_runner=_agent_skill_kubectl_runner,
                context_provider=_agent_skill_context_provider,
                llm_client=llm_client,
                llm_timeout_s=agent_llm_timeout_s,
            )
            result = agent.run(SkillAgentRequest(
                query=query,
                namespace=namespace,
                skill_workflow=skill_workflow or {},
                run_id=run_id,
            ))
            result_dict = result.to_dict()
            result_dict["session_id"] = run_id
            result_dict["cluster_id"] = cluster_id
            result_dict["cluster_kind"] = _cluster_kind(cluster) if cluster else ""
            diagnosis = result_dict.get("diagnosis") if isinstance(result_dict.get("diagnosis"), dict) else {}
            if result_dict.get("root_cause") and not diagnosis.get("root_cause"):
                diagnosis["root_cause"] = result_dict.get("root_cause")
                diagnosis["confidence"] = result_dict.get("confidence", diagnosis.get("confidence"))
                diagnosis["fault_type"] = result_dict.get("fault_type") or diagnosis.get("fault_type")
                diagnosis["skill_id"] = (
                    ((result_dict.get("skill_runtime_result") or {}).get("skill_runtime") or {}).get("primary_skill_id")
                    if isinstance(result_dict.get("skill_runtime_result"), dict)
                    else diagnosis.get("skill_id")
                )
                result_dict["diagnosis"] = diagnosis
            _state["rca_runs"][run_id]["partial_result"] = {
                "status": "agent_skill_completed" if result.skill_runtime_result else "agent_skill_failed",
                "source": "skill_agent",
                "updated_at": time.time(),
                "skill_runtime_result": result.skill_runtime_result,
                "agent_skill_bridge": result.agent_skill_bridge,
            }
            _state["rca_runs"][run_id]["result"] = result_dict
            _state["rca_runs"][run_id]["status"] = result.status
            _finalize_ai_event_agent_skill_run(
                run_id=run_id,
                skill_workflow=skill_workflow,
                result_dict=result_dict,
                status=result.status,
            )
        except Exception as e:
            logger.error(f"Agent+Skill RCA error: {e}", exc_info=True)
            _state["rca_runs"][run_id]["status"] = "failed"
            _state["rca_runs"][run_id]["result"] = {"status": "failed", "error": str(e)}
            _finalize_ai_event_agent_skill_run(
                run_id=run_id,
                skill_workflow=skill_workflow,
                result_dict={"status": "failed", "error": str(e)},
                status="failed",
            )
        finally:
            saved = _save_rca_history(required_run_id=run_id)
            if saved and guarded_auto_recovery:
                _schedule_guarded_auto_recovery(run_id)

    def _run_sync():
        """Run pipeline in a thread to avoid blocking the event loop."""
        try:
            if guarded_auto_recovery:
                pipeline_cfg = copy.deepcopy(_cluster_scoped_config(cluster_id) if cluster_id else cfg)
                # This mode must use the guarded object action path after the
                # diagnosis is durably stored, never the legacy direct writer.
                pipeline_cfg.pipeline.enable_recovery = False
                pipeline_cfg.runtime.enable_self_healing = False
                pipeline = Pipeline(pipeline_cfg)
            else:
                pipeline = Pipeline(_cluster_scoped_config(cluster_id)) if cluster_id else _get_pipeline()
            initial_evidence = body.get("initial_evidence") if isinstance(body.get("initial_evidence"), dict) else None
            if skill_workflow:
                payload = skill_workflow.get("payload") if isinstance(skill_workflow.get("payload"), dict) else {}
                initial_evidence = {
                    "payload": payload,
                    "skill_workflow": skill_workflow,
                    "source": "alert_agent_skill_request",
                }
            result = asyncio.run(
                pipeline.run(
                    query,
                    namespace,
                    log_cb,
                    initial_evidence=initial_evidence,
                    skill_workflow=skill_workflow,
                    analysis_phases=analysis_phases,
                )
            )
            result_dict = result.to_dict()
            skill_event = next(
                (
                    evt for evt in reversed(_state["rca_runs"][run_id].get("events", []))
                    if isinstance(evt, dict) and evt.get("event") == "skill_runtime" and evt.get("result")
                ),
                None,
            )
            if skill_event:
                result_dict["skill_runtime_result"] = skill_event.get("result")
                result_dict["agent_skill_bridge"] = {
                    "mode": "agent_pipeline_tool_call",
                    "tool": skill_event.get("tool") or "agenticsre_skill_run",
                }
            _state["rca_runs"][run_id]["result"] = result_dict
            _state["rca_runs"][run_id]["status"] = result.status
        except Exception as e:
            logger.error(f"RCA pipeline error: {e}", exc_info=True)
            _state["rca_runs"][run_id]["status"] = "failed"
            result = {"error": str(e)}
            _state["rca_runs"][run_id]["result"] = result
        finally:
            saved = _save_rca_history(required_run_id=run_id)
            if saved and guarded_auto_recovery:
                _schedule_guarded_auto_recovery(run_id)

    # Run in background thread (pipeline does sync LLM calls that block the loop)
    import threading
    target = _run_agent_skill_sync if skill_workflow else _run_sync
    t = threading.Thread(target=target, daemon=True, name=f"rca-{run_id}")
    t.start()
    return {"run_id": run_id}


@app.get("/api/rca/history")
async def rca_history(response: Response, limit: int = Query(20, ge=1, le=100),
                      q: str = Query("", max_length=200), cursor: str = Query("", max_length=2048)):
    """Read retained summaries using a stable (time, id) boundary, never offsets."""
    query = q.strip().casefold()
    before = None
    if cursor:
        try:
            payload = json.loads(base64.b64decode(cursor + '=' * (-len(cursor) % 4),
                                                 altchars=b'-_', validate=True))
            boundary = payload['before']
            if (type(payload['v']) is not int or payload['v'] != 1 or payload['q'] != query
                    or not isinstance(boundary, list) or len(boundary) != 2
                    or type(boundary[0]) not in (int, float) or not math.isfinite(boundary[0])
                    or boundary[0] < 0 or not isinstance(boundary[1], str)
                    or not boundary[1] or len(boundary[1]) > 200):
                raise ValueError('invalid history boundary')
            before = tuple(boundary)
        except (ValueError, TypeError, KeyError, IndexError, OverflowError):
            raise HTTPException(400, "历史翻页标记无效或搜索条件已改变，请刷新列表。")

    def text(value):
        return value if isinstance(value, str) else ''

    rows = []
    # Background diagnosis threads can add runs. Snapshot the values before iterating.
    for run in list(_state['rca_runs'].values()):
        if not isinstance(run, dict) or not text(run.get('id')):
            continue
        stamp = run.get('started_at')
        if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp < 0:
            stamp = 0
        result = run.get('result') if isinstance(run.get('result'), dict) else {}
        rows.append({'id': run['id'], 'query': text(run.get('query')),
                     'status': text(run.get('status')) or 'unknown', 'started_at': stamp,
                     'namespace': text(run.get('namespace')), 'cluster_id': text(run.get('cluster_id')),
                     'duration_s': result.get('duration_s', 0),
                     'session_id': text(result.get('session_id'))})
    retained_total = len(rows)
    rows = [row for row in rows if not query or any(query in row[field].casefold()
            for field in ('id', 'query', 'namespace', 'cluster_id'))]
    rows.sort(key=lambda row: (row['started_at'], row['id']), reverse=True)
    total = len(rows)
    if before is not None:
        rows = [row for row in rows if (row['started_at'], row['id']) < before]
    page = rows[:limit]
    has_more = len(rows) > limit
    next_cursor = None
    if has_more:
        last = page[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps({
            'v': 1, 'q': query, 'before': [last['started_at'], last['id']],
        }, ensure_ascii=False).encode()).decode().rstrip('=')
    response.headers['Cache-Control'] = 'private, no-store'
    return {'runs': page, 'total': total, 'retained_total': retained_total,
            'limit': limit, 'query': q.strip(), 'has_more': has_more, 'next_cursor': next_cursor}


@app.get("/api/rca/{run_id}")
async def rca_status(run_id: str):
    run = _state["rca_runs"].get(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return _json_safe_snapshot(run)


@app.get("/api/rca/{run_id}/stream")
async def rca_stream(run_id: str):
    """SSE stream for RCA execution logs."""
    run = _state["rca_runs"].get(run_id)
    if not run:
        raise HTTPException(404, "Run not found")

    async def event_gen():
        log_idx = 0
        evt_idx = 0
        while True:
            # Send new logs
            logs = run["logs"]
            if log_idx < len(logs):
                for msg in logs[log_idx:]:
                    yield f"data: {json.dumps({'type': 'log', 'msg': msg})}\n\n"
                log_idx = len(logs)

            # Send new structured events
            events = run.get("events", [])
            if evt_idx < len(events):
                for evt in events[evt_idx:]:
                    yield f"data: {json.dumps({'type': 'event', 'data': evt})}\n\n"
                evt_idx = len(events)

            if run["status"] in ("completed", "failed"):
                yield f"data: {json.dumps({'type': 'done', 'status': run['status'], 'result': run.get('result')})}\n\n"
                break

            yield f": heartbeat\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.get("/api/ontology/schema")
async def operational_ontology_schema():
    return ontology_schema()


@app.get("/api/rca/{run_id}/ontology")
async def rca_ontology(run_id: str):
    """A stable semantic view of recorded evidence; no new collection or LLM call."""
    run = _state["rca_runs"].get(run_id)
    if not run:
        raise HTTPException(404, "分析记录不存在")
    model = build_investigation(_json_safe_snapshot(run))
    return {key: value for key, value in model.items()
            if key not in {"observations", "relations", "objects", "tool_executions"}} | {
        "action_context": action_context(model),
        "history_eligible_object_ids": eligible_object_ids(model),
        "counts": {key: len(model[key]) for key in ("objects", "observations", "relations", "tool_executions")}}


_ONTOLOGY_HISTORY_READERS = threading.BoundedSemaphore(2)


@app.get("/api/rca/{run_id}/objects/{entity_id}/history")
def rca_object_history(run_id: str, entity_id: str,
                       revision: str = Query(..., min_length=1, max_length=128),
                       history_revision: str = Query('', max_length=128),
                       offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100)):
    """Read retained evidence in a worker; never reread the entire history file."""
    if not _ONTOLOGY_HISTORY_READERS.acquire(blocking=False):
        raise HTTPException(503, "对象历史读取繁忙，请稍后重试；已有记录未改变。")
    try:
        if _RCA_HISTORY_LOAD_FAILED:
            raise HTTPException(503, "历史存储未能完整载入，请先恢复存储后重试。")
        run = _state['rca_runs'].get(run_id)
        if not run:
            raise HTTPException(404, "分析记录不存在")
        deadline = time.monotonic() + 3
        check_source_budget(run, deadline=deadline)
        try:
            source = _json_safe_snapshot({k: run.get(k) for k in
                ('id', 'status', 'cluster_id', 'namespace', 'result', 'events')})
        except (RuntimeError, RecursionError) as exc:
            raise HistoryUnavailable('source_changed_during_snapshot') from exc
        # Retention is selected using the original run timestamps. The source
        # compact snapshot is substituted only after that selection.
        model = build_investigation(source)
        if revision != model['revision'] or model['run_id'] != run_id:
            raise HTTPException(409, "调查记录已更新，请重新打开记录后读取对象历史。")
        if not any(obj['id'] == entity_id for obj in model['objects']):
            raise HTTPException(404, "本次调查没有该对象。")
        try:
            records = list(_state['rca_runs'].values())
        except RuntimeError as exc:
            raise HistoryUnavailable('history_changed_during_read') from exc
        result = build_object_history(model, source, records, entity_id, deadline=deadline)
        if (offset and not history_revision) or history_revision and history_revision != result['history_revision']:
            raise HTTPException(409, "对象历史已变化，请刷新第一页；已有页面仅供旧版本参考。")
        items = result.pop('items')
        result.update(items=items[offset:offset + limit], total=len(items), offset=offset, limit=limit,
                      has_more=offset + limit < len(items))
        return JSONResponse(result, headers={'Cache-Control': 'private, no-store'})
    except HistoryBindingError as exc:
        raise HTTPException(409, str(exc)) from exc
    except HistoryUnavailable as exc:
        raise HTTPException(503, "对象历史超出读取预算，请稍后重试；不能据此认为没有历史记录。") from exc
    finally:
        _ONTOLOGY_HISTORY_READERS.release()


@app.get("/api/rca/{run_id}/strategy")
async def rca_diagnostic_strategy(run_id: str):
    run = _state["rca_runs"].get(run_id)
    if not run:
        raise HTTPException(404, "分析记录不存在")
    return strategy_view(_json_safe_snapshot(run))


@app.get("/api/rca/{run_id}/ontology/{collection}")
async def rca_ontology_collection(run_id: str, collection: str, entity_id: str = "",
                                  revision: str = "", record_id: str = "", offset: int = Query(0, ge=0),
                                  limit: int = Query(50, ge=1, le=100)):
    run = _state["rca_runs"].get(run_id)
    if not run:
        raise HTTPException(404, "分析记录不存在")
    model = build_investigation(_json_safe_snapshot(run))
    if revision and revision != model["revision"]:
        raise HTTPException(409, "调查记录已更新，请刷新后读取同一版本。")
    try:
        return query_investigation(model, collection, entity_id=entity_id, record_id=record_id, offset=offset, limit=limit)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


_LINEAGE_COLLECTION_LOCK = threading.Lock()


@app.get("/api/rca/{run_id}/lineage")
async def rca_lineage(run_id: str):
    run = _state["rca_runs"].get(run_id)
    if not run:
        raise HTTPException(404, "分析记录不存在")
    return {"run_id": run_id, "snapshots": _json_safe_snapshot(run.get("lineage_snapshots", []))}


def _collect_recorded_lineage(run_id: str, body: Dict[str, Any]):
    # Lock stays in the worker when an HTTP request is cancelled. Every snapshot
    # is appended once; concurrent requests cannot overwrite the history.
    if not _LINEAGE_COLLECTION_LOCK.acquire(blocking=False):
        raise HTTPException(409, "已有追溯采集正在运行，请稍后读取记录。")
    try:
        run = _state["rca_runs"].get(run_id)
        if not run:
            raise HTTPException(404, "分析记录不存在")
        if run.get("status") not in {"completed", "failed"}:
            raise HTTPException(409, "请等待诊断结束后采集补充追溯。")
        model = build_investigation(_json_safe_snapshot(run))
        if not body.get("revision") or body["revision"] != model["revision"]:
            raise HTTPException(409, "调查版本已变化，请重新打开记录。")
        target = next((o for o in model["objects"] if o["id"] == body.get("entity_id")), None)
        if (not traceable_object(target) or target["cluster_id"] != model["scope"]["cluster_id"]
                or model["scope"]["namespace"] and target["namespace"] != model["scope"]["namespace"]):
            raise HTTPException(409, "追溯需要本次范围内具有真实 UID 的工作负载对象。")
        snapshots = run.setdefault("lineage_snapshots", [])
        if len(snapshots) >= 20:
            raise HTTPException(409, "本次调查已保存 20 次追溯，请使用已有记录或发起新的诊断。")
        snapshot = collect_lineage(target, lambda obj: _ontology_live_resource(obj, timeout=10),
                                   source_run_id=run_id, source_revision=model["revision"])
        snapshots.append(snapshot)
        if not _save_rca_history(required_run_id=run_id):
            snapshots.remove(snapshot)
            raise HTTPException(503, "追溯结果未能持久化，请重试；诊断结论未改变。")
        return _json_safe_snapshot(snapshot)
    finally:
        _LINEAGE_COLLECTION_LOCK.release()


@app.post("/api/rca/{run_id}/lineage")
async def rca_collect_lineage(run_id: str, request: Request):
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(422, "请求必须是合法 JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(422, "请求必须包含对象及调查版本")
    return await asyncio.to_thread(_collect_recorded_lineage, run_id, body)


@app.get("/api/rca/{run_id}/lineage/compare")
async def rca_compare_lineage(run_id: str, before_id: str = "", after_id: str = ""):
    run = _state["rca_runs"].get(run_id)
    if not run:
        raise HTTPException(404, "分析记录不存在")
    snapshots = _json_safe_snapshot(run.get("lineage_snapshots", []))
    before = next((s for s in snapshots if s.get("id") == before_id), None)
    after = next((s for s in snapshots if s.get("id") == after_id), None)
    if before is None or after is None:
        raise HTTPException(404, "追溯快照不存在")
    try:
        return compare_snapshots(before, after)
    except ValueError as exc:
        raise HTTPException(409, {"message": "快照对象或时间范围不满足比较条件", "reason": str(exc)}) from exc


# ─────────────────────────────────────────
# Remediation APIs
# ─────────────────────────────────────────

_RCA_REMEDIATION_LOCK = threading.Lock()


def _run_recorded_rca_remediation(run_id: str, *, rollback: bool = False):
    # Keep the lock in the worker: HTTP cancellation must not release it while
    # a write is still running. This serializes this single-process JSON store.
    if not _RCA_REMEDIATION_LOCK.acquire(blocking=False):
        raise HTTPException(409, "A remediation operation is already running")
    try:
        run = _state["rca_runs"].get(run_id)
        if not run:
            raise HTTPException(404, "Run not found")
        if rollback:
            execution = run.get("remediation_result")
            if not execution or not execution.get("actions"):
                raise HTTPException(409, "No durable remediation actions to roll back")
        else:
            if run.get("remediation_result") is not None:
                raise HTTPException(409, "Remediation already attempted; inspect recorded outcome before proceeding")
            result = run.get("result") or {}
            inner = result.get("result", result)
            rem_data = (inner.get("evidence") or {}).get("remediation") if isinstance(inner, dict) else None
            if not rem_data:
                rem_data = next((event.get("data") for event in reversed(run.get("events", []))
                                 if event.get("event") == "remediation"), None)
            if not rem_data or rem_data.get("status") != "pending_approval":
                raise HTTPException(400, "No pending remediation plan found")
            plan = copy.deepcopy(rem_data.get("plan") or {})
            if not plan.get("actions"):
                raise HTTPException(400, "Remediation plan has no actions")

        from tools import build_tool_registry
        from agents import RemediationAgent
        cluster_id = run.get("cluster_id")
        cfg = _cluster_scoped_config(cluster_id) if cluster_id else _get_config()
        registry = build_tool_registry(cfg, allow_write=True, isolated=True)
        # Execution/rollback never calls a model to regenerate approved actions.
        agent = RemediationAgent(None, registry, cfg)
        agent.namespace = run.get("namespace") or ""

        def checkpoint(record):
            run["remediation_result"] = copy.deepcopy(record)
            if not _save_rca_history():
                run["persistence_error"] = True
                raise HTTPException(503, "Cannot checkpoint remediation; stop and reconcile recorded actions")

        if rollback:
            response = agent.rollback(copy.deepcopy(execution), checkpoint=checkpoint)
            run["remediation_rollback"] = copy.deepcopy(response)
            event = "remediation_rollback"
        else:
            response = agent.execute_plan(plan, checkpoint=checkpoint)
            checkpoint(response)
            event = "remediation_executed"
        run.setdefault("events", []).append({"event": event, "data": copy.deepcopy(response)})
        if not _save_rca_history():
            run["persistence_error"] = True
            raise HTTPException(503, "Cannot save remediation result; inspect recorded actions")
        return response
    finally:
        _RCA_REMEDIATION_LOCK.release()


@app.post("/api/rca/{run_id}/remediation/approve")
async def rca_remediation_approve(run_id: str):
    """Execute the stored approved plan once, with durable action checkpoints."""
    return await asyncio.to_thread(_run_recorded_rca_remediation, run_id)


@app.post("/api/rca/{run_id}/remediation/rollback")
async def rca_remediation_rollback(run_id: str):
    """Undo durable successful actions; never replay an unknown write outcome."""
    return await asyncio.to_thread(_run_recorded_rca_remediation, run_id, rollback=True)


@app.get("/api/rca/{run_id}/remediation")
async def rca_remediation_status(run_id: str):
    """Get remediation status for a run."""
    run = _state["rca_runs"].get(run_id)
    if not run:
        raise HTTPException(404, "Run not found")

    # Find remediation event
    rem_data = None
    for evt in reversed(run.get("events", [])):
        if evt.get("event") in ("remediation", "remediation_executed", "remediation_rollback"):
            rem_data = evt.get("data")
            break

    return {
        "run_id": run_id,
        "remediation": rem_data,
        "execution": run.get("remediation_result"),
        "rollback": run.get("remediation_rollback"),
    }


@app.get("/api/heal/recipes")
async def heal_recipes():
    """Return deterministic self-healing knowledge-base recipes."""
    library = _load_heal_recipe_library()
    recipes = [_public_heal_recipe(r) for r in library.get("recipes", [])]
    validation = _validate_heal_recipe_library(library)
    categories = sorted({r.get("category", "unknown") for r in recipes})
    fault_types = sorted({r.get("fault_type", "unknown") for r in recipes})
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for recipe in recipes:
        grouped.setdefault(str(recipe.get("category") or "unknown"), []).append(recipe)
    return {
        "recipes": recipes,
        "grouped": grouped,
        "version": library.get("version"),
        "source": library.get("source"),
        "description": library.get("description"),
        "validation": {
            "ok": validation.get("ok"),
            "error_count": validation.get("error_count"),
            "warning_count": validation.get("warning_count"),
            "issues": validation.get("issues", [])[:20],
        },
        "total": len(recipes),
        "categories": categories,
        "fault_types": fault_types,
        "revision": _heal_library_revision(library),
    }


@app.put("/api/heal/recipes/{fault_type}")
def update_heal_recipe(fault_type: str, body: Dict[str, Any]):
    return _update_heal_recipe(fault_type, body)


def _heal_history_id(entry: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(entry, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _heal_draft_hash(draft: Dict[str, Any]) -> str:
    return _heal_history_id({k: draft.get(k) for k in ("id", "fault_type", "base_revision", "recipe", "restored_from")})


def _write_heal_state(state: Dict[str, Any]) -> None:
    path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=HEAL_RECIPES_STATE.parent, delete=False) as tmp:
            path = tmp.name
            json.dump(state, tmp, ensure_ascii=False, allow_nan=False)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(path, HEAL_RECIPES_STATE)
    finally:
        if path and os.path.exists(path):
            os.unlink(path)


@app.get("/api/heal/recipe-drafts")
def heal_recipe_drafts():
    library = _load_heal_recipe_library()
    state = json.loads(HEAL_RECIPES_STATE.read_text()) if HEAL_RECIPES_STATE.exists() else {}
    return {"drafts": list(reversed(state.get("drafts", []))), "revision": _heal_library_revision(library)}


@app.post("/api/heal/recipe-drafts/{draft_id}/decision")
def decide_heal_recipe_draft(draft_id: str, body: Dict[str, Any], request: Request):
    body = dict(body)
    session = _get_auth_session(request)
    if _auth_enabled() and session:
        body["reviewer"] = session["username"]
        body["identity_source"] = "authenticated_session"
    elif _auth_enabled() and _bearer_token_valid(request):
        body["reviewer"] = "api-token"
        body["identity_source"] = "api_token"
    else:
        body["identity_source"] = "operator_label"
    decision, reviewer = body.get("decision"), body.get("reviewer")
    if decision not in ("approve", "reject") or not isinstance(reviewer, str) or not 1 <= len(reviewer.strip()) <= 100:
        raise HTTPException(422, "请选择通过或驳回，并填写审核人记录")
    if not isinstance(body.get("comment", ""), str) or len(body.get("comment", "")) > 2000:
        raise HTTPException(422, "审核意见不能超过 2000 字符")
    HEAL_RECIPES_STATE.parent.mkdir(parents=True, exist_ok=True)
    if decision == "approve":
        draft = next((d for d in heal_recipe_drafts()["drafts"] if d["id"] == draft_id), None)
        if draft is None:
            raise HTTPException(404, "草稿不存在")
        patch = {k: draft["recipe"][k] for k in ("description", "category", "risk", "signals", "actions") if k in draft["recipe"]}
        return _update_heal_recipe(draft["fault_type"], {"revision": body.get("revision"), "recipe": patch},
                                  restore=draft.get("restored_from"), approval={**body, "id": draft_id})
    with HEAL_RECIPES_STATE.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(HEAL_RECIPES_STATE.read_text()) if HEAL_RECIPES_STATE.exists() else {}
        draft = next((d for d in state.get("drafts", []) if d["id"] == draft_id), None)
        if draft is None:
            raise HTTPException(404, "草稿不存在")
        if draft["status"] != "pending" or body.get("content_hash") != _heal_draft_hash(draft):
            raise HTTPException(409, "草稿状态或内容已变化，请刷新审核列表")
        draft.update(status="rejected", reviewer=reviewer.strip(), identity_source=body["identity_source"], comment=body.get("comment", ""), decided_at=time.time())
        _write_heal_state(state)
    return {"status": "ok", "draft": draft}


@app.get("/api/heal/recipes/{fault_type}/history")
def heal_recipe_history(fault_type: str):
    if HEAL_RECIPES_STATE.exists():
        try:
            state = json.loads(HEAL_RECIPES_STATE.read_text(encoding="utf-8"))
            library, history = state["library"], state["history"]
        except Exception as exc:
            raise HTTPException(503, "知识库历史读取失败") from exc
    else:
        library, history = _load_heal_recipe_library(), []
    if not any(r.get("fault_type") == fault_type for r in library["recipes"]):
        raise HTTPException(404, "策略不存在")
    return {"revision": _heal_library_revision(library), "fault_type": fault_type,
            "history": [{**entry, "entry_id": _heal_history_id(entry)} for entry in reversed(history)
                        if entry.get("fault_type") == fault_type], "retention_limit": 100}


@app.post("/api/heal/recipes/{fault_type}/restore")
def restore_heal_recipe(fault_type: str, body: Dict[str, Any]):
    view = heal_recipe_history(fault_type)
    if body.get("revision") != view["revision"]:
        raise HTTPException(409, "知识库版本已变化，请刷新历史后重新选择")
    side = body.get("snapshot")
    if side not in ("before", "after"):
        raise HTTPException(422, "请选择修改前或修改后的版本")
    entry = next((e for e in view["history"] if e["entry_id"] == body.get("entry_id")), None)
    if entry is None:
        raise HTTPException(404, "历史版本不存在或已超出保留范围")
    snapshot = entry[side]
    fields = ("description", "category", "risk", "signals", "actions")
    patch = {k: snapshot[k] for k in fields if k in snapshot}
    # Restore content through the same validator/CAS path. Never restore approval
    # exemptions or old success labels, and append a new audit event.
    return _update_heal_recipe(fault_type, {"revision": body["revision"], "recipe": patch},
                               restore={"entry_id": entry["entry_id"], "snapshot": side})


def _update_heal_recipe(fault_type: str, body: Dict[str, Any], *, restore=None, approval=None):
    """Edit an existing recipe with compare-and-swap and durable audit history."""
    patch = body.get("recipe")
    fields = {"description", "category", "risk", "signals", "actions"}
    if not isinstance(patch, dict) or not patch or set(patch) - fields:
        raise HTTPException(422, "仅允许修改描述、分类、风险、信号和动作")
    for field in ("description", "category"):
        if field in patch and (not isinstance(patch[field], str) or not patch[field].strip() or len(patch[field]) > 4000):
            raise HTTPException(422, f"{field} 必须为非空文本且不超过 4000 字符")
    if "risk" in patch and patch["risk"] not in ("low", "medium", "high"):
        raise HTTPException(422, "风险必须为 low / medium / high")
    if "signals" in patch and (not isinstance(patch["signals"], list) or len(patch["signals"]) > 100 or any(not isinstance(s, str) or not s.strip() or len(s) > 1000 for s in patch["signals"])):
        raise HTTPException(422, "信号必须为非空字符串列表，最多 100 条")
    if "actions" in patch:
        actions = patch["actions"]
        if not isinstance(actions, list) or not 1 <= len(actions) <= 50:
            raise HTTPException(422, "动作数量必须为 1 至 50")
        for action in actions:
            if not isinstance(action, dict) or set(action) - {"step", "command", "rollback_command", "risk"}:
                raise HTTPException(422, "动作字段无效")
            if any(not isinstance(action.get(k, ""), str) or len(action.get(k, "")) > 8000 for k in ("step", "command", "rollback_command")) or not action.get("step", "").strip():
                raise HTTPException(422, "动作名称及命令必须为有效文本")
            if action.get("risk") not in ("low", "medium", "high"):
                raise HTTPException(422, "动作必须指定有效风险等级")
            for k in ("command", "rollback_command"):
                command = action.get(k, "")
                if not command and k == "rollback_command":
                    continue
                cleaned = re.sub(r"<[a-z_]+>", "sample", command)
                if re.search(r"[;|&`$\n\r<>]", cleaned):
                    raise HTTPException(422, "动作不允许 shell 拼接、重定向或命令替换")
                try:
                    args = shlex.split(cleaned)
                except ValueError:
                    raise HTTPException(422, "命令引号未闭合")
                if any(arg.startswith(("--kubeconfig", "--context", "--server", "--token", "--client-", "--certificate-", "--as")) or arg == "-s" for arg in args):
                    raise HTTPException(422, "集群连接和身份由系统配置管理")
                if len(args) < 2 or args[1] in {"exec", "run", "proxy", "port-forward", "config"}:
                    raise HTTPException(422, "不允许此类 kubectl 动作")
                if args[1] not in {"get", "describe", "logs", "top", "events", "version", "cluster-info", "explain"} and action["risk"] == "low":
                    raise HTTPException(422, "写操作不能标记为低风险")
                reason = _validate_heal_command(_sample_materialize_heal_command(command))
                if reason:
                    raise HTTPException(422, reason)
    HEAL_RECIPES_STATE.parent.mkdir(parents=True, exist_ok=True)
    with HEAL_RECIPES_STATE.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        library = _load_heal_recipe_library()
        revision = _heal_library_revision(library)
        if body.get("revision") != revision:
            raise HTTPException(409, "知识库版本已变化，请刷新后重新编辑")
        old = next((r for r in library["recipes"] if r.get("fault_type") == fault_type), None)
        if old is None:
            raise HTTPException(404, "策略不存在")
        recipe = {**old, **patch}
        # Edited commands have no execution evidence yet. Keep all existing
        # manual-only constraints and require approval of newly generated plans.
        recipe.update(requires_approval=True, auto_recovery_supported=False,
                      coverage="unverified", updated_at=time.time())
        recipe["risk"] = max([old.get("risk", "medium"), recipe["risk"]] + [a.get("risk", "medium") for a in recipe["actions"]], key=_risk_rank)
        candidate = {**library, "recipes": [recipe if r is old else r for r in library["recipes"]], "version": f"edited-{uuid.uuid4().hex[:12]}"}
        candidate.pop("source", None)
        validation = _validate_heal_recipe_library(candidate)
        if not validation["ok"]:
            raise HTTPException(422, validation)
        state = json.loads(HEAL_RECIPES_STATE.read_text()) if HEAL_RECIPES_STATE.exists() else {"library": {k: v for k, v in library.items() if k != "source"}, "history": []}
        drafts = state.setdefault("drafts", [])
        if approval is None:
            if any(d["status"] == "pending" and d["fault_type"] == fault_type for d in drafts):
                raise HTTPException(409, "此策略已有待审核草稿，请先审核或驳回")
            if sum(d["status"] == "pending" for d in drafts) >= 100:
                raise HTTPException(409, "待审核草稿已达上限，请先处理")
            draft = {"id": "recipe-draft-" + uuid.uuid4().hex, "fault_type": fault_type,
                     "base_revision": revision, "before": old, "recipe": recipe,
                     "status": "pending", "created_at": time.time(), "restored_from": restore}
            draft["content_hash"] = _heal_draft_hash(draft)
            state["drafts"] = [d for d in drafts if d["status"] == "pending"] + [d for d in drafts if d["status"] != "pending"][-100:] + [draft]
            _write_heal_state(state)
            return {"status": "ok", "publication_status": "pending", "revision": revision, "draft": draft}
        draft = next((d for d in drafts if d["id"] == approval["id"]), None)
        if draft is None:
            raise HTTPException(404, "草稿不存在")
        if (draft["status"] != "pending" or draft["base_revision"] != revision
                or draft["fault_type"] != fault_type or approval.get("content_hash") != _heal_draft_hash(draft)):
            raise HTTPException(409, "草稿状态、内容或发布基线已变化，请重新提交")
        # Publish precisely the reviewed recipe, never a newly supplied patch.
        recipe = copy.deepcopy(draft["recipe"])
        candidate["recipes"] = [recipe if r is old else r for r in library["recipes"]]
        draft.update(status="approved", reviewer=approval["reviewer"].strip(),
                     identity_source=approval["identity_source"],
                     comment=approval.get("comment", ""), decided_at=time.time())
        history = state["history"]
        event = {"at": time.time(), "fault_type": fault_type, "previous_revision": revision, "before": old, "after": recipe,
                 "operation": "restore" if restore else "edit"}
        event["approval"] = {"draft_id": draft["id"], "content_hash": _heal_draft_hash(draft),
                             "reviewer": draft["reviewer"], "identity_source": draft["identity_source"], "comment": draft["comment"]}
        if restore:
            event["restored_from"] = restore
        history = (history + [event])[-100:]
        _write_heal_state({**state, "library": candidate, "history": history})
    return {"status": "ok", "publication_status": "published", "revision": _heal_library_revision(candidate), "recipe": recipe}


@app.get("/api/heal/recipes/validate")
async def heal_recipes_validate():
    """Validate the external self-healing recipe library."""
    return _validate_heal_recipe_library(_load_heal_recipe_library())


@app.post("/api/heal/suggest")
async def heal_suggest(request: Request):
    """Generate self-healing suggestions without executing commands."""
    body = await request.json()
    ontology = _bind_ontology_heal_input(body)
    result = _build_heal_suggestions(body)
    if ontology:
        result["ontology_action_context"] = ontology
    cfg = _get_config()
    result["policy"] = {
        "enabled": cfg.remediation.enabled,
        "dry_run": getattr(cfg.remediation, "dry_run", True),
        "require_approval": cfg.remediation.require_approval,
        "max_auto_risk_level": getattr(cfg.remediation, "max_auto_risk_level", "medium"),
        "recommend_only": getattr(cfg.remediation, "recommend_only", True),
    }
    return result


@app.post("/api/heal/capability")
async def heal_capability(request: Request):
    """Analyze an alert/RCA payload and return available self-healing capability."""
    body = await request.json()
    ontology = _bind_ontology_heal_input(body)
    result = _build_heal_suggestions(body)
    if ontology:
        result["ontology_action_context"] = ontology
    cfg = _get_config()
    result["source"] = body.get("source") or ("alert" if body.get("alert") else "diagnosis")
    result["policy"] = {
        "enabled": cfg.remediation.enabled,
        "dry_run": getattr(cfg.remediation, "dry_run", True),
        "require_approval": cfg.remediation.require_approval,
        "max_auto_risk_level": getattr(cfg.remediation, "max_auto_risk_level", "medium"),
        "recommend_only": getattr(cfg.remediation, "recommend_only", True),
    }
    result["summary"] = {
        "fault_type": result.get("fault_type"),
        "namespace": result.get("namespace"),
        "target": result.get("target"),
        "suggestion_count": len(result.get("suggestions") or []),
        "blocked_count": len(result.get("blocked_templates") or []),
        "available": bool(result.get("suggestions")),
    }
    return result


@app.post("/api/skill-workflows/operate")
async def skill_workflow_operate(request: Request):
    """Run the skill-native workflow from the legacy dashboard process."""
    body = await request.json()
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else body
    domain = str(body.get("domain") or "")
    stages = body.get("stages") if isinstance(body.get("stages"), list) else None
    dry_run = bool(body.get("dry_run", True))
    allow_legacy_fallback = bool(body.get("allow_legacy_fallback", True))
    workflow_body = {
        "payload": payload,
        "domain": domain,
        "stages": stages,
        "dry_run": dry_run,
        "allow_legacy_fallback": allow_legacy_fallback,
    }
    run_id = f"skillwf-ui-{uuid.uuid4().hex[:10]}"
    started = time.time()
    record: Dict[str, Any] = {
        "id": run_id,
        "status": "running",
        "started_at": started,
        "finished_at": None,
        "duration_ms": None,
        "domain": domain,
        "dry_run": dry_run,
        "request": workflow_body,
        "result": None,
        "error": "",
        "dashboard_bridge": {},
    }
    try:
        result = await asyncio.to_thread(_post_skill_runtime_workflow, workflow_body)
        result["dashboard_bridge"] = {
            "mode": "http_proxy",
            "skill_runtime_url": SKILL_RUNTIME_URL,
        }
        result["dashboard_run_id"] = run_id
        record["status"] = result.get("status") or "completed"
        record["runtime_run_id"] = result.get("workflow_run_id")
        record["dashboard_bridge"] = result["dashboard_bridge"]
        record["result"] = result
        return result
    except Exception as proxy_exc:
        try:
            result = await asyncio.to_thread(
                _skill_workflow().run,
                payload,
                domain=domain,
                stages=stages,
                dry_run=dry_run,
                allow_legacy_fallback=allow_legacy_fallback,
            )
            result["dashboard_bridge"] = {
                "mode": "local_fallback",
                "skill_runtime_url": SKILL_RUNTIME_URL,
                "proxy_error": str(proxy_exc),
            }
            result["dashboard_run_id"] = run_id
            record["status"] = result.get("status") or "completed"
            record["dashboard_bridge"] = result["dashboard_bridge"]
            record["result"] = result
            return result
        except Exception as local_exc:
            record["status"] = "failed"
            record["error"] = f"proxy_error={proxy_exc}; local_error={local_exc}"
            raise HTTPException(
                503,
                {
                    "message": "Skill workflow runtime unavailable",
                    "skill_runtime_url": SKILL_RUNTIME_URL,
                    "proxy_error": str(proxy_exc),
                    "local_error": str(local_exc),
                },
            )
    finally:
        finished = time.time()
        record["finished_at"] = finished
        record["duration_ms"] = int((finished - started) * 1000)
        _save_skill_workflow_run(record)


@app.get("/api/skill-workflows/skills")
async def skill_workflow_skills():
    """Expose the skill catalog to the dashboard without hardcoding UI options."""
    try:
        result = await asyncio.to_thread(_get_skill_runtime_skills)
        result["dashboard_bridge"] = {
            "mode": "http_proxy",
            "skill_runtime_url": SKILL_RUNTIME_URL,
        }
        return result
    except Exception as proxy_exc:
        try:
            workflow = _skill_workflow()
            skills = [skill_catalog_item_from_definition(item) for item in workflow.executor.registry.list()]
            return {
                "skills": skills,
                "dashboard_bridge": {
                    "mode": "local_fallback",
                    "skill_runtime_url": SKILL_RUNTIME_URL,
                    "proxy_error": str(proxy_exc),
                },
            }
        except Exception as local_exc:
            raise HTTPException(
                503,
                {
                    "message": "Skill catalog unavailable",
                    "skill_runtime_url": SKILL_RUNTIME_URL,
                    "proxy_error": str(proxy_exc),
                    "local_error": str(local_exc),
                },
            )


@app.get("/api/skill-workflows/skills/{skill_id}")
async def skill_workflow_skill_detail(skill_id: str):
    """Expose one Skill's user-facing Markdown and source files for catalog review."""
    clean_id = str(skill_id or "").strip()
    if not clean_id:
        raise HTTPException(400, "skill_id is required")
    if not _SAFE_ID_RE.match(clean_id):
        raise HTTPException(400, "skill_id may only contain letters, numbers, dot, underscore, and hyphen")
    encoded_id = urllib.parse.quote(clean_id, safe="")

    detail_proxy_error = ""
    try:
        result = await asyncio.to_thread(_get_skill_runtime_json, f"/api/skills/{encoded_id}/detail")
        if "metadata" in result:
            result["dashboard_bridge"] = {
                "mode": "http_proxy",
                "skill_runtime_url": SKILL_RUNTIME_URL,
            }
            return result
    except Exception as detail_proxy_exc:
        detail_proxy_error = str(detail_proxy_exc)

    try:
        metadata = await asyncio.to_thread(_get_skill_runtime_json, f"/api/skills/{encoded_id}")
        local_detail = await asyncio.to_thread(_local_skill_detail, clean_id)
        local_detail["metadata"].update({k: v for k, v in metadata.items() if v is not None})
        local_detail["dashboard_bridge"] = {
            "mode": "http_proxy_with_local_sources",
            "skill_runtime_url": SKILL_RUNTIME_URL,
            "detail_proxy_error": detail_proxy_error,
        }
        return local_detail
    except Exception as proxy_exc:
        try:
            result = await asyncio.to_thread(_local_skill_detail, clean_id)
            result["dashboard_bridge"] = {
                "mode": "local_fallback",
                "skill_runtime_url": SKILL_RUNTIME_URL,
                "proxy_error": str(proxy_exc),
                "detail_proxy_error": detail_proxy_error,
            }
            return result
        except HTTPException:
            raise
        except Exception as local_exc:
            raise HTTPException(
                503,
                {
                    "message": "Skill detail unavailable",
                    "skill_id": clean_id,
                    "skill_runtime_url": SKILL_RUNTIME_URL,
                    "proxy_error": str(proxy_exc),
                    "local_error": str(local_exc),
                },
            )


@app.get("/api/skill-workflows/runs")
async def skill_workflow_runs(limit: int = 50):
    limit = max(1, min(limit, 200))
    local_runs = _load_skill_workflow_runs()
    local_summaries = [_skill_workflow_run_summary(item) for item in local_runs[:limit]]
    runtime_summaries: List[Dict[str, Any]] = []
    runtime_error = ""
    try:
        runtime = await asyncio.to_thread(_get_skill_runtime_json, f"/api/workflows/runs?limit={limit}")
        runtime_summaries = runtime.get("runs") if isinstance(runtime.get("runs"), list) else []
    except Exception as exc:
        runtime_error = str(exc)
    return {
        "runs": local_summaries,
        "total": len(local_runs),
        "runtime_runs": runtime_summaries,
        "runtime_total": len(runtime_summaries),
        "runtime_error": runtime_error,
    }


@app.get("/api/skill-workflows/runs/{run_id}")
async def skill_workflow_run_detail(run_id: str):
    if not _SAFE_ID_RE.match(str(run_id or "")):
        raise HTTPException(400, "run_id may only contain letters, numbers, dot, underscore, and hyphen")
    for item in _load_skill_workflow_runs():
        if item.get("id") == run_id or item.get("runtime_run_id") == run_id:
            return item
    path = _SKILL_WORKFLOW_ARTIFACT_DIR / f"{run_id}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise HTTPException(500, f"Failed to read skill workflow artifact: {exc}")
    try:
        encoded_id = urllib.parse.quote(run_id, safe="")
        return await asyncio.to_thread(_get_skill_runtime_json, f"/api/workflows/runs/{encoded_id}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise HTTPException(404, "Skill workflow run not found")
        raise HTTPException(exc.code, f"Skill runtime run lookup failed: {exc}")
    except Exception as exc:
        raise HTTPException(503, f"Skill runtime run lookup unavailable: {exc}")


@app.get("/api/heal/runs")
async def heal_runs(limit: int = 50, offset: int = Query(0, ge=0), filter: str = "all"):
    predicates = {
        "all": lambda r: True,
        "dry_run": lambda r: r.get("dry_run") is True or r.get("status") == "dry_run",
        "real": lambda r: r.get("dry_run") is False,
        "failed": lambda r: r.get("success") is False or r.get("status") == "failed",
        "recovered": lambda r: (r.get("verification") or {}).get("recovered") is True,
        "unrecovered": lambda r: (r.get("verification") or {}).get("recovered") is False,
    }
    if filter not in predicates:
        raise HTTPException(400, "Invalid heal run filter")
    runs = await asyncio.to_thread(_load_heal_runs)
    filtered = [r for r in runs if predicates[filter](r)]
    limit = max(1, min(limit, 200))
    selected = filtered[offset:offset + limit]
    summary = {
        "total": len(runs),
        "returned": len(selected),
        "dry_run": sum(1 for r in runs if r.get("dry_run") is True or r.get("status") == "dry_run"),
        "real": sum(1 for r in runs if r.get("dry_run") is False),
        "succeeded": sum(1 for r in runs if r.get("success") is True),
        "failed": sum(1 for r in runs if r.get("success") is False or r.get("status") == "failed"),
        "verification_pending": sum(1 for r in runs if r.get("status") == "verification_pending"),
        "verified_recovered": sum(1 for r in runs if (r.get("verification") or {}).get("recovered") is True),
        "verified_unrecovered": sum(1 for r in runs if (r.get("verification") or {}).get("recovered") is False),
    }
    return {"runs": selected, "total": len(runs), "summary": summary,
            "filter": filter, "offset": offset, "limit": limit, "filtered_total": len(filtered),
            "next_offset": offset + limit if offset + limit < len(filtered) else None}


@app.get("/api/heal/runs/{run_id}")
async def heal_run_detail(run_id: str):
    if not _SAFE_ID_RE.fullmatch(run_id) or run_id in {".", ".."}:
        raise HTTPException(400, "Invalid heal run ID")
    for run in _load_heal_runs():
        if run.get("id") == run_id:
            return run
    path = _HEAL_ARTIFACT_DIR / f"{run_id}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            raise HTTPException(500, f"Failed to load heal run: {e}")
    raise HTTPException(404, "Heal run not found")


def _record_verified_recovery_outcome(
    *,
    run: Dict[str, Any],
    body: Dict[str, Any],
    suggestion: Dict[str, Any],
    recovery_scope: Dict[str, Any],
) -> Dict[str, Any]:
    """Persist only real, terminal recovery outcomes for future auto gates."""
    if run.get("dry_run") is True:
        return {"recorded": False, "reason": "dry_run", **recovery_scope}
    verification = run.get("verification") if isinstance(run.get("verification"), dict) else {}
    recovered = verification.get("recovered")
    if recovered is not True and recovered is not False:
        return {"recorded": False, "reason": "recovery_not_verified", **recovery_scope}
    if verification.get('status') not in {'verified', 'failed'}:
        return {'recorded': False, 'reason': 'recovery_not_verified', **recovery_scope}
    if not recovery_scope.get("scope_complete"):
        return {"recorded": False, "reason": "recovery_scope_incomplete", **recovery_scope}
    if (not valid_verification_fingerprint(recovery_scope.get('verification_fingerprint')) or
            recovery_scope.get('verification_contract_version') != VERIFICATION_CONTRACT_VERSION):
        return {'recorded': False, 'reason': 'verification_contract_missing', **recovery_scope}
    if (verification.get('verification_fingerprint') != recovery_scope['verification_fingerprint'] or
            verification.get('verification_contract_version') != VERIFICATION_CONTRACT_VERSION or
            verification.get('verification_contract_complete') is not True or
            verification.get('verification_level') != recovery_scope.get('verification_level')):
        return {'recorded': False, 'reason': 'verification_contract_mismatch', **recovery_scope}
    if recovered is True and verification.get('verification_level') in {'http_headers', 'json_assertions'} and (
            verification.get('verification_scope') != 'infrastructure_and_business_http' or
            verification.get('business_validation') != verification['verification_level']):
        return {'recorded': False, 'reason': 'verification_contract_mismatch', **recovery_scope}

    diagnosis = suggestion.get("diagnosis_used") if isinstance(suggestion.get("diagnosis_used"), dict) else {}
    target = suggestion.get("target") if isinstance(suggestion.get("target"), dict) else {}
    execution_mode = str(body.get("execution_mode") or "").strip().lower()
    trigger = "auto" if body.get("auto_execute") or execution_mode in {"auto", "automatic", "fully_automatic"} else (
        "button" if body.get("approved") else "manual"
    )
    fault = {
        "heal_run_id": run.get("id"),
        "source": body.get("source") or "heal_execute",
        "description": body.get("message") or suggestion.get("diagnosis_summary") or "verified recovery outcome",
        "root_cause": diagnosis.get("root_cause") or body.get("root_cause") or "",
        "fault_type": run.get("fault_type") or suggestion.get("fault_type") or "",
        "error_pattern": diagnosis.get("error_pattern") or body.get("error_pattern") or "",
        "cluster_id": run.get("cluster_id") or "",
        "namespace": run.get("namespace") or "default",
        "environment": body.get("environment") or body.get("stage") or "",
        "workload_version": (
            body.get("workload_version")
            or body.get("application_version")
            or body.get("release")
            or body.get("image")
            or ""
        ),
        "target": target,
        "resolution": {
            "status": "success" if recovered is True else "failed",
            "trigger": trigger,
            "commands": list(run.get("commands") or []),
            "rollback_commands": list(run.get("rollback_commands") or []),
            "action_fingerprint": recovery_scope.get("action_fingerprint"),
            "environment_fingerprint": recovery_scope.get("environment_fingerprint"),
            "scope_version": recovery_scope.get("scope_version"),
            "verification_status": verification.get("status"),
            'verification_fingerprint': verification['verification_fingerprint'],
            'verification_contract_version': verification['verification_contract_version'],
            'verification_level': verification['verification_level'],
        },
    }
    try:
        fault_id = _get_fault_store().add_fault(fault)
        return {"recorded": True, "fault_id": fault_id, **recovery_scope}
    except Exception as exc:
        logger.warning("Failed to record verified recovery outcome: %s", exc)
        return {"recorded": False, "reason": "history_store_error", "error": str(exc), **recovery_scope}


def _bind_ontology_heal_input(body: Dict[str, Any], *, execution: bool = False) -> Optional[Dict[str, Any]]:
    """Use server-held diagnosis and scope when a proposal references a run."""
    run_id = body.get("source_run_id")
    if not run_id:
        return None
    run = _state["rca_runs"].get(run_id)
    if not run:
        raise HTTPException(404, "关联诊断不存在")
    model = build_investigation(_json_safe_snapshot(run))
    revision = body.get("ontology_revision")
    if (revision and revision != model["revision"]) or (execution and not revision):
        raise HTTPException(409, "处置关联的调查版本已变化或缺失，请重新生成方案。")
    for key in ("cluster_id", "namespace"):
        if body.get(key) and body[key] != model["scope"][key]:
            raise HTTPException(409, "处置范围与关联诊断不一致")
        body[key] = model["scope"][key]
    report = next((r for r in reversed(result_envelopes(run)) if r.get("root_cause")), {})
    body["diagnosis"] = copy.deepcopy(report)
    body["root_cause"] = report.get("root_cause", "")
    body["fault_type"] = report.get("fault_type", "")
    context = action_context(model)
    target = context.get("target") or {}
    root = report.get("root_entity") or {}
    # A named root can anchor a proposal even while its incarnation is missing;
    # the missing-identity blocker still prevents real execution.
    selected = target or root
    field = {"Pod": "pod", "Deployment": "deployment", "Service": "service", "Node": "node"}.get(selected.get("kind"))
    if field:
        if body.get(field) and body[field] != selected.get("name"):
            raise HTTPException(409, "处置对象与关联诊断不一致")
        body[field] = selected.get("name", "")
    body["ontology_revision"] = model["revision"]
    return context


def _ontology_shell_sync(command: str, timeout: int, cluster_id: str) -> Dict[str, Any]:
    """Scope executable argv only; never rewrite JSON Patch or image strings."""
    args = shlex.split(command)
    if not args or args[0] != "kubectl":
        raise HTTPException(409, "对象动作必须使用类型化 Kubernetes 操作")
    cluster = _find_k8s_cluster(cluster_id)
    if not cluster:
        raise HTTPException(409, "关联集群不存在")
    argv = [_cluster_kubectl_path(cluster)]
    if _cluster_kubeconfig_path(cluster):
        argv.append(f"--kubeconfig={_cluster_kubeconfig_path(cluster)}")
    if cluster.get("context"):
        argv.append(f"--context={cluster['context']}")
    argv.extend(args[1:])
    return _shell_sync(shlex.join(argv), timeout, None)


def _ontology_live_resource(target: Dict[str, Any], *, timeout: int = 45) -> Dict[str, Any]:
    command = shlex.join(["kubectl", "get", target["kind"].lower(), target["name"],
                          "-n", target["namespace"], "-o", "json"])
    checked = _ontology_shell_sync(command, timeout, target["cluster_id"])
    try:
        checked["json"] = json.loads(checked.get("stdout") or "{}")
    except (ValueError, TypeError):
        checked["ok"] = False
    return checked


def _verify_ontology_action(context: Dict[str, Any], commands: List[str]) -> Dict[str, Any]:
    if context["blockers"]:
        raise HTTPException(409, {"message": "关联诊断未满足对象动作前置条件", "ontology": context})
    target = context["target"]
    if not all(command_targets_bound_object(command, target) for command in commands):
        raise HTTPException(409, "动作必须显式指向绑定对象；跨对象动作需要独立建模和验证。")
    if not all(_is_safe_k8s_name(target.get(k, "")) for k in ("name", "namespace")):
        raise HTTPException(409, "绑定对象名称无法安全查询")
    checked = _ontology_live_resource(target)
    metadata = (checked.get("json") or {}).get("metadata") or {}
    if not checked.get("ok") or metadata.get("uid") != target["uid"]:
        raise HTTPException(409, "目标已更换或无法读取，必须重新诊断。")
    if str(metadata.get("resourceVersion") or "") != str(context.get("expected_resource_version") or ""):
        raise HTTPException(409, "目标资源版本已变化，必须重新取证。")
    return checked["json"]


def _execute_ontology_command(command: str, context: Dict[str, Any], proposal_id: str) -> Dict[str, Any]:
    with _heal_run_guard(proposal_id):
        proposal = next((r for r in _load_heal_runs() if r.get("id") == proposal_id), {})
        if proposal.get("execution_claim"):
            raise HTTPException(409, "该动作已提交过；请核对执行记录，不能重复提交。")
        resource = _verify_ontology_action(context, [command])
        try:
            materialized = preconditioned_action(command, context["target"], resource, restart_marker=proposal_id)
        except ValueError as exc:
            return {"command": command, "ok": False, "stderr": str(exc), "executed": False}
        proposal["execution_claim"] = {"status": "outcome_unknown", "started_at": time.time()}
        _save_heal_run(proposal)  # Persist before dispatch; a lost response never triggers retry.
        try:
            result = _ontology_shell_sync(materialized, 120, context["target"]["cluster_id"])
        except Exception:
            result = {"ok": False, "stderr": "outcome_unknown", "outcome_unknown": True}
        proposal["execution_claim"].update(status="completed" if result.get("ok") else "outcome_unknown",
                                            finished_at=time.time())
        persistence_error = False
        try:
            _save_heal_run(proposal)
        except Exception:
            # Dispatch has already occurred. The durable pre-dispatch claim is
            # intentionally still unknown; do not report this as "not sent".
            persistence_error = True
        return {**result, "command": command, "materialized_command": materialized,
                "executed": True, "persistence_error": persistence_error,
                "outcome_unknown": persistence_error or not bool(result.get("ok"))}


@app.post("/api/heal/execute")
async def heal_execute(request: Request):
    """Execute user-confirmed self-healing commands, defaulting to dry-run."""
    body = await request.json()
    ontology = _bind_ontology_heal_input(body, execution=True)
    cfg = _get_config()
    suggestion = _build_heal_suggestions(body)
    namespace = body.get("namespace") or suggestion.get("namespace") or cfg.kubernetes.namespace or "default"
    dry_run = bool(body.get("dry_run", getattr(cfg.remediation, "dry_run", True)))
    cluster_id = body.get("cluster_id") or None
    commands = [str(c).strip() for c in (body.get("commands") or []) if str(c).strip()]
    rollback_commands = [str(c).strip() for c in (body.get("rollback_commands") or []) if str(c).strip()]
    if not commands:
        commands = [s["command"] for s in suggestion.get("suggestions", []) if s.get("executable")]
        rollback_commands = [s.get("rollback_command", "") for s in suggestion.get("suggestions", []) if s.get("rollback_command")]
    if not commands:
        raise HTTPException(400, "No executable healing commands")
    max_steps = max(1, int(getattr(cfg.remediation, "max_steps", 5) or 5))
    if not dry_run and len(commands) > max_steps:
        raise HTTPException(400, f"Self-healing command count {len(commands)} exceeds max_steps={max_steps}")

    blocked = [{"command": cmd, "reason": _validate_heal_command(cmd)} for cmd in commands]
    blocked = [b for b in blocked if b["reason"]]
    if blocked:
        raise HTTPException(400, {"message": "Healing commands failed safety validation", "blocked": blocked})
    target = suggestion.get("target") if isinstance(suggestion.get("target"), dict) else {}
    recovery_scope = _recovery_scope_for_plan(
        body=body,
        target=target,
        commands=commands,
        namespace=namespace,
        cfg=cfg,
    )
    actual_auto_gate = None
    ontology_proposal_id = str(body.get("ontology_proposal_id") or "")
    if ontology and not dry_run:
        if len(commands) != 1:
            raise HTTPException(409, "对象动作每次提交一个原子操作；请在方案中选择单个动作后重新预演。")
        proposal = next((r for r in _load_heal_runs() if r.get("id") == ontology_proposal_id), {})
        if (proposal.get("dry_run") is not True or proposal.get("commands") != commands
                or proposal.get("rollback_commands") != rollback_commands
                or (proposal.get("ontology_action_context") or {}) != ontology):
            raise HTTPException(409, "动作内容尚未绑定预演记录，请先对当前方案执行 Dry-run。")
    if not dry_run:
        diagnosis_gate = suggestion.get("diagnosis_gate") or {}
        if not diagnosis_gate.get("ready"):
            raise HTTPException(400, {
                "message": "Self-healing real execution requires a high-confidence RCA diagnosis",
                "diagnosis_gate": diagnosis_gate,
            })
        execution_mode = str(body.get("execution_mode") or "").strip().lower()
        auto_requested = bool(
            body.get("auto_execute")
            or execution_mode in {"auto", "automatic", "fully_automatic"}
        )
        if auto_requested:
            suggestion_by_command = {
                str(item.get("command") or ""): item
                for item in suggestion.get("suggestions", [])
                if item.get("command")
            }
            selected_actions = []
            for index, command in enumerate(commands):
                action = dict(suggestion_by_command.get(command) or {
                    "command": command,
                    "risk": "medium",
                    "source": "caller_selected",
                })
                if len(rollback_commands) == len(commands) and rollback_commands[index]:
                    action["rollback_command"] = rollback_commands[index]
                selected_actions.append(action)
            history_stats, recovery_scope = _scoped_recovery_history_stats(
                body=body,
                diagnosis=suggestion.get("diagnosis_used") or {},
                fault_type=str(suggestion.get("fault_type") or ""),
                target=target,
                commands=commands,
                namespace=namespace,
                cfg=cfg,
            )
            actual_auto_gate = _build_auto_execution_gate(
                recipe=suggestion.get("recipe"),
                suggestions=selected_actions,
                diagnosis=suggestion.get("diagnosis_used") or {},
                cfg=cfg,
                recovery_history_stats=history_stats,
            )
            if not actual_auto_gate.get("ready"):
                raise HTTPException(400, {
                    "message": "Self-healing automatic execution gate rejected this action and environment scope",
                    "auto_execution_gate": actual_auto_gate,
                    "recovery_history_scope": recovery_scope,
                })
        if getattr(cfg.remediation, "recommend_only", False):
            raise HTTPException(400, "Self-healing is in recommend_only mode; disable recommend_only in configuration center to execute real actions")
        if not cfg.remediation.enabled:
            raise HTTPException(400, "Self-healing execution is disabled in configuration center")
        if cfg.remediation.require_approval and not bool(body.get("approved")):
            raise HTTPException(400, "Self-healing execution requires approved=true")
        high_risk = [cmd for cmd in commands if any(x in cmd for x in (" delete ", " drain ", " cordon ", " taint "))]
        if high_risk and getattr(cfg.remediation, "require_confirmation_for_high_risk", True) and not bool(body.get("risk_ack")):
            raise HTTPException(400, "High-risk healing commands require risk_ack=true")
        if ontology:
            await asyncio.to_thread(_verify_ontology_action, ontology, commands + rollback_commands)

    run_id = f"heal-{uuid.uuid4().hex[:10]}"
    started_at = time.time()
    results: List[Dict[str, Any]] = []
    for idx, cmd in enumerate(commands):
        if dry_run:
            result = {"command": cmd, "ok": True, "dry_run": True, "stdout": "", "stderr": ""}
        else:
            if ontology:
                try:
                    result = await asyncio.to_thread(_execute_ontology_command, cmd, ontology, ontology_proposal_id)
                except HTTPException as exc:
                    result = {"command": cmd, "ok": False, "executed": False, "stderr": str(exc.detail)}
            else:
                result = await asyncio.to_thread(_shell_sync, cmd, 120, cluster_id)
        results.append({"index": idx, **result})
        if not result.get("ok") and not dry_run:
            break
    action_success = all(r.get("ok") for r in results)
    not_dispatched = any(r.get("executed") is False for r in results)
    outcome_unknown = any(
        r.get("outcome_unknown") or (not r.get("ok") and r.get("executed") is not False and not (
            r.get("executed") is True and r.get("execution_failure_confirmed") is True))
        for r in results
    )
    try:
        if dry_run:
            verification = {"status": "skipped", "reason": "dry_run", "recovered": None, "checks": []}
        elif not action_success:
            # A confirmed command failure is an unsuccessful recovery attempt
            # under the planned acceptance contract. It does not assert that
            # business probes ran or that their response was evaluated.
            verification = {"status": "failed", "reason": "action_execution_failed", "recovered": False,
                            "checks": [], "verification_scope": "action_execution",
                            "verification_contract_basis": "planned_acceptance",
                            **{k: recovery_scope.get(k) for k in (
                                "verification_fingerprint", "verification_contract_version",
                                "verification_contract_complete", "verification_level")}}
        else:
            verification = await asyncio.to_thread(
                _verify_heal_recovery,
                namespace=namespace,
                suggestion=suggestion,
                commands=commands,
                cluster_id=cluster_id,
                dry_run=dry_run,
                executed_success=action_success,
            )
            if not isinstance(verification, dict):
                raise ValueError("Invalid recovery verification result")
            verification = await asyncio.to_thread(
                verify_business_recovery, verification,
                profiles=getattr(cfg.remediation, "business_recovery_probes", []),
                cluster_id=cluster_id, namespace=namespace,
                service=str(target.get("service") or ""),
            )
    except Exception:
        logger.exception("Recovery verification failed for heal run %s", run_id)
        verification = {"status": "unknown", "reason": "verification_error",
                        "recovered": None, "checks": []}
    recovered = verification.get("recovered")
    if outcome_unknown or not_dispatched:
        verification = {"status": "unknown" if outcome_unknown else "skipped",
                        "reason": "outcome_unknown" if outcome_unknown else "action_not_dispatched",
                        "recovered": None, "checks": []}
        recovered = None
    if dry_run:
        status, success = "dry_run", action_success
    elif outcome_unknown:
        status, success, action_success = "outcome_unknown", None, None
    elif not_dispatched:
        status, success = "blocked", False
    elif not action_success or recovered is False:
        status, success = "failed", False
    elif recovered is True and verification.get("status") == "verified":
        status, success = "succeeded", True
    else:
        status, success = "verification_pending", None
        # Nonterminal or malformed evidence must not enter recovery statistics.
        verification = {**verification, "recovered": None}
    run = {
        "id": run_id,
        "status": status,
        "success": success,
        "action_success": action_success,
        "dry_run": dry_run,
        "namespace": namespace,
        "cluster_id": cluster_id,
        "commands": commands,
        "rollback_commands": rollback_commands,
        "ontology_action_context": ontology,
        "results": results,
        "verification": verification,
        "source": body.get("source") or "api",
        "fault_type": body.get("fault_type") or suggestion.get("fault_type") or _normalize_heal_fault_type(body.get("message", "")),
        "diagnosis_mode": suggestion.get("diagnosis_mode") or body.get("diagnosis_mode") or "alert_only",
        "diagnosis_used": suggestion.get("diagnosis_used") or {},
        "diagnosis_gate": suggestion.get("diagnosis_gate") or {},
        "auto_execution_gate": actual_auto_gate or (suggestion.get("capability") or {}).get("auto_execution_gate") or {},
        "recovery_history_scope": recovery_scope,
        "verification_target": copy.deepcopy(target),
        "diagnosis_summary": suggestion.get("diagnosis_summary") or "",
        "edited_actions": body.get("edited_actions") or [],
        "policy": {
            "enabled": cfg.remediation.enabled,
            "dry_run_default": getattr(cfg.remediation, "dry_run", True),
            "require_approval": cfg.remediation.require_approval,
            "recommend_only": getattr(cfg.remediation, "recommend_only", True),
            "max_steps": max_steps,
            "confidence_threshold": getattr(cfg.remediation, "confidence_threshold", 0.85),
        },
        "started_at": started_at,
        "finished_at": time.time(),
    }
    run["recovery_history_record"] = _record_verified_recovery_outcome(
        run=run,
        body=body,
        suggestion=suggestion,
        recovery_scope=recovery_scope,
    )
    _save_heal_run(run)
    return run


class _InternalHealRequest:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return copy.deepcopy(self.body)


async def _guarded_auto_recovery(run_id: str) -> Dict[str, Any]:
    """Run at most one server-selected, ontology-bound auto action per RCA.

    The durable claim is written before preview or dispatch. A process crash
    leaves an unknown outcome requiring reconciliation, never an automatic retry.
    """
    cfg = _get_config()
    if getattr(cfg.remediation, "auto_loop_enabled", False) is not True:
        return {"status": "disabled", "reason": "auto_loop_disabled"}
    run = _state["rca_runs"].get(run_id)
    if not run:
        raise HTTPException(404, "关联诊断不存在")
    if run.get("status") != "completed":
        return {"status": "blocked", "reason": "investigation_not_completed"}
    if run.get("guarded_auto_recovery") is not True:
        return {"status": "blocked", "reason": "investigation_not_guarded"}
    started = run.get("started_at")
    if (type(started) not in (int, float) or not math.isfinite(started)
            or not 0 <= time.time() - started <= 600):
        return {"status": "blocked", "reason": "investigation_not_fresh"}
    claim_id = "auto-" + run_id
    with _heal_run_guard(claim_id):
        claim_path = _HEAL_ARTIFACT_DIR / f"{claim_id}.json"
        if claim_path.exists():
            try:
                claim = json.loads(claim_path.read_text(encoding="utf-8"))
                if not isinstance(claim, dict) or claim.get("source_run_id") != run_id:
                    raise ValueError("invalid claim")
                return ({**claim, "status": "outcome_unknown", "reason": "interrupted_attempt_requires_reconciliation"}
                        if claim.get("status") in {"preparing", "dispatching"} else claim)
            except (ValueError, OSError):
                raise HTTPException(503, "自动恢复记录不可读取，请人工核对")
        claim = {"source_run_id": run_id, "status": "preparing", "started_at": time.time()}
        _atomic_write_json(claim_path, claim)
        dispatched = False
        try:
            model = build_investigation(_json_safe_snapshot(run))
            context = action_context(model)
            target = context.get("target") or {}
            if not context.get("recorded_conditions_met") or target.get("kind") != "Deployment":
                claim.update(status="blocked", reason="ontology_action_not_ready", blockers=context.get("blockers", []))
            else:
                services = {o["id"]: o for o in model["objects"] if o.get("kind") == "Service"
                            and o.get("identity_status") == "uid_bound"}
                related = {r["source_id"] for r in model["relations"]
                           if r.get("type") == "matches_workload_template"
                           and r.get("target_id") == target.get("id") and r.get("source_id") in services}
                if len(related) != 1 or model.get("gaps"):
                    claim.update(status="blocked", reason="service_binding_incomplete_or_ambiguous")
                else:
                    service = services[next(iter(related))]
                    policies = [p for p in getattr(cfg.remediation, "auto_scale_targets", [])
                                if isinstance(p, dict) and all(p.get(k) == v for k, v in {
                                    "cluster_id": target.get("cluster_id"), "namespace": target.get("namespace"),
                                    "deployment": target.get("name"), "service": service.get("name")}.items())]
                    if (len(policies) != 1 or type(policies[0].get("replicas")) is not int
                            or not 1 <= policies[0]["replicas"] <= 100):
                        claim.update(status="blocked", reason="explicit_scale_policy_required")
                        claim["finished_at"] = time.time()
                        _atomic_write_json(claim_path, claim)
                        return claim
                    policy = copy.deepcopy(policies[0])
                    body = {"source_run_id": run_id, "ontology_revision": model["revision"],
                            "service": service["name"], "source": "guarded_auto_recovery"}
                    _bind_ontology_heal_input(body, execution=True)
                    suggestion = _build_heal_suggestions(body)
                    candidates = []
                    for item in suggestion.get("suggestions", []):
                        command = str(item.get("command") or "")
                        try:
                            args = shlex.split(command)
                        except ValueError:
                            continue
                        requested = [v.split("=", 1)[1] for v in args if v.startswith("--replicas=")]
                        requested += [args[i + 1] for i, v in enumerate(args[:-1]) if v == "--replicas"]
                        if (item.get("source") == "kb" and item.get("executable") is True
                                and len(args) > 2 and args[:2] == ["kubectl", "scale"]
                                and requested == [str(policy["replicas"])]
                                and command_targets_bound_object(command, target)
                                and item.get("rollback_command")
                                and command_targets_bound_object(item["rollback_command"], target)):
                            candidates.append(item)
                    if len(candidates) != 1:
                        claim.update(status="blocked", reason="unique_scoped_recipe_action_required")
                    else:
                        item = candidates[0]
                        live = await asyncio.to_thread(_verify_ontology_action, context, [item["command"], item["rollback_command"]])
                        service_versions = {o.get("resource_version") for o in model["observations"]
                                            if o.get("entity_id") == service["id"] and o.get("temporality") != "historical"
                                            and o.get("resource_version")}
                        checked_service = await asyncio.to_thread(_ontology_live_resource, service)
                        service_resource = checked_service.get("json") or {}
                        service_meta = service_resource.get("metadata") or {}
                        service_spec = service_resource.get("spec") or {}
                        selector = service_spec.get("selector")
                        labels = (((live.get("spec") or {}).get("template") or {}).get("metadata") or {}).get("labels") or {}
                        if (len(service_versions) != 1 or not checked_service.get("ok")
                                or service_meta.get("uid") != service.get("uid")
                                or service_meta.get("resourceVersion") not in service_versions
                                or service_meta.get("deletionTimestamp")
                                or service_spec.get("type") == "ExternalName"
                                or not isinstance(selector, dict) or not selector or not isinstance(labels, dict)
                                or any(labels.get(k) != v for k, v in selector.items())):
                            claim.update(status="blocked", reason="live_service_binding_changed")
                            claim["finished_at"] = time.time()
                            _atomic_write_json(claim_path, claim)
                            return claim
                        if (not isinstance(live.get("spec"), dict)
                                or type(live["spec"].get("replicas")) is not int
                                or live["spec"]["replicas"] != 0):
                            claim.update(status="blocked", reason="live_deployment_not_scaled_to_zero")
                            claim["finished_at"] = time.time()
                            _atomic_write_json(claim_path, claim)
                            return claim
                        body.update(commands=[item["command"]], rollback_commands=[item["rollback_command"]], dry_run=True)
                        preview = await heal_execute(_InternalHealRequest(body))
                        if preview.get("status") != "dry_run" or not preview.get("id"):
                            claim.update(status="blocked", reason="preview_failed")
                        else:
                            # Preview and network calls consume time. Recheck the
                            # Service immediately before dispatch; the object
                            # update itself separately tests the Deployment RV.
                            final_service = await asyncio.to_thread(_ontology_live_resource, service)
                            final_resource = final_service.get("json") or {}
                            final_meta = final_resource.get("metadata") or {}
                            final_spec = final_resource.get("spec") or {}
                            current_cfg = _get_config()
                            current_policies = [p for p in getattr(current_cfg.remediation, "auto_scale_targets", [])
                                                if isinstance(p, dict) and all(p.get(k) == v for k, v in {
                                                    "cluster_id": target.get("cluster_id"), "namespace": target.get("namespace"),
                                                    "deployment": target.get("name"), "service": service.get("name")}.items())]
                            if (getattr(current_cfg.remediation, "auto_loop_enabled", False) is not True
                                    or len(current_policies) != 1 or current_policies[0] != policy
                                    or not 0 <= time.time() - started <= 600 or not final_service.get("ok")
                                    or final_meta.get("uid") != service.get("uid")
                                    or final_meta.get("resourceVersion") not in service_versions
                                    or final_meta.get("deletionTimestamp")
                                    or final_spec.get("type") == "ExternalName"
                                    or final_spec.get("selector") != selector):
                                claim.update(status="blocked", reason="service_binding_or_diagnosis_changed_after_preview",
                                             proposal_id=preview["id"], finished_at=time.time())
                                _atomic_write_json(claim_path, claim)
                                return claim
                            claim.update(status="dispatching", proposal_id=preview["id"])
                            _atomic_write_json(claim_path, claim)
                            body.update(dry_run=False, auto_execute=True, ontology_proposal_id=preview["id"])
                            dispatched = True
                            result = await heal_execute(_InternalHealRequest(body))
                            claim.update(status=result.get("status", "outcome_unknown"), heal_run_id=result.get("id"),
                                         success=result.get("success"), verification=result.get("verification"))
        except Exception as exc:
            rejected = isinstance(exc, HTTPException) and exc.status_code in (400, 409)
            claim.update(status="outcome_unknown" if dispatched and not rejected else "blocked",
                         reason=("auto_gate_rejected" if rejected else
                                 "dispatch_or_verification_error" if dispatched else "preparation_error"),
                         error_type=type(exc).__name__)
            detail = exc.detail if isinstance(exc, HTTPException) and isinstance(exc.detail, dict) else {}
            gates = (detail.get("auto_execution_gate"), detail.get("diagnosis_gate"))
            blockers = [str(value) for gate in gates if isinstance(gate, dict)
                        for value in (gate.get("blockers") or []) if isinstance(value, str)]
            if blockers:
                claim["blockers"] = list(dict.fromkeys(blockers))
        claim["finished_at"] = time.time()
        _atomic_write_json(claim_path, claim)
        return claim


def _schedule_guarded_auto_recovery(run_id: str) -> None:
    if getattr(_get_config().remediation, "auto_loop_enabled", False) is not True:
        return
    try:
        asyncio.run(_guarded_auto_recovery(run_id))
    except Exception:
        logger.exception("Guarded auto recovery could not be recorded for %s", run_id)


@app.post("/api/rca/{run_id}/auto-heal")
async def rca_guarded_auto_heal(run_id: str):
    return await _guarded_auto_recovery(run_id)


@app.get("/api/rca/{run_id}/auto-heal")
async def rca_guarded_auto_heal_status(run_id: str):
    claim_id = "auto-" + run_id
    if not _SAFE_ID_RE.fullmatch(claim_id) or claim_id in {".", ".."}:
        raise HTTPException(400, "无效的诊断标识")
    path = _HEAL_ARTIFACT_DIR / f"{claim_id}.json"
    if not path.exists():
        raise HTTPException(404, "未找到自动恢复记录")
    try:
        claim = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(claim, dict) or claim.get("source_run_id") != run_id:
            raise ValueError("invalid claim")
    except (ValueError, OSError) as exc:
        raise HTTPException(503, "自动恢复记录不可读取，请人工核对") from exc
    if claim.get("status") in {"preparing", "dispatching"}:
        return {**claim, "outcome_final": False}
    return claim


def _reverify_heal_run(run_id: str) -> Dict[str, Any]:
    with _heal_run_guard(run_id):
        run = next((r for r in _load_heal_runs() if r.get("id") == run_id), None)
        if run is None:
            raise HTTPException(404, "Heal run not found in active ledger")
        if run.get("status") != "verification_pending" or run.get("dry_run") is not False or run.get("action_success") is not True:
            raise HTTPException(409, "Only pending recovery after successful real execution can be reverified")
        if run.get("rollback") and (run["rollback"].get("dry_run") is not True):
            raise HTTPException(409, "Run has real rollback activity; verify manually")
        target = run.get("verification_target")
        if not isinstance(target, dict) or not target:
            raise HTTPException(409, "Original verification target missing; verify manually")
        previous = copy.deepcopy(run.get("verification"))
        saved_scope = run.get('recovery_history_scope')
        saved_scope = saved_scope if isinstance(saved_scope, dict) else {}
        previous_policy = previous if isinstance(previous, dict) else {}
        expected_contract = (run.get('verification_required_fingerprint') or
                             saved_scope.get('verification_fingerprint') or previous_policy.get('verification_fingerprint'))
        if valid_verification_fingerprint(expected_contract):
            run['verification_required_fingerprint'] = expected_contract
        try:
            verification = _verify_heal_recovery(
                namespace=run["namespace"], suggestion={"target": target},
                commands=run.get("commands") or [], cluster_id=run.get("cluster_id"),
                dry_run=False, executed_success=True)
            verification = verify_business_recovery(verification,
                profiles=getattr(_get_config().remediation, "business_recovery_probes", []),
                cluster_id=run.get("cluster_id"), namespace=run["namespace"],
                service=str(target.get("service") or ""))
            if valid_verification_fingerprint(expected_contract) and verification.get('verification_fingerprint') != expected_contract:
                if verification.get('recovered') is False and verification.get('verification_scope') == 'infrastructure':
                    verification = {**verification, 'verification_contract_changed': True}
                else:
                    verification = {**verification, 'status': 'unknown', 'recovered': None,
                                    'reason': 'verification_contract_changed'}
            elif expected_contract and not valid_verification_fingerprint(expected_contract):
                verification = {**verification, 'status': 'unknown', 'recovered': None,
                                'reason': 'verification_contract_invalid'}
            # Never downgrade a prior business requirement to infrastructure-only success.
            if verification.get("recovered") is True and verification.get("verification_scope") != "infrastructure_and_business_http":
                verification = {**verification, "status": "unknown", "recovered": None,
                                "reason": "business_probe_required_for_reverification"}
        except Exception:
            logger.exception("Recovery recheck failed for %s", run_id)
            verification = {"status": "unknown", "recovered": None, "reason": "verification_error", "checks": []}
        recovered = verification.get("recovered")
        terminal = verification.get("status") in {"verified", "failed"} and type(recovered) is bool
        if not terminal:
            verification = {**verification, "recovered": None}
        run["verification_history"] = [*(run.get("verification_history") or []),
            {"verification": previous, "rechecked_at": time.time()}][-20:]
        run["verification"] = verification
        run["verification_updated_at"] = time.time()
        run["status"] = ("succeeded" if recovered else "failed") if terminal else "verification_pending"
        run["success"] = recovered if terminal else None
        # A later observation cannot establish that the original action caused recovery.
        run["recovery_history_record"] = {"recorded": False, "reason": "reverification_observation_only"}
        _save_heal_run(run)
        return run


@app.post("/api/heal/runs/{run_id}/verify")
async def heal_run_verify(run_id: str):
    return await asyncio.to_thread(_reverify_heal_run, run_id)


@app.post("/api/heal/runs/{run_id}/rollback")
async def heal_run_rollback(run_id: str, request: Request):
    body = await request.json()
    return await asyncio.to_thread(_heal_run_rollback_locked, run_id, body)


def _heal_run_rollback_locked(run_id: str, body: Dict[str, Any]):
    # The worker owns the lock even after the HTTP coroutine is cancelled.
    with _heal_run_guard(run_id):
        return _heal_run_rollback_sync(run_id, body)


def _heal_run_rollback_sync(run_id: str, body: Dict[str, Any]):
    """Run recorded rollback commands for a healing run."""
    dry_run = bool(body.get("dry_run", True))
    approved = bool(body.get("approved"))
    run = None
    for item in _load_heal_runs():
        if item.get("id") == run_id:
            run = item
            break
    if not run:
        raise HTTPException(404, "Heal run not found")
    if not dry_run and run.get("ontology_action_context"):
        raise HTTPException(409, "对象动作回退必须重新取证，并以当前对象版本生成独立方案。")
    commands = [cmd for cmd in (run.get("rollback_commands") or []) if cmd]
    if not commands:
        raise HTTPException(400, "No rollback commands recorded")
    blocked = [{"command": cmd, "reason": _validate_heal_command(cmd)} for cmd in commands]
    blocked = [b for b in blocked if b["reason"]]
    if blocked:
        raise HTTPException(400, {"message": "Rollback commands failed safety validation", "blocked": blocked})
    if not dry_run and not approved:
        raise HTTPException(400, "Rollback execution requires approved=true")
    if not dry_run:
        if (run.get("rollback") or {}).get("status") in {"running", "outcome_unknown"}:
            raise HTTPException(409, "Previous rollback outcome is unknown; inspect before retrying")
        run["rollback"] = {"status": "running", "dry_run": False,
                           "started_at": time.time(), "results": [], "success": None}
        _save_heal_run(run)  # Fail closed before the first remote write.
    results = []
    for idx, cmd in enumerate(reversed(commands)):
        if dry_run:
            result = {"command": cmd, "ok": True, "dry_run": True, "stdout": "", "stderr": ""}
        else:
            try:
                result = _shell_sync(cmd, 120, run.get("cluster_id"))
            except Exception:
                logger.exception("Rollback outcome unknown for %s", run_id)
                result = {"command": cmd, "ok": False, "error": "outcome_unknown"}
        results.append({"index": idx, **result})
        if not dry_run and not result.get("ok"):
            break
    rollback_result = {
        "status": "dry_run" if dry_run else ("completed" if all(r.get("ok") for r in results) else "outcome_unknown"),
        "dry_run": dry_run,
        "results": results,
        "finished_at": time.time(),
        "success": all(r.get("ok") for r in results),
    }
    run["rollback_preview" if dry_run else "rollback"] = rollback_result
    _save_heal_run(run)
    return rollback_result


# ─────────────────────────────────────────
# Pipeline APIs
# ─────────────────────────────────────────

@app.get("/api/pipeline/history")
async def pipeline_history():
    pipeline = _get_pipeline()
    return {"history": pipeline.get_history()}


@app.get("/api/pipeline/stats")
async def pipeline_stats():
    pipeline = _get_pipeline()
    return pipeline.get_stats()


# ─────────────────────────────────────────
# Daemon Management APIs
# ─────────────────────────────────────────

@app.get("/api/daemon/status")
async def daemon_status():
    daemon = _state.get("daemon")
    if daemon and daemon._running:
        return daemon.status()
    return {"running": False}


@app.post("/api/daemon/start")
async def daemon_start(request: Request):
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    
    if _state.get("daemon") and _state["daemon"]._running:
        return {"status": "already_running"}

    cfg = _get_config()

    def _push_signal(signal_obj):
        """Push detection signal to SSE deque for real-time streaming."""
        _state["detection_signals"].append(
            signal_obj.to_dict() if hasattr(signal_obj, "to_dict") else signal_obj
        )

    daemon = Daemon(
        cfg,
        log_callback=lambda msg: _state["daemon_logs"].append(msg),
        signal_callback=_push_signal,
    )
    _state["daemon"] = daemon

    def run():
        asyncio.run(daemon.start())

    t = threading.Thread(target=run, daemon=True, name="sre-daemon")
    t.start()
    _state["daemon_thread"] = t
    return {"status": "started"}


@app.post("/api/daemon/stop")
async def daemon_stop():
    daemon = _state.get("daemon")
    if daemon and daemon._running:
        asyncio.run_coroutine_threadsafe(daemon.stop(), daemon._loop)
        return {"status": "stopping"}
    return {"status": "not_running"}


@app.get("/api/daemon/logs")
async def daemon_logs(limit: int = 100):
    logs = list(_state["daemon_logs"])[-limit:]
    return {"logs": logs}


@app.get("/api/daemon/logs/stream")
async def daemon_log_stream():
    """SSE stream for daemon logs."""
    async def event_gen():
        idx = 0
        while True:
            logs = list(_state["daemon_logs"])
            if idx < len(logs):
                for msg in logs[idx:]:
                    yield f"data: {json.dumps({'type': 'log', 'msg': msg})}\n\n"
                idx = len(logs)
            
            # Status heartbeat
            daemon = _state.get("daemon")
            if daemon and daemon._running:
                yield f"data: {json.dumps({'type': 'status', 'data': daemon.status()})}\n\n"
            
            yield f": heartbeat\n\n"
            await asyncio.sleep(3)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


# ─────────────────────────────────────────
# Knowledge Base APIs (故障知识库)
# ─────────────────────────────────────────

def _get_fault_store():
    if "fault_store" not in _state:
        from memory import FaultContextStore
        _state["fault_store"] = FaultContextStore(_get_config())
    return _state["fault_store"]


def _get_feedback_store():
    if "feedback_store" not in _state:
        from memory import ExpertFeedbackStore
        _state["feedback_store"] = ExpertFeedbackStore()
    return _state["feedback_store"]


def _get_human_review_store():
    if "human_review_store" not in _state:
        from memory import HumanReviewStore
        _state["human_review_store"] = HumanReviewStore()
    return _state["human_review_store"]


def _get_evolution_tracker():
    if "evolution_tracker" not in _state:
        from memory import EvolutionTracker
        _state["evolution_tracker"] = EvolutionTracker.from_config()
    return _state["evolution_tracker"]


def _get_trace_store():
    """Reload-on-read trace store so we always see the latest pipeline_traces.json."""
    from memory import TraceStore
    return TraceStore(_get_config())


@app.get("/api/knowledge/stats")
async def knowledge_stats():
    """知识库统计信息"""
    store = _get_fault_store()
    fb = _get_feedback_store()
    stats = store.stats()
    fb_stats = fb.get_feedback_stats()
    return {
        "backend": stats.get("backend", "json"),
        "rules_count": stats.get("rules_count", 0),
        "faults_count": stats.get("faults_count", 0),
        "feedback_count": fb_stats.get("total", 0),
        "total_rules_generated": fb_stats.get("total_rules_generated", 0),
        "health_score": stats.get("health_score", 0),
        "avg_quality_score": stats.get("avg_quality_score", 0),
        "stale_rules": stats.get("stale_rules", 0),
        "low_quality_rules": stats.get("low_quality_rules", 0),
        "conflicts": stats.get("conflicts", 0),
    }


@app.get("/api/knowledge/governance")
async def knowledge_governance():
    """记忆治理报告：质量、陈旧规则、冲突规则。"""
    store = _get_fault_store()
    return store.governance_report()


_KNOWLEDGE_FAULT_TYPE_ZH = {
    "configuration error": "配置错误",
    "configuration/operational error": "配置或运维操作错误",
    "resource configuration": "资源配置异常",
    "resource configuration & performance bottleneck": "资源配置异常与性能瓶颈",
    "observability": "可观测性异常",
    "infrastructure": "基础设施异常",
    "infrastructure/network": "基础设施网络异常",
    "infrastructure_network": "基础设施网络异常",
    "infrastructure_connectivity": "基础设施连通性异常",
    "network_issue": "网络异常",
    "dependency_failure": "依赖服务异常",
    "resource_exhaustion": "资源耗尽",
    "application configuration / dependency": "应用配置或依赖异常",
}


_KNOWLEDGE_TRANSLATIONS = (
    ("Logs show identical error pattern 'Failed to select targets: no pod is selected'", "日志显示相同错误模式：选择故障注入目标失败，没有选中任何 Pod"),
    ("Failed to select targets: no pod is selected", "选择故障注入目标失败：没有选中任何 Pod"),
    ("for multiple PodChaos experiments", "多个 PodChaos 实验"),
    ("occurring simultaneously with", "同时伴随"),
    ("selector in the PodChaos resource", "PodChaos 资源中的选择器"),
    ("does not match any existing pods in the astronomy-shop namespace", "没有匹配 astronomy-shop 命名空间中的任何现有 Pod"),
    ("causing target selection failure across multiple chaos experiments", "导致多个混沌实验均无法选择目标"),
    ("selector does not match any existing pods in the namespace", "选择器没有匹配命名空间中的任何现有 Pod"),
    ("PodChaos experiment selector configuration error", "PodChaos 实验选择器配置错误"),
    ("selector in the PodChaos resource does not match any existing pods", "PodChaos 资源中的选择器没有匹配任何现有 Pod"),
    ("Multiple chaos experiments fail simultaneously", "多个混沌实验同时失败"),
    ("identical target selection errors", "相同的目标选择错误"),
    ("service metrics remain normal", "服务指标保持正常"),
    ("Logs show identical error pattern", "日志显示相同错误模式"),
    ("Multiple experiments affected", "多个实验受到影响"),
    ("suggest", "说明"),
    ("Systematic selector configuration issue in chaos framework resources", "混沌框架资源存在系统性的选择器配置问题"),
    ("Chaos framework configuration problem rather than runtime or infrastructure issue", "这是混沌框架配置问题，而不是运行时或基础设施故障"),
    ("Cross-signal correlation shows perfect alignment between logs and events", "跨信号关联显示日志与事件完全一致"),
    ("Network connectivity failure preventing Docker image pull from container registry", "网络连通性故障导致无法从容器镜像仓库拉取镜像"),
    ("Container initialization failure due to infrastructure connectivity issues rather than application error", "容器初始化失败由基础设施连通性问题引起，而不是应用错误"),
    ("Infrastructure-level failure (likely network) preventing container initialization", "基础设施层故障（很可能是网络问题）阻止了容器初始化"),
    ("Pod reports CrashLoopBackOff with exit code 1", "Pod 报告 CrashLoopBackOff，退出码为 1"),
    ("Kubernetes events show image pull failure sequence", "Kubernetes 事件显示镜像拉取失败链路"),
    ("No application logs, metrics, or traces exist", "未采集到应用日志、指标或链路追踪"),
    ("Pod in CrashLoopBackOff", "Pod 处于 CrashLoopBackOff"),
    ("image pull failure", "镜像拉取失败"),
    ("pod scheduling failure cascade", "Pod 调度失败级联"),
    ("Memory limit configuration is insufficient for the application's memory requirements", "内存限制配置低于应用实际内存需求"),
    ("Pod status shows OOMKilled", "Pod 状态显示 OOMKilled"),
    ("memory usage metrics exceed configured limits.memory", "内存使用指标超过配置的 limits.memory"),
    ("Monitoring and observability configuration issues preventing capture of critical failure signals", "监控和可观测性配置问题导致关键故障信号未被采集"),
    ("Logs are missing and event records are blank while other monitoring data exists", "在其他监控数据存在的情况下，日志缺失且事件记录为空"),
    ("Application-specific configuration or dependency failure causing immediate container crash during initialization", "应用配置或依赖异常导致容器在初始化阶段立即崩溃"),
    ("missing environment variables", "环境变量缺失"),
    ("invalid configuration files", "配置文件无效"),
    ("unresolved dependencies", "依赖无法解析"),
    ("Manual or automated scaling operation incorrectly set", "人工或自动化扩缩容操作错误地设置了"),
    ("deployment replicas to 0", "Deployment 副本数为 0"),
    ("human error or misconfigured automation", "人为操作错误或自动化配置错误"),
    ("monitoring gaps", "监控缺口"),
    ("failed to detect the 0-replica state", "未能检测到 0 副本状态"),
    ("The etcd cluster lost quorum", "etcd 集群丢失多数派"),
    ("Kubernetes API server unavailability", "Kubernetes API Server 不可用"),
    ("telemetry", "遥测数据"),
    ("network issue making one or more etcd members unreachable", "网络问题导致一个或多个 etcd 成员不可达"),
    ("Resource exhaustion is unlikely", "资源耗尽可能性较低"),
    ("metrics endpoint is unreachable", "指标端点不可达"),
    ("TargetDown alert", "TargetDown 告警"),
    ("observability pipeline failure", "可观测性链路故障"),
    ("Insufficient signals to confirm a single root cause", "当前信号不足，无法确认单一根因"),
    ("Watchdog alert fired as a preconfigured heartbeat", "Watchdog 告警作为预配置心跳触发"),
    ("No application or infrastructure fault was detected", "未检测到应用或基础设施故障"),
    ("No active incident detected", "未检测到活跃故障"),
    ("all monitored services", "所有被监控服务"),
    ("appear healthy based on available metrics", "基于现有指标看起来健康"),
    ("Root cause remains undetermined", "根因仍未确定"),
    ("total requested CPU exceeding cluster allocatable capacity", "总 CPU 请求量超过集群可分配容量"),
    ("monitoring misconfiguration", "监控配置错误"),
    ("The specific cause", "具体原因"),
    ("cannot be determined from the available data", "无法从现有数据中确定"),
    ("Container registry", "容器镜像仓库"),
    ("container registry", "容器镜像仓库"),
    ("container initialization", "容器初始化"),
    ("critical failure signals", "关键故障信号"),
    ("runtime or infrastructure issue", "运行时或基础设施故障"),
    ("configuration problem", "配置问题"),
    ("configuration error", "配置错误"),
    ("target selection", "目标选择"),
    ("no pod is selected", "没有选中任何 Pod"),
    ("no service metric anomalies", "没有服务指标异常"),
    ("simultaneously", "同时"),
    ("temporal clustering", "时间聚集"),
    ("no service impact", "没有服务影响"),
)


def _knowledge_fault_type_zh(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return _KNOWLEDGE_FAULT_TYPE_ZH.get(text.lower(), text)


def _knowledge_text_zh(value: Any) -> str:
    """Deterministic Chinese projection for legacy English knowledge text.

    原始知识库内容不改写，避免影响向量检索和审计；这里只为 API/前端展示生成中文投影。
    """
    text = str(value or "")
    if not text:
        return ""
    translated = text
    for source, target in _KNOWLEDGE_TRANSLATIONS:
        translated = translated.replace(source, target)
    severity_map = {
        "[CRITICAL]": "[严重]",
        "[WARNING]": "[警告]",
        "[INFO]": "[提示]",
        "[NONE]": "[无级别]",
        "[critical]": "[严重]",
        "[warning]": "[警告]",
        "[none]": "[无级别]",
        "Alert context": "告警上下文",
        "Alert": "告警",
        "Failed:": "失败:",
        "Failed：": "失败：",
        "Service unavailable": "服务不可用",
        "health check": "健康检查",
        "alerts": "条告警",
        "alert": "告警",
    }
    for source, target in severity_map.items():
        translated = translated.replace(source, target)
    translated = re.sub(r"\bAND\b", "且", translated)
    translated = re.sub(r"\bwhile\b", "同时", translated)
    translated = re.sub(r"\blikely\b", "可能", translated)
    translated = re.sub(r"\bthe\b", "", translated, flags=re.I)
    translated = re.sub(r"\be\.g\.\s*,?", "例如", translated, flags=re.I)
    translated = re.sub(r"\bfor\b", "针对", translated, flags=re.I)
    translated = re.sub(r"\bwith\b", "伴随", translated, flags=re.I)
    translated = re.sub(r"\bin the ([A-Za-z0-9_.-]+) namespace\b", r"在 \1 命名空间中", translated, flags=re.I)
    translated = re.sub(r"\bin namespace ([A-Za-z0-9_.-]+)\b", r"在 \1 命名空间中", translated, flags=re.I)
    translated = re.sub(r"\bcausing\b", "导致", translated, flags=re.I)
    translated = re.sub(r"(?<![-\w])failure(?![-\w])", "失败", translated, flags=re.I)
    translated = re.sub(r"\bmetrics\b", "指标", translated, flags=re.I)
    translated = re.sub(r"\bservice\b", "服务", translated, flags=re.I)
    translated = re.sub(r"\bruntime\b", "运行时", translated, flags=re.I)
    translated = re.sub(r"\binfrastructure\b", "基础设施", translated, flags=re.I)
    translated = re.sub(r"\bconfiguration\b", "配置", translated, flags=re.I)
    translated = re.sub(r"\bselector\b", "选择器", translated, flags=re.I)
    translated = re.sub(r"\bno evidence\b", "没有证据", translated, flags=re.I)
    translated = re.sub(r"\bmissing\b", "缺失", translated, flags=re.I)
    translated = re.sub(r"\bunavailable\b", "不可用", translated, flags=re.I)
    translated = re.sub(r"\bhealthy\b", "健康", translated, flags=re.I)
    translated = re.sub(r"\bunknown\b", "未知", translated, flags=re.I)
    translated = re.sub(r"\s{2,}", " ", translated).strip()
    return translated


def _knowledge_rule_view(rule: Dict[str, Any]) -> Dict[str, Any]:
    from memory.fault_context_store import FaultContextStore
    from memory.rule_lifecycle import effective_status, history
    item = dict(rule)
    item["revision"] = FaultContextStore.rule_revision(rule)
    item['stored_status'] = rule.get('status', 'active')
    item['status'] = effective_status(rule)
    from memory.feedback_lifecycle import rule_block_reason
    item['source_block_reason'] = rule_block_reason(rule)
    item['lifecycle_events'] = len(history(rule))
    item.pop('lifecycle_history', None)
    item['independent_benefit_verified'] = False
    item["fault_type_zh"] = _knowledge_fault_type_zh(item.get("fault_type"))
    item["condition_zh"] = _knowledge_text_zh(item.get("condition") or item.get("text"))
    item["conclusion_zh"] = _knowledge_text_zh(item.get("conclusion"))
    item["text_zh"] = _knowledge_text_zh(item.get("text") or "")
    item["from_trace_zh"] = _knowledge_text_zh(item.get("from_trace") or "")
    return item


def _knowledge_fault_view(fault: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(fault)
    item["fault_type_zh"] = _knowledge_fault_type_zh(item.get("fault_type"))
    item["description_zh"] = _knowledge_text_zh(item.get("description") or item.get("text"))
    item["root_cause_zh"] = _knowledge_text_zh(item.get("root_cause"))
    item["resolution_zh"] = _knowledge_text_zh(item.get("resolution") or "")
    item["text_zh"] = _knowledge_text_zh(item.get("text") or "")
    return item


@app.get("/api/knowledge/rules")
async def knowledge_rules():
    """获取所有诊断规则"""
    store = _get_fault_store()
    try:
        return {"rules": [_knowledge_rule_view(r) for r in store.list_rules(limit=1000, raise_errors=True)]}
    except Exception:
        raise HTTPException(503, 'rule_storage_unavailable')


def _validate_rule_patch(body, *, editing=False):
    from memory.rule_lifecycle import EDITABLE, valid_rules
    allowed = EDITABLE | ({'revision'} if editing else set())
    if not isinstance(body, dict) or set(body) - allowed:
        raise HTTPException(422, '包含不可编辑字段')
    patch = {k: v for k, v in body.items() if k != 'revision'}
    if not valid_rules([patch]):
        raise HTTPException(422, '条件和结论不能为空；置信度须为0至1的有限数值')
    for key in ('name', 'fault_type', 'namespace', 'cluster_id', 'entity_uid', 'workload_version'):
        if key in patch and (not isinstance(patch[key], str) or len(patch[key]) > 8000):
            raise HTTPException(422, '规则字段必须为文本且不超过8000字符')
    from memory.memory_scope import epoch
    for key in ('valid_from', 'valid_to', 'review_expires_at'):
        if key in patch and epoch(patch[key]) is None:
            raise HTTPException(422, '规则有效期必须为有限的epoch秒')
    return patch


@app.post("/api/knowledge/rules")
async def knowledge_add_rule(request: Request):
    """添加诊断规则"""
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(422, 'invalid_rule_request')
    patch = _validate_rule_patch(body)
    try:
        store = _get_fault_store()
        receipt = await asyncio.to_thread(store.add_rule, {**patch, 'source': 'manual',
            'status': 'candidate', 'memory_status': 'candidate'}, return_receipt=True)
    except Exception:
        raise HTTPException(503, 'rule_storage_unavailable')
    return {"status": "ok", "rule_id": receipt['rule_id'], 'rule_status': receipt['effective_status'],
            'created': receipt['created'], 'revision': receipt['revision'], 'rules_activated': 0}


@app.put("/api/knowledge/rules/{rule_id}")
def knowledge_update_rule(rule_id: str, body: Dict[str, Any]):
    patch = _validate_rule_patch(body, editing=True)
    try:
        store = _get_fault_store()
        rule = store.update_rule(rule_id, patch, body.get("revision", ""))
    except KeyError:
        raise HTTPException(404, "规则不存在")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except Exception:
        raise HTTPException(503, 'rule_storage_unavailable')
    return {"status": "ok", "rule_id": rule_id, "revision": store.rule_revision(rule), 'rule_status': rule['status']}


@app.get('/api/knowledge/rules/{rule_id}/history')
def knowledge_rule_history(rule_id: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100), revision: str = ''):
    try:
        result = _get_fault_store().rule_history(rule_id, offset=offset, limit=limit)
        if revision and revision != result['revision']:
            raise HTTPException(409, 'rule_revision_conflict')
        return result
    except KeyError:
        raise HTTPException(404, 'rule_not_found')
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, 'rule_history_unavailable')


@app.post('/api/knowledge/rules/{rule_id}/decision')
def knowledge_rule_decision(rule_id: str, body: Dict[str, Any], request: Request):
    if set(body) - {'revision', 'decision', 'reviewer', 'reason', 'event_id'}:
        raise HTTPException(422, 'invalid_rule_review_fields')
    reviewer, identity = body.get('reviewer'), 'operator_label'
    session = _get_auth_session(request)
    if _auth_enabled() and session:
        reviewer, identity = session['username'], 'authenticated_session'
    elif _auth_enabled() and _bearer_token_valid(request):
        reviewer, identity = 'api-token', 'api_token'
    try:
        return _get_fault_store().decide_rule(rule_id, revision=body.get('revision'), decision=body.get('decision'),
            reviewer=reviewer, reason=body.get('reason'), event_id=body.get('event_id'), identity_source=identity)
    except KeyError:
        raise HTTPException(404, 'rule_not_found')
    except ValueError as exc:
        conflict = str(exc) in {'rule_revision_conflict', 'rule_event_content_conflict',
                               'invalid_rule_state_transition', 'rule_history_retention_limit'}
        raise HTTPException(409 if conflict else 422, str(exc))
    except Exception:
        raise HTTPException(503, 'rule_review_storage_unavailable')


@app.delete("/api/knowledge/rules/{rule_id}")
async def knowledge_delete_rule(rule_id: str):
    """删除诊断规则"""
    raise HTTPException(409, '请使用带版本和原因的停用审核，保留规则与历史记录')


@app.get("/api/knowledge/faults")
async def knowledge_faults():
    """获取所有历史故障记录"""
    store = _get_fault_store()
    if store._backend == "json":
        return {"faults": [_knowledge_fault_view(f) for f in store._faults_data]}
    else:
        try:
            result = store._faults_col.get(limit=500)
            faults = []
            for i, doc_id in enumerate(result.get("ids", [])):
                meta = result.get("metadatas", [])[i] if result.get("metadatas") else {}
                doc = result.get("documents", [])[i] if result.get("documents") else ""
                faults.append({**meta, "fault_id": doc_id, "text": doc})
            return {"faults": [_knowledge_fault_view(f) for f in faults]}
        except Exception as e:
            return {"faults": [], "error": str(e)}


@app.post("/api/knowledge/faults")
async def knowledge_add_fault(request: Request):
    """添加历史故障记录"""
    body = await request.json()
    store = _get_fault_store()
    fault_id = store.add_fault({
        "description": body.get("description", ""),
        "root_cause": body.get("root_cause", ""),
        "fault_type": body.get("fault_type", ""),
        "affected_services": body.get("affected_services", ""),
        "resolution": body.get("resolution", ""),
    })
    return {"status": "ok", "fault_id": fault_id}


@app.delete("/api/knowledge/faults/{fault_id}")
async def knowledge_delete_fault(fault_id: str):
    """删除故障记录"""
    try:
        store = _get_fault_store()
        await asyncio.to_thread(store.delete_fault, fault_id)
    except Exception:
        raise HTTPException(503, 'fault_storage_unavailable')
    return {"status": "ok"}


@app.get("/api/knowledge/search")
async def knowledge_search(q: str = ""):
    """搜索知识库"""
    if not q:
        return {"rules": [], "faults": []}
    store = _get_fault_store()
    rules = store.query_similar_rules(q, n=10)
    faults = store.query_similar_faults(q, n=10)
    return {
        "rules": [_knowledge_rule_view(r) for r in rules],
        "faults": [_knowledge_fault_view(f) for f in faults],
    }


@app.post("/api/knowledge/feedback")
async def knowledge_feedback(request: Request):
    """Record and consume a version-bound expert feedback event."""
    from memory.feedback_contract import FeedbackConflict, FeedbackStorageError
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(422, 'invalid_feedback_request')
    if not isinstance(body, dict):
        raise HTTPException(422, 'feedback_request_must_be_object')
    try:
        fb = _get_feedback_store()
        cfg = _get_config()
        learner = None
        if _llm_configured(cfg.llm):
            from memory import ContextLearner
            learner = ContextLearner(LLMClient(cfg.llm), _get_fault_store(), cfg)
        return await asyncio.to_thread(fb.submit_feedback,
            incident_id=body.get('incident_id', ''), expert_diagnosis=body.get('expert_diagnosis', ''),
            comment=body.get('comment', ''), context_learner=learner,
            trace_store=_get_trace_store(), fault_store=_get_fault_store(),
            event_id=body.get('event_id'), expected_diagnosis_revision=body.get('diagnosis_revision'),
            rule_feedback=body.get('rule_feedback'))
    except FeedbackConflict as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except (OSError, FeedbackStorageError):
        raise HTTPException(503, 'feedback_storage_unavailable')


@app.get("/api/knowledge/feedback")
async def knowledge_feedback_list():
    """获取反馈历史"""
    from memory.feedback_contract import FeedbackStorageError
    try:
        fb = _get_feedback_store()
        return {"feedback": fb.get_recent_feedback(n=50), "stats": fb.get_feedback_stats()}
    except (OSError, FeedbackStorageError):
        raise HTTPException(503, 'feedback_storage_unavailable')


@app.post('/api/knowledge/feedback/{feedback_id}/retract')
def knowledge_feedback_retract(feedback_id: str, body: Dict[str, Any], request: Request):
    from memory.feedback_contract import FeedbackConflict, FeedbackStorageError
    if set(body) - {'revision', 'reviewer', 'reason', 'event_id'}:
        raise HTTPException(422, 'invalid_feedback_retraction_fields')
    reviewer, identity = body.get('reviewer'), 'operator_label'
    session = _get_auth_session(request)
    if _auth_enabled() and session:
        reviewer, identity = session['username'], 'authenticated_session'
    elif _auth_enabled() and _bearer_token_valid(request):
        reviewer, identity = 'api-token', 'api_token'
    try:
        return _get_feedback_store().retract_feedback(feedback_id, revision=body.get('revision'),
            reviewer=reviewer, reason=body.get('reason'), event_id=body.get('event_id'), identity_source=identity)
    except KeyError:
        raise HTTPException(404, 'feedback_not_found')
    except FeedbackConflict as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except (OSError, FeedbackStorageError):
        raise HTTPException(503, 'feedback_retraction_storage_unavailable')


@app.get("/api/hitl/reviews")
async def hitl_reviews(status: str = "", limit: int = 50):
    """List human review queue items."""
    store = _get_human_review_store()
    return {
        "reviews": store.list_reviews(status=status, limit=limit),
        "stats": store.stats(),
    }


@app.post("/api/hitl/reviews/{review_id}/decision")
async def hitl_review_decision(review_id: str, request: Request):
    """Submit a human review decision and optionally trigger supervised learning."""
    from memory.feedback_contract import FeedbackConflict, FeedbackStorageError
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(422, 'invalid_review_request')
    if not isinstance(body, dict):
        raise HTTPException(422, 'review_request_must_be_object')
    cfg = _get_config()
    learner = None
    if _llm_configured(cfg.llm):
        from memory import ContextLearner
        learner = ContextLearner(LLMClient(cfg.llm), _get_fault_store(), cfg)

    try:
        review = await asyncio.to_thread(_get_human_review_store().decide_review,
            review_id=review_id,
            decision=body.get("decision", ""),
            reviewer=body.get("reviewer", ""),
            expert_diagnosis=body.get("expert_diagnosis", ""),
            comment=body.get("comment", ""),
            context_learner=learner,
            feedback_store=_get_feedback_store(),
            trace_store=_get_trace_store(),
            fault_store=_get_fault_store(),
        )
        return {"status": "ok", "review": review}
    except KeyError:
        raise HTTPException(404, "Review not found")
    except FeedbackConflict as exc:
        raise HTTPException(409, str(exc))
    except (OSError, FeedbackStorageError):
        raise HTTPException(503, 'review_storage_unavailable')
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/evolution/report")
async def evolution_report():
    """Return system evolution trends and recommendations."""
    tracker = _get_evolution_tracker()
    return tracker.get_evolution_report(fault_store=_get_fault_store())


# ─────────────────────────────────────────
# SOW Alignment & Fault Injection APIs
# ─────────────────────────────────────────

def _load_fault_scenarios() -> List[Dict[str, Any]]:
    """Load general scenarios, preferring an explicitly activated catalog version."""
    try:
        active = _fault_mode_registry().active_document("general")
        if isinstance(active, dict):
            return copy.deepcopy(active.get("scenarios", []))
    except FaultModeRegistryError as exc:
        logger.warning("Failed to load active general fault catalog: %s", exc)
    if not SCENARIOS_FILE.exists():
        return []
    try:
        data = yaml.safe_load(SCENARIOS_FILE.read_text(encoding="utf-8")) or {}
        return data.get("scenarios", [])
    except Exception as e:
        logger.warning("Failed to load fault scenarios: %s", e)
        return []


def _load_precision_fault_catalog() -> Dict[str, Any]:
    """Load the AgenticSRE-owned typed catalog; it never contains commands."""
    try:
        active = _fault_mode_registry().active_document("precision")
        if isinstance(active, dict):
            return copy.deepcopy(active)
    except FaultModeRegistryError as exc:
        logger.warning("Failed to load active precision fault catalog: %s", exc)
    if not PRECISION_FAULT_SCENARIOS_FILE.exists():
        return {}
    try:
        data = yaml.safe_load(PRECISION_FAULT_SCENARIOS_FILE.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Failed to load precision fault scenarios: %s", exc)
        return {}


def _load_precision_fault_scenarios() -> List[Dict[str, Any]]:
    catalog = _load_precision_fault_catalog()
    scenarios = catalog.get("scenarios") if isinstance(catalog.get("scenarios"), list) else []
    return [item for item in scenarios if isinstance(item, dict) and item.get("id")]


def _load_llm_fault_scenarios() -> List[Dict[str, Any]]:
    """Load vLLM inference fault scenarios, honoring an activated version."""
    scenarios: List[Dict[str, Any]] = []
    try:
        active = _fault_mode_registry().active_document("inference")
        if isinstance(active, dict):
            scenarios.extend(copy.deepcopy(active.get("scenarios", [])))
        else:
            if LLM_FAULT_SCENARIOS_FILE.exists():
                data = yaml.safe_load(LLM_FAULT_SCENARIOS_FILE.read_text(encoding="utf-8")) or {}
                scenarios.extend(data.get("scenarios", []))
            if LLM_FAULT_SCENARIOS_OVERLAY_FILE.exists():
                data = yaml.safe_load(LLM_FAULT_SCENARIOS_OVERLAY_FILE.read_text(encoding="utf-8")) or {}
                scenarios.extend(data.get("scenarios", []))

        seen = set()
        deduped: List[Dict[str, Any]] = []
        for scenario in scenarios:
            sid = scenario.get("id")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            symptoms = scenario.get("expected_symptoms") or []
            scenario["symptom_count"] = len(symptoms) if isinstance(symptoms, list) else 0
            scenario["experiment_family"] = "智算故障实验"
            scenario["applicable_targets"] = _llm_fault_applicable_targets(scenario)
            scenario["requires_risk_ack"] = _llm_fault_requires_risk_ack(
                str(scenario.get("fault_type") or ""),
                str(scenario.get("severity") or ""),
            )
            deduped.append(scenario)
        return deduped
    except Exception as e:
        logger.warning("Failed to load vLLM fault scenarios: %s", e)
        return []


def _llm_fault_tool_status() -> Dict[str, Any]:
    scenarios = _load_llm_fault_scenarios()
    layers = sorted({s.get("layer", "unknown") for s in scenarios})
    fault_types = sorted({s.get("fault_type", "unknown") for s in scenarios})
    return {
        "available": LLM_FAULT_INJECTOR_DIR.exists() and LLM_FAULT_SCENARIOS_FILE.exists(),
        "tool_dir": str(LLM_FAULT_INJECTOR_DIR),
        "scenarios_file": str(LLM_FAULT_SCENARIOS_FILE),
        "overlay_file": str(LLM_FAULT_SCENARIOS_OVERLAY_FILE),
        "scenario_count": len(scenarios),
        "layers": layers,
        "fault_types": fault_types,
        "design_reference": str(ROOT_DIR / "doc" / "智算推理场景运行时测试工具设计报告.docx"),
        "environment": _llm_fault_environment(),
    }


def _find_llm_fault_scenario(scenario_id: str) -> Optional[Dict[str, Any]]:
    """Look up one vLLM fault scenario by id."""
    for scenario in _load_llm_fault_scenarios():
        if scenario.get("id") == scenario_id:
            return scenario
    return None


def _llm_fault_required_params(scenario: Dict[str, Any], target_mode: str) -> List[Dict[str, str]]:
    """Return user-facing runtime parameters needed for an LLM fault scenario."""
    fault_type = str(scenario.get("fault_type") or "")
    params: List[Dict[str, str]] = []
    if target_mode == "host":
        params.append({"name": "host_id", "required": "true", "description": "GPU 裸机目标档案"})
        if fault_type in {"request_queue_overflow", "continuous_batch_disruption"}:
            params.extend([
                {"name": "endpoint", "required": "true", "description": "OpenAI-compatible completions endpoint"},
                {"name": "model", "required": "true", "description": "业务侧请求使用的模型名"},
                {"name": "concurrent", "required": "false", "description": "并发请求数，默认 16，建议演示 8-16、压测 32+"},
                {"name": "rounds", "required": "false", "description": "每个并发线程请求轮数，默认 2"},
                {"name": "prompt_len", "required": "false", "description": "压测 prompt 长度，默认 2048 字符"},
                {"name": "max_tokens", "required": "false", "description": "每个请求最大输出 token，默认 128"},
            ])
        if fault_type in {"packet_loss", "bandwidth_throttle"}:
            params.append({"name": "interface", "required": "true", "description": "注入 tc/netem 的网卡名"})
        if fault_type in {"tokenizer_error", "weight_corruption", "model_permission_error"}:
            params.append({"name": "model_dir", "required": "true", "description": "模型资产目录，避免默认路径不匹配"})
    else:
        params.extend([
            {"name": "cluster_id", "required": "false", "description": "K8s 集群档案"},
            {"name": "namespace", "required": "true", "description": "vLLM Deployment 命名空间"},
            {"name": "deployment", "required": "true", "description": "vLLM Deployment 名称"},
        ])
    return params


def _llm_fault_preflight(
    scenario: Dict[str, Any],
    body: Dict[str, Any],
    *,
    action: str = "inject",
) -> Dict[str, Any]:
    """Check whether one LLM fault scenario is executable for the selected target."""
    target_mode = str(body.get("target_mode") or "k8s")
    fault_type = str(scenario.get("fault_type") or "")
    blockers: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    parameters = _llm_fault_required_params(scenario, target_mode)
    supported = False
    command_preview = ""
    recovery_preview = ""
    layer = str(scenario.get("layer") or "")

    if target_mode not in {"k8s", "host"}:
        blockers.append({"field": "target_mode", "message": "target_mode must be k8s or host"})
    elif target_mode == "host":
        host_id = str(body.get("host_id") or "").strip()
        host = _find_llm_host(host_id)
        if not host_id:
            blockers.append({"field": "host_id", "message": "请选择 GPU 裸机目标"})
        elif not host:
            blockers.append({"field": "host_id", "message": f"GPU 主机档案不存在: {host_id}"})
        elif not _llm_host_address_configured(host):
            blockers.append({"field": "host_id", "message": f"GPU 主机尚未配置地址: {host_id}"})
        endpoint = str(body.get("endpoint") or "").strip()
        model = str(body.get("model") or "").strip()
        interface = str(body.get("interface") or "").strip()
        model_dir = str(body.get("model_dir") or "").strip()
        concurrent = _clamp_int(body.get("concurrent"), 16, 1, 128)
        rounds = _clamp_int(body.get("rounds"), 2, 1, 50)
        prompt_len = _clamp_int(body.get("prompt_len"), 2048, 16, 32768)
        max_tokens = _clamp_int(body.get("max_tokens"), 128, 1, 2048)
        if fault_type in {"request_queue_overflow", "continuous_batch_disruption"}:
            if not endpoint:
                blockers.append({"field": "endpoint", "message": "业务侧负载场景需要 endpoint"})
            if not model:
                blockers.append({"field": "model", "message": "业务侧负载场景需要模型名"})
        if fault_type in {"packet_loss", "bandwidth_throttle"} and not interface:
            blockers.append({"field": "interface", "message": "网络故障需要明确网卡名"})
        if fault_type in {"tokenizer_error", "weight_corruption", "model_permission_error"} and not model_dir:
            warnings.append({"field": "model_dir", "message": "未指定模型目录时会尝试 /model /models /data/models，可能找不到资产"})

        try:
            sys.path.insert(0, str(LLM_FAULT_INJECTOR_DIR.parent))
            from vllm_fault_injector.host_faults import build_host_fault_command

            built = build_host_fault_command(
                fault_type,
                action,
                dry_run=False,
                interface=interface or "eth0",
                endpoint=endpoint or "http://127.0.0.1:8000/v1/completions",
                model=model or "vllm",
                model_dir=model_dir,
                concurrent=concurrent,
                rounds=rounds,
                prompt_len=prompt_len,
                max_tokens=max_tokens,
            )
            supported = bool(built.supported)
            layer = built.layer or layer
            command_preview = built.command
            recovery_preview = built.recovery_command
            if not built.supported:
                blockers.append({"field": "fault_type", "message": built.message})
        except Exception as e:
            blockers.append({"field": "tool", "message": f"host fault builder unavailable: {e}"})
    else:
        status = _llm_fault_tool_status()
        if not status.get("available"):
            blockers.append({"field": "tool", "message": "vLLM fault injector tool not found"})
        cluster_id = body.get("cluster_id") or None
        if cluster_id and not _find_k8s_cluster(cluster_id):
            warnings.append({"field": "cluster_id", "message": f"集群档案不存在，将使用默认 kubectl: {cluster_id}"})
        namespace = str(body.get("namespace") or "").strip()
        deployment = str(body.get("deployment") or "").strip()
        if not namespace:
            blockers.append({"field": "namespace", "message": "需要 namespace"})
        if not deployment:
            blockers.append({"field": "deployment", "message": "需要 deployment"})
        if fault_type not in set(status.get("fault_types") or []):
            blockers.append({"field": "fault_type", "message": f"K8s injector does not support {fault_type}"})
        supported = not any(b.get("field") in {"tool", "fault_type"} for b in blockers)
        if supported and namespace and deployment:
            try:
                sys.path.insert(0, str(LLM_FAULT_INJECTOR_DIR.parent))
                from vllm_fault_injector.injector import FaultInjector

                service = str(body.get("service") or deployment).strip() or deployment
                preview_target = _llm_fault_injector_target(fault_type, deployment, service)

                async def build_preview():
                    injector = FaultInjector(
                        kubectl_cmd=_build_kubectl_cmd(cluster_id),
                        namespace=namespace,
                        deployment=preview_target,
                        dry_run=True,
                    )
                    if action == "cleanup":
                        results = await injector.recover(
                            layer if layer in {"software", "driver", "network", "os"} else "all"
                        )
                        commands = [
                            str(item.command or "")
                            for item in results
                            if getattr(item, "command", None)
                        ]
                        return "\n".join(commands), ""
                    result = await injector.inject(fault_type)
                    return str(result.command or ""), str(result.recovery_cmd or "")

                command_preview, recovery_preview = asyncio.run(build_preview())
            except Exception as exc:
                blockers.append({"field": "preview", "message": f"无法生成 K8s 命令预览: {exc}"})

    high_risk = _llm_fault_requires_risk_ack(fault_type, str(scenario.get("severity") or ""))
    if not bool(body.get("dry_run", True)):
        if not bool(body.get("confirm")):
            blockers.append({"field": "confirm", "message": "真实执行需要 confirm=true"})
        if action == "experiment" and not bool(body.get("experiment_ack")):
            blockers.append({"field": "experiment_ack", "message": "真实实验需要 experiment_ack=true"})
        if target_mode == "host" and not bool(body.get("host_ack")):
            blockers.append({"field": "host_ack", "message": "裸机真实执行需要 host_ack=true"})
        if high_risk and not bool(body.get("risk_ack")):
            blockers.append({"field": "risk_ack", "message": "高风险场景需要 risk_ack=true"})

    return {
        "ok": supported and not blockers,
        "supported": supported,
        "target_mode": target_mode,
        "action": action,
        "scenario_id": scenario.get("id"),
        "fault_type": fault_type,
        "layer": layer,
        "severity": scenario.get("severity"),
        "high_risk": high_risk,
        "parameters": parameters,
        "blockers": blockers,
        "warnings": warnings,
        "command_preview": command_preview,
        "recovery_preview": recovery_preview,
        "observability": {
            "mode": "host_direct" if target_mode == "host" else "prometheus_or_vllm_metrics",
            "requires_prometheus": target_mode != "host",
            "signals": _llm_fault_observation_hints(scenario, target_mode),
        },
    }


def _find_k8s_fault_scenario(scenario_id: str) -> Optional[Dict[str, Any]]:
    """Look up one general-compute K8s fault scenario by id."""
    for scenario in _load_fault_scenarios():
        if scenario.get("id") == scenario_id:
            return scenario
    return None


def _normalize_fault_experiment_body(body: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize a live fault benchmark request."""
    steps = body.get("steps")
    if not isinstance(steps, list) or not steps:
        raise HTTPException(400, "steps must be a non-empty list")
    if len(steps) > 50:
        raise HTTPException(400, "steps is limited to 50 entries")

    dry_run = bool(body.get("dry_run", True))
    benchmark_type = body.get("benchmark_type") or body.get("scenario_family") or "llm"
    if benchmark_type not in {"llm", "k8s"}:
        raise HTTPException(400, "benchmark_type must be 'llm' or 'k8s'")
    target_mode = body.get("target_mode", "k8s")
    if target_mode not in {"k8s", "host"}:
        raise HTTPException(400, "target_mode must be 'k8s' or 'host'")
    if benchmark_type == "k8s":
        target_mode = "k8s"

    total_duration_s = int(body.get("total_duration_s") or 0)
    max_total_duration_s = int(body.get("max_total_duration_s") or 24 * 3600)
    if total_duration_s < 0 or max_total_duration_s <= 0:
        raise HTTPException(400, "duration values must be non-negative")
    if total_duration_s > max_total_duration_s:
        raise HTTPException(400, "total_duration_s exceeds max_total_duration_s")

    common = {
        "dry_run": dry_run,
        "target_mode": target_mode,
        "cluster_id": body.get("cluster_id") or None,
        "namespace": body.get("namespace") or "default",
        "deployment": body.get("deployment") or "vllm-server",
        "service": body.get("service") or body.get("deployment") or "vllm-server",
        "host_id": body.get("host_id") or "",
        "confirm": bool(body.get("confirm")),
        "risk_ack": bool(body.get("risk_ack")),
        "host_ack": bool(body.get("host_ack")),
        "experiment_ack": bool(body.get("experiment_ack")),
        "baseline_samples": int(body.get("baseline_samples", 2)),
        "fault_samples": int(body.get("fault_samples", 3)),
        "sample_interval": float(body.get("sample_interval", 3.0)),
        "background_load": bool(body.get("background_load")),
        "loadgen_duration_s": _clamp_int(body.get("loadgen_duration_s", 180), 180, 10, 7200),
        "loadgen_concurrency": _clamp_int(body.get("loadgen_concurrency", body.get("concurrent", 8)), 8, 1, 256),
        "loadgen_prompt_len": _clamp_int(body.get("loadgen_prompt_len", body.get("prompt_len", 2048)), 2048, 16, 32768),
        "loadgen_max_tokens": _clamp_int(body.get("loadgen_max_tokens", body.get("max_tokens", 128)), 128, 1, 4096),
        "loadgen_timeout_s": _clamp_int(body.get("loadgen_timeout_s", 60), 60, 5, 600),
        "loadgen_qps": max(0.0, _to_float(body.get("loadgen_qps", 0.0), 0.0)),
        "loadgen_image": body.get("loadgen_image") or "",
        "loadgen_endpoint": body.get("loadgen_endpoint") or "",
        "interface": body.get("interface") or "eth0",
        "endpoint": body.get("endpoint") or "http://127.0.0.1:8000/v1/completions",
        "model": body.get("model") or "vllm",
        "model_dir": body.get("model_dir") or "",
    }

    if not dry_run and (not common["confirm"] or not common["experiment_ack"]):
        raise HTTPException(
            400,
            "Real live benchmark execution requires confirm=true and experiment_ack=true.",
        )
    if target_mode == "host" and not dry_run and not common["host_ack"]:
        raise HTTPException(400, "Bare-metal benchmark execution requires host_ack=true.")

    normalized_steps = []
    for idx, raw_step in enumerate(steps, start=1):
        if not isinstance(raw_step, dict):
            raise HTTPException(400, f"step {idx} must be an object")
        scenario_id = raw_step.get("scenario_id") or raw_step.get("id")
        if not scenario_id:
            raise HTTPException(400, f"step {idx} missing scenario_id")
        scenario = _find_k8s_fault_scenario(str(scenario_id)) if benchmark_type == "k8s" else _find_llm_fault_scenario(str(scenario_id))
        if not scenario:
            raise HTTPException(404, f"scenario '{scenario_id}' not found")
        action = raw_step.get("action") or "experiment"
        if action not in {"experiment", "inject"}:
            raise HTTPException(400, f"step {idx} action must be 'experiment' or 'inject'")
        hold_s = int(raw_step.get("hold_s", raw_step.get("duration_s", 30)))
        wait_after_s = int(raw_step.get("wait_after_s", body.get("default_wait_after_s", 5)))
        if hold_s < 0 or wait_after_s < 0:
            raise HTTPException(400, f"step {idx} duration values must be non-negative")
        fault_type = scenario.get("fault_type")
        if not dry_run and benchmark_type == "llm" and _llm_fault_requires_risk_ack(
            str(fault_type or ""),
            str(scenario.get("severity") or ""),
        ) and not common["risk_ack"]:
            raise HTTPException(
                400,
                f"Step {idx} fault type '{fault_type}' is high risk and requires risk_ack=true.",
            )
        normalized_steps.append({
            "index": idx,
            "benchmark_type": benchmark_type,
            "scenario_id": str(scenario_id),
            "scenario_name": scenario.get("name"),
            "fault_type": fault_type,
            "layer": scenario.get("layer"),
            "category": scenario.get("category"),
            "action": action,
            "hold_s": hold_s,
            "wait_after_s": wait_after_s,
            "baseline_samples": int(raw_step.get("baseline_samples", common["baseline_samples"])),
            "fault_samples": int(raw_step.get("fault_samples", common["fault_samples"])),
            "sample_interval": float(raw_step.get("sample_interval", common["sample_interval"])),
        })
    return {
        "name": body.get("name") or f"Live Fault Benchmark {time.strftime('%Y%m%d-%H%M%S')}",
        "business_system": body.get("business_system") or ("social-network" if benchmark_type == "k8s" else "llm-inference"),
        "benchmark_type": benchmark_type,
        "dry_run": dry_run,
        "target_mode": target_mode,
        "total_duration_s": total_duration_s,
        "max_total_duration_s": max_total_duration_s,
        "workload": body.get("workload") or {"type": "external", "enabled": False},
        "collector": body.get("collector") or {"prometheus": True, "vllm_metrics": True},
        "common_body": common,
        "steps": normalized_steps,
    }


async def _sleep_with_cancel(seconds: int, cancel_event: asyncio.Event, run: Dict[str, Any]) -> None:
    """Sleep in small increments so a running benchmark can be cancelled."""
    deadline = time.time() + max(0, seconds)
    while time.time() < deadline:
        if cancel_event.is_set():
            run["cancel_requested"] = True
            raise asyncio.CancelledError()
        await asyncio.sleep(min(1.0, max(0.0, deadline - time.time())))


async def _run_k8s_faultlab_action(
    action: str,
    scenario: Dict[str, Any],
    body: Dict[str, Any],
) -> Dict[str, Any]:
    """Run a general-compute K8s faultlab action with dry-run safety by default."""
    if action not in {"inject", "cleanup"}:
        raise HTTPException(400, "K8s live benchmark action must be inject or cleanup")
    dry_run = bool(body.get("dry_run", True))
    cluster_id = body.get("cluster_id") or None
    namespace_override = (body.get("namespace") or "").strip() or None
    commands_key = "commands" if action == "inject" else "cleanup"
    namespace = namespace_override or scenario.get("inject", {}).get("namespace") or "default"
    guarded_plan = _faultlab_guarded_skill_plan(
        scenario,
        cluster_id=cluster_id,
        namespace=namespace,
        duration_s=int(body.get("hold_s") or body.get("duration_s") or 30),
    )
    if not guarded_plan.get("ready"):
        raise HTTPException(400, {"message": "K8s benchmark safety plan is blocked", "guarded_plan": guarded_plan})
    if not dry_run and not bool(body.get("confirm")):
        raise HTTPException(400, "Real K8s benchmark execution requires confirm=true")
    if not dry_run and not bool(body.get("experiment_ack")):
        raise HTTPException(400, "Real K8s benchmark execution requires experiment_ack=true")
    commands = _prepare_faultlab_commands(
        scenario,
        cluster_id,
        namespace_override,
        commands_key,
        bool(body.get("background_load")) and action == "inject",
    )
    payload = {
        "status": "dry_run" if dry_run else "executed",
        "action": action,
        "benchmark_type": "k8s",
        "scenario": scenario,
        "cluster_id": cluster_id,
        "namespace": namespace,
        "commands": commands,
        "guarded_plan": guarded_plan,
        "transaction_undo": guarded_plan.get("rollback") if action == "cleanup" else {},
        "verification_plan": guarded_plan.get("verification") if action == "cleanup" else {},
    }
    if dry_run:
        return payload
    results = [await asyncio.to_thread(_shell_sync, cmd, 120) for cmd in commands]
    payload["results"] = results
    payload["success"] = all(r.get("ok") for r in results)
    return payload


async def _execute_fault_experiment(run_id: str) -> None:
    """Run a live fault benchmark timeline in the background."""
    records = _load_fault_experiments()
    run = next((r for r in records if r.get("id") == run_id), None)
    if not run:
        return

    cancel_event = _state["fault_experiment_cancel"].setdefault(run_id, asyncio.Event())
    spec = run["spec"]
    started_at = time.time()
    run.update({
        "status": "running",
        "started_at": started_at,
        "timeline": run.get("timeline") or [],
        "results": run.get("results") or [],
    })
    _save_fault_experiment(run)

    async def add_event(phase: str, message: str, **extra: Any) -> None:
        run["timeline"].append({
            "ts": time.time(),
            "phase": phase,
            "message": message,
            **extra,
        })
        _save_fault_experiment(run)

    try:
        await add_event("start", "live benchmark started", dry_run=spec["dry_run"])
        for step in spec["steps"]:
            if cancel_event.is_set():
                raise asyncio.CancelledError()
            benchmark_type = step.get("benchmark_type") or spec.get("benchmark_type", "llm")
            scenario = _find_k8s_fault_scenario(step["scenario_id"]) if benchmark_type == "k8s" else _find_llm_fault_scenario(step["scenario_id"])
            if not scenario:
                raise RuntimeError(f"scenario '{step['scenario_id']}' not found")

            step_body = {
                **spec["common_body"],
                "baseline_samples": step["baseline_samples"],
                "fault_samples": step["fault_samples"],
                "sample_interval": step["sample_interval"],
            }
            step_started = time.time()
            await add_event(
                "step_start",
                f"step {step['index']} {step['action']} {step['scenario_id']}",
                step=step,
            )

            if step["action"] == "experiment":
                if benchmark_type == "k8s":
                    inject_result = await _run_k8s_faultlab_action("inject", scenario, step_body)
                    await add_event("hold", f"holding injected fault for {step['hold_s']}s", step=step)
                    try:
                        await _sleep_with_cancel(step["hold_s"], cancel_event, run)
                    finally:
                        cleanup_result = await _run_k8s_faultlab_action("cleanup", scenario, step_body)
                    result = {
                        "status": "dry_run" if spec["dry_run"] else "executed",
                        "benchmark_type": "k8s",
                        "inject_result": inject_result,
                        "cleanup_result": cleanup_result,
                    }
                else:
                    result = await _run_llm_fault("experiment", scenario, step_body)
                run["results"].append({
                    "step": step,
                    "action": "experiment",
                    "started_at": step_started,
                    "finished_at": time.time(),
                    "result": result,
                })
            else:
                inject_result = await (_run_k8s_faultlab_action("inject", scenario, step_body) if benchmark_type == "k8s" else _run_llm_fault("inject", scenario, step_body))
                await add_event("hold", f"holding injected fault for {step['hold_s']}s", step=step)
                cleanup_result: Dict[str, Any]
                try:
                    await _sleep_with_cancel(step["hold_s"], cancel_event, run)
                finally:
                    cleanup_result = await (_run_k8s_faultlab_action("cleanup", scenario, step_body) if benchmark_type == "k8s" else _run_llm_fault("cleanup", scenario, step_body))
                run["results"].append({
                    "step": step,
                    "action": "inject",
                    "started_at": step_started,
                    "finished_at": time.time(),
                    "inject_result": inject_result,
                    "cleanup_result": cleanup_result,
                })

            await add_event(
                "step_done",
                f"step {step['index']} completed",
                step_index=step["index"],
                duration_s=round(time.time() - step_started, 3),
            )
            await _sleep_with_cancel(step["wait_after_s"], cancel_event, run)

            if spec["total_duration_s"] and time.time() - started_at >= spec["total_duration_s"]:
                await add_event("time_limit", "total_duration_s reached")
                break

        run["status"] = "completed"
        await add_event("completed", "live benchmark completed")
    except asyncio.CancelledError:
        run["status"] = "cancelled"
        await add_event("cancelled", "live benchmark cancelled")
    except Exception as e:
        logger.exception("Fault experiment failed")
        run["status"] = "failed"
        run["error"] = str(e)
        await add_event("failed", str(e))
    finally:
        run["finished_at"] = time.time()
        run["duration_s"] = round(run["finished_at"] - started_at, 3)
        artifact_path = _write_fault_experiment_artifact(run)
        if artifact_path:
            run["artifact_path"] = artifact_path
        _save_fault_experiment(run)
        _state["fault_experiment_tasks"].pop(run_id, None)
        _state["fault_experiment_cancel"].pop(run_id, None)


def _build_kubectl_cmd(cluster_id: Optional[str] = None) -> str:
    """Return kubectl command usable by the vLLM injector.

    When cluster_id is provided, includes --kubeconfig / --context flags so the
    command targets that cluster profile instead of the container default.
    """
    cfg = _get_config()
    cluster = _find_k8s_cluster(cluster_id) if cluster_id else None
    kubectl_path = _cluster_kubectl_path(cluster)
    flags = ""
    if cluster:
        kubeconfig = _cluster_kubeconfig_path(cluster)
        if kubeconfig:
            flags += f" --kubeconfig={shlex.quote(kubeconfig)}"
        if cluster.get("context"):
            flags += f" --context={shlex.quote(cluster['context'])}"
    kubectl = f"{shlex.quote(kubectl_path)}{flags}"
    if cfg.kubernetes.use_ssh and cfg.kubernetes.ssh_jump_host:
        ssh_target = cfg.kubernetes.ssh_target or cfg.kubernetes.target_host
        return (
            f"ssh -J {shlex.quote(cfg.kubernetes.ssh_jump_host)} "
            f"{shlex.quote(ssh_target)} {kubectl}"
        )
    return kubectl


def _vllm_loadgen_python() -> str:
    """Small stdlib-only OpenAI completions load generator for in-cluster Pods."""
    return r'''
import argparse
import json
import queue
import statistics
import threading
import time
import urllib.error
import urllib.request

parser = argparse.ArgumentParser(description="vLLM OpenAI-compatible load generator")
parser.add_argument("--endpoint", required=True)
parser.add_argument("--model", required=True)
parser.add_argument("--duration-s", type=int, default=180)
parser.add_argument("--concurrency", type=int, default=8)
parser.add_argument("--prompt-len", type=int, default=2048)
parser.add_argument("--max-tokens", type=int, default=128)
parser.add_argument("--timeout-s", type=int, default=60)
parser.add_argument("--qps", type=float, default=0.0)
args = parser.parse_args()

deadline = time.time() + max(1, args.duration_s)
results = queue.Queue()
prompt = "A" * max(1, args.prompt_len)
payload = json.dumps({"model": args.model, "prompt": prompt, "max_tokens": max(1, args.max_tokens)}).encode("utf-8")
headers = {"Content-Type": "application/json"}
interval = (args.concurrency / args.qps) if args.qps and args.qps > 0 else 0

def worker(worker_id):
    count = 0
    while time.time() < deadline:
        started = time.time()
        status = 0
        err = ""
        try:
            req = urllib.request.Request(args.endpoint, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=max(1, args.timeout_s)) as resp:
                status = getattr(resp, "status", 200)
                resp.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            err = str(exc)
        except Exception as exc:
            err = str(exc)
        latency = time.time() - started
        results.put({"ok": 200 <= int(status or 0) < 300, "status": status, "latency": latency, "error": err[:160]})
        count += 1
        if interval > 0:
            time.sleep(min(interval, max(0, deadline - time.time())))
    results.put({"worker_done": worker_id, "requests": count})

threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(max(1, args.concurrency))]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()

items = []
workers = []
while not results.empty():
    item = results.get()
    if item.get("worker_done") is not None:
        workers.append(item)
    else:
        items.append(item)
latencies = [x["latency"] for x in items if x.get("ok")]
errors = [x for x in items if not x.get("ok")]
summary = {
    "endpoint": args.endpoint,
    "model": args.model,
    "duration_s": args.duration_s,
    "concurrency": args.concurrency,
    "prompt_len": args.prompt_len,
    "max_tokens": args.max_tokens,
    "requests": len(items),
    "success": len(items) - len(errors),
    "errors": len(errors),
    "rps": round(len(items) / max(1, args.duration_s), 3),
    "latency_p50_s": round(statistics.median(latencies), 4) if latencies else None,
    "latency_p95_s": round(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], 4) if latencies else None,
    "sample_errors": errors[:5],
    "workers": workers,
}
print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
'''.strip()


def _http_loadgen_python() -> str:
    """Stdlib-only HTTP load generator used by general-compute clusters."""
    return r'''
import argparse
import json
import queue
import statistics
import threading
import time
import urllib.error
import urllib.request

parser = argparse.ArgumentParser(description="bounded HTTP load generator")
parser.add_argument("--endpoint", required=True)
parser.add_argument("--method", choices=("GET", "POST", "PUT", "PATCH", "DELETE"), default="GET")
parser.add_argument("--body", default="")
parser.add_argument("--content-type", default="application/json")
parser.add_argument("--duration-s", type=int, default=180)
parser.add_argument("--concurrency", type=int, default=8)
parser.add_argument("--timeout-s", type=int, default=10)
parser.add_argument("--qps", type=float, default=0.0)
args = parser.parse_args()

deadline = time.time() + max(1, args.duration_s)
results = queue.Queue()
payload = args.body.encode("utf-8") if args.body and args.method != "GET" else None
headers = {"Content-Type": args.content_type} if payload is not None else {}
interval = (args.concurrency / args.qps) if args.qps and args.qps > 0 else 0

def worker(worker_id):
    count = 0
    while time.time() < deadline:
        started = time.time()
        status = 0
        err = ""
        try:
            req = urllib.request.Request(args.endpoint, data=payload, headers=headers, method=args.method)
            with urllib.request.urlopen(req, timeout=max(1, args.timeout_s)) as resp:
                status = getattr(resp, "status", 200)
                resp.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            err = str(exc)
        except Exception as exc:
            err = str(exc)
        results.put({
            "ok": 200 <= int(status or 0) < 400,
            "status": status,
            "latency": time.time() - started,
            "error": err[:160],
        })
        count += 1
        if interval > 0:
            time.sleep(min(interval, max(0, deadline - time.time())))
    results.put({"worker_done": worker_id, "requests": count})

threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(max(1, args.concurrency))]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()

items = []
while not results.empty():
    item = results.get()
    if item.get("worker_done") is None:
        items.append(item)
latencies = [item["latency"] for item in items if item.get("ok")]
errors = [item for item in items if not item.get("ok")]
summary = {
    "endpoint": args.endpoint,
    "method": args.method,
    "duration_s": args.duration_s,
    "concurrency": args.concurrency,
    "requests": len(items),
    "success": len(items) - len(errors),
    "errors": len(errors),
    "rps": round(len(items) / max(1, args.duration_s), 3),
    "latency_p50_s": round(statistics.median(latencies), 4) if latencies else None,
    "latency_p95_s": round(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], 4) if latencies else None,
    "sample_errors": errors[:5],
}
print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
'''.strip()


def _validate_loadgen_target(namespace: str, service: str, port: int) -> None:
    k8s_name = re.compile(r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$")
    if not k8s_name.fullmatch(namespace) or not k8s_name.fullmatch(service):
        raise HTTPException(400, "namespace and service must be valid Kubernetes DNS names")
    if port < 1 or port > 65535:
        raise HTTPException(400, "service port must be between 1 and 65535")


def _load_loadgen_benchmarks() -> List[Dict[str, Any]]:
    path = ROOT_DIR / "configs" / "loadgen_benchmarks.yaml"
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("Failed to load load-generator benchmark catalog: %s", exc)
        return []
    benchmarks = payload.get("benchmarks") or []
    return [item for item in benchmarks if isinstance(item, dict) and item.get("id")]


async def _discover_loadgen_benchmarks(cluster_id: str) -> List[Dict[str, Any]]:
    raw = await _kubectl_json("get services --all-namespaces", cluster_id=cluster_id)
    deployed = {
        (str(item.get("metadata", {}).get("namespace") or ""), str(item.get("metadata", {}).get("name") or ""))
        for item in raw.get("items") or []
        if isinstance(item, dict)
    }
    discovered = []
    for benchmark in _load_loadgen_benchmarks():
        defaults = benchmark.get("defaults") if isinstance(benchmark.get("defaults"), dict) else {}
        namespace = str(defaults.get("namespace") or "default")
        discovery = benchmark.get("discovery") if isinstance(benchmark.get("discovery"), dict) else {}
        required = [str(name) for name in discovery.get("required_services") or [] if name]
        available = bool(required) and all((namespace, name) in deployed for name in required)
        discovered.append({
            "id": str(benchmark.get("id") or ""),
            "name": str(benchmark.get("name") or benchmark.get("id") or ""),
            "description": str(benchmark.get("description") or ""),
            "available": available,
            "missing_services": [name for name in required if (namespace, name) not in deployed],
            "namespace": namespace,
            "service": str(defaults.get("service") or ""),
            "port": _clamp_int(defaults.get("port", 80), 80, 1, 65535),
            "trace_backend": str(defaults.get("trace_backend") or ""),
            "trace_service": str(defaults.get("trace_service") or defaults.get("service") or ""),
            "scenarios": [
                {
                    "id": str(scenario.get("id") or ""),
                    "name": str(scenario.get("name") or scenario.get("id") or ""),
                    "description": str(scenario.get("description") or ""),
                    "method": str(scenario.get("method") or "GET").upper(),
                    "path": str(scenario.get("path") or "/"),
                }
                for scenario in benchmark.get("scenarios") or []
                if isinstance(scenario, dict) and scenario.get("id")
            ],
        })
    return discovered


def _loadgen_benchmark_selection(
    benchmarks: List[Dict[str, Any]],
    benchmark_id: str,
    scenario_id: str,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    benchmark = next((item for item in benchmarks if item.get("id") == benchmark_id), None)
    if not benchmark:
        raise HTTPException(400, f"unknown load benchmark: {benchmark_id}")
    if not benchmark.get("available"):
        missing = ", ".join(benchmark.get("missing_services") or []) or "required services"
        raise HTTPException(409, f"benchmark '{benchmark_id}' is not deployed; missing: {missing}")
    scenarios = benchmark.get("scenarios") or []
    scenario = next((item for item in scenarios if item.get("id") == scenario_id), None)
    if not scenario:
        raise HTTPException(400, f"unknown benchmark scenario: {scenario_id}")
    return benchmark, scenario


def _build_http_k8s_loadgen_plan(
    body: Dict[str, Any],
    *,
    cluster_id: str,
    namespace: str,
    service: str,
    port: int,
) -> Dict[str, Any]:
    """Build a bounded, independently stoppable HTTP load task."""
    _validate_loadgen_target(namespace, service, port)
    method = str(body.get("method") or "GET").strip().upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise HTTPException(400, f"unsupported HTTP method: {method}")
    path = str(body.get("path") or "/").strip() or "/"
    if not path.startswith("/") or any(ch in path for ch in ("\r", "\n", " ")):
        raise HTTPException(400, "request path must start with '/' and contain no whitespace")
    duration_s = _clamp_int(body.get("duration_s", 180), 180, 10, 7200)
    concurrency = _clamp_int(body.get("concurrency", 8), 8, 1, 256)
    timeout_s = _clamp_int(body.get("timeout_s", 10), 10, 1, 600)
    qps = max(0.0, _to_float(body.get("qps", 0.0), 0.0))
    request_body = str(body.get("body") or "")
    content_type = str(body.get("content_type") or "application/json").strip() or "application/json"
    cluster = _find_k8s_cluster(cluster_id) or {}
    image = str(body.get("image") or cluster.get("loadgen_image") or "python:3.11-alpine").strip()
    run_id = _k8s_label_value(body.get("run_id") or f"http-{uuid.uuid4().hex[:12]}")
    name = f"agenticsre-http-load-{run_id}"[:56].rstrip("-")
    script_cm = f"{name}-script"[:63].rstrip("-")
    endpoint = f"http://{service}.{namespace}.svc.cluster.local:{port}{path}"
    selector = f"app=agenticsre-http-loadgen,agenticsre.io/run-id={run_id}"
    labels = [
        "app=agenticsre-http-loadgen",
        f"agenticsre.io/run-id={run_id}",
        f"agenticsre.io/service={_k8s_label_value(service)}",
    ]
    if body.get("benchmark_id"):
        labels.append(f"agenticsre.io/benchmark={_k8s_label_value(body.get('benchmark_id'))}")
    if body.get("scenario_id"):
        labels.append(f"agenticsre.io/scenario={_k8s_label_value(body.get('scenario_id'))}")
    kubectl = _build_kubectl_cmd(cluster_id)
    namespace_q = shlex.quote(namespace)
    args = [
        "--endpoint", endpoint,
        "--method", method,
        "--duration-s", str(duration_s),
        "--concurrency", str(concurrency),
        "--timeout-s", str(timeout_s),
    ]
    if request_body and method != "GET":
        args.extend(["--body", request_body, "--content-type", content_type])
    if qps > 0:
        args.extend(["--qps", str(qps)])
    overrides = {
        "spec": {
            "restartPolicy": "Never",
            "containers": [{
                "name": "loadgen",
                "image": image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["python3", "/opt/loadgen/loadgen.py"],
                "args": args,
                "volumeMounts": [{"name": "script", "mountPath": "/opt/loadgen"}],
            }],
            "volumes": [{"name": "script", "configMap": {"name": script_cm}}],
        }
    }
    cleanup = (
        f"{kubectl} -n {namespace_q} delete pod,configmap "
        f"-l {shlex.quote(selector)} --ignore-not-found=true"
    )
    create_cm = (
        f"{kubectl} -n {namespace_q} create configmap {shlex.quote(script_cm)} "
        f"--from-literal=loadgen.py={shlex.quote(_http_loadgen_python())}"
    )
    label_cm = (
        f"{kubectl} -n {namespace_q} label configmap {shlex.quote(script_cm)} "
        + " ".join(shlex.quote(label) for label in labels)
        + " --overwrite"
    )
    run = (
        f"{kubectl} -n {namespace_q} run {shlex.quote(name)} --restart=Never "
        f"--image={shlex.quote(image)} --labels={shlex.quote(','.join(labels))} "
        f"--overrides={shlex.quote(json.dumps(overrides, ensure_ascii=False, separators=(',', ':')))}"
    )
    return {
        "enabled": True,
        "kind": "http",
        "run_id": run_id,
        "cluster_id": cluster_id,
        "namespace": namespace,
        "service": service,
        "port": port,
        "endpoint": endpoint,
        "method": method,
        "duration_s": duration_s,
        "concurrency": concurrency,
        "timeout_s": timeout_s,
        "qps": qps,
        "image": image,
        "pod": name,
        "commands": [create_cm, label_cm, run],
        "cleanup_commands": [cleanup],
    }


def _loadgen_pod_records(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    records = []
    for pod in payload.get("items") or []:
        metadata = pod.get("metadata") or {}
        status = pod.get("status") or {}
        labels = metadata.get("labels") or {}
        container_statuses = status.get("containerStatuses") or []
        state = (container_statuses[0].get("state") if container_statuses else {}) or {}
        detail = next((key for key in ("running", "waiting", "terminated") if state.get(key)), "")
        records.append({
            "run_id": labels.get("agenticsre.io/run-id") or metadata.get("name", ""),
            "name": metadata.get("name", ""),
            "namespace": metadata.get("namespace", ""),
            "service": labels.get("agenticsre.io/service", ""),
            "deployment": labels.get("agenticsre.io/deployment", ""),
            "benchmark_id": labels.get("agenticsre.io/benchmark", ""),
            "scenario_id": labels.get("agenticsre.io/scenario", ""),
            "model": labels.get("agenticsre.io/model", ""),
            "phase": status.get("phase", "Unknown"),
            "state": detail,
            "started_at": status.get("startTime") or metadata.get("creationTimestamp", ""),
            "node": pod.get("spec", {}).get("nodeName", ""),
        })
    return records


async def _list_loadgen_pods(cluster_id: str, namespace: str, app_label: str) -> Dict[str, Any]:
    namespace_q = shlex.quote(namespace)
    result = await asyncio.to_thread(
        _shell_sync,
        f"kubectl -n {namespace_q} get pod -l {shlex.quote(f'app={app_label}')} -o json",
        60,
        cluster_id,
    )
    raw = result.get("stdout") or "{}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {}
    return {"ok": bool(result.get("ok")), "runs": _loadgen_pod_records(payload), "error": result.get("stderr", "")}


@app.post("/api/loadgen/general/{cluster_id}")
async def start_general_loadgen(cluster_id: str, request: Request):
    cluster = _find_k8s_cluster(cluster_id)
    if not cluster:
        raise HTTPException(404, f"cluster '{cluster_id}' not found")
    if _cluster_kind(cluster) != "general":
        raise HTTPException(409, "general load generation requires a general-compute cluster")
    body = await request.json()
    benchmark_id = str(body.get("benchmark_id") or "").strip()
    scenario_id = str(body.get("scenario_id") or "").strip()
    if not benchmark_id or not scenario_id:
        raise HTTPException(400, "benchmark_id and scenario_id are required")
    benchmark, scenario = _loadgen_benchmark_selection(
        await _discover_loadgen_benchmarks(cluster_id),
        benchmark_id,
        scenario_id,
    )
    namespace = str(benchmark.get("namespace") or cluster.get("default_namespace") or "default").strip()
    service = str(benchmark.get("service") or "").strip()
    port = _clamp_int(benchmark.get("port", 80), 80, 1, 65535)
    plan_body = {
        **body,
        "method": scenario.get("method") or "GET",
        "path": scenario.get("path") or "/",
    }
    plan = _build_http_k8s_loadgen_plan(
        plan_body,
        cluster_id=cluster_id,
        namespace=namespace,
        service=service,
        port=port,
    )
    plan.update({
        "benchmark_id": benchmark_id,
        "benchmark_name": benchmark.get("name"),
        "scenario_id": scenario_id,
        "scenario_name": scenario.get("name"),
        "trace_backend": benchmark.get("trace_backend"),
        "trace_service": benchmark.get("trace_service"),
    })
    if bool(body.get("dry_run")):
        return {"status": "dry_run", "loadgen": plan}
    results = []
    for command in plan["commands"]:
        results.append(await asyncio.to_thread(_shell_sync, command, 120))
        if not results[-1].get("ok"):
            break
    return {
        "status": "started" if results and all(item.get("ok") for item in results) else "failed",
        "loadgen": {**plan, "results": results},
    }


@app.get("/api/loadgen/general/{cluster_id}/benchmarks")
async def general_loadgen_benchmarks(cluster_id: str):
    cluster = _find_k8s_cluster(cluster_id)
    if not cluster:
        raise HTTPException(404, f"cluster '{cluster_id}' not found")
    if _cluster_kind(cluster) != "general":
        raise HTTPException(409, "general load generation requires a general-compute cluster")
    return {"cluster_id": cluster_id, "benchmarks": await _discover_loadgen_benchmarks(cluster_id)}


@app.get("/api/loadgen/general/{cluster_id}")
async def list_general_loadgen(cluster_id: str, namespace: str = ""):
    cluster = _find_k8s_cluster(cluster_id)
    if not cluster:
        raise HTTPException(404, f"cluster '{cluster_id}' not found")
    if _cluster_kind(cluster) != "general":
        raise HTTPException(409, "general load generation requires a general-compute cluster")
    effective_namespace = namespace.strip() or str(cluster.get("default_namespace") or "default")
    status = await _list_loadgen_pods(cluster_id, effective_namespace, "agenticsre-http-loadgen")
    return {"cluster_id": cluster_id, "namespace": effective_namespace, **status}


@app.delete("/api/loadgen/general/{cluster_id}/{run_id}")
async def stop_general_loadgen(cluster_id: str, run_id: str, namespace: str = ""):
    cluster = _find_k8s_cluster(cluster_id)
    if not cluster:
        raise HTTPException(404, f"cluster '{cluster_id}' not found")
    if _cluster_kind(cluster) != "general":
        raise HTTPException(409, "general load generation requires a general-compute cluster")
    effective_namespace = namespace.strip() or str(cluster.get("default_namespace") or "default")
    safe_run_id = _k8s_label_value(run_id)
    selector = f"app=agenticsre-http-loadgen,agenticsre.io/run-id={safe_run_id}"
    command = (
        f"kubectl -n {shlex.quote(effective_namespace)} delete pod,configmap "
        f"-l {shlex.quote(selector)} --ignore-not-found=true"
    )
    result = await asyncio.to_thread(_shell_sync, command, 120, cluster_id)
    return {
        "status": "stopped" if result.get("ok") else "failed",
        "cluster_id": cluster_id,
        "namespace": effective_namespace,
        "run_id": safe_run_id,
        "result": result,
    }


def _build_vllm_k8s_loadgen_plan(
    body: Dict[str, Any],
    *,
    cluster_id: Optional[str],
    namespace: str,
    deployment: str,
) -> Dict[str, Any]:
    """Build bounded in-cluster vLLM business load commands."""
    service = str(body.get("service") or body.get("svc") or deployment or "vllm-server").strip()
    model = str(body.get("model") or body.get("served_model_name") or "vllm").strip() or "vllm"
    endpoint = str(body.get("loadgen_endpoint") or "").strip()
    if not endpoint:
        endpoint = f"http://{service}.{namespace}.svc.cluster.local:8000/v1/completions"
    image = str(body.get("loadgen_image") or body.get("image") or "vllm/vllm-openai:latest").strip()
    duration_s = _clamp_int(body.get("loadgen_duration_s", body.get("duration_s", 180)), 180, 10, 7200)
    concurrency = _clamp_int(body.get("loadgen_concurrency", body.get("concurrent", 8)), 8, 1, 256)
    prompt_len = _clamp_int(body.get("loadgen_prompt_len", body.get("prompt_len", 2048)), 2048, 16, 32768)
    max_tokens = _clamp_int(body.get("loadgen_max_tokens", body.get("max_tokens", 128)), 128, 1, 4096)
    timeout_s = _clamp_int(body.get("loadgen_timeout_s", 60), 60, 5, 600)
    qps = max(0.0, _to_float(body.get("loadgen_qps", 0.0), 0.0))
    node_name = str(body.get("loadgen_node_name") or body.get("node_name") or "").strip()
    run_id = _k8s_label_value(body.get("run_id") or f"vllm-{uuid.uuid4().hex[:12]}")
    name = f"agenticsre-vllm-load-{_k8s_name_fragment(deployment, max_len=18)}-{run_id}"[:56].rstrip("-")
    script_cm = f"{name}-script"
    target_selector = (
        "app=agenticsre-vllm-loadgen,"
        f"agenticsre.io/deployment={_k8s_label_value(deployment)},"
        f"agenticsre.io/service={_k8s_label_value(service)}"
    )
    label_selector = (
        f"app=agenticsre-vllm-loadgen,agenticsre.io/run-id={run_id}"
        if body.get("run_id")
        else target_selector
    )
    label_args = [
        "app=agenticsre-vllm-loadgen",
        f"agenticsre.io/run-id={run_id}",
        f"agenticsre.io/deployment={_k8s_label_value(deployment)}",
        f"agenticsre.io/service={_k8s_label_value(service)}",
        f"agenticsre.io/model={_k8s_label_value(model)}",
    ]
    namespace_q = shlex.quote(namespace)
    kubectl = _build_kubectl_cmd(cluster_id)
    cleanup_commands = [
        f"{kubectl} -n {namespace_q} delete pod,configmap -l {shlex.quote(label_selector)} --ignore-not-found=true"
    ]
    args = [
        "--endpoint",
        endpoint,
        "--model",
        model,
        "--duration-s",
        str(duration_s),
        "--concurrency",
        str(concurrency),
        "--prompt-len",
        str(prompt_len),
        "--max-tokens",
        str(max_tokens),
        "--timeout-s",
        str(timeout_s),
    ]
    if qps > 0:
        args.extend(["--qps", str(qps)])
    overrides = {
        "spec": {
            "restartPolicy": "Never",
            "tolerations": [
                {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"},
                {"key": "node.kubernetes.io/disk-pressure", "operator": "Exists", "effect": "NoSchedule"},
            ],
            "containers": [{
                "name": "loadgen",
                "image": image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["python3", "/opt/loadgen/loadgen.py"],
                "args": args,
                "volumeMounts": [{"name": "script", "mountPath": "/opt/loadgen"}],
            }],
            "volumes": [{"name": "script", "configMap": {"name": script_cm}}],
        }
    }
    if node_name:
        overrides["spec"]["nodeSelector"] = {"kubernetes.io/hostname": node_name}
    create_configmap_command = (
        f"{kubectl} -n {namespace_q} create configmap {shlex.quote(script_cm)} "
        f"--from-literal=loadgen.py={shlex.quote(_vllm_loadgen_python())}"
    )
    label_configmap_command = (
        f"{kubectl} -n {namespace_q} label configmap {shlex.quote(script_cm)} "
        + " ".join(shlex.quote(item) for item in label_args)
        + " --overwrite"
    )
    run_command = (
        f"{kubectl} -n {namespace_q} run {shlex.quote(name)} "
        f"--restart=Never --image={shlex.quote(image)} --labels={shlex.quote(','.join(label_args))} "
        f"--overrides={shlex.quote(json.dumps(overrides, ensure_ascii=False, separators=(',', ':')))}"
    )
    log_commands = [
        f"{kubectl} -n {namespace_q} logs -l {shlex.quote(label_selector)} --tail=80",
        f"{kubectl} -n {namespace_q} get pod -l {shlex.quote(label_selector)} -o wide",
    ]
    return {
        "enabled": True,
        "kind": "k8s_pod",
        "run_id": run_id,
        "namespace": namespace,
        "deployment": deployment,
        "service": service,
        "pod": name,
        "endpoint": endpoint,
        "model": model,
        "image": image,
        "duration_s": duration_s,
        "concurrency": concurrency,
        "prompt_len": prompt_len,
        "max_tokens": max_tokens,
        "timeout_s": timeout_s,
        "qps": qps,
        "node_name": node_name,
        "commands": [*cleanup_commands, create_configmap_command, label_configmap_command, run_command],
        "cleanup_commands": cleanup_commands,
        "log_commands": log_commands,
        "description": "在目标 Namespace 内启动有限时长的 vLLM OpenAI 请求负载 Pod，用于触发排队、TTFT/TPOT/E2E 时延和成功率指标变化。",
    }


def _merge_cluster_defaults_for_vllm_loadgen(
    body: Dict[str, Any],
    cluster: Optional[Dict[str, Any]],
    *,
    namespace: str,
    deployment: str,
    service: str,
) -> Dict[str, Any]:
    """Merge AI-cluster runtime defaults into a vLLM loadgen request.

    Older UI/API callers used "vllm" as a placeholder model. On the T4
    cluster the served model is configured by the cluster profile, so treating
    that placeholder as explicit causes 404 responses from vLLM.
    """
    merged = dict(body)
    if cluster:
        merged.setdefault("namespace", namespace)
        merged["service"] = merged.get("service") or cluster.get("service") or service or deployment
        raw_model = str(merged.get("model") or merged.get("served_model_name") or "").strip()
        cluster_model = str(cluster.get("model") or "").strip()
        if cluster_model and (not raw_model or raw_model.lower() == "vllm"):
            merged["model"] = cluster_model
    else:
        merged.setdefault("service", service or deployment)
    return merged


def _service_selects_pod(service: Dict[str, Any], pod: Dict[str, Any]) -> bool:
    selector = service.get("selector") if isinstance(service.get("selector"), dict) else {}
    labels = pod.get("labels") if isinstance(pod.get("labels"), dict) else {}
    return bool(selector) and all(labels.get(key) == value for key, value in selector.items())


def _vllm_service_models_via_api(
    cluster_id: str,
    namespace: str,
    service: str,
    port: int,
    port_name: str = "",
) -> List[str]:
    """Read the OpenAI-compatible model catalog through the Kubernetes service proxy."""
    service_ref_candidates = []
    if port_name:
        service_ref_candidates.append(f"http:{service}:{port_name}")
    service_ref_candidates.extend([f"http:{service}:{port}", f"{service}:{port}", service])
    for service_ref in dict.fromkeys(service_ref_candidates):
        raw_path = f"/api/v1/namespaces/{namespace}/services/{service_ref}/proxy/v1/models"
        result = _shell_sync(f"kubectl get --raw {shlex.quote(raw_path)}", 30, cluster_id)
        if not result.get("ok"):
            continue
        try:
            payload = json.loads(result.get("stdout") or "{}")
        except json.JSONDecodeError:
            continue
        models = sorted({
            str(item.get("id") or "").strip()
            for item in payload.get("data") or []
            if isinstance(item, dict) and item.get("id")
        })
        if models:
            return models
    return []


async def _ai_loadgen_targets(cluster: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Discover ready vLLM services and bind each service to its served models."""
    cluster_id = str(cluster.get("id") or "")
    inventory = await _collect_ai_k8s_inventory(cluster)
    pods = [
        pod for pod in inventory.get("pods") or []
        if pod.get("phase") == "Running" and int(pod.get("ready") or 0) > 0
    ]
    targets = []
    for service in inventory.get("services") or []:
        matched_pods = [pod for pod in pods if _service_selects_pod(service, pod)]
        pod_models = sorted({
            model
            for pod in matched_pods
            for model in pod.get("served_models") or []
            if model
        })
        if not pod_models:
            continue
        port_records = service.get("port_records") or []
        port_record = next(
            (item for item in port_records if _to_int(item.get("port"), 0) == 8000),
            port_records[0] if port_records else {"port": 8000, "name": ""},
        )
        port = _clamp_int(port_record.get("port", 8000), 8000, 1, 65535)
        namespace = str(service.get("namespace") or cluster.get("default_namespace") or "llm-k8s")
        live_models = await asyncio.to_thread(
            _vllm_service_models_via_api,
            cluster_id,
            namespace,
            str(service.get("name") or ""),
            port,
            str(port_record.get("name") or ""),
        )
        models = live_models or pod_models
        targets.append({
            "namespace": namespace,
            "service": service.get("name") or "",
            "deployment": service.get("name") or "",
            "port": port,
            "models": models,
            "default_model": models[0] if models else "",
            "pods": [pod.get("name") for pod in matched_pods],
            "nodes": sorted({pod.get("node") for pod in matched_pods if pod.get("node")}),
            "source": "v1/models" if live_models else "k8s_runtime",
        })
    configured_model = str(cluster.get("model") or "").strip()
    targets.sort(key=lambda item: (
        configured_model not in (item.get("models") or []),
        item.get("service") or "",
    ))
    return targets


def _select_ai_loadgen_target(
    targets: List[Dict[str, Any]],
    *,
    service: str,
    model: str,
) -> Dict[str, Any]:
    target = next((item for item in targets if item.get("service") == service), None) if service else None
    if not target and model:
        target = next((item for item in targets if model in (item.get("models") or [])), None)
    if not target and targets:
        target = targets[0]
    if not target:
        raise HTTPException(409, "no ready vLLM load target was discovered in this AI cluster")
    selected_model = model or str(target.get("default_model") or "")
    if selected_model not in (target.get("models") or []):
        raise HTTPException(
            409,
            f"model '{selected_model}' is not served by service '{target.get('service')}'",
        )
    return {**target, "model": selected_model}


async def _execute_vllm_loadgen_plan(plan: Dict[str, Any], timeout_s: int = 120) -> List[Dict[str, Any]]:
    results = []
    for cmd in plan.get("commands") or []:
        results.append(await asyncio.to_thread(_shell_sync, cmd, timeout_s))
        if not results[-1].get("ok"):
            break
    return results


@app.post("/api/ai-clusters/{cluster_id}/loadgen")
async def ai_cluster_start_loadgen(cluster_id: str, request: Request):
    """Start a bounded in-cluster vLLM request load generator for dashboard testing."""
    cluster = _find_k8s_cluster(cluster_id)
    if not cluster:
        raise HTTPException(404, f"cluster '{cluster_id}' not found")
    if _cluster_kind(cluster) != "ai":
        raise HTTPException(400, f"cluster '{cluster_id}' is not marked as an AI cluster")
    body = await request.json()
    selected_target = _select_ai_loadgen_target(
        await _ai_loadgen_targets(cluster),
        service=str(body.get("service") or "").strip(),
        model=str(body.get("model") or body.get("served_model_name") or "").strip(),
    )
    namespace = str(selected_target.get("namespace") or cluster.get("default_namespace") or "llm-k8s").strip() or "llm-k8s"
    deployment = str(selected_target.get("deployment") or selected_target.get("service") or "vllm-server").strip()
    body = {
        **body,
        "namespace": namespace,
        "deployment": deployment,
        "service": selected_target.get("service"),
        "model": selected_target.get("model"),
        "loadgen_endpoint": (
            body.get("loadgen_endpoint")
            or f"http://{selected_target.get('service')}.{namespace}.svc.cluster.local:{selected_target.get('port')}/v1/completions"
        ),
    }
    plan_body = _merge_cluster_defaults_for_vllm_loadgen(
        body,
        cluster,
        namespace=namespace,
        deployment=deployment,
        service=str(body.get("service") or cluster.get("service") or deployment),
    )
    plan = _build_vllm_k8s_loadgen_plan(
        plan_body,
        cluster_id=cluster_id,
        namespace=namespace,
        deployment=deployment,
    )
    if bool(body.get("dry_run")):
        return {"status": "dry_run", "cluster": _redact_cluster_runtime_fields(cluster), "loadgen": plan}
    results = await _execute_vllm_loadgen_plan(plan)
    status = "started" if results and all(r.get("ok") for r in results) else "failed"
    return {
        "status": status,
        "cluster": _redact_cluster_runtime_fields(cluster),
        "loadgen": {**plan, "results": results},
    }


@app.get("/api/ai-clusters/{cluster_id}/loadgen-targets")
async def ai_cluster_loadgen_targets(cluster_id: str):
    cluster = _find_k8s_cluster(cluster_id)
    if not cluster:
        raise HTTPException(404, f"cluster '{cluster_id}' not found")
    if _cluster_kind(cluster) != "ai":
        raise HTTPException(400, f"cluster '{cluster_id}' is not marked as an AI cluster")
    targets = await _ai_loadgen_targets(cluster)
    return {
        "cluster": _redact_cluster_runtime_fields(cluster),
        "targets": targets,
        "default_target": targets[0] if targets else None,
    }


@app.get("/api/ai-clusters/{cluster_id}/loadgen")
async def ai_cluster_loadgen_status(cluster_id: str, namespace: str = ""):
    """List independently started vLLM load tasks in the selected cluster."""
    cluster = _find_k8s_cluster(cluster_id)
    if not cluster:
        raise HTTPException(404, f"cluster '{cluster_id}' not found")
    if _cluster_kind(cluster) != "ai":
        raise HTTPException(400, f"cluster '{cluster_id}' is not marked as an AI cluster")
    effective_namespace = namespace.strip() or str(cluster.get("default_namespace") or "llm-k8s")
    status = await _list_loadgen_pods(cluster_id, effective_namespace, "agenticsre-vllm-loadgen")
    return {"cluster_id": cluster_id, "namespace": effective_namespace, **status}


@app.delete("/api/ai-clusters/{cluster_id}/loadgen")
async def ai_cluster_cleanup_loadgen(cluster_id: str, request: Request):
    """Delete dashboard-created vLLM load generator Pods and ConfigMaps."""
    cluster = _find_k8s_cluster(cluster_id)
    if not cluster:
        raise HTTPException(404, f"cluster '{cluster_id}' not found")
    if _cluster_kind(cluster) != "ai":
        raise HTTPException(400, f"cluster '{cluster_id}' is not marked as an AI cluster")
    try:
        body = await request.json()
    except Exception:
        body = {}
    namespace = str(body.get("namespace") or cluster.get("default_namespace") or "llm-k8s").strip() or "llm-k8s"
    deployment = str(body.get("deployment") or cluster.get("deployment") or "vllm-server").strip() or "vllm-server"
    plan = _build_vllm_k8s_loadgen_plan(
        {
            **body,
            "service": body.get("service") or cluster.get("service") or deployment,
            "model": body.get("model") or cluster.get("model") or "vllm",
        },
        cluster_id=cluster_id,
        namespace=namespace,
        deployment=deployment,
    )
    results = [
        await asyncio.to_thread(_shell_sync, cmd, 120)
        for cmd in plan.get("cleanup_commands", [])
    ]
    return {
        "status": "cleaned" if all(r.get("ok") for r in results) else "failed",
        "cluster": _redact_cluster_runtime_fields(cluster),
        "loadgen": {**plan, "commands": plan.get("cleanup_commands", []), "results": results},
    }


def _build_host_ssh_prefix(host: Dict[str, Any]) -> str:
    """Build the SSH command prefix to reach an LLM GPU host."""
    ssh_user = host.get("ssh_user", "root")
    ssh_key = host.get("ssh_key_path", "")
    jump = host.get("jump_host", "")
    ssh_password_env = str(host.get("ssh_password_env", "") or "").strip()
    jump_password_env = str(host.get("jump_password_env", "") or "").strip()
    address = str(host.get("host") or "").strip()
    if not address:
        raise ValueError(f"llm host '{host.get('id') or ''}' has no configured address")
    target = f"{ssh_user}@{address}"

    ssh_opts = [
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
    ]

    if jump and jump_password_env:
        jump_ssh = ["ssh", *ssh_opts]
        jump_script = (
            'SSHPASS="$SSHPASS_JUMP"; export SSHPASS; '
            f"exec sshpass -e {' '.join(shlex.quote(p) for p in jump_ssh)} -W \"$1:$2\" {shlex.quote(jump)}"
        )
        proxy_cmd = f"sh -c {shlex.quote(jump_script)} sh %h %p"
        ssh_opts += ["-o", "ProxyCommand=" + proxy_cmd]
    elif jump:
        ssh_opts += ["-J", jump]

    parts: List[str] = ["ssh", *ssh_opts]
    if ssh_key:
        parts += ["-i", ssh_key]

    target_password = os.environ.get(ssh_password_env) if ssh_password_env else ""
    if target_password and not ssh_key:
        parts = ["sshpass", "-e", *parts]

    parts.append(target)
    return " ".join(shlex.quote(p) for p in parts)


def _host_ssh_env(host: Dict[str, Any]) -> Dict[str, str]:
    """Return runtime-only environment variables needed by sshpass."""
    env: Dict[str, str] = {}
    ssh_password_env = str(host.get("ssh_password_env", "") or "").strip()
    jump_password_env = str(host.get("jump_password_env", "") or "").strip()
    if ssh_password_env and os.environ.get(ssh_password_env):
        env["SSHPASS"] = os.environ[ssh_password_env]
    if jump_password_env and os.environ.get(jump_password_env):
        env["SSHPASS_JUMP"] = os.environ[jump_password_env]
    return env


def _redact_host_runtime_fields(host: Dict[str, Any]) -> Dict[str, Any]:
    redacted = dict(host)
    for key in ("ssh_password", "jump_password"):
        redacted.pop(key, None)
    for key in ("ssh_password_env", "jump_password_env"):
        if redacted.get(key):
            redacted[key] = f"${redacted[key]}"
    redacted["configured"] = _llm_host_address_configured(host)
    return redacted


async def _verify_llm_host_queue_recovered(
    host: Dict[str, Any],
    *,
    metrics_endpoint: str = "http://127.0.0.1:8000/metrics",
    timeout_s: int = 60,
    interval_s: int = 5,
) -> Dict[str, Any]:
    """Poll vLLM queue metrics after cleanup to prove business-side recovery."""
    timeout_s = _clamp_int(timeout_s, 60, 5, 600)
    interval_s = _clamp_int(interval_s, 5, 1, 30)
    remote_probe = (
        f"metrics=$(curl -fsS --max-time 5 {shlex.quote(metrics_endpoint)} 2>/dev/null); "
        "if [ -z \"$metrics\" ]; then echo 'METRICS=unavailable'; exit 0; fi; "
        "printf '%s\\n' \"$metrics\" | awk '"
        "BEGIN {running=\"\"; waiting=\"\"} "
        "/^vllm:num_requests_running(\\{| )/ {running=$NF} "
        "/^vllm:num_requests_waiting(\\{| )/ {waiting=$NF} "
        "END {printf \"METRICS=ok\\nRUNNING=%s\\nWAITING=%s\\n\", running, waiting}'"
    )
    ssh_prefix = _build_host_ssh_prefix(host)
    deadline = time.time() + timeout_s
    attempts: List[Dict[str, Any]] = []
    recovered = False
    last: Dict[str, Any] = {}
    while True:
        result = await asyncio.to_thread(
            _shell_sync,
            f"{ssh_prefix} {shlex.quote(remote_probe)}",
            20,
            None,
            _host_ssh_env(host),
        )
        sample: Dict[str, Any] = {
            "ok": result.get("ok"),
            "stdout": result.get("stdout"),
            "stderr": result.get("stderr"),
            "returncode": result.get("returncode"),
        }
        for line in (result.get("stdout") or "").splitlines():
            if line.startswith("METRICS="):
                sample["metrics_status"] = line[len("METRICS="):].strip()
            elif line.startswith("RUNNING="):
                sample["running"] = _to_float(line[len("RUNNING="):].strip(), -1)
            elif line.startswith("WAITING="):
                sample["waiting"] = _to_float(line[len("WAITING="):].strip(), -1)
        last = sample
        attempts.append(sample)
        if sample.get("metrics_status") == "ok" and sample.get("running") == 0 and sample.get("waiting") == 0:
            recovered = True
            break
        if time.time() >= deadline:
            break
        await asyncio.sleep(interval_s)
    return {
        "checked": True,
        "recovered": recovered,
        "timeout_s": timeout_s,
        "interval_s": interval_s,
        "attempts": len(attempts),
        "last": last,
    }


async def _run_llm_fault_on_host(
    action: str,
    scenario: Dict[str, Any],
    body: Dict[str, Any],
    host: Dict[str, Any],
) -> Dict[str, Any]:
    """Drive the vLLM fault injector remotely over SSH on a bare-metal GPU host.

    The command is built locally and executed over SSH, so the target host does
    not need to have the Python fault-injector package installed.
    """
    dry_run = bool(body.get("dry_run", True))
    fault_type = scenario.get("fault_type") or scenario.get("id") or ""
    interface = body.get("interface") or "eth0"
    endpoint = body.get("endpoint") or "http://127.0.0.1:8000/v1/completions"
    metrics_endpoint = body.get("metrics_endpoint") or "http://127.0.0.1:8000/metrics"
    model = body.get("model") or "vllm"
    model_dir = body.get("model_dir") or ""
    concurrent = _clamp_int(body.get("concurrent"), 16, 1, 128)
    rounds = _clamp_int(body.get("rounds"), 2, 1, 50)
    prompt_len = _clamp_int(body.get("prompt_len"), 2048, 16, 32768)
    max_tokens = _clamp_int(body.get("max_tokens"), 128, 1, 2048)
    recovery_timeout_s = _clamp_int(body.get("recovery_timeout_s"), 60, 5, 600)

    sys.path.insert(0, str(LLM_FAULT_INJECTOR_DIR.parent))
    try:
        from vllm_fault_injector.host_faults import build_host_fault_command
    except Exception as e:
        logger.exception("Failed to import host fault command builder")
        return {
            "status": "import_error",
            "action": action,
            "scenario": scenario,
            "target_mode": "host",
            "host": host,
            "message": str(e),
        }

    built = build_host_fault_command(
        fault_type,
        action,
        dry_run=False,
        interface=interface,
        endpoint=endpoint,
        model=model,
        model_dir=model_dir,
        concurrent=concurrent,
        rounds=rounds,
        prompt_len=prompt_len,
        max_tokens=max_tokens,
    )
    if not built.supported:
        return {
            "status": "unsupported",
            "action": action,
            "scenario": scenario,
            "target_mode": "host",
            "host": host,
            "message": built.message,
            "fault_type": fault_type,
        }
    _require_fault_execution_approval(
        body=body,
        target_mode="host",
        action=action,
        fault_type=fault_type,
        severity=str(scenario.get("severity") or ""),
    )

    ssh_prefix = _build_host_ssh_prefix(host)
    remote_cmd = built.command
    full_cmd = f"{ssh_prefix} {shlex.quote(remote_cmd)}"
    started_at = time.time()

    if dry_run:
        payload = {
            "status": "dry_run",
            "action": action,
            "scenario": scenario,
            "target_mode": "host",
            "host": host,
            "fault_type": fault_type,
            "layer": built.layer,
            "message": built.message,
            "remote_command": remote_cmd,
            "recovery_command": built.recovery_command,
            "parameters": {
                "interface": interface,
                "endpoint": endpoint,
                "metrics_endpoint": metrics_endpoint,
                "model": model,
                "model_dir": model_dir,
                "concurrent": concurrent,
                "rounds": rounds,
                "prompt_len": prompt_len,
                "max_tokens": max_tokens,
                "recovery_timeout_s": recovery_timeout_s,
            },
            "command": full_cmd,
        }
        payload["effect_summary"] = _summarize_llm_fault_effect(payload)
        return payload

    result = await asyncio.to_thread(_shell_sync, full_cmd, 300, None, _host_ssh_env(host))
    payload = {
        "status": "executed" if result.get("ok") else "failed",
        "action": action,
        "scenario": scenario,
        "target_mode": "host",
        "host": host,
        "fault_type": fault_type,
        "layer": built.layer,
        "message": built.message,
        "remote_command": remote_cmd,
        "recovery_command": built.recovery_command,
        "parameters": {
            "interface": interface,
            "endpoint": endpoint,
            "metrics_endpoint": metrics_endpoint,
            "model": model,
            "model_dir": model_dir,
            "concurrent": concurrent,
            "rounds": rounds,
            "prompt_len": prompt_len,
            "max_tokens": max_tokens,
            "recovery_timeout_s": recovery_timeout_s,
        },
        "command": result.get("command"),
        "stdout": result.get("stdout"),
        "stderr": result.get("stderr"),
        "returncode": result.get("returncode"),
    }
    if action == "cleanup" and payload["status"] == "executed" and fault_type in {"request_queue_overflow", "continuous_batch_disruption"}:
        payload["recovery_verification"] = await _verify_llm_host_queue_recovered(
            host,
            metrics_endpoint=metrics_endpoint,
            timeout_s=recovery_timeout_s,
        )
    payload["effect_summary"] = _summarize_llm_fault_effect(payload)
    _append_fault_run({
        "id": f"fault-{uuid.uuid4().hex[:10]}",
        "started_at": started_at,
        "finished_at": time.time(),
        "status": payload["status"],
        "target_mode": "host",
        "host_id": host.get("id"),
        "host": host.get("host"),
        "action": action,
        "scenario_id": scenario.get("id"),
        "fault_type": fault_type,
        "layer": built.layer,
        "remote_command": remote_cmd,
        "recovery_command": built.recovery_command,
        "returncode": result.get("returncode"),
        "stdout": result.get("stdout"),
        "stderr": result.get("stderr"),
    })
    return payload


async def _run_llm_fault(
    action: str,
    scenario: Dict[str, Any],
    body: Dict[str, Any],
) -> Dict[str, Any]:
    """Run a vLLM fault action with dry-run as the default safety mode.

    Body fields:
      - target_mode: "k8s" (default) or "host"
      - cluster_id: K8s cluster profile id (k8s mode only)
      - host_id: GPU host profile id (host mode only)
      - namespace / deployment: passed through to FaultInjector (k8s mode)
    """
    dry_run = bool(body.get("dry_run", True))
    target_mode = body.get("target_mode", "k8s")

    if target_mode == "host":
        host_id = body.get("host_id")
        host = _find_llm_host(host_id)
        if not host:
            raise HTTPException(400, f"llm host '{host_id}' not found")
        _require_llm_host_address(host, str(host_id or ""))
        return await _run_llm_fault_on_host(action, scenario, body, host)

    cluster_id = body.get("cluster_id") or None
    cluster = _find_k8s_cluster(cluster_id) if cluster_id else None
    namespace = body.get("namespace") or (cluster.get("default_namespace") if cluster else None) or "default"
    deployment = body.get("deployment") or "vllm-server"
    service = body.get("service") or deployment
    fault_type = scenario.get("fault_type")
    layer = scenario.get("layer") or "all"
    baseline_samples = int(body.get("baseline_samples", 2))
    fault_samples = int(body.get("fault_samples", 3))
    sample_interval = float(body.get("sample_interval", 3.0))
    observation_delay_s = float(body.get("observation_delay_s", 3.0))
    background_load = bool(body.get("background_load"))
    loadgen_timeout_s = _clamp_int(body.get("loadgen_timeout_s", 60), 60, 5, 600)
    loadgen_plan: Optional[Dict[str, Any]] = None
    if background_load or action == "cleanup":
        loadgen_body = _merge_cluster_defaults_for_vllm_loadgen(
            body,
            cluster,
            namespace=namespace,
            deployment=deployment,
            service=service,
        )
        if action == "experiment" and not loadgen_body.get("loadgen_duration_s"):
            loadgen_body["loadgen_duration_s"] = int(max(
                60,
                (baseline_samples + fault_samples + 2) * max(1.0, sample_interval) + 30,
            ))
        loadgen_plan = _build_vllm_k8s_loadgen_plan(
            loadgen_body,
            cluster_id=cluster_id,
            namespace=namespace,
            deployment=deployment,
        )
    _require_fault_execution_approval(
        body=body,
        target_mode="k8s",
        action=action,
        fault_type=fault_type,
        severity=str(scenario.get("severity") or ""),
    )

    status = _llm_fault_tool_status()
    if not status["available"]:
        return {
            "status": "unavailable",
            "message": "vLLM fault injector tool not found. Set VLLM_FAULT_INJECTOR_DIR or deploy the tool package.",
            "tool": status,
            "scenario": scenario,
            "dry_run": dry_run,
            "target_mode": "k8s",
            "cluster_id": cluster_id,
        }

    sys.path.insert(0, str(LLM_FAULT_INJECTOR_DIR.parent))
    try:
        from vllm_fault_injector.injector import FaultInjector
    except Exception as e:
        logger.exception("Failed to import vLLM fault injector")
        return {
            "status": "import_error",
            "message": str(e),
            "tool": status,
            "scenario": scenario,
            "dry_run": dry_run,
            "target_mode": "k8s",
            "cluster_id": cluster_id,
        }

    prometheus_url = (
        str(cluster.get("prometheus_url") or "").strip()
        if isinstance(cluster, dict)
        else ""
    ) or _discover_prometheus_url() or "http://localhost:9090"
    injector = FaultInjector(
        kubectl_cmd=_build_kubectl_cmd(cluster_id),
        namespace=namespace,
        deployment=_llm_fault_injector_target(str(fault_type or ""), deployment, service),
        dry_run=dry_run,
        prometheus_url=prometheus_url,
    )

    base_meta = {
        "target_mode": "k8s",
        "cluster_id": cluster_id,
        "namespace": namespace,
        "deployment": deployment,
        "service": service,
        "background_load": background_load,
    }

    try:
        started_at = time.time()
        if action == "cleanup":
            loadgen_results: List[Dict[str, Any]] = []
            if loadgen_plan and not dry_run:
                loadgen_results = [
                    await asyncio.to_thread(_shell_sync, cmd, loadgen_timeout_s)
                    for cmd in loadgen_plan.get("cleanup_commands", [])
                ]
            source_run = _matching_fault_run_for_recovery(
                fault_run_id=str(body.get("fault_run_id") or ""),
                cluster_id=str(cluster_id or ""),
                namespace=namespace,
                deployment=deployment,
                scenario_id=str(scenario.get("id") or ""),
            )
            recovery_command = _fault_run_recovery_command(source_run)
            recovery_source = "recorded_run"
            if not recovery_command:
                preview_injector = FaultInjector(
                    kubectl_cmd=_build_kubectl_cmd(cluster_id),
                    namespace=namespace,
                    deployment=_llm_fault_injector_target(str(fault_type or ""), deployment, service),
                    dry_run=True,
                    prometheus_url=prometheus_url,
                )
                preview_result = await preview_injector.inject(fault_type)
                recovery_command = str(getattr(preview_result, "recovery_cmd", "") or "").strip()
                recovery_source = "injector_contract_preview"
            if not recovery_command:
                raise HTTPException(409, "The injector did not provide a recovery contract for this fault run")
            if dry_run:
                recovery_result = {
                    "ok": True,
                    "returncode": 0,
                    "stdout": "",
                    "stderr": "",
                }
            else:
                recovery_result = await asyncio.to_thread(
                    _shell_sync,
                    recovery_command,
                    loadgen_timeout_s,
                    cluster_id,
                )
            results = [{
                "fault_type": f"recover_{fault_type}",
                "layer": layer,
                "success": bool(recovery_result.get("ok")),
                "message": (
                    "Recovery contract prepared"
                    if dry_run
                    else "Recovery contract executed"
                ),
                "recovery_cmd": None,
                "command": recovery_command,
                "returncode": recovery_result.get("returncode"),
                "stdout": recovery_result.get("stdout"),
                "stderr": recovery_result.get("stderr"),
                "source": recovery_source,
                "source_fault_run_id": str((source_run or {}).get("id") or ""),
            }]
            observation = {}
            if not dry_run:
                observation = await _collect_llm_fault_live_observation(
                    cluster_id=cluster_id,
                    namespace=namespace,
                    deployment=deployment,
                    service=service,
                    delay_s=min(observation_delay_s, 3.0),
                )
            payload = {
                "status": "dry_run" if dry_run else "executed",
                "action": "cleanup",
                "scenario": scenario,
                **base_meta,
                "results": results,
            }
            if observation:
                payload["observation"] = observation
            if loadgen_plan:
                payload["loadgen"] = {
                    **loadgen_plan,
                    "commands": loadgen_plan.get("cleanup_commands", []),
                    "results": loadgen_results,
                }
            payload["effect_summary"] = _summarize_llm_fault_effect(payload)
            if not dry_run:
                fault_run_id = f"fault-{uuid.uuid4().hex[:10]}"
                _append_fault_run({
                    "id": fault_run_id,
                    "started_at": started_at,
                    "finished_at": time.time(),
                    "status": payload["status"],
                    "target_mode": "k8s",
                    "cluster_id": cluster_id,
                    "namespace": namespace,
                    "deployment": deployment,
                    "service": service,
                    "action": "cleanup",
                    "scenario_id": scenario.get("id"),
                    "fault_type": fault_type,
                    "layer": layer,
                    "results": payload["results"],
                    "observation": observation,
                    "loadgen": payload.get("loadgen"),
                })
                payload["fault_run_id"] = fault_run_id
                payload["ai_event_detection"] = await _refresh_ai_events_after_fault_action(cluster)
                payload["ai_event_detection"]["follow_up"] = _schedule_ai_event_follow_up(
                    cluster,
                    fault_run_id=fault_run_id,
                    expected_detection_time_s=scenario.get("expected_detection_time_s"),
                )
            return payload
        if action == "experiment":
            loadgen_results: List[Dict[str, Any]] = []
            if loadgen_plan and background_load and not dry_run:
                loadgen_results = await _execute_vllm_loadgen_plan(loadgen_plan, timeout_s=loadgen_timeout_s)
            result = await injector.run_experiment(
                fault_type,
                baseline_samples=baseline_samples,
                fault_samples=fault_samples,
                sample_interval=sample_interval,
                recovery_layer=layer if layer in {"software", "driver", "network", "os"} else "all",
            )
            payload = {
                "status": "dry_run" if dry_run else "executed",
                "action": "experiment",
                "scenario": scenario,
                **base_meta,
                "result": result,
            }
            if loadgen_plan and background_load:
                payload["loadgen"] = {**loadgen_plan, "results": loadgen_results}
            payload["effect_summary"] = _summarize_llm_fault_effect(payload)
            if not dry_run:
                fault_run_id = f"fault-{uuid.uuid4().hex[:10]}"
                _append_fault_run({
                    "id": fault_run_id,
                    "started_at": started_at,
                    "finished_at": time.time(),
                    "status": payload["status"],
                    "target_mode": "k8s",
                    "cluster_id": cluster_id,
                    "namespace": namespace,
                    "deployment": deployment,
                    "service": service,
                    "action": "experiment",
                    "scenario_id": scenario.get("id"),
                    "fault_type": fault_type,
                    "layer": layer,
                    "result": result,
                    "loadgen": payload.get("loadgen"),
                })
                payload["fault_run_id"] = fault_run_id
                payload["ai_event_detection"] = await _refresh_ai_events_after_fault_action(cluster)
                payload["ai_event_detection"]["follow_up"] = _schedule_ai_event_follow_up(
                    cluster,
                    fault_run_id=fault_run_id,
                    expected_detection_time_s=scenario.get("expected_detection_time_s"),
                )
            return payload
        loadgen_results: List[Dict[str, Any]] = []
        if loadgen_plan and background_load and not dry_run:
            loadgen_results = await _execute_vllm_loadgen_plan(loadgen_plan, timeout_s=loadgen_timeout_s)
        result = await injector.inject(fault_type)
        result_payload = getattr(result, "__dict__", str(result))
        injection_ok = bool(result_payload.get("success")) if isinstance(result_payload, dict) else False
        observation = {}
        if not dry_run and injection_ok:
            observation = await _collect_llm_fault_live_observation(
                cluster_id=cluster_id,
                namespace=namespace,
                deployment=deployment,
                service=service,
                delay_s=observation_delay_s,
            )
        payload = {
            "status": "dry_run" if dry_run else ("executed" if injection_ok else "failed"),
            "action": "inject",
            "scenario": scenario,
            **base_meta,
            "result": result_payload,
        }
        if observation:
            payload["observation"] = observation
        if loadgen_plan and background_load:
            payload["loadgen"] = {**loadgen_plan, "results": loadgen_results}
        payload["effect_summary"] = _summarize_llm_fault_effect(payload)
        if not dry_run:
            fault_run_id = f"fault-{uuid.uuid4().hex[:10]}"
            _append_fault_run({
                "id": fault_run_id,
                "started_at": started_at,
                "finished_at": time.time(),
                "status": payload["status"],
                "target_mode": "k8s",
                "cluster_id": cluster_id,
                "namespace": namespace,
                "deployment": deployment,
                "service": service,
                "action": "inject",
                "scenario_id": scenario.get("id"),
                "fault_type": fault_type,
                "layer": layer,
                "result": payload["result"],
                "observation": observation,
                "loadgen": payload.get("loadgen"),
            })
            payload["fault_run_id"] = fault_run_id
            payload["ai_event_detection"] = await _refresh_ai_events_after_fault_action(cluster)
            payload["ai_event_detection"]["follow_up"] = _schedule_ai_event_follow_up(
                cluster,
                fault_run_id=fault_run_id,
                expected_detection_time_s=scenario.get("expected_detection_time_s"),
            )
        return payload
    except Exception as e:
        logger.exception("vLLM fault action failed")
        raise HTTPException(500, str(e))


@app.get("/api/sow/alignment")
async def sow_alignment():
    """Current implementation alignment with the SoW acceptance points."""
    cfg = _get_config()
    kb = _get_fault_store().stats()
    hitl = _get_human_review_store().stats()
    evo = _get_evolution_tracker().get_evolution_report(fault_store=_get_fault_store())
    scenarios = _load_fault_scenarios()
    categories = sorted({s.get("category", "unknown") for s in scenarios})
    fault_types = sorted({s.get("fault_type", "unknown") for s in scenarios})
    return {
        "items": [
            {
                "requirement": "智算/通算专用运维智能体",
                "implementation": "告警、指标、日志、调用链、事件、Profiling、规划、修复智能体",
                "status": "implemented",
            },
            {
                "requirement": "告警压缩与根因推荐准确度 >= 80%",
                "implementation": "AlertAgent + E2E 场景评分；需以集群评测结果持续刷新",
                "status": "verifying",
            },
            {
                "requirement": "多智能体协作范式与架构选型",
                "implementation": "chain/react/reflection/plan_and_execute/debate/voting 对比评测",
                "status": "implemented",
            },
            {
                "requirement": "行为可观测与验证",
                "implementation": "TraceStore、RCAJudge、BehaviorValidator、Web SSE 事件流",
                "status": "implemented",
            },
            {
                "requirement": "反馈机制与历史轨迹增量更新上下文",
                "implementation": "FaultContextStore、ExpertFeedback、HITL 队列、EvolutionTracker",
                "status": "implemented",
            },
            {
                "requirement": "多轮反馈后根因定位准确率提升 >= 10%",
                "implementation": "enriched/baseline 评测模式 + 演化快照趋势",
                "status": "verifying",
            },
        ],
        "signals": {
            "memory": kb,
            "hitl": hitl,
            "evolution": evo,
            "fault_scenarios": {
                "count": len(scenarios),
                "categories": categories,
                "fault_types": fault_types,
            },
            "llm_fault_scenarios": _llm_fault_tool_status(),
            "config": {
                "memory_enabled": cfg.memory.enabled,
                "evolution_enabled": cfg.evolution.enabled,
                "hitl_enabled": True,
                "recovery_requires_approval": cfg.remediation.require_approval,
            },
        },
    }


# ─────────────────────────────────────────
# Fault Target Profiles (clusters + GPU hosts)
# ─────────────────────────────────────────

_FAULT_TARGET_REQUIRED_FIELDS = {
    "k8s_clusters": ("id", "name"),
    "llm_hosts": ("id", "name"),
}


def _validate_target_list(kind: str, items: Any) -> List[Dict[str, Any]]:
    """Validate and normalize a list of target profile dicts."""
    if items is None:
        return []
    if not isinstance(items, list):
        raise HTTPException(400, f"{kind} must be a list")
    required = _FAULT_TARGET_REQUIRED_FIELDS[kind]
    seen_ids = set()
    cleaned: List[Dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            raise HTTPException(400, f"{kind} entries must be objects")
        if kind == "llm_hosts" and (raw.get("ssh_password") or raw.get("jump_password")):
            raise HTTPException(400, "llm_hosts must not contain plaintext passwords; use *_password_env")
        if kind == "k8s_clusters" and not _is_valid_kubectl_path(raw.get("kubectl_path")):
            raise HTTPException(400, "k8s_clusters kubectl_path must be a command name or absolute path without shell arguments")
        for field_name in required:
            if not raw.get(field_name):
                raise HTTPException(400, f"{kind} entry missing '{field_name}'")
        item_id = raw["id"]
        if item_id in seen_ids:
            raise HTTPException(400, f"{kind} duplicate id '{item_id}'")
        seen_ids.add(item_id)
        cleaned.append({k: v for k, v in raw.items() if v is not None})
    return cleaned


@app.get("/api/fault-targets")
async def get_fault_targets():
    """Return all fault target profiles (K8s clusters + GPU hosts)."""
    cfg = _get_config()
    return {
        "k8s_clusters": list(cfg.fault_targets.k8s_clusters),
        "llm_hosts": [_redact_host_runtime_fields(h) for h in cfg.fault_targets.llm_hosts],
    }


@app.get("/api/clusters")
async def clusters_catalog():
    """Return all configured clusters grouped by role for dashboard selectors."""
    cfg = _get_config()
    clusters = [_redact_cluster_runtime_fields(c) for c in cfg.fault_targets.k8s_clusters]
    hosts = [_redact_host_runtime_fields(h) for h in cfg.fault_targets.llm_hosts]
    return {
        "clusters": clusters,
        "general_clusters": [c for c in clusters if c.get("kind") != "ai"],
        "ai_clusters": [c for c in clusters if c.get("kind") == "ai"],
        "llm_hosts": hosts,
    }


@app.get("/api/ai-clusters")
async def ai_clusters_catalog():
    """Return configured AI clusters for legacy frontend widgets."""
    cfg = _get_config()
    clusters = [
        _redact_cluster_runtime_fields(c)
        for c in cfg.fault_targets.k8s_clusters
        if _cluster_kind(c) == "ai"
    ]
    return {
        "clusters": clusters,
        "ai_clusters": clusters,
        "hosts": [
            _redact_host_runtime_fields(h)
            for h in cfg.fault_targets.llm_hosts
            if str(h.get("ai_cluster_id") or h.get("cluster_id") or "")
            in {str(c.get("id") or "") for c in clusters}
        ],
    }


@app.put("/api/fault-targets")
async def update_fault_targets(request: Request):
    """Replace fault target profiles and persist to data/cluster_profiles.json."""
    body = await request.json()
    clusters = _validate_target_list("k8s_clusters", body.get("k8s_clusters"))
    hosts = _validate_target_list("llm_hosts", body.get("llm_hosts"))
    cfg = _get_config()
    previous_clusters = cfg.fault_targets.k8s_clusters
    previous_hosts = cfg.fault_targets.llm_hosts
    cfg.fault_targets.k8s_clusters = clusters
    cfg.fault_targets.llm_hosts = hosts
    try:
        _save_fault_targets()
    except Exception:
        cfg.fault_targets.k8s_clusters = previous_clusters
        cfg.fault_targets.llm_hosts = previous_hosts
        raise
    return {
        "status": "ok",
        "k8s_clusters": [_redact_cluster_runtime_fields(c) for c in clusters],
        "llm_hosts": [_redact_host_runtime_fields(h) for h in hosts],
    }


@app.get("/api/fault-targets/k8s/{cluster_id}/discover")
async def discover_k8s_cluster(cluster_id: str):
    """Probe a K8s cluster profile: list nodes + namespaces using its kubeconfig/context."""
    cluster = _find_k8s_cluster(cluster_id)
    if not cluster:
        raise HTTPException(404, f"cluster '{cluster_id}' not found")
    kubectl_client_result = await asyncio.to_thread(
        _shell_sync,
        "kubectl version --client=true --output=yaml",
        20,
        cluster_id,
    )
    ns_result = await asyncio.to_thread(
        _shell_sync,
        "kubectl get ns -o jsonpath='{.items[*].metadata.name}'",
        20,
        cluster_id,
    )
    nodes_result = await asyncio.to_thread(
        _shell_sync,
        "kubectl get nodes -o jsonpath='{.items[*].metadata.name}'",
        20,
        cluster_id,
    )
    auth_result = await asyncio.to_thread(
        _shell_sync,
        "kubectl auth can-i get pods --all-namespaces",
        20,
        cluster_id,
    )
    return {
        "cluster": _redact_cluster_runtime_fields(cluster),
        "kubectl_path": _cluster_kubectl_path(cluster),
        "namespaces": (ns_result.get("stdout") or "").replace("'", "").split() if ns_result.get("ok") else [],
        "nodes": (nodes_result.get("stdout") or "").replace("'", "").split() if nodes_result.get("ok") else [],
        "kubectl_client_probe": kubectl_client_result,
        "ns_probe": ns_result,
        "nodes_probe": nodes_result,
        "auth_probe": auth_result,
    }


@app.post("/api/fault-targets/llm-host/{host_id}/probe")
async def probe_llm_host(host_id: str):
    """SSH-probe a GPU host for reachability and fault-injection capabilities."""
    host = _find_llm_host(host_id)
    if not host:
        raise HTTPException(404, f"llm host '{host_id}' not found")
    _require_llm_host_address(host, host_id)
    endpoint = "http://127.0.0.1:8000/v1/models"
    remote_probe = (
        "printf 'HOST='; hostname; "
        "printf 'GPU='; nvidia-smi -L 2>/dev/null | head -20 || true; "
        "printf 'TOOLS='; for t in python3 tc iptables nvidia-smi curl; do command -v $t >/dev/null 2>&1 && printf \"$t:yes \" || printf \"$t:no \"; done; echo; "
        "printf 'IFACES='; (ls /sys/class/net 2>/dev/null || ip -o link show 2>/dev/null | awk -F': ' '{print $2}') | tr '\\n' ' '; echo; "
        f"printf 'ENDPOINT='; curl -fsS --max-time 3 {shlex.quote(endpoint)} >/dev/null 2>&1 && echo ok || echo unavailable"
    )
    cmd = f"{_build_host_ssh_prefix(host)} {shlex.quote(remote_probe)}"
    result = await asyncio.to_thread(_shell_sync, cmd, 25, None, _host_ssh_env(host))
    stdout = result.get("stdout") or ""
    tools: Dict[str, bool] = {}
    interfaces: List[str] = []
    endpoint_ok = False
    for line in stdout.splitlines():
        if line.startswith("TOOLS="):
            for item in line[len("TOOLS="):].split():
                if ":" in item:
                    k, v = item.split(":", 1)
                    tools[k] = v == "yes"
        elif line.startswith("IFACES="):
            interfaces = [x for x in line[len("IFACES="):].split() if x]
        elif line.startswith("ENDPOINT="):
            endpoint_ok = line.strip().endswith("ok")
    capability = {
        "network_faults": bool(tools.get("tc")),
        "gpu_faults": bool(tools.get("nvidia-smi") and tools.get("python3")),
        "load_faults": bool(tools.get("python3")),
        "endpoint_probe": endpoint_ok,
    }
    return {
        "host": _redact_host_runtime_fields(host),
        "command": result.get("command"),
        "ok": result.get("ok"),
        "tools": tools,
        "interfaces": interfaces,
        "capability": capability,
        "stdout": result.get("stdout"),
        "stderr": result.get("stderr"),
    }


def _parse_llm_host_observation(stdout: str) -> Dict[str, Any]:
    gpus: List[Dict[str, Any]] = []
    processes: List[str] = []
    metrics: Dict[str, float] = {}
    metric_lines: List[str] = []
    host_name = ""
    timestamp = 0
    endpoint_status = "unknown"
    metrics_status = "unknown"
    in_gpu_csv = False

    def _to_float(value: str) -> Optional[float]:
        value = value.strip()
        if not value or value.upper() in {"N/A", "[N/A]"}:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line == "GPUCSV_BEGIN":
            in_gpu_csv = True
            continue
        if line == "GPUCSV_END":
            in_gpu_csv = False
            continue
        if in_gpu_csv:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 8:
                gpus.append({
                    "index": parts[0],
                    "name": parts[1],
                    "utilization_gpu_pct": _to_float(parts[2]),
                    "memory_used_mb": _to_float(parts[3]),
                    "memory_total_mb": _to_float(parts[4]),
                    "power_watts": _to_float(parts[5]),
                    "temperature_c": _to_float(parts[6]),
                    "clock_mhz": _to_float(parts[7]),
                })
            continue
        if line.startswith("HOST="):
            host_name = line[len("HOST="):].strip()
        elif line.startswith("TS="):
            try:
                timestamp = int(line[len("TS="):].strip())
            except ValueError:
                timestamp = 0
        elif line.startswith("PROC="):
            processes = [x.strip() for x in line[len("PROC="):].split("|") if x.strip()]
        elif line.startswith("ENDPOINT="):
            endpoint_status = line[len("ENDPOINT="):].strip() or "unknown"
        elif line.startswith("METRICS="):
            metrics_status = line[len("METRICS="):].strip() or "unknown"
        elif line.startswith("METRIC_LINE="):
            metric_line = line[len("METRIC_LINE="):].strip()
            metric_lines.append(metric_line)
            match = re.match(
                r"(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)(?:\{[^}]*\})?\s+"
                r"(?P<value>-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)$",
                metric_line,
            )
            if match:
                metrics[match.group("name")] = float(match.group("value"))

    return {
        "hostname": host_name,
        "timestamp": timestamp or int(time.time()),
        "gpus": gpus,
        "processes": processes,
        "endpoint_status": endpoint_status,
        "metrics_status": metrics_status,
        "metrics": metrics,
        "metric_lines": metric_lines,
        "observability_mode": "host_direct",
        "requires_prometheus": False,
    }


async def _observe_llm_host_direct(
    host: Dict[str, Any],
    *,
    models_endpoint: str = "http://127.0.0.1:8000/v1/models",
    metrics_endpoint: str = "http://127.0.0.1:8000/metrics",
) -> Dict[str, Any]:
    """Collect direct host-side observation without requiring Prometheus."""
    remote_observe = (
        "printf 'HOST='; hostname; "
        "printf 'TS='; date +%s; "
        "printf 'GPUCSV_BEGIN\\n'; "
        "nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu,clocks.current.graphics "
        "--format=csv,noheader,nounits 2>/dev/null || true; "
        "printf 'GPUCSV_END\\n'; "
        "printf 'PROC='; "
        "ps -eo pid=,args= | awk '/vllm.entrypoints|vllm serve|openai.api_server/ "
        "&& $0 !~ /awk / && $0 !~ /pgrep / && $0 !~ /grep / && $0 !~ /sh -c/ && $0 !~ /ssh / {print}' "
        "| head -5 | tr '\\n' '|' || true; echo; "
        f"if curl -fsS --max-time 3 {shlex.quote(models_endpoint)} >/dev/null 2>&1; then echo 'ENDPOINT=ok'; else echo 'ENDPOINT=unavailable'; fi; "
        f"metrics=$(curl -fsS --max-time 3 {shlex.quote(metrics_endpoint)} 2>/dev/null); "
        "if [ -n \"$metrics\" ]; then echo 'METRICS=ok'; "
        "printf '%s\\n' \"$metrics\" | grep -E 'vllm:(num_requests_running|num_requests_waiting|gpu_cache_usage_perc|avg_generation_throughput_toks_per_s|request_success_total|request_failure_total)' "
        "| head -20 | sed 's/^/METRIC_LINE=/'; "
        "else echo 'METRICS=unavailable'; fi"
    )
    cmd = f"{_build_host_ssh_prefix(host)} {shlex.quote(remote_observe)}"
    result = await asyncio.to_thread(_shell_sync, cmd, 30, None, _host_ssh_env(host))
    observation = _parse_llm_host_observation(result.get("stdout") or "")
    return {
        "host": _redact_host_runtime_fields(host),
        "ok": result.get("ok"),
        "command": result.get("command"),
        "stdout": result.get("stdout"),
        "stderr": result.get("stderr"),
        "returncode": result.get("returncode"),
        "observation": observation,
    }


@app.post("/api/fault-targets/llm-host/{host_id}/observe")
async def observe_llm_host(host_id: str, request: Request):
    """Collect direct host-side observation without requiring Prometheus."""
    host = _find_llm_host(host_id)
    if not host:
        raise HTTPException(404, f"llm host '{host_id}' not found")
    _require_llm_host_address(host, host_id)
    try:
        body = await request.json()
    except Exception:
        body = {}
    models_endpoint = str(body.get("models_endpoint") or "http://127.0.0.1:8000/v1/models")
    metrics_endpoint = str(body.get("metrics_endpoint") or "http://127.0.0.1:8000/metrics")
    return await _observe_llm_host_direct(host, models_endpoint=models_endpoint, metrics_endpoint=metrics_endpoint)


def _prom_series_to_chart_series(result: Dict[str, Any], label_key: str = "model_name") -> List[Dict[str, Any]]:
    series: List[Dict[str, Any]] = []
    for item in result.get("results") or []:
        metric = item.get("metric") or {}
        if "," in label_key:
            label_parts = [str(metric.get(k.strip()) or "").strip() for k in label_key.split(",")]
            label = " / ".join(part for part in label_parts if part)
        else:
            label = metric.get(label_key)
        label = label or metric.get("instance") or metric.get("job") or str(metric or {})
        values_raw = item.get("values") or []
        timestamps: List[float] = []
        values: List[float] = []
        for raw in values_raw:
            if not isinstance(raw, list) or len(raw) < 2:
                continue
            try:
                timestamps.append(float(raw[0]))
                values.append(float(raw[1]) if raw[1] != "NaN" else 0.0)
            except (TypeError, ValueError):
                continue
        series.append({
            "label": label,
            "timestamps": timestamps,
            "values": [round(v, 6) for v in values],
            "metric": metric,
        })
    return series


def _llm_metric_specs() -> Dict[str, Dict[str, str]]:
    return {
        "running": {
            "title": "运行中请求",
            "unit": "",
            "query": "vllm:num_requests_running",
        },
        "waiting": {
            "title": "等待队列",
            "unit": "",
            "query": "vllm:num_requests_waiting",
        },
        "kv_cache": {
            "title": "KV Cache 使用率",
            "unit": "%",
            "query": "vllm:gpu_cache_usage_perc * 100",
        },
        "prompt_tps": {
            "title": "Prefill 吞吐",
            "unit": " tok/s",
            "query": "vllm:avg_prompt_throughput_toks_per_s",
        },
        "generation_tps": {
            "title": "Generation 吞吐",
            "unit": " tok/s",
            "query": "vllm:avg_generation_throughput_toks_per_s",
        },
        "ttft_p95": {
            "title": "TTFT P95",
            "unit": " s",
            "query": "histogram_quantile(0.95, sum by(le, model_name)(rate(vllm:time_to_first_token_seconds_bucket[1m])))",
        },
        "tpot_p95": {
            "title": "TPOT P95",
            "unit": " s",
            "query": "histogram_quantile(0.95, sum by(le, model_name)(rate(vllm:time_per_output_token_seconds_bucket[1m])))",
        },
        "e2e_latency_p95": {
            "title": "请求端到端时延 P95",
            "unit": " s",
            "query": "histogram_quantile(0.95, sum by(le, model_name)(rate(vllm:e2e_request_latency_seconds_bucket[1m])))",
        },
        "queue_p95": {
            "title": "Queue Time P95",
            "unit": " s",
            "query": "histogram_quantile(0.95, sum by(le, model_name)(rate(vllm:request_queue_time_seconds_bucket[1m])))",
        },
        "success_rate": {
            "title": "成功请求速率",
            "unit": "/s",
            "query": "sum by(model_name)(rate(vllm:request_success_total[1m]))",
        },
    }


def _gpu_metric_specs() -> Dict[str, Dict[str, str]]:
    return {
        "gpu_util": {
            "title": "GPU 利用率",
            "unit": "%",
            "query": "avg by(instance)(DCGM_FI_DEV_GPU_UTIL)",
        },
        "gpu_memory_used": {
            "title": "GPU 显存使用率",
            "unit": "%",
            "query": "sum by(instance)(DCGM_FI_DEV_FB_USED) / sum by(instance)(DCGM_FI_DEV_FB_TOTAL) * 100",
        },
        "gpu_temp": {
            "title": "GPU 温度",
            "unit": "℃",
            "query": "avg by(instance)(DCGM_FI_DEV_GPU_TEMP)",
        },
    }


def _gpu_device_metric_specs() -> Dict[str, Dict[str, str]]:
    return {
        "gpu_util_per_device": {
            "title": "单卡 GPU 利用率",
            "unit": "%",
            "query": "avg by(instance, gpu)(DCGM_FI_DEV_GPU_UTIL)",
        },
        "gpu_memory_used_per_device": {
            "title": "单卡 GPU 显存使用率",
            "unit": "%",
            "query": "avg by(instance, gpu)(DCGM_FI_DEV_FB_USED / DCGM_FI_DEV_FB_TOTAL * 100)",
        },
        "gpu_temp_per_device": {
            "title": "单卡 GPU 温度",
            "unit": "℃",
            "query": "avg by(instance, gpu)(DCGM_FI_DEV_GPU_TEMP)",
        },
    }


def _node_metric_specs() -> Dict[str, Dict[str, str]]:
    return {
        "node_cpu_usage": {
            "title": "节点 CPU 使用率",
            "unit": "%",
            "query": 'avg by(instance)(1 - rate(node_cpu_seconds_total{mode="idle"}[1m])) * 100',
        },
        "node_memory_usage": {
            "title": "节点内存使用率",
            "unit": "%",
            "query": "(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100",
        },
        "node_rootfs_usage": {
            "title": "节点根分区使用率",
            "unit": "%",
            "query": '100 * (1 - max by(instance)(node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay"}) / max by(instance)(node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay"}))',
        },
        "node_disk_read": {
            "title": "节点磁盘读取",
            "unit": " MB/s",
            "query": 'sum by(instance)(rate(node_disk_read_bytes_total[1m])) / 1024 / 1024',
        },
        "node_disk_write": {
            "title": "节点磁盘写入",
            "unit": " MB/s",
            "query": 'sum by(instance)(rate(node_disk_written_bytes_total[1m])) / 1024 / 1024',
        },
        "node_network_receive": {
            "title": "节点网络接收",
            "unit": " MB/s",
            "query": 'sum by(instance)(rate(node_network_receive_bytes_total{device!="lo"}[1m])) / 1024 / 1024',
        },
        "node_network_transmit": {
            "title": "节点网络发送",
            "unit": " MB/s",
            "query": 'sum by(instance)(rate(node_network_transmit_bytes_total{device!="lo"}[1m])) / 1024 / 1024',
        },
    }


async def _query_metric_charts(
    base_url: str,
    specs: Dict[str, Dict[str, str]],
    *,
    start_ts: str,
    end_ts: str,
    step: str,
    label_key: str = "model_name",
) -> Dict[str, Any]:
    charts: Dict[str, Any] = {}
    for key, spec in specs.items():
        result = await asyncio.to_thread(
            _prom_query_url_sync,
            base_url,
            spec["query"],
            query_type="range",
            start=start_ts,
            end=end_ts,
            step=step,
        )
        charts[key] = {
            "title": spec["title"],
            "unit": spec["unit"],
            "query": spec["query"],
            "series": [] if result.get("error") else _prom_series_to_chart_series(result, label_key=label_key),
            "error": result.get("error"),
        }
    return charts


async def _await_with_timeout(awaitable: Any, timeout_s: float, fallback: Any, label: str) -> Any:
    """Return fallback instead of failing the whole dashboard when one source hangs."""
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_s)
    except Exception as exc:
        logger.warning("AI dashboard source timed out or failed: %s: %s", label, exc)
        if isinstance(fallback, dict):
            result = dict(fallback)
            result.setdefault("available", False)
            result.setdefault("error", str(exc) or f"{label} timed out")
            result.setdefault("message", f"{label} 数据源超时或不可用，已降级返回其余数据。")
            return result
        return fallback


@app.get("/api/fault-targets/llm-host/{host_id}/metrics-history")
async def llm_host_metrics_history(
    host_id: str,
    duration: str = "15m",
    step: str = "",
    prometheus_url: str = "",
    metrics_endpoint: str = "http://127.0.0.1:8000/metrics",
):
    """Return vLLM inference metric history for a GPU host.

    Prometheus gives stable time-series storage. If it is not configured, the
    endpoint falls back to a host-direct snapshot so the UI can still explain
    that no historical backend is available.
    """
    host = _find_llm_host(host_id)
    if not host:
        raise HTTPException(404, f"llm host '{host_id}' not found")
    _require_llm_host_address(host, host_id)

    dur_seconds = _duration_seconds(duration, 900)
    now = int(time.time())
    start_ts = str(now - dur_seconds)
    end_ts = str(now)
    if not step:
        step = "5s" if dur_seconds <= 600 else "15s" if dur_seconds <= 1800 else "30s"

    base_url = (
        prometheus_url.strip().rstrip("/")
        or str(host.get("prometheus_url") or "").strip().rstrip("/")
        or _discover_prometheus_url()
    )

    if base_url and _prometheus_api_ok(base_url):
        charts = await _query_metric_charts(base_url, _llm_metric_specs(), start_ts=start_ts, end_ts=end_ts, step=step)
        return {
            "mode": "prometheus",
            "host": _redact_host_runtime_fields(host),
            "prometheus_url": base_url,
            "duration": duration,
            "step": step,
            "charts": charts,
            "storage": {
                "available": True,
                "message": "Prometheus 正在提供 vLLM 指标历史，可展示故障前后趋势。",
            },
        }

    snapshot = await _observe_llm_host_direct(host, metrics_endpoint=metrics_endpoint)
    return {
        "mode": "host_direct_snapshot",
        "host": _redact_host_runtime_fields(host),
        "duration": duration,
        "step": step,
        "metrics_endpoint": metrics_endpoint,
        "snapshot": snapshot.get("observation") if isinstance(snapshot, dict) else None,
        "storage": {
            "available": False,
            "message": "未配置可用 Prometheus；当前仅能显示一次性 SSH+/metrics 快照，无法展示稳定时序趋势。",
        },
    }


def _chart_latest(charts: Dict[str, Any], key: str) -> Optional[float]:
    series = ((charts.get(key) or {}).get("series") or [])
    for item in series:
        values = item.get("values") or []
        if values:
            try:
                number = float(values[-1])
                if math.isfinite(number):
                    return number
            except (TypeError, ValueError):
                continue
    return None


def _chart_peak(charts: Dict[str, Any], key: str) -> Optional[float]:
    series = ((charts.get(key) or {}).get("series") or [])
    peaks: List[float] = []
    for item in series:
        for value in item.get("values") or []:
            try:
                number = float(value)
                if math.isfinite(number):
                    peaks.append(number)
            except (TypeError, ValueError):
                continue
    return max(peaks) if peaks else None


def _chart_highest_latest(charts: Dict[str, Any], key: str) -> Dict[str, Any]:
    """Identify the node with the highest latest sample, not the historical peak."""
    candidates = []
    for item in (charts.get(key) or {}).get("series") or []:
        values = item.get("values") or []
        if not values:
            continue
        try:
            number = float(values[-1])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(number):
            continue
        metric = item.get("metric") or {}
        node = str(metric.get("node") or metric.get("instance") or item.get("label") or "")
        candidates.append((number, node))
    if not candidates:
        return {"value": None, "node": ""}
    value, node = max(candidates)
    return {"value": value, "node": node}


def _prometheus_targets_sync(base_url: str) -> Dict[str, Any]:
    if not base_url:
        return {"available": False, "targets": [], "error": "prometheus url is empty"}
    import requests as req
    try:
        resp = req.get(f"{base_url.rstrip('/')}/api/v1/targets", timeout=8)
        resp.raise_for_status()
        data = resp.json()
        active = data.get("data", {}).get("activeTargets", []) if data.get("status") == "success" else []
        targets = []
        for item in active:
            labels = item.get("labels") or {}
            discovered = item.get("discoveredLabels") or {}
            targets.append({
                "job": labels.get("job") or discovered.get("__meta_kubernetes_pod_label_app") or "-",
                "instance": labels.get("instance") or discovered.get("__address__") or "-",
                "service": labels.get("service") or labels.get("app") or "",
                "health": item.get("health") or "unknown",
                "last_scrape": item.get("lastScrape") or "",
                "scrape_url": item.get("scrapeUrl") or "",
                "error": item.get("lastError") or "",
                "labels": labels,
            })
        return {"available": True, "targets": targets}
    except Exception as exc:
        return {"available": False, "targets": [], "error": str(exc)}


def _prom_instant_values(base_url: str, query: str) -> List[Dict[str, Any]]:
    result = _prom_query_url_sync(base_url, query)
    if result.get("error"):
        return []
    values: List[Dict[str, Any]] = []
    for item in result.get("results") or []:
        metric = item.get("metric") or {}
        raw = item.get("value") or []
        value = None
        if isinstance(raw, list) and len(raw) >= 2:
            try:
                value = float(raw[1])
            except (TypeError, ValueError):
                value = None
        values.append({"metric": metric, "value": value})
    return values


def _prom_instant_first_value(base_url: str, query: str) -> Optional[float]:
    """Return the first numeric instant Prometheus value for lightweight checks."""
    for item in _prom_instant_values(base_url, query):
        value = item.get("value")
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _prom_range_numeric_summary(
    base_url: str,
    query: str,
    *,
    start: float,
    end: float,
    step_s: int = 15,
) -> Dict[str, Any]:
    """Summarize numeric samples from a bounded Prometheus range query."""
    result = _prom_query_url_sync(
        base_url,
        query,
        query_type="range",
        start=str(float(start)),
        end=str(float(end)),
        step=f"{max(1, int(step_s))}s",
    )
    values: List[float] = []
    for item in result.get("results") or []:
        for sample in item.get("values") or []:
            if not isinstance(sample, list) or len(sample) < 2:
                continue
            try:
                value = float(sample[1])
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                values.append(value)
    if not values:
        return {"count": 0, "min": None, "max": None, "last": None}
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "last": values[-1],
    }


async def _ai_event_light_dashboard_payload(
    cluster: Dict[str, Any],
    *,
    duration: str = "15m",
    prometheus_url: str = "",
) -> Dict[str, Any]:
    """Build a fast event-detection payload without full dashboard fan-out.

    The full AI dashboard intentionally gathers topology, charts, target
    inventory, trace sync, and TELLER summaries. Event-center refresh must stay
    responsive during demos and live detection, so it only samples the signals
    needed by the detector.
    """
    base_url = _reachable_cluster_prometheus_url(cluster, prometheus_url)
    storage = {
        "available": False,
        "message": "Prometheus 不可用；事件检测将依赖 K8s Event、故障实验和已缓存 Trace。",
    }
    summary: Dict[str, Any] = {}
    peaks: Dict[str, Any] = {}
    if base_url and _prometheus_api_ok(base_url):
        storage = {
            "available": True,
            "message": "Prometheus 正在提供智算事件检测指标。",
        }
        queries = {
            "running_requests": "sum(vllm:num_requests_running)",
            "waiting_requests": "sum(vllm:num_requests_waiting)",
            "success_rps": "sum(rate(vllm:request_success_total[1m]))",
            "ttft_p95": "histogram_quantile(0.95, sum by(le)(rate(vllm:time_to_first_token_seconds_bucket[1m])))",
            "tpot_p95": "histogram_quantile(0.95, sum by(le)(rate(vllm:time_per_output_token_seconds_bucket[1m])))",
            "e2e_latency_p95": "histogram_quantile(0.95, sum by(le)(rate(vllm:e2e_request_latency_seconds_bucket[1m])))",
            "kv_cache": "max(vllm:gpu_cache_usage_perc) * 100",
            "gpu_util": "avg(DCGM_FI_DEV_GPU_UTIL)",
            "gpu_memory_used": "sum(DCGM_FI_DEV_FB_USED) / sum(DCGM_FI_DEV_FB_TOTAL) * 100",
            "gpu_temp": "avg(DCGM_FI_DEV_GPU_TEMP)",
            "node_cpu_usage": 'avg(1 - rate(node_cpu_seconds_total{mode="idle"}[1m])) * 100',
            "node_memory_usage": "avg(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100",
            "node_rootfs_usage": 'max(100 * (1 - node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay"} / node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay"}))',
            "node_disk_read": "sum(rate(node_disk_read_bytes_total[1m])) / 1024 / 1024",
            "node_disk_write": "sum(rate(node_disk_written_bytes_total[1m])) / 1024 / 1024",
            "node_network_receive": 'sum(rate(node_network_receive_bytes_total{device!="lo"}[1m])) / 1024 / 1024',
            "node_network_transmit": 'sum(rate(node_network_transmit_bytes_total{device!="lo"}[1m])) / 1024 / 1024',
        }
        results = await asyncio.gather(
            *[
                asyncio.to_thread(_prom_instant_first_value, base_url, query)
                for query in queries.values()
            ],
            return_exceptions=True,
        )
        for key, value in zip(queries.keys(), results):
            summary[key] = None if isinstance(value, Exception) else value
        peaks.update({key: value for key, value in summary.items() if value is not None})

    fine_grained_trace = {
        "available": False,
        "configured": bool(_cluster_teller_trace_dir(cluster, "")),
        "summary": {},
        "request_context": {
            "available": False,
            "request_count": 0,
            "message": "事件中心采用轻量检测，不阻塞扫描 TELLER 文件；请在智算观测页查看完整 Trace。",
        },
    }
    return {
        "mode": "event_light",
        "cluster": _redact_cluster_runtime_fields(cluster),
        "prometheus_url": base_url,
        "duration": duration,
        "storage": storage,
        "summary": summary,
        "peaks": peaks,
        "fine_grained_trace": fine_grained_trace,
    }


def _summarize_prometheus_inventory(base_url: str) -> Dict[str, Any]:
    if not base_url:
        return {"targets": {"available": False, "targets": []}, "gpu_devices": [], "vllm_series": []}
    gpu_by_key: Dict[str, Dict[str, Any]] = {}

    def gpu_record(metric: Dict[str, Any]) -> Dict[str, Any]:
        node = metric.get("node") or metric.get("Hostname") or metric.get("instance") or "-"
        return {
            "instance": metric.get("instance") or node,
            "node": node,
            "hostname": metric.get("Hostname") or node,
            "gpu": metric.get("gpu") or metric.get("device") or "-",
            "device": metric.get("device") or "",
            "uuid": metric.get("UUID") or metric.get("uuid") or "",
            "model": metric.get("modelName") or metric.get("model") or "",
        }

    def merge_gpu_metric(metric: Dict[str, Any], field: str, value: Any) -> None:
        record = gpu_record(metric)
        key = f"{record.get('instance') or record.get('node') or '-'}::{record.get('gpu') or '-'}"
        current = gpu_by_key.setdefault(key, record)
        for name in ("node", "hostname", "instance", "device", "uuid", "model"):
            if not current.get(name) and record.get(name):
                current[name] = record[name]
        current[field] = value

    for item in _prom_instant_values(base_url, "DCGM_FI_DEV_GPU_UTIL"):
        metric = item.get("metric") or {}
        merge_gpu_metric(metric, "utilization", item.get("value"))
    for item in _prom_instant_values(base_url, "DCGM_FI_DEV_FB_USED"):
        metric = item.get("metric") or {}
        merge_gpu_metric(metric, "memory_used_mib", item.get("value"))
    for item in _prom_instant_values(base_url, "DCGM_FI_DEV_FB_TOTAL"):
        metric = item.get("metric") or {}
        merge_gpu_metric(metric, "memory_total_mib", item.get("value"))
    for item in _prom_instant_values(base_url, "DCGM_FI_DEV_GPU_TEMP"):
        metric = item.get("metric") or {}
        merge_gpu_metric(metric, "temperature", item.get("value"))

    gpu_devices = []
    for item in gpu_by_key.values():
        used = item.get("memory_used_mib")
        total = item.get("memory_total_mib")
        item["memory_used_pct"] = round((used / total * 100), 2) if used is not None and total else None
        gpu_devices.append(item)
    gpu_devices.sort(key=lambda x: (str(x.get("instance") or ""), str(x.get("gpu") or "")))

    vllm_series = []
    vllm_query = (
        "vllm:num_requests_running or "
        "vllm:num_requests_waiting or "
        "vllm:gpu_cache_usage_perc or "
        "vllm:avg_generation_throughput_toks_per_s"
    )
    for item in _prom_instant_values(base_url, vllm_query):
        metric = item.get("metric") or {}
        name = metric.get("model_name") or metric.get("served_model_name") or metric.get("model") or ""
        vllm_series.append({
            "instance": metric.get("instance") or "-",
            "job": metric.get("job") or "",
            "model": name,
            "metric": metric.get("__name__") or "",
            "value": item.get("value"),
            "running": item.get("value") if metric.get("__name__") == "vllm:num_requests_running" else None,
        })
    return {
        "targets": _prometheus_targets_sync(base_url),
        "gpu_devices": gpu_devices,
        "vllm_series": vllm_series,
    }


def _compact_k8s_node(item: Dict[str, Any]) -> Dict[str, Any]:
    meta = item.get("metadata") or {}
    status = item.get("status") or {}
    info = status.get("nodeInfo") or {}
    addresses = {a.get("type"): a.get("address") for a in status.get("addresses") or []}
    conditions = status.get("conditions") or []
    ready = next((c for c in conditions if c.get("type") == "Ready"), {})
    alloc = status.get("allocatable") or {}
    return {
        "name": meta.get("name") or "",
        "ready": ready.get("status") == "True",
        "internal_ip": addresses.get("InternalIP") or "",
        "roles": ",".join(k.split("/", 1)[-1] for k in (meta.get("labels") or {}) if k.startswith("node-role.kubernetes.io/")) or "worker",
        "kubelet_version": info.get("kubeletVersion") or "",
        "runtime": info.get("containerRuntimeVersion") or "",
        "os": info.get("osImage") or "",
        "cpu": alloc.get("cpu") or "",
        "memory": alloc.get("memory") or "",
        "gpu": alloc.get("nvidia.com/gpu") or "",
    }


def _container_cli_option(parts: List[Any], names: List[str]) -> str:
    text_parts = [str(p).strip() for p in parts if str(p).strip()]
    for idx, part in enumerate(text_parts):
        for name in names:
            if part == name and idx + 1 < len(text_parts):
                return text_parts[idx + 1].strip()
            if part.startswith(f"{name}="):
                return part.split("=", 1)[1].strip()
    return ""


def _container_env_option(container: Dict[str, Any], names: List[str]) -> str:
    expected = {n.upper() for n in names}
    for env in container.get("env") or []:
        name = str(env.get("name") or "").upper()
        if name in expected and env.get("value") is not None:
            return str(env.get("value") or "").strip()
    return ""


def _extract_vllm_served_model(container: Dict[str, Any]) -> str:
    parts = list(container.get("command") or []) + list(container.get("args") or [])
    served = _container_cli_option(parts, ["--served-model-name", "--served_model_name"])
    if served:
        return served
    served = _container_env_option(container, ["SERVED_MODEL_NAME", "VLLM_SERVED_MODEL_NAME"])
    if served:
        return served
    model = _container_cli_option(parts, ["--model", "--model-name", "--model_name"])
    if model and not model.startswith("/"):
        return model
    model = _container_env_option(container, ["MODEL_NAME", "VLLM_MODEL_NAME"])
    return model


def _compact_k8s_pod(item: Dict[str, Any]) -> Dict[str, Any]:
    meta = item.get("metadata") or {}
    spec = item.get("spec") or {}
    status = item.get("status") or {}
    containers = spec.get("containers") or []
    container_statuses = {s.get("name"): s for s in status.get("containerStatuses") or []}
    owner_refs = meta.get("ownerReferences") or []
    served_models = sorted({m for c in containers for m in [_extract_vllm_served_model(c)] if m})
    return {
        "name": meta.get("name") or "",
        "namespace": meta.get("namespace") or "",
        "node": spec.get("nodeName") or "",
        "phase": status.get("phase") or "",
        "pod_ip": status.get("podIP") or "",
        "host_ip": status.get("hostIP") or "",
        "restarts": sum(int((s or {}).get("restartCount") or 0) for s in status.get("containerStatuses") or []),
        "ready": sum(1 for s in status.get("containerStatuses") or [] if s.get("ready")),
        "containers": len(containers),
        "container_specs": [
            {
                "name": c.get("name") or "",
                "image": c.get("image") or "",
                "ports": [
                    {
                        "name": p.get("name") or "",
                        "containerPort": p.get("containerPort"),
                        "protocol": p.get("protocol") or "",
                    }
                    for p in c.get("ports") or []
                ],
                "resources": c.get("resources") or {},
            }
            for c in containers
        ],
        "container_statuses": [
            {
                "name": name or "",
                "ready": bool(s.get("ready")),
                "restartCount": int(s.get("restartCount") or 0),
                "image": s.get("image") or "",
                "state": next(iter((s.get("state") or {}).keys()), ""),
            }
            for name, s in container_statuses.items()
        ],
        "images": [c.get("image") for c in containers if c.get("image")],
        "served_models": served_models,
        "served_model_name": served_models[0] if served_models else "",
        "owner": owner_refs[0].get("name") if owner_refs else "",
        "app": (meta.get("labels") or {}).get("app") or "",
        "labels": meta.get("labels") or {},
    }


def _compact_k8s_service(item: Dict[str, Any]) -> Dict[str, Any]:
    meta = item.get("metadata") or {}
    spec = item.get("spec") or {}
    ports = []
    port_records = []
    for p in spec.get("ports") or []:
        text = f"{p.get('port')}"
        if p.get("nodePort"):
            text += f":{p.get('nodePort')}"
        if p.get("protocol"):
            text += f"/{p.get('protocol')}"
        ports.append(text)
        port_records.append({
            "name": p.get("name") or "",
            "port": p.get("port"),
            "target_port": p.get("targetPort"),
            "node_port": p.get("nodePort"),
            "protocol": p.get("protocol") or "TCP",
        })
    return {
        "name": meta.get("name") or "",
        "namespace": meta.get("namespace") or "",
        "type": spec.get("type") or "",
        "cluster_ip": spec.get("clusterIP") or "",
        "ports": ports,
        "port_records": port_records,
        "selector": spec.get("selector") or {},
    }


async def _collect_ai_k8s_inventory(cluster: Dict[str, Any]) -> Dict[str, Any]:
    namespace = str(cluster.get("default_namespace") or "default").strip() or "default"
    cluster_id = str(cluster.get("id") or "") or None
    inventory = {
        "available": False,
        "namespace": namespace,
        "nodes": [],
        "pods": [],
        "pod_scope": "non_terminal",
        "services": [],
        "message": "未配置 kubeconfig/context，无法读取 K8s 实时对象；已使用 Prometheus 构建观测拓扑。",
    }
    if not (cluster.get("kubeconfig") or cluster.get("context")):
        return inventory

    node_res, pod_res, svc_res = await asyncio.gather(
        asyncio.to_thread(_shell_sync, "kubectl get nodes -o json", 30, cluster_id),
        asyncio.to_thread(
            _shell_sync,
            f"kubectl get pods -n {shlex.quote(namespace)} "
            "--field-selector='status.phase!=Succeeded,status.phase!=Failed' -o json",
            30, cluster_id,
        ),
        asyncio.to_thread(_shell_sync, f"kubectl get svc -n {shlex.quote(namespace)} -o json", 30, cluster_id),
    )
    inventory["available"] = bool(node_res.get("ok") or pod_res.get("ok") or svc_res.get("ok"))
    errors = []
    for name, res in (("nodes", node_res), ("pods", pod_res), ("services", svc_res)):
        if not res.get("ok"):
            errors.append(f"{name}: {res.get('stderr') or res.get('stdout') or 'failed'}")
            continue
        try:
            payload = json.loads(res.get("stdout") or "{}")
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            continue
        if name == "nodes":
            inventory["nodes"] = [_compact_k8s_node(i) for i in payload.get("items") or []]
        elif name == "pods":
            inventory["pods"] = [
                _compact_k8s_pod(i) for i in payload.get("items") or []
                if (i.get("status") or {}).get("phase") not in ("Succeeded", "Failed")
            ]
        else:
            inventory["services"] = [_compact_k8s_service(i) for i in payload.get("items") or []]
    inventory["message"] = "; ".join(errors) if errors else "K8s 实时对象已采集。"
    return inventory


def _derive_ai_served_models(
    cluster: Dict[str, Any],
    k8s_inventory: Dict[str, Any],
    prometheus_inventory: Dict[str, Any],
) -> List[Dict[str, Any]]:
    records: Dict[str, Dict[str, Any]] = {}

    def upsert(name: str, source: str, **fields: Any) -> None:
        model_name = str(name or "").strip()
        if not model_name:
            return
        key = model_name.lower()
        record = records.setdefault(key, {"name": model_name, "sources": []})
        if source and source not in record["sources"]:
            record["sources"].append(source)
        for field, value in fields.items():
            if value not in (None, "", [], {}):
                record[field] = value

    for pod in k8s_inventory.get("pods") or []:
        for model_name in pod.get("served_models") or []:
            upsert(
                model_name,
                "k8s_pod",
                namespace=pod.get("namespace"),
                pod=pod.get("name"),
                node=pod.get("node"),
                phase=pod.get("phase"),
            )
    for item in prometheus_inventory.get("vllm_series") or []:
        upsert(
            item.get("model") or "",
            "prometheus",
            instance=item.get("instance"),
            running=item.get("running"),
        )
    upsert(
        cluster.get("model") or "",
        "cluster_config",
        endpoint=cluster.get("vllm_endpoint"),
        namespace=cluster.get("default_namespace"),
    )
    return sorted(records.values(), key=lambda x: (x.get("pod") is None, x.get("name") or ""))


@app.get("/api/ai-clusters/{cluster_id}/dashboard")
async def ai_cluster_dashboard(
    cluster_id: str,
    duration: str = "15m",
    step: str = "",
    start: str = "",
    end: str = "",
    prometheus_url: str = "",
    teller_trace_dir: str = "",
):
    """Return an AdminLTE-style aggregate dashboard payload for an AI/vLLM cluster."""
    cluster = _find_k8s_cluster(cluster_id)
    if not cluster:
        raise HTTPException(404, f"cluster '{cluster_id}' not found")
    if _cluster_kind(cluster) != "ai":
        raise HTTPException(400, f"cluster '{cluster_id}' is not marked as an AI cluster")

    now = int(time.time())
    start_epoch = _parse_epoch_seconds(start)
    end_epoch = _parse_epoch_seconds(end)
    if start_epoch and end_epoch and end_epoch > start_epoch:
        dur_seconds = max(1, min(end_epoch - start_epoch, 7 * 86400))
        start_ts = str(start_epoch)
        end_ts = str(end_epoch)
        effective_duration = f"{dur_seconds}s"
    else:
        dur_seconds = _duration_seconds(duration, 900)
        start_epoch = now - dur_seconds
        end_epoch = now
        start_ts = str(start_epoch)
        end_ts = str(end_epoch)
        effective_duration = duration
    if not step:
        step = "5s" if dur_seconds <= 600 else "15s" if dur_seconds <= 1800 else "30s"

    base_url = _reachable_cluster_prometheus_url(cluster, prometheus_url)
    explicit_teller_dir = bool(str(teller_trace_dir or "").strip())
    teller_dir = _cluster_teller_trace_dir(cluster, teller_trace_dir)
    related_hosts = [
        _redact_host_runtime_fields(h)
        for h in _get_config().fault_targets.llm_hosts
        if str(h.get("ai_cluster_id") or h.get("cluster_id") or "") == cluster_id
    ]

    storage = {
        "available": False,
        "message": "未配置可用 Prometheus；智算 Dashboard 只能展示集群档案，无法展示稳定时序趋势。",
    }
    llm_charts: Dict[str, Any] = {}
    gpu_charts: Dict[str, Any] = {}
    gpu_device_charts: Dict[str, Any] = {}
    node_charts: Dict[str, Any] = {}
    prometheus_inventory = {"targets": {"available": False, "targets": []}, "gpu_devices": [], "vllm_series": []}
    if base_url and _prometheus_api_ok(base_url):
        prometheus_result = await _await_with_timeout(
            asyncio.gather(
                _query_metric_charts(base_url, _llm_metric_specs(), start_ts=start_ts, end_ts=end_ts, step=step),
                _query_metric_charts(base_url, _gpu_metric_specs(), start_ts=start_ts, end_ts=end_ts, step=step, label_key="instance"),
                _query_metric_charts(base_url, _gpu_device_metric_specs(), start_ts=start_ts, end_ts=end_ts, step=step, label_key="instance,gpu"),
                _query_metric_charts(base_url, _node_metric_specs(), start_ts=start_ts, end_ts=end_ts, step=step, label_key="instance"),
                asyncio.to_thread(_summarize_prometheus_inventory, base_url),
            ),
            18,
            None,
            "prometheus",
        )
        if prometheus_result:
            llm_charts, gpu_charts, gpu_device_charts, node_charts, prometheus_inventory = prometheus_result
            storage = {
                "available": True,
                "message": "Prometheus 正在提供智算集群 vLLM/GPU/节点基础资源指标历史。",
            }
        else:
            storage = {
                "available": False,
                "message": "Prometheus 查询超时或不可用；已返回 K8s/TELLER 等其余数据。",
            }
    k8s_inventory = await _await_with_timeout(
        _collect_ai_k8s_inventory(cluster),
        18,
        {
            "available": False,
            "namespace": str(cluster.get("default_namespace") or "default").strip() or "default",
            "nodes": [],
            "pods": [],
            "services": [],
        },
        "k8s_inventory",
    )
    if explicit_teller_dir:
        teller_trace_sync = {
            "ok": False,
            "skipped": True,
            "copied_dirs": [],
            "message": "已显式指定 TELLER trace 目录，跳过 K8s trace 同步，直接读取该目录。",
        }
    else:
        teller_trace_sync = await _await_with_timeout(
            asyncio.to_thread(
                _sync_teller_trace_from_k8s,
                cluster,
                teller_dir,
                namespace=str(cluster.get("default_namespace") or "llm-k8s"),
            ),
            12,
            {"ok": False, "copied_dirs": [], "message": "TELLER trace 同步超时，使用本地已缓存 trace 目录。"},
            "teller_trace_sync",
        )
    teller_run_prefix = str(cluster.get("vllm_deployment") or cluster.get("deployment") or "vllm-server")
    trace_summary_dir = teller_dir
    copied_dirs = teller_trace_sync.get("copied_dirs") or []
    if teller_trace_sync.get("ok") and copied_dirs:
        trace_summary_dir = str(copied_dirs[0])
    fine_grained_trace = await _await_with_timeout(
        asyncio.to_thread(_summarize_teller_trace, trace_summary_dir, 12000, run_prefix="" if explicit_teller_dir else teller_run_prefix, max_files=24),
        25,
        {
            "available": False,
            "configured": bool(trace_summary_dir),
            "trace_dir": trace_summary_dir,
            "summary": {},
            "request_context": {"available": False, "request_count": 0, "message": "TELLER trace 摘要超时。"},
            "e2e_trace": {
                "schema_version": "agenticsre.ai_trace.v1",
                "available": False,
                "coverage": "none",
                "request_count": 0,
                "requests": [],
                "gaps": ["TELLER trace 摘要超时，未生成端到端请求链路。"],
            },
        },
        "teller_trace_summary",
    )
    teller_integration = _build_teller_vllm_integration_plan(cluster, include_preflight=False)
    served_models = _derive_ai_served_models(cluster, k8s_inventory, prometheus_inventory)

    coverage_warnings = []
    if not storage['available']:
        coverage_warnings.append('本集群 Prometheus 未配置或不可达；不会使用其他集群的指标。')
    if not any(_chart_latest(gpu_charts, key) is not None for key in ('gpu_util', 'gpu_memory_used')):
        coverage_warnings.append('未采集到 GPU 指标，请检查 GPU 资源、设备插件与 DCGM Exporter。')
    if not any(_chart_latest(llm_charts, key) is not None for key in ('running', 'waiting', 'success_rate')):
        coverage_warnings.append('未采集到 vLLM 指标，请检查模型服务就绪状态与抓取配置。')
    storage['warnings'] = coverage_warnings

    node_usage = {
        key: _chart_highest_latest(node_charts, key)
        for key in ("node_cpu_usage", "node_memory_usage", "node_rootfs_usage")
    }
    summary = {
        "running_requests": _chart_latest(llm_charts, "running"),
        "waiting_requests": _chart_latest(llm_charts, "waiting"),
        "success_rate": _chart_latest(llm_charts, "success_rate"),
        "ttft_p95": _chart_latest(llm_charts, "ttft_p95"),
        "tpot_p95": _chart_latest(llm_charts, "tpot_p95"),
        "e2e_latency_p95": _chart_latest(llm_charts, "e2e_latency_p95"),
        "kv_cache": _chart_latest(llm_charts, "kv_cache"),
        "gpu_util": _chart_latest(gpu_charts, "gpu_util"),
        "gpu_memory_used": _chart_latest(gpu_charts, "gpu_memory_used"),
        "gpu_temp": _chart_latest(gpu_charts, "gpu_temp"),
        "node_cpu_usage": node_usage["node_cpu_usage"]["value"],
        "node_memory_usage": node_usage["node_memory_usage"]["value"],
        "node_rootfs_usage": node_usage["node_rootfs_usage"]["value"],
        "node_disk_read": _chart_latest(node_charts, "node_disk_read"),
        "node_disk_write": _chart_latest(node_charts, "node_disk_write"),
        "node_network_receive": _chart_latest(node_charts, "node_network_receive"),
        "node_network_transmit": _chart_latest(node_charts, "node_network_transmit"),
    }

    return {
        "mode": "prometheus" if storage["available"] else "profile_only",
        "cluster": _redact_cluster_runtime_fields(cluster),
        "hosts": related_hosts,
        "prometheus_url": base_url,
        "duration": effective_duration,
        "requested_duration": duration,
        "time_range": {
            "start": start_epoch,
            "end": end_epoch,
            "duration_s": dur_seconds,
            "mode": "absolute" if start and end else "relative",
        },
        "step": step,
        "storage": storage,
        "fine_grained_trace": fine_grained_trace,
        "teller_trace_sync": teller_trace_sync,
        "teller_integration": teller_integration,
        "summary": summary,
        "summary_nodes": {key: item["node"] for key, item in node_usage.items()},
        "charts": {
            "llm": llm_charts,
            "gpu": gpu_charts,
            "gpu_device": gpu_device_charts,
            "node": node_charts,
        },
        "inventory": {
            "k8s": k8s_inventory,
            "prometheus": prometheus_inventory,
        },
        "topology": {
            "nodes": k8s_inventory.get("nodes") or [],
            "pods": k8s_inventory.get("pods") or [],
            "services": k8s_inventory.get("services") or [],
            "targets": (prometheus_inventory.get("targets") or {}).get("targets") or [],
            "gpu_devices": prometheus_inventory.get("gpu_devices") or [],
            "vllm_series": prometheus_inventory.get("vllm_series") or [],
            "models": served_models,
        },
        "model": cluster.get("model") or (served_models[0].get("name") if served_models else ""),
        "vllm_endpoint": cluster.get("vllm_endpoint") or "",
    }


@app.get("/api/ai-clusters/{cluster_id}/topology")
async def ai_cluster_topology(
    cluster_id: str,
    duration: str = "15m",
    step: str = "",
    start: str = "",
    end: str = "",
    prometheus_url: str = "",
    teller_trace_dir: str = "",
):
    """Compatibility endpoint returning the AI cluster dependency topology."""
    payload = await ai_cluster_dashboard(
        cluster_id,
        duration=duration,
        step=step,
        start=start,
        end=end,
        prometheus_url=prometheus_url,
        teller_trace_dir=teller_trace_dir,
    )
    return {
        "cluster": payload.get("cluster") or {},
        "hosts": payload.get("hosts") or [],
        "topology": payload.get("topology") or {},
        "inventory": payload.get("inventory") or {},
        "storage": payload.get("storage") or {},
        "prometheus_url": payload.get("prometheus_url") or "",
        "time_range": payload.get("time_range") or {},
    }


@app.get("/api/ai-clusters/{cluster_id}/metrics")
async def ai_cluster_metrics(
    cluster_id: str,
    duration: str = "15m",
    step: str = "",
    start: str = "",
    end: str = "",
    prometheus_url: str = "",
    teller_trace_dir: str = "",
):
    """Compatibility endpoint returning AI cluster metric charts and summary."""
    payload = await ai_cluster_dashboard(
        cluster_id,
        duration=duration,
        step=step,
        start=start,
        end=end,
        prometheus_url=prometheus_url,
        teller_trace_dir=teller_trace_dir,
    )
    return {
        "cluster": payload.get("cluster") or {},
        "summary": payload.get("summary") or {},
        "charts": payload.get("charts") or {},
        "storage": payload.get("storage") or {},
        "prometheus_url": payload.get("prometheus_url") or "",
        "duration": payload.get("duration") or duration,
        "time_range": payload.get("time_range") or {},
        "step": payload.get("step") or step,
    }


@app.get("/api/ai-clusters/{cluster_id}/teller/integration-plan")
async def ai_cluster_teller_integration_plan(cluster_id: str):
    """Return a non-mutating plan for enabling real request-level TELLER on vLLM."""
    cluster = _find_k8s_cluster(cluster_id)
    if not cluster:
        raise HTTPException(404, f"cluster '{cluster_id}' not found")
    if _cluster_kind(cluster) != "ai":
        raise HTTPException(400, f"cluster '{cluster_id}' is not marked as an AI cluster")
    return _build_teller_vllm_integration_plan(cluster)


@app.get("/api/ai-clusters/{cluster_id}/teller/preflight")
async def ai_cluster_teller_preflight(cluster_id: str):
    """Run read-only checks before enabling request-level TELLER on vLLM."""
    cluster = _find_k8s_cluster(cluster_id)
    if not cluster:
        raise HTTPException(404, f"cluster '{cluster_id}' not found")
    if _cluster_kind(cluster) != "ai":
        raise HTTPException(400, f"cluster '{cluster_id}' is not marked as an AI cluster")
    return await asyncio.to_thread(_run_teller_vllm_preflight, cluster)


@app.get("/api/ai-events")
async def ai_events(limit: int = 100, status: str = "", cluster_id: str = "", duration: str = "1h"):
    """List AI-cluster events used to trigger diagnosis and recovery workflows."""
    events = _load_ai_events()
    window_s = _duration_seconds(duration, 3600)
    cutoff = time.time() - window_s if window_s > 0 else 0
    probe_clusters = [
        c for c in _get_config().fault_targets.k8s_clusters
        if _cluster_kind(c) == "ai" and (not cluster_id or str(c.get("id") or "") == cluster_id)
    ]
    if cluster_id:
        events = [e for e in events if str(e.get("cluster_id") or "") == cluster_id]
    if status:
        events = [e for e in events if str(e.get("status") or "") == status]
    if cutoff:
        events = [
            e for e in events
            if float(e.get("last_seen_at") or e.get("updated_at") or e.get("created_at") or 0) >= cutoff
        ]
    return {
        "events": _compact_ai_events_for_list(events, limit),
        "total": len(events),
        "duration": duration,
        "window": {"duration": duration, "seconds": window_s},
        "detection": await _ai_event_detection_config_for_clusters(probe_clusters),
    }


@app.post("/api/ai-events/refresh")
async def ai_events_refresh(request: Request):
    """Generate AI events from current dashboard metrics and recent LLM experiment records."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    cluster_id = str(body.get("cluster_id") or "")
    duration = str(body.get("duration") or "15m")
    window_s = _duration_seconds(duration, _AI_EVENT_DEFAULT_WINDOW_SECONDS)
    prometheus_url = str(body.get("prometheus_url") or "")
    clusters = [
        c for c in _get_config().fault_targets.k8s_clusters
        if _cluster_kind(c) == "ai" and (not cluster_id or str(c.get("id") or "") == cluster_id)
    ]
    generated: List[Dict[str, Any]] = []
    source_counts: Dict[str, int] = {}
    scanned_cluster_ids: List[str] = []
    scanned_clusters: List[Dict[str, Any]] = []
    for cluster in clusters:
        scanned_cluster_ids.append(str(cluster.get("id") or "default"))
        scanned_clusters.append(cluster)
        payload = await _ai_event_light_dashboard_payload(
            cluster,
            duration=duration,
            prometheus_url=prometheus_url,
        )
        dashboard_events = _generate_ai_events_from_dashboard_payload(payload, source="ai_dashboard")
        k8s_events = await _generate_ai_events_from_k8s_events(cluster, window_s=window_s)
        pod_health_events = await _generate_ai_events_from_pod_health(cluster, window_s=window_s)
        fault_run_events = _generate_ai_events_from_fault_runs(cluster, window_s=window_s)
        source_counts["ai_dashboard"] = source_counts.get("ai_dashboard", 0) + len(dashboard_events)
        source_counts["k8s_event"] = source_counts.get("k8s_event", 0) + len(k8s_events)
        source_counts["k8s_pod_health"] = source_counts.get("k8s_pod_health", 0) + len(pod_health_events)
        source_counts["fault_run"] = source_counts.get("fault_run", 0) + len(fault_run_events)
        if pod_health_events:
            logger.info(
                "AI events refresh generated %d pod health events for cluster %s",
                len(pod_health_events),
                cluster.get("id") or "default",
            )
        generated.extend(dashboard_events)
        generated.extend(k8s_events)
        generated.extend(pod_health_events)
        generated.extend(fault_run_events)

    include_experiments = body.get("include_experiments", True) is not False
    if include_experiments:
        for run in _load_fault_experiments()[:50]:
            spec = run.get("spec") if isinstance(run.get("spec"), dict) else {}
            if (spec.get("benchmark_type") or "llm") != "llm":
                continue
            run_cluster_id = str(spec.get("cluster_id") or cluster_id or "")
            if cluster_id and run_cluster_id and run_cluster_id != cluster_id:
                continue
            cluster = _find_k8s_cluster(run_cluster_id) if run_cluster_id else (clusters[0] if clusters else None)
            if not cluster:
                continue
            if run.get("status") in {"running", "failed", "cancelled", "completed"}:
                severity = "warning" if run.get("status") != "failed" else "critical"
                generated.append(_ai_event_base(
                    cluster=cluster,
                    event_type="fault_experiment_signal",
                    severity=severity,
                    title=f"智算连续实验 {run.get('status')}",
                    description=f"连续实验 {run.get('name') or run.get('id')} 包含 {len(spec.get('steps') or [])} 个故障步骤，可触发诊断/自愈分析。",
                    metric="fault_experiment",
                    value=run.get("status"),
                    threshold="diagnose",
                    source="fault_experiment",
                    experiment_run_id=run.get("id") or "",
                ))
                source_counts["fault_experiment"] = source_counts.get("fault_experiment", 0) + 1
    events = _merge_ai_events_refresh(generated, cluster_ids=scanned_cluster_ids)
    return {
        "generated": _compact_ai_events_for_list(generated, 100),
        "count": len(generated),
        "source_counts": source_counts,
        "events": _compact_ai_events_for_list(events, 100),
        "window": {"duration": duration, "seconds": window_s},
        "detection": await _ai_event_detection_config_for_clusters(scanned_clusters),
    }


@app.post("/api/ai-events/{event_id}/status")
async def ai_event_update_status(event_id: str, request: Request):
    body = await request.json()
    status = str(body.get("status") or "").strip()
    if status not in {"observing", "active", "acknowledged", "diagnosed", "healing", "resolved"}:
        raise HTTPException(400, "invalid status")
    events = _load_ai_events()
    event = next((e for e in events if e.get("id") == event_id), None)
    if not event:
        raise HTTPException(404, "AI event not found")
    event["status"] = status
    event["resolution"] = body.get("resolution") or event.get("resolution")
    return _compact_ai_event_for_list(_upsert_ai_event(event))


_AI_ANALYSIS_PHASES = ("context", "hypothesis", "plan", "investigation", "reasoning")
_AI_ANALYSIS_CORE_PHASES = ("investigation", "reasoning")


def _normalize_ai_analysis_phases(value: Any) -> List[str]:
    requested = value if isinstance(value, list) else list(_AI_ANALYSIS_PHASES)
    selected = [phase for phase in _AI_ANALYSIS_PHASES if phase in requested]
    for phase in _AI_ANALYSIS_CORE_PHASES:
        if phase not in selected:
            selected.append(phase)
    return [phase for phase in _AI_ANALYSIS_PHASES if phase in selected]


def _ai_event_rca_query(
    event: Dict[str, Any],
    payload: Dict[str, Any],
    mode: str = "analysis",
    analysis_phases: Optional[List[str]] = None,
) -> str:
    title = event.get("title") or event.get("event_type") or "智算事件"
    description = event.get("description") or ""
    if event.get("source") == "fault_experiment":
        title = "智算运行异常：推理服务观测信号异常"
        description = "诊断输入来自运行期观测信号，不包含实验场景 ID、注入故障类型或实验标签。"
    action = "分析根因、证据、影响范围和置信度"
    if mode == "healing":
        action = "分析根因并给出受控自愈建议、风险门禁和验证步骤"
    phases = _normalize_ai_analysis_phases(analysis_phases)
    phase_labels = {
        "context": "上下文检索",
        "hypothesis": "假设生成",
        "plan": "验证规划",
        "investigation": "调查取证",
        "reasoning": "推理裁决",
    }
    process_instruction = "、".join(phase_labels[phase] for phase in phases)
    return (
        f"[智算事件][{payload.get('fault_type') or event.get('event_type') or 'llm'}] "
        f"{title}; {description}; cluster={payload.get('cluster_id') or ''}; "
        f"namespace={payload.get('namespace') or 'llm-k8s'}; "
        f"deployment={payload.get('deployment') or 'vllm-server'}; "
        f"metric={event.get('metric') or ''}; value={event.get('value') or ''}; "
        f"本次仅执行已选分析阶段：{process_instruction}；未选阶段不得补做或伪造过程记录；"
        f"请输出中文结论，并{action}。"
    )


@app.post("/api/ai-events/{event_id}/agent")
async def ai_event_agent_workflow(event_id: str, request: Request):
    """Trigger the multi-agent RCA path for an AI-cluster event."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    events = _load_ai_events()
    event = next((e for e in events if e.get("id") == event_id), None)
    if not event:
        raise HTTPException(404, "AI event not found")
    payload = _ai_event_to_skill_payload(event, include_live_observation=False)
    namespace = str(payload.get("namespace") or "llm-k8s")
    mode = str(body.get("mode") or "analysis")
    analysis_phases = _normalize_ai_analysis_phases(body.get("analysis_phases"))
    query = _ai_event_rca_query(event, payload, mode=mode, analysis_phases=analysis_phases)
    try:
        class _RCARequest:
            async def json(self):
                return {
                    "query": query,
                    "namespace": namespace,
                    "cluster_id": str(payload.get("cluster_id") or ""),
                    "analysis_phases": analysis_phases,
                    "dedup_key": ":".join((
                        "ai-event",
                        event_id,
                        "multi-agent",
                        mode,
                        ",".join(analysis_phases),
                    )),
                    "initial_evidence": {
                        "source": "ai_event_center",
                        "payload": payload,
                    },
                }

        run_resp = await rca_run(_RCARequest())
        run_id = run_resp.get("run_id") if isinstance(run_resp, dict) else ""
        run_record = {
            "run_id": run_id,
            "query": query,
            "namespace": namespace,
            "mode": "multi_agent",
            "status": "running",
            "requested_stages": ["detect", "diagnose"] if mode != "healing" else ["detect", "diagnose", "heal", "verify"],
            "analysis_phases": analysis_phases,
            "created_at": time.time(),
            "reused": bool(run_resp.get("reused")) if isinstance(run_resp, dict) else False,
        }
        _ensure_ai_event_agent_runs(event)["multi_agent"] = run_record
        event["agent_run"] = event.get("agent_run") or run_record
        event["status"] = "diagnosed" if mode != "healing" else "healing"
        event["resolution"] = event.get("resolution") or "多智能体 RCA 已触发。"
        compact = _compact_ai_event_for_list(_upsert_ai_event(event))
        return {"event": compact, "agent_run": run_record}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, {"message": "AI event multi-agent workflow failed", "error": str(exc)})


@app.post("/api/ai-events/{event_id}/heal")
async def ai_event_heal_capability(event_id: str, request: Request):
    """Generate the multi-agent self-healing plan for an AI-cluster event."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    events = _load_ai_events()
    event = next((e for e in events if e.get("id") == event_id), None)
    if not event:
        raise HTTPException(404, "AI event not found")
    payload = _ai_event_to_skill_payload(event, include_live_observation=False)
    heal_payload = {
        **payload,
        "source": "ai_event_agent_heal",
        "alert": {
            "source": event.get("source") or "ai_event",
            "severity": event.get("severity") or "warning",
            "title": event.get("title") or event.get("event_type") or "智算事件",
            "description": event.get("description") or "",
            "namespace": payload.get("namespace") or "",
            "service": payload.get("service") or payload.get("deployment") or "",
            "labels": {
                "event_id": event_id,
                "event_type": event.get("event_type") or "",
                "cluster_id": event.get("cluster_id") or "",
            },
            "raw_data": event,
        },
        "object": payload.get("service") or payload.get("deployment") or "",
        "message": payload.get("message") or event.get("description") or "",
    }
    if isinstance(body.get("diagnosis"), dict):
        heal_payload["diagnosis"] = body.get("diagnosis")
    result = _build_heal_suggestions(heal_payload)
    cfg = _get_config()
    result["source"] = "ai_event_agent_heal"
    result["policy"] = {
        "enabled": cfg.remediation.enabled,
        "dry_run": getattr(cfg.remediation, "dry_run", True),
        "require_approval": cfg.remediation.require_approval,
        "max_auto_risk_level": getattr(cfg.remediation, "max_auto_risk_level", "medium"),
        "recommend_only": getattr(cfg.remediation, "recommend_only", True),
    }
    result["summary"] = {
        "fault_type": result.get("fault_type"),
        "namespace": result.get("namespace"),
        "target": result.get("target"),
        "suggestion_count": len(result.get("suggestions") or []),
        "blocked_count": len(result.get("blocked_templates") or []),
        "available": bool(result.get("suggestions")),
    }
    event["heal_result"] = result
    event["status"] = "healing"
    event["resolution"] = event.get("resolution") or "多智能体自愈建议已生成，默认 Dry-run。"
    compact = _compact_ai_event_for_list(_upsert_ai_event(event))
    return {"event": compact, "heal_result": result}


@app.post("/api/ai-events/{event_id}/skill")
async def ai_event_skill_workflow(event_id: str, request: Request):
    body = await _request_json_or_empty(request)
    events = _load_ai_events()
    event = next((e for e in events if e.get("id") == event_id), None)
    if not event:
        raise HTTPException(404, "AI event not found")
    payload = _ai_event_to_skill_payload(event, include_live_observation=False)
    analysis_phases = _normalize_ai_analysis_phases(body.get("analysis_phases"))
    query = _ai_event_rca_query(
        event,
        payload,
        mode="healing" if "heal" in body.get("stages", []) else "analysis",
        analysis_phases=analysis_phases,
    )
    query += " 必须由 Agent 选择并调用匹配 Skill，记录 Skill 调用、工具调查证据、最终中文根因和置信度。"
    namespace = str(payload.get("namespace") or "llm-k8s")
    stages = body.get("stages") if isinstance(body.get("stages"), list) else ["detect", "diagnose", "heal", "verify"]
    skill_workflow = {
        "payload": payload,
        "domain": payload.get("domain") or "llm",
        "stages": stages,
        "analysis_phases": analysis_phases,
        "dry_run": True,
        "allow_legacy_fallback": True,
        "llm_timeout_s": _clamp_int(body.get("llm_timeout_s"), 60, 2, 300),
    }
    try:
        class _RCARequest:
            async def json(self):
                return {
                    "query": query,
                    "namespace": namespace,
                    "skill_workflow": skill_workflow,
                    "dedup_key": ":".join((
                        "ai-event",
                        event_id,
                        "agent-skill",
                        ",".join(str(stage) for stage in stages),
                        ",".join(analysis_phases),
                    )),
                }

        run_resp = await rca_run(_RCARequest())
        run_id = run_resp.get("run_id") if isinstance(run_resp, dict) else ""
        event["agent_run"] = {
            "run_id": run_id,
            "query": query,
            "namespace": namespace,
            "mode": "agent_skill",
            "status": "running",
            "requested_stages": stages,
            "analysis_phases": analysis_phases,
            "created_at": time.time(),
            "reused": bool(run_resp.get("reused")) if isinstance(run_resp, dict) else False,
        }
        _ensure_ai_event_agent_runs(event)["agent_skill"] = event["agent_run"]
        event["status"] = "diagnosed"
        event["resolution"] = event.get("resolution") or "Agent RCA 已触发，Skill 将由 Agent 工具链调用。"
        compact = _compact_ai_event_for_list(_upsert_ai_event(event))
        compact["agent_run"] = event["agent_run"]
        return {"event": compact, "agent_run": event["agent_run"]}
    except Exception as exc:
        raise HTTPException(503, {"message": "AI event Agent+Skill workflow failed", "error": str(exc)})


@app.post("/api/ai-clusters/{cluster_id}/teller/collect")
async def ai_cluster_teller_collect(cluster_id: str, request: Request):
    """Run one controlled TELLER/CUPTI-style GPU profiler collection job."""
    cluster = _find_k8s_cluster(cluster_id)
    if not cluster:
        raise HTTPException(404, f"cluster '{cluster_id}' not found")
    if _cluster_kind(cluster) != "ai":
        raise HTTPException(400, f"cluster '{cluster_id}' is not marked as an AI cluster")
    body = await request.json()
    if not bool(body.get("confirm")):
        raise HTTPException(400, "TELLER collection uses a short GPU profiler job and requires confirm=true.")

    namespace = str(body.get("namespace") or cluster.get("default_namespace") or "llm-k8s").strip() or "llm-k8s"
    node_name = str(body.get("node") or "").strip() or _find_vllm_node_for_cluster(cluster_id, namespace)
    image = str(body.get("image") or cluster.get("teller_probe_image") or cluster.get("vllm_image") or "vllm/vllm-openai:latest").strip()
    trace_dir = _cluster_teller_trace_dir(cluster, str(body.get("teller_trace_dir") or ""))
    iterations = max(1, min(int(body.get("iterations") or 8), 50))
    matrix_size = max(256, min(int(body.get("matrix_size") or 1024), 4096))
    timeout_seconds = max(30, min(int(body.get("timeout_seconds") or 180), 600))
    hold_seconds = max(30, min(int(body.get("hold_seconds") or 300), 900))
    gpu_limit = max(1, min(int(body.get("gpu_limit") or 1), 8))

    result = await asyncio.to_thread(
        _run_teller_probe_collection,
        cluster,
        namespace=namespace,
        node_name=node_name,
        image=image,
        iterations=iterations,
        matrix_size=matrix_size,
        timeout_seconds=timeout_seconds,
        hold_seconds=hold_seconds,
        gpu_limit=gpu_limit,
        trace_dir=trace_dir,
    )
    result["cluster"] = _redact_cluster_runtime_fields(cluster)
    return result


def _fault_run_target_key(run: Dict[str, Any]) -> tuple:
    """Return the resource identity used to pair an injection with cleanup."""
    return (
        str(run.get("target_mode") or "k8s"),
        str(run.get("cluster_id") or ""),
        str(run.get("namespace") or ""),
        str(run.get("deployment") or ""),
        str(run.get("scenario_id") or ""),
    )


def _active_fault_runs(runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return injections that have no newer matching cleanup record."""
    recovered_ids = set()
    recovered_targets = set()
    active: List[Dict[str, Any]] = []
    now = time.time()
    for run in runs:
        if str(run.get("status") or "") not in {"executed", "completed"}:
            continue
        action = str(run.get("action") or "")
        target_key = _fault_run_target_key(run)
        if action == "cleanup":
            recovered_targets.add(target_key)
            for item in run.get("results") or []:
                source_id = str(item.get("source_fault_run_id") or "") if isinstance(item, dict) else ""
                if source_id:
                    recovered_ids.add(source_id)
            continue
        if action != "inject":
            continue
        run_id = str(run.get("id") or "")
        if run_id in recovered_ids or target_key in recovered_targets:
            continue
        item = dict(run)
        started_at = float(item.get("started_at") or item.get("finished_at") or now)
        item["age_seconds"] = max(0, round(now - started_at, 1))
        item["recoverable"] = bool(_fault_run_recovery_command(item))
        active.append(item)
    return active


@app.get("/api/fault-runs")
async def fault_runs(limit: int = 50, active_only: bool = False):
    """Return recent real fault records or currently active injections."""
    runs = _load_fault_runs()
    selected = _active_fault_runs(runs) if active_only else runs
    return {
        "runs": selected[: max(1, min(limit, 200))],
        "total": len(selected),
        "active_only": active_only,
    }


@app.get("/api/fault-mode-versions")
async def fault_mode_versions(family: str = ""):
    """List immutable fault-mode catalog versions and the active pointer."""
    registry = _fault_mode_registry()
    families = [family] if family else list(FAMILY_META)
    try:
        return {
            "families": [registry.list_versions(item) for item in families],
            "policy": {
                "version_rollback_is_catalog_only": True,
                "runtime_fault_cleanup": "独立执行，不由版本回退触发",
            },
        }
    except FaultModeRegistryError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/fault-mode-versions/{family}/diff")
async def fault_mode_version_diff(family: str, from_version: str = "", to_version: str = ""):
    """Compare two catalog snapshots by scenario id and changed fields."""
    if not from_version or not to_version:
        raise HTTPException(400, "from_version and to_version are required")
    try:
        return _fault_mode_registry().diff(family, from_version, to_version)
    except FaultModeRegistryError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/fault-mode-versions/{family}/analyze")
async def fault_mode_version_analyze_get(family: str, version: str = ""):
    """Return deterministic catalog analysis without invoking an LLM."""
    try:
        return {"status": "ok", "analysis": _fault_mode_registry().analyze(family, version)}
    except FaultModeRegistryError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/fault-mode-versions/{family}/{version}")
async def fault_mode_version_detail(family: str, version: str):
    """Return one immutable catalog snapshot for authorized management clients."""
    try:
        return _fault_mode_registry().get_version(family, version)
    except FaultModeRegistryError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/fault-mode-versions/{family}/snapshot")
async def fault_mode_version_snapshot(family: str, request: Request):
    """Create an immutable snapshot of the current working-tree catalog."""
    body = await _request_json_or_empty(request)
    try:
        return _fault_mode_registry().create_snapshot(
            family,
            version=str(body.get("version") or ""),
            parent_version=str(body.get("parent_version") or ""),
            note=str(body.get("note") or ""),
            actor=str(body.get("actor") or ""),
        )
    except FaultModeRegistryError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/fault-mode-versions/{family}/merge")
async def fault_mode_version_merge(family: str, request: Request):
    """Preview or commit a deterministic union merge, requiring explicit conflict resolutions."""
    body = await _request_json_or_empty(request)
    base_version = str(body.get("base_version") or "")
    incoming_version = str(body.get("incoming_version") or "")
    if not base_version or not incoming_version:
        raise HTTPException(400, "base_version and incoming_version are required")
    resolutions = body.get("resolutions") if isinstance(body.get("resolutions"), dict) else {}
    try:
        return _fault_mode_registry().merge(
            family,
            base_version,
            incoming_version,
            target_version=str(body.get("target_version") or ""),
            resolutions={str(key): str(value) for key, value in resolutions.items()},
            note=str(body.get("note") or ""),
            actor=str(body.get("actor") or ""),
            commit=bool(body.get("commit")),
        )
    except FaultModeRegistryError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/fault-mode-versions/{family}/rollback")
async def fault_mode_version_rollback(family: str, request: Request):
    """Activate a previous catalog snapshot; this never executes runtime Undo."""
    body = await _request_json_or_empty(request)
    version = str(body.get("version") or "")
    if not version:
        raise HTTPException(400, "version is required")
    try:
        result = _fault_mode_registry().activate(
            family,
            version,
            actor=str(body.get("actor") or ""),
            reason=str(body.get("reason") or ""),
        )
    except FaultModeRegistryError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "status": "ok",
        "operation": "catalog_version_rollback",
        "note": "故障模式库版本回退，不执行任何故障清理或自愈动作",
        **result,
    }


def _recover_fault_mode_llm_json(value: Any) -> Optional[Dict[str, Any]]:
    """Recover one JSON object from models that wrap it in a short explanation."""
    if isinstance(value, dict) and not value.get("parse_error"):
        return value
    raw = str(value.get("raw_response") or "") if isinstance(value, dict) else str(value or "")
    raw = raw.strip()
    if not raw:
        return None
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _sanitize_fault_mode_llm_result(value: Dict[str, Any], allowed_ids: set[str]) -> Dict[str, Any]:
    """Keep advisory LLM output bounded and remove invented scenario identifiers."""
    result: Dict[str, Any] = {}
    for key in ("summary", "confidence"):
        if key in value:
            result[key] = value[key]
    patterns = value.get("cross_cutting_patterns")
    if isinstance(patterns, list):
        result["cross_cutting_patterns"] = [str(item)[:500] for item in patterns[:20]]
    for key in ("classes", "risks"):
        items = value.get(key)
        if not isinstance(items, list):
            continue
        sanitized = []
        for item in items[:30]:
            if not isinstance(item, dict):
                continue
            scenario_ids = [
                str(sid) for sid in item.get("scenario_ids", [])
                if str(sid) in allowed_ids
            ]
            sanitized.append({
                field: item.get(field)
                for field in ("name", "risk", "reason")
                if item.get(field) is not None
            } | {"scenario_ids": scenario_ids})
        result[key] = sanitized
    return result


@app.post("/api/fault-mode-versions/{family}/analyze")
async def fault_mode_version_analyze(family: str, request: Request):
    """Analyze a catalog deterministically and optionally ask an LLM for semantic grouping."""
    body = await _request_json_or_empty(request)
    version = str(body.get("version") or "")
    use_llm = bool(body.get("use_llm"))
    registry = _fault_mode_registry()
    try:
        deterministic = registry.analyze(family, version)
        document = registry.get_version(family, version)["document"] if version else registry.current_document(family)
    except FaultModeRegistryError as exc:
        raise HTTPException(400, str(exc)) from exc
    response: Dict[str, Any] = {"status": "ok", "analysis": deterministic, "llm": None}
    if not use_llm:
        return response
    cfg = _get_config()
    if not _llm_configured(cfg.llm):
        response["llm"] = {
            "status": "unavailable",
            "reason": "LLM 未配置 API Key；已返回确定性分类结果",
            "advisory_only": True,
        }
        return response
    scenarios = [
        item for item in document.get("scenarios", [])
        if isinstance(item, dict) and item.get("id")
    ]
    try:
        batch_size = max(3, min(10, int(os.environ.get("AGENTICSRE_FAULT_MODE_LLM_BATCH_SIZE", "6"))))
    except (TypeError, ValueError):
        batch_size = 6
    try:
        analysis_max_tokens = max(
            2048,
            min(16384, int(os.environ.get("AGENTICSRE_FAULT_MODE_LLM_MAX_TOKENS", "8192"))),
        )
    except (TypeError, ValueError):
        analysis_max_tokens = 8192
    analysis_model = str(
        os.environ.get("AGENTICSRE_FAULT_MODE_LLM_MODEL")
        or getattr(cfg.llm, "model", "")
        or ""
    ).strip()
    system_prompt = (
        "你是 SRE 故障模式分析助手。只做语义总结、分类和风险解释，不执行命令，也不决定版本发布、合并裁决或回退。"
        "本次只分析给定的一小批场景。请严格输出 JSON，不要输出 Markdown 或推理过程。"
        "字段必须为 summary、classes、cross_cutting_patterns、risks、confidence。"
        "classes 是对象数组，每项含 name、scenario_ids、reason；risks 是对象数组，每项含 scenario_ids、risk、reason；"
        "scenario_ids 只能使用输入中的 ID，不得编造。"
    )
    batches = [scenarios[index:index + batch_size] for index in range(0, len(scenarios), batch_size)]
    client = LLMClient(cfg.llm)

    async def _analyze_batch(batch: List[Dict[str, Any]]) -> Any:
        payload = registry.llm_input({"scenarios": batch})
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "请分析这一批故障模式：\n" + json.dumps(payload, ensure_ascii=False)},
        ]
        return await client.async_chat(
            messages,
            temperature=0.0,
            max_tokens=analysis_max_tokens,
            model=analysis_model or None,
        )

    try:
        batch_results = await asyncio.gather(
            *[_analyze_batch(batch) for batch in batches],
            return_exceptions=True,
        )
        allowed_ids = {str(item.get("id")) for item in scenarios}
        merged_classes: Dict[str, Dict[str, Any]] = {}
        merged_risks: List[Dict[str, Any]] = []
        patterns: List[str] = []
        summaries: List[str] = []
        succeeded = 0
        failures: List[str] = []
        for index, raw_result in enumerate(batch_results):
            if isinstance(raw_result, Exception):
                failures.append(f"batch-{index + 1}: {raw_result}")
                continue
            recovered = _recover_fault_mode_llm_json(raw_result)
            if recovered is None:
                failures.append(f"batch-{index + 1}: 大模型未返回可恢复的 JSON 对象")
                continue
            succeeded += 1
            sanitized = _sanitize_fault_mode_llm_result(recovered, allowed_ids)
            summary = str(sanitized.get("summary") or "").strip()
            if summary:
                summaries.append(summary[:600])
            for item in sanitized.get("classes", []):
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "未命名种类")
                current = merged_classes.setdefault(name, {"name": name, "scenario_ids": [], "reason": item.get("reason") or ""})
                current["scenario_ids"] = sorted(set(current["scenario_ids"]) | set(item.get("scenario_ids") or []))
                if not current.get("reason") and item.get("reason"):
                    current["reason"] = item["reason"]
            for item in sanitized.get("risks", []):
                if isinstance(item, dict):
                    merged_risks.append(item)
            for pattern in sanitized.get("cross_cutting_patterns", []):
                if pattern not in patterns:
                    patterns.append(pattern)
        llm_status = "ok" if succeeded == len(batches) else ("partial" if succeeded else "invalid")
        response["llm"] = {
            "status": llm_status,
            "model": analysis_model,
            "batch_size": batch_size,
            "batch_count": len(batches),
            "succeeded_batches": succeeded,
            "failed_batches": len(failures),
            "summary": "；".join(summaries)[:2400] if summaries else "",
            "classes": list(merged_classes.values()),
            "cross_cutting_patterns": patterns[:20],
            "risks": merged_risks[:30],
            "confidence": None,
            "failures": failures[:10],
            "advisory_only": True,
        }
    except Exception as exc:
        logger.warning("Fault-mode LLM analysis failed: %s", exc)
        response["llm"] = {"status": "error", "reason": str(exc), "model": analysis_model, "advisory_only": True}

    return response


@app.get("/api/faultlab/scenarios")
async def faultlab_scenarios():
    """List available general-compute K8s fault experiment scenarios."""
    return {"family": "通算故障实验", "scenarios": _load_fault_scenarios()}


@app.get("/api/faultlab/precision-scenarios")
async def precision_faultlab_scenarios():
    """List registered fine-grained scenarios and reviewed adapter readiness."""
    from skill_runtime.precision_execution import precision_adapter_preflight

    catalog = _load_precision_fault_catalog()
    scenarios = _load_precision_fault_scenarios()
    scenarios_by_id = {str(item.get("id") or ""): item for item in scenarios}
    latest_recovery_sources: Dict[str, Dict[str, str]] = {}
    for run in _load_fault_runs():
        scenario_id = str(run.get("scenario_id") or "")
        if scenario_id in latest_recovery_sources:
            continue
        scenario = scenarios_by_id.get(scenario_id)
        if not scenario:
            continue
        results = run.get("results") if isinstance(run.get("results"), list) else []
        result = results[0] if results and isinstance(results[0], dict) else {}
        verification = result.get("verification") if isinstance(result.get("verification"), dict) else {}
        validation = str(verification.get("validation") or "")
        cluster_id = str(run.get("cluster_id") or "")
        namespace = str(run.get("namespace") or "")
        if (
            str(run.get("action") or "") == "precision_experiment"
            and str(run.get("status") or "") == "completed"
            and str(result.get("status") or "") == "succeeded"
            and verification.get("cleanup_complete") is True
            and validation.startswith("PASS")
            and list(run.get("target_entity_ids") or []) == list(scenario.get("target_entity_ids") or [])
            and list(run.get("target_scopes") or []) == _precision_target_scopes(scenario, namespace)
            and run.get("transaction_undo") == _precision_transaction_undo(
                scenario, cluster_id=cluster_id or None, namespace=namespace,
            )
        ):
            latest_recovery_sources[scenario_id] = {
                "fault_run_id": str(run.get("id") or ""),
                "cluster_id": cluster_id,
                "namespace": namespace,
            }
    methods = Counter(str((item.get("precision") or {}).get("method") or "") for item in scenarios)
    enriched = []
    ready_count = 0
    for scenario in scenarios:
        preflight = precision_adapter_preflight(str(scenario.get("id") or ""), root_dir=ROOT_DIR)
        ready_count += int(bool(preflight.get("ready")))
        enriched.append({
            **scenario,
            "adapter": preflight.get("adapter"),
            "adapter_preflight": preflight,
            "latest_recovery_source": latest_recovery_sources.get(str(scenario.get("id") or "")),
        })
    return {
        "family": "AgenticSRE 细粒度故障注入",
        "execution_mode": str(catalog.get("execution_mode") or "approval_gated_transactional_execution"),
        "validation_status": str(catalog.get("validation_status") or "not_validated"),
        "scenario_count": len(scenarios),
        "adapter_ready_count": ready_count,
        "method_counts": dict(sorted((key, value) for key, value in methods.items() if key)),
        "scenarios": enriched,
    }


def _apply_ns_override(cmd: str, old_ns: str, new_ns: str) -> str:
    """Replace -n <old> / --namespace=<old> occurrences in a kubectl command."""
    if not new_ns or not old_ns or new_ns == old_ns:
        return cmd
    cmd = re.sub(rf'(-n\s+){re.escape(old_ns)}\b', rf'\g<1>{new_ns}', cmd)
    cmd = re.sub(rf'(--namespace[=\s]+){re.escape(old_ns)}\b', rf'\g<1>{new_ns}', cmd)
    return cmd


def _patch_offline_image(cmd: str) -> str:
    """Prefer images bundled in the offline package for fault-lab helper pods."""
    patched = str(cmd or "")
    if "--image=containerstack/alpine-stress" in patched and " stress --cpu " in patched:
        patched = re.sub(
            r"--image=containerstack/alpine-stress\s+--restart=Never\s+--\s+stress\s+--cpu\s+\d+\s+--timeout\s+(\d+)",
            r"""--image=alpine:3.18 --image-pull-policy=IfNotPresent --restart=Never -- /bin/sh -c 'trap "kill 0" TERM INT; for i in 1 2 3 4; do while true; do :; done & done; sleep \1 & wait'""",
            patched,
        )
    elif "--image=containerstack/alpine-stress" in patched and " stress --vm " in patched:
        patched = re.sub(
            r"--image=containerstack/alpine-stress\s+--restart=Never\s+(--limits='memory=[^']+'\s+)?--\s+stress\s+--vm\s+\d+\s+--vm-bytes\s+\S+\s+--timeout\s+(\d+)",
            r"--image=alpine:3.18 --image-pull-policy=IfNotPresent --restart=Never \1-- /bin/sh -c 'mkdir -p /dev/shm/agenticsre; dd if=/dev/zero of=/dev/shm/agenticsre/fill bs=1M count=128; sleep \2'",
            patched,
        )
    elif "--image=containerstack/alpine-stress" in patched:
        patched = patched.replace(
            "--image=containerstack/alpine-stress",
            "--image=alpine:3.18 --image-pull-policy=IfNotPresent",
        )
    if "--image=busybox" in patched:
        patched = patched.replace(
            "--image=busybox",
            "--image=alpine:3.18 --image-pull-policy=IfNotPresent",
        )
    return patched


def _strip_force_flag(cmd: str) -> str:
    """Use normal pod deletion for cleanup so kubelet can signal containers."""
    stripped = str(cmd or "")
    stripped = re.sub(r"\s+--force\b", "", stripped)
    stripped = re.sub(r"\s+--grace-period=0\b", "", stripped)
    stripped = re.sub(r"\s+--grace-period\s+0\b", "", stripped)
    return stripped


def _extract_kubectl_run_pod(cmd: str) -> Optional[tuple[str, str]]:
    match = re.search(
        r"\bkubectl(?:\s+(?:--context=\S+|--kubeconfig=\S+))*"
        r"(?:\s+-n\s+(?P<ns1>\S+)|\s+--namespace[=\s]+(?P<ns2>\S+))?"
        r"\s+run\s+(?P<pod>[A-Za-z0-9_.-]+)\b",
        cmd,
    )
    if not match:
        return None
    return (match.group("pod"), match.group("ns1") or match.group("ns2") or "")


def _build_pre_cleanup(commands: List[str], namespace: str) -> List[str]:
    """Remove stale helper pods before a new injection attempt."""
    pre_cleanup: List[str] = []
    seen: set[tuple[str, str]] = set()
    for cmd in commands:
        parsed = _extract_kubectl_run_pod(cmd)
        if not parsed:
            continue
        pod_name, cmd_ns = parsed
        ns = cmd_ns or namespace or "default"
        key = (ns, pod_name)
        if key in seen:
            continue
        seen.add(key)
        pre_cleanup.append(
            f"kubectl -n {shlex.quote(ns)} delete pod {shlex.quote(pod_name)} "
            "--ignore-not-found=true --force --grace-period=0 2>/dev/null || true"
        )
    return pre_cleanup


def _find_target_node(target_service: str, namespace: str, cluster_id: Optional[str]) -> Optional[str]:
    """Best-effort lookup of the node hosting the scenario's target service."""
    ns = namespace or "default"
    targets = [t.strip() for t in str(target_service or "").split(",") if t.strip()]
    if not targets:
        return None
    result = _shell_sync(f"kubectl -n {shlex.quote(ns)} get pods -o json", 12, cluster_id)
    if not result.get("ok"):
        return None
    try:
        pod_list = json.loads(result.get("stdout") or "{}")
    except json.JSONDecodeError:
        return None
    for target in targets:
        for pod in pod_list.get("items", []):
            meta = pod.get("metadata") or {}
            spec = pod.get("spec") or {}
            status = pod.get("status") or {}
            labels = meta.get("labels") or {}
            name = meta.get("name") or ""
            label_values = {str(v) for v in labels.values()}
            if status.get("phase") != "Running":
                continue
            if name.startswith(target) or target in label_values:
                node = spec.get("nodeName")
                if node:
                    return str(node)
    return None


def _inject_node_name(cmd: str, node_name: Optional[str]) -> str:
    """Pin kubectl-run helper pods to the target service node when possible."""
    if not node_name or " run " not in cmd:
        return cmd
    parsed = _extract_kubectl_run_pod(cmd)
    if not parsed:
        return cmd
    if parsed[0] == "compose-post-overrequest":
        return cmd
    overrides = json.dumps(
        {"apiVersion": "v1", "spec": {"nodeName": node_name}},
        separators=(",", ":"),
    )
    if "--overrides=" in cmd or "--overrides " in cmd:
        return _merge_kubectl_run_overrides(cmd, {"spec": {"nodeName": node_name}})
    flag = f"--overrides={shlex.quote(overrides)}"
    if " -- " in cmd:
        return cmd.replace(" -- ", f" {flag} -- ", 1)
    return f"{cmd} {flag}"


def _merge_kubectl_run_overrides(cmd: str, patch: Dict[str, Any]) -> str:
    """Merge a small JSON patch into an existing kubectl run --overrides payload."""
    match = re.search(r"--overrides=(?P<quote>['\"])(?P<body>.*?)(?P=quote)", cmd)
    if not match:
        return cmd
    try:
        current = json.loads(match.group("body"))
    except json.JSONDecodeError:
        return cmd

    def merge(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
        for key, value in src.items():
            if isinstance(value, dict) and isinstance(dst.get(key), dict):
                merge(dst[key], value)
            else:
                dst[key] = value
        return dst

    merged = json.dumps(merge(current, patch), separators=(",", ":"))
    return cmd[:match.start("body")] + merged + cmd[match.end("body"):]


def _prepare_faultlab_commands(
    scenario: Dict[str, Any],
    cluster_id: Optional[str],
    namespace_override: Optional[str],
    commands_key: str,
    background_load: bool,
    resolve_target_node: bool = True,
) -> List[str]:
    """Apply cluster + namespace overrides to scenario inject/cleanup commands."""
    inject = scenario.get("inject", {})
    raw_commands = list(inject.get(commands_key, []))
    scenario_ns = inject.get("namespace", "")
    effective_ns = namespace_override or scenario_ns

    commands: List[str] = []
    if background_load and commands_key == "commands":
        load_command = (
            "kubectl -n %s create job aiopslab-bg-load-%s "
            "--image=alpine:3.18 --image-pull-policy=IfNotPresent "
            "--labels=app=agenticsre-bg-load -- /bin/sh -c "
            "'for i in $(seq 1 180); do wget -q -O /dev/null --timeout=2 "
            "http://nginx-thrift:8080/wrk2-api/home-timeline/read?start=0\\&stop=10 || true; sleep 1; done'"
        ) % (effective_ns or "default", int(time.time()))
        commands.append(load_command)
    commands.extend(raw_commands)
    commands = [_patch_offline_image(c) for c in commands]

    if namespace_override and scenario_ns:
        commands = [_apply_ns_override(c, scenario_ns, namespace_override) for c in commands]
    if commands_key == "commands":
        target_node = None
        if resolve_target_node:
            target_node = _find_target_node(scenario.get("target_service", ""), effective_ns or "default", cluster_id)
        commands = [_inject_node_name(c, target_node) for c in commands]
        commands = _build_pre_cleanup(commands, effective_ns or "default") + commands
    elif commands_key == "cleanup":
        commands = [_strip_force_flag(c) for c in commands]
        commands.insert(
            0,
            f"kubectl -n {shlex.quote(effective_ns or 'default')} delete job "
            "-l app=agenticsre-bg-load --ignore-not-found=true",
        )
    if cluster_id:
        commands = [_apply_cluster_to_cmd(c, cluster_id) for c in commands]
    return commands


def _faultlab_target_scopes(
    scenario: Dict[str, Any],
    namespace: Optional[str] = None,
) -> List[Dict[str, str]]:
    """Bind every registered target to its exact namespace without exposing commands."""
    inject = scenario.get("inject") if isinstance(scenario.get("inject"), dict) else {}
    commands = [str(item) for key in ("commands", "cleanup") for item in inject.get(key, []) or []]
    registered_namespace = str(inject.get("namespace") or "default")
    effective_namespace = str(namespace or registered_namespace)
    scopes: List[Dict[str, str]] = []
    patterns = (
        (r"\b(?:deploy|deployment)/([a-z0-9][a-z0-9.-]{0,62})\b", "deployment"),
        (r"\b(?:run|delete\s+pod)\s+([a-z0-9][a-z0-9.-]{0,62})\b", "pod"),
        (r"\bservice/([a-z0-9][a-z0-9.-]{0,62})\b", "service"),
    )
    for command in commands:
        command_namespaces = re.findall(
            r"\bkubectl\b[^;&|]*?(?:-n\s+|--namespace(?:=|\s+))([a-z0-9][a-z0-9.-]{0,62})\b",
            command.lower(),
        )
        command_namespace = command_namespaces[0] if command_namespaces else registered_namespace
        if command_namespace == registered_namespace:
            command_namespace = effective_namespace
        for pattern, kind in patterns:
            for name in re.findall(pattern, command.lower()):
                entity = f"{kind}/{name}"
                scope = {"namespace": command_namespace, "target_entity_id": entity}
                if scope not in scopes:
                    scopes.append(scope)
    if scopes:
        return scopes
    for name in str(scenario.get("target_service") or "").split(","):
        clean = name.strip().lower()
        if clean and clean != "multiple" and _is_safe_k8s_name(clean):
            scope = {"namespace": effective_namespace, "target_entity_id": f"service/{clean}"}
            if scope not in scopes:
                scopes.append(scope)
    return scopes


def _faultlab_target_entity_ids(scenario: Dict[str, Any]) -> List[str]:
    """Derive typed target identities from the exact registered target scopes."""
    return list(dict.fromkeys(
        scope["target_entity_id"] for scope in _faultlab_target_scopes(scenario)
    ))


def _faultlab_guarded_skill_plan(
    scenario: Dict[str, Any],
    *,
    cluster_id: Optional[str],
    namespace: str,
    duration_s: int = 30,
) -> Dict[str, Any]:
    """Build the auditable inject/cleanup contract for one registered scenario."""
    from skill_runtime.executor import SkillExecutor

    target_scopes = _faultlab_target_scopes(scenario, namespace)
    targets = list(dict.fromkeys(scope["target_entity_id"] for scope in target_scopes))
    max_entities = max(1, len(target_scopes))
    payload = {
        "operation": "fault_injection",
        "scenario_id": str(scenario.get("id") or ""),
        "registered_scenario": True,
        "cluster_id": str(cluster_id or ""),
        "namespace": namespace,
        "target_entity_ids": targets,
        "target_scopes": target_scopes,
        "fault_type": str(scenario.get("fault_type") or "FaultExperiment"),
        "duration_s": max(1, min(int(duration_s or 30), 120)),
        "approval_required": True,
        "auto_cleanup": True,
        "blast_radius_budget": {
            "max_affected_entities": max_entities,
            "max_duration_s": 120,
        },
        "abort_conditions": [
            "target_scope_miss",
            "critical_alert",
            "blast_radius_exceeded",
            "fault_active_timeout",
        ],
    }
    executor = SkillExecutor.from_dir(SKILL_RUNTIME_SKILLS_DIR)
    run = executor.run(
        "k8s.guarded_fault_injection",
        payload,
        stages=("detect", "diagnose", "heal", "verify"),
        dry_run=True,
    ).to_dict()
    suggestions = (((run.get("stages") or {}).get("heal") or {}).get("output") or {}).get("suggestions") or []
    action = next(
        (item for item in suggestions if isinstance(item, dict) and item.get("operation") == "agenticsre.k8s.fault.inject"),
        {},
    )
    return {
        "skill_id": "k8s.guarded_fault_injection",
        "run": run,
        "action": action,
        "rollback": action.get("rollback") if isinstance(action, dict) else {},
        "verification": action.get("verification") if isinstance(action, dict) else {},
        "ready": bool(action),
    }


def _precision_target_scopes(
    scenario: Dict[str, Any], namespace: str,
) -> List[Dict[str, str]]:
    return [
        {"namespace": namespace, "target_entity_id": str(target)}
        for target in scenario.get("target_entity_ids") or []
    ]


def _precision_transaction_undo(
    scenario: Dict[str, Any], *, cluster_id: Optional[str], namespace: str,
) -> Dict[str, Any]:
    return {
        "operation": "agenticsre.precision.cleanup",
        "parameters": {
            "scenario_id": str(scenario.get("id") or ""),
            "cluster_id": str(cluster_id or ""),
            "namespace": namespace,
            "target_entity_ids": list(scenario.get("target_entity_ids") or []),
            "target_scopes": _precision_target_scopes(scenario, namespace),
            "registered_cleanup_only": True,
        },
    }


def _precision_faultlab_guarded_plan(
    scenario: Dict[str, Any],
    *,
    cluster_id: Optional[str],
    namespace: str,
) -> Dict[str, Any]:
    """Resolve one registered scenario into a typed reviewed-adapter plan."""
    from skill_runtime.executor import SkillExecutor
    from skill_runtime.precision_execution import precision_adapter_preflight

    safety = scenario.get("safety") if isinstance(scenario.get("safety"), dict) else {}
    budget = scenario.get("blast_radius_budget") if isinstance(scenario.get("blast_radius_budget"), dict) else {}
    precision = scenario.get("precision") if isinstance(scenario.get("precision"), dict) else {}
    payload = {
        "operation": "fault_injection",
        "scenario_id": str(scenario.get("id") or ""),
        "registered_scenario": True,
        "cluster_id": str(cluster_id or ""),
        "namespace": namespace,
        "target_entity_ids": list(scenario.get("target_entity_ids") or []),
        "target_scopes": _precision_target_scopes(scenario, namespace),
        "fault_type": str(scenario.get("fault_type") or "FineGrainedFault"),
        "duration_s": int(safety.get("duration_s") or 15),
        "approval_required": True,
        "auto_cleanup": True,
        "blast_radius_budget": budget,
        "abort_conditions": [
            "scope_miss",
            "control_group_affected",
            "blast_radius_exceeded",
            "critical_alert",
            "duration_exceeded",
        ],
        "precision": {**precision, "safety": safety},
    }
    run = SkillExecutor.from_dir(SKILL_RUNTIME_SKILLS_DIR).run(
        "k8s.guarded_fault_injection",
        payload,
        stages=("detect", "diagnose", "heal", "verify"),
        dry_run=True,
    ).to_dict()
    suggestions = (((run.get("stages") or {}).get("heal") or {}).get("output") or {}).get("suggestions") or []
    action = next(
        (item for item in suggestions if isinstance(item, dict) and item.get("operation") == "agenticsre.k8s.fault.inject"),
        {},
    )
    return {
        "skill_id": "k8s.guarded_fault_injection",
        "run": run,
        "action": action,
        "rollback": action.get("rollback") if isinstance(action, dict) else {},
        "verification": action.get("verification") if isinstance(action, dict) else {},
        "ready": bool(action),
        "adapter_preflight": precision_adapter_preflight(
            str(scenario.get("id") or ""), root_dir=ROOT_DIR,
        ),
        "real_execution_ready": bool(action) and precision_adapter_preflight(
            str(scenario.get("id") or ""), root_dir=ROOT_DIR,
        ).get("ready"),
    }


def _precision_execution_environment(
    scenario: Dict[str, Any], cluster_id: Optional[str],
) -> Dict[str, str]:
    """Build environment variables from a registered cluster profile only."""
    env: Dict[str, str] = {}
    cluster = _find_k8s_cluster(cluster_id) if cluster_id else None
    if cluster:
        kubeconfig = _cluster_kubeconfig_path(cluster)
        context = str(cluster.get("context") or "").strip()
        if kubeconfig:
            env["KUBECONFIG"] = kubeconfig
        if context:
            env["AGENTICSRE_GPU_CONTEXT"] = context
    method = str((scenario.get("precision") or {}).get("method") or "")
    if method == "dcgm":
        env.setdefault("AGENTICSRE_GPU_CONTEXT", "kubernetes-admin@kubernetes")
    return env


def _precision_runtime_preflight(
    scenario: Dict[str, Any], *, cluster_id: Optional[str],
) -> Dict[str, Any]:
    """Check the selected environment without creating or changing resources."""
    from skill_runtime.precision_execution import precision_adapter_preflight

    static = precision_adapter_preflight(str(scenario.get("id") or ""), root_dir=ROOT_DIR)
    checks: List[Dict[str, Any]] = []
    blockers = list(static.get("blockers") or [])
    cluster_check = _shell_sync("kubectl get nodes -o name", 15, cluster_id)
    checks.append({"name": "cluster_reachable", **cluster_check})
    if not cluster_check.get("ok"):
        blockers.append("目标 Kubernetes 集群不可达")
    method = str((scenario.get("precision") or {}).get("method") or "")
    if method in {"request", "bytecode", "platform_native"}:
        crd = {
            "request": "httpchaos.chaos-mesh.org",
            "bytecode": "jvmchaos.chaos-mesh.org",
            "platform_native": "networkchaos.chaos-mesh.org",
        }[method]
        crd_check = _shell_sync(f"kubectl get crd {shlex.quote(crd)} -o name", 15, cluster_id)
        checks.append({"name": "chaos_mesh_crd", **crd_check})
        if not crd_check.get("ok"):
            blockers.append(f"目标集群缺少所需 Chaos Mesh CRD: {crd}")
    return {
        **static,
        "status": "ready" if not blockers else "blocked",
        "ready": not blockers,
        "cluster_id": str(cluster_id or ""),
        "runtime_checks": checks,
        "blockers": list(dict.fromkeys(blockers)),
    }


def _precision_cleanup_resources(
    scenario_id: str, *, cluster_id: Optional[str], dry_run: bool,
) -> Dict[str, Any]:
    """Remove only fixed namespaces registered by the selected adapter."""
    from skill_runtime.precision_execution import precision_adapter

    spec = precision_adapter(scenario_id)
    resources = [{"kind": "namespace", "name": name} for name in spec.cleanup_namespaces]
    if dry_run:
        return {"status": "dry_run", "resources": resources, "results": []}
    results = [
        _shell_sync(
            f"kubectl delete namespace {shlex.quote(item['name'])} "
            "--ignore-not-found=true --wait=true --timeout=120s",
            150,
            cluster_id,
        )
        for item in resources
    ]
    return {
        "status": "executed" if all(item.get("ok") for item in results) else "failed",
        "resources": resources,
        "results": results,
    }


def _precision_residue_check(
    scenario_id: str, *, cluster_id: Optional[str],
) -> Dict[str, Any]:
    from skill_runtime.precision_execution import precision_adapter

    spec = precision_adapter(scenario_id)
    checks = []
    for namespace in spec.cleanup_namespaces:
        result = _shell_sync(
            f"kubectl get namespace {shlex.quote(namespace)} --ignore-not-found -o name",
            15,
            cluster_id,
        )
        present = bool(str(result.get("stdout") or "").strip())
        checks.append({"namespace": namespace, "present": present, "result": result})
    passed = all(not item["present"] and item["result"].get("ok") for item in checks)
    return {"status": "passed" if passed else "failed", "passed": passed, "checks": checks}


def _precision_approved_experiment_run(
    scenario: Dict[str, Any],
    body: Dict[str, Any],
    *,
    cluster_id: Optional[str],
    namespace: str,
) -> Optional[Dict[str, Any]]:
    """Resolve one successful persisted experiment as the sole recovery authority."""
    fault_run_id = str(body.get("fault_run_id") or "").strip()
    if not fault_run_id:
        return None
    expected_targets = list(scenario.get("target_entity_ids") or [])
    expected_scopes = _precision_target_scopes(scenario, namespace)
    expected_undo = _precision_transaction_undo(
        scenario,
        cluster_id=cluster_id,
        namespace=namespace,
    )
    for run in _load_fault_runs():
        if str(run.get("id") or "") != fault_run_id:
            continue
        results = run.get("results") if isinstance(run.get("results"), list) else []
        result = results[0] if results and isinstance(results[0], dict) else {}
        verification = result.get("verification") if isinstance(result.get("verification"), dict) else {}
        validation = str(verification.get("validation") or "")
        if str(run.get("action") or "") != "precision_experiment":
            return None
        if str(run.get("status") or "") != "completed" or str(result.get("status") or "") != "succeeded":
            return None
        if verification.get("cleanup_complete") is not True:
            return None
        if not validation.startswith("PASS"):
            return None
        if str(run.get("scenario_id") or "") != str(scenario.get("id") or ""):
            return None
        if str(run.get("cluster_id") or "") != str(cluster_id or ""):
            return None
        if str(run.get("namespace") or "") != namespace:
            return None
        if list(run.get("target_entity_ids") or []) != expected_targets:
            return None
        if list(run.get("target_scopes") or []) != expected_scopes:
            return None
        if run.get("transaction_undo") != expected_undo:
            return None
        return run
    return None


def _precision_transactional_heal_plan(
    scenario: Dict[str, Any],
    source_run: Dict[str, Any],
    *,
    cluster_id: Optional[str],
    namespace: str,
) -> Dict[str, Any]:
    """Build a four-stage plan from server-owned experiment evidence only."""
    from skill_runtime.executor import SkillExecutor

    results = source_run.get("results") if isinstance(source_run.get("results"), list) else []
    result = results[0] if results and isinstance(results[0], dict) else {}
    evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
    validation = str(((result.get("verification") or {}).get("validation") or evidence.get("validation") or ""))
    targets = list(scenario.get("target_entity_ids") or [])
    checkpoint = {
        "summary": {
            "source_status": str(source_run.get("status") or ""),
            "baseline_evidence": str(evidence.get("before_evidence") or ""),
            "recovery_evidence": str(evidence.get("recovery_evidence") or ""),
            "validation": validation,
        },
    }
    payload = {
        "operation": "transactional_self_healing",
        "incident_id": f"precision-recovery-{source_run.get('id') or 'unknown'}",
        "scenario_id": str(scenario.get("id") or ""),
        "registered_scenario": True,
        "cluster_id": str(cluster_id or ""),
        "namespace": namespace,
        "target_entity_ids": targets,
        "target_scopes": _precision_target_scopes(scenario, namespace),
        "diagnosis": {
            "fault_type": str(scenario.get("fault_type") or "fine_grained_fault"),
            "root_cause": "已登记的细粒度故障实验命中预期效果，原交易已执行 Undo",
            "root_cause_component": str(targets[0] if targets else ""),
            "confidence": 0.99,
        },
        "remediation_strategy": {
            "id": "execute_transaction_undo",
            "name": "复用原细粒度故障交易 Undo",
            "execution_mode": "exact_approved_transaction_undo",
        },
        "checkpoint": checkpoint,
        "permitted_undo_action": source_run.get("transaction_undo") or {},
        "authorization_basis": "approved_fault_transaction_undo",
        "max_recovery_s": 120,
    }
    run = SkillExecutor.from_dir(SKILL_RUNTIME_SKILLS_DIR).run(
        "k8s.transactional_self_healing",
        payload,
        stages=("detect", "diagnose", "heal", "verify"),
        dry_run=True,
    ).to_dict()
    suggestions = (((run.get("stages") or {}).get("heal") or {}).get("output") or {}).get("suggestions") or []
    action = next(
        (
            item for item in suggestions
            if isinstance(item, dict) and item.get("operation") == "agenticsre.k8s.remediation.execute_undo"
        ),
        {},
    )
    blockers = (((run.get("stages") or {}).get("diagnose") or {}).get("output") or {}).get("blockers") or []
    return {
        "skill_id": "k8s.transactional_self_healing",
        "run": run,
        "action": action,
        "verification": action.get("verification") if isinstance(action, dict) else {},
        "blockers": blockers,
        "ready": bool(action),
        "execution_mode": "exact_approved_transaction_undo",
        "source_fault_run_id": str(source_run.get("id") or ""),
        "source_recovery_already_completed": True,
        "checkpoint": checkpoint,
    }


def _faultlab_recovery_strategy(scenario: Dict[str, Any]) -> Dict[str, str]:
    inject = scenario.get("inject") if isinstance(scenario.get("inject"), dict) else {}
    cleanup_text = " ".join(str(item).lower() for item in inject.get("cleanup") or [])
    if " scale " in f" {cleanup_text} " and "--replicas" in cleanup_text:
        strategy_id, name = "restore_replica_count", "恢复故障前副本数"
    elif " set image " in f" {cleanup_text} ":
        strategy_id, name = "restore_deployment_image", "恢复故障前镜像"
    elif "networkchaos" in cleanup_text or "networkpolicy" in cleanup_text:
        strategy_id, name = "remove_network_fault", "移除已登记网络故障"
    elif "delete pod" in cleanup_text or "delete job" in cleanup_text or " exec " in f" {cleanup_text} ":
        strategy_id, name = "remove_fault_helper", "清理故障辅助负载"
    else:
        strategy_id, name = "execute_transaction_undo", "执行原故障交易 Undo"
    return {
        "id": strategy_id,
        "name": name,
        "execution_mode": "exact_approved_transaction_undo",
    }


def _faultlab_approved_injection_run(
    scenario: Dict[str, Any],
    body: Dict[str, Any],
    *,
    cluster_id: Optional[str],
    namespace: str,
) -> Optional[Dict[str, Any]]:
    """Resolve approval from one persisted real injection, never from a caller flag."""
    fault_run_id = str(body.get("fault_run_id") or "").strip()
    if not fault_run_id:
        return None
    expected_scopes = _faultlab_target_scopes(scenario, namespace)
    expected_targets = list(dict.fromkeys(scope["target_entity_id"] for scope in expected_scopes))
    for run in _load_fault_runs():
        if str(run.get("id") or "") != fault_run_id:
            continue
        if str(run.get("action") or "") != "inject" or str(run.get("status") or "") != "executed":
            return None
        if str(run.get("scenario_id") or "") != str(scenario.get("id") or ""):
            return None
        if str(run.get("cluster_id") or "") != str(cluster_id or ""):
            return None
        if str(run.get("namespace") or "") != namespace:
            return None
        if list(run.get("target_entity_ids") or []) != expected_targets:
            return None
        if list(run.get("target_scopes") or []) != expected_scopes:
            return None
        undo = run.get("transaction_undo") if isinstance(run.get("transaction_undo"), dict) else {}
        if str(undo.get("operation") or "") != "agenticsre.k8s.fault.cleanup":
            return None
        return run
    return None


def _faultlab_transactional_heal_plan(
    scenario: Dict[str, Any],
    body: Dict[str, Any],
    *,
    cluster_id: Optional[str],
    namespace: str,
) -> Dict[str, Any]:
    """Build an exact-Undo self-healing plan without accepting executable input."""
    from skill_runtime.executor import SkillExecutor

    guarded = _faultlab_guarded_skill_plan(
        scenario,
        cluster_id=cluster_id,
        namespace=namespace,
        duration_s=int(body.get("duration_s") or 30),
    )
    diagnosis = body.get("diagnosis") if isinstance(body.get("diagnosis"), dict) else {}
    checkpoint = body.get("checkpoint") if isinstance(body.get("checkpoint"), dict) else {}
    source_run = _faultlab_approved_injection_run(
        scenario,
        body,
        cluster_id=cluster_id,
        namespace=namespace,
    )
    payload = {
        "operation": "transactional_self_healing",
        "incident_id": str(body.get("incident_id") or f"faultlab-{scenario.get('id') or 'incident'}"),
        "scenario_id": str(scenario.get("id") or ""),
        "registered_scenario": True,
        "cluster_id": str(cluster_id or ""),
        "namespace": namespace,
        "target_entity_ids": _faultlab_target_entity_ids(scenario),
        "target_scopes": _faultlab_target_scopes(scenario, namespace),
        "diagnosis": diagnosis,
        "remediation_strategy": _faultlab_recovery_strategy(scenario),
        "checkpoint": checkpoint,
        "permitted_undo_action": (source_run or {}).get("transaction_undo") or guarded.get("rollback") or {},
        "authorization_basis": "approved_fault_transaction_undo" if source_run else "",
        "max_recovery_s": int(body.get("max_recovery_s") or 60),
    }
    run = SkillExecutor.from_dir(SKILL_RUNTIME_SKILLS_DIR).run(
        "k8s.transactional_self_healing",
        payload,
        stages=("detect", "diagnose", "heal", "verify"),
        dry_run=True,
    ).to_dict()
    suggestions = (((run.get("stages") or {}).get("heal") or {}).get("output") or {}).get("suggestions") or []
    action = next(
        (
            item for item in suggestions
            if isinstance(item, dict) and item.get("operation") == "agenticsre.k8s.remediation.execute_undo"
        ),
        {},
    )
    blockers = (((run.get("stages") or {}).get("diagnose") or {}).get("output") or {}).get("blockers") or []
    return {
        "skill_id": "k8s.transactional_self_healing",
        "run": run,
        "action": action,
        "verification": action.get("verification") if isinstance(action, dict) else {},
        "blockers": blockers,
        "ready": bool(action),
        "execution_mode": "plan_only",
        "source_fault_run_id": str((source_run or {}).get("id") or ""),
    }


@app.post("/api/faultlab/precision/plan/{scenario_id}")
async def precision_faultlab_plan(scenario_id: str, request: Request):
    """Plan one registered fine-grained scenario and its reviewed adapter."""
    body = await request.json()
    scenario = next(
        (item for item in _load_precision_fault_scenarios() if str(item.get("id") or "") == scenario_id),
        None,
    )
    if not scenario:
        raise HTTPException(404, "Precision fault scenario not found")
    namespace = str(body.get("namespace") or "agenticsre-faultlab").strip()
    if not _is_safe_k8s_name(namespace):
        raise HTTPException(400, "namespace is invalid")
    plan = _precision_faultlab_guarded_plan(
        scenario,
        cluster_id=body.get("cluster_id") or None,
        namespace=namespace,
    )
    if not plan.get("ready"):
        raise HTTPException(400, {"message": "Precision fault plan is blocked", "plan": plan})
    return {
        "status": "planned",
        "scenario": scenario,
        "cluster_id": body.get("cluster_id") or None,
        "namespace": namespace,
        "guarded_plan": plan,
    }


@app.post("/api/faultlab/precision/preflight/{scenario_id}")
async def precision_faultlab_preflight(scenario_id: str, request: Request):
    """Run read-only cluster and adapter checks for one registered scenario."""
    body = await _request_json_or_empty(request)
    scenario = next(
        (item for item in _load_precision_fault_scenarios() if str(item.get("id") or "") == scenario_id),
        None,
    )
    if not scenario:
        raise HTTPException(404, "Precision fault scenario not found")
    preflight = await asyncio.to_thread(
        _precision_runtime_preflight,
        scenario,
        cluster_id=body.get("cluster_id") or None,
    )
    return {"status": preflight["status"], "scenario": scenario, "preflight": preflight}


@app.post("/api/faultlab/precision/experiment/{scenario_id}")
async def precision_faultlab_experiment(scenario_id: str, request: Request):
    """Run a fixed baseline/inject/effect/Undo/recovery transaction."""
    from skill_runtime.precision_execution import PrecisionExecutionError, execute_precision_adapter

    body = await request.json()
    scenario = next(
        (item for item in _load_precision_fault_scenarios() if str(item.get("id") or "") == scenario_id),
        None,
    )
    if not scenario:
        raise HTTPException(404, "Precision fault scenario not found")
    dry_run = body.get("dry_run", True)
    if not isinstance(dry_run, bool):
        raise HTTPException(400, "dry_run must be a JSON boolean")
    if not dry_run and not bool(body.get("confirm")):
        raise HTTPException(400, "Real fine-grained fault experiment requires confirm=true")
    if not dry_run and not bool(body.get("experiment_ack")):
        raise HTTPException(400, "Real fine-grained fault experiment requires experiment_ack=true")
    cluster_id = body.get("cluster_id") or None
    namespace = str(body.get("namespace") or "agenticsre-faultlab").strip()
    if not _is_safe_k8s_name(namespace):
        raise HTTPException(400, "namespace is invalid")
    guarded_plan = _precision_faultlab_guarded_plan(
        scenario,
        cluster_id=cluster_id,
        namespace=namespace,
    )
    if not guarded_plan.get("real_execution_ready"):
        raise HTTPException(400, {"message": "Precision adapter is not ready", "guarded_plan": guarded_plan})
    if not dry_run:
        runtime_preflight = await asyncio.to_thread(
            _precision_runtime_preflight, scenario, cluster_id=cluster_id,
        )
        if not runtime_preflight.get("ready"):
            raise HTTPException(400, {"message": "Precision runtime preflight is blocked", "preflight": runtime_preflight})
    else:
        runtime_preflight = guarded_plan.get("adapter_preflight") or {}

    fault_run_id = f"precision-{uuid.uuid4().hex[:10]}"
    started_at = time.time()
    target_scopes = _precision_target_scopes(scenario, namespace)
    transaction_undo = _precision_transaction_undo(
        scenario,
        cluster_id=cluster_id,
        namespace=namespace,
    )
    try:
        result = await asyncio.to_thread(
            execute_precision_adapter,
            scenario_id,
            root_dir=ROOT_DIR,
            dry_run=dry_run,
            confirmed=bool(body.get("confirm")),
            experiment_ack=bool(body.get("experiment_ack")),
            env=_precision_execution_environment(scenario, cluster_id),
        )
    except PrecisionExecutionError as exc:
        failed = {
            "id": fault_run_id,
            "action": "precision_experiment",
            "target_mode": "k8s",
            "status": "failed",
            "scenario_id": scenario_id,
            "cluster_id": str(cluster_id or ""),
            "namespace": namespace,
            "target_entity_ids": list(scenario.get("target_entity_ids") or []),
            "target_scopes": target_scopes,
            "transaction_undo": transaction_undo,
            "started_at": started_at,
            "finished_at": time.time(),
            "results": [{"ok": False, "error": str(exc)}],
        }
        if not dry_run:
            _append_fault_run(failed)
        raise HTTPException(400, {"message": str(exc), "fault_run_id": fault_run_id}) from exc

    if not dry_run:
        _append_fault_run({
            "id": fault_run_id,
            "action": "precision_experiment",
            "target_mode": "k8s",
            "status": "completed" if result.get("status") == "succeeded" else "failed",
            "scenario_id": scenario_id,
            "cluster_id": str(cluster_id or ""),
            "namespace": namespace,
            "target_entity_ids": list(scenario.get("target_entity_ids") or []),
            "target_scopes": target_scopes,
            "transaction_undo": transaction_undo,
            "verification_plan": guarded_plan.get("verification") or {},
            "started_at": started_at,
            "finished_at": time.time(),
            "results": [result],
        })
    return {
        "status": result.get("status"),
        "scenario": scenario,
        "cluster_id": cluster_id,
        "fault_run_id": fault_run_id,
        "preflight": runtime_preflight,
        "guarded_plan": guarded_plan,
        "transaction_undo": transaction_undo,
        "result": result,
    }


@app.post("/api/faultlab/precision/heal/{scenario_id}")
async def precision_faultlab_transactional_heal(scenario_id: str, request: Request):
    """Replay exact persisted Undo and verify an already recovered transaction."""
    body = await _request_json_or_empty(request)
    scenario = next(
        (item for item in _load_precision_fault_scenarios() if str(item.get("id") or "") == scenario_id),
        None,
    )
    if not scenario:
        raise HTTPException(404, "Precision fault scenario not found")
    dry_run = body.get("dry_run", True)
    if not isinstance(dry_run, bool):
        raise HTTPException(400, "dry_run must be a JSON boolean")
    cluster_id = body.get("cluster_id") or None
    namespace = str(body.get("namespace") or "agenticsre-faultlab").strip()
    if not _is_safe_k8s_name(namespace):
        raise HTTPException(400, "namespace is invalid")
    source_run = _precision_approved_experiment_run(
        scenario,
        body,
        cluster_id=cluster_id,
        namespace=namespace,
    )
    if not source_run:
        raise HTTPException(
            400,
            "Self-healing verification requires the exact persisted ID of a successful real precision experiment",
        )
    plan = _precision_transactional_heal_plan(
        scenario,
        source_run,
        cluster_id=cluster_id,
        namespace=namespace,
    )
    if not plan.get("ready"):
        raise HTTPException(400, {"message": "Precision self-healing contract is blocked", "plan": plan})
    if dry_run:
        return {
            "status": "planned",
            "scenario_id": scenario_id,
            "cluster_id": cluster_id,
            "namespace": namespace,
            "fault_run_id": str(source_run.get("id") or ""),
            "transactional_heal": plan,
            "recovery_state": "source_transaction_already_recovered",
        }

    started_at = time.time()
    cleanup = await asyncio.to_thread(
        _precision_cleanup_resources,
        scenario_id,
        cluster_id=cluster_id,
        dry_run=False,
    )
    residue = await asyncio.to_thread(
        _precision_residue_check,
        scenario_id,
        cluster_id=cluster_id,
    )
    results = source_run.get("results") if isinstance(source_run.get("results"), list) else []
    source_result = results[0] if results and isinstance(results[0], dict) else {}
    source_verification = (
        source_result.get("verification")
        if isinstance(source_result.get("verification"), dict)
        else {}
    )
    passed = (
        cleanup.get("status") == "executed"
        and residue.get("passed") is True
        and source_verification.get("cleanup_complete") is True
    )
    heal_run_id = f"precision-heal-{uuid.uuid4().hex[:10]}"
    recovery = {
        "status": "already_recovered" if passed else "failed",
        "source_fault_run_id": str(source_run.get("id") or ""),
        "source_recovery_already_completed": True,
        "executed_action": "idempotent_transaction_undo_replay",
        "cleanup_replay": cleanup,
        "residue_verification": residue,
        "source_transaction_verification": source_verification,
        "passed": passed,
    }
    _append_fault_run({
        "id": heal_run_id,
        "action": "precision_heal",
        "target_mode": "k8s",
        "status": "completed" if passed else "failed",
        "scenario_id": scenario_id,
        "cluster_id": str(cluster_id or ""),
        "namespace": namespace,
        "target_entity_ids": list(scenario.get("target_entity_ids") or []),
        "target_scopes": _precision_target_scopes(scenario, namespace),
        "source_fault_run_id": str(source_run.get("id") or ""),
        "transaction_undo": source_run.get("transaction_undo") or {},
        "started_at": started_at,
        "finished_at": time.time(),
        "results": [recovery],
    })
    return {
        "status": recovery["status"],
        "scenario_id": scenario_id,
        "cluster_id": cluster_id,
        "namespace": namespace,
        "fault_run_id": heal_run_id,
        "source_fault_run_id": str(source_run.get("id") or ""),
        "transactional_heal": plan,
        "recovery": recovery,
    }


@app.post("/api/faultlab/precision/cleanup/{scenario_id}")
async def precision_faultlab_cleanup(scenario_id: str, request: Request):
    """Run idempotent cleanup for only the adapter's registered resources."""
    body = await _request_json_or_empty(request)
    if not any(str(item.get("id") or "") == scenario_id for item in _load_precision_fault_scenarios()):
        raise HTTPException(404, "Precision fault scenario not found")
    dry_run = body.get("dry_run", True)
    if not isinstance(dry_run, bool):
        raise HTTPException(400, "dry_run must be a JSON boolean")
    if not dry_run and not bool(body.get("confirm")):
        raise HTTPException(400, "Real fine-grained cleanup requires confirm=true")
    result = await asyncio.to_thread(
        _precision_cleanup_resources,
        scenario_id,
        cluster_id=body.get("cluster_id") or None,
        dry_run=dry_run,
    )
    return {"scenario_id": scenario_id, **result}


@app.post("/api/faultlab/precision/verify/{scenario_id}")
async def precision_faultlab_verify(scenario_id: str, request: Request):
    """Verify that the registered temporary resources have no residue."""
    body = await _request_json_or_empty(request)
    if not any(str(item.get("id") or "") == scenario_id for item in _load_precision_fault_scenarios()):
        raise HTTPException(404, "Precision fault scenario not found")
    result = await asyncio.to_thread(
        _precision_residue_check,
        scenario_id,
        cluster_id=body.get("cluster_id") or None,
    )
    return {"scenario_id": scenario_id, "verification": result, "status": result["status"]}


@app.post("/api/faultlab/heal/{scenario_id}")
async def faultlab_transactional_heal(scenario_id: str, request: Request):
    """Plan exact transaction Undo; execution still uses the guarded cleanup API."""
    body = await request.json()
    scenario = next((item for item in _load_fault_scenarios() if item.get("id") == scenario_id), None)
    if not scenario:
        raise HTTPException(404, "Scenario not found")
    namespace = str(body.get("namespace") or scenario.get("inject", {}).get("namespace") or "default").strip()
    if not _is_safe_k8s_name(namespace):
        raise HTTPException(400, "namespace is invalid")
    plan = _faultlab_transactional_heal_plan(
        scenario,
        body,
        cluster_id=body.get("cluster_id") or None,
        namespace=namespace,
    )
    return {
        "status": "planned" if plan.get("ready") else "blocked",
        "scenario_id": scenario_id,
        "cluster_id": body.get("cluster_id") or None,
        "namespace": namespace,
        "transactional_heal": plan,
        "execution_endpoint": f"/api/faultlab/cleanup/{scenario_id}",
        "execution_requirements": ["confirm=true"],
    }


@app.post("/api/faultlab/inject/{scenario_id}")
async def faultlab_inject(scenario_id: str, request: Request):
    """Inject one predefined scenario. Defaults to dry_run=true.

    Body fields (all optional):
      - dry_run (bool, default true)
      - background_load (bool, default false)
      - cluster_id (str): K8s cluster profile id (rewrites kubectl context/kubeconfig)
      - namespace (str): override the scenario's default namespace
    """
    body = await request.json()
    dry_run = body.get("dry_run", True)
    background_load = body.get("background_load", False)
    cluster_id = body.get("cluster_id") or None
    namespace_override = (body.get("namespace") or "").strip() or None
    scenarios = _load_fault_scenarios()
    scenario = next((s for s in scenarios if s.get("id") == scenario_id), None)
    if not scenario:
        raise HTTPException(404, "Scenario not found")

    effective_namespace = namespace_override or scenario.get("inject", {}).get("namespace") or "default"
    guarded_plan = _faultlab_guarded_skill_plan(
        scenario,
        cluster_id=cluster_id,
        namespace=effective_namespace,
        duration_s=int(body.get("duration_s") or body.get("hold_s") or 30),
    )
    if not guarded_plan.get("ready"):
        raise HTTPException(400, {"message": "Fault injection safety plan is blocked", "guarded_plan": guarded_plan})

    if not dry_run and not bool(body.get("confirm")):
        raise HTTPException(400, "Real K8s fault injection requires confirm=true")
    if not dry_run and not bool(body.get("experiment_ack")):
        raise HTTPException(400, "Real K8s fault injection requires experiment_ack=true")

    commands = _prepare_faultlab_commands(
        scenario, cluster_id, namespace_override, "commands", background_load, resolve_target_node=not dry_run,
    )

    if dry_run:
        return {
            "status": "dry_run",
            "scenario": scenario,
            "cluster_id": cluster_id,
            "namespace": effective_namespace,
            "commands": commands,
            "background_load": background_load,
            "guarded_plan": guarded_plan,
        }

    results = [await asyncio.to_thread(_shell_sync, cmd, 120) for cmd in commands]
    execution_ok = bool(results) and all(bool(item.get("ok")) for item in results)
    fault_run_id = f"fault-{uuid.uuid4().hex[:10]}"
    transaction_undo = guarded_plan.get("rollback") or {}
    _append_fault_run({
        "id": fault_run_id,
        "action": "inject",
        "target_mode": "k8s",
        "status": "executed" if execution_ok else "failed",
        "scenario_id": scenario_id,
        "cluster_id": str(cluster_id or ""),
        "namespace": effective_namespace,
        "target_entity_ids": _faultlab_target_entity_ids(scenario),
        "target_scopes": _faultlab_target_scopes(scenario, effective_namespace),
        "transaction_undo": transaction_undo,
        "verification_plan": guarded_plan.get("verification") or {},
        "started_at": time.time(),
        "finished_at": time.time(),
        "results": results,
    })
    return {
        "status": "executed" if execution_ok else "failed",
        "scenario_id": scenario_id,
        "cluster_id": cluster_id,
        "namespace": effective_namespace,
        "results": results,
        "background_load": background_load,
        "guarded_plan": guarded_plan,
        "fault_run_id": fault_run_id,
        "transaction_undo": transaction_undo,
    }


@app.post("/api/faultlab/cleanup/{scenario_id}")
async def faultlab_cleanup(scenario_id: str, request: Request):
    """Run cleanup commands for one predefined scenario."""
    body = await request.json()
    dry_run = body.get("dry_run", True)
    cluster_id = body.get("cluster_id") or None
    namespace_override = (body.get("namespace") or "").strip() or None
    scenarios = _load_fault_scenarios()
    scenario = next((s for s in scenarios if s.get("id") == scenario_id), None)
    if not scenario:
        raise HTTPException(404, "Scenario not found")

    effective_namespace = namespace_override or scenario.get("inject", {}).get("namespace") or "default"
    guarded_plan = _faultlab_guarded_skill_plan(
        scenario,
        cluster_id=cluster_id,
        namespace=effective_namespace,
        duration_s=int(body.get("duration_s") or 30),
    )

    if not dry_run and not bool(body.get("confirm")):
        raise HTTPException(400, "Real K8s fault cleanup requires confirm=true")

    commands = _prepare_faultlab_commands(
        scenario, cluster_id, namespace_override, "cleanup", False,
    )
    if dry_run:
        return {
            "status": "dry_run",
            "scenario": scenario,
            "cluster_id": cluster_id,
            "namespace": effective_namespace,
            "commands": commands,
            "transaction_undo": guarded_plan.get("rollback") or {},
            "verification_plan": guarded_plan.get("verification") or {},
        }

    results = [await asyncio.to_thread(_shell_sync, cmd, 120) for cmd in commands]
    cleanup_ok = bool(results) and all(bool(item.get("ok")) for item in results)
    cleanup_run_id = f"fault-{uuid.uuid4().hex[:10]}"
    source_fault_run_id = str(body.get("fault_run_id") or "")
    _append_fault_run({
        "id": cleanup_run_id,
        "action": "cleanup",
        "target_mode": "k8s",
        "status": "executed" if cleanup_ok else "failed",
        "scenario_id": scenario_id,
        "cluster_id": str(cluster_id or ""),
        "namespace": effective_namespace,
        "target_entity_ids": _faultlab_target_entity_ids(scenario),
        "target_scopes": _faultlab_target_scopes(scenario, effective_namespace),
        "started_at": time.time(),
        "finished_at": time.time(),
        "results": [
            {**item, "source_fault_run_id": source_fault_run_id}
            if isinstance(item, dict) and source_fault_run_id
            else item
            for item in results
        ],
    })
    return {
        "status": "executed" if cleanup_ok else "failed",
        "scenario_id": scenario_id,
        "cluster_id": cluster_id,
        "namespace": effective_namespace,
        "results": results,
        "transaction_undo": guarded_plan.get("rollback") or {},
        "verification_plan": guarded_plan.get("verification") or {},
        "fault_run_id": cleanup_run_id,
        "source_fault_run_id": source_fault_run_id,
    }


@app.get("/api/llm-faultlab/scenarios")
async def llm_faultlab_scenarios():
    """List vLLM/LLM inference fault experiment scenarios."""
    scenarios = _load_llm_fault_scenarios()
    tool_status = _llm_fault_tool_status()
    aishperf = _aishperf_catalog()
    aishperf_cases = aishperf.load_cases()
    return {
        "family": "智算故障实验",
        "tool": tool_status,
        "environment": tool_status.get("environment"),
        "scenario_count": len(scenarios),
        "scenarios": scenarios,
        "external_sources": {
            "aishperf": {
                "status": aishperf.status(),
                "case_count": len(aishperf_cases),
                "domain_counts": dict(sorted(Counter(domain for case in aishperf_cases for domain in case.domains).items())),
                "sample_gpu_cases": aishperf.list_cases(domain="gpu", limit=8),
                "aiops_eval": aishperf.aiops_eval_capability(),
            }
        },
    }


@app.get("/api/integrations/aishperf/status")
async def aishperf_status():
    """Expose read-only AISHPerf/Alcor source availability and capabilities."""
    catalog = _aishperf_catalog()
    cases = catalog.load_cases()
    domains = Counter(domain for case in cases for domain in case.domains)
    return {
        "status": "ok" if cases else "unavailable",
        "source": catalog.status(),
        "case_count": len(cases),
        "domain_counts": dict(sorted(domains.items())),
        "aiops_eval": catalog.aiops_eval_capability(),
        "integration_mode": "catalog_and_dry_run",
        "safety": {
            "auto_execute": False,
            "requires_approval": True,
            "note": "AISHPerf 场景默认只作为外部故障 catalog 和 dry-run 计划接入，不直接执行高风险命令。",
        },
    }


@app.get("/api/integrations/aishperf/scenarios")
async def aishperf_scenarios(
    domain: str = "",
    query: str = "",
    limit: int = Query(200, ge=1, le=500),
):
    """List AISHPerf aiops-chaos cases as external fault scenarios."""
    catalog = _aishperf_catalog()
    return {
        "status": "ok" if catalog.status().get("available") else "unavailable",
        "source": catalog.status(),
        "scenarios": catalog.list_cases(domain=domain, query=query, limit=limit),
    }


@app.get("/api/integrations/aishperf/scenarios/{case_id}")
async def aishperf_scenario_detail(case_id: str):
    """Show one AISHPerf case with commands for review."""
    catalog = _aishperf_catalog()
    case = catalog.find_case(case_id)
    if not case:
        raise HTTPException(404, "AISHPerf scenario not found")
    return {
        "status": "ok",
        "source": catalog.status(),
        "scenario": case.to_dict(include_commands=True),
    }


@app.post("/api/integrations/aishperf/scenarios/{case_id}/dry-run")
async def aishperf_scenario_dry_run(case_id: str, request: Request):
    """Build a dry-run AEMU execution plan for an AISHPerf case."""
    body = await request.json()
    catalog = _aishperf_catalog()
    try:
        return catalog.build_dry_run_plan(
            case_id,
            node=str(body.get("node") or "${NODE}"),
            namespace=str(body.get("namespace") or "${NAMESPACE}"),
        )
    except KeyError:
        raise HTTPException(404, "AISHPerf scenario not found")


@app.post("/api/integrations/aishperf/scenarios/{case_id}/evaluate")
async def aishperf_scenario_evaluate(case_id: str, request: Request):
    """Build an AISHPerf-style evaluation summary for an external AISHPerf case."""
    body = await request.json()
    catalog = _aishperf_catalog()
    case = catalog.find_case(case_id)
    if not case:
        raise HTTPException(404, "AISHPerf scenario not found")
    scenario = {
        "id": case.id,
        "name": case.name,
        "fault_type": body.get("fault_type") or "aishperf_aiinfra_fault",
        "layer": "aiinfra",
        "severity": case.risk,
        "expected_symptoms": case.domains,
    }
    return build_aiops_eval_summary(
        scenario=scenario,
        body={
            **body,
            "result": body.get("result") or {"status": "dry_run", "action": "aishperf_dry_run"},
        },
        source="aishperf.aiops-chaos",
    )


@app.post("/api/llm-faultlab/preflight/{scenario_id}")
async def llm_faultlab_preflight(scenario_id: str, request: Request):
    """Check runtime prerequisites before executing one LLM fault scenario."""
    body = await request.json()
    scenario = _find_llm_fault_scenario(scenario_id)
    if not scenario:
        raise HTTPException(404, "Scenario not found")
    preflight = await asyncio.to_thread(
        _llm_fault_preflight,
        scenario,
        body,
        action=body.get("action") or "inject",
    )
    return {
        "status": "ok",
        "scenario": scenario,
        "preflight": preflight,
    }


@app.post("/api/llm-faultlab/inject/{scenario_id}")
async def llm_faultlab_inject(scenario_id: str, request: Request):
    """Inject one vLLM inference fault scenario. Defaults to dry_run=true."""
    body = await request.json()
    scenarios = _load_llm_fault_scenarios()
    scenario = next((s for s in scenarios if s.get("id") == scenario_id), None)
    if not scenario:
        raise HTTPException(404, "Scenario not found")
    return await _run_llm_fault("inject", scenario, body)


@app.post("/api/llm-faultlab/experiment/{scenario_id}")
async def llm_faultlab_experiment(scenario_id: str, request: Request):
    """Run baseline -> inject -> measure -> recover for one vLLM scenario."""
    body = await request.json()
    scenarios = _load_llm_fault_scenarios()
    scenario = next((s for s in scenarios if s.get("id") == scenario_id), None)
    if not scenario:
        raise HTTPException(404, "Scenario not found")
    return await _run_llm_fault("experiment", scenario, body)


@app.post("/api/llm-faultlab/cleanup/{scenario_id}")
async def llm_faultlab_cleanup(scenario_id: str, request: Request):
    """Recover the affected layer for one vLLM inference fault scenario."""
    body = await request.json()
    scenarios = _load_llm_fault_scenarios()
    scenario = next((s for s in scenarios if s.get("id") == scenario_id), None)
    if not scenario:
        raise HTTPException(404, "Scenario not found")
    return await _run_llm_fault("cleanup", scenario, body)


@app.post("/api/llm-faultlab/diagnose/{scenario_id}")
async def llm_faultlab_diagnose(scenario_id: str, request: Request):
    """Diagnose an LLM fault-lab result using scenario metadata and live observation."""
    body = await request.json()
    scenario = _find_llm_fault_scenario(scenario_id)
    if not scenario:
        raise HTTPException(404, "Scenario not found")
    nested_result = body.get("result") if isinstance(body.get("result"), dict) else {}
    fault_run_id = str(body.get("fault_run_id") or nested_result.get("fault_run_id") or "")
    if fault_run_id:
        stored_run = next((item for item in _load_fault_runs() if str(item.get("id") or "") == fault_run_id), None)
        if isinstance(stored_run, dict):
            if isinstance(stored_run.get("observation_extrema"), dict):
                body["observation_extrema"] = stored_run.get("observation_extrema")
            if isinstance(stored_run.get("observation_context"), dict):
                body["observation_context"] = stored_run.get("observation_context")
            if isinstance(stored_run.get("metrics_target"), dict):
                body["metrics_target"] = stored_run.get("metrics_target")
    diagnosis = _build_llm_fault_diagnosis(body, scenario)
    return {
        "status": "diagnosed",
        "scenario": scenario,
        "diagnosis": diagnosis,
        "observation_required": not bool(body.get("observation")),
        "next_actions": [
            "执行或刷新观测，确认 endpoint、/metrics、GPU 和进程状态",
            "生成自愈建议，K8s 目标可进入自愈中心计划，裸机目标走受控恢复动作",
        ],
    }


@app.post("/api/llm-faultlab/heal/{scenario_id}")
async def llm_faultlab_heal(scenario_id: str, request: Request):
    """Generate self-healing guidance for an LLM fault-lab diagnosis."""
    body = await request.json()
    scenario = _find_llm_fault_scenario(scenario_id)
    if not scenario:
        raise HTTPException(404, "Scenario not found")
    diagnosis = body.get("diagnosis") if isinstance(body.get("diagnosis"), dict) else _build_llm_fault_diagnosis(body, scenario)
    target_mode = str(body.get("target_mode") or "k8s")
    if target_mode == "host":
        host = _find_llm_host(body.get("host_id"))
        return {
            "status": "suggested",
            "scenario": scenario,
            "diagnosis": diagnosis,
            "heal": _build_llm_host_heal_plan(body, scenario, diagnosis, host),
        }

    heal_body = {
        "source": "llm_faultlab",
        "namespace": body.get("namespace") or "default",
        "deployment": body.get("deployment") or "vllm-server",
        "fault_type": diagnosis.get("k8s_fault_type") or "HighLatency",
        "root_cause": diagnosis.get("root_cause"),
        "root_cause_component": body.get("deployment") or diagnosis.get("root_cause_component") or "vllm-server",
        "remediation_hint": diagnosis.get("remediation_suggestion"),
        "message": " ".join(str(x) for x in diagnosis.get("evidence", []) or []),
        "diagnosis": {
            **diagnosis,
            "fault_type": diagnosis.get("k8s_fault_type") or "HighLatency",
        },
    }
    heal = _build_heal_suggestions(heal_body)
    return {
        "status": "suggested",
        "scenario": scenario,
        "diagnosis": diagnosis,
        "heal": heal,
        "heal_payload": heal_body,
    }


@app.post("/api/llm-faultlab/evaluate/{scenario_id}")
async def llm_faultlab_evaluate(scenario_id: str, request: Request):
    """Build an AISHPerf-style read-only evaluation summary for one LLM run."""
    body = await request.json()
    scenario = _find_llm_fault_scenario(scenario_id)
    if not scenario:
        raise HTTPException(404, "Scenario not found")
    return build_aiops_eval_summary(scenario=scenario, body=body)


@app.post("/api/llm-faultlab/evaluate/{scenario_id}/archive")
async def llm_faultlab_archive_evaluation(scenario_id: str, request: Request):
    """Archive one AISHPerf-style summary as run.json/result.json artifacts."""
    body = await request.json()
    scenario = _find_llm_fault_scenario(scenario_id)
    if not scenario:
        raise HTTPException(404, "Scenario not found")
    summary = build_aiops_eval_summary(scenario=scenario, body=body)
    return {"status": "archived", "archive": _archive_aishperf_eval(summary, body)}


@app.get("/api/integrations/aishperf/eval-runs")
async def aishperf_eval_runs(limit: int = 50):
    """List archived AISHPerf-style AgenticSRE evaluation summaries."""
    runs = _load_aishperf_eval_runs()
    return {"runs": runs[: max(1, min(limit, 200))], "total": len(runs)}


@app.get("/api/integrations/aishperf/eval-runs/{run_id}")
async def aishperf_eval_run_artifact(run_id: str):
    """Return archived run.json/result.json artifacts for one evaluation run."""
    run = next((item for item in _load_aishperf_eval_runs() if item.get("id") == run_id), None)
    if not run:
        raise HTTPException(404, "Archived evaluation run not found")
    artifact_dir = Path(str(run.get("artifact_dir") or ""))
    if not artifact_dir.exists() or not artifact_dir.is_dir():
        return run
    artifact: Dict[str, Any] = {}
    for filename in ("run.json", "result.json", "metric_definitions.json", "summary.json", "inputs.json"):
        path = artifact_dir / filename
        if path.exists():
            try:
                artifact[filename] = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                artifact[filename] = {"error": str(exc)}
    return {"run": run, "artifact": artifact}


@app.get("/api/fault-experiments")
async def fault_experiments(limit: int = 50):
    """List live fault benchmark experiments."""
    runs = _load_fault_experiments()
    trimmed = runs[: max(1, min(limit, 200))]
    return {"experiments": trimmed, "total": len(runs)}


@app.post("/api/fault-experiments")
async def create_fault_experiment(request: Request):
    """Create a live benchmark spec without starting it."""
    body = await request.json()
    spec = _normalize_fault_experiment_body(body)
    run_id = f"exp-{uuid.uuid4().hex[:10]}"
    run = {
        "id": run_id,
        "name": spec["name"],
        "status": "created",
        "created_at": time.time(),
        "updated_at": time.time(),
        "spec": spec,
        "timeline": [],
        "results": [],
        "artifact_path": None,
    }
    _save_fault_experiment(run)
    return run


@app.post("/api/fault-experiments/start")
async def create_and_start_fault_experiment(request: Request):
    """Create and immediately start a live benchmark experiment."""
    body = await request.json()
    spec = _normalize_fault_experiment_body(body)
    run_id = f"exp-{uuid.uuid4().hex[:10]}"
    run = {
        "id": run_id,
        "name": spec["name"],
        "status": "queued",
        "created_at": time.time(),
        "updated_at": time.time(),
        "spec": spec,
        "timeline": [],
        "results": [],
        "artifact_path": None,
    }
    _save_fault_experiment(run)
    _state["fault_experiment_cancel"][run_id] = asyncio.Event()
    task = asyncio.create_task(_execute_fault_experiment(run_id))
    _state["fault_experiment_tasks"][run_id] = task
    return run


@app.post("/api/fault-experiments/{run_id}/start")
async def start_fault_experiment(run_id: str):
    """Start a previously created live benchmark experiment."""
    records = _load_fault_experiments()
    run = next((r for r in records if r.get("id") == run_id), None)
    if not run:
        raise HTTPException(404, "Experiment not found")
    if run.get("status") in {"running", "queued"}:
        return run
    if run.get("status") not in {"created", "failed", "cancelled"}:
        raise HTTPException(400, f"Experiment status '{run.get('status')}' cannot be started")
    run["status"] = "queued"
    run["updated_at"] = time.time()
    run["timeline"] = []
    run["results"] = []
    run["error"] = None
    _save_fault_experiment(run)
    _state["fault_experiment_cancel"][run_id] = asyncio.Event()
    task = asyncio.create_task(_execute_fault_experiment(run_id))
    _state["fault_experiment_tasks"][run_id] = task
    return run


@app.post("/api/fault-experiments/{run_id}/cancel")
async def cancel_fault_experiment(run_id: str):
    """Request cancellation for a running live benchmark."""
    records = _load_fault_experiments()
    run = next((r for r in records if r.get("id") == run_id), None)
    if not run:
        raise HTTPException(404, "Experiment not found")
    event = _state["fault_experiment_cancel"].get(run_id)
    if event:
        event.set()
    run["cancel_requested"] = True
    run["updated_at"] = time.time()
    if run.get("status") in {"created", "queued"}:
        run["status"] = "cancelled"
    _save_fault_experiment(run)
    return run


@app.get("/api/fault-experiments/{run_id}")
async def get_fault_experiment(run_id: str):
    """Return one live benchmark run."""
    records = _load_fault_experiments()
    run = next((r for r in records if r.get("id") == run_id), None)
    if not run:
        raise HTTPException(404, "Experiment not found")
    return run


@app.get("/api/fault-experiments/{run_id}/artifact")
async def get_fault_experiment_artifact(run_id: str):
    """Return the saved artifact for a completed benchmark run."""
    records = _load_fault_experiments()
    run = next((r for r in records if r.get("id") == run_id), None)
    if not run:
        raise HTTPException(404, "Experiment not found")
    artifact_path = run.get("artifact_path")
    if artifact_path and Path(artifact_path).exists():
        try:
            return json.loads(Path(artifact_path).read_text(encoding="utf-8"))
        except Exception as e:
            raise HTTPException(500, f"Failed to read artifact: {e}")
    return run


# ─────────────────────────────────────────
# Fault Report APIs (故障报告)
# ─────────────────────────────────────────

def _build_report(run: dict) -> dict:
    """构建完整的中文故障报告数据"""
    from memory.history_retrieval import recorded_history
    result = run.get("result") or {}
    inner = result.get("result", result) if isinstance(result, dict) else result
    rca = (inner.get("result") if isinstance(inner, dict) and isinstance(inner.get("result"), dict) else inner) or {}

    events = run.get("events", [])
    hypotheses = []
    evidence_list = []
    judge_data = None
    remediation_data = None
    for evt in events:
        ename = evt.get("event", "")
        if ename == "hypotheses":
            hypotheses = evt.get("items", [])
        elif ename == "evidence":
            evidence_list.append({"agent": evt.get("agent", ""), "summary": evt.get("summary", ""), "success": evt.get("success", False)})
        elif ename == "judge":
            judge_data = evt.get("data")
        elif ename == "remediation":
            remediation_data = evt.get("data")
        elif ename == "remediation_executed":
            remediation_data = evt.get("data")

    started = run.get("started_at", 0)
    duration = None
    for envelope in (result, inner, rca):
        value = envelope.get("duration_s") if isinstance(envelope, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            duration = value
            break
    if duration is None and started:
        finished = run.get("finished_at")
        if finished is None and run.get("status") in {"queued", "starting", "running"}:
            finished = time.time()
        if isinstance(finished, (int, float)) and math.isfinite(finished):
            duration = max(0, finished - started)

    return {
        "report_id": run.get("id", ""),
        "title": "故障诊断报告",
        "generated_at": time.time(),
        "basic_info": {
            "报告编号": run.get("id", ""),
            "开始时间": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)) if started else "-",
            "故障描述": run.get("query", ""),
            "命名空间": run.get("namespace", "（全部）"),
            "分析状态": run.get("status", "unknown"),
            "耗时(秒)": round(duration, 1) if duration is not None else "-",
        },
        "detection": {
            "告警列表": [e.get("data", {}) for e in events if e.get("event") == "phase_complete" and e.get("phase") == 0],
        },
        "diagnosis": {
            "假设列表": [{"排名": i+1, "描述": h.get("description", ""), "置信度": f"{round(h.get('confidence', 0)*100)}%"} for i, h in enumerate(hypotheses)],
            "证据收集": [{"智能体": e["agent"], "结果": e["summary"], "成功": e["success"]} for e in evidence_list],
            "根因结论": rca.get("root_cause", "N/A"),
            "置信度": f"{round((rca.get('confidence', 0) or 0)*100)}%",
            "故障类型": rca.get("fault_type", ""),
            "受影响服务": rca.get("affected_services", []),
            "证据摘要": rca.get("evidence_summary", {}),
        },
        "remediation": {
            "状态": (remediation_data or {}).get("status", "未触发"),
            "修复方案": (remediation_data or {}).get("plan", {}),
            "执行结果": (remediation_data or {}).get("actions", []),
        },
        "timeline": rca.get("timeline", []),
        "ontology": {key: value for key, value in build_investigation(run).items()
                     if key not in {"observations", "tool_executions"}},
        "supplemental_lineage": _json_safe_snapshot(run.get("lineage_snapshots", [])),
        "lineage_comparisons": latest_comparisons(_json_safe_snapshot(run.get("lineage_snapshots", []))),
        "diagnostic_strategy": strategy_view(_json_safe_snapshot(run)),
        "history_retrieval": recorded_history(run),
        "quality": {
            "评级": (judge_data or {}).get("judge_level", ""),
            "评分": (judge_data or {}).get("combined_score", 0),
            "需要人工复核": (judge_data or {}).get("needs_review", False),
        },
        "suggestions": {
            "修复建议": rca.get("remediation_suggestion", ""),
            "预防措施": rca.get("prevention", ""),
        },
        "logs": run.get("logs", [])[-50:],
    }


@app.get("/api/report/{run_id}")
async def get_report(run_id: str):
    """生成故障诊断报告 JSON"""
    run = _state["rca_runs"].get(run_id)
    if not run:
        raise HTTPException(404, "分析记录不存在")
    return _build_report(run)


@app.get("/api/report/{run_id}/export", response_class=HTMLResponse)
async def export_report(run_id: str):
    """导出故障报告为自包含 HTML（可打印/另存为）"""
    run = _state["rca_runs"].get(run_id)
    if not run:
        raise HTTPException(404, "分析记录不存在")
    report = _build_report(run)
    bi = report["basic_info"]
    diag = report["diagnosis"]
    rem = report["remediation"]
    qual = report["quality"]
    sugg = report["suggestions"]

    def esc(s):
        if not isinstance(s, str):
            s = str(s) if s is not None else ""
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # Build hypotheses table
    hyp_rows = ""
    for h in diag.get("假设列表", []):
        hyp_rows += f'<tr><td>{h["排名"]}</td><td>{esc(h["描述"])}</td><td>{h["置信度"]}</td></tr>'

    # Build evidence table
    ev_rows = ""
    for e in diag.get("证据收集", []):
        status = "成功" if e["成功"] else "失败"
        ev_rows += f'<tr><td>{esc(e["智能体"])}</td><td>{esc(e["结果"])}</td><td>{status}</td></tr>'

    # Build timeline
    tl_html = ""
    for t in report.get("timeline", []):
        tl_html += f'<div class="tl-item"><span class="tl-time">{esc(t.get("time",""))}</span><span>{esc(t.get("event",""))}</span></div>'

    # Build remediation
    rem_html = f'<p><strong>状态：</strong>{esc(rem["状态"])}</p>'
    for act in rem.get("执行结果", []):
        if isinstance(act, dict):
            rem_html += f'<div class="rem-item">{esc(act.get("description",""))} — {esc(act.get("status",""))}</div>'

    # Evidence summary
    ev_summary = ""
    for k, v in diag.get("证据摘要", {}).items():
        if v:
            ev_summary += f'<div class="ev-block"><strong>{esc(k)}:</strong> {esc(str(v)[:500])}</div>'

    # Affected services
    affected = ", ".join([str(s) for s in diag.get("受影响服务", [])]) or "未知"
    evidence_section = f"<h3>2.4 证据摘要</h3>{ev_summary}" if ev_summary else ""
    timeline_section = f"<h2>四、事件时间线</h2>{tl_html}" if tl_html else ""
    review_badge = ' &nbsp;&nbsp;<span class="badge badge-bronze">需要人工复核</span>' if qual.get("需要人工复核") else ""
    fix_suggestion = (
        f'<div class="suggestion"><strong>修复建议：</strong>{esc(sugg["修复建议"])}</div>'
        if sugg.get("修复建议") else '<p style="color:#94a3b8">无修复建议</p>'
    )
    prevention = (
        f'<div class="suggestion"><strong>预防措施：</strong>{esc(sugg["预防措施"])}</div>'
        if sugg.get("预防措施") else ""
    )
    lineage_html = ""
    if report.get("supplemental_lineage"):
        lineage_html = ('<h2>补充：发布与镜像追溯</h2><p>诊断后独立采集，未经过原诊断审核；'
                        '关系不能单独证明因果，修订号不代表 Git 提交。</p><pre style="white-space:pre-wrap;overflow-wrap:anywhere">'
                        + esc(json.dumps(report["supplemental_lineage"], ensure_ascii=False, indent=2)) + '</pre>')
        if report.get("lineage_comparisons"):
            lineage_html += ('<h3>同一对象最近两次快照比较</h3><p>仅表示已采集观测的差异，不能证明发布或故障因果。</p>'
                '<pre style="white-space:pre-wrap;overflow-wrap:anywhere">' + esc(json.dumps(
                    report["lineage_comparisons"], ensure_ascii=False, indent=2)) + '</pre>')
    strategy_html = ('<h2>假设产生与调查调整</h2><p>候选和引用是待审核的判断，引用存在不等于因果成立；'
        '本次调查调整不代表已学习跨事故最优策略。</p><pre style="white-space:pre-wrap;overflow-wrap:anywhere">'
        + esc(json.dumps(report["diagnostic_strategy"], ensure_ascii=False, indent=2)) + '</pre>')
    history_html = ('<h2>历史知识检索</h2><p>历史仅用于候选假设；not_recorded 表示旧记录未保存检索状态。</p>'
        '<pre style="white-space:pre-wrap;overflow-wrap:anywhere">'
        + esc(json.dumps(report['history_retrieval'], ensure_ascii=False, indent=2)) + '</pre>')

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>故障诊断报告 — {esc(bi['报告编号'])}</title>
<style>
  @media print {{ body {{ font-size: 12px; }} .no-print {{ display: none; }} }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, 'Microsoft YaHei', sans-serif; color: #1a2b42; padding: 40px; max-width: 900px; margin: 0 auto; line-height: 1.6; background: #fff; }}
  h1 {{ font-size: 24px; text-align: center; margin-bottom: 8px; color: #1e6fd9; }}
  .subtitle {{ text-align: center; color: #64748b; margin-bottom: 30px; font-size: 14px; }}
  h2 {{ font-size: 16px; color: #1e6fd9; border-bottom: 2px solid #1e6fd9; padding-bottom: 6px; margin: 24px 0 12px; }}
  h3 {{ font-size: 14px; color: #334155; margin: 16px 0 8px; }}
  table {{ width: 100%; border-collapse: collapse; margin: 8px 0 16px; font-size: 13px; }}
  th {{ background: #f0f4f8; padding: 8px 12px; text-align: left; border: 1px solid #e2e8f0; font-weight: 600; }}
  td {{ padding: 8px 12px; border: 1px solid #e2e8f0; }}
  .info-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 8px 0 16px; }}
  .info-item {{ padding: 8px 12px; background: #f8fafc; border-radius: 6px; font-size: 13px; }}
  .info-item strong {{ color: #1e6fd9; }}
  .root-cause {{ background: #f0f4f8; border-left: 4px solid #1e6fd9; padding: 16px; margin: 12px 0; border-radius: 0 6px 6px 0; font-size: 15px; }}
  .confidence {{ display: inline-block; padding: 4px 12px; border-radius: 12px; font-size: 13px; font-weight: 600; }}
  .conf-high {{ background: #d1fae5; color: #065f46; }}
  .conf-mid {{ background: #fef3c7; color: #92400e; }}
  .conf-low {{ background: #fee2e2; color: #991b1b; }}
  .tl-item {{ display: flex; gap: 12px; padding: 6px 0; border-bottom: 1px solid #f1f5f9; font-size: 13px; }}
  .tl-time {{ color: #64748b; min-width: 120px; font-weight: 600; }}
  .ev-block {{ padding: 8px 12px; background: #f8fafc; border-radius: 6px; margin: 4px 0; font-size: 13px; }}
  .rem-item {{ padding: 6px 12px; background: #eff6ff; border-radius: 4px; margin: 4px 0; font-size: 13px; }}
  .suggestion {{ background: #f0fdf4; border-left: 4px solid #10b981; padding: 12px 16px; margin: 8px 0; border-radius: 0 6px 6px 0; font-size: 13px; }}
  .print-btn {{ display: block; margin: 30px auto; padding: 10px 32px; background: #1e6fd9; color: #fff; border: none; border-radius: 8px; font-size: 14px; cursor: pointer; }}
  .print-btn:hover {{ background: #1558b0; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; font-weight: 600; }}
  .badge-gold {{ background: #fef3c7; color: #d97706; }}
  .badge-silver {{ background: #f1f5f9; color: #64748b; }}
  .badge-bronze {{ background: #fee2e2; color: #ef4444; }}
</style>
</head>
<body>
<h1>故障诊断报告</h1>
<div class="subtitle">AgenticSRE 智能运维系统 — 自动生成</div>

<h2>一、基本信息</h2>
<div class="info-grid">
  <div class="info-item"><strong>报告编号：</strong>{esc(bi['报告编号'])}</div>
  <div class="info-item"><strong>开始时间：</strong>{esc(bi['开始时间'])}</div>
  <div class="info-item"><strong>分析状态：</strong>{esc(bi['分析状态'])}</div>
  <div class="info-item"><strong>耗时：</strong>{bi['耗时(秒)']} 秒</div>
</div>
<div class="info-item" style="margin-bottom:16px"><strong>故障描述：</strong>{esc(bi['故障描述'])}</div>

<h2>二、诊断阶段</h2>
<h3>2.1 假设列表</h3>
<table><thead><tr><th>排名</th><th>假设描述</th><th>置信度</th></tr></thead><tbody>{hyp_rows if hyp_rows else '<tr><td colspan="3" style="text-align:center;color:#94a3b8">无假设数据</td></tr>'}</tbody></table>

<h3>2.2 证据收集</h3>
<table><thead><tr><th>智能体</th><th>调查结果</th><th>状态</th></tr></thead><tbody>{ev_rows if ev_rows else '<tr><td colspan="3" style="text-align:center;color:#94a3b8">无证据数据</td></tr>'}</tbody></table>

<h3>2.3 根因结论</h3>
<div class="root-cause">{esc(diag['根因结论'])}</div>
<p style="margin:8px 0">
  <strong>置信度：</strong><span class="confidence conf-low">{diag.get('置信度','')}</span>
  &nbsp;&nbsp;<strong>故障类型：</strong>{esc(diag.get('故障类型',''))}
  &nbsp;&nbsp;<strong>受影响服务：</strong>{esc(affected)}
</p>

    {evidence_section}

{lineage_html}
{strategy_html}
{history_html}
<h2>三、自愈修复</h2>
{rem_html}

    {timeline_section}

<h2>{'五' if tl_html else '四'}、质量评估</h2>
<p>
  <strong>评级：</strong><span class="badge badge-{str(qual.get('评级','') or 'bronze').lower() if isinstance(qual.get('评级',''), str) else 'bronze'}">{esc(str(qual.get('评级','') or '未评估'))}</span>
  &nbsp;&nbsp;<strong>评分：</strong>{round(float(qual.get('评分',0) or 0), 3)}
      {review_badge}
</p>

<h2>{'六' if tl_html else '五'}、建议</h2>
    {fix_suggestion}
    {prevention}

<div style="text-align:center" class="no-print">
  <button class="print-btn" onclick="window.print()" style="display:inline-block;margin:30px 8px;padding:10px 28px;background:#1e6fd9;color:#fff;border:none;border-radius:8px;font-size:14px;cursor:pointer">打印 / 导出 PDF</button>
  <a href="/api/report/{run_id}/word" style="display:inline-block;margin:30px 8px;padding:10px 28px;background:#10b981;color:#fff;border:none;border-radius:8px;font-size:14px;cursor:pointer;text-decoration:none">导出 Word 文档</a>
</div>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.get("/api/report/{run_id}/word")
async def export_report_word(run_id: str):
    """导出故障报告为 Word (.docx) 文档，使用 Word-compatible HTML 格式"""
    from io import BytesIO

    run = _state["rca_runs"].get(run_id)
    if not run:
        raise HTTPException(404, "分析记录不存在")
    report = _build_report(run)
    bi = report["basic_info"]
    diag = report["diagnosis"]
    rem = report["remediation"]
    qual = report["quality"]
    sugg = report["suggestions"]

    def esc(s):
        s = str(s) if s is not None else ""
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    affected = ", ".join([str(s) for s in diag.get("受影响服务", [])]) or "未知"

    hyp_rows = ""
    for h in diag.get("假设列表", []):
        hyp_rows += f'<tr><td>{h.get("排名","")}</td><td>{esc(h.get("描述",""))}</td><td>{h.get("置信度","")}</td></tr>'

    ev_rows = ""
    for e in diag.get("证据收集", []):
        ev_rows += f'<tr><td>{esc(e.get("智能体",""))}</td><td>{esc(e.get("结果",""))}</td><td>{"成功" if e.get("成功") else "失败"}</td></tr>'

    ev_summary = ""
    for k, v in diag.get("证据摘要", {}).items():
        if v:
            ev_summary += f'<p><b>{esc(k)}:</b> {esc(str(v)[:500])}</p>'

    tl_html = ""
    for t in report.get("timeline", []):
        tl_html += f'<p>{esc(t.get("time",""))}: {esc(t.get("event",""))}</p>'

    rem_html = f'<p><b>状态：</b>{esc(rem.get("状态","未触发"))}</p>'
    for act in rem.get("执行结果", []):
        if isinstance(act, dict):
            rem_html += f'<p>- {esc(act.get("description",""))} — {esc(act.get("status",""))}</p>'

    judge_level = str(qual.get("评级", "未评估") or "未评估")
    judge_score = 0
    try:
        judge_score = round(float(qual.get("评分", 0) or 0), 3)
    except Exception:
        pass

    word_html = f"""<html xmlns:o='urn:schemas-microsoft-com:office:office'
xmlns:w='urn:schemas-microsoft-com:office:word'
xmlns='http://www.w3.org/TR/REC-html40'>
<head><meta charset="UTF-8">
<!--[if gte mso 9]><xml><w:WordDocument><w:View>Print</w:View></w:WordDocument></xml><![endif]-->
<style>
body {{ font-family: '微软雅黑', 'Microsoft YaHei', SimSun, sans-serif; color: #1a2b42; padding: 30px; line-height: 1.8; font-size: 12pt; }}
h1 {{ font-size: 22pt; text-align: center; color: #1e6fd9; margin-bottom: 4px; }}
.subtitle {{ text-align: center; color: #888; margin-bottom: 24px; font-size: 10pt; }}
h2 {{ font-size: 14pt; color: #1e6fd9; border-bottom: 2px solid #1e6fd9; padding-bottom: 4px; margin-top: 20px; }}
h3 {{ font-size: 12pt; color: #334155; margin-top: 14px; }}
table {{ width: 100%; border-collapse: collapse; margin: 8px 0 14px; font-size: 10pt; }}
th {{ background: #f0f4f8; padding: 6px 10px; text-align: left; border: 1px solid #ccc; font-weight: bold; }}
td {{ padding: 6px 10px; border: 1px solid #ccc; }}
.root-cause {{ background: #f0f4f8; border-left: 4px solid #1e6fd9; padding: 12px; margin: 10px 0; font-size: 13pt; }}
.suggestion {{ background: #f0fdf4; border-left: 4px solid #10b981; padding: 10px 14px; margin: 6px 0; }}
</style>
</head>
<body>
<h1>故障诊断报告</h1>
<p class="subtitle">AgenticSRE 智能运维系统 — 自动生成</p>

<h2>一、基本信息</h2>
<table>
{''.join(f"<tr><th>{esc(k)}</th><td>{esc(str(v))}</td></tr>" for k, v in bi.items())}
</table>

<h2>二、诊断阶段</h2>
<h3>2.1 假设列表</h3>
<table><tr><th>排名</th><th>假设描述</th><th>置信度</th></tr>
{hyp_rows if hyp_rows else '<tr><td colspan="3" style="text-align:center;color:#999">无假设数据</td></tr>'}
</table>

<h3>2.2 证据收集</h3>
<table><tr><th>智能体</th><th>调查结果</th><th>状态</th></tr>
{ev_rows if ev_rows else '<tr><td colspan="3" style="text-align:center;color:#999">无证据数据</td></tr>'}
</table>

<h3>2.3 根因结论</h3>
<div class="root-cause">{esc(diag.get('根因结论', 'N/A'))}</div>
<p><b>置信度:</b> {esc(diag.get('置信度',''))} &nbsp; <b>故障类型:</b> {esc(diag.get('故障类型',''))} &nbsp; <b>受影响服务:</b> {esc(affected)}</p>

{f'<h3>2.4 证据摘要</h3>{ev_summary}' if ev_summary else ''}

<h2>三、自愈修复</h2>
{rem_html}

{f'<h2>四、事件时间线</h2>{tl_html}' if tl_html else ''}

<h2>{'五' if tl_html else '四'}、质量评估</h2>
<p><b>评级:</b> {esc(judge_level)} &nbsp; <b>评分:</b> {judge_score}{'  <b style="color:red">需要人工复核</b>' if qual.get('需要人工复核') else ''}</p>

<h2>{'六' if tl_html else '五'}、建议</h2>
{f'<div class="suggestion"><b>修复建议：</b>{esc(sugg.get("修复建议",""))}</div>' if sugg.get('修复建议') else '<p style="color:#999">无修复建议</p>'}
{f'<div class="suggestion"><b>预防措施：</b>{esc(sugg.get("预防措施",""))}</div>' if sugg.get('预防措施') else ''}

</body></html>"""

    buf = BytesIO(word_html.encode('utf-8'))
    filename = f"故障报告_{run_id}.doc"
    from urllib.parse import quote
    return StreamingResponse(
        buf,
        media_type="application/msword",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


# ─────────────────────────────────────────
# Hermes Agent Integration (NousResearch)
# ─────────────────────────────────────────

_hermes_sessions: Dict[str, Dict] = {}  # session_id → {agent, history, messages, status}
_hermes_probe_cache = {}
_hermes_probe_lock = threading.Lock()


def _init_hermes_agent():
    """Verify the actual subprocess import, without contacting a model."""
    hermes_paths = [
        str(ROOT_DIR / "hermes-agent"),
        "/tmp/hermes-agent",
        "/home/ubuntu/AgenticSRE/hermes-agent",
    ]
    for p in hermes_paths:
        if Path(p).exists() and Path(p, "run_agent.py").exists():
            # Use the same interpreter and package precedence as chat. The
            # import is isolated because both projects have a tools package.
            key = (p, sys.executable, Path(p, "run_agent.py").stat().st_mtime_ns)
            with _hermes_probe_lock:
                cached = _hermes_probe_cache.get(key)
                if cached and time.monotonic() - cached[0] < 60:
                    return cached[1]
                script = '''import json, sys
sys.path.insert(0, sys.argv[1])
try:
    from run_agent import AIAgent
    print("HERMES_PROBE:" + json.dumps({"ok": True}))
except Exception as exc:
    print("HERMES_PROBE:" + json.dumps({"ok": False, "type": type(exc).__name__,
        "module": getattr(exc, "name", None) if isinstance(exc, ModuleNotFoundError) else None}))
'''
                try:
                    proc = subprocess.run([sys.executable, "-c", script, p], cwd=p,
                                          capture_output=True, text=True, timeout=15)
                    lines = [line[13:] for line in proc.stdout.splitlines() if line.startswith("HERMES_PROBE:")]
                    probe = json.loads(lines[-1]) if lines else {}
                    error = None if proc.returncode == 0 and probe.get("ok") else "智能助手运行时导入失败"
                    if error:
                        module = str(probe.get("module") or "")
                        if re.fullmatch(r"[a-zA-Z0-9_.]+", module):
                            error += f"：缺少依赖 {module}"
                except subprocess.TimeoutExpired:
                    error = "智能助手运行时检查超时"
                except (OSError, ValueError):
                    error = "智能助手运行时检查失败"
                result = (None, error) if error else (p, None)
                _hermes_probe_cache.clear()
                _hermes_probe_cache[key] = (time.monotonic(), result)
                return result
    return None, "智能助手运行时未安装或不可访问"


def _build_hermes_system_prompt(cfg) -> str:
    return (
        "You are an expert SRE assistant for AgenticSRE. The environment may contain multiple "
        "general-compute Kubernetes clusters and AI inference clusters. Never assume cluster, "
        "namespace, workload, service, model, or node names; discover them from tools and evidence.\n\n"
        "You have terminal and file tools plus a live AgenticSRE Skill toolset:\n"
        "- agenticsre_skills_list: discover the current executable skill catalog.\n"
        "- agenticsre_skill_detail: inspect one skill's contract, risk, and instructions.\n"
        "- agenticsre_skill_run: invoke one exact skill selected by you.\n\n"
        "When the user asks which skills exist, call agenticsre_skills_list with limit=100 and summarize "
        "the returned live catalog. Before diagnosing an incident, inspect evidence, search the catalog, "
        "select exact skill ids yourself, and invoke them with agenticsre_skill_run. Never delegate selection "
        "or orchestration to a deterministic workflow. Do not claim that a skill was used unless an exact Skill tool result confirms it. "
        "Skill execution is always dry-run; healing stages may only produce guarded plans.\n\n"
        "Available observability endpoints:\n"
        f"- Prometheus: {cfg.observability.prometheus_url}\n"
        f"- Jaeger: {cfg.observability.jaeger_url}\n"
        f"- Elasticsearch: {cfg.observability.elasticsearch_url}\n\n"
        "Always respond in Chinese. Be concise, evidence-based, and actionable. "
        "Show commands only when they were actually run or are clearly marked as dry-run plans."
    )


def _build_hermes_subprocess_script(payload_b64: Optional[str] = None) -> str:
    payload_source = f'"{payload_b64}"' if payload_b64 is not None else 'sys.stdin.read()'
    return f'''
import sys, json, os, base64

payload = json.loads(base64.b64decode({payload_source}).decode())
sys.path.insert(0, payload["hermes_path"])
if payload["project_root"] not in sys.path:
    sys.path.append(payload["project_root"])
os.environ["OPENAI_API_KEY"] = payload["api_key"]

from run_agent import AIAgent
from integrations.hermes_skill_runtime import extract_hermes_tool_activity, register_hermes_skill_runtime_tools

agenticsre_tools = register_hermes_skill_runtime_tools(payload["skill_runtime_url"])

agent = AIAgent(
    base_url=payload["base_url"],
    api_key=payload["api_key"],
    model=payload["model"],
    max_iterations=30,
    enabled_toolsets=["terminal", "file", "agenticsre_skills"],
    save_trajectories=False,
    quiet_mode=True,
    ephemeral_system_prompt=payload["system_prompt"],
    skip_context_files=True,
    skip_memory=True,
)

result = agent.run_conversation(payload["message"], conversation_history=payload["history"])
tool_calls, skill_tool_results = extract_hermes_tool_activity(result.get("messages", []))

output = {{
    "response": result.get("final_response", ""),
    "api_calls": result.get("api_calls", 0),
    "completed": result.get("completed", False),
    "tool_calls": tool_calls,
    "skill_tool_results": skill_tool_results,
    "registered_agenticsre_tools": agenticsre_tools,
    "input_tokens": getattr(agent, "session_prompt_tokens", 0) or 0,
    "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0,
}}
print("HERMES_RESULT:" + json.dumps(output, ensure_ascii=False, default=str))
'''


def _run_hermes_sync(hermes_path: str, message: str, history: list, cfg) -> dict:
    """Run Hermes Agent in subprocess to avoid tools/ namespace collision."""
    import subprocess
    import tempfile
    import base64

    system_prompt = _build_hermes_system_prompt(cfg)

    # Encode all dynamic data as base64 to avoid quoting/escaping issues
    payload = {
        "hermes_path": hermes_path,
        "project_root": str(ROOT_DIR),
        "skill_runtime_url": SKILL_RUNTIME_URL,
        "base_url": cfg.llm.base_url,
        "api_key": cfg.llm.api_key,
        "model": cfg.llm.model,
        "system_prompt": system_prompt,
        "message": message,
        "history": history,
    }
    payload_b64 = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()

    # Credentials travel through stdin, never a script file or process args.
    script = _build_hermes_subprocess_script()

    try:
        # Find python in venv or system
        python_bin = sys.executable
        proc = subprocess.run(
            [python_bin, "-c", script], input=payload_b64,
            capture_output=True, text=True, timeout=180,
            cwd=hermes_path,
        )
        stdout = proc.stdout
        stderr = proc.stderr

        if "HERMES_RESULT:" in stdout:
            json_str = stdout.split("HERMES_RESULT:")[1].strip().split("\n")[0]
            return json.loads(json_str)
        else:
            return {"response": f"Agent 执行失败: {stderr[-500:] if stderr else 'no output'}", "error": True}
    except subprocess.TimeoutExpired:
        return {"response": "Agent 执行超时（180秒）", "error": True}
    except Exception as e:
        return {"response": f"执行错误: {str(e)}", "error": True}


def _build_hermes_capabilities(cfg) -> Dict[str, Any]:
    hermes_path, hermes_error = _init_hermes_agent()
    llm_configured = _llm_configured(cfg.llm)
    runtime_connected = False
    runtime_error = ""
    skills: List[Dict[str, Any]] = []
    try:
        catalog = _get_skill_runtime_skills()
        skills = [item for item in catalog.get("skills") or [] if isinstance(item, dict)]
        runtime_connected = True
    except Exception as exc:
        runtime_error = str(exc)[:300]

    domain_counts = Counter(str(item.get("domain") or "generic") for item in skills)
    ready = bool(hermes_path) and llm_configured and runtime_connected
    return {
        "status": "ready" if ready else "degraded",
        "hermes_agent": {
            "available": bool(hermes_path),
            "error": hermes_error or "",
        },
        "llm": {
            "configured": llm_configured,
            "model": str(getattr(cfg.llm, "model", "") or ""),
        },
        "skill_runtime": {
            "connected": runtime_connected,
            "skill_count": len(skills),
            "domains": [
                {"id": domain, "count": count}
                for domain, count in sorted(domain_counts.items(), key=lambda item: (-item[1], item[0]))
            ],
            "error": runtime_error,
        },
        "toolset": {
            "name": HERMES_SKILL_TOOLSET,
            "protocol_tool_count": len(HERMES_SKILL_TOOL_NAMES),
            "protocol_tools": list(HERMES_SKILL_TOOL_NAMES),
            "skill_tool_count": len(skills),
            "skill_tools": [
                {"skill_id": str(item.get("id") or ""), "tool": skill_tool_name(str(item.get("id") or ""))}
                for item in skills if item.get("id")
            ],
            "tool_count": len(HERMES_SKILL_TOOL_NAMES) + len(skills),
            "dry_run": True,
        },
    }


@app.get("/api/hermes/capabilities")
async def hermes_capabilities():
    """Report whether the assistant can reach its model and live Skill tools."""
    cfg = _get_config()
    return await asyncio.to_thread(_build_hermes_capabilities, cfg)


@app.post("/api/hermes/chat")
async def hermes_chat(request: Request):
    """Send a message to Hermes Agent and get a response."""
    body = await request.json()
    message = body.get("message", "").strip()
    session_id = body.get("session_id", "")

    if not message:
        raise HTTPException(400, "Missing 'message'")

    cfg = _get_config()
    if not _llm_configured(cfg.llm):
        raise HTTPException(503, "LLM API Key 未配置")

    hermes_path, hermes_error = await asyncio.to_thread(_init_hermes_agent)
    if not hermes_path:
        raise HTTPException(503, hermes_error)

    # Create or get session
    if not session_id or session_id not in _hermes_sessions:
        session_id = f"hermes-{uuid.uuid4().hex[:8]}"
        _hermes_sessions[session_id] = {
            "id": session_id,
            "messages": [],
            "status": "idle",
            "created_at": time.time(),
            "total_tokens": 0,
            "tool_calls_count": 0,
            "skill_tool_results_count": 0,
            "registered_tools": [],
        }

    session = _hermes_sessions[session_id]
    session["messages"].append({"role": "user", "content": message, "ts": time.time()})
    session["status"] = "thinking"

    # Run agent in background thread
    def _run():
        try:
            hermes_path, err = _init_hermes_agent()
            if err:
                session["messages"].append({
                    "role": "assistant", "content": f"Error: {err}", "ts": time.time()
                })
                session["status"] = "error"
                return

            # Build conversation history for context
            history = []
            for msg in session["messages"][:-1]:
                if msg["role"] in ("user", "assistant"):
                    history.append({"role": msg["role"], "content": msg["content"]})

            cfg = _get_config()
            result = _run_hermes_sync(hermes_path, message, history, cfg)

            response = result.get("response", "")
            tool_calls = result.get("tool_calls", [])
            skill_tool_results = result.get("skill_tool_results", [])
            input_tokens = result.get("input_tokens", 0) or 0
            output_tokens = result.get("output_tokens", 0) or 0
            registered_tools = result.get("registered_agenticsre_tools") or []

            session["messages"].append({
                "role": "assistant",
                "content": response,
                "ts": time.time(),
                "tool_calls": tool_calls,
                "skill_tool_results": skill_tool_results,
                "api_calls": result.get("api_calls", 0),
                "registered_tools": registered_tools,
                "tokens": {"input": input_tokens, "output": output_tokens},
            })
            session["registered_tools"] = registered_tools
            session["total_tokens"] += input_tokens + output_tokens
            session["tool_calls_count"] += len(tool_calls)
            session["skill_tool_results_count"] = session.get("skill_tool_results_count", 0) + len(skill_tool_results)
            session["status"] = "error" if result.get("error") else "idle"

        except Exception as e:
            logger.error(f"Hermes Agent error: {e}", exc_info=True)
            session["messages"].append({
                "role": "assistant",
                "content": f"Agent 执行出错: {str(e)}",
                "ts": time.time(),
            })
            session["status"] = "error"

    stream_after = len(session["messages"])
    thread = threading.Thread(target=_run, daemon=True, name=f"hermes-{session_id}")
    thread.start()

    return {
        "session_id": session_id,
        "status": "thinking",
        "message_count": stream_after,
    }


@app.get("/api/hermes/chat/{session_id}")
async def hermes_session_status(session_id: str):
    """Get current session state and messages."""
    session = _hermes_sessions.get(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    return session


@app.get("/api/hermes/chat/{session_id}/stream")
async def hermes_stream(session_id: str, after: Optional[int] = Query(None, ge=0)):
    """SSE stream for Hermes Agent responses."""
    session = _hermes_sessions.get(session_id)
    if not session:
        raise HTTPException(404, "Session not found")

    async def event_gen():
        msg_count = len(session["messages"]) if after is None else min(after, len(session["messages"]))
        while True:
            current_count = len(session["messages"])
            if current_count > msg_count:
                for msg in session["messages"][msg_count:]:
                    yield f"data: {json.dumps(msg, ensure_ascii=False, default=str)}\n\n"
                msg_count = current_count

            if session["status"] in ("idle", "error"):
                yield f"data: {json.dumps({'type': 'done', 'status': session['status']})}\n\n"
                break

            yield f": heartbeat\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.get("/api/hermes/sessions")
async def hermes_sessions_list():
    """List all Hermes Agent sessions."""
    sessions = []
    for sid, s in sorted(_hermes_sessions.items(), key=lambda x: x[1].get("created_at", 0), reverse=True):
        msg_count = len(s.get("messages", []))
        last_msg = s["messages"][-1]["content"][:80] if s.get("messages") else ""
        sessions.append({
            "id": sid,
            "status": s.get("status", "idle"),
            "message_count": msg_count,
            "last_message": last_msg,
            "total_tokens": s.get("total_tokens", 0),
            "tool_calls_count": s.get("tool_calls_count", 0),
            "skill_tool_results_count": s.get("skill_tool_results_count", 0),
            "created_at": s.get("created_at", 0),
        })
    return {"sessions": sessions[:20]}


@app.delete("/api/hermes/sessions/{session_id}")
async def hermes_delete_session(session_id: str):
    """Delete a Hermes Agent session."""
    if session_id in _hermes_sessions:
        del _hermes_sessions[session_id]
    return {"status": "ok"}


# ─────────────────────────────────────────
# Health & Meta
# ─────────────────────────────────────────

@app.get("/api/health")
async def health():
    cfg = _get_config()
    llm_ok = _llm_configured(cfg.llm)
    persistence = _state.get(
        "persistence_status",
        {"status": "initializing", "backend": "unknown"},
    )
    return {
        "status": "ok" if persistence.get("status") != "error" else "error",
        "timestamp": time.time(),
        "llm_configured": llm_ok,
        "persistence": persistence,
    }


@app.get("/api/config")
async def get_config_info():
    cfg = _get_config()
    return {
        "auth": {
            "enabled": _auth_enabled(),
            "username": _auth_username() if _auth_enabled() else "",
        },
        "llm_model": cfg.llm.model,
        "pipeline": {
            "max_iterations": cfg.pipeline.max_evidence_iterations,
            "confidence_threshold": cfg.pipeline.hypothesis_confidence_threshold,
            "enable_correlation": cfg.pipeline.enable_correlation,
            "enable_graph_rca": cfg.pipeline.enable_graph_rca,
            "enable_recovery": cfg.pipeline.enable_recovery,
        },
        "daemon": {
            "poll_interval": cfg.daemon.poll_interval_seconds,
            "dedup_ttl": cfg.daemon.dedup_ttl_seconds,
            "max_concurrent": cfg.daemon.max_concurrent_pipelines,
        },
    }


# ─────────────────────────────────────────
# Startup
# ─────────────────────────────────────────

@app.on_event("startup")
async def on_startup():
    logger.info("AgenticSRE Dashboard starting...")
    _load_rca_history()
    if _PLATFORM_CONFIG_FILE.exists():
        _apply_platform_config()
    else:
        _load_detection_config()
    _load_fault_targets()
    if (ROOT_DIR / 'data' / 'alert_compression' / 'jobs' / 'jobs.sqlite3').exists():
        await _ensure_alert_job_worker()
    cfg = _get_config()
    try:
        store_stats = _get_fault_store().stats()
        _state["persistence_status"] = {
            "status": "ok",
            "backend": store_stats.get("backend", "unknown"),
            "rules_count": store_stats.get("rules_count", 0),
            "faults_count": store_stats.get("faults_count", 0),
            "path": str(cfg.memory.db_path),
        }
        logger.info(
            "Persistence initialized: backend=%s path=%s",
            _state["persistence_status"]["backend"],
            cfg.memory.db_path,
        )
    except Exception as exc:
        _state["persistence_status"] = {
            "status": "error",
            "backend": "unavailable",
            "path": str(cfg.memory.db_path),
            "error": str(exc),
        }
        logger.exception("Persistent database initialization failed")
    if not _llm_configured(cfg.llm):
        logger.warning(
            "⚠️  LLM API Key 未配置！RCA、告警压缩等 LLM 功能将不可用。"
            "请在项目根目录 .env 文件中设置 LLM_API_KEY=<your-key>，"
            "或在 configs/config_cluster.yaml 中配置 llm.api_key，然后重启服务。"
        )
    else:
        logger.info(f"LLM configured: model={cfg.llm.model}, base_url={cfg.llm.base_url}")


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    uvicorn.run(app, host="0.0.0.0", port=8080)
