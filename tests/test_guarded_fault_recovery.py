from __future__ import annotations

import pathlib
import sys

import pytest
import yaml
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import web_app.app as dashboard  # noqa: E402
from skill_runtime.executor import SkillExecutor  # noqa: E402
from skill_runtime.precision_injection import PrecisionInjectionError, build_precision_contract  # noqa: E402
from skill_runtime.precision_execution import (  # noqa: E402
    PRECISION_ADAPTERS,
    execute_precision_adapter,
    precision_adapter_catalog,
)
from skill_runtime.workflow import SkillWorkflow  # noqa: E402


SKILLS = ROOT / "skill_runtime" / "skills"


def _guarded_payload(**overrides):
    payload = {
        "operation": "fault_injection",
        "scenario_id": "sn-svc-scale-down",
        "registered_scenario": True,
        "cluster_id": "",
        "namespace": "test-social-network",
        "target_entity_ids": ["deployment/home-timeline-service"],
        "target_scopes": [{
            "namespace": "test-social-network",
            "target_entity_id": "deployment/home-timeline-service",
        }],
        "fault_type": "service_unavailable",
        "duration_s": 30,
        "approval_required": True,
        "auto_cleanup": True,
        "blast_radius_budget": {"max_affected_entities": 1, "max_duration_s": 120},
    }
    payload.update(overrides)
    return payload


def test_guarded_injection_emits_typed_action_undo_and_verification() -> None:
    run = SkillExecutor.from_dir(SKILLS).run(
        "k8s.guarded_fault_injection", _guarded_payload(), dry_run=True,
    ).to_dict()
    action = run["stages"]["heal"]["output"]["suggestions"][0]

    assert action["operation"] == "agenticsre.k8s.fault.inject"
    assert action["rollback"]["operation"] == "agenticsre.k8s.fault.cleanup"
    assert action["verification"]["operation"] == "agenticsre.k8s.recovery.verify"
    assert action["requires_approval"] is True
    assert "command" not in action


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"registered_scenario": False}, "已登记场景"),
        ({"commands": ["kubectl delete pod anything"]}, "command/shell/script/kubectl"),
        ({
            "target_entity_ids": ["deployment/a", "deployment/b"],
            "target_scopes": [
                {"namespace": "test-social-network", "target_entity_id": "deployment/a"},
                {"namespace": "test-social-network", "target_entity_id": "deployment/b"},
            ],
        }, "爆炸半径"),
        ({"duration_s": 121}, "duration_s"),
    ],
)
def test_guarded_injection_rejects_unsafe_scope(overrides, message) -> None:
    run = SkillExecutor.from_dir(SKILLS).run(
        "k8s.guarded_fault_injection", _guarded_payload(**overrides), dry_run=True,
    ).to_dict()
    output = run["stages"]["diagnose"]["output"]
    assert run["stages"]["diagnose"]["status"] == "blocked"
    assert message in " ".join(output["blockers"])
    assert run["stages"]["heal"]["output"]["suggestions"] == []


def test_workflow_preserves_typed_operations_without_turning_them_into_commands() -> None:
    workflow = SkillWorkflow(SkillExecutor.from_dir(SKILLS))
    result = workflow.run(
        _guarded_payload(candidate_skills=["k8s.guarded_fault_injection"]),
        stages=("detect", "diagnose", "heal", "verify"),
        domain="k8s",
        dry_run=True,
        allow_legacy_fallback=False,
    )

    assert result["execution_plan"]["command_count"] == 0
    assert result["execution_plan"]["operation_count"] == 1
    assert result["execution_plan"]["operations"][0]["operation"] == "agenticsre.k8s.fault.inject"
    assert result["capability"]["requires_approval"] is True
    assert result["capability"]["supports_rollback"] is True


def test_transactional_self_healing_reuses_only_exact_approved_undo() -> None:
    executor = SkillExecutor.from_dir(SKILLS)
    guarded = executor.run("k8s.guarded_fault_injection", _guarded_payload(), dry_run=True).to_dict()
    undo = guarded["stages"]["heal"]["output"]["suggestions"][0]["rollback"]
    payload = {
        "operation": "transactional_self_healing",
        "incident_id": "incident-1",
        "scenario_id": "sn-svc-scale-down",
        "registered_scenario": True,
        "cluster_id": "",
        "namespace": "test-social-network",
        "target_entity_ids": ["deployment/home-timeline-service"],
        "target_scopes": [{
            "namespace": "test-social-network",
            "target_entity_id": "deployment/home-timeline-service",
        }],
        "diagnosis": {
            "fault_type": "service_disruption",
            "root_cause": "replica count was reduced",
            "confidence": 0.96,
        },
        "remediation_strategy": {
            "id": "restore_replica_count",
            "name": "恢复故障前副本数",
            "execution_mode": "exact_approved_transaction_undo",
        },
        "checkpoint": {"summary": {"ready_replicas": 1, "endpoint_count": 1}},
        "permitted_undo_action": undo,
        "authorization_basis": "approved_fault_transaction_undo",
        "max_recovery_s": 60,
    }
    run = executor.run("k8s.transactional_self_healing", payload, dry_run=True).to_dict()
    action = run["stages"]["heal"]["output"]["suggestions"][0]

    assert action["operation"] == "agenticsre.k8s.remediation.execute_undo"
    assert action["parameters"]["undo_action"] == undo
    assert action["requires_approval"] is False
    assert "command" not in action

    payload["permitted_undo_action"] = {
        **undo,
        "parameters": {**undo["parameters"], "namespace": "another-namespace"},
    }
    blocked = executor.run("k8s.transactional_self_healing", payload, dry_run=True).to_dict()
    assert blocked["stages"]["diagnose"]["status"] == "blocked"
    assert "作用域不一致" in " ".join(blocked["stages"]["diagnose"]["output"]["blockers"])


def test_transactional_self_healing_accepts_exact_precision_undo() -> None:
    payload = {
        "operation": "transactional_self_healing",
        "incident_id": "precision-recovery-1",
        "scenario_id": "precise-dcgm-pcie-replay",
        "registered_scenario": True,
        "cluster_id": "",
        "namespace": "agenticsre-faultlab",
        "target_entity_ids": ["gpu/isolated-gpu-0"],
        "target_scopes": [{
            "namespace": "agenticsre-faultlab",
            "target_entity_id": "gpu/isolated-gpu-0",
        }],
        "diagnosis": {
            "fault_type": "dcgm_metric_error",
            "root_cause": "registered precision experiment",
            "root_cause_component": "gpu/isolated-gpu-0",
            "confidence": 0.99,
        },
        "remediation_strategy": {
            "id": "execute_transaction_undo",
            "name": "执行原细粒度 Undo",
            "execution_mode": "exact_approved_transaction_undo",
        },
        "checkpoint": {"summary": {"validation": "PASS cleanup=complete"}},
        "permitted_undo_action": {
            "operation": "agenticsre.precision.cleanup",
            "parameters": {
                "scenario_id": "precise-dcgm-pcie-replay",
                "cluster_id": "",
                "namespace": "agenticsre-faultlab",
                "target_entity_ids": ["gpu/isolated-gpu-0"],
                "target_scopes": [{
                    "namespace": "agenticsre-faultlab",
                    "target_entity_id": "gpu/isolated-gpu-0",
                }],
                "registered_cleanup_only": True,
            },
        },
        "authorization_basis": "approved_fault_transaction_undo",
        "max_recovery_s": 120,
    }
    workflow = SkillWorkflow(SkillExecutor.from_dir(SKILLS))
    result = workflow.run(
        {**payload, "candidate_skills": ["k8s.transactional_self_healing"]},
        stages=("detect", "diagnose", "heal", "verify"),
        domain="k8s",
        dry_run=True,
        allow_legacy_fallback=False,
    )

    action = result["execution_plan"]["operations"][0]
    assert action["operation"] == "agenticsre.k8s.remediation.execute_undo"
    assert action["parameters"]["undo_action"]["operation"] == "agenticsre.precision.cleanup"
    assert action["requires_approval"] is False


def test_precision_catalog_has_18_scenarios_across_12_methods() -> None:
    data = yaml.safe_load((ROOT / "configs" / "precision_fault_scenarios.yaml").read_text(encoding="utf-8"))
    scenarios = data["scenarios"]
    methods = {item["precision"]["method"] for item in scenarios}

    assert len(scenarios) == 18
    assert len(methods) == 12
    assert data["execution_mode"] == "approval_gated_transactional_execution"
    assert "command" not in str(data).lower()


def test_every_precision_scenario_builds_a_bounded_contract() -> None:
    data = yaml.safe_load((ROOT / "configs" / "precision_fault_scenarios.yaml").read_text(encoding="utf-8"))
    for scenario in data["scenarios"]:
        contract = build_precision_contract(
            {"precision": {**scenario["precision"], "safety": scenario["safety"]}},
            target_entity_ids=scenario["target_entity_ids"],
            fault_type=scenario["fault_type"],
            duration_s=scenario["safety"]["duration_s"],
            blast_radius_budget=scenario["blast_radius_budget"],
        )
        assert contract["execution"]["mode"] == "approval_gated_transactional_adapter"
        assert contract["safety"]["auto_cleanup"] is True
        assert contract["safety"]["requires_approval"] is True
        assert contract["scope"]["target_entity_ids"] == scenario["target_entity_ids"]


def test_precision_contract_rejects_commands_scope_drift_and_wildcards() -> None:
    with pytest.raises(PrecisionInjectionError, match="commands and scripts"):
        build_precision_contract(
            {"precision": {"method": "request", "command": "anything"}},
            target_entity_ids=["service/frontend"],
            fault_type="http_header_delay",
            duration_s=15,
            blast_radius_budget={"max_affected_entities": 1},
        )
    with pytest.raises(PrecisionInjectionError, match="exactly match"):
        build_precision_contract(
            {"precision": {
                "method": "request",
                "scope": {"target_entity_ids": ["service/other"]},
                "match": {"route": "/healthz"},
                "fault": {"action": "delay", "delay_ms": 10},
            }},
            target_entity_ids=["service/frontend"],
            fault_type="http_header_delay",
            duration_s=15,
            blast_radius_budget={"max_affected_entities": 1},
        )
    with pytest.raises(PrecisionInjectionError, match="wildcards"):
        build_precision_contract(
            {"precision": {
                "method": "request",
                "match": {"route": "/checkout/*"},
                "fault": {"action": "delay", "delay_ms": 10},
            }},
            target_entity_ids=["service/frontend"],
            fault_type="http_header_delay",
            duration_s=15,
            blast_radius_budget={"max_affected_entities": 1},
        )


def test_faultlab_apis_expose_guarded_plans_and_block_unapproved_execution(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(dashboard, "_shell_sync", lambda *args, **kwargs: calls.append((args, kwargs)))
    client = TestClient(dashboard.app)

    catalog = client.get("/api/faultlab/precision-scenarios")
    assert catalog.status_code == 200
    assert catalog.json()["scenario_count"] == 18
    assert len(catalog.json()["method_counts"]) == 12
    assert catalog.json()["adapter_ready_count"] == 18
    assert catalog.json()["validation_status"] == "remote_real_validation_17_passed_1_gpu_capacity_blocked"

    precise = client.post(
        "/api/faultlab/precision/plan/precise-seccomp-openat-errno",
        json={"dry_run": True, "namespace": "agenticsre-faultlab"},
    )
    assert precise.status_code == 200
    precise_action = precise.json()["guarded_plan"]["action"]
    assert precise_action["parameters"]["precision"]["method"] == "seccomp"
    assert precise.json()["guarded_plan"]["real_execution_ready"] is True

    real_plan = client.post(
        "/api/faultlab/precision/plan/precise-seccomp-openat-errno",
        json={"dry_run": False, "namespace": "agenticsre-faultlab"},
    )
    assert real_plan.status_code == 200
    assert real_plan.json()["status"] == "planned"

    preview = client.post("/api/faultlab/inject/sn-svc-scale-down", json={"dry_run": True})
    assert preview.status_code == 200
    assert preview.json()["guarded_plan"]["ready"] is True
    denied_inject = client.post("/api/faultlab/inject/sn-svc-scale-down", json={"dry_run": False})
    assert denied_inject.status_code == 400
    assert calls == []


def test_all_precision_scenarios_have_fixed_transactional_adapters() -> None:
    data = yaml.safe_load((ROOT / "configs" / "precision_fault_scenarios.yaml").read_text(encoding="utf-8"))
    scenario_ids = {item["id"] for item in data["scenarios"]}

    assert set(PRECISION_ADAPTERS) == scenario_ids
    assert len(precision_adapter_catalog()) == 18
    for scenario_id in sorted(scenario_ids):
        result = execute_precision_adapter(scenario_id, root_dir=ROOT, dry_run=True)
        assert result["status"] == "dry_run"
        assert result["adapter"]["real_execution_ready"] is True
        assert result["transaction"]["auto_cleanup"] is True
        assert result["transaction"]["stages"][-1] == "residue_check"


def test_every_precision_scenario_builds_exact_transactional_heal_plan() -> None:
    namespace = "agenticsre-faultlab"
    for scenario in dashboard._load_precision_fault_scenarios():
        source = {
            "id": f"source-{scenario['id']}",
            "action": "precision_experiment",
            "status": "completed",
            "scenario_id": scenario["id"],
            "cluster_id": "",
            "namespace": namespace,
            "target_entity_ids": scenario["target_entity_ids"],
            "target_scopes": dashboard._precision_target_scopes(scenario, namespace),
            "transaction_undo": dashboard._precision_transaction_undo(
                scenario, cluster_id=None, namespace=namespace,
            ),
            "results": [{
                "status": "succeeded",
                "evidence": {
                    "before_evidence": "baseline captured",
                    "recovery_evidence": "baseline restored",
                    "validation": "PASS effect=true recovery=true cleanup=complete",
                },
                "verification": {
                    "validation": "PASS effect=true recovery=true cleanup=complete",
                    "cleanup_complete": True,
                },
            }],
        }
        plan = dashboard._precision_transactional_heal_plan(
            scenario,
            source,
            cluster_id=None,
            namespace=namespace,
        )

        assert plan["ready"] is True, scenario["id"]
        assert list(plan["run"]["stages"]) == ["detect", "diagnose", "heal", "verify"]
        assert plan["action"]["parameters"]["undo_action"] == source["transaction_undo"]
        assert plan["source_recovery_already_completed"] is True


def test_precision_adapter_real_path_requires_approval_and_pass_evidence() -> None:
    scenario_id = "precise-application-failpoint-error"
    with pytest.raises(Exception, match="confirm=true"):
        execute_precision_adapter(
            scenario_id, root_dir=ROOT, dry_run=False, experiment_ack=True,
        )

    captured = {}

    def runner(argv, **kwargs):
        import subprocess

        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=(
                "before_evidence=healthy\n"
                "fault_evidence=target_failed control_ok\n"
                "recovery_evidence=restored\n"
                "validation=PASS effect=true recovery=true cleanup=complete\n"
            ),
            stderr="",
        )

    result = execute_precision_adapter(
        scenario_id,
        root_dir=ROOT,
        dry_run=False,
        confirmed=True,
        experiment_ack=True,
        runner=runner,
    )
    assert result["status"] == "succeeded"
    assert result["verification"]["cleanup_complete"] is True
    assert captured["argv"] == [str(ROOT / "scripts" / "validate_application_failpoint_precision.py")]
    assert "shell" not in captured["kwargs"]


def test_faultlab_transactional_heal_requires_persisted_approved_injection(monkeypatch) -> None:
    client = TestClient(dashboard.app)
    blocked = client.post("/api/faultlab/heal/sn-svc-scale-down", json={})
    assert blocked.status_code == 200
    assert blocked.json()["status"] == "blocked"

    monkeypatch.setattr(dashboard, "_load_fault_runs", lambda: [{
        "id": "fault-approved",
        "action": "inject",
        "status": "executed",
        "scenario_id": "sn-svc-scale-down",
        "cluster_id": "",
        "namespace": "test-social-network",
        "target_entity_ids": ["deployment/home-timeline-service"],
        "target_scopes": [{
            "namespace": "test-social-network",
            "target_entity_id": "deployment/home-timeline-service",
        }],
        "transaction_undo": {
            "operation": "agenticsre.k8s.fault.cleanup",
            "parameters": {
                "scenario_id": "sn-svc-scale-down",
                "cluster_id": "",
                "namespace": "test-social-network",
                "target_entity_ids": ["deployment/home-timeline-service"],
                "target_scopes": [{
                    "namespace": "test-social-network",
                    "target_entity_id": "deployment/home-timeline-service",
                }],
            },
        },
    }])
    ready = client.post(
        "/api/faultlab/heal/sn-svc-scale-down",
        json={
            "incident_id": "incident-1",
            "fault_run_id": "fault-approved",
            "diagnosis": {
                "fault_type": "service_disruption",
                "root_cause": "home-timeline-service replica count is zero",
                "root_cause_component": "deployment/home-timeline-service",
                "confidence": 0.96,
            },
            "checkpoint": {"summary": {"ready_replicas": 1, "endpoint_count": 1}},
        },
    )
    assert ready.status_code == 200
    result = ready.json()
    assert result["status"] == "planned"
    assert result["transactional_heal"]["action"]["operation"] == "agenticsre.k8s.remediation.execute_undo"
    assert result["transactional_heal"]["source_fault_run_id"] == "fault-approved"
    assert result["execution_requirements"] == ["confirm=true"]


def test_precision_transactional_heal_requires_exact_successful_source(monkeypatch) -> None:
    scenario = next(
        item for item in dashboard._load_precision_fault_scenarios()
        if item["id"] == "precise-application-failpoint-error"
    )
    namespace = "agenticsre-faultlab"
    source = {
        "id": "precision-approved",
        "action": "precision_experiment",
        "status": "completed",
        "scenario_id": scenario["id"],
        "cluster_id": "",
        "namespace": namespace,
        "target_entity_ids": scenario["target_entity_ids"],
        "target_scopes": dashboard._precision_target_scopes(scenario, namespace),
        "transaction_undo": dashboard._precision_transaction_undo(
            scenario, cluster_id=None, namespace=namespace,
        ),
        "results": [{
            "status": "succeeded",
            "evidence": {
                "before_evidence": "healthy",
                "recovery_evidence": "restored",
                "validation": "PASS effect=true recovery=true cleanup=complete",
            },
            "verification": {
                "validation": "PASS effect=true recovery=true cleanup=complete",
                "cleanup_complete": True,
            },
        }],
    }
    records = []
    monkeypatch.setattr(dashboard, "_load_fault_runs", lambda: [source])
    monkeypatch.setattr(dashboard, "_append_fault_run", lambda record: records.append(record))
    monkeypatch.setattr(
        dashboard,
        "_precision_cleanup_resources",
        lambda *_args, **_kwargs: {"status": "executed", "resources": [], "results": []},
    )
    monkeypatch.setattr(
        dashboard,
        "_precision_residue_check",
        lambda *_args, **_kwargs: {"status": "passed", "passed": True, "checks": []},
    )
    client = TestClient(dashboard.app)

    blocked = client.post(f"/api/faultlab/precision/heal/{scenario['id']}", json={"dry_run": False})
    assert blocked.status_code == 400

    source["results"][0]["verification"]["validation"] = ""
    unverified = client.post(
        f"/api/faultlab/precision/heal/{scenario['id']}",
        json={"dry_run": True, "fault_run_id": source["id"], "namespace": namespace},
    )
    assert unverified.status_code == 400
    source["results"][0]["verification"]["validation"] = "PASS effect=true recovery=true cleanup=complete"

    planned = client.post(
        f"/api/faultlab/precision/heal/{scenario['id']}",
        json={"dry_run": True, "fault_run_id": source["id"], "namespace": namespace},
    )
    assert planned.status_code == 200
    assert planned.json()["status"] == "planned"
    assert planned.json()["transactional_heal"]["action"]["parameters"]["undo_action"] == source["transaction_undo"]

    executed = client.post(
        f"/api/faultlab/precision/heal/{scenario['id']}",
        json={"dry_run": False, "fault_run_id": source["id"], "namespace": namespace},
    )
    assert executed.status_code == 200
    result = executed.json()
    assert result["status"] == "already_recovered"
    assert result["recovery"]["passed"] is True
    assert result["recovery"]["source_recovery_already_completed"] is True
    assert records[0]["action"] == "precision_heal"
    assert records[0]["source_fault_run_id"] == source["id"]


def test_multi_target_registered_scenario_keeps_exact_scope() -> None:
    scenario = next(
        item for item in dashboard._load_fault_scenarios()
        if item["id"] == "sn-staged-write-cascade"
    )
    targets = dashboard._faultlab_target_entity_ids(scenario)
    plan = dashboard._faultlab_guarded_skill_plan(
        scenario,
        cluster_id=None,
        namespace="test-social-network",
    )
    assert targets == ["deployment/post-storage-mongodb", "deployment/compose-post-service"]
    assert plan["action"]["parameters"]["target_entity_ids"] == targets


def test_cross_namespace_scenario_keeps_each_target_namespace() -> None:
    scenario = next(
        item for item in dashboard._load_fault_scenarios()
        if item["id"] == "sn-observability-blindspot"
    )
    scopes = dashboard._faultlab_target_scopes(scenario, "agenticsre-remote-e2e")
    plan = dashboard._faultlab_guarded_skill_plan(
        scenario,
        cluster_id=None,
        namespace="agenticsre-remote-e2e",
    )

    assert scopes == [
        {
            "namespace": "agenticsre-faultlab-observe",
            "target_entity_id": "deployment/kube-prom-kube-state-metrics",
        },
        {
            "namespace": "agenticsre-remote-e2e",
            "target_entity_id": "deployment/compose-post-service",
        },
    ]
    assert plan["action"]["parameters"]["target_scopes"] == scopes
    assert plan["rollback"]["parameters"]["target_scopes"] == scopes


def test_frontend_exposes_precision_catalog_and_plan_actions() -> None:
    html = (ROOT / "web_app" / "templates" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "web_app" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert 'data-faultlab-tab="precision"' in html
    assert 'id="faultlab-tab-precision"' in html
    assert 'id="precisionfaultlab-cluster"' in html
    assert 'id="precisionfaultlab-output"' in html
    assert "openPrecisionFaultLabView()" in html
    assert "/api/faultlab/precision-scenarios" in js
    assert "/api/faultlab/precision/plan/" in js
    assert "/api/faultlab/precision/preflight/" in js
    assert "/api/faultlab/precision/experiment/" in js
    assert "/api/faultlab/precision/heal/" in js
    assert "/api/faultlab/precision/cleanup/" in js
    assert "/api/faultlab/precision/verify/" in js
    assert "function precisionFaultLabBaseBody()" in js
    assert "function healPrecisionFaultScenario(scenarioId)" in js
    assert "dry_run: true" in js


def test_real_injection_record_is_server_owned_and_reusable_for_exact_undo(monkeypatch) -> None:
    records = []

    def fake_shell(command, *_args, **_kwargs):
        if "get pods -o json" in command:
            return {"ok": True, "stdout": '{"items": []}', "stderr": "", "returncode": 0}
        return {"ok": True, "stdout": "ok", "stderr": "", "returncode": 0, "command": command}

    monkeypatch.setattr(dashboard, "_shell_sync", fake_shell)
    monkeypatch.setattr(dashboard, "_append_fault_run", lambda record: records.append(record))
    client = TestClient(dashboard.app)
    response = client.post(
        "/api/faultlab/inject/sn-svc-scale-down",
        json={"dry_run": False, "confirm": True, "experiment_ack": True},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["fault_run_id"].startswith("fault-")
    assert result["transaction_undo"]["operation"] == "agenticsre.k8s.fault.cleanup"
    assert len(records) == 1
    assert records[0]["id"] == result["fault_run_id"]
    assert records[0]["target_entity_ids"] == ["deployment/home-timeline-service"]
    assert records[0]["transaction_undo"] == result["transaction_undo"]


def test_cleanup_record_links_to_source_fault_run(monkeypatch) -> None:
    records = []
    monkeypatch.setattr(
        dashboard,
        "_shell_sync",
        lambda command, *_args, **_kwargs: {
            "ok": True,
            "stdout": "ok",
            "stderr": "",
            "returncode": 0,
            "command": command,
        },
    )
    monkeypatch.setattr(dashboard, "_append_fault_run", lambda record: records.append(record))
    client = TestClient(dashboard.app)
    response = client.post(
        "/api/faultlab/cleanup/sn-svc-scale-down",
        json={"dry_run": False, "confirm": True, "fault_run_id": "fault-source"},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["source_fault_run_id"] == "fault-source"
    assert len(records) == 1
    assert records[0]["action"] == "cleanup"
    assert all(item["source_fault_run_id"] == "fault-source" for item in records[0]["results"])


def test_real_endpoints_report_registered_command_failures(monkeypatch) -> None:
    records = []
    monkeypatch.setattr(
        dashboard,
        "_shell_sync",
        lambda command, *_args, **_kwargs: {
            "ok": False,
            "stdout": "",
            "stderr": "expected test failure",
            "returncode": 1,
            "command": command,
        },
    )
    monkeypatch.setattr(dashboard, "_append_fault_run", lambda record: records.append(record))
    client = TestClient(dashboard.app)

    injected = client.post(
        "/api/faultlab/inject/sn-svc-scale-down",
        json={"dry_run": False, "confirm": True, "experiment_ack": True},
    )
    cleaned = client.post(
        "/api/faultlab/cleanup/sn-svc-scale-down",
        json={"dry_run": False, "confirm": True},
    )

    assert injected.status_code == 200
    assert injected.json()["status"] == "failed"
    assert cleaned.status_code == 200
    assert cleaned.json()["status"] == "failed"
    assert [record["status"] for record in records] == ["failed", "failed"]


def test_every_registered_scenario_completes_simulated_transactional_flow(monkeypatch) -> None:
    records = []

    def fake_shell(command, *_args, **_kwargs):
        stdout = '{"items": []}' if "get pods -o json" in command else "ok"
        return {"ok": True, "stdout": stdout, "stderr": "", "returncode": 0, "command": command}

    monkeypatch.setattr(dashboard, "_shell_sync", fake_shell)
    monkeypatch.setattr(dashboard, "_append_fault_run", lambda record: records.insert(0, record))
    monkeypatch.setattr(dashboard, "_load_fault_runs", lambda: records)
    client = TestClient(dashboard.app)

    for scenario in dashboard._load_fault_scenarios():
        scenario_id = scenario["id"]
        namespace = scenario["inject"]["namespace"]
        preview = client.post(
            f"/api/faultlab/inject/{scenario_id}",
            json={"dry_run": True, "namespace": namespace},
        )
        assert preview.status_code == 200, scenario_id
        assert preview.json()["guarded_plan"]["ready"] is True, scenario_id

        injected = client.post(
            f"/api/faultlab/inject/{scenario_id}",
            json={
                "dry_run": False,
                "confirm": True,
                "experiment_ack": True,
                "namespace": namespace,
            },
        )
        assert injected.status_code == 200, scenario_id
        injection = injected.json()
        assert injection["status"] == "executed", scenario_id

        healed = client.post(
            f"/api/faultlab/heal/{scenario_id}",
            json={
                "incident_id": f"incident-{scenario_id}",
                "fault_run_id": injection["fault_run_id"],
                "namespace": namespace,
                "diagnosis": {
                    "fault_type": scenario["expected"]["fault_type"],
                    "root_cause": f"verified registered fault on {scenario['target_service']}",
                    "root_cause_component": dashboard._faultlab_target_entity_ids(scenario)[0],
                    "confidence": 0.96,
                },
                "checkpoint": {"summary": {"ready": True, "endpoint_count": 1}},
            },
        )
        assert healed.status_code == 200, scenario_id
        assert healed.json()["status"] == "planned", scenario_id

        cleaned = client.post(
            f"/api/faultlab/cleanup/{scenario_id}",
            json={
                "dry_run": False,
                "confirm": True,
                "fault_run_id": injection["fault_run_id"],
                "namespace": namespace,
            },
        )
        assert cleaned.status_code == 200, scenario_id
        assert cleaned.json()["status"] == "executed", scenario_id


def test_every_precision_scenario_is_available_through_reviewed_adapter_api() -> None:
    client = TestClient(dashboard.app)
    catalog = client.get("/api/faultlab/precision-scenarios").json()

    for scenario in catalog["scenarios"]:
        response = client.post(
            f"/api/faultlab/precision/plan/{scenario['id']}",
            json={"dry_run": True, "namespace": "agenticsre-faultlab"},
        )
        assert response.status_code == 200, scenario["id"]
        result = response.json()
        assert result["guarded_plan"]["ready"] is True, scenario["id"]
        assert result["guarded_plan"]["real_execution_ready"] is True, scenario["id"]
        preview = client.post(
            f"/api/faultlab/precision/experiment/{scenario['id']}",
            json={"dry_run": True, "namespace": "agenticsre-faultlab"},
        )
        assert preview.status_code == 200, scenario["id"]
        assert preview.json()["status"] == "dry_run", scenario["id"]


def test_scheduling_capacity_probe_is_not_pinned_to_a_node(monkeypatch) -> None:
    scenario = next(
        item for item in dashboard._load_fault_scenarios()
        if item["id"] == "sn-scaleout-capacity-failure"
    )
    monkeypatch.setattr(dashboard, "_find_target_node", lambda *_args, **_kwargs: "worker-a")

    commands = dashboard._prepare_faultlab_commands(
        scenario,
        cluster_id=None,
        namespace_override=None,
        commands_key="commands",
        background_load=False,
        resolve_target_node=True,
    )

    capacity_command = next(item for item in commands if "run compose-post-overrequest" in item)
    assert "nodeName" not in capacity_command
