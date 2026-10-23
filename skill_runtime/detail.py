"""Review-only Skill documentation and source helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .frontmatter import parse_skill_md
from .stage_contract import evaluate_skill_stage_contract


DEFAULT_SOURCE_MAX_CHARS = 300_000
REQUIRED_SKILL_MD_SECTIONS = {
    "适用场景": ("适用场景", "when to use", "use cases", "purpose"),
    "输入信号": ("输入信号", "inputs", "input signals", "evidence", "evidence model"),
    "执行阶段": ("执行阶段", "workflow", "stages", "procedure"),
    "风险与门禁": ("风险与门禁", "risk", "approval", "policy", "safety", "safety rules"),
    "验证方式": ("验证方式", "verify", "verification", "validation", "validation checklist", "output contract"),
}


def skill_markdown_from_metadata(metadata: Dict[str, Any]) -> str:
    """Build a readable SKILL.md-style document when no Markdown file exists."""
    stages = metadata.get("stages") if isinstance(metadata.get("stages"), list) else []
    permissions = metadata.get("permissions") if isinstance(metadata.get("permissions"), list) else []
    dependencies = metadata.get("tool_dependencies") if isinstance(metadata.get("tool_dependencies"), list) else []
    tags = metadata.get("tags") if isinstance(metadata.get("tags"), list) else []
    lines = [
        f"# {metadata.get('name') or metadata.get('id') or 'Skill'}",
        "",
        metadata.get("description") or "该 Skill 尚未提供独立 Markdown 说明，以下内容由 skill.yaml 元数据生成。",
        "",
        "## 基本信息",
        "",
        f"- ID: `{metadata.get('id') or '-'}`",
        f"- 版本: `{metadata.get('version') or '-'}`",
        f"- 领域: `{metadata.get('domain') or 'generic'}`",
        f"- 阶段: {', '.join(f'`{item}`' for item in stages) if stages else '-'}",
        f"- 风险: `{metadata.get('risk') or 'medium'}`",
        f"- 需要审批: {'是' if metadata.get('requires_approval', True) else '否'}",
        f"- 可自动执行: {'是' if metadata.get('safe_auto_execute') else '否'}",
        "",
        "## 置信度与门禁",
        "",
        f"- 进入自愈门禁的最低诊断置信度: `{metadata.get('min_confidence_for_heal', '-')}`",
        f"- 进入全自动门禁的最低诊断置信度: `{metadata.get('min_confidence_for_auto_heal', '-')}`",
        f"- 最少证据源: `{metadata.get('min_evidence_sources', '-')}`",
        "",
        "## 运行依赖",
        "",
        f"- 权限: {', '.join(f'`{item}`' for item in permissions) if permissions else '无额外权限'}",
        f"- 工具依赖: {', '.join(f'`{item}`' for item in dependencies) if dependencies else '无额外工具依赖'}",
        f"- 标签: {', '.join(f'`{item}`' for item in tags) if tags else '-'}",
        "",
        "## 使用方式",
        "",
        "该 Skill 仅由 AgenticSRE Skill Runtime 按元数据中声明的阶段调用；未声明阶段不属于其能力边界。",
        "生产告警和智算事件建议从对应事件中心触发；本页面用于查看能力边界、源码和审计信息。",
    ]
    return "\n".join(lines)


def read_skill_source_file(skill_dir: Path, name: str, max_chars: int = DEFAULT_SOURCE_MAX_CHARS) -> str:
    path = skill_dir / name
    if not path.exists() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[:max_chars]


def _markdown_headings(text: str) -> List[str]:
    headings = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        headings.append(stripped.lstrip("#").strip().lower())
    return headings


def _has_required_section(text: str, aliases: tuple[str, ...]) -> bool:
    lowered = text.lower()
    headings = _markdown_headings(text)
    for alias in aliases:
        needle = alias.lower()
        if any(needle in heading for heading in headings):
            return True
    return any(alias.lower() in lowered for alias in aliases)


def skill_source_status(definition: Any) -> Dict[str, Any]:
    """Return lightweight source/documentation status for catalog cards."""
    metadata = definition.metadata.to_dict()
    skill_dir = Path(definition.metadata.source_dir or "")
    markdown_path = skill_dir / "SKILL.md"
    yaml_path = skill_dir / "skill.yaml"
    python_path = skill_dir / "skill.py"
    frontmatter, _body = parse_skill_md(markdown_path)
    warnings = list(getattr(definition, "warnings", []) or [])
    return {
        "markdown_source": "SKILL.md" if markdown_path.exists() else "generated_from_skill_yaml",
        "source_of_truth": "SKILL.md frontmatter" if frontmatter else "skill.yaml",
        "has_markdown": markdown_path.exists(),
        "has_frontmatter": bool(frontmatter),
        "has_yaml": yaml_path.exists(),
        "has_python": python_path.exists(),
        "entrypoint": metadata.get("entrypoint") or "skill.py",
        "metadata_warnings": warnings,
        "source_file_count": sum(1 for path in (markdown_path, yaml_path, python_path) if path.exists()),
        "source_dir": str(skill_dir),
        "documentation_status": "complete" if markdown_path.exists() else "metadata_generated",
        "documentation_label": "完整说明" if markdown_path.exists() else "元数据生成",
        "id": metadata.get("id"),
    }


def skill_contract_status(definition: Any, markdown: str = "") -> Dict[str, Any]:
    """Evaluate whether a runtime skill follows a Markdown-first open skill contract."""
    metadata = definition.metadata.to_dict()
    skill_dir = Path(definition.metadata.source_dir or "")
    markdown_path = skill_dir / "SKILL.md"
    yaml_path = skill_dir / "skill.yaml"
    python_path = skill_dir / "skill.py"
    text = markdown or read_skill_source_file(skill_dir, "SKILL.md", max_chars=80_000)
    frontmatter, _body = parse_skill_md(markdown_path)
    warnings = list(getattr(definition, "warnings", []) or [])
    stages = metadata.get("stages") if isinstance(metadata.get("stages"), list) else []
    stage_contract = evaluate_skill_stage_contract(skill_dir)
    missing_sections: List[str] = []
    for section, aliases in REQUIRED_SKILL_MD_SECTIONS.items():
        if not _has_required_section(text, aliases):
            missing_sections.append(section)
    entrypoint = metadata.get("entrypoint") or "skill.py"
    entrypoint_path = skill_dir / entrypoint
    missing_files = []
    if not markdown_path.exists():
        missing_files.append("SKILL.md")
    if not yaml_path.exists() and not frontmatter:
        missing_files.append("skill.yaml or SKILL.md frontmatter")
    if not entrypoint_path.exists():
        missing_files.append(entrypoint)
    missing_metadata = [
        key for key in ("id", "name", "domain", "description", "risk")
        if not metadata.get(key)
    ]
    if not stages:
        missing_metadata.append("stages")
    has_runtime_adapter = bool(getattr(definition, "module", None)) or entrypoint_path.exists()
    has_markdown_contract = markdown_path.exists() and not missing_sections
    has_policy = bool(metadata.get("permissions") is not None and metadata.get("min_confidence_for_heal") is not None)
    is_complete = (
        not missing_files
        and not missing_metadata
        and has_markdown_contract
        and has_runtime_adapter
        and has_policy
        and stage_contract.valid
    )
    if is_complete:
        maturity = "complete"
        label = "完整 Skill"
    elif markdown_path.exists() and python_path.exists():
        maturity = "runtime_with_partial_contract"
        label = "运行可用，契约待补全"
    elif python_path.exists():
        maturity = "runtime_only"
        label = "仅运行时 Skill"
    else:
        maturity = "draft"
        label = "草案"
    recommendations = []
    if not markdown_path.exists():
        recommendations.append("补充 Markdown-first 的 SKILL.md，把适用场景、输入信号、流程、风险和验证写成可审查契约。")
    if missing_sections:
        recommendations.append(f"补齐 SKILL.md 章节: {', '.join(missing_sections)}。")
    if not has_runtime_adapter:
        recommendations.append("补充 skill.py 或 scripts/ 适配器，保持运行逻辑可测试、可 dry-run。")
    if not has_policy:
        recommendations.append("补充权限、风险、置信度和审批门禁。")
    if not stage_contract.valid:
        recommendations.append("同步 SKILL.md、skill.yaml 与 skill.py 的阶段声明。")
    return {
        "schema_version": "agenticsre.skill_contract.v1",
        "style": "markdown_first_runtime_adapter",
        "source_of_truth": "SKILL.md frontmatter" if frontmatter else "skill.yaml",
        "maturity": maturity,
        "label": label,
        "is_complete": is_complete,
        "has_frontmatter": bool(frontmatter),
        "has_markdown_contract": has_markdown_contract,
        "has_runtime_adapter": has_runtime_adapter,
        "has_policy": has_policy,
        "stage_contract": stage_contract.to_dict(),
        "entrypoint": entrypoint,
        "metadata_warnings": warnings,
        "missing_files": missing_files,
        "missing_metadata": missing_metadata,
        "missing_sections": missing_sections,
        "recommended_layout": ["SKILL.md", "skill.yaml", "skill.py", "references/", "scripts/"],
        "recommendations": recommendations,
    }


def skill_catalog_item_from_definition(definition: Any) -> Dict[str, Any]:
    item = definition.metadata.to_dict()
    item["source_status"] = skill_source_status(definition)
    item["contract_status"] = skill_contract_status(definition)
    return item


def _source_file_info(skill_dir: Path, name: str, content: str) -> Dict[str, Any]:
    path = skill_dir / name
    exists = path.exists() and path.is_file()
    size = path.stat().st_size if exists else 0
    return {
        "name": name,
        "exists": exists,
        "size_bytes": size,
        "read_chars": len(content or ""),
        "truncated": bool(exists and size > DEFAULT_SOURCE_MAX_CHARS),
    }


def skill_detail_from_definition(definition: Any) -> Dict[str, Any]:
    metadata = definition.metadata.to_dict()
    skill_dir = Path(definition.metadata.source_dir or "")
    if not skill_dir.exists():
        raise FileNotFoundError(f"Skill source directory not found: {definition.metadata.id}")

    frontmatter, markdown = parse_skill_md(skill_dir / "SKILL.md")
    markdown_source = "SKILL.md"
    if not markdown:
        markdown = skill_markdown_from_metadata(metadata)
        markdown_source = "generated_from_skill_yaml"
    yaml_text = read_skill_source_file(skill_dir, "skill.yaml")
    python_text = read_skill_source_file(skill_dir, "skill.py")
    source_status = skill_source_status(definition)
    contract_status = skill_contract_status(definition, markdown if markdown_source == "SKILL.md" else "")
    source_status["truncated"] = any(
        _source_file_info(skill_dir, name, content)["truncated"]
        for name, content in (
            ("SKILL.md", markdown if markdown_source == "SKILL.md" else ""),
            ("skill.yaml", yaml_text),
            ("skill.py", python_text),
        )
    )

    return {
        "metadata": metadata,
        "frontmatter": frontmatter,
        "markdown": markdown,
        "markdown_source": markdown_source,
        "yaml": yaml_text,
        "python": python_text,
        "source_status": source_status,
        "contract_status": contract_status,
        "source_files": {
            "markdown": _source_file_info(skill_dir, "SKILL.md", markdown if markdown_source == "SKILL.md" else ""),
            "yaml": _source_file_info(skill_dir, "skill.yaml", yaml_text),
            "python": _source_file_info(skill_dir, "skill.py", python_text),
        },
        "files": {
            "skill_dir": str(skill_dir),
            "markdown": "SKILL.md" if (skill_dir / "SKILL.md").exists() else "",
            "yaml": "skill.yaml" if (skill_dir / "skill.yaml").exists() else "",
            "python": "skill.py" if (skill_dir / "skill.py").exists() else "",
        },
    }
