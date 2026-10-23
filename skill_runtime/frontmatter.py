"""Markdown frontmatter helpers for AgenticSRE skills."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Tuple

import yaml


def split_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Split YAML frontmatter from Markdown content.

    The parser intentionally supports only the common `---` fenced YAML block
    at the beginning of the file. Invalid or non-mapping YAML is treated as an
    empty frontmatter block so that documentation remains readable.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for index in range(1, len(lines)):
        if lines[index].strip() != "---":
            continue
        raw = "\n".join(lines[1:index])
        body = "\n".join(lines[index + 1 :]).lstrip("\n")
        try:
            data = yaml.safe_load(raw) or {}
        except yaml.YAMLError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        return data, body
    return {}, text


def parse_skill_md(path: Path) -> Tuple[Dict[str, Any], str]:
    if not path.exists() or not path.is_file():
        return {}, ""
    return split_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
