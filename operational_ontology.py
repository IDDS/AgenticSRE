"""A run-scoped operational ontology over observed data, never inferred causes.

Logical identities and links are independent of storage. Unknown identity, time,
and failed tool outcomes remain explicit. This is an internal semantic contract;
it does not require or connect to a Palantir or Alibaba service.
"""
from __future__ import annotations

import hashlib
import json
import shlex
import copy
import yaml
from typing import Any


SCHEMA_VERSION = "operational-ontology/v1"
CLUSTER_SCOPED = {"Node", "Namespace", "PersistentVolume", "Cluster", "Image"}
KINDS = {k.lower(): k for k in (
    "Pod", "Node", "Deployment", "ReplicaSet", "StatefulSet", "DaemonSet",
    "Service", "Endpoints", "Job", "CronJob", "Namespace", "PersistentVolume",
    "PersistentVolumeClaim", "Cluster", "Image", "Host", "Application",
)}


def ontology_schema() -> dict:
    """Capabilities, not a claim that every object or relation was observed."""
    return {"schema_version": SCHEMA_VERSION, "object_types": sorted(set(KINDS.values())),
            "identity_fields": ["cluster_id", "namespace", "kind", "name", "uid"],
            "relation_types": {"owned_by": {"source": "metadata.ownerReferences"},
                               "scheduled_on": {"source": "spec.nodeName"}},
            "action_types": [
                {"id": "scale", "object_types": ["Deployment"], "parameter": "replicas"},
                {"id": "restart", "object_types": ["Deployment"]},
                {"id": "set_image", "object_types": ["Deployment"], "parameter": "container_images"}],
            "action_contract": {"conditions": ["run_revision", "persisted_dry_run_plan", "independent_review",
                                                "existing_execution_policy", "uid", "resource_version"],
                                "atomicity": "one_kubernetes_object_json_patch",
                                "unknown_outcome": "inspect_before_any_new_proposal",
                                "rollback": "new_evidence_and_new_proposal_required"},
            "query_collections": ["objects", "observations", "relations", "tool_executions"],
            "relation_is_causation": False}


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _rows(value: Any) -> list:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _hash(prefix: str, value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return prefix + hashlib.sha256(data.encode()).hexdigest()[:24]


def observation_id(item: dict) -> str:
    """Shared reference for a recorded observation; collection attempts stay distinct."""
    canonical = {k: item.get(k) for k in ("signal_type", "source", "source_agent", "observation", "timestamp", "raw_ref",
                                         "collection_attempt", "iteration", "collected_at")}
    return _hash("obs-", canonical)


def observation_entity(item: dict, *, cluster_id: str = "", namespace: str = "") -> dict | None:
    """Resolve only explicit identities and typed signal contracts, not prose.

    Name-only resources cannot merge with a UID-bearing incarnation. Run scope
    is a fallback, never a replacement for explicit source scope.
    """
    raw = _dict(item.get("raw_ref"))
    meta = _dict(raw.get("metadata"))
    involved = _dict(raw.get("involvedObject")) or _dict(raw.get("regarding"))
    if involved and not all(_text(involved.get(k)) for k in ("kind", "name")):
        return None
    signal = _text(item.get("signal_type"))
    kind = _text(involved.get("kind")) or _text(raw.get("kind"))
    name = _text(involved.get("name")) or _text(meta.get("name")) or _text(raw.get("name"))
    if not kind:
        if signal == "service_endpoints_empty":
            kind = "Endpoints"
        elif signal.startswith(("pod_", "container_")) or raw.get("pod"):
            kind = "Pod"
        elif signal.startswith("deployment_") or raw.get("deployment"):
            kind = "Deployment"
    if not name:
        name = _text(raw.get("pod")) or _text(raw.get("deployment"))
    # A metric's display name is not an object identity. Labels are evidence.
    labels = _dict(raw.get("labels"))
    if signal == "metric_series_observed" and not kind:
        for field, resource_kind in (("pod", "Pod"), ("service", "Service"), ("node", "Node")):
            if _text(labels.get(field)):
                name, kind = labels[field], resource_kind
                break
    kind = KINDS.get(kind.casefold(), kind)
    if not kind or not name:
        return None
    cluster = _text(raw.get("cluster_id")) or _text(labels.get("cluster_id")) or cluster_id
    ns = (_text(involved.get("namespace")) or _text(meta.get("namespace"))
          or _text(labels.get("namespace")) or _text(raw.get("namespace")) or namespace)
    if kind in CLUSTER_SCOPED:
        ns = ""
    uid = (_text(involved.get("uid")) or _text(meta.get("uid"))
           or (_text(raw.get("pod_uid")) if kind == "Pod" else "") or _text(raw.get("uid")))
    # A raw event UID identifies the Event, not the referenced Pod.
    if involved:
        uid = _text(involved.get("uid"))
    identity = {"cluster_id": cluster, "namespace": ns, "kind": kind, "name": name, "uid": uid}
    return {"id": _hash("obj-", identity), **identity,
            "identity_status": "uid_bound" if uid and cluster and (ns or kind in CLUSTER_SCOPED)
            else "name_bound" if cluster and (ns or kind in CLUSTER_SCOPED) else "scope_incomplete"}


def observation_semantics(item: dict, *, cluster_id: str = "", namespace: str = "") -> dict:
    raw = _dict(item.get("raw_ref"))
    signal = _text(item.get("signal_type"))
    return {"entity": observation_entity(item, cluster_id=cluster_id, namespace=namespace),
            "observed_at": raw.get("observed_at") or item.get("collected_at") or None,
            "event_time": item.get("timestamp") if signal.endswith("warning_event") else None,
            "sampled_at": item.get("timestamp") if signal == "metric_series_observed" else None,
            "resource_version": raw.get("resource_version") or _dict(raw.get("metadata")).get("resourceVersion") or None,
            "temporality": "historical" if raw.get("previous") or raw.get("stale")
            or item.get("signal_type") == "container_previous_termination" else "observed",
            "unit": raw.get("unit"), "value_type": raw.get("value_type"),
            "is_causal_proof": False}


def flatten_observations(evidence: dict) -> list[dict]:
    """Preserve original observations, including repeated collection versions."""
    return [{**item, "source_agent": agent}
            for agent, payload in evidence.items()
            if agent != "structured_evidence" and isinstance(payload, dict)
            for item in _rows(payload.get("evidence_items")) if isinstance(item, dict)]


def result_envelopes(run: dict) -> list[dict]:
    layers, layer = [], _dict(run.get("result"))
    for _ in range(4):
        if not layer:
            break
        layers.append(layer)
        layer = _dict(layer.get("result"))
    return layers


def _skill_commands(layers: list[dict]) -> list[dict]:
    for layer in layers:
        commands = _dict(_dict(layer.get("agent_evidence")).get("agent_evidence_collection")).get("commands")
        if isinstance(commands, list):
            return [r for r in commands if isinstance(r, dict)]
    return []


def _command_observations(call: dict) -> list[dict]:
    """Only structured successful read results become object observations.

    Parse resource JSON/YAML and the explicit minimal Pod snapshot protocol.
    Other text remains unresolved; no LLM summary becomes a measured fact.
    """
    if call.get("success") is not True or call.get("executed") is not True or call.get("outcome_unknown"):
        return []
    data = call.get("data")
    command = _text(call.get("command")) or _text(_dict(call.get("args")).get("command"))
    identity_projection = r'{.metadata.name}{"\n"}{.metadata.namespace}{"\n"}{.metadata.uid}{"\n"}{.metadata.resourceVersion}{"\n"}{.spec.nodeName}{"\n"}{.status.containerStatuses}'
    if isinstance(data, str) and identity_projection in command:
        fields = data.split("\n", 5)
        if len(fields) != 6 or not all(fields[:4]):
            return []
        name, namespace, uid, version, node, statuses = fields
        try:
            statuses = json.loads(statuses or "[]")
        except ValueError:
            return []
        if not isinstance(statuses, list):
            return []
        data = {"kind": "Pod", "metadata": {"name": name, "namespace": namespace,
                "uid": uid, "resourceVersion": version}, "spec": {"nodeName": node},
                "status": {"containerStatuses": statuses}}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            # safe_load is deterministic; only Kubernetes resource documents
            # survive the kind/metadata checks below.
            try:
                data = yaml.safe_load(data)
            except yaml.YAMLError:
                return []
    data = _dict(data)
    rows = _rows(data.get("items")) if _text(data.get("kind")).endswith("List") else [data]
    observations = []
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") not in set(KINDS.values()) or not _dict(row.get("metadata")).get("name"):
            continue
        # This projection needs identity/topology/status, never env values,
        # Secret contents, credentials or last-applied configuration annotations.
        metadata = _dict(row.get("metadata"))
        spec = _dict(row.get("spec"))
        projected = {"kind": row["kind"], "metadata": {k: metadata[k] for k in (
            "name", "namespace", "uid", "resourceVersion", "ownerReferences") if k in metadata},
            "spec": {k: spec[k] for k in ("nodeName", "nodeSelector", "replicas", "selector") if k in spec},
            "status": {k: _dict(row.get("status"))[k] for k in ("phase", "readyReplicas", "availableReplicas", "containerStatuses")
                       if k in _dict(row.get("status"))}}
        observations.append({"signal_type": "resource_observed", "source": call.get("command") or call.get("tool"),
             "source_agent": "skill_agent", "observation": json.dumps(projected, ensure_ascii=False, sort_keys=True),
             "timestamp": call.get("timestamp", ""), "observation_truncated": True,
             "raw_ref": {**projected, "projection": "identity_topology_status", "cluster_id": call.get("cluster_id", ""),
                         "observed_at": call.get("timestamp", "")}})
    return observations


def committed_tool_observations(call: dict) -> list[dict]:
    """Public projection used by both Skill reasoning and investigation views."""
    return _command_observations(call)


def build_investigation(run: dict) -> dict:
    """Project a persisted run into a deterministic, independently queryable view."""
    scope = {"cluster_id": _text(run.get("cluster_id")), "namespace": _text(run.get("namespace"))}
    layers = result_envelopes(run)
    report = next((r for r in reversed(layers) if "root_cause" in r), {})
    judge = next((_dict(r.get("judge")) for r in layers if isinstance(r.get("judge"), dict)), {})
    events = [e for e in _rows(run.get("events")) if isinstance(e, dict)]
    candidates = []
    # New runs preserve the complete collector contract. Legacy review catalogs
    # are explicitly marked excerpts, never described as complete collection.
    native = next((r["observations"] for r in layers if isinstance(r.get("observations"), list)), None)
    batches = [e for e in events if e.get("event") == "observation_batch"]
    if native is not None:
        candidates.extend((item, "collector") for item in native if isinstance(item, dict))
    elif batches:
        candidates.extend((item, "collector") for batch in batches for item in _rows(batch.get("items")) if isinstance(item, dict))
    else:
        for event in events:
            if event.get("event") == "structured_evidence":
                candidates.extend((item, "legacy_event_excerpt") for item in _rows(event.get("items")) if isinstance(item, dict))
        for fact in _rows(judge.get("review_evidence")):
            if isinstance(fact, dict):
                candidates.append(({**fact, "source_agent": fact.get("agent", "review_catalog")}, "review_excerpt"))
    commands = _skill_commands(layers)
    for call in commands:
        candidates.extend((item, "committed_tool") for item in _command_observations(call))
    objects, observations, links, seen = {}, [], [], set()
    for item, origin in candidates:
        if not item.get("source") or not item.get("observation"):
            continue
        canonical = {k: item.get(k) for k in ("signal_type", "source", "source_agent", "observation", "timestamp", "raw_ref",
                                              "collection_attempt", "iteration", "collected_at")}
        eid = observation_id(item)
        if eid in seen:
            continue
        seen.add(eid)
        semantics = observation_semantics(item, **scope)
        entity = semantics.pop("entity")
        if entity:
            objects[entity["id"]] = entity
        raw = _dict(item.get("raw_ref"))
        obs = {"id": eid, **canonical, **semantics, "entity_id": entity["id"] if entity else None,
               "origin": origin, "excerpted": origin.endswith("excerpt") or bool(item.get("observation_truncated"))
               or bool(raw.get("truncated") or raw.get("message_truncated")),
               "scope_status": "outside_run" if entity and (
                   scope["cluster_id"] and entity["cluster_id"] != scope["cluster_id"] or
                   scope["namespace"] and entity["namespace"] and entity["namespace"] != scope["namespace"])
               else "unresolved" if not entity else "in_run"}
        observations.append(obs)
        if entity and entity["kind"] == "Pod":
            spec = _dict(raw.get("spec"))
            node = _text(spec.get("nodeName")) or _text(raw.get("node_name"))
            if node:
                target = observation_entity({"raw_ref": {"kind": "Node", "name": node}}, cluster_id=entity["cluster_id"])
                objects[target["id"]] = target
                links.append({"source_id": entity["id"], "target_id": target["id"], "type": "scheduled_on",
                              "evidence_id": eid, "observed_at": obs["observed_at"], "is_causal_proof": False})
        if entity:
            metadata = _dict(raw.get("metadata"))
            for owner in _rows(metadata.get("ownerReferences")):
                if not isinstance(owner, dict):
                    continue
                target = observation_entity({"raw_ref": {**owner, "namespace": entity["namespace"]}}, cluster_id=entity["cluster_id"])
                if target:
                    objects[target["id"]] = target
                    links.append({"source_id": entity["id"], "target_id": target["id"], "type": "owned_by",
                                  "evidence_id": eid, "observed_at": obs["observed_at"], "is_causal_proof": False})
    tools = [{"id": _hash("tool-", [i, call]), "tool": call.get("tool"),
              "command": call.get("command") or _dict(call.get("args")).get("command"),
              "cluster_id": call.get("cluster_id") or scope["cluster_id"],
              "status": "unknown" if call.get("outcome_unknown") else "succeeded"
              if call.get("success") is True and call.get("executed") is True else "not_executed"
              if call.get("executed") is not True else "failed", "error": call.get("error")}
             for i, call in enumerate(commands)]
    failures = [{"agent": e.get("agent"), "summary": e.get("summary"), "status": "unknown"
                 if e.get("outcome_unknown") else "failed"} for e in events
                if e.get("event") == "evidence" and e.get("success") is False]
    collection_history = next((_rows(r.get("collection_history")) for r in layers if "collection_history" in r),
                              [_dict(e.get("collection")) for e in batches])
    failures.extend({"agent": r.get("agent"), "status": r.get("status"), "attempt": r.get("attempt"),
                     "errors": r.get("collection_errors"), "error": r.get("error")}
                    for r in collection_history if isinstance(r, dict) and r.get("status") in {"partial", "failed"})
    gaps = []
    if native is None and not batches and not commands:
        gaps.append("legacy_evidence_may_be_excerpted")
    if not observations:
        gaps.append("no_structured_observations")
    if any(o["entity_id"] is None for o in observations):
        gaps.append("unresolved_observation_identity")
    if any(o["identity_status"] != "uid_bound" for o in objects.values() if o["kind"] not in CLUSTER_SCOPED):
        gaps.append("object_uid_or_scope_missing")
    if any(not (o["observed_at"] or o["event_time"] or o["sampled_at"]) for o in observations):
        gaps.append("observation_time_missing")
    if failures or any(t["status"] != "succeeded" for t in tools):
        gaps.append("collection_incomplete")
    if any(o["scope_status"] == "outside_run" for o in observations):
        gaps.append("cross_scope_observations_retained_for_audit")
    review_links = []
    cited = _rows(_dict(judge.get("llm_based")).get("supported_evidence_ids"))
    for fact in _rows(judge.get("review_evidence")):
        if not isinstance(fact, dict):
            continue
        matches = []
        for obs in observations:
            same_reference = all(json.dumps(obs.get(key), sort_keys=True, default=str) ==
                                 json.dumps(fact.get(key), sort_keys=True, default=str)
                                 for key in ("source", "signal_type", "timestamp", "raw_ref"))
            text_matches = obs["observation"] == fact.get("observation") or (
                fact.get("observation_truncated") is True and
                str(obs["observation"]).startswith(str(fact.get("observation") or "")))
            if same_reference and text_matches:
                matches.append(obs["id"])
        review_links.append({"review_id": fact.get("id"), "observation_ids": matches,
                             "cited_by_reviewer": fact.get("id") in cited,
                             "status": "resolved" if matches else "unresolved"})
    if any(link["status"] == "unresolved" for link in review_links):
        gaps.append("review_citation_unresolved")
    for link in links:
        for side in ("source", "target"):
            obj = objects[link[f"{side}_id"]]
            link[f"{side}_label"] = f"{obj['kind']}/{obj['name']}"
    model = {"schema_version": SCHEMA_VERSION, "run_id": run.get("id"), "status": run.get("status"),
             "scope": scope, "objects": sorted(objects.values(), key=lambda o: o["id"]),
             "observations": observations, "relations": links, "tool_executions": tools,
             "collection_failures": failures, "gaps": gaps,
             "review_evidence_links": review_links,
             "decision": {"root_cause": report.get("root_cause"), "root_entity": report.get("root_entity"),
                          "fault_type": report.get("fault_type"), "confidence": report.get("confidence"),
                          "report_revision": _hash("decision-", report),
                          "judge_score": judge.get("combined_score"),
                          "diagnosis_status": report.get("diagnosis_status", "unreviewed"),
                          "automation_gate": report.get("automation_gate"),
                          "review_status": "requires_review" if judge.get("needs_review") is True else
                          "reviewed" if judge.get("needs_review") is False else "not_available",
                          "review_blockers": judge.get("review_blockers", [])},
             "semantics": {"relations_are_causal_proof": False, "times_are_collection_or_event_times": True,
                           "scope": "recorded_investigation_only", "live_state_verified": False}}
    model["revision"] = _hash("rev-", model)
    return model


def action_context(model: dict) -> dict:
    """Bind an action proposal to one recorded object incarnation and decision.

    Passing these checks does not authorize execution. Live state, permissions,
    approval, command scope and recovery gates must still be enforced by host.
    """
    root = _dict(model["decision"].get("root_entity"))
    root_scope_conflict = bool(root.get("cluster_id") and root["cluster_id"] != model["scope"]["cluster_id"]
                               or root.get("namespace") and model["scope"]["namespace"]
                               and root["namespace"] != model["scope"]["namespace"])
    matches = [obj for obj in model["objects"] if all(obj.get(k) == root.get(k)
               for k in ("namespace", "kind", "name")) and obj["cluster_id"] == model["scope"]["cluster_id"]
               and (not root.get("uid") or root["uid"] == obj["uid"])] if root and not root_scope_conflict else []
    # Name-bound observations do not add another incarnation if exactly one UID
    # has been observed. Multiple UIDs remain ambiguous, never "latest wins".
    bound = [obj for obj in matches if obj["identity_status"] == "uid_bound"]
    target = bound[0] if len(bound) == 1 else None
    versions = [o["resource_version"] for o in model["observations"]
                if target and o["entity_id"] == target["id"] and o.get("resource_version")
                and o["temporality"] != "historical"]
    blockers = []
    if model["status"] != "completed":
        blockers.append("investigation_not_completed")
    if not target:
        blockers.append("target_incarnation_unresolved")
    if not versions:
        blockers.append("target_resource_version_missing")
    if model["decision"]["review_status"] != "reviewed":
        blockers.append("independent_review_required")
    if _dict(model["decision"].get("automation_gate")).get("allowed") is not True:
        blockers.append("diagnosis_gate_not_allowed")
    return {"source_run_id": model["run_id"], "ontology_revision": model["revision"],
            "target": target, "blockers": blockers, "recorded_conditions_met": not blockers,
            "expected_resource_version": versions[-1] if versions else None,
            "live_state_verified": False, "execution_authorized": False,
            "required_checks": ["live_target_uid", "command_scope", "existing_approval_and_risk_gates",
                                "recovery_verification"]}


def command_targets_bound_object(command: str, target: dict) -> bool:
    """A deliberately small grammar for direct-object ontology actions.

    Cross-object and selector-based actions need their own typed definitions;
    sharing a name or namespace is insufficient authorization.
    """
    if any(char in command for char in "\n\r;|&<>`$"):
        return False
    try:
        args = shlex.split(command)
    except ValueError:
        return False
    if not args or args.pop(0) != "kubectl":
        return False
    ns, rest = [], []
    while args:
        arg = args.pop(0)
        if arg in {"-n", "--namespace"}:
            if not args:
                return False
            ns.append(args.pop(0))
        elif arg.startswith("--namespace="):
            ns.append(arg.split("=", 1)[1])
        else:
            rest.append(arg)
    if ns != [target.get("namespace")] or not rest:
        return False
    if any(arg in {"--all", "-A", "--all-namespaces", "-l", "--selector", "--context", "--kubeconfig", "--server"}
           or arg.startswith(("--selector=", "--context=", "--kubeconfig=", "--server=")) for arg in rest):
        return False
    if rest[0] in {"get", "describe", "patch", "scale"}:
        index = 1
    elif len(rest) > 2 and ((rest[0] == "rollout" and rest[1] in {"restart", "undo", "status"})
                            or rest[:2] == ["set", "image"]):
        index = 2
    else:
        return False
    if len(rest) <= index:
        return False
    resource = rest[index].split("/", 1)
    if len(resource) == 1:
        if len(rest) <= index + 1:
            return False
        resource.append(rest[index + 1])
        end = index + 2
    else:
        end = index + 1
    aliases = {"pods": "Pod", "pod": "Pod", "deploy": "Deployment", "deployments": "Deployment",
               "deployment": "Deployment", "statefulset": "StatefulSet", "sts": "StatefulSet",
               "service": "Service", "svc": "Service"}
    if aliases.get(resource[0]) != target.get("kind") or resource[1] != target.get("name"):
        return False
    # Reject extra object names. Only flags, their arguments, and set-image
    # assignments follow the selected resource in the supported grammar.
    tail = rest[end:]
    while tail:
        arg = tail.pop(0)
        if arg in {"-p", "--patch", "--type", "--replicas", "--timeout", "-o", "--output", "--to-revision"}:
            if not tail:
                return False
            tail.pop(0)
        elif arg.startswith(("--replicas=", "--timeout=", "--type=", "--to-revision=")):
            continue
        elif rest[:2] == ["set", "image"] and "=" in arg and not arg.startswith("-"):
            continue
        else:
            return False
    return True


def preconditioned_action(command: str, target: dict, resource: dict, *, restart_marker: str) -> str:
    """Compile supported writes into a single atomic Kubernetes JSON Patch.

    Kubernetes tests UID and resourceVersion in the same API operation as the
    update. Unsupported writes have no fallback to name-based shell execution.
    This guarantees one resource update, not a multi-command transaction.
    """
    if not command_targets_bound_object(command, target):
        raise ValueError("command_scope_not_bound")
    args = shlex.split(command)[1:]
    rest = []
    while args:
        arg = args.pop(0)
        if arg in {"-n", "--namespace"}:
            args.pop(0)
        elif not arg.startswith("--namespace="):
            rest.append(arg)
    if rest[0] in {"get", "describe"} or rest[:2] == ["rollout", "status"]:
        return command
    metadata, spec = _dict(resource.get("metadata")), _dict(resource.get("spec"))
    if metadata.get("uid") != target.get("uid") or not metadata.get("resourceVersion"):
        raise ValueError("live_resource_identity_missing")
    edits = []
    if rest[0] == "scale" and target["kind"] == "Deployment":
        values = []
        for i, arg in enumerate(rest):
            if arg.startswith("--replicas="):
                values.append(arg.split("=", 1)[1])
            elif arg == "--replicas" and i + 1 < len(rest):
                values.append(rest[i + 1])
        if len(values) != 1 or not values[0].isdigit():
            raise ValueError("invalid_scale_parameters")
        edits = [{"op": "add", "path": "/spec/replicas", "value": int(values[0])}]
    elif rest[:2] == ["rollout", "restart"] and target["kind"] == "Deployment":
        template = _dict(spec.get("template"))
        template_metadata = _dict(template.get("metadata"))
        if "metadata" not in template:
            edits.append({"op": "add", "path": "/spec/template/metadata", "value": {}})
        if "annotations" not in template_metadata:
            edits.append({"op": "add", "path": "/spec/template/metadata/annotations", "value": {}})
        edits.append({"op": "add", "path": "/spec/template/metadata/annotations/agenticsre.io~1restart-proposal", "value": restart_marker})
    elif rest[:2] == ["set", "image"] and target["kind"] == "Deployment":
        containers = copy.deepcopy(_rows(_dict(_dict(spec.get("template")).get("spec")).get("containers")))
        assignments = [arg for arg in rest[3:] if "=" in arg and not arg.startswith("-")]
        changed = set()
        for assignment in assignments:
            name, image = assignment.split("=", 1)
            matching = [(i, c) for i, c in enumerate(containers) if c.get("name") == name]
            if not image or len(matching) != 1 or name in changed:
                raise ValueError("invalid_image_parameters")
            edits.append({"op": "add", "path": f"/spec/template/spec/containers/{matching[0][0]}/image", "value": image})
            changed.add(name)
        if not changed:
            raise ValueError("image_assignment_missing")
    else:
        raise ValueError("typed_action_not_supported")
    patch = [{"op": "test", "path": "/metadata/uid", "value": target["uid"]},
             {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]}, *edits]
    return " ".join(["kubectl", "patch", target["kind"].lower(), shlex.quote(target["name"]),
                     "-n", shlex.quote(target["namespace"]), "--type=json", "-p",
                     shlex.quote(json.dumps(patch, ensure_ascii=False, separators=(",", ":")))])


def query_investigation(model: dict, collection: str, *, entity_id: str = "", record_id: str = "", offset: int = 0,
                        limit: int = 50) -> dict:
    if collection not in {"objects", "observations", "relations", "tool_executions"}:
        raise ValueError("Unknown ontology collection")
    rows = model[collection]
    if record_id:
        rows = [r for r in rows if r.get("id") == record_id]
    if entity_id:
        rows = [r for r in rows if r.get("entity_id") == entity_id or r.get("id") == entity_id
                or r.get("source_id") == entity_id or r.get("target_id") == entity_id]
    return {"run_id": model["run_id"], "revision": model["revision"], "collection": collection,
            "total": len(rows), "offset": offset, "limit": limit, "items": rows[offset:offset + limit]}
