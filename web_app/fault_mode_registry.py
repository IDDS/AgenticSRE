"""Versioned fault-mode catalog management.

The registry deliberately keeps catalog lifecycle operations deterministic and
does not execute fault commands.  An LLM may optionally summarize or classify
catalog entries, but it never decides whether a version can be published or
rolled back; those decisions are made by schema and safety validation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml


class FaultModeRegistryError(ValueError):
    """A caller-visible catalog/version validation error."""


FAMILY_META = {
    "general": {
        "label": "通算故障模式",
        "sources": ("eval/fault_scenarios.yaml",),
    },
    "precision": {
        "label": "细粒度故障模式",
        "sources": ("configs/precision_fault_scenarios.yaml",),
    },
    "inference": {
        "label": "智算推理故障模式",
        "sources": (
            "vllm_fault_injector/scenarios.yaml",
            "eval/llm_fault_scenarios.yaml",
        ),
    },
}

_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_FAMILY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _canonical(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def _checksum(document: Dict[str, Any]) -> str:
    payload = json.dumps(_canonical(document), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_component(value: str, kind: str) -> str:
    value = str(value or "").strip()
    regex = _FAMILY_RE if kind == "family" else _VERSION_RE
    if not regex.fullmatch(value):
        raise FaultModeRegistryError(f"invalid {kind}: {value!r}")
    return value


def _deep_diff(before: Any, after: Any, path: str = "") -> List[str]:
    if type(before) is not type(after):
        return [path or "$"]
    if isinstance(before, dict):
        paths: List[str] = []
        for key in sorted(set(before) | set(after)):
            child = f"{path}.{key}" if path else str(key)
            if key not in before or key not in after:
                paths.append(child)
            else:
                paths.extend(_deep_diff(before[key], after[key], child))
        return paths
    if isinstance(before, list):
        paths: List[str] = []
        for index in range(max(len(before), len(after))):
            child = f"{path}[{index}]"
            if index >= len(before) or index >= len(after):
                paths.append(child)
            else:
                paths.extend(_deep_diff(before[index], after[index], child))
        return paths
    return [] if before == after else [path or "$" ]


def _scenario_map(document: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for scenario in document.get("scenarios", []):
        if isinstance(scenario, dict) and scenario.get("id"):
            result[str(scenario["id"])] = _canonical(scenario)
    return result


def validate_catalog(document: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the safe, command-free shape needed by the registry."""
    errors: List[str] = []
    if not isinstance(document, dict):
        return {"valid": False, "errors": ["catalog must be an object"], "scenario_count": 0}
    scenarios = document.get("scenarios")
    if not isinstance(scenarios, list):
        errors.append("scenarios must be a list")
        scenarios = []
    seen = set()
    for index, item in enumerate(scenarios):
        if not isinstance(item, dict):
            errors.append(f"scenarios[{index}] must be an object")
            continue
        sid = str(item.get("id") or "").strip()
        if not sid:
            errors.append(f"scenarios[{index}] missing id")
        elif sid in seen:
            errors.append(f"duplicate scenario id: {sid}")
        else:
            seen.add(sid)
    return {"valid": not errors, "errors": errors, "scenario_count": len(scenarios)}


def classify_scenario(scenario: Dict[str, Any]) -> str:
    """Deterministic baseline class; an LLM may refine the label later."""
    text = " ".join(
        str(scenario.get(key) or "")
        for key in ("layer", "category", "fault_type", "name", "description")
    ).lower()
    if any(token in text for token in ("gpu", "cuda", "nccl", "driver", "dcgm", "rdma", "cupti")):
        return "GPU与驱动"
    if any(token in text for token in ("network", "通信", "grpc", "http", "dns", "rdma", "endpoint")):
        return "网络与分布式通信"
    if any(token in text for token in ("syscall", "ebpf", "seccomp", "elf", "gdb", "jvm", "bytecode", "native")):
        return "系统调用与运行时"
    if any(token in text for token in ("cpu", "memory", "disk", "resource", "os", "pressure", "stress")):
        return "操作系统与资源"
    return "软件与推理运行时"


class FaultModeRegistry:
    """File-backed version registry for the three AgenticSRE fault catalogs."""

    def __init__(self, root_dir: Optional[os.PathLike[str] | str] = None, registry_dir: Optional[os.PathLike[str] | str] = None):
        self.root_dir = Path(root_dir or Path(__file__).resolve().parent)
        self.registry_dir = Path(registry_dir or self.root_dir / "data" / "fault_mode_versions")
        self.snapshot_dir = self.registry_dir / "snapshots"
        self.manifest_path = self.registry_dir / "registry.json"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)

    def _manifest(self) -> Dict[str, Any]:
        if not self.manifest_path.exists():
            return {"schema_version": "agenticsre.fault-mode-registry.v1", "active": {}, "versions": {}, "audit": []}
        try:
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                value.setdefault("active", {})
                value.setdefault("versions", {})
                value.setdefault("audit", [])
                return value
        except (OSError, json.JSONDecodeError):
            pass
        raise FaultModeRegistryError("fault-mode registry manifest is unreadable")

    def _save_manifest(self, manifest: Dict[str, Any]) -> None:
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="registry-", suffix=".json", dir=str(self.registry_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle, indent=2, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
            os.replace(temp_name, self.manifest_path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def _snapshot_path(self, family: str, version: str) -> Path:
        return self.snapshot_dir / _safe_component(family, "family") / f"{_safe_component(version, 'version')}.json"

    def _load_snapshot(self, family: str, version: str) -> Dict[str, Any]:
        path = self._snapshot_path(family, version)
        if not path.exists():
            raise FaultModeRegistryError(f"version not found: {family}/{version}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FaultModeRegistryError(f"snapshot unreadable: {family}/{version}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("document"), dict):
            raise FaultModeRegistryError(f"snapshot invalid: {family}/{version}")
        return data

    def current_document(self, family: str) -> Dict[str, Any]:
        family = _safe_component(family, "family")
        if family not in FAMILY_META:
            raise FaultModeRegistryError(f"unsupported family: {family}")
        document: Dict[str, Any] = {"scenarios": []}
        first_meta = True
        for relative in FAMILY_META[family]["sources"]:
            path = self.root_dir / relative
            if not path.exists():
                continue
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                continue
            if first_meta:
                document.update({key: copy.deepcopy(value) for key, value in data.items() if key != "scenarios"})
                first_meta = False
            scenarios = data.get("scenarios") if isinstance(data.get("scenarios"), list) else []
            document["scenarios"].extend(copy.deepcopy(item) for item in scenarios if isinstance(item, dict))
        return _canonical(document)

    def active_version(self, family: str) -> Optional[str]:
        return self._manifest().get("active", {}).get(_safe_component(family, "family"))

    def active_document(self, family: str) -> Optional[Dict[str, Any]]:
        version = self.active_version(family)
        if not version:
            return None
        return self._load_snapshot(family, version)["document"]

    def create_snapshot(
        self,
        family: str,
        *,
        version: str = "",
        parent_version: str = "",
        note: str = "",
        actor: str = "",
        document: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        family = _safe_component(family, "family")
        if family not in FAMILY_META:
            raise FaultModeRegistryError(f"unsupported family: {family}")
        document = _canonical(copy.deepcopy(document if document is not None else self.current_document(family)))
        validation = validate_catalog(document)
        if not validation["valid"]:
            raise FaultModeRegistryError("catalog validation failed: " + "; ".join(validation["errors"]))
        digest = _checksum(document)
        version = version.strip() or f"v{time.strftime('%Y%m%d%H%M%S')}-{digest[:8]}"
        version = _safe_component(version, "version")
        path = self._snapshot_path(family, version)
        manifest = self._manifest()
        existing = manifest.setdefault("versions", {}).setdefault(family, {}).get(version)
        if existing:
            if existing.get("checksum") != digest:
                raise FaultModeRegistryError(f"version already exists with different content: {family}/{version}")
            return {**existing, "status": "already_exists"}
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "family": family,
            "version": version,
            "parent_version": parent_version or self.active_version(family) or "",
            "created_at": time.time(),
            "actor": actor or "system",
            "note": note,
            "checksum": digest,
            "scenario_count": validation["scenario_count"],
            "document": document,
        }
        fd, temp_name = tempfile.mkstemp(prefix="snapshot-", suffix=".json", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        manifest["versions"].setdefault(family, {})[version] = {
            key: payload[key]
            for key in ("family", "version", "parent_version", "created_at", "actor", "note", "checksum", "scenario_count")
        }
        self._audit(manifest, "snapshot", family, version, actor, {"note": note})
        self._save_manifest(manifest)
        return {**manifest["versions"][family][version], "status": "created"}

    def list_versions(self, family: str) -> Dict[str, Any]:
        family = _safe_component(family, "family")
        if family not in FAMILY_META:
            raise FaultModeRegistryError(f"unsupported family: {family}")
        manifest = self._manifest()
        versions = list(manifest.get("versions", {}).get(family, {}).values())
        versions.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)
        current = self.current_document(family)
        recent_audit = [
            item for item in reversed(manifest.get("audit", []))
            if item.get("family") == family
        ][:20]
        return {
            "family": family,
            "label": FAMILY_META[family]["label"],
            "active_version": manifest.get("active", {}).get(family),
            "versions": versions,
            "current_checksum": _checksum(current),
            "current_scenario_count": len(current.get("scenarios", [])),
            "recent_audit": recent_audit,
        }

    def get_version(self, family: str, version: str) -> Dict[str, Any]:
        return self._load_snapshot(_safe_component(family, "family"), _safe_component(version, "version"))

    def diff(self, family: str, from_version: str, to_version: str) -> Dict[str, Any]:
        family = _safe_component(family, "family")
        before = _scenario_map(self._load_snapshot(family, from_version)["document"])
        after = _scenario_map(self._load_snapshot(family, to_version)["document"])
        added = [{"id": sid, "scenario": after[sid]} for sid in sorted(set(after) - set(before))]
        removed = [{"id": sid, "scenario": before[sid]} for sid in sorted(set(before) - set(after))]
        changed = []
        for sid in sorted(set(before) & set(after)):
            fields = _deep_diff(before[sid], after[sid])
            if fields:
                changed.append({"id": sid, "fields": fields, "before": before[sid], "after": after[sid]})
        return {
            "family": family,
            "from_version": from_version,
            "to_version": to_version,
            "summary": {"added": len(added), "removed": len(removed), "changed": len(changed)},
            "added": added,
            "removed": removed,
            "changed": changed,
        }

    def merge(
        self,
        family: str,
        base_version: str,
        incoming_version: str,
        *,
        target_version: str = "",
        resolutions: Optional[Dict[str, str]] = None,
        note: str = "",
        actor: str = "",
        commit: bool = False,
    ) -> Dict[str, Any]:
        family = _safe_component(family, "family")
        base = _scenario_map(self._load_snapshot(family, base_version)["document"])
        incoming = _scenario_map(self._load_snapshot(family, incoming_version)["document"])
        resolutions = resolutions or {}
        merged: Dict[str, Dict[str, Any]] = {}
        conflicts: List[Dict[str, Any]] = []
        for sid in sorted(set(base) | set(incoming)):
            if sid not in base:
                merged[sid] = incoming[sid]
            elif sid not in incoming:
                merged[sid] = base[sid]
            elif base[sid] == incoming[sid]:
                merged[sid] = base[sid]
            elif resolutions.get(sid) == "base":
                merged[sid] = base[sid]
            elif resolutions.get(sid) == "incoming":
                merged[sid] = incoming[sid]
            else:
                conflicts.append({"id": sid, "fields": _deep_diff(base[sid], incoming[sid]), "base": base[sid], "incoming": incoming[sid]})
        result: Dict[str, Any] = {
            "family": family,
            "base_version": base_version,
            "incoming_version": incoming_version,
            "conflicts": conflicts,
            "conflict_count": len(conflicts),
            "scenario_count": len(merged),
            "status": "conflict" if conflicts else "ready",
        }
        if conflicts or not commit:
            return result
        source = self._load_snapshot(family, base_version)["document"]
        document = copy.deepcopy(source)
        document["scenarios"] = [merged[sid] for sid in sorted(merged)]
        created = self.create_snapshot(
            family,
            version=target_version,
            parent_version=base_version,
            note=note or f"merge {base_version} + {incoming_version}",
            actor=actor,
            document=document,
        )
        manifest = self._manifest()
        self._audit(
            manifest,
            "merge",
            family,
            created["version"],
            actor,
            {"base_version": base_version, "incoming_version": incoming_version},
        )
        self._save_manifest(manifest)
        return {**result, "status": "created", "version": created["version"]}

    def activate(self, family: str, version: str, *, actor: str = "", reason: str = "") -> Dict[str, Any]:
        family = _safe_component(family, "family")
        version = _safe_component(version, "version")
        snapshot = self._load_snapshot(family, version)
        validation = validate_catalog(snapshot["document"])
        if not validation["valid"]:
            raise FaultModeRegistryError("cannot activate invalid catalog")
        manifest = self._manifest()
        previous = manifest.setdefault("active", {}).get(family)
        manifest["active"][family] = version
        self._audit(manifest, "activate", family, version, actor, {"previous_version": previous or "", "reason": reason})
        self._save_manifest(manifest)
        return {"family": family, "previous_version": previous, "active_version": version, "reason": reason}

    def analyze(self, family: str, version: str = "") -> Dict[str, Any]:
        family = _safe_component(family, "family")
        document = self._load_snapshot(family, version)["document"] if version else self.current_document(family)
        scenarios = [item for item in document.get("scenarios", []) if isinstance(item, dict)]
        classes: Dict[str, int] = {}
        risks: Dict[str, int] = {}
        methods: Dict[str, int] = {}
        for scenario in scenarios:
            label = classify_scenario(scenario)
            classes[label] = classes.get(label, 0) + 1
            risk = str(scenario.get("severity") or scenario.get("risk") or "未标注")
            risks[risk] = risks.get(risk, 0) + 1
            precision = scenario.get("precision") if isinstance(scenario.get("precision"), dict) else {}
            inject = scenario.get("inject") if isinstance(scenario.get("inject"), dict) else {}
            method = str(precision.get("method") or inject.get("method") or "未标注")
            methods[method] = methods.get(method, 0) + 1
        return {
            "family": family,
            "version": version or "working-tree",
            "scenario_count": len(scenarios),
            "class_counts": dict(sorted(classes.items())),
            "risk_counts": dict(sorted(risks.items())),
            "method_counts": dict(sorted(methods.items())),
            "classification": [
                {"id": item.get("id"), "name": item.get("name"), "class": classify_scenario(item)}
                for item in scenarios
            ],
            "llm_role": "可选语义总结/细分类；不参与版本发布、合并冲突裁决或回退决定",
        }

    @staticmethod
    def llm_input(document: Dict[str, Any], limit: int = 80) -> Dict[str, Any]:
        """Build a command-free, bounded prompt payload for optional LLM analysis."""
        items = []
        for scenario in document.get("scenarios", [])[:limit]:
            if not isinstance(scenario, dict):
                continue
            items.append({
                key: scenario.get(key)
                for key in ("id", "name", "layer", "category", "fault_type", "description", "expected_symptoms", "severity", "risk")
                if scenario.get(key) is not None
            })
        return {"scenario_count": len(document.get("scenarios", [])), "scenarios": items}

    @staticmethod
    def _audit(manifest: Dict[str, Any], action: str, family: str, version: str, actor: str, details: Dict[str, Any]) -> None:
        manifest.setdefault("audit", []).append({
            "timestamp": time.time(),
            "action": action,
            "family": family,
            "version": version,
            "actor": actor or "system",
            "details": details,
        })
        manifest["audit"] = manifest["audit"][-500:]
