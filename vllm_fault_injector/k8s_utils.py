"""Small Kubernetes helpers shared by fault injection and observation."""

from __future__ import annotations

from typing import Any


def build_label_selector(selector_spec: Any) -> str:
    """Convert a Kubernetes LabelSelector object into CLI selector syntax."""
    if not isinstance(selector_spec, dict):
        return ""
    parts = [
        f"{key}={value}"
        for key, value in (selector_spec.get("matchLabels") or {}).items()
        if str(key).strip() and str(value).strip()
    ]
    for expression in selector_spec.get("matchExpressions") or []:
        if not isinstance(expression, dict):
            continue
        key = str(expression.get("key") or "").strip()
        operator = str(expression.get("operator") or "").strip()
        values = [str(value) for value in expression.get("values") or [] if str(value).strip()]
        if not key:
            continue
        if operator == "In" and values:
            parts.append(f"{key} in ({','.join(values)})")
        elif operator == "NotIn" and values:
            parts.append(f"{key} notin ({','.join(values)})")
        elif operator == "Exists":
            parts.append(key)
        elif operator == "DoesNotExist":
            parts.append(f"!{key}")
    return ",".join(parts)
