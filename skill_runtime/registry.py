"""Skill discovery and module loading."""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Dict, Iterable, List, Optional

import yaml

from .frontmatter import parse_skill_md
from .schemas import SkillMetadata


@dataclass(frozen=True)
class SkillDefinition:
    metadata: SkillMetadata
    module: Optional[ModuleType]
    warnings: List[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.metadata.id


class SkillRegistry:
    def __init__(self, skills_dir: str | Path):
        self.skills_dir = Path(skills_dir)
        self._skills: Dict[str, SkillDefinition] = {}

    def load(self) -> "SkillRegistry":
        self._clear_loaded_modules()
        self._skills = {}
        if not self.skills_dir.exists():
            return self
        skill_dirs = {
            path.parent
            for pattern in ("*/skill.yaml", "*/SKILL.md")
            for path in self.skills_dir.glob(pattern)
        }
        for skill_dir in sorted(skill_dirs):
            skill_yaml = skill_dir / "skill.yaml"
            skill_md = skill_dir / "SKILL.md"
            yaml_data = {}
            if skill_yaml.exists():
                with skill_yaml.open("r", encoding="utf-8") as fh:
                    yaml_data = yaml.safe_load(fh) or {}
            frontmatter, _body = parse_skill_md(skill_md)
            metadata, warnings = SkillMetadata.from_sources(
                frontmatter=frontmatter,
                yaml_data=yaml_data,
                source_dir=str(skill_dir),
            )
            try:
                module = self._load_entrypoint(metadata, skill_dir, warnings)
            except Exception as exc:  # pragma: no cover - defensive registry isolation
                warnings.append(f"failed to load entrypoint: {type(exc).__name__}: {exc}")
                module = None
            self._skills[metadata.id] = SkillDefinition(metadata=metadata, module=module, warnings=warnings)
        return self

    def _clear_loaded_modules(self) -> None:
        for name in [key for key in sys.modules if key.startswith("skill_runtime.loaded.")]:
            sys.modules.pop(name, None)

    def _load_entrypoint(self, metadata: SkillMetadata, skill_dir: Path, warnings: List[str]) -> Optional[ModuleType]:
        entrypoint = metadata.entrypoint or "skill.py"
        entrypoint_path = Path(entrypoint)
        if entrypoint_path.is_absolute() or ".." in entrypoint_path.parts:
            warnings.append(f"unsafe entrypoint ignored: {entrypoint}")
            return None
        path = skill_dir / entrypoint_path
        if not path.exists():
            warnings.append(f"executable adapter not found: {entrypoint}")
            return None
        if path.suffix != ".py":
            warnings.append(f"unsupported entrypoint type ignored: {entrypoint}")
            return None
        return self._load_module(metadata.id, path)

    def _load_module(self, skill_id: str, path: Path) -> ModuleType:
        if not path.exists():
            raise FileNotFoundError(f"Skill {skill_id} is missing executable adapter: {path.name}")
        module_name = f"skill_runtime.loaded.{skill_id.replace('.', '_').replace('-', '_')}_{abs(hash(str(path.resolve())))}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if not spec or not spec.loader:
            raise ImportError(f"Cannot load skill module: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    def list(self) -> List[SkillDefinition]:
        return list(self._skills.values())

    def ids(self) -> List[str]:
        return sorted(self._skills)

    def get(self, skill_id: str) -> SkillDefinition:
        if skill_id not in self._skills:
            raise KeyError(f"Skill not found: {skill_id}")
        return self._skills[skill_id]

    def matching(self, stages: Iterable[str] | None = None, domain: str = "") -> List[SkillDefinition]:
        requested = {str(s) for s in stages or []}
        items = self.list()
        if domain:
            items = [item for item in items if item.metadata.domain in {domain, "generic"}]
        if requested:
            items = [item for item in items if requested.intersection(item.metadata.stages)]
        return items

    def search(
        self,
        text: str,
        *,
        stages: Iterable[str] | None = None,
        domain: str = "",
        limit: int = 10,
    ) -> List[SkillDefinition]:
        """Return candidate skills ranked by manifest keywords and description.

        This is intentionally lightweight. Executable skills still make the
        final detect/diagnose decision, but Agent and UI callers can use this
        method to present a standard skill catalog/search surface.
        """
        query = str(text or "").lower()
        candidates = self.matching(stages=stages, domain=domain)

        def score(item: SkillDefinition) -> tuple[int, str]:
            metadata = item.metadata
            value = 0
            haystack = " ".join([
                metadata.id,
                metadata.name,
                metadata.description,
                " ".join(metadata.tags),
            ]).lower()
            if query and query in haystack:
                value += 3
            for keyword in metadata.matching_keywords:
                token = str(keyword or "").lower()
                if token and token in query:
                    value += 5
                elif query and token and query in token:
                    value += 2
            return (value, metadata.id)

        ranked = sorted(candidates, key=score, reverse=True)
        if query:
            positive = [item for item in ranked if score(item)[0] > 0]
            if positive:
                ranked = positive
        return ranked[: max(1, int(limit or 10))]
