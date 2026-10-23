"""Validate stage boundaries across SKILL.md, skill.yaml, and skill.py."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

import yaml


STAGE_ORDER = ("detect", "diagnose", "heal", "verify")
_STAGE_SET = set(STAGE_ORDER)
_STAGE_ITEM_RE = re.compile(
    r"^\s*(?:[-*]|\d+[.)])\s+`?(detect|diagnose|heal|verify)`?(?:\s*[:：]|\s+|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SkillStageContract:
    declared_stages: List[str]
    documented_stages: List[str]
    adapter_stages: List[str]
    errors: List[str]

    @property
    def valid(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "declared_stages": list(self.declared_stages),
            "documented_stages": list(self.documented_stages),
            "adapter_stages": list(self.adapter_stages),
            "errors": list(self.errors),
        }


def _ordered_unique(stages: Iterable[str]) -> List[str]:
    found = {str(stage).strip().lower() for stage in stages if str(stage).strip()}
    return [stage for stage in STAGE_ORDER if stage in found]


def manifest_stages(path: Path) -> List[str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = data.get("stages") or data.get("stage") or []
    if isinstance(raw, str):
        raw = [raw]
    stages = [str(stage).strip().lower() for stage in raw]
    unknown = [stage for stage in stages if stage not in _STAGE_SET]
    if unknown:
        raise ValueError(f"未知阶段: {', '.join(unknown)}")
    return _ordered_unique(stages)


def documented_stages(path: Path) -> List[str]:
    text = path.read_text(encoding="utf-8")
    in_stage_section = False
    stages: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            heading = stripped[3:].strip().lower()
            in_stage_section = heading in {"执行阶段", "stages", "execution stages"}
            continue
        if not in_stage_section:
            continue
        match = _STAGE_ITEM_RE.match(line)
        if match:
            stages.append(match.group(1).lower())
    return _ordered_unique(stages)


def adapter_stages(path: Path) -> List[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _STAGE_SET
    ]
    return _ordered_unique(names)


def evaluate_skill_stage_contract(skill_dir: Path) -> SkillStageContract:
    manifest_path = skill_dir / "skill.yaml"
    markdown_path = skill_dir / "SKILL.md"
    adapter_path = skill_dir / "skill.py"
    errors: List[str] = []

    declared = manifest_stages(manifest_path) if manifest_path.exists() else []
    documented = documented_stages(markdown_path) if markdown_path.exists() else []
    adapter = adapter_stages(adapter_path) if adapter_path.exists() else []

    if documented != declared:
        errors.append(
            "SKILL.md 阶段与 skill.yaml 不一致: "
            f"声明={declared}, 文档={documented}"
        )
    missing_adapter = [stage for stage in declared if stage not in adapter]
    if missing_adapter:
        errors.append(f"skill.py 缺少已声明阶段: {missing_adapter}")
    extra_adapter = [stage for stage in adapter if stage not in declared]
    if extra_adapter:
        errors.append(f"skill.py 包含未声明阶段: {extra_adapter}")

    return SkillStageContract(
        declared_stages=declared,
        documented_stages=documented,
        adapter_stages=adapter,
        errors=errors,
    )


def remove_undeclared_adapter_stages(skill_dir: Path) -> List[str]:
    """Remove dormant top-level stage functions that are absent from skill.yaml."""
    manifest_path = skill_dir / "skill.yaml"
    adapter_path = skill_dir / "skill.py"
    if not manifest_path.exists() or not adapter_path.exists():
        return []

    declared = set(manifest_stages(manifest_path))
    source = adapter_path.read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    tree = ast.parse(source, filename=str(adapter_path))
    removable = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in _STAGE_SET
        and node.name not in declared
    ]
    removed = [node.name for node in removable]
    for node in sorted(removable, key=lambda item: item.lineno, reverse=True):
        start = node.lineno - 1
        end = node.end_lineno or node.lineno
        while end < len(lines) and not lines[end].strip():
            end += 1
        del lines[start:end]
    if removed:
        adapter_path.write_text("".join(lines).rstrip() + "\n", encoding="utf-8")
    return _ordered_unique(removed)
