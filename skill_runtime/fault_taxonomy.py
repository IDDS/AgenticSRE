"""Shared fault taxonomy for skill routing and diagnosis normalization."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List

import yaml


TAXONOMY_PATH = Path(__file__).with_name("fault_taxonomy.yaml")


def _text(*items: Any) -> str:
    return " ".join(str(item or "") for item in items).lower()


def _contains_any(text: str, indicators: Iterable[Any]) -> bool:
    return any(str(token or "").lower() in text for token in indicators)


@lru_cache(maxsize=1)
def load_taxonomy() -> Dict[str, Any]:
    if not TAXONOMY_PATH.exists():
        return {}
    with TAXONOMY_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def classify_domain(query: str = "", namespace: str = "", payload: Dict[str, Any] | None = None) -> str:
    payload = payload or {}
    explicit = str(payload.get("domain") or "").strip()
    if explicit:
        return explicit
    text = _text(
        query,
        namespace,
        payload.get("fault_type"),
        payload.get("message"),
        payload.get("root_cause"),
        payload.get("host_id"),
    )
    matches: List[tuple[int, str]] = []
    for domain, spec in (load_taxonomy().get("domains") or {}).items():
        if _contains_any(text, spec.get("indicators") or []):
            matches.append((int(spec.get("priority") or 0), str(domain)))
    if not matches:
        return "k8s"
    matches.sort(reverse=True)
    return matches[0][1]


def classify_fault_type(query: str = "", domain: str = "", payload: Dict[str, Any] | None = None) -> str:
    payload = payload or {}
    explicit = str(payload.get("fault_type") or "").strip()
    if explicit:
        return explicit
    domain = domain or classify_domain(query, payload=payload)
    text = _text(
        query,
        payload.get("message"),
        payload.get("root_cause"),
        payload.get("raw_fault_type"),
        payload.get("alert_name"),
        payload.get("service"),
        payload.get("deployment"),
        payload.get("object"),
    )
    if domain == "k8s" and _contains_any(text, (
        "prometheus scrape",
        "scrape failed",
        "scrape_error",
        "metrics endpoint",
        "metricsbindaddress",
        "metrics bind",
        "/metrics",
        "servicemonitor",
        "podmonitor",
        "bound to localhost",
    )):
        return "prometheus_scrape_failed"
    if domain == "k8s" and _contains_any(text, (
        "etcdinsufficientmembers",
        "insufficient members",
        "etcd quorum",
        "quorum etcd",
        "kube-etcd",
        "etcdserver",
        "kube-apiserver",
        "kubeapiserver",
        "apiserverdown",
    )):
        return "control_plane_unhealthy"
    domain_faults = (load_taxonomy().get("fault_types") or {}).get(domain) or {}
    matches: List[tuple[int, str]] = []
    for fault_type, spec in domain_faults.items():
        if _contains_any(text, spec.get("indicators") or []):
            matches.append((int(spec.get("priority") or 0), str(fault_type)))
    if matches:
        matches.sort(reverse=True)
        return matches[0][1]
    return "endpoint_timeout" if domain == "llm" else "service_unavailable"


def classify_payload(payload: Dict[str, Any] | None, *, query: str = "", namespace: str = "") -> Dict[str, Any]:
    payload = payload or {}
    domain = classify_domain(query, namespace, payload)
    fault_type = classify_fault_type(query or str(payload.get("message") or ""), domain, payload)
    taxonomy = load_taxonomy()
    fault_spec = ((taxonomy.get("fault_types") or {}).get(domain) or {}).get(fault_type) or {}
    llm_experiment = (taxonomy.get("llm_experiment_faults") or {}).get(fault_type) or {}
    return {
        "domain": domain,
        "fault_type": fault_type,
        "fault_family": llm_experiment.get("family") or fault_spec.get("family") or "",
        "llm_experiment": llm_experiment,
    }


def skill_aliases(skill_id: str) -> List[str]:
    raw = (load_taxonomy().get("skill_aliases") or {}).get(skill_id) or []
    return [str(item).lower() for item in raw if str(item).strip()]


def llm_experiment_spec(fault_type: str) -> Dict[str, Any]:
    return dict((load_taxonomy().get("llm_experiment_faults") or {}).get(str(fault_type or "").lower()) or {})


def classify_observable_signal(class_name: str, text: str) -> Dict[str, Any]:
    """Classify an observable signal with taxonomy-configured indicators."""
    classes = load_taxonomy().get("observable_signal_classes") or {}
    entries = classes.get(class_name) or {}
    haystack = str(text or "").lower()
    default = entries.get("default") if isinstance(entries.get("default"), dict) else {}
    for name, spec in entries.items():
        if name == "default" or not isinstance(spec, dict):
            continue
        if _contains_any(haystack, spec.get("indicators") or []):
            return {"name": name, **spec}
    if default:
        return {"name": "default", **default}
    return {"name": "unknown", "label": "未知", "fault_type": ""}


def llm_experiment_fault_type_from_payload(payload: Dict[str, Any] | None) -> str:
    """Deprecated: diagnosis must not infer from fault-lab truth labels.

    Experiment metadata is reserved for offline evaluation. Agent+Skill
    diagnosis should classify from observable evidence such as endpoint health,
    Kubernetes state, Prometheus metrics, load-generation responses and traces.
    """
    return ""


def llm_experiment_spec_from_payload(payload: Dict[str, Any] | None) -> Dict[str, Any]:
    return llm_experiment_spec(llm_experiment_fault_type_from_payload(payload))


def normalize_llm_experiment_fault_type(fault_type: str) -> str:
    """Return the diagnosis-level LLM fault type for a fault-lab scenario."""
    raw = str(fault_type or "").strip()
    if not raw:
        return "fault_experiment_signal"
    spec = llm_experiment_spec(raw)
    return str(spec.get("diagnosis_fault_type") or raw)


def llm_experiment_family(fault_type: str) -> str:
    """Return the taxonomy family for an LLM fault-lab scenario."""
    return str(llm_experiment_spec(fault_type).get("family") or "")


def preferred_skill_for_payload(payload: Dict[str, Any] | None) -> str:
    classified = classify_payload(payload or {})
    if classified.get("domain") != "llm":
        return ""
    family = str(classified.get("fault_family") or "")
    routes = load_taxonomy().get("llm_family_skill_routes") or {}
    return str(routes.get(family) or "")


def is_k8s_orchestration_text(text: str) -> bool:
    domain = classify_domain(text)
    fault_type = classify_fault_type(text, domain)
    return domain == "k8s" and fault_type in {
        "scheduling_failed",
        "image_pull_error",
        "config_missing",
        "hpa_scaling_failed",
        "volume_mount_failed",
        "pod_crashloop",
        "node_not_ready",
        "oom_killed",
        "network_policy_deny",
    }
