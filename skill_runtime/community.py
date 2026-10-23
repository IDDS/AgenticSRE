"""Community skill discovery and conservative conversion adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import yaml

from .evolution import SkillDraft, _slug


@dataclass(frozen=True)
class CommunitySkillRef:
    name: str
    source: str
    identifier: str
    relative_path: str
    category: str
    trust_level: str
    description: str
    tags: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source,
            "identifier": self.identifier,
            "relative_path": self.relative_path,
            "category": self.category,
            "trust_level": self.trust_level,
            "description": self.description,
            "tags": self.tags,
        }


def _read_text(path: Path, limit: int = 20000) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[:limit]


def _description_from_markdown(text: str) -> str:
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("```"):
            continue
        lines.append(line)
        if len(" ".join(lines)) >= 260:
            break
    return " ".join(lines)[:320]


def _risk_for_category(category: str, text: str) -> str:
    joined = f"{category} {text}".lower()
    if any(token in joined for token in ("security", "devops", "mcp", "mlops", "autonomous", "docker", "terminal", "credential")):
        return "high"
    if any(token in joined for token in ("research", "migration", "communication")):
        return "medium"
    return "low"


class HermesCommunitySource:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def list(self, query: str = "", limit: int = 100) -> List[CommunitySkillRef]:
        if not self.root.exists():
            return []
        q = query.lower().strip()
        refs: List[CommunitySkillRef] = []
        for skill_file in sorted(self.root.glob("*/*/SKILL.md")):
            rel = skill_file.relative_to(self.root)
            category = rel.parts[0]
            name = rel.parts[1]
            text = _read_text(skill_file, limit=12000)
            description = _description_from_markdown(text)
            haystack = f"{category} {name} {description}".lower()
            if q and q not in haystack:
                continue
            refs.append(CommunitySkillRef(
                name=name,
                source="hermes",
                identifier=f"hermes:{category}/{name}",
                relative_path=str(rel),
                category=category,
                trust_level="community",
                description=description,
                tags=[category, name],
            ))
            if len(refs) >= limit:
                break
        return refs

    def get(self, relative_path: str) -> tuple[CommunitySkillRef, str]:
        target = (self.root / relative_path).resolve()
        if self.root not in target.parents or target.name != "SKILL.md":
            raise ValueError("community skill path must point to a SKILL.md under the configured Hermes optional-skills root")
        rel = target.relative_to(self.root)
        if len(rel.parts) < 3:
            raise ValueError("invalid Hermes skill path")
        category, name = rel.parts[0], rel.parts[1]
        text = _read_text(target)
        ref = CommunitySkillRef(
            name=name,
            source="hermes",
            identifier=f"hermes:{category}/{name}",
            relative_path=str(rel),
            category=category,
            trust_level="community",
            description=_description_from_markdown(text),
            tags=[category, name],
        )
        return ref, text

    def convert_to_draft(self, relative_path: str, *, target_domain: str = "generic") -> SkillDraft:
        ref, text = self.get(relative_path)
        skill_id = f"{target_domain}.community_{_slug(ref.source)}_{_slug(ref.category)}_{_slug(ref.name)}"
        risk = _risk_for_category(ref.category, text)
        manifest = {
            "id": skill_id,
            "version": "0.1.0-community-draft",
            "name": f"Community import: {ref.name}",
            "domain": target_domain,
            "description": f"社区 skill 转换草案，来源 {ref.identifier}；需人工适配为 SRE 检测/诊断/自愈闭环。",
            "stages": ["detect", "diagnose", "heal", "verify"],
            "risk": risk,
            "permissions": ["review.only"],
            "tags": ["community", ref.source, ref.category, ref.name],
            "policy": {
                "requires_approval": True,
                "safe_auto_execute": False,
            },
            "confidence": {
                "min_evidence_sources": 2,
                "min_confidence_for_heal": 0.85,
                "min_confidence_for_auto_heal": 0.9,
            },
            "community_source": ref.to_dict(),
        }
        safe_description = ref.description.replace('"""', "'''")
        skill_py = f'''from __future__ import annotations

COMMUNITY_SOURCE = {ref.to_dict()!r}
COMMUNITY_DESCRIPTION = """{safe_description}"""


def detect(context):
    return {{
        "status": "skipped",
        "confidence": 0.0,
        "summary": "社区 skill 草案默认不参与自动检测，需人工补充 SRE 场景匹配逻辑。",
        "evidence": [],
    }}


def diagnose(context):
    return {{
        "status": "blocked",
        "confidence": 0.0,
        "summary": "社区 skill 已转换为候选草案，但尚未适配 AgenticSRE 证据链和根因输出。",
        "evidence": ["community skill requires review"],
        "community_source": COMMUNITY_SOURCE,
        "community_description": COMMUNITY_DESCRIPTION,
    }}


def heal(context):
    return {{
        "status": "blocked",
        "confidence": 0.0,
        "summary": "社区 skill 草案禁止生成真实自愈动作，需人工定义 dry-run、审批、回滚和验证。",
        "evidence": ["community skill heal disabled by default"],
        "suggestions": [],
    }}


def verify(context):
    return {{
        "status": "blocked",
        "confidence": 0.0,
        "summary": "社区 skill 草案需要人工补充验证计划。",
        "evidence": ["community skill verify disabled by default"],
        "checks": [],
    }}
'''
        return SkillDraft(
            skill_id=skill_id,
            directory=skill_id.replace(".", "_"),
            manifest=manifest,
            skill_py=skill_py,
            tests=[
                "人工确认许可证、来源可信度和依赖风险",
                "补充 detect 场景命中/误命中测试",
                "补充 diagnose 证据链、置信度和中文输出测试",
                "补充 heal dry-run、审批、回滚和高风险阻断测试",
                "补充 verify 恢复判定测试",
            ],
            provenance={
                "source": ref.source,
                "identifier": ref.identifier,
                "relative_path": ref.relative_path,
                "trust_level": ref.trust_level,
            },
            safety_notes=[
                "社区 skill 只进入候选库，不自动启用。",
                "默认权限为 review.only，safe_auto_execute=false。",
                "必须完成安全扫描、适配测试和人工评审后才能发布。",
            ],
        )


class LocalCommunityCatalog:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def list_sources(self) -> List[Dict[str, Any]]:
        if not self.root.exists():
            return []
        sources: List[Dict[str, Any]] = []
        for path in sorted(self.root.glob("*/*.yaml")):
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            data["catalog_path"] = str(path.relative_to(self.root))
            sources.append(data)
        return sources

    def get_source(self, catalog_path: str) -> Dict[str, Any]:
        target = (self.root / catalog_path).resolve()
        if self.root not in target.parents or target.suffix not in {".yaml", ".yml"}:
            raise ValueError("invalid local community source path")
        with target.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        data["catalog_path"] = str(target.relative_to(self.root))
        return data

    def convert_local_skill(self, catalog_path: str, *, target_domain: str = "generic") -> SkillDraft:
        source = self.get_source(catalog_path)
        local_path = source.get("local_path")
        if not local_path:
            raise ValueError("community source does not define local_path")
        raw_path = Path(str(local_path)).expanduser()
        skill_path = raw_path.resolve() if raw_path.is_absolute() else (self.root / raw_path).resolve()
        if not skill_path.exists() or skill_path.name != "SKILL.md":
            raise FileNotFoundError(f"local SKILL.md not found: {skill_path}")
        content = _read_text(skill_path, limit=200_000)
        return convert_markdown_skill_to_draft(
            content=content,
            source=str(source.get("homepage") or source.get("source") or catalog_path),
            target_domain=target_domain,
        )


def _frontmatter_name(text: str, fallback: str) -> tuple[str, str]:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            meta_text = parts[1]
            body = parts[2]
            name = fallback
            description = ""
            for line in meta_text.splitlines():
                if line.strip().startswith("name:"):
                    name = line.split(":", 1)[1].strip().strip('"\'') or fallback
                elif line.strip().startswith("description:"):
                    description = line.split(":", 1)[1].strip().strip('"\'')
            return name, description or _description_from_markdown(body)
    return fallback, _description_from_markdown(text)


def _allowed_skill_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"}:
        raise ValueError("Only http(s) skill URLs are supported")
    allowed_raw_prefixes = (
        "/anthropics/skills/",
        "/aliyun/alibabacloud-aiops-skills/",
    )
    allowed_github_blob_prefixes = (
        "/anthropics/skills/blob/",
        "/aliyun/alibabacloud-aiops-skills/blob/",
    )
    if parsed.netloc == "raw.githubusercontent.com":
        if not any(parsed.path.startswith(prefix) for prefix in allowed_raw_prefixes):
            raise ValueError("Only allowlisted skill repositories may be fetched")
        return url
    if parsed.netloc == "github.com" and any(parsed.path.startswith(prefix) for prefix in allowed_github_blob_prefixes):
        raw_path = parsed.path.replace("/blob/", "/", 1)
        return f"https://raw.githubusercontent.com{raw_path}"
    raise ValueError("Only Anthropic skills and Alibaba Cloud AIOps skills GitHub URLs are allowlisted for direct fetch")


def fetch_allowlisted_skill_markdown(url: str, timeout: float = 15.0) -> str:
    raw_url = _allowed_skill_url(url)
    request = Request(raw_url, headers={"User-Agent": "AgenticSRE-SkillRuntime/0.1"})
    with urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get("content-type", "")
        if "text" not in content_type and "octet-stream" not in content_type:
            raise ValueError(f"Unexpected content type: {content_type}")
        data = response.read(200_000)
    return data.decode("utf-8", errors="replace")


def convert_markdown_skill_to_draft(
    *,
    content: str,
    source: str,
    target_domain: str = "generic",
) -> SkillDraft:
    name, description = _frontmatter_name(content, fallback="community-skill")
    skill_id = f"{target_domain}.community_markdown_{_slug(name)}"
    risk = _risk_for_category(target_domain, content)
    manifest = {
        "id": skill_id,
        "version": "0.1.0-community-draft",
        "name": f"Community markdown import: {name}",
        "domain": target_domain,
        "description": f"外部 SKILL.md 转换草案，来源 {source}；需人工适配为 AgenticSRE 故障闭环。",
        "stages": ["detect", "diagnose", "heal", "verify"],
        "risk": risk,
        "permissions": ["review.only"],
        "tags": ["community", "markdown", _slug(name)],
        "policy": {
            "requires_approval": True,
            "safe_auto_execute": False,
        },
        "confidence": {
            "min_evidence_sources": 2,
            "min_confidence_for_heal": 0.85,
            "min_confidence_for_auto_heal": 0.9,
        },
        "community_source": {
            "source": source,
            "name": name,
            "description": description,
        },
    }
    safe_description = description.replace('"""', "'''")
    skill_py = f'''from __future__ import annotations

COMMUNITY_SOURCE = {manifest["community_source"]!r}
COMMUNITY_DESCRIPTION = """{safe_description}"""


def detect(context):
    return {{
        "status": "skipped",
        "confidence": 0.0,
        "summary": "外部 SKILL.md 草案默认不参与自动检测，需人工补充 AgenticSRE 场景匹配逻辑。",
        "evidence": [],
    }}


def diagnose(context):
    return {{
        "status": "blocked",
        "confidence": 0.0,
        "summary": "外部 SKILL.md 已转换为候选草案，但尚未适配 SRE 证据链、根因和置信度。",
        "evidence": ["external SKILL.md requires review"],
        "community_source": COMMUNITY_SOURCE,
        "community_description": COMMUNITY_DESCRIPTION,
    }}


def heal(context):
    return {{
        "status": "blocked",
        "confidence": 0.0,
        "summary": "外部 SKILL.md 草案禁止生成真实自愈动作，需人工定义 dry-run、审批、回滚和验证。",
        "evidence": ["external SKILL.md heal disabled by default"],
        "suggestions": [],
    }}


def verify(context):
    return {{
        "status": "blocked",
        "confidence": 0.0,
        "summary": "外部 SKILL.md 草案需要人工补充验证计划。",
        "evidence": ["external SKILL.md verify disabled by default"],
        "checks": [],
    }}
'''
    return SkillDraft(
        skill_id=skill_id,
        directory=skill_id.replace(".", "_"),
        manifest=manifest,
        skill_py=skill_py,
        tests=[
            "确认原始 SKILL.md 的许可证、来源和依赖风险",
            "按 AgenticSRE schema 补齐 detect/diagnose/heal/verify 测试",
            "对 should-trigger / should-not-trigger 查询做触发测试",
            "对真实故障样本做 dry-run 和前向测试",
        ],
        provenance={"source": source, "format": "SKILL.md"},
        safety_notes=[
            "外部 SKILL.md 只转换为候选草案，不自动启用。",
            "默认权限 review.only，requires_approval=true，safe_auto_execute=false。",
            "URL 拉取仅允许 Anthropic skills 仓库；其他社区来源应先镜像到受控目录再转换。",
        ],
    )
