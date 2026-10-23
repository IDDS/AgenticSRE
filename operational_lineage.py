"""Bounded, read-only Kubernetes provenance collected after a diagnosis.

These snapshots are supplemental observations, not evidence reviewed by the
original RCA judge. A controller revision is not a Git commit or a release event.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from operational_provenance import PROJECTION_VERSION, metadata_declarations


RESOURCE_KINDS = {"Pod", "ReplicaSet", "Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}
NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?\Z")


def _dict(value):
    return value if isinstance(value, dict) else {}


def _rows(value):
    return value if isinstance(value, list) else []


def _text(value):
    return value if isinstance(value, str) else ""


def traceable_object(target):
    return (isinstance(target, dict) and target.get("kind") in RESOURCE_KINDS
            and all(isinstance(target.get(k), str) and target[k] for k in ("cluster_id", "uid"))
            and all(isinstance(target.get(k), str) and 0 < len(target[k]) <= 253
                    and NAME.fullmatch(target[k]) for k in ("name", "namespace")))


def _project(resource):
    """Allowlist excludes env, credentials, arbitrary annotations and messages."""
    meta, spec, status = (_dict(resource.get(k)) for k in ("metadata", "spec", "status"))
    revision = _dict(meta.get("annotations")).get("deployment.kubernetes.io/revision")
    revision = revision if isinstance(revision, str) and revision.isdigit() else None
    owners = [{k: o[k] for k in ("kind", "name", "uid", "controller") if k in o}
              for o in _rows(meta.get("ownerReferences")) if isinstance(o, dict)]
    pod_spec = spec if resource["kind"] == "Pod" else _dict(_dict(spec.get("template")).get("spec"))
    if resource["kind"] == "CronJob":
        pod_spec = _dict(_dict(_dict(_dict(spec.get("jobTemplate")).get("spec")).get("template")).get("spec"))
    containers = []
    for field, status_field, category in (("containers", "containerStatuses", "application"),
                                        ("initContainers", "initContainerStatuses", "init"),
                                        ("ephemeralContainers", "ephemeralContainerStatuses", "ephemeral")):
        for c in _rows(pod_spec.get(field)):
            if not isinstance(c, dict) or not _text(c.get("name")) or not _text(c.get("image")):
                continue
            matches = [s for s in _rows(status.get(status_field)) if isinstance(s, dict) and s.get("name") == c["name"]]
            runtime = matches[0] if len(matches) == 1 and resource["kind"] == "Pod" else {}
            containers.append({"container": c["name"], "container_kind": category,
                "declared_image": c["image"], "runtime_image": _text(runtime.get("image")) or None,
                "runtime_image_id": _text(runtime.get("imageID")) or None,
                "runtime_status": "reported" if _text(runtime.get("imageID")) else "not_reported"})
    return {"kind": resource["kind"], "metadata": {
        **{k: meta[k] for k in ("name", "namespace", "uid", "resourceVersion", "creationTimestamp") if k in meta},
        "ownerReferences": owners}, "controller_revision": revision, "containers": containers,
        "projection_version": PROJECTION_VERSION, "source_metadata": metadata_declarations(resource)}


def collect_lineage(target, read_resource, *, source_run_id, source_revision):
    """Follow only explicitly declared controlling owners, verifying every UID.

    Each object is an independent API snapshot, not a cross-resource transaction.
    read_resource accepts a typed identity and returns {ok, json}; failures never
    persist its raw output. Maximum six API reads; cycles and scope escapes stop.
    """
    if not traceable_object(target):
        raise ValueError("trace_target_identity_incomplete")
    result = {"id": "trace-" + uuid.uuid4().hex[:24], "source_run_id": source_run_id,
        "source_ontology_revision": source_revision, "target": dict(target),
        "started_at": datetime.now(timezone.utc).isoformat(), "observations": [], "relations": [],
        "gaps": [], "status": "complete", "release_trace_complete": False,
        "semantics": {"phase": "post_diagnosis", "reviewed_by_original_judge": False,
            "is_causal_proof": False, "atomic_multi_resource_snapshot": False,
            "revision_is_git_commit": False, "creation_time_is_release_time": False}}
    current, visited, previous = dict(target), set(), None
    for _ in range(6):
        key = tuple(current.get(k) for k in ("cluster_id", "namespace", "kind", "name", "uid"))
        if key in visited:
            result["gaps"].append("owner_cycle")
            break
        visited.add(key)
        try:
            response = read_resource(current)
        except Exception:
            response = {}
        resource = _dict(_dict(response).get("json"))
        meta = _dict(resource.get("metadata"))
        if _dict(response).get("ok") is not True or not resource:
            result["gaps"].append("resource_read_failed")
            break
        if resource.get("kind") != current["kind"] or any(meta.get(k) != current[k] for k in ("name", "namespace", "uid")):
            result["gaps"].append("resource_identity_changed")
            break
        if not isinstance(meta.get("resourceVersion"), str) or not meta["resourceVersion"]:
            result["gaps"].append("resource_version_missing")
            break
        projected = _project(resource)
        observed_at = datetime.now(timezone.utc).isoformat()
        obs_id = "trace-obs-" + hashlib.sha256(json.dumps([result["id"], key], sort_keys=True).encode()).hexdigest()[:24]
        obs = {"id": obs_id, "entity": dict(current), "observed_at": observed_at,
               "source": "kubernetes_api_get", "resource_version": meta["resourceVersion"],
               "projection": "identity_controller_revision_container_images", "raw_ref": projected}
        result["observations"].append(obs)
        if previous:
            result["relations"].append({"type": "controlled_by", "source_id": previous["id"],
                "target_id": obs_id, "evidence_ids": [previous["id"], obs_id],
                "verification": "owner_reference_uid_matched", "is_causal_proof": False})
        if current["kind"] in {"Deployment", "StatefulSet", "DaemonSet"}:
            result["release_trace_complete"] = True
        owners = [o for o in projected["metadata"]["ownerReferences"] if o.get("controller") is True]
        if not owners:
            if current["kind"] == "Pod":
                result["gaps"].append("pod_has_no_controlling_owner")
            break
        if len(owners) != 1:
            result["gaps"].append("ambiguous_controlling_owner")
            break
        owner = owners[0]
        current = {k: target[k] for k in ("cluster_id", "namespace")} | {
            k: owner.get(k) for k in ("kind", "name", "uid")}
        if not traceable_object(current):
            result["gaps"].append("owner_identity_incomplete_or_unsupported")
            break
        previous = obs
    else:
        result["gaps"].append("owner_depth_limit")
    if result["gaps"]:
        result["status"] = "partial" if result["observations"] else "unavailable"
    result["gaps"].append("git_and_release_events_not_connected")
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    return result
