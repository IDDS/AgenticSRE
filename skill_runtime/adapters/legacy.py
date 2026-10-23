"""Optional bridge to the current AgenticSRE implementation.

This adapter is deliberately thin. Skill packages can call it when they want to
reuse proven legacy behavior, while the runtime remains decoupled from the old
multi-agent orchestration internals.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict

import yaml


ROOT_DIR = Path(__file__).resolve().parents[2]
HEAL_RECIPES_FILE = ROOT_DIR / "configs" / "heal_recipes.yaml"


class LegacyAgenticSREAdapter:
    def normalize_heal_fault_type(self, text: str) -> str:
        try:
            from web_app.app import _normalize_heal_fault_type

            return _normalize_heal_fault_type(text)
        except Exception:
            return _fallback_normalize_heal_fault_type(text)


    def build_heal_suggestions(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            from web_app.app import _build_heal_suggestions

            return _build_heal_suggestions(payload)
        except Exception as exc:
            return _fallback_build_heal_suggestions(payload, adapter_error=str(exc))


    def calibrate_llm_fault_confidence(self, **kwargs: Any) -> tuple[float, Dict[str, Any]]:
        from skill_runtime.confidence import calibrate_llm_fault_confidence

        return calibrate_llm_fault_confidence(**kwargs)


def _load_recipes() -> list[Dict[str, Any]]:
    # Never resurrect seed recipes when the shared edited library is unreadable.
    # Propagate that failure so the runtime records a failed stage instead.
    if (ROOT_DIR / "data" / "heal_recipe_library.json").exists():
        from web_app.app import _load_heal_recipes
        return _load_heal_recipes()
    if not HEAL_RECIPES_FILE.exists():
        return []
    data = yaml.safe_load(HEAL_RECIPES_FILE.read_text(encoding="utf-8")) or {}
    recipes = data.get("recipes") if isinstance(data, dict) else []
    return [item for item in recipes if isinstance(item, dict)]


def _fallback_normalize_heal_fault_type(text: str) -> str:
    raw = str(text or "")
    lower = raw.lower()
    for recipe in _load_recipes():
        fault_type = str(recipe.get("fault_type") or "")
        if fault_type and fault_type.lower() in lower:
            return fault_type
        for signal in recipe.get("signals") or []:
            if str(signal).lower() in lower:
                return fault_type
    if any(token in lower for token in ("503", "service unavailable", "no endpoints", "endpoint")):
        return "ServiceUnavailable"
    if any(token in lower for token in ("crashloop", "back-off restarting")):
        return "PodCrashLoop"
    if any(token in lower for token in ("imagepullbackoff", "errimagepull", "pull image")):
        return "ImagePullError"
    if any(token in lower for token in ("nodenotready", "node not ready")):
        return "NodeNotReady"
    if any(token in lower for token in ("dns", "coredns", "kube-dns")):
        return "DNSResolution"
    return "Unknown"


def _extract_name(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"[A-Za-z0-9][A-Za-z0-9_.-]*", text)
    return match.group(0) if match else ""


def _materialize(
    command: str,
    *,
    namespace: str,
    pod: str,
    deployment: str,
    node: str,
    service: str,
    container: str = "",
    current_image: str = "",
    last_good_image: str = "",
) -> str:
    return (
        str(command or "")
        .replace("<namespace>", namespace)
        .replace("<pod>", pod or "<pod>")
        .replace("<deployment>", deployment or "<deployment>")
        .replace("<node>", node or "<node>")
        .replace("<service>", service or "<service>")
        .replace("<container>", container or "<container>")
        .replace("<current_image>", current_image or "<current_image>")
        .replace("<last_good_image>", last_good_image or "<last_good_image>")
    )


def _fallback_build_heal_suggestions(payload: Dict[str, Any], adapter_error: str = "") -> Dict[str, Any]:
    text = " ".join(
        str(payload.get(key, ""))
        for key in ("fault_type", "message", "root_cause", "root_cause_component", "service", "deployment", "pod", "node")
    )
    fault_type = _fallback_normalize_heal_fault_type(text)
    namespace = _extract_name(payload.get("namespace")) or "default"
    pod = _extract_name(payload.get("pod"))
    deployment = _extract_name(payload.get("deployment") or payload.get("root_cause_component"))
    node = _extract_name(payload.get("node"))
    service = _extract_name(payload.get("service") or payload.get("svc"))
    container = _extract_name(payload.get("container") or payload.get("container_name")) or "nginx"
    current_image = str(payload.get("current_image") or payload.get("bad_image") or "").strip()
    last_good_image = str(payload.get("last_good_image") or payload.get("expected_image") or "").strip()

    recipes = _load_recipes()
    recipe = next((item for item in recipes if item.get("fault_type") == fault_type), None)
    suggestions: list[Dict[str, Any]] = []
    seen_commands: set[str] = set()

    def add_suggestion(item: Dict[str, Any]) -> None:
        command = str(item.get("command") or "")
        if not command or command in seen_commands:
            return
        seen_commands.add(command)
        suggestions.append(item)

    if recipe:
        for action in recipe.get("actions") or []:
            command = _materialize(
                action.get("command", ""),
                namespace=namespace,
                pod=pod,
                deployment=deployment,
                node=node,
                service=service,
                container=container,
                current_image=current_image,
                last_good_image=last_good_image,
            )
            if "<" in command and ">" in command:
                continue
            add_suggestion(
                {
                    "step": action.get("step"),
                    "command": command,
                    "rollback_command": _materialize(
                        action.get("rollback_command", ""),
                        namespace=namespace,
                        pod=pod,
                        deployment=deployment,
                        node=node,
                        service=service,
                        container=container,
                        current_image=current_image,
                        last_good_image=last_good_image,
                    ),
                    "risk": action.get("risk") or recipe.get("risk") or "medium",
                    "source": "fallback_kb",
                    "executable": True,
                }
            )

    if fault_type == "ServiceUnavailable" and deployment:
        add_suggestion(
            {
                "step": f"恢复 Deployment {deployment} 至 1 个副本",
                "command": f"kubectl scale deployment/{deployment} -n {namespace} --replicas=1",
                "rollback_command": f"kubectl scale deployment/{deployment} -n {namespace} --replicas=0",
                "risk": "medium",
                "source": "fallback_dynamic",
                "executable": True,
            }
        )
    if fault_type == "ImagePullError" and deployment and container and last_good_image:
        add_suggestion(
            {
                "step": f"将 Deployment {deployment} 的 {container} 恢复到可用镜像",
                "command": f"kubectl set image deployment/{deployment} {container}={last_good_image} -n {namespace}",
                "rollback_command": (
                    f"kubectl set image deployment/{deployment} {container}={current_image} -n {namespace}"
                    if current_image else ""
                ),
                "risk": "medium",
                "source": "fallback_dynamic",
                "executable": True,
            }
        )
    if service:
        endpoint_check = {
            "step": f"检查 Service {service} Endpoints",
            "command": f"kubectl get endpoints {service} -n {namespace} -o wide",
            "rollback_command": "",
            "risk": "low",
            "source": "fallback_dynamic",
            "executable": True,
        }
        if endpoint_check["command"] not in seen_commands:
            suggestions.insert(0, endpoint_check)
            seen_commands.add(endpoint_check["command"])

    return {
        "fault_type": fault_type,
        "namespace": namespace,
        "target": {"pod": pod, "deployment": deployment, "node": node, "service": service},
        "recipe": {
            "fault_type": recipe.get("fault_type"),
            "risk": recipe.get("risk"),
            "requires_approval": recipe.get("requires_approval", True),
            "description": recipe.get("description"),
        }
        if recipe
        else None,
        "suggestions": suggestions,
        "blocked_templates": [],
        "capability": {
            "source": "fallback_kb",
            "legacy_adapter_error": adapter_error,
            "auto_execution_eligible": False,
            "diagnosis_required_for_real_execution": True,
        },
        "recommended_strategy": "fallback_kb" if suggestions else "none",
        "plan_source": "fallback_kb",
    }
