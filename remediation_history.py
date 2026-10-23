"""Stable scope identifiers for recovery-outcome statistics.

Recovery success is meaningful only for a sufficiently similar action and
environment.  These helpers deliberately keep the identifiers deterministic,
compact, and free of raw command text so they can be stored in either JSON or
ChromaDB metadata.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from typing import Any, Dict, Iterable, List, Mapping, Optional


_TARGET_KEYS = (
    "deployment",
    "service",
    "pod",
    "node",
    "network_policy",
    "network_chaos",
)
_READ_ONLY_KUBECTL_VERBS = {
    "api-resources",
    "api-versions",
    "auth",
    "cluster-info",
    "describe",
    "diff",
    "explain",
    "get",
    "logs",
    "proxy",
    "top",
    "version",
    "wait",
}
_KUBECTL_GLOBAL_FLAGS_WITH_VALUE = {
    "--as",
    "--as-group",
    "--cache-dir",
    "--certificate-authority",
    "--client-certificate",
    "--client-key",
    "--cluster",
    "--context",
    "--kubeconfig",
    "--namespace",
    "--request-timeout",
    "--server",
    "--tls-server-name",
    "--token",
    "--user",
    "-n",
    "-s",
}


def _stable_hash(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return f"{prefix}:v1:{hashlib.sha256(encoded).hexdigest()[:20]}"


def canonicalize_command(command: Any) -> str:
    """Normalize harmless whitespace/quoting differences in a command."""
    text = str(command or "").strip()
    if not text:
        return ""
    try:
        return " ".join(shlex.split(text))
    except ValueError:
        return re.sub(r"\s+", " ", text)


def is_mutating_command(command: Any) -> bool:
    """Conservatively distinguish recovery actions from observation commands."""
    normalized = canonicalize_command(command)
    if not normalized:
        return False
    try:
        tokens = shlex.split(normalized)
    except ValueError:
        tokens = normalized.split()
    if not tokens:
        return False
    executable = tokens[0].rsplit("/", 1)[-1].lower()
    if executable != "kubectl":
        return True
    arguments: List[str] = []
    skip_next = False
    for token in tokens[1:]:
        if skip_next:
            skip_next = False
            continue
        lower = token.lower()
        if lower in _KUBECTL_GLOBAL_FLAGS_WITH_VALUE:
            skip_next = True
            continue
        if lower.startswith("-"):
            continue
        arguments.append(lower)
    if not arguments:
        return False
    verb = arguments[0]
    if verb == "rollout" and len(arguments) > 1:
        return arguments[1] not in {"history", "status"}
    return verb not in _READ_ONLY_KUBECTL_VERBS


def build_action_fingerprint(commands: Iterable[Any]) -> str:
    """Fingerprint ordered mutating commands; read-only checks do not split history."""
    canonical = [canonicalize_command(command) for command in commands]
    canonical = [command for command in canonical if command and is_mutating_command(command)]
    return _stable_hash("action", canonical) if canonical else ""


def build_environment_fingerprint(
    *,
    cluster_id: Any = "",
    namespace: Any = "",
    target: Optional[Mapping[str, Any]] = None,
    environment: Any = "",
    workload_version: Any = "",
) -> str:
    """Fingerprint the recovery environment without embedding credentials."""
    target = target or {}
    normalized_target = {
        key: str(target.get(key) or "").strip().lower()
        for key in _TARGET_KEYS
        if str(target.get(key) or "").strip()
    }
    payload = {
        "cluster_id": str(cluster_id or "local-default").strip().lower(),
        "namespace": str(namespace or "default").strip().lower(),
        "environment": str(environment or "").strip().lower(),
        "workload_version": str(workload_version or "").strip(),
        "target": normalized_target,
    }
    return _stable_hash("environment", payload)


def build_recovery_scope(
    *,
    commands: Iterable[Any],
    cluster_id: Any = "",
    namespace: Any = "",
    target: Optional[Mapping[str, Any]] = None,
    environment: Any = "",
    workload_version: Any = "",
) -> Dict[str, Any]:
    command_list: List[str] = [canonicalize_command(command) for command in commands]
    command_list = [command for command in command_list if command]
    mutating_commands = [command for command in command_list if is_mutating_command(command)]
    action_fingerprint = build_action_fingerprint(mutating_commands)
    environment_fingerprint = build_environment_fingerprint(
        cluster_id=cluster_id,
        namespace=namespace,
        target=target,
        environment=environment,
        workload_version=workload_version,
    )
    return {
        "scope_version": "recovery_scope_v1",
        "action_fingerprint": action_fingerprint,
        "environment_fingerprint": environment_fingerprint,
        "action_count": len(mutating_commands),
        "command_count": len(command_list),
        "scope_complete": bool(action_fingerprint and environment_fingerprint),
    }
