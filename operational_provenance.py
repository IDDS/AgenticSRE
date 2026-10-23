"""Typed metadata declarations and conservative comparisons of saved snapshots.

No Git, registry or controller network requests are made here. A copied OCI key
on a Kubernetes object is a declaration, not verified image or build provenance.
"""
from __future__ import annotations

from datetime import datetime
import re
from urllib.parse import urlsplit


PROJECTION_VERSION = "workload-provenance/v2"
OCI = "org.opencontainers.image."
SOURCE_KEYS = {OCI + key for key in ("source", "revision", "version", "created")}
LABEL_KEYS = {"app.kubernetes.io/version", "app.kubernetes.io/managed-by",
              "argocd.argoproj.io/instance", "kustomize.toolkit.fluxcd.io/name",
              "kustomize.toolkit.fluxcd.io/namespace", "helm.toolkit.fluxcd.io/name",
              "helm.toolkit.fluxcd.io/namespace"}
ANNOTATION_KEYS = SOURCE_KEYS | {"meta.helm.sh/release-name", "meta.helm.sh/release-namespace"}
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/+:-]{0,127}\Z")


def _dict(value):
    return value if isinstance(value, dict) else {}


def _rows(value):
    return value if isinstance(value, list) else []


def _time(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    except ValueError:
        return None


def _safe_value(key, value):
    if not isinstance(value, str) or not value or len(value) > 512:
        return None
    if key == OCI + "source":
        if any(ord(char) < 33 or ord(char) == 127 for char in value):
            return None
        try:
            url = urlsplit(value)
            if (url.scheme != "https" or not url.hostname or url.username is not None
                    or url.password is not None or url.query or url.fragment
                    or not re.fullmatch(r"[A-Za-z0-9.-]+", url.hostname)
                    or not re.fullmatch(r"/[A-Za-z0-9._~/-]+", url.path)):
                return None
            url.port  # Validate port syntax, without connecting to the address.
            return value
        except ValueError:
            return None
    if key == OCI + "created":
        return value if _time(value) else None
    return value if TOKEN.fullmatch(value) else None


def metadata_declarations(resource):
    """Keep an exact source path and only allowlisted, bounded safe values.

    Do not pair source from one metadata location with revision from another.
    Labels selecting a GitOps object do not verify that object's existence.
    """
    kind = resource.get("kind")
    spec = _dict(resource.get("spec"))
    locations = [("metadata", _dict(resource.get("metadata")))]
    if kind == "CronJob":
        locations.append(("spec.jobTemplate.spec.template.metadata", _dict(_dict(
            _dict(_dict(spec.get("jobTemplate")).get("spec")).get("template")).get("metadata"))))
    elif kind != "Pod":
        locations.append(("spec.template.metadata", _dict(_dict(spec.get("template")).get("metadata"))))
    claims, rejected = [], []
    for path, meta in locations:
        for field, keys in (("annotations", ANNOTATION_KEYS), ("labels", LABEL_KEYS)):
            for key, value in sorted(_dict(meta.get(field)).items()):
                if key not in keys:
                    continue
                safe = _safe_value(key, value)
                if safe is None:
                    rejected.append({"path": path + "." + field, "key": key,
                                     "reason": "unsupported_or_sensitive_value_omitted"})
                else:
                    claims.append({"path": path + "." + field, "key": key, "value": safe,
                        "verification": "kubernetes_metadata_declaration_only"})
    candidates = []
    for path, _ in locations:
        values = {c["key"]: c["value"] for c in claims if c["path"] == path + ".annotations"}
        if OCI + "source" in values and OCI + "revision" in values:
            revision = values[OCI + "revision"]
            candidates.append({"path": path + ".annotations", "source": values[OCI + "source"],
                "revision": revision, "revision_format": "full_hex_identifier" if re.fullmatch(
                    r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})", revision) else "revision_identifier",
                "verification": "declared_unverified", "image_digest_binding_verified": False})
    return {"claims": claims, "code_candidates": candidates, "rejected_fields": rejected,
            "status": "declared_unverified" if claims else "not_declared",
            "semantics": {"git_commit_verified": False, "image_provenance_verified": False,
                          "gitops_resource_verified": False, "external_requests": 0}}


def _identity(entity):
    return tuple(entity.get(k) for k in ("cluster_id", "namespace", "kind", "name", "uid"))


def _fields(observation):
    raw = _dict(observation.get("raw_ref"))
    fields = {"controller_revision": raw.get("controller_revision")}
    for container in _rows(raw.get("containers")):
        if not isinstance(container, dict):
            continue
        key = f"containers/{container.get('container_kind')}/{container.get('container')}"
        for field in ("declared_image", "runtime_image_id"):
            fields[f"{key}/{field}"] = container.get(field)
    for claim in _rows(_dict(raw.get("source_metadata")).get("claims")):
        if isinstance(claim, dict):
            fields[f"source_metadata/{claim.get('path')}/{claim.get('key')}"] = claim.get("value")
    for owner in _rows(_dict(raw.get("metadata")).get("ownerReferences")):
        if isinstance(owner, dict) and owner.get("controller") is True:
            fields[f"controlling_owner/{owner.get('kind')}/{owner.get('name')}"] = owner.get("uid")
    return fields


def compare_snapshots(before, after):
    """Changes in observations, not resource creation/deletion or causal claims."""
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise ValueError("snapshot_missing")
    if not before.get("id") or before.get("id") == after.get("id"):
        raise ValueError("distinct_snapshots_required")
    if before.get("source_run_id") != after.get("source_run_id"):
        raise ValueError("different_investigations")
    target = _dict(before.get("target"))
    if not all(target.get(k) for k in ("cluster_id", "namespace", "kind", "name", "uid")) or _identity(target) != _identity(_dict(after.get("target"))):
        raise ValueError("different_target_incarnation")
    finished, started = _time(before.get("finished_at")), _time(after.get("started_at"))
    if finished is None or started is None or started <= finished:
        raise ValueError("snapshot_time_order_unverified")
    old = {_identity(_dict(o.get("entity"))): o for o in _rows(before.get("observations")) if isinstance(o, dict)}
    new = {_identity(_dict(o.get("entity"))): o for o in _rows(after.get("observations")) if isinstance(o, dict)}
    changes, gaps = [], []
    unmatched = []
    for key in sorted(set(old) | set(new)):
        a, b = old.get(key), new.get(key)
        if not a or not b:
            unmatched.append({"entity": (a or b)["entity"], "observed_in": "before_only" if a else "after_only",
                              "evidence_id": (a or b)["id"], "meaning": "not_observed_in_other_snapshot"})
            continue
        a_fields, b_fields = _fields(a), _fields(b)
        new_projection = all(_dict(o.get("raw_ref")).get("projection_version") == PROJECTION_VERSION for o in (a,b))
        if not new_projection:
            gaps.append("legacy_source_metadata_not_comparable")
        for field in sorted(set(a_fields) | set(b_fields)):
            if field.startswith("source_metadata/") and not new_projection:
                continue
            left, right = a_fields.get(field), b_fields.get(field)
            if left == right:
                continue
            availability = left in (None, "") or right in (None, "")
            changes.append({"entity": b["entity"], "field": field, "before": left, "after": right,
                "type": "observation_availability_changed" if availability else "observed_value_changed",
                "evidence_ids": [a["id"], b["id"]], "is_causal_proof": False})
    if unmatched or before.get("status") != "complete" or after.get("status") != "complete":
        gaps.append("snapshot_coverage_differs_or_incomplete")
    return {"before_id": before["id"], "after_id": after["id"], "target": target,
        "changes": changes, "unmatched_observations": unmatched, "gaps": sorted(set(gaps)),
        "status": "observed_changes" if changes else "no_comparable_value_changes",
        "semantics": {"is_causal_proof": False, "unmatched_means_created_or_deleted": False,
                      "absence_of_changes_proves_no_release": False, "resource_version_is_ordered": False,
                      "git_commit_verified": False}}


def latest_comparisons(snapshots):
    """One reproducible latest pair per incarnation, including blocked pairs."""
    groups = {}
    for snapshot in _rows(snapshots):
        if isinstance(snapshot, dict):
            groups.setdefault(_identity(_dict(snapshot.get("target"))), []).append(snapshot)
    result = []
    for group in groups.values():
        if len(group) < 2:
            continue
        before, after = group[-2:]
        try:
            result.append(compare_snapshots(before, after))
        except ValueError as exc:
            result.append({"before_id":before.get("id"), "after_id":after.get("id"),
                           "status":"not_comparable", "reason":str(exc)})
    return result
