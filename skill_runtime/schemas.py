"""Shared data contracts for the skill runtime.

The runtime intentionally keeps schemas small and JSON-friendly. Skills may
return extra keys, but the executor normalizes these core fields for API and UI
consumers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


JsonDict = Dict[str, Any]


@dataclass(frozen=True)
class SkillMetadata:
    id: str
    version: str
    name: str
    domain: str
    stages: List[str]
    description: str = ""
    risk: str = "low"
    requires_approval: bool = True
    safe_auto_execute: bool = False
    min_confidence_for_heal: float = 0.85
    min_confidence_for_auto_heal: float = 0.9
    min_evidence_sources: int = 1
    permissions: List[str] = field(default_factory=list)
    allowed_tools: List[str] = field(default_factory=list)
    tool_dependencies: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    license: str = ""
    entrypoint: str = "skill.py"
    matching_keywords: List[str] = field(default_factory=list)
    icon: str = ""
    category: str = ""
    source_dir: str = ""

    @classmethod
    def from_yaml(cls, data: JsonDict, source_dir: str = "") -> "SkillMetadata":
        data = normalize_skill_manifest(data)
        skill_id = str(data.get("id") or "").strip()
        if not skill_id:
            raise ValueError("skill manifest must define id")
        stages = data.get("stages") or data.get("stage") or []
        if isinstance(stages, str):
            stages = [stages]
        confidence = data.get("confidence") if isinstance(data.get("confidence"), dict) else {}
        policy = data.get("policy") if isinstance(data.get("policy"), dict) else {}
        matching = data.get("matching") if isinstance(data.get("matching"), dict) else {}
        permissions = data.get("permissions") or []
        allowed_tools = data.get("allowed_tools") or []
        tool_dependencies = data.get("tool_dependencies") or []
        matching_keywords = data.get("matching_keywords") or matching.get("keywords") or []
        tags = data.get("tags") or []
        ui = data.get("ui") if isinstance(data.get("ui"), dict) else {}
        entrypoint = data.get("entrypoint") or "skill.py"
        if isinstance(entrypoint, dict):
            entrypoint = entrypoint.get("module") or entrypoint.get("path") or "skill.py"
        return cls(
            id=skill_id,
            version=str(data.get("version") or "0.1.0"),
            name=str(data.get("name") or skill_id),
            domain=str(data.get("domain") or matching.get("domain") or "generic"),
            stages=[str(s) for s in stages],
            description=str(data.get("description") or ""),
            risk=str(data.get("risk") or policy.get("risk") or "low"),
            requires_approval=bool(policy.get("requires_approval", data.get("requires_approval", True))),
            safe_auto_execute=bool(policy.get("safe_auto_execute", data.get("safe_auto_execute", False))),
            min_confidence_for_heal=float(confidence.get("min_confidence_for_heal", 0.85)),
            min_confidence_for_auto_heal=float(confidence.get("min_confidence_for_auto_heal", 0.9)),
            min_evidence_sources=int(confidence.get("min_evidence_sources", 1)),
            permissions=[str(p) for p in permissions],
            allowed_tools=[str(t) for t in allowed_tools],
            tool_dependencies=[str(t) for t in tool_dependencies],
            tags=[str(t) for t in tags],
            license=str(data.get("license") or ""),
            entrypoint=str(entrypoint or "skill.py"),
            matching_keywords=[str(item) for item in matching_keywords],
            icon=str(data.get("icon") or ui.get("icon") or ""),
            category=str(data.get("category") or ui.get("category") or ""),
            source_dir=source_dir,
        )

    @classmethod
    def from_markdown(cls, frontmatter: JsonDict, source_dir: str = "") -> "SkillMetadata":
        return cls.from_yaml(frontmatter, source_dir=source_dir)

    @classmethod
    def from_sources(
        cls,
        *,
        frontmatter: JsonDict | None = None,
        yaml_data: JsonDict | None = None,
        source_dir: str = "",
    ) -> tuple["SkillMetadata", List[str]]:
        base = normalize_skill_manifest(yaml_data or {})
        markdown = normalize_skill_manifest(frontmatter or {})
        warnings: List[str] = []
        model_keys = {"name", "description", "license", "allowed_tools"}
        runtime_keys = {
            "id", "version", "domain", "stages", "stage", "risk", "permissions",
            "tool_dependencies", "tags", "policy", "confidence", "entrypoint",
            "matching", "matching_keywords", "ui", "category", "schema_version",
        }
        leaked = sorted(runtime_keys.intersection(markdown))
        if leaked:
            warnings.append(
                "SKILL.md frontmatter contains runtime manifest fields ignored: "
                + ", ".join(leaked)
            )
        merged = dict(base)
        for key in model_keys:
            value = markdown.get(key)
            if value not in (None, "", [], {}):
                merged[key] = value
        metadata = cls.from_yaml(merged, source_dir=source_dir)
        return metadata, warnings

    def to_dict(self) -> JsonDict:
        return {
            "id": self.id,
            "version": self.version,
            "name": self.name,
            "domain": self.domain,
            "stages": self.stages,
            "description": self.description,
            "risk": self.risk,
            "requires_approval": self.requires_approval,
            "safe_auto_execute": self.safe_auto_execute,
            "min_confidence_for_heal": self.min_confidence_for_heal,
            "min_confidence_for_auto_heal": self.min_confidence_for_auto_heal,
            "min_evidence_sources": self.min_evidence_sources,
            "permissions": self.permissions,
            "allowed_tools": self.allowed_tools,
            "tool_dependencies": self.tool_dependencies,
            "tags": self.tags,
            "license": self.license,
            "entrypoint": self.entrypoint,
            "matching_keywords": self.matching_keywords,
            "icon": self.icon,
            "category": self.category,
            "source_dir": self.source_dir,
        }


def normalize_skill_manifest(data: JsonDict | None) -> JsonDict:
    if not isinstance(data, dict):
        return {}
    normalized = dict(data)
    if "allowed-tools" in normalized and "allowed_tools" not in normalized:
        normalized["allowed_tools"] = normalized.get("allowed-tools")
    if "tool-dependencies" in normalized and "tool_dependencies" not in normalized:
        normalized["tool_dependencies"] = normalized.get("tool-dependencies")
    if "schema" in normalized and "schema_version" not in normalized:
        normalized["schema_version"] = normalized.get("schema")
    matching = normalized.get("matching") if isinstance(normalized.get("matching"), dict) else {}
    if matching.get("keywords") and "matching_keywords" not in normalized:
        normalized["matching_keywords"] = matching.get("keywords")
    if matching.get("domain") and "domain" not in normalized:
        normalized["domain"] = matching.get("domain")
    return normalized


def deep_merge(base: JsonDict, overlay: JsonDict) -> JsonDict:
    merged: JsonDict = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        elif value not in (None, ""):
            merged[key] = value
    return merged


@dataclass
class StageResult:
    stage: str
    status: str = "skipped"
    confidence: float = 0.0
    evidence: List[str] = field(default_factory=list)
    summary: str = ""
    output: JsonDict = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> JsonDict:
        return {
            "stage": self.stage,
            "status": self.status,
            "confidence": round(float(self.confidence or 0.0), 3),
            "evidence": list(self.evidence),
            "summary": self.summary,
            "output": self.output,
            "error": self.error,
        }


@dataclass
class SkillRun:
    run_id: str
    skill: SkillMetadata
    status: str
    stages: Dict[str, StageResult]
    policy: JsonDict
    trace: List[JsonDict]
    requested_stages: List[str] = field(default_factory=list)
    implemented_stages: List[str] = field(default_factory=list)

    def to_dict(self) -> JsonDict:
        skipped = {"skipped", "missing", "no_match"}
        executed_stages = [
            name
            for name, result in self.stages.items()
            if str(result.status or "").lower() not in skipped
        ]
        return {
            "run_id": self.run_id,
            "skill": self.skill.to_dict(),
            "status": self.status,
            "stages": {name: result.to_dict() for name, result in self.stages.items()},
            "declared_stages": list(self.skill.stages),
            "requested_stages": list(self.requested_stages),
            "implemented_stages": list(self.implemented_stages),
            "attempted_stages": list(self.stages.keys()),
            "executed_stages": executed_stages,
            "skipped_stages": [
                name
                for name, result in self.stages.items()
                if str(result.status or "").lower() in skipped
            ],
            "policy": self.policy,
            "trace": self.trace,
        }


def normalize_stage_result(stage: str, raw: Any) -> StageResult:
    if isinstance(raw, StageResult):
        return raw
    if not isinstance(raw, dict):
        return StageResult(stage=stage, status="ok" if raw else "skipped", output={"value": raw})
    evidence = raw.get("evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    return StageResult(
        stage=stage,
        status=str(raw.get("status") or ("ok" if raw else "skipped")),
        confidence=float(raw.get("confidence") or 0.0),
        evidence=[str(item) for item in evidence if item],
        summary=str(raw.get("summary") or ""),
        output={k: v for k, v in raw.items() if k not in {"status", "confidence", "evidence", "summary", "error"}},
        error=str(raw.get("error") or ""),
    )


def merge_evidence(*results: Optional[StageResult]) -> List[str]:
    seen = set()
    merged: List[str] = []
    for result in results:
        if not result:
            continue
        for item in result.evidence:
            if item not in seen:
                seen.add(item)
                merged.append(item)
    return merged
