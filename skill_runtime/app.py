"""Standalone FastAPI service for skill-based operations workflows."""

from __future__ import annotations

import json
import re
import time
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .community import (
    HermesCommunitySource,
    LocalCommunityCatalog,
    convert_markdown_skill_to_draft,
    fetch_allowlisted_skill_markdown,
)
from .detail import skill_catalog_item_from_definition, skill_detail_from_definition
from .evolution import SkillDraftGenerator, SkillDraftVerifier
from .executor import SkillExecutor
from .workflow import SkillWorkflow


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SKILLS_DIR = ROOT_DIR / "skill_runtime" / "skills"
DEFAULT_HERMES_OPTIONAL_SKILLS_DIR = ROOT_DIR / "hermes-agent" / "optional-skills"
DEFAULT_COMMUNITY_SOURCES_DIR = ROOT_DIR / "skill_runtime" / "community_sources"
# Dashboard and Skill Runtime share /app/data. Keep each process in a distinct
# namespace so their read-modify-write JSON indexes cannot overwrite each other.
SKILL_WORKFLOW_RUNS_FILE = ROOT_DIR / "data" / "skill_runtime" / "workflow_runs.json"
SKILL_WORKFLOW_ARTIFACT_DIR = ROOT_DIR / "data" / "skill_runtime" / "workflow_runs"
_WORKFLOW_RUNS_LOCK = threading.RLock()
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")

app = FastAPI(title="AgenticSRE Skill Runtime", version="0.1.0")
executor = SkillExecutor.from_dir(DEFAULT_SKILLS_DIR)
workflow = SkillWorkflow(executor)
draft_generator = SkillDraftGenerator()
draft_verifier = SkillDraftVerifier()
hermes_community = HermesCommunitySource(DEFAULT_HERMES_OPTIONAL_SKILLS_DIR)
local_community = LocalCommunityCatalog(DEFAULT_COMMUNITY_SOURCES_DIR)


def _load_workflow_runs() -> List[Dict[str, Any]]:
    if not SKILL_WORKFLOW_RUNS_FILE.exists():
        return []
    try:
        data = json.loads(SKILL_WORKFLOW_RUNS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_workflow_run(record: Dict[str, Any]) -> None:
    with _WORKFLOW_RUNS_LOCK:
        SKILL_WORKFLOW_RUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
        runs = [item for item in _load_workflow_runs() if item.get("id") != record.get("id")]
        runs.insert(0, record)
        runs = runs[:300]
        _atomic_write_json(SKILL_WORKFLOW_RUNS_FILE, runs)
        SKILL_WORKFLOW_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(SKILL_WORKFLOW_ARTIFACT_DIR / f"{record['id']}.json", record)


def _atomic_write_json(path: Path, value: Any) -> None:
    """Replace JSON atomically so readers never observe a partial document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(
            json.dumps(value, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def _workflow_run_summary(record: Dict[str, Any]) -> Dict[str, Any]:
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    diagnosis = result.get("diagnosis") if isinstance(result.get("diagnosis"), dict) else {}
    migration = result.get("migration") if isinstance(result.get("migration"), dict) else {}
    execution_plan = result.get("execution_plan") if isinstance(result.get("execution_plan"), dict) else {}
    skill_runtime = result.get("skill_runtime") if isinstance(result.get("skill_runtime"), dict) else {}
    return {
        "id": record.get("id"),
        "status": record.get("status"),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "duration_ms": record.get("duration_ms"),
        "domain": result.get("domain") or record.get("domain"),
        "dry_run": result.get("dry_run", record.get("dry_run")),
        "fault_type": diagnosis.get("fault_type") or (record.get("request") or {}).get("payload", {}).get("fault_type"),
        "primary_skill_id": skill_runtime.get("primary_skill_id") or migration.get("primary_skill_id"),
        "matched_count": skill_runtime.get("matched_count") or migration.get("skill_matched_count") or 0,
        "migration_mode": migration.get("mode"),
        "confidence": diagnosis.get("confidence"),
        "command_count": execution_plan.get("command_count") or 0,
    }


class SkillRunRequest(BaseModel):
    payload: Dict[str, Any]
    stages: Optional[List[str]] = None
    dry_run: bool = True


class SkillMatchRequest(BaseModel):
    payload: Dict[str, Any]
    stages: Optional[List[str]] = None
    domain: str = ""
    dry_run: bool = True


class SkillCompareRequest(BaseModel):
    payload: Dict[str, Any]
    stages: Optional[List[str]] = None
    domain: str = ""
    dry_run: bool = True


class SkillWorkflowRequest(BaseModel):
    payload: Dict[str, Any]
    stages: Optional[List[str]] = None
    domain: str = ""
    dry_run: bool = True
    allow_legacy_fallback: bool = True


class SkillDraftRequest(BaseModel):
    incident: Dict[str, Any]
    traces: Optional[List[Dict[str, Any]]] = None
    feedback: Optional[List[Dict[str, Any]]] = None
    source: str = "api"


class SkillDraftVerifyRequest(BaseModel):
    draft: Dict[str, Any]


class SkillDraftReleaseGateRequest(BaseModel):
    draft: Dict[str, Any]
    evidence: Dict[str, Any]


class CommunityConvertRequest(BaseModel):
    relative_path: str
    target_domain: str = "generic"


class CommunityLocalConvertRequest(BaseModel):
    catalog_path: str
    target_domain: str = "generic"


class CommunityMarkdownConvertRequest(BaseModel):
    content: Optional[str] = None
    url: Optional[str] = None
    source: str = "manual"
    target_domain: str = "generic"


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok", "skills": len(executor.registry.list())}


@app.post("/api/skills/reload")
async def reload_skills() -> Dict[str, Any]:
    executor.registry.load()
    return {"status": "ok", "skills": len(executor.registry.list())}


@app.get("/api/skills")
async def list_skills() -> Dict[str, Any]:
    return {
        "skills": [skill_catalog_item_from_definition(item) for item in executor.registry.list()],
    }


@app.get("/api/skills/{skill_id}")
async def get_skill(skill_id: str) -> Dict[str, Any]:
    try:
        return executor.registry.get(skill_id).metadata.to_dict()
    except KeyError as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/skills/{skill_id}/detail")
async def get_skill_detail(skill_id: str) -> Dict[str, Any]:
    """Return metadata plus human docs and source files for review-only UI display."""
    try:
        definition = executor.registry.get(skill_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc))

    try:
        return skill_detail_from_definition(definition)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/skills/{skill_id}/run")
def run_skill(skill_id: str, body: SkillRunRequest) -> Dict[str, Any]:
    try:
        return executor.run(
            skill_id,
            body.payload,
            stages=body.stages,
            dry_run=body.dry_run,
        ).to_dict()
    except KeyError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/skills/run-matching")
def run_matching(body: SkillMatchRequest) -> Dict[str, Any]:
    return executor.run_matching(
        body.payload,
        stages=body.stages,
        domain=body.domain,
        dry_run=body.dry_run,
    )


@app.post("/api/compare/legacy")
def compare_legacy(body: SkillCompareRequest) -> Dict[str, Any]:
    """Run Skill Runtime beside legacy healing logic for migration validation."""
    skill_result = executor.run_matching(
        body.payload,
        stages=body.stages,
        domain=body.domain,
        dry_run=body.dry_run,
    )

    legacy_adapter = executor.adapters.get("legacy")
    legacy_result: Dict[str, Any]
    if legacy_adapter is None:
        legacy_result = {"status": "unavailable", "error": "legacy adapter is not configured"}
    else:
        try:
            fault_text = " ".join(
                str(body.payload.get(key, ""))
                for key in ("fault_type", "message", "root_cause", "diagnosis")
            )
            legacy_heal = legacy_adapter.build_heal_suggestions(body.payload)
            legacy_result = {
                "status": "ok",
                "fault_type": legacy_adapter.normalize_heal_fault_type(fault_text),
                "heal": legacy_heal,
            }
        except Exception as exc:  # pragma: no cover - defensive API boundary
            legacy_result = {"status": "error", "error": str(exc)}

    skill_ids = [item["skill"]["id"] for item in skill_result.get("runs", [])]
    legacy_suggestions = []
    if isinstance(legacy_result.get("heal"), dict):
        legacy_suggestions = legacy_result["heal"].get("suggestions") or []
    return {
        "skill_runtime": skill_result,
        "legacy": legacy_result,
        "comparison": {
            "skill_matched_count": len(skill_ids),
            "skill_ids": skill_ids,
            "legacy_fault_type": legacy_result.get("fault_type"),
            "legacy_suggestion_count": len(legacy_suggestions),
            "has_skill_match": bool(skill_ids),
            "has_legacy_suggestions": bool(legacy_suggestions),
        },
    }


@app.post("/api/workflows/operate")
def operate_workflow(body: SkillWorkflowRequest) -> Dict[str, Any]:
    """Run the skill-native detect/diagnose/heal/verify workflow.

    This is the migration endpoint for replacing the previous agent-centric
    diagnosis/healing path. It composes matching skills first and only uses the
    legacy adapter as an explicit fallback when skill coverage is incomplete.
    """
    run_id = f"skillwf-{uuid.uuid4().hex[:10]}"
    started = time.time()
    record: Dict[str, Any] = {
        "id": run_id,
        "status": "running",
        "started_at": started,
        "finished_at": None,
        "duration_ms": None,
        "domain": body.domain,
        "dry_run": body.dry_run,
        "request": body.model_dump(),
        "result": None,
        "error": "",
    }
    try:
        result = workflow.run(
            body.payload,
            stages=body.stages,
            domain=body.domain,
            dry_run=body.dry_run,
            allow_legacy_fallback=body.allow_legacy_fallback,
        )
        result["workflow_run_id"] = run_id
        record["status"] = result.get("status") or "completed"
        record["result"] = result
        return result
    except Exception as exc:
        record["status"] = "failed"
        record["error"] = str(exc)
        raise
    finally:
        finished = time.time()
        record["finished_at"] = finished
        record["duration_ms"] = int((finished - started) * 1000)
        _save_workflow_run(record)


@app.get("/api/workflows/runs")
async def workflow_runs(limit: int = 50) -> Dict[str, Any]:
    runs = _load_workflow_runs()
    selected = runs[: max(1, min(limit, 200))]
    return {
        "runs": [_workflow_run_summary(item) for item in selected],
        "total": len(runs),
    }


@app.get("/api/workflows/runs/{run_id}")
async def workflow_run_detail(run_id: str) -> Dict[str, Any]:
    if not SAFE_ID_RE.match(str(run_id or "")):
        raise HTTPException(400, "run_id may only contain letters, numbers, dot, underscore, and hyphen")
    for item in _load_workflow_runs():
        if item.get("id") == run_id:
            return item
    path = SKILL_WORKFLOW_ARTIFACT_DIR / f"{run_id}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise HTTPException(500, f"Failed to read workflow artifact: {exc}")
    raise HTTPException(404, "Skill workflow run not found")


@app.post("/api/skills/draft")
async def draft_skill(body: SkillDraftRequest) -> Dict[str, Any]:
    """Generate a review-only candidate skill draft from incident material."""
    draft = draft_generator.draft(body.model_dump()).to_dict()
    draft["verification"] = draft_verifier.verify_draft(draft).to_dict()
    return draft


@app.post("/api/skills/draft/verify")
async def verify_skill_draft(body: SkillDraftVerifyRequest) -> Dict[str, Any]:
    """Run static release gates for a generated or imported skill draft."""
    return draft_verifier.verify_draft(body.draft).to_dict()


@app.post("/api/skills/draft/release-gate")
async def release_gate_skill_draft(body: SkillDraftReleaseGateRequest) -> Dict[str, Any]:
    """Evaluate whether a reviewed draft has enough evidence to publish manually."""
    return draft_verifier.release_gate(body.draft, body.evidence)


@app.get("/api/community/hermes/skills")
async def list_hermes_skills(query: str = "", limit: int = 100) -> Dict[str, Any]:
    """List local Hermes optional skills as review-only import candidates."""
    skills = hermes_community.list(query=query, limit=max(1, min(limit, 500)))
    return {
        "source": "hermes",
        "count": len(skills),
        "skills": [item.to_dict() for item in skills],
    }


@app.post("/api/community/hermes/convert")
async def convert_hermes_skill(body: CommunityConvertRequest) -> Dict[str, Any]:
    """Convert a Hermes community skill into a disabled AgenticSRE draft."""
    draft = hermes_community.convert_to_draft(
        body.relative_path,
        target_domain=body.target_domain,
    ).to_dict()
    draft["verification"] = draft_verifier.verify_draft(draft).to_dict()
    return draft


@app.get("/api/community/sources")
async def list_community_sources() -> Dict[str, Any]:
    sources = local_community.list_sources()
    return {
        "count": len(sources),
        "sources": sources,
    }


@app.post("/api/community/local/convert")
async def convert_local_community_skill(body: CommunityLocalConvertRequest) -> Dict[str, Any]:
    draft = local_community.convert_local_skill(
        body.catalog_path,
        target_domain=body.target_domain,
    ).to_dict()
    draft["verification"] = draft_verifier.verify_draft(draft).to_dict()
    return draft


@app.post("/api/community/markdown/convert")
async def convert_markdown_community_skill(body: CommunityMarkdownConvertRequest) -> Dict[str, Any]:
    if body.content:
        content = body.content
        source = body.source
    elif body.url:
        content = fetch_allowlisted_skill_markdown(body.url)
        source = body.url
    else:
        raise HTTPException(400, "Either content or url is required")
    draft = convert_markdown_skill_to_draft(
        content=content,
        source=source,
        target_domain=body.target_domain,
    ).to_dict()
    draft["verification"] = draft_verifier.verify_draft(draft).to_dict()
    return draft
