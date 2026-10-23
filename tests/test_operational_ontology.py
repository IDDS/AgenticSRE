"""Behavioral contracts for ontology-backed investigation and action proposals."""
import asyncio
import copy
import json

import pytest

from operational_ontology import (action_context, build_investigation, command_targets_bound_object,
                                  observation_entity, query_investigation, preconditioned_action)


def fact(*, name="worker", uid="uid-a", cluster="east", namespace="shop", **raw):
    return {"signal_type": "pod_waiting_state", "source": "kubectl", "source_agent": "event_agent",
            "timestamp": "2026-10-20T01:00:00Z", "observation": "Pod current state Pending",
            "raw_ref": {"kind": "Pod", "name": name, "namespace": namespace, "pod_uid": uid,
                        "cluster_id": cluster, "observed_at": "2026-10-20T01:00:00Z", "resource_version": "100", **raw}}


def run(*facts):
    return {"id": "rca-test", "status": "completed", "cluster_id": "east", "namespace": "shop",
            "result": {"observations": list(facts), "judge": {"needs_review": False},
                       "result": {"root_cause": "observed scheduling failure", "fault_type": "SchedulingFailed",
                                  "root_entity": {"kind": "Pod", "name": "worker", "namespace": "shop"},
                                  "automation_gate": {"allowed": True}}}, "events": []}


@pytest.mark.parametrize("change", [dict(uid="uid-b"), dict(cluster="west"), dict(namespace="other"), dict(uid="")])
def test_identity_never_merges_incarnations_or_scopes(change):
    model = build_investigation(run(fact(), fact(**change)))
    assert len(model["objects"]) == 2
    assert len({o["id"] for o in model["objects"]}) == 2


def test_no_prose_or_display_name_identity_inference():
    item = {"source": "prometheus", "signal_type": "metric_series_observed", "service": "worker",
            "observation": "Pod/worker seems down", "raw_ref": {"namespace": "shop"}}
    assert observation_entity(item) is None
    assert observation_entity(fact())["uid"] == "uid-a"


def test_event_uid_is_not_pod_uid():
    item = fact()
    item["raw_ref"] = {"kind": "Event", "metadata": {"uid": "event-uid"},
                       "involvedObject": {"kind": "Pod", "name": "worker", "namespace": "shop"}}
    assert observation_entity(item)["uid"] == ""


def test_snapshot_is_deterministic_readonly_and_revision_bound():
    record = run(fact())
    before = copy.deepcopy(record)
    model = build_investigation(record)
    assert record == before and model == build_investigation(record)
    record["result"]["observations"].append(fact(uid="new"))
    assert model["revision"] != build_investigation(record)["revision"]


def test_relations_need_explicit_structured_evidence_and_do_not_prove_cause():
    model = build_investigation(run(fact(node_name="node-a", metadata={"ownerReferences": [
        {"kind": "ReplicaSet", "name": "worker-rs", "uid": "rs-uid"}]})))
    assert {r["type"] for r in model["relations"]} == {"scheduled_on", "owned_by"}
    assert all(r["evidence_id"] in {o["id"] for o in model["observations"]} for r in model["relations"])
    assert all(r["is_causal_proof"] is False for r in model["relations"])
    assert build_investigation(run(fact()))["relations"] == []


def test_failures_history_and_missing_time_remain_explicit():
    item = fact(previous=True, observed_at="")
    item["timestamp"] = ""
    record = run(item)
    record["events"] = [{"event": "evidence", "agent": "log_agent", "success": False, "summary": "unavailable"}]
    model = build_investigation(record)
    assert {"observation_time_missing", "collection_incomplete"} <= set(model["gaps"])
    assert model["observations"][0]["temporality"] == "historical"
    assert action_context(model)["recorded_conditions_met"] is False


def test_query_keeps_object_binding_and_paginates():
    model = build_investigation(run(fact(), fact(name="peer")))
    entity = next(o for o in model["objects"] if o["name"] == "worker")
    page = query_investigation(model, "observations", entity_id=entity["id"], limit=1)
    assert page["total"] == 1 and page["items"][0]["entity_id"] == entity["id"]
    assert query_investigation(model, "observations", offset=2)["items"] == []
    with pytest.raises(ValueError):
        query_investigation(model, "summaries")


def test_legacy_excerpt_cannot_claim_complete_observation_collection():
    record = run()
    del record["result"]["observations"]
    record["result"]["judge"]["review_evidence"] = [fact()]
    model = build_investigation(record)
    assert model["observations"][0]["excerpted"] is True
    assert "legacy_evidence_may_be_excerpted" in model["gaps"]


@pytest.mark.parametrize("unknown,executed,success,expected", [
    (False, True, True, 1), (True, True, True, 0), (False, False, True, 0), (False, True, False, 0),
])
def test_only_committed_successful_structured_skill_results_become_observations(unknown, executed, success, expected):
    record = run()
    record["result"] = {"agent_evidence": {"agent_evidence_collection": {"commands": [{
        "tool": "kubectl", "command": "get pod/worker -o json", "executed": executed,
        "success": success, "outcome_unknown": unknown, "data": json.dumps({
            "kind": "Pod", "metadata": {"name": "worker", "namespace": "shop", "uid": "uid-a"}})}]}}}
    model = build_investigation(record)
    assert len(model["observations"]) == expected
    assert model["tool_executions"][0]["status"] == (
        "unknown" if unknown else "succeeded" if expected else "not_executed" if not executed else "failed")


def test_action_binds_one_uid_and_requires_review_and_existing_gate():
    model = build_investigation(run(fact()))
    context = action_context(model)
    assert context["recorded_conditions_met"] is True
    assert context["target"]["uid"] == "uid-a"
    assert context["execution_authorized"] is False
    assert not action_context(build_investigation(run(fact(), fact(uid="rebuilt"))))["recorded_conditions_met"]
    record = run(fact())
    record["result"]["judge"] = {}
    assert "independent_review_required" in action_context(build_investigation(record))["blockers"]


@pytest.mark.parametrize("command,expected", [
    ("kubectl get pod worker -n shop -o json", True),
    ("kubectl describe pod/worker --namespace=shop", True),
    ("kubectl get pod/peer -n shop", False),
    ("kubectl get pod worker peer -n shop", False),
    ("kubectl get pod/worker -n other", False),
    ("kubectl get pod/worker -n shop --context other", False),
    ("kubectl get pods -A", False),
    ("kubectl get pod worker -n shop; kubectl delete pod peer -n shop", False),
    ("kubectl get pod worker -n shop -n other", False),
    ("kubectl scale deployment/worker -n shop --replicas=2", False),
])
def test_actions_cannot_escape_bound_resource_scope(command, expected):
    assert command_targets_bound_object(command, {"kind": "Pod", "name": "worker", "namespace": "shop"}) is expected


def test_actual_rca_report_formatting_uses_typed_object_identity():
    from agents.evidence import format_evidence
    result = format_evidence({"event_agent": {"evidence_items": [fact()]}})
    assert '"entity":' in result and '"uid": "uid-a"' in result


def test_strict_root_alignment_rejects_uid_and_cluster_disagreement():
    from orchestrator.collaboration_optimizer import root_scoped_evidence
    root = {"root_entity": {"kind": "Pod", "name": "worker", "namespace": "shop", "uid": "uid-a", "cluster_id": "east"}}
    observations = [fact(), fact(uid="rebuilt"), fact(cluster="west"), fact(uid="")]
    selected = root_scoped_evidence(root, {"event_agent": {"evidence_items": observations}})
    assert selected["event_agent"]["evidence_items"] == [observations[0]]


def test_api_pagination_revision_and_report_share_same_ontology(monkeypatch):
    import web_app.app as app
    record = run(fact())
    monkeypatch.setitem(app._state, "rca_runs", {record["id"]: record})
    summary = asyncio.run(app.rca_ontology(record["id"]))
    page = asyncio.run(app.rca_ontology_collection(record["id"], "observations", revision=summary["revision"], offset=0, limit=10))
    assert page["total"] == summary["counts"]["observations"] == 1
    assert app._build_report(record)["ontology"]["revision"] == summary["revision"]
    with pytest.raises(app.HTTPException) as exc:
        asyncio.run(app.rca_ontology_collection(record["id"], "observations", revision="stale", offset=0, limit=10))
    assert exc.value.status_code == 409


def test_action_uses_persisted_decision_and_rejects_stale_or_cross_scope_payload(monkeypatch):
    import web_app.app as app
    record = run(fact())
    monkeypatch.setitem(app._state, "rca_runs", {record["id"]: record})
    body = {"source_run_id": record["id"], "diagnosis": {"root_cause": "caller invented"}}
    context = app._bind_ontology_heal_input(body)
    assert body["diagnosis"]["root_cause"] == "observed scheduling failure"
    assert body["cluster_id"] == "east" and body["pod"] == "worker"
    assert context["ontology_revision"] == body["ontology_revision"]
    for bad in ({"cluster_id": "west"}, {"namespace": "other"}, {"pod": "peer"}, {"ontology_revision": "old"}):
        with pytest.raises(app.HTTPException) as exc:
            app._bind_ontology_heal_input({"source_run_id": record["id"], **bad}, execution=True)
        assert exc.value.status_code == 409


@pytest.mark.parametrize("uid,version", [("rebuilt", "100"), ("uid-a", "101"), ("", "")])
def test_real_action_rechecks_uid_and_resource_version_before_execution(monkeypatch, uid, version):
    import web_app.app as app
    context = action_context(build_investigation(run(fact())))
    monkeypatch.setattr(app, "_ontology_live_resource", lambda *a, **k: {
        "ok": True, "json": {"metadata": {"uid": uid, "resourceVersion": version}}})
    with pytest.raises(app.HTTPException) as exc:
        app._verify_ontology_action(context, ["kubectl get pod worker -n shop"])
    assert exc.value.status_code == 409


def test_each_collection_attempt_is_retained_even_after_recovery_or_failure():
    from orchestrator.session import RCASession
    session = RCASession()
    first = fact()
    session.record_evidence("event_agent", {"evidence_items": [first]})
    first["observation"] = "caller changed"
    session.record_evidence("event_agent", {"evidence_items": [fact(uid="rebuilt")]})
    session.record_evidence("event_agent", {"error": "timeout", "evidence_items": []})
    assert len(session.observation_history) == 2 and len(session.collection_history) == 3
    assert session.observation_history[0]["observation"] != "caller changed"
    assert session.evidence["event_agent"]["error"] == "timeout"
    assert [r["collection_attempt"] for r in session.observation_history] == [1, 2]


def test_interrupted_record_recovers_full_checkpoint_batches_and_partial_errors():
    record = run()
    record["status"] = "failed"
    record["result"] = {"error": "process_interrupted"}
    record["events"] = [{"event": "observation_batch", "items": [fact()], "collection": {
        "agent": "metric_agent", "status": "partial", "collection_errors": [{"metric": "cpu", "error": "timeout"}]}}]
    model = build_investigation(record)
    assert len(model["observations"]) == 1
    assert "legacy_evidence_may_be_excerpted" not in model["gaps"]
    assert "collection_incomplete" in model["gaps"]
    assert model["collection_failures"][0]["errors"][0]["metric"] == "cpu"


def test_sample_time_and_event_time_do_not_claim_to_be_collection_time():
    from operational_ontology import observation_semantics
    item = fact(observed_at="")
    item["signal_type"] = "pod_warning_event"
    fields = observation_semantics(item)
    assert fields["observed_at"] is None and fields["event_time"] == item["timestamp"]
    item["signal_type"] = "metric_series_observed"
    fields = observation_semantics(item)
    assert fields["observed_at"] is None and fields["sampled_at"] == item["timestamp"]


def test_endpoint_observation_is_not_retyped_as_related_deployment():
    item = {"signal_type": "service_endpoints_empty", "raw_ref": {"name": "worker", "namespace": "shop", "deployment": "worker"}}
    assert observation_entity(item)["kind"] == "Endpoints"


def test_metric_labels_override_query_scope_without_merging_names():
    item = {"signal_type": "metric_series_observed", "raw_ref": {
        "namespace": "shop", "labels": {"pod": "worker", "namespace": "other"}}}
    assert observation_entity(item)["namespace"] == "other"


def test_root_in_another_cluster_cannot_bind_same_named_local_object():
    record = run(fact())
    record["result"]["result"]["root_entity"]["cluster_id"] = "west"
    assert action_context(build_investigation(record))["target"] is None


def test_review_ids_resolve_to_original_observation_without_creating_new_fact():
    original = fact()
    record = run(original)
    record["result"]["judge"].update(review_evidence=[dict(original, id="E1")],
                                      llm_based={"supported_evidence_ids": ["E1"]})
    model = build_investigation(record)
    assert len(model["observations"]) == 1
    assert model["review_evidence_links"] == [{"review_id": "E1", "observation_ids": [model["observations"][0]["id"]],
                                              "cited_by_reviewer": True, "status": "resolved"}]


@pytest.mark.parametrize("command,path,value", [
    ("kubectl scale deployment/worker -n shop --replicas=2", "/spec/replicas", 2),
    ("kubectl rollout restart deployment/worker -n shop", "/spec/template/metadata/annotations/agenticsre.io~1restart-proposal", "proposal-test"),
    ("kubectl set image deployment/worker app=registry/app:v2 -n shop", "/spec/template/spec/containers/0/image", "registry/app:v2"),
])
def test_external_writes_use_atomic_uid_and_version_tests(command, path, value):
    import shlex
    target = {"kind": "Deployment", "name": "worker", "namespace": "shop", "uid": "uid-a"}
    resource = {"metadata": {"uid": "uid-a", "resourceVersion": "100"},
                "spec": {"template": {"spec": {"containers": [{"name": "app", "image": "registry/app:v1"}]}}}}
    materialized = preconditioned_action(command, target, resource, restart_marker="proposal-test")
    args = shlex.split(materialized)
    patch = json.loads(args[args.index("-p") + 1])
    assert patch[:2] == [{"op": "test", "path": "/metadata/uid", "value": "uid-a"},
                         {"op": "test", "path": "/metadata/resourceVersion", "value": "100"}]
    assert patch[-1]["path"] == path and patch[-1]["value"] == value


def test_unsupported_write_never_falls_back_to_name_based_shell():
    target = {"kind": "Pod", "name": "worker", "namespace": "shop", "uid": "uid-a"}
    with pytest.raises(ValueError, match="typed_action_not_supported"):
        preconditioned_action("kubectl patch pod/worker -n shop -p '{}'", target,
                              {"metadata": {"uid": "uid-a", "resourceVersion": "100"}}, restart_marker="test")


def test_consumed_proposal_cannot_dispatch_again(monkeypatch, tmp_path):
    import web_app.app as app
    context = action_context(build_investigation(run(fact())))
    monkeypatch.setattr(app, "_HEAL_ARTIFACT_DIR", tmp_path)
    monkeypatch.setattr(app, "_load_heal_runs", lambda: [{"id": "heal-preview", "execution_claim": {"status": "outcome_unknown"}}])
    monkeypatch.setattr(app, "_shell_sync", lambda *a, **k: pytest.fail("must not dispatch"))
    with pytest.raises(app.HTTPException) as exc:
        app._execute_ontology_command("kubectl get pod/worker -n shop", context, "heal-preview")
    assert exc.value.status_code == 409


@pytest.mark.parametrize("remote_ok", [True, False])
def test_real_endpoint_requires_exact_server_recorded_dry_run_plan(monkeypatch, tmp_path, remote_ok):
    import web_app.app as app
    from configs.config_loader import AppConfig
    record = run(fact())
    monkeypatch.setitem(app._state, "rca_runs", {record["id"]: record})
    cfg = AppConfig()
    cfg.remediation.enabled = True
    cfg.remediation.recommend_only = False
    cfg.remediation.require_approval = True
    monkeypatch.setattr(app, "_get_config", lambda: cfg)
    monkeypatch.setattr(app, "_HEAL_ARTIFACT_DIR", tmp_path)
    history = []
    monkeypatch.setattr(app, "_load_heal_runs", lambda: copy.deepcopy(history))
    def save(row):
        history[:] = [r for r in history if r["id"] != row["id"]] + [copy.deepcopy(row)]
    monkeypatch.setattr(app, "_save_heal_run", save)
    monkeypatch.setattr(app, "_build_heal_suggestions", lambda b: {
        "namespace": "shop", "target": {"pod": "worker"}, "diagnosis_gate": {"ready": True}})
    monkeypatch.setattr(app, "_verify_heal_recovery", lambda **kw: {"status": "unknown", "recovered": False})
    monkeypatch.setattr(app, "_ontology_live_resource", lambda *a, **k: {"ok": True,
        "json": {"metadata": {"uid": "uid-a", "resourceVersion": "100"}}})
    dispatched = []
    monkeypatch.setattr(app, "_ontology_shell_sync", lambda command, *a: dispatched.append(command) or {"ok": remote_ok})
    class Request:
        def __init__(self, body):
            self.body = body
        async def json(self):
            return dict(self.body)
    body = {"source_run_id": "rca-test", "ontology_revision": build_investigation(record)["revision"],
            "commands": ["kubectl get pod/worker -n shop"], "dry_run": True}
    preview = asyncio.run(app.heal_execute(Request(body)))
    assert not dispatched and preview["dry_run"] is True
    real = {**body, "dry_run": False, "approved": True, "ontology_proposal_id": preview["id"]}
    with pytest.raises(app.HTTPException) as exc:
        asyncio.run(app.heal_execute(Request({**real, "commands": ["kubectl get pod/worker -n shop -o json"]})))
    assert exc.value.status_code == 409 and not dispatched
    execution = asyncio.run(app.heal_execute(Request(real)))
    assert len(dispatched) == 1
    if remote_ok:
        assert execution["action_success"] is True
    else:
        assert execution["status"] == "outcome_unknown" and execution["success"] is None
        assert execution["action_success"] is None and execution["verification"]["recovered"] is None
        assert execution["recovery_history_record"]["recorded"] is False
    replay = asyncio.run(app.heal_execute(Request(real)))
    assert len(dispatched) == 1 and replay["action_success"] is False


def test_cluster_routing_preserves_object_names_and_image_arguments(monkeypatch):
    import shlex
    import web_app.app as app
    cluster = {"id": "east", "context": "east-context"}
    monkeypatch.setattr(app, "_find_k8s_cluster", lambda _: cluster)
    monkeypatch.setattr(app, "_cluster_kubectl_path", lambda _: "/opt/bin/kubectl")
    monkeypatch.setattr(app, "_cluster_kubeconfig_path", lambda _: "/etc/kube/east.yaml")
    calls = []
    monkeypatch.setattr(app, "_shell_sync", lambda command, *a: calls.append(command) or {"ok": True})
    original = "kubectl set image deployment/kubectl-worker app=registry/kubectl:v2 -n shop"
    app._ontology_shell_sync(original, 30, "east")
    args = shlex.split(calls[0])
    assert args[0] == "/opt/bin/kubectl" and "--context=east-context" in args
    assert "deployment/kubectl-worker" in args and "app=registry/kubectl:v2" in args


def test_post_dispatch_persistence_failure_is_unknown_not_unsent(monkeypatch, tmp_path):
    import web_app.app as app
    context = action_context(build_investigation(run(fact())))
    context["target"]["kind"] = "Deployment"
    monkeypatch.setattr(app, "_HEAL_ARTIFACT_DIR", tmp_path)
    monkeypatch.setattr(app, "_load_heal_runs", lambda: [{"id": "heal-preview"}])
    monkeypatch.setattr(app, "_verify_ontology_action", lambda *a: {
        "metadata": {"uid": "uid-a", "resourceVersion": "100"}})
    saves, dispatched = [], []
    def save(row):
        saves.append(copy.deepcopy(row))
        if len(saves) == 2:
            raise app.HTTPException(503, "simulated disk failure")
    monkeypatch.setattr(app, "_save_heal_run", save)
    monkeypatch.setattr(app, "_ontology_shell_sync", lambda command, *a: dispatched.append(command) or {"ok": True})
    result = app._execute_ontology_command("kubectl scale deployment/worker -n shop --replicas=2", context, "heal-preview")
    assert len(dispatched) == 1 and saves[0]["execution_claim"]["status"] == "outcome_unknown"
    assert result["executed"] is True and result["outcome_unknown"] is True and result["persistence_error"] is True


def test_projected_skill_snapshot_binds_status_to_measured_uid():
    record = run()
    projection = r'{.metadata.name}{"\n"}{.metadata.namespace}{"\n"}{.metadata.uid}{"\n"}{.metadata.resourceVersion}{"\n"}{.spec.nodeName}{"\n"}{.status.containerStatuses}'
    status = [{"name": "app", "state": {"waiting": {"reason": "ImagePullBackOff"}}}]
    record["result"] = {"agent_evidence": {"agent_evidence_collection": {"commands": [{
        "tool": "kubectl", "command": f"get pod/worker -o jsonpath='{projection}'",
        "success": True, "executed": True, "data": "worker\nshop\nmeasured-uid\n100\nnode-a\n" + json.dumps(status)}]}}}
    model = build_investigation(record)
    assert any(obj["uid"] == "measured-uid" for obj in model["objects"])
    assert "ImagePullBackOff" in model["observations"][0]["observation"]
    assert model["observations"][0]["resource_version"] == "100"
    assert model["relations"][0]["type"] == "scheduled_on"


@pytest.mark.parametrize("name,namespace,uid", [("peer", "audit", "uid"), ("demo", "other", "uid"), ("demo", "audit", "")])
def test_image_skill_rejects_snapshot_with_missing_or_conflicting_identity(name, namespace, uid):
    from pathlib import Path
    from skill_runtime.executor import SkillExecutor
    skills = Path(__file__).resolve().parents[1] / "skill_runtime" / "skills"
    def runner(*args):
        return {"success": True, "executed": True, "data": f"{name}\n{namespace}\n{uid}\n100\n\n" + json.dumps([
            {"state": {"waiting": {"reason": "ImagePullBackOff"}}}])}
    execution = SkillExecutor.from_dir(skills).run("k8s.image_pull_error", {"pod": "demo", "namespace": "audit"},
        stages=["detect", "diagnose"], tool_runner=runner).to_dict()
    assert execution["stages"]["detect"]["status"] == "skipped"
    assert execution["stages"]["diagnose"]["status"] == "skipped"


@pytest.mark.parametrize("missing", [0, 1, 2, 3])
def test_ontology_projection_adapter_never_invents_missing_identity(missing):
    record = run()
    projection = r'{.metadata.name}{"\n"}{.metadata.namespace}{"\n"}{.metadata.uid}{"\n"}{.metadata.resourceVersion}{"\n"}{.spec.nodeName}{"\n"}{.status.containerStatuses}'
    fields = ["worker", "shop", "uid-a", "100", "node-a", "[]"]
    fields[missing] = ""
    record["result"] = {"agent_evidence": {"agent_evidence_collection": {"commands": [{
        "tool": "kubectl", "command": f"get pod/worker -o jsonpath='{projection}'", "executed": True,
        "success": True, "data": "\n".join(fields)}]}}}
    assert build_investigation(record)["objects"] == []


def test_current_pod_collection_preserves_node_membership_as_observation():
    from agents.pod_state_evidence import problem_containers
    pod = {"metadata": {"name": "worker", "namespace": "shop", "uid": "uid-a", "resourceVersion": "100"},
           "spec": {"nodeName": "node-a"}, "status": {"phase": "Pending"}}
    rows = problem_containers([pod], "shop")
    assert rows[0]["node_name"] == "node-a" and rows[0]["pod_uid"] == "uid-a"
    model = build_investigation(run(fact(**rows[0])))
    assert model["relations"][0]["target_label"] == "Node/node-a"
