#!/usr/bin/env python3
"""Regression tests for the standalone skill runtime MVP."""

from __future__ import annotations

import pathlib
import asyncio
import json
import os
import subprocess
import sys

from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skill_runtime.app import app, executor  # noqa: E402
from skill_runtime.detail import skill_contract_status  # noqa: E402
from skill_runtime.executor import SkillExecutor  # noqa: E402
from skill_runtime.frontmatter import split_frontmatter  # noqa: E402
from skill_runtime.registry import SkillRegistry  # noqa: E402
from skill_runtime.workflow import SkillWorkflow  # noqa: E402
from integrations.aishperf.catalog import AISHPerfCatalog  # noqa: E402
from orchestrator.skill_agent import SkillAgent, SkillAgentRequest  # noqa: E402
from orchestrator.skill_bridge import AgentSkillBridge, build_skill_payload, summarize_skill_result  # noqa: E402
from tools import build_tool_registry  # noqa: E402


class FakeSkillLLM:
    def __init__(self, *args, **kwargs):
        pass

    def json_chat(self, messages, temperature=None, max_tokens=None):
        content = messages[-1]["content"]
        if "skill_diagnose_stages" in content:
            return {"root_cause": "Service 无可用 Endpoint，需结合当前只读结果定位。", "confidence": 0.7, "reasoning_chain": "当前证据与专项 Skill 一致"}
        if "skill_catalog" in content:
            if "resource_pressure" in content and "t4-vllm-k8s" in content:
                return {"skill_calls": [{"skill_id": "llm.k8s_vllm_observability", "stages": ["detect", "diagnose"]}]}
            return {
                "skill_calls": [
                    {
                        "skill_id": "k8s.service_unavailable",
                        "stages": ["detect", "diagnose"],
                        "reason": "服务不可用观测证据匹配该 Skill",
                    }
                ]
            }
        return {
            "hypotheses": [
                {
                    "id": "H1",
                    "description": "LLM 根据观测证据判断目标服务后端或资源链路异常。",
                    "confidence": 0.84,
                    "service": "target",
                    "fault_type": "service_unavailable",
                    "expected_evidence": ["检查 endpoints", "检查 Pod Ready"],
                    "investigation_plan": ["kubectl get pods,svc,endpoints -o wide"],
                    "supporting_evidence": ["当前告警和历史规则均指向服务链路"],
                }
            ]
        }


def _fixture_kubectl_runner(command, namespace, payload):
    return {"ok": True, "stdout": "NAME ENDPOINTS\nfrontend <none>", "returncode": 0}


def _fixture_image_tool(tool, arguments, payload):
    return {"success": True, "executed": True, "data": json.dumps([{
        "name": "api", "image": "repo/api:bad", "state": {"waiting": {
            "reason": "ImagePullBackOff", "message": "registry image not found"}}}])}


def test_skill_markdown_frontmatter_parser() -> None:
    metadata, body = split_frontmatter(
        """---
id: demo.md_only
name: Markdown Skill
domain: k8s
stages: [detect, diagnose]
---
# Markdown Skill

## 适用场景
"""
    )

    assert metadata["id"] == "demo.md_only"
    assert metadata["stages"] == ["detect", "diagnose"]
    assert body.startswith("# Markdown Skill")


def test_builtin_skill_stage_contracts_are_role_specific() -> None:
    registry = SkillRegistry(ROOT / "skill_runtime" / "skills").load()
    definitions = {item.id: item for item in registry.list()}

    assert len(definitions) == 62
    assert definitions['k8s.job_failed'].metadata.stages == ['detect', 'diagnose', 'verify']
    assert definitions["generic.fault_detection"].metadata.stages == ["detect"]
    assert definitions["generic.root_cause_diagnosis"].metadata.stages == ["diagnose"]
    assert definitions["generic.healing_planner"].metadata.stages == ["heal"]
    assert definitions["generic.recovery_verifier"].metadata.stages == ["verify"]
    assert definitions["generic.risk_gatekeeper"].metadata.stages == ["diagnose", "heal"]
    assert definitions["llm.business_slo"].metadata.stages == ["detect", "diagnose", "verify"]
    assert all("heal" not in item.metadata.stages for item in definitions.values() if item.id.startswith("tool."))
    assert any(item.metadata.stages != ["detect", "diagnose", "heal", "verify"] for item in definitions.values())


def test_executor_reports_only_declared_stage_capabilities() -> None:
    runtime = SkillExecutor.from_dir(ROOT / "skill_runtime" / "skills")
    run = runtime.run(
        "generic.fault_detection",
        {"message": "frontend service unavailable", "fault_type": "service_unavailable"},
        stages=["detect", "diagnose", "heal", "verify"],
        dry_run=True,
    ).to_dict()

    assert run["declared_stages"] == ["detect"]
    assert run["implemented_stages"] == ["detect"]
    assert run["attempted_stages"] == ["detect"]
    assert run["executed_stages"] == ["detect"]
    assert run["policy"]["supports_heal"] is False
    assert "heal_stage_not_declared" in run["policy"]["heal_blockers"]


def test_registry_loads_standard_markdown_skill_with_yaml_manifest(tmp_path: pathlib.Path) -> None:
    skill_dir = tmp_path / "md_only"
    skill_dir.mkdir()
    (skill_dir / "skill.yaml").write_text(
        """
id: demo.md_only
version: 1.0.0
name: YAML Skill Name
domain: k8s
stages: [detect, diagnose]
description: Runtime manifest description
policy:
  requires_approval: true
""",
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text(
        """---
name: Markdown Only Skill
description: Documentation-only skill
---
# Markdown Only Skill

## Purpose
用于验证 Markdown-first Skill 可以被目录加载。

## Inputs
告警和指标。

## Workflow
detect / diagnose。

## Safety Rules
只读。

## Validation Checklist
确认运行阶段被标记为 skipped。
""",
        encoding="utf-8",
    )

    registry = SkillRegistry(tmp_path).load()
    definition = registry.get("demo.md_only")
    run = SkillExecutor(registry).run("demo.md_only", {"message": "test"}, dry_run=True).to_dict()

    assert definition.module is None
    assert definition.metadata.name == "Markdown Only Skill"
    assert definition.metadata.domain == "k8s"
    assert run["status"] == "completed"
    assert run["stages"]["detect"]["status"] == "skipped"
    assert "文档型 Skill" in run["stages"]["detect"]["summary"]


def test_markdown_frontmatter_does_not_override_runtime_manifest(tmp_path: pathlib.Path) -> None:
    skill_dir = tmp_path / "override"
    skill_dir.mkdir()
    (skill_dir / "skill.yaml").write_text(
        """
id: demo.override
name: YAML Name
domain: generic
stages: [detect]
description: from yaml
matching:
  keywords: [yaml-keyword]
""",
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text(
        """---
name: Markdown Name
description: Markdown description for external agents
---
# Markdown Name

## Purpose
vLLM 故障。

## Inputs
Prometheus 指标。

## Workflow
detect / diagnose。

## Safety Rules
只读。

## Validation Checklist
检查输出。
""",
        encoding="utf-8",
    )
    (skill_dir / "skill.py").write_text(
        "def detect(context):\n    return {'status': 'matched', 'confidence': 0.8, 'summary': 'ok'}\n",
        encoding="utf-8",
    )

    registry = SkillRegistry(tmp_path).load()
    definition = registry.get("demo.override")

    assert definition.metadata.name == "Markdown Name"
    assert definition.metadata.description == "Markdown description for external agents"
    assert definition.metadata.domain == "generic"
    assert definition.metadata.stages == ["detect"]
    assert definition.metadata.matching_keywords == ["yaml-keyword"]
    assert not any("overrides skill.yaml" in item for item in definition.warnings)


def test_markdown_runtime_frontmatter_fields_are_ignored(tmp_path: pathlib.Path) -> None:
    skill_dir = tmp_path / "runtime_leak"
    skill_dir.mkdir()
    (skill_dir / "skill.yaml").write_text(
        """
id: demo.runtime_leak
name: YAML Name
domain: generic
stages: [detect]
description: from yaml
matching:
  keywords: [yaml-keyword]
""",
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text(
        """---
name: Markdown Name
description: Markdown description
domain: llm
stages: [diagnose]
matching:
  keywords: [vllm, gpu]
---
# Markdown Name

## Purpose
验证 SKILL.md 运行时字段不会影响执行。

## Inputs
告警。

## Workflow
只读检测。

## Safety Rules
不执行变更。

## Validation Checklist
检查 manifest 未被覆盖。
""",
        encoding="utf-8",
    )

    definition = SkillRegistry(tmp_path).load().get("demo.runtime_leak")

    assert definition.metadata.name == "Markdown Name"
    assert definition.metadata.domain == "generic"
    assert definition.metadata.stages == ["detect"]
    assert definition.metadata.matching_keywords == ["yaml-keyword"]
    assert any("runtime manifest fields ignored" in item for item in definition.warnings)


def test_registry_isolates_broken_skill_entrypoint(tmp_path: pathlib.Path) -> None:
    good_dir = tmp_path / "good"
    bad_dir = tmp_path / "bad"
    good_dir.mkdir()
    bad_dir.mkdir()
    (good_dir / "skill.yaml").write_text(
        "id: demo.good\nname: Good\ndomain: k8s\nstages: [detect]\n",
        encoding="utf-8",
    )
    (good_dir / "skill.py").write_text(
        "def detect(context):\n    return {'status': 'matched', 'confidence': 0.9}\n",
        encoding="utf-8",
    )
    (bad_dir / "skill.yaml").write_text(
        "id: demo.bad\nname: Bad\ndomain: k8s\nstages: [detect]\n",
        encoding="utf-8",
    )
    (bad_dir / "skill.py").write_text("raise RuntimeError('boom during import')\n", encoding="utf-8")

    registry = SkillRegistry(tmp_path).load()

    assert registry.get("demo.good").module is not None
    bad = registry.get("demo.bad")
    assert bad.module is None
    assert any("failed to load entrypoint" in item for item in bad.warnings)


def test_run_matching_does_not_execute_unmatched_heal(tmp_path: pathlib.Path) -> None:
    skill_dir = tmp_path / "unmatched"
    skill_dir.mkdir()
    marker = tmp_path / "heal_marker.txt"
    (skill_dir / "skill.yaml").write_text(
        "id: demo.unmatched\nname: Unmatched\ndomain: k8s\nstages: [detect, heal]\n",
        encoding="utf-8",
    )
    (skill_dir / "skill.py").write_text(
        f"""
def detect(context):
    return {{'status': 'skipped', 'confidence': 0.0}}

def heal(context):
    open({str(marker)!r}, 'w', encoding='utf-8').write('called')
    return {{'status': 'ok', 'confidence': 1.0, 'suggestions': [{{'command': 'echo bad'}}]}}
""",
        encoding="utf-8",
    )

    result = SkillExecutor(SkillRegistry(tmp_path).load()).run_matching(
        {"message": "should not match"},
        domain="k8s",
        dry_run=True,
    )

    assert result["matched_count"] == 0
    assert not marker.exists()


def test_dry_run_blocks_heal_that_reports_execution(tmp_path: pathlib.Path) -> None:
    skill_dir = tmp_path / "dryrun_violation"
    skill_dir.mkdir()
    (skill_dir / "skill.yaml").write_text(
        "id: demo.dryrun_violation\nname: Dry Run Violation\ndomain: k8s\nstages: [detect, heal]\n",
        encoding="utf-8",
    )
    (skill_dir / "skill.py").write_text(
        """
def detect(context):
    return {'status': 'matched', 'confidence': 0.9}

def heal(context):
    return {'status': 'ok', 'confidence': 0.9, 'executed': True, 'suggestions': [{'command': 'kubectl delete pod x'}]}
""",
        encoding="utf-8",
    )

    run = SkillExecutor(SkillRegistry(tmp_path).load()).run(
        "demo.dryrun_violation",
        {"message": "test"},
        dry_run=True,
    ).to_dict()

    assert run["status"] == "failed"
    assert run["stages"]["heal"]["status"] == "error"
    assert run["stages"]["heal"]["error"] == "dry_run_contract_violation"


def test_registry_loads_initial_skills() -> None:
    ids = set(executor.registry.ids())

    assert "k8s.service_unavailable" in ids
    assert "k8s.pod_crashloop" in ids
    assert "k8s.image_pull_error" in ids
    assert "k8s.node_not_ready" in ids
    assert "k8s.dns_resolution" in ids
    assert "k8s.network_policy_deny" in ids
    assert "k8s.config_missing" in ids
    assert "k8s.volume_mount_failed" in ids
    assert "k8s.hpa_scaling_failed" in ids
    assert "k8s.resource_quota_exceeded" in ids
    assert "k8s.deployment_unhealthy" in ids
    assert "k8s.scheduling_failed" in ids
    assert "k8s.oom_killed" in ids
    assert "k8s.resource_pressure" in ids
    assert "k8s.control_plane_unhealthy" in ids
    assert "k8s.observability_signal" in ids
    assert "llm.vllm_process_crash" in ids
    assert "tool.k8s_readiness" in ids
    assert "tool.db_connection_pool" in ids
    assert "tool.llm_host_readiness" in ids
    assert "llm.k8s_vllm_observability" in ids
    assert "k8s.legacy_recipe_bridge" in ids
    assert "generic.fault_detection" in ids
    assert "generic.evidence_collection" in ids
    assert "generic.root_cause_diagnosis" in ids
    assert "generic.healing_planner" in ids
    assert "generic.recovery_verifier" in ids
    assert "generic.risk_gatekeeper" in ids
    assert "generic.knowledge_retrieval" in ids
    assert "generic.diagnosis_fusion" in ids
    assert "llm.business_slo" in ids


def test_all_runtime_skills_have_complete_markdown_contracts() -> None:
    incomplete = []
    for definition in executor.registry.list():
        status = skill_contract_status(definition)
        if not status["is_complete"]:
            incomplete.append({
                "id": definition.id,
                "label": status["label"],
                "missing_files": status["missing_files"],
                "missing_sections": status["missing_sections"],
                "missing_metadata": status["missing_metadata"],
            })

    assert incomplete == []


def test_catalog_exposes_skill_ui_metadata() -> None:
    skill = executor.registry.get("llm.business_slo")
    assert skill is not None

    metadata = skill.metadata.to_dict()

    assert metadata["icon"] == "activity"
    assert metadata["category"] == "业务 SLO"
    assert metadata["domain"] == "llm"


def test_skill_runtime_detail_api_exposes_docs_and_sources() -> None:
    client = TestClient(app)
    response = client.get("/api/skills/llm.business_slo/detail")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["metadata"]["id"] == "llm.business_slo"
    assert body["markdown_source"] == "SKILL.md"
    assert "智算业务 SLO" in body["markdown"]
    assert "请求成功率" in body["markdown"]
    assert "id: llm.business_slo" in body["yaml"]
    assert "def diagnose" in body["python"]
    assert body["files"]["markdown"] == "SKILL.md"
    assert body["files"]["yaml"] == "skill.yaml"
    assert body["files"]["python"] == "skill.py"
    assert body["source_status"]["has_markdown"] is True
    assert body["source_status"]["source_of_truth"] in {"skill.yaml", "SKILL.md frontmatter"}
    assert body["source_status"]["documentation_status"] == "complete"
    assert body["contract_status"]["label"]
    assert "missing_sections" in body["contract_status"]
    assert "frontmatter" in body
    assert body["source_files"]["markdown"]["exists"] is True
    assert body["source_files"]["markdown"]["truncated"] is False


def test_skill_runtime_detail_api_exposes_vllm_process_skill_markdown() -> None:
    client = TestClient(app)
    response = client.get("/api/skills/llm.vllm_process_crash/detail")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["metadata"]["id"] == "llm.vllm_process_crash"
    assert body["markdown_source"] == "SKILL.md"
    assert "vLLM 进程崩溃" in body["markdown"]
    assert "llm_fault_observable_signals_v1" in body["markdown"]


def test_skill_runtime_rejects_unsafe_run_id() -> None:
    client = TestClient(app)
    response = client.get("/api/workflows/runs/bad%24id")

    assert response.status_code == 400, response.text


def test_aishperf_catalog_exposes_extended_external_scenarios() -> None:
    catalog = AISHPerfCatalog(root="/path/that/does/not/exist")
    cases = catalog.load_cases()

    assert len(cases) >= 20
    assert any("gpu" in case.domains for case in cases)
    assert any("rdma" in case.domains for case in cases)
    assert any("kubernetes" in case.domains for case in cases)
    assert any("ascend" in case.domains for case in cases)
    assert catalog.list_cases(domain="gpu", limit=10)
    assert catalog.list_cases(query="vllm", limit=10)


def test_service_unavailable_skill_full_flow() -> None:
    run = executor.run(
        "k8s.service_unavailable",
        {
            "namespace": "default",
            "fault_type": "ServiceUnavailable",
            "message": "service frontend returns 503 because deployment scaled to zero and no endpoints",
            "service": "frontend",
            "deployment": "frontend",
        },
        dry_run=True,
    ).to_dict()

    assert run["status"] == "completed"
    assert run["stages"]["detect"]["status"] == "matched"
    assert run["stages"]["diagnose"]["confidence"] >= 0.85
    assert run["policy"]["heal_ready"] is True
    assert run["policy"]["auto_heal_ready"] is False
    assert "approval_required" in run["policy"]["auto_heal_blockers"]
    assert run["stages"]["heal"]["output"]["suggestions"]


def test_run_matching_filters_to_matched_skills() -> None:
    result = executor.run_matching(
        {
            "namespace": "default",
            "message": "Pod checkout is in CrashLoopBackOff: Back-off restarting failed container",
            "pod": "checkout-abc",
            "deployment": "checkout",
        },
        domain="k8s",
        dry_run=True,
    )

    ids = {run["skill"]["id"] for run in result["runs"]}
    assert "k8s.pod_crashloop" in ids
    assert "k8s.service_unavailable" not in ids


def test_generic_skills_join_domain_matching_as_auxiliary_layer() -> None:
    result = executor.run_matching(
        {
            "namespace": "default",
            "fault_type": "UnclassifiedTimeout",
            "message": "checkout request timeout and p99 latency degraded",
            "deployment": "checkout",
        },
        domain="k8s",
        dry_run=True,
    )

    ids = {run["skill"]["id"] for run in result["runs"]}
    assert "generic.fault_detection" in ids
    assert "generic.evidence_collection" in ids
    assert "generic.root_cause_diagnosis" in ids
    assert "generic.healing_planner" in ids
    assert "generic.recovery_verifier" in ids


def test_specific_skill_still_wins_over_generic_primary() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "namespace": "default",
            "fault_type": "ServiceUnavailable",
            "message": "service frontend returns 503 because no endpoints",
            "service": "frontend",
            "deployment": "frontend",
        },
        domain="k8s",
        dry_run=True,
    )

    assert result["skill_runtime"]["primary_skill_id"] == "k8s.service_unavailable"
    assert result["migration"]["mode"] == "skill_primary"
    assert any(run["skill"]["id"].startswith("generic.") for run in result["skill_runtime"]["runs"])


def test_helper_skill_does_not_win_primary_diagnosis() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "domain": "llm",
            "fault_type": "business_slo_degradation",
            "message": "vLLM 推理请求变慢，TTFT P95 升高，等待队列积压，成功率下降。",
            "metrics": {
                "success_rate": 0.91,
                "ttft_p95_seconds": 4.2,
                "tpot_p95_seconds": 0.22,
                "e2e_p95_seconds": 12.5,
                "running_requests": 8,
                "waiting_requests": 12,
                "kv_cache_usage": 0.93,
                "gpu_utilization": 0.98,
            },
        },
        domain="llm",
        dry_run=True,
    )

    assert result["skill_runtime"]["primary_skill_id"] == "llm.business_slo"
    assert result["diagnosis"]["summary"].startswith("vLLM")


def test_observability_signal_timeout_defaults_to_read_only_plan() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "namespace": "default",
            "fault_type": "UnclassifiedTimeout",
            "message": "checkout request timeout and p99 latency degraded",
            "deployment": "checkout",
        },
        domain="k8s",
        dry_run=True,
    )

    assert result["skill_runtime"]["primary_skill_id"] == "k8s.observability_signal"
    assert result["execution_plan"]["commands"]
    assert not any("rollout restart" in cmd for cmd in result["execution_plan"]["commands"])
    assert any("describe deployment/checkout" in cmd for cmd in result["execution_plan"]["commands"])


def test_llm_vllm_process_crash_uses_skill_confidence_calibrator() -> None:
    run = executor.run(
        "llm.vllm_process_crash",
        {
            "fault_type": "process_crash",
            "message": "vLLM process crash and connection refused",
            "endpoint_status": "failed",
            "metrics_status": "ok",
            "process_count": 0,
            "gpu_count": 3,
            "metrics_count": 8,
            "waiting_requests": 0,
            "running_requests": 0,
            "deployment": "vllm-server",
            "namespace": "llm-test",
            "desired_replicas": 1,
            "endpoint": "http://vllm-server.llm-test:8000/health",
        },
        dry_run=False,
    ).to_dict()

    diagnosis = run["stages"]["diagnose"]
    assert diagnosis["status"] == "ok"
    assert diagnosis["output"]["fault_type"] == "LLM::vllm_process_crash"
    assert "vLLM 主进程" in diagnosis["output"]["root_cause"]
    assert diagnosis["output"]["confidence_explanation"]["method"] == "llm_fault_observable_signals_v1"
    assert run["policy"]["risk"] == "high"
    assert run["stages"]["verify"]["output"]["checks"]
    suggestions = run["stages"]["heal"]["output"]["suggestions"]
    assert any("scale deployment/vllm-server -n llm-test --replicas=1" in item["command"] for item in suggestions)


def test_llm_vllm_process_crash_does_not_need_legacy_confidence_adapter() -> None:
    class BrokenLegacy:
        def calibrate_llm_fault_confidence(self, **kwargs):
            raise ImportError("legacy unavailable")

    custom = SkillExecutor(executor.registry, adapters={"legacy": BrokenLegacy()})
    run = custom.run(
        "llm.vllm_process_crash",
        {
            "fault_type": "process_crash",
            "message": "vLLM process crash and connection refused",
            "endpoint_status": "failed",
            "metrics_status": "ok",
            "process_count": 0,
            "metrics_count": 4,
            "deployment": "vllm-sim",
            "namespace": "llm-skill-realtest",
        },
        dry_run=True,
    ).to_dict()

    assert run["status"] == "completed"
    assert run["stages"]["diagnose"]["status"] == "ok"
    assert run["stages"]["diagnose"]["output"]["fault_type"] == "LLM::vllm_process_crash"
    assert run["stages"]["diagnose"]["output"]["confidence_explanation"]["method"] == "llm_fault_observable_signals_v1"
    assert run["stages"]["heal"]["output"]["suggestions"]


def test_llm_vllm_process_crash_generates_host_recovery_plan() -> None:
    run = executor.run(
        "llm.vllm_process_crash",
        {
            "fault_type": "process_crash",
            "message": "vLLM process crash and connection refused on bare metal GPU host",
            "endpoint_status": "failed",
            "metrics_status": "failed",
            "process_count": 0,
            "gpu_count": 3,
            "host_id": "33.33.33.127",
            "service_name": "vllm-openai",
        },
        dry_run=False,
    ).to_dict()

    suggestions = run["stages"]["heal"]["output"]["suggestions"]
    assert any("pgrep -af" in item["command"] for item in suggestions)
    assert any(item["command"] == "systemctl restart vllm-openai" for item in suggestions)
    assert run["stages"]["diagnose"]["output"]["fault_type"] == "LLM::vllm_process_crash"
    assert run["policy"]["requires_approval"] is True
    assert "skill 包含写/重启权限，真实执行必须审批。" in run["policy"]["heal_blocker_messages"]


def test_llm_host_readiness_is_read_only_and_reports_observations() -> None:
    run = executor.run(
        "tool.llm_host_readiness",
        {
            "host_id": "33.33.33.127",
            "message": "bare-metal GPU vLLM host readiness",
            "ssh_status": "ok",
            "gpu_count": 3,
            "process_count": 1,
            "metrics_status": "ok",
            "metrics_count": 12,
            "metrics": {
                "vllm:num_requests_running": 2,
                "vllm:num_requests_waiting": 0,
            },
        },
        dry_run=True,
    ).to_dict()

    assert run["status"] == "completed"
    assert run["policy"]["risk"] == "low"
    assert run["policy"]["supports_heal"] is False
    assert run["policy"]["heal_ready"] is False
    assert "heal_stage_not_declared" in run["policy"]["heal_blockers"]
    assert run["policy"]["requires_approval"] is False
    diagnosis = run["stages"]["diagnose"]["output"]
    assert diagnosis["health"] == "ready"
    assert diagnosis["observed"]["gpu_count"] == 3
    assert diagnosis["observed"]["running_requests"] == 2
    commands = "\n".join(diagnosis["commands"])
    assert "nvidia-smi" in commands
    assert "ps -eo pid,args" in commands
    assert "curl -fsS" in commands
    assert "heal" not in run["stages"]


def test_k8s_vllm_observability_skill_generates_closed_loop_plan() -> None:
    run = executor.run(
        "llm.k8s_vllm_observability",
        {
            "fault_type": "PodDelete",
            "message": "vLLM pod delete caused inference connection refused then recovered",
            "namespace": "llm-k8s",
            "deployment": "vllm-server",
            "service": "vllm-server",
            "endpoint_status": "failed",
            "metrics_status": "ok",
            "deployment_ready_replicas": 0,
            "deployment_desired_replicas": 1,
            "request_failures": 17,
            "e2e_p95_seconds": 0.785,
            "ttft_p95_seconds": 0.059,
            "metrics_count": 8,
            "gpu_count": 3,
            "prometheus_url": "http://127.0.0.1:30090",
            "endpoint": "http://127.0.0.1:30080/health",
            "model": "qwen2.5-coder-3b-k8s",
        },
        dry_run=False,
    ).to_dict()

    assert run["status"] == "completed"
    assert run["stages"]["detect"]["status"] == "matched"
    diagnosis = run["stages"]["diagnose"]["output"]
    assert diagnosis["fault_type"] == "LLM::k8s_vllm_unavailable"
    assert diagnosis["confidence_explanation"]["method"] == "llm_fault_observable_signals_v1"
    assert "K8S vLLM" in run["stages"]["diagnose"]["summary"]
    suggestions = run["stages"]["heal"]["output"]["suggestions"]
    assert any("api/v1/targets" in item["command"] for item in suggestions)
    assert any("scale deployment/vllm-server -n llm-k8s --replicas=1" in item["command"] for item in suggestions)
    assert any("/v1/completions" in item["command"] for item in suggestions)
    assert run["policy"]["requires_approval"] is True
    checks = run["stages"]["verify"]["output"]["checks"]
    assert any("rollout status deployment/vllm-server" in item for item in checks)
    assert any("e2e_request_latency" in item for item in checks)


def test_k8s_vllm_observability_can_be_primary_llm_workflow() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "fault_type": "HighLatency",
            "message": "K8S vLLM inference p95 latency degraded with Prometheus metrics",
            "namespace": "llm-k8s",
            "deployment": "vllm-server",
            "service": "vllm-server",
            "endpoint_status": "ok",
            "metrics_status": "ok",
            "deployment_ready_replicas": 1,
            "deployment_desired_replicas": 1,
            "e2e_p95_seconds": 3.2,
            "e2e_p95_threshold_seconds": 2.0,
            "metrics_count": 10,
            "gpu_count": 3,
        },
        domain="llm",
        dry_run=True,
    )

    assert result["status"] == "completed"
    assert result["skill_runtime"]["primary_skill_id"] == "llm.k8s_vllm_observability"
    assert result["diagnosis"]["fault_type"] == "LLM::k8s_vllm_high_latency"
    assert result["capability"]["requires_approval"] is True
    assert result["execution_plan"]["commands"]


def test_vllm_scheduler_queue_routes_real_loadgen_result() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "fault_type": "request_queue_overflow",
            "message": "vLLM request queue overflow under load, several requests closed and recovered after rollout",
            "namespace": "llm-k8s",
            "deployment": "vllm-server",
            "service": "vllm-server",
            "cluster_id": "t4-vllm-k8s",
            "target_mode": "k8s",
            "model": "qwen2.5-coder-3b-k8s",
            "endpoint_status": "ok",
            "metrics_status": "ok",
            "metrics_count": 7,
            "process_count": 1,
            "gpu_count": 1,
            "metrics": {
                "vllm:num_requests_running": 0,
                "vllm:num_requests_waiting": 0,
                "loadgen_requests": 38,
                "loadgen_success": 34,
                "loadgen_errors": 4,
                "e2e_latency_p95_s": 0.287,
                "request_success_rate_5m": 0.034,
            },
        },
        domain="llm",
        dry_run=True,
    )

    assert result["status"] == "completed"
    assert result["skill_runtime"]["primary_skill_id"] == "llm.vllm_scheduler_queue"
    assert result["diagnosis"]["skill_id"] == "llm.vllm_scheduler_queue"
    assert result["diagnosis"]["fault_type"] == "LLM::vllm_scheduler_queue_pressure"
    assert "请求队列" in result["diagnosis"]["root_cause"]
    assert result["capability"]["requires_approval"] is True


def test_ai_fault_event_endpoint_observation_routes_to_model_endpoint_skill() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "domain": "llm",
            "fault_type": "endpoint_timeout",
            "raw_fault_type": "observable_endpoint_unavailable",
            "message": "智算运行异常，观测到 Service endpoints 为空或推理端点不可用",
            "namespace": "llm-k8s",
            "deployment": "vllm-server",
            "service": "vllm-server",
            "endpoint": "http://127.0.0.1:30080/v1/completions",
            "model": "qwen2.5-coder-3b-k8s",
            "endpoint_status": "failed",
            "endpoint_count": 0,
            "http_status": 503,
            "metrics_status": "ok",
            "metrics_count": 3,
        },
        domain="llm",
        dry_run=True,
    )

    assert result["status"] == "completed"
    assert result["skill_runtime"]["primary_skill_id"] == "llm.model_serving_endpoint"
    assert result["diagnosis"]["fault_type"] == "LLM::model_serving_endpoint_unavailable"
    assert "Endpoint" in result["diagnosis"]["root_cause"] or "端点" in result["diagnosis"]["root_cause"]


def test_model_serving_endpoint_does_not_match_healthy_url_without_error_signal() -> None:
    run = executor.run(
        "llm.model_serving_endpoint",
        {
            "domain": "llm",
            "fault_type": "fault_experiment_signal",
            "message": "智算运行窗口观测",
            "namespace": "llm-k8s",
            "deployment": "vllm-server",
            "service": "vllm-server",
            "endpoint": "http://vllm-server:8000/v1/completions",
            "endpoint_status": "ok",
            "endpoint_count": 1,
            "http_status": 200,
        },
        dry_run=True,
    ).to_dict()

    assert run["stages"]["detect"]["status"] == "skipped"


def test_abnormal_container_restart_overrides_concurrent_latency_signals() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "domain": "llm",
            "fault_type": "fault_experiment_signal",
            "raw_fault_type": "fault_experiment_signal",
            "message": "故障窗口内 vLLM Pod 被重建，当前服务已重新就绪",
            "namespace": "llm-k8s",
            "deployment": "vllm-server",
            "service": "vllm-server",
            "endpoint_status": "ok",
            "endpoint_count": 1,
            "deployment_ready_replicas": 1,
            "deployment_desired_replicas": 1,
            "process_count": 1,
            "pod_count": 1,
            "pod_restart_count": 1,
            "container_restarted_in_window": True,
            "last_terminated_reason": "Error",
            "last_exit_code": 247,
            "pod_age_seconds": 5.0,
            "agent_evidence_summary": (
                "请求窗口同时观测到 TTFT、TPOT 和 E2E P95 波动；"
                "容器生命周期显示故障窗口内发生异常重启"
            ),
            "observation_signals": [{
                "signal": "container_restarted_in_window",
                "value": True,
                "candidate_skill": "llm.vllm_process_crash",
                "candidate_fault": "vllm_process_restart",
            }],
            "candidate_skills": ["llm.vllm_process_crash"],
            "candidate_faults": ["vllm_process_restart"],
            "evidence": [
                {"source": "k8s", "signal": "container_restarted_in_window", "value": True},
                {"source": "k8s", "signal": "last_exit_code", "value": 247},
            ],
        },
        domain="llm",
        dry_run=True,
    )

    assert result["skill_runtime"]["primary_skill_id"] == "llm.vllm_process_crash"
    assert result["diagnosis"]["fault_type"] == "LLM::vllm_process_crash"
    assert "进程" in result["diagnosis"]["root_cause"] or "重启" in result["diagnosis"]["root_cause"]


def test_pod_recreation_without_abnormal_termination_does_not_imply_process_crash() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "domain": "llm",
            "fault_type": "fault_experiment_signal",
            "raw_fault_type": "fault_experiment_signal",
            "message": "工作负载发布过程中 Pod 重建，容器无异常终止证据",
            "namespace": "inference",
            "deployment": "model-server",
            "service": "model-server",
            "endpoint_status": "failed",
            "endpoint_count": 0,
            "deployment_ready_replicas": 0,
            "deployment_desired_replicas": 1,
            "pod_recreated_in_window": True,
            "pod_restart_count": 0,
        },
        domain="llm",
        dry_run=True,
    )

    assert result["skill_runtime"]["primary_skill_id"] == "llm.model_serving_endpoint"
    assert result["diagnosis"]["fault_type"] == "LLM::model_serving_endpoint_unavailable"


def test_cuda_runtime_evidence_overrides_endpoint_and_process_symptoms() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "domain": "llm",
            "fault_type": "fault_experiment_signal",
            "raw_fault_type": "fault_experiment_signal",
            "namespace": "inference",
            "deployment": "model-server",
            "service": "model-server",
            "endpoint_status": "failed",
            "endpoint_count": 0,
            "process_count": 0,
            "pod_restart_count": 1,
            "container_terminated_in_window": True,
            "last_terminated_reason": "Error",
            "last_exit_code": 1,
            "container_waiting_reasons": ["CrashLoopBackOff"],
            "container_log_excerpt": "RuntimeError: Failed to infer device type",
        },
        domain="llm",
        dry_run=True,
    )

    assert result["skill_runtime"]["primary_skill_id"] == "llm.cuda_driver_failure"
    assert result["diagnosis"]["fault_type"] == "LLM::cuda_runtime_or_context_error"
    assert "CUDA" in result["diagnosis"]["root_cause"]


def test_cuda_skill_does_not_use_experiment_labels_as_runtime_evidence() -> None:
    run = executor.run(
        "llm.cuda_driver_failure",
        {
            "domain": "llm",
            "fault_type": "cuda_error",
            "scenario_id": "driver-case-a",
            "scenario_name": "CUDA fault experiment",
            "endpoint_status": "ok",
        },
        dry_run=True,
    ).to_dict()

    assert run["stages"]["detect"]["status"] == "skipped"


def test_cuda_visibility_conflict_is_runtime_evidence_for_gpu_workload() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "domain": "llm",
            "fault_type": "fault_experiment_signal",
            "namespace": "inference",
            "deployment": "model-server",
            "endpoint_status": "failed",
            "endpoint_count": 0,
            "process_count": 0,
            "accelerator_visibility": {"server:CUDA_VISIBLE_DEVICES": "-1"},
            "accelerator_resource_limits": {"vendor.example/gpu": 1.0},
        },
        domain="llm",
        dry_run=True,
    )

    assert result["skill_runtime"]["primary_skill_id"] == "llm.cuda_driver_failure"
    assert result["diagnosis"]["fault_type"] == "LLM::cuda_device_visibility_misconfiguration"
    assert "加速器可见性" in result["diagnosis"]["root_cause"]
    assert any(
        "CUDA_VISIBLE_DEVICES-" in command
        for command in result["execution_plan"]["commands"]
    )


def test_observation_candidate_skill_can_cross_domain_without_overriding_fault_type() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "domain": "llm",
            "fault_type": "fault_experiment_signal",
            "raw_fault_type": "observable_resource_pressure",
            "message": "智算事件中心观测到节点资源异常，但不使用注入标签作为诊断结论",
            "cluster_id": "t4-vllm-k8s",
            "namespace": "llm-k8s",
            "deployment": "vllm-server",
            "node_cpu_usage": 93.5,
            "node_memory_usage": 61.2,
            "observation_signals": [
                {
                    "signal": "node_cpu_usage",
                    "value": 93.5,
                    "threshold": 85,
                    "candidate_skill": "k8s.resource_pressure",
                    "candidate_fault": "HighCPUUsage",
                    "reason": "节点 CPU 使用率超过阈值",
                }
            ],
            "candidate_skills": ["k8s.resource_pressure"],
            "candidate_faults": ["HighCPUUsage"],
            "evidence": [
                {"source": "prometheus", "signal": "node_cpu_usage", "value": 93.5, "threshold": 85},
                {"source": "k8s", "signal": "namespace", "value": "llm-k8s"},
            ],
        },
        domain="llm",
        dry_run=True,
    )

    assert result["status"] == "completed"
    assert result["skill_runtime"]["primary_skill_id"] == "k8s.resource_pressure"
    assert result["diagnosis"]["skill_id"] == "k8s.resource_pressure"
    assert result["diagnosis"]["fault_type"] in {"K8S::resource_pressure", "HighCPUUsage"}
    assert "CPU" in result["diagnosis"]["root_cause"] or "资源" in result["diagnosis"]["root_cause"]
    assert result["diagnosis"]["confidence_explanation"]["method"] == "skill_evidence_calibrated_v2"


def test_candidate_skill_is_soft_hint_not_forced_root_cause() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "domain": "llm",
            "fault_type": "fault_experiment_signal",
            "message": "智算事件只有普通上下文，没有资源压力观测证据",
            "namespace": "llm-k8s",
            "deployment": "vllm-server",
            "node_cpu_usage": 12.0,
            "node_memory_usage": 23.0,
            "candidate_skills": ["k8s.resource_pressure"],
            "observation_signals": [
                {
                    "signal": "node_cpu_usage",
                    "value": 12.0,
                    "threshold": 85,
                    "candidate_skill": "k8s.resource_pressure",
                    "reason": "低于阈值的候选线索不应强制诊断",
                }
            ],
            "evidence": [
                {"source": "prometheus", "signal": "node_cpu_usage", "value": 12.0, "threshold": 85}
            ],
        },
        domain="llm",
        dry_run=True,
    )

    matched_ids = {
        (item.get("skill") or {}).get("id")
        for item in (result.get("skill_runtime") or {}).get("runs", [])
    }
    assert "k8s.resource_pressure" not in matched_ids
    assert result["skill_runtime"]["primary_skill_id"] != "k8s.resource_pressure"


def test_ai_endpoint_fault_not_overridden_by_background_rootfs_pressure() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "domain": "llm",
            "fault_type": "fault_experiment_signal",
            "message": "智算事件显示 vLLM Service Endpoint 为空，节点根分区长期处于高水位但不是本次直接现象",
            "namespace": "llm-k8s",
            "deployment": "vllm-server",
            "service": "vllm-server",
            "model": "qwen2.5-coder-3b-k8s",
            "endpoint_status": "failed",
            "endpoint_count": 0,
            "node_cpu_usage": 3.0,
            "node_memory_usage": 21.0,
            "node_rootfs_usage": 96.0,
            "observation_signals": [
                {
                    "signal": "endpoint_status",
                    "value": "failed",
                    "threshold": "available",
                    "candidate_skill": "llm.model_serving_endpoint",
                    "candidate_fault": "model_serving_endpoint_unavailable",
                },
                {
                    "signal": "node_rootfs_usage",
                    "value": 96.0,
                    "threshold": 85.0,
                    "candidate_skill": "k8s.resource_pressure",
                    "candidate_fault": "DiskPressure",
                },
            ],
            "candidate_skills": ["llm.model_serving_endpoint", "k8s.resource_pressure"],
        },
        domain="llm",
        dry_run=True,
    )

    assert result["skill_runtime"]["primary_skill_id"] == "llm.model_serving_endpoint"
    assert result["diagnosis"]["fault_type"] == "LLM::model_serving_endpoint_unavailable"
    assert "Endpoint" in result["diagnosis"]["root_cause"] or "入口不可用" in result["diagnosis"]["root_cause"]


def test_ai_node_rootfs_pressure_is_not_reported_as_high_cpu() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "domain": "llm",
            "fault_type": "node_resource_pressure",
            "raw_fault_type": "node_resource_pressure",
            "event_type": "node_resource_pressure",
            "message": "智算事件中心观测到节点基础资源异常，vLLM 所在节点根分区超过阈值",
            "namespace": "llm-k8s",
            "deployment": "vllm-server",
            "service": "vllm-server",
            "model": "qwen2.5-coder-3b-k8s",
            "endpoint_status": "ok",
            "metrics_status": "ok",
            "node_cpu_usage": 2.5,
            "node_memory_usage": 22.0,
            "metrics": {"metric": "node_rootfs_usage", "value": 96.0, "threshold": 85.0},
            "evidence": [
                {"source": "prometheus", "signal": "node_rootfs_usage", "value": 96.0, "threshold": 85.0},
            ],
            "observation_signals": [
                {
                    "signal": "node_rootfs_usage",
                    "value": 96.0,
                    "threshold": 85.0,
                    "candidate_skill": "k8s.resource_pressure",
                    "candidate_fault": "DiskPressure",
                }
            ],
            "candidate_skills": ["k8s.resource_pressure"],
        },
        domain="llm",
        dry_run=True,
    )

    assert result["status"] == "completed"
    assert result["skill_runtime"]["primary_skill_id"] == "llm.k8s_vllm_observability"
    assert result["diagnosis"]["fault_type"] == "LLM::k8s_vllm_node_disk_pressure"
    assert "HighCpu" not in result["diagnosis"]["root_cause"]
    assert "磁盘" in result["diagnosis"]["root_cause"] or "根分区" in result["diagnosis"]["root_cause"]


def test_fault_run_metric_is_not_treated_as_node_resource_metric() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "domain": "llm",
            "fault_type": "fault_experiment_signal",
            "raw_fault_type": "fault_experiment_signal",
            "message": "智算运行异常：推理服务观测信号异常",
            "namespace": "llm-k8s",
            "deployment": "vllm-server",
            "service": "vllm-server",
            "model": "qwen2.5-coder-3b-k8s",
            "endpoint_status": "ok",
            "metrics_status": "ok",
            "metrics_count": 13,
            "running_requests": 0,
            "waiting_requests": 0,
            "node_cpu_usage": 0.6,
            "node_memory_usage": 21.2,
            "node_rootfs_usage": 95.9,
            "metrics": {"metric": "fault_run", "value": "observable_anomaly", "threshold": "diagnose"},
            "evidence": [
                {"source": "observation", "signal": "metrics_status", "value": "ok"},
                {"source": "observation", "signal": "running_requests", "value": 0},
                {"source": "observation", "signal": "waiting_requests", "value": 0},
                {"source": "observation", "signal": "node_rootfs_usage", "value": 95.9},
            ],
        },
        domain="llm",
        dry_run=True,
    )

    assert result["skill_runtime"]["primary_skill_id"] in {
        "llm.k8s_vllm_observability",
        "llm.business_slo",
        "generic.root_cause_diagnosis",
    }
    assert result["diagnosis"]["fault_type"] == "LLM::k8s_vllm_experiment_effect_unconfirmed"
    assert "根分区" not in result["diagnosis"]["root_cause"]
    assert "磁盘" not in result["diagnosis"]["root_cause"]
    assert result["diagnosis"]["confidence"] <= 0.62


def test_fault_experiment_slo_degradation_is_not_overridden_by_background_resource() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "domain": "llm",
            "fault_type": "fault_experiment_signal",
            "raw_fault_type": "fault_experiment_signal",
            "message": "故障窗口内 vLLM 请求成功率降低且端到端 P95 升高",
            "namespace": "llm-k8s",
            "deployment": "vllm-server",
            "service": "vllm-server",
            "model": "qwen2.5-coder-3b-k8s",
            "endpoint_status": "ok",
            "endpoint_count": 1,
            "metrics_status": "ok",
            "success_rate": 0.13,
            "e2e_p95_seconds": 4.875,
            "e2e_p95_threshold_seconds": 3.0,
            "waiting_requests": 0,
            "node_rootfs_usage": 95.9,
            "metrics": {"metric": "fault_run", "value": "observable_anomaly", "threshold": "diagnose"},
            "observation_signals": [
                {
                    "signal": "success_rate",
                    "value": 0.13,
                    "threshold": 0.99,
                    "candidate_skill": "llm.business_slo",
                    "candidate_fault": "business_success_rate_drop",
                },
                {
                    "signal": "e2e_p95_seconds",
                    "value": 4.875,
                    "threshold": 3.0,
                    "candidate_skill": "llm.business_slo",
                    "candidate_fault": "e2e_latency_high",
                },
            ],
            "background_observation_signals": [
                {
                    "signal": "node_rootfs_usage",
                    "value": 95.9,
                    "threshold": 85.0,
                    "candidate_skill": "k8s.resource_pressure",
                    "candidate_fault": "DiskPressure",
                }
            ],
            "candidate_skills": ["llm.business_slo"],
            "candidate_faults": ["business_success_rate_drop", "e2e_latency_high"],
            "evidence": [
                {"source": "observation", "signal": "success_rate", "value": 0.13},
                {"source": "observation", "signal": "e2e_p95_seconds", "value": 4.875},
                {"source": "observation", "signal": "node_rootfs_usage", "value": 95.9},
            ],
        },
        domain="llm",
        dry_run=True,
    )

    assert result["skill_runtime"]["primary_skill_id"] == "llm.business_slo"
    assert result["diagnosis"]["fault_type"] == "LLM::business_success_rate_drop"
    assert "成功率" in result["diagnosis"]["root_cause"]
    assert "根分区" not in result["diagnosis"]["root_cause"]
    assert "磁盘" not in result["diagnosis"]["root_cause"]


def test_loadgen_skill_requires_explicit_loadgen_signal() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "domain": "llm",
            "fault_type": "fault_experiment_signal",
            "message": "智算事件只有 vLLM 基础上下文，没有负载生成器指标",
            "namespace": "llm-k8s",
            "deployment": "vllm-server",
            "service": "vllm-server",
            "model": "qwen2.5-coder-3b-k8s",
            "endpoint_status": "ok",
            "metrics_status": "ok",
        },
        domain="llm",
        dry_run=True,
    )

    matched_ids = {
        (item.get("skill") or {}).get("id")
        for item in (result.get("skill_runtime") or {}).get("runs", [])
    }
    assert "llm.vllm_load_generator" not in matched_ids


def test_ai_operational_skills_route_observability_loadgen_and_endpoint_faults() -> None:
    workflow = SkillWorkflow(executor)
    cases = [
        (
            {
                "fault_type": "dcgm_exporter_down",
                "message": "智算 GPU metrics missing because dcgm-exporter Prometheus target down",
                "namespace": "llm-k8s",
                "node": "dds-127",
                "target_status": "down",
                "metrics": {"up": 0},
            },
            "llm.dcgm_exporter_observability",
            "LLM::dcgm_exporter_target_down",
        ),
        (
            {
                "fault_type": "teller_trace_stale",
                "message": "TELLER trace 文件长时间未更新，只有 NVTX 没有 CUDA event",
                "namespace": "llm-k8s",
                "fine_grained_trace": {"age_seconds": 900, "nvtx_events": 12, "cuda_events": 0},
            },
            "llm.teller_trace_collection",
            "LLM::teller_trace_collection_stale",
        ),
        (
            {
                "fault_type": "loadgen_traffic_zero",
                "message": "vLLM 负载生成器没有背景流量，前端指标不变化",
                "endpoint": "http://127.0.0.1:30081/v1/completions",
                "model": "qwen2.5-coder-14b",
                "loadgen_requests": 0,
                "request_rate": 0,
            },
            "llm.vllm_load_generator",
            "LLM::load_generator_no_traffic",
        ),
        (
            {
                "fault_type": "model_not_found",
                "message": "OpenAI API model_not_found on /v1/completions for vLLM model name",
                "endpoint": "http://vllm/v1/completions",
                "model": "gemma4-12b",
                "http_status": 404,
            },
            "llm.model_serving_endpoint",
            "LLM::model_serving_model_not_found",
        ),
    ]

    for payload, expected_skill, expected_fault in cases:
        result = workflow.run(payload, domain="llm", dry_run=True)

        assert result["status"] == "completed", payload["fault_type"]
        assert result["skill_runtime"]["primary_skill_id"] == expected_skill, payload["fault_type"]
        assert result["diagnosis"]["fault_type"] == expected_fault, payload["fault_type"]
        assert result["diagnosis"]["root_cause"], payload["fault_type"]
        assert result["execution_plan"]["commands"], payload["fault_type"]


def test_ai_specific_skills_route_parallel_gpu_replica_and_hardware_faults() -> None:
    workflow = SkillWorkflow(executor)
    cases = [
        (
            {
                "fault_type": "tp_size_mismatch",
                "message": "vLLM tensor_parallel_size world_size rank mismatch on multi GPU model shard",
                "namespace": "llm-k8s",
                "deployment": "vllm-large",
                "tensor_parallel_size": 4,
                "pipeline_parallel_size": 1,
                "world_size": 4,
                "gpu_count": 3,
            },
            "llm.parallel_topology_consistency",
            "LLM::parallel_topology_gpu_count_mismatch",
        ),
        (
            {
                "fault_type": "gpu_allocatable_missing",
                "message": "GPU Pod Pending: Insufficient nvidia.com/gpu, nvidia-device-plugin not ready",
                "namespace": "llm-k8s",
                "node": "dds-127",
                "gpu_allocatable": 0,
                "device_plugin_ready": "false",
            },
            "llm.gpu_device_plugin_health",
            "LLM::nvidia_device_plugin_unhealthy",
        ),
        (
            {
                "fault_type": "replica_traffic_imbalance",
                "message": "vLLM replica traffic imbalance, hot replica and sticky session cause GPU utilization skew",
                "namespace": "llm-k8s",
                "service": "vllm-server",
                "deployment": "vllm-server",
                "replica_qps_skew": 0.72,
                "ready_replicas": 2,
                "desired_replicas": 2,
            },
            "llm.replica_traffic_imbalance",
            "LLM::sticky_session_replica_hotspot",
        ),
        (
            {
                "fault_type": "model_hardware_compatibility",
                "message": "Gemma BF16 unsupported dtype on NVIDIA T4 compute capability 7.5 with vLLM",
                "model": "gemma-3-4b",
                "gpu_model": "NVIDIA T4",
                "dtype": "bf16",
                "compute_capability": 7.5,
                "gpu_memory_gb": 16,
                "model_params_b": 4,
            },
            "llm.model_hardware_compatibility",
            "LLM::dtype_not_supported_by_gpu",
        ),
    ]

    for payload, expected_skill, expected_fault in cases:
        result = workflow.run(payload, domain="llm", dry_run=True)

        assert result["status"] == "completed", payload["fault_type"]
        assert result["skill_runtime"]["primary_skill_id"] == expected_skill, payload["fault_type"]
        assert result["diagnosis"]["fault_type"] == expected_fault, payload["fault_type"]
        assert result["execution_plan"]["commands"], payload["fault_type"]


def test_ai_deep_infra_skills_route_hardware_partition_repository_and_topology_faults() -> None:
    workflow = SkillWorkflow(executor)
    cases = [
        (
            {
                "fault_type": "gpu_xid_error",
                "message": "GPU Xid error and ECC retired_pages increase on dds-128",
                "node": "dds-128",
                "gpu_uuid": "GPU-test",
                "xid_errors": 1,
                "ecc_errors": 0,
            },
            "llm.gpu_hardware_health",
            "LLM::gpu_xid_hardware_or_driver_fault",
        ),
        (
            {
                "fault_type": "gpu_partition_isolation",
                "message": "MIG profile mismatch, GPU slice not visible for vLLM Pod",
                "node": "dds-a100",
                "mig_profile": "1g.10gb",
                "resource_name": "nvidia.com/mig-1g.10gb",
            },
            "llm.gpu_partition_isolation",
            "LLM::mig_profile_or_resource_mismatch",
        ),
        (
            {
                "fault_type": "model_repository_access",
                "message": "model repository object storage download timeout, shard download failed",
                "model": "qwen2.5-14b",
                "repository": "s3://models/qwen2.5-14b",
                "http_status": 503,
            },
            "llm.model_repository_access",
            "LLM::model_repository_download_timeout",
        ),
        (
            {
                "fault_type": "topology_aware_scheduling",
                "message": "vLLM tensor parallel cross-node placement causes RDMA locality latency regression",
                "namespace": "llm-k8s",
                "deployment": "vllm-large",
                "cross_node_parallel_groups": 1,
                "rdma_latency_ms": 18,
            },
            "llm.topology_aware_scheduling",
            "LLM::cross_node_parallel_placement",
        ),
    ]

    for payload, expected_skill, expected_fault in cases:
        result = workflow.run(payload, domain="llm", dry_run=True)

        assert result["status"] == "completed", payload["fault_type"]
        assert result["skill_runtime"]["primary_skill_id"] == expected_skill, payload["fault_type"]
        assert result["diagnosis"]["fault_type"] == expected_fault, payload["fault_type"]
        assert result["execution_plan"]["commands"], payload["fault_type"]


def test_k8s_vllm_observability_classifies_ai_unique_fault_types() -> None:
    workflow = SkillWorkflow(executor)
    cases = [
        ("kv_cache_exhaustion", "llm.gpu_resource_pressure", "LLM::kv_cache_exhaustion", "KV Cache", {"kv_cache_usage": 0.96}),
        ("request_queue_overflow", "llm.vllm_scheduler_queue", "LLM::vllm_waiting_queue_backlog", "等待队列", {"waiting_requests": 4}),
        ("cuda_error", "llm.cuda_driver_failure", "LLM::cuda_runtime_or_context_error", "CUDA", {"cuda_error_count": 2}),
        ("nccl_failure", "llm.distributed_communication", "LLM::nccl_tensor_parallel_communication_failure", "NCCL", {"nccl_error_count": 1}),
        ("tokenizer_error", "llm.model_asset_failure", "LLM::tokenizer_asset_failure", "Tokenizer", {"model_asset_error": "tokenizer load failed"}),
        ("gpu_clock_throttle", "llm.gpu_hardware_health", "LLM::gpu_thermal_or_power_throttle", "GPU", {"gpu_throttle_reasons": "thermal", "gpu_temperature_c": 88}),
        ("cupti_trace_overhead", "llm.teller_trace_collection", "LLM::teller_trace_collection_degraded", "TELLER", {"fine_grained_trace": {"age_seconds": 3, "nvtx_events": 12, "cuda_events": 0}}),
    ]

    for fault_type, expected_skill, expected, keyword, observed in cases:
        payload = {
            "fault_type": "fault_experiment_signal",
            "message": f"智算故障实验出现 {fault_type} 的可观测症状，依据指标和日志验证而不是使用注入标签",
            "namespace": "llm-k8s",
            "deployment": "vllm-server",
            "service": "vllm-server",
            "cluster_id": "t4-vllm-k8s",
            "target_mode": "k8s",
            "endpoint_status": "ok",
            "metrics_status": "ok",
            "metrics_count": 5,
            "gpu_count": 3,
        }
        payload.update(observed)
        result = workflow.run(
            payload,
            domain="llm",
            dry_run=True,
        )

        assert result["status"] == "completed", fault_type
        assert result["skill_runtime"]["primary_skill_id"] == expected_skill, fault_type
        assert result["diagnosis"]["fault_type"] == expected, fault_type
        assert keyword in result["diagnosis"]["root_cause"], fault_type


def test_gpu_resource_skill_reads_structured_ai_event_metric_values() -> None:
    workflow = SkillWorkflow(executor)
    result = workflow.run(
        {
            "domain": "llm",
            "fault_type": "gpu_oom",
            "raw_fault_type": "gpu_memory_pressure",
            "message": "GPU 显存或 KV Cache 压力过高",
            "namespace": "llm-k8s",
            "deployment": "vllm-server",
            "metrics": {
                "metric": "gpu_memory/kv_cache",
                "value": {"kv_cache": 95.26, "gpu_memory_used": 36.24},
                "threshold": {"kv_cache": 60.0, "gpu_memory_used": 90.0},
            },
        },
        domain="llm",
        dry_run=True,
    )

    assert result["skill_runtime"]["primary_skill_id"] == "llm.gpu_resource_pressure"
    assert result["diagnosis"]["fault_type"] == "LLM::kv_cache_exhaustion"
    primary_run = next(
        run for run in result["skill_runtime"]["runs"]
        if run["skill"]["id"] == "llm.gpu_resource_pressure"
    )
    snapshot = primary_run["stages"]["diagnose"]["output"]["resource_snapshot"]
    assert snapshot["kv_cache_usage"] == 0.9526
    assert snapshot["gpu_memory_usage"] == 0.3624


def test_prometheus_tool_and_scrape_target_skills_route_precisely() -> None:
    workflow = SkillWorkflow(executor)

    scrape = workflow.run(
        {
            "domain": "k8s",
            "fault_type": "prometheus_scrape_failed",
            "alertname": "TargetDown",
            "message": "Prometheus TargetDown scrape failed for kube-proxy metricsBindAddress 127.0.0.1:10249",
            "namespace": "kube-system",
            "service": "kube-proxy",
        },
        domain="k8s",
        dry_run=True,
    )
    assert scrape["skill_runtime"]["primary_skill_id"] == "k8s.prometheus_scrape_target"
    assert scrape["diagnosis"]["fault_type"] == "K8S::prometheus_metrics_bind_localhost"

    service = workflow.run(
        {
            "domain": "k8s",
            "fault_type": "service_unavailable",
            "message": "TargetDown HTTP 503 no endpoints service unavailable",
            "namespace": "default",
            "service": "frontend",
            "deployment": "frontend",
        },
        domain="k8s",
        dry_run=True,
    )
    assert service["skill_runtime"]["primary_skill_id"] == "k8s.service_unavailable"

    tool = workflow.run(
        {
            "fault_type": "prometheus_query",
            "message": "使用 PromQL 查询最近 1 小时 GPU 利用率峰值和阈值",
            "prometheus_url": "http://127.0.0.1:19090",
            "query": "max_over_time(DCGM_FI_DEV_GPU_UTIL[1h])",
            "purpose": "threshold validation",
        },
        domain="tool",
        dry_run=True,
    )
    assert tool["skill_runtime"]["primary_skill_id"] == "tool.prometheus_query"
    assert tool["diagnosis"]["fault_type"] == "Tool::prometheus_query_validation"


def test_agent_skill_payload_routes_prometheus_scrape_from_llm_hypothesis() -> None:
    evidence = {
        "agent_workflow": {
            "hypotheses": [
                {
                    "description": "kube-proxy 的 metricsBindAddress 绑定在 127.0.0.1，Prometheus 无法远程抓取 10249 /metrics。",
                    "fault_type": "misconfiguration",
                    "service": "kube-proxy",
                    "expected_evidence": ["metricsBindAddress=127.0.0.1", "TargetDown", "scrape connection refused"],
                }
            ]
        },
        "agent_evidence_collection": {
            "commands": [
                {
                    "command": "describe svc kube-proxy",
                    "summary": "service not found in namespace",
                    "sample": "TargetDown scrape failed",
                }
            ]
        },
        "agent_skill_context": {
            "payload": {
                "payload": {
                    "source": "alert_center",
                    "domain": "k8s",
                    "namespace": "kube-system",
                    "alertname": "TargetDown",
                    "service": "kube-proxy",
                }
            }
        },
    }
    payload = build_skill_payload(
        "Alert TargetDown kube-proxy target down",
        namespace="kube-system",
        evidence=evidence,
    )
    result = SkillWorkflow(executor).run(payload, domain="k8s", dry_run=True)

    assert "metricsBindAddress" in payload["agent_evidence_summary"]
    assert result["skill_runtime"]["primary_skill_id"] == "k8s.prometheus_scrape_target"
    assert result["diagnosis"]["fault_type"] == "K8S::prometheus_metrics_bind_localhost"
    assert "127.0.0.1" in result["diagnosis"]["root_cause"]


def test_agent_skill_scrape_hypothesis_overrides_generic_targetdown_and_control_plane_samples() -> None:
    evidence = {
        "agent_workflow": {
            "hypotheses": [
                {
                    "id": "H1",
                    "description": "kube-proxy 的 metricsBindAddress 配置为 127.0.0.1，导致 Prometheus 远程抓取 10249 /metrics 时 connection refused",
                    "fault_type": "misconfiguration",
                    "service": "kube-proxy",
                    "expected_evidence": [
                        "kube-proxy 配置文件中 metricsBindAddress 为 127.0.0.1",
                        "Prometheus target 显示 scrape connection refused",
                    ],
                    "supporting_evidence": [
                        "Prometheus 抓取错误为 connection refused，符合仅监听本地回环时行为",
                    ],
                    "source": "llm",
                    "llm_used": True,
                }
            ],
            "verification_plan": {
                "checks": [
                    {
                        "purpose": "查看 kube-proxy DaemonSet 或 ConfigMap 中的 metricsBindAddress",
                        "commands": ["kubectl get cm -n kube-system kube-proxy -o yaml"],
                    }
                ]
            },
        },
        "agent_evidence_collection": {
            "commands": [
                {
                    "command": "kubectl get pods,svc,endpoints -o wide",
                    "summary": "kubectl 执行成功，但样本中包含 kube-apiserver 等控制面 Pod",
                    "sample": "pod/kube-apiserver 1/1 Running\npod/kube-proxy 1/1 Running\nscrape_error connection refused",
                },
                {
                    "command": "kubectl describe svc kube-proxy",
                    "summary": "services kube-proxy not found",
                    "sample": "Error from server (NotFound): services \"kube-proxy\" not found",
                },
            ]
        },
        "agent_skill_context": {
            "payload": {
                "payload": {
                    "source": "alert_center",
                    "domain": "k8s",
                    "fault_type": "service_unavailable",
                    "raw_fault_type": "TargetDown",
                    "namespace": "kube-system",
                    "service": "kube-prom-kube-prometheus-kube-proxy",
                    "evidence": [
                        {"source": "prometheus", "signal": "up", "value": "0"},
                        {"source": "prometheus", "signal": "scrape_error", "value": "connection refused or target down"},
                    ],
                }
            }
        },
    }
    payload = build_skill_payload(
        "Alert TargetDown: kube-prometheus kube-proxy target down in kube-system. Prometheus cannot scrape metrics endpoint; service may be unavailable or metrics endpoint may be bound to localhost.",
        namespace="kube-system",
        evidence=evidence,
    )
    result = SkillWorkflow(executor).run(payload, domain="k8s", dry_run=True)

    assert payload["inferred_fault_type"] == "prometheus_scrape_failed"
    assert result["skill_runtime"]["primary_skill_id"] == "k8s.prometheus_scrape_target"
    assert result["diagnosis"]["fault_type"] == "K8S::prometheus_metrics_bind_localhost"


def test_skill_agent_parses_llm_raw_response_json() -> None:
    agent = SkillAgent(
        registry=None,
        skills_dir=ROOT / "skill_runtime" / "skills",
        llm_client=object(),
    )

    data = agent._normalize_llm_hypothesis_response({
        "raw_response": (
            '{"hypotheses":[{"id":"H1","description":"kube-proxy metricsBindAddress 绑定 127.0.0.1",'
            '"confidence":0.9,"fault_type":"configuration_error",'
            '"expected_evidence":["Prometheus scrape connection refused"]}]}'
        )
    })

    assert data["hypotheses"][0]["description"].startswith("kube-proxy")


def test_aishperf_aiinfra_skill_reuses_eval_methodology() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "source": "aishperf",
            "external_source": "aishperf.aiops-chaos",
            "domain": "llm",
            "scenario_id": "aishperf-gpu-xid-error",
            "aemu_fault_id": "gpu-xid-error",
            "scenario_name": "gpu-xid-error",
            "fault_type": "aishperf_aiinfra_fault",
            "fault_domain": "gpu",
            "domains": ["gpu", "rdma", "kubernetes"],
            "risk": "critical",
            "namespace": "llm-k8s",
            "deployment": "vllm-server",
            "node": "dds-128",
            "message": "AISHPerf GPU/RDMA fault caused vLLM TTFT regression and request failures",
            "dry_run_plan": {"status": "dry_run", "aemu_http": {"inject": "POST /api/fault/gpu-xid-error"}},
            "signals": {"aemu_plan_ready": True, "external_available": True, "prometheus_connected": True},
            "metrics": {
                "TTFT_Mean_ms": 1280,
                "TPOT_Mean_ms": 92,
                "Output_tok_throughput_tok_per_s": 18.5,
                "error_rate": 0.12,
            },
            "prometheus_url": "http://127.0.0.1:19090",
        },
        domain="llm",
        dry_run=True,
    )

    assert result["status"] == "completed"
    assert result["skill_runtime"]["primary_skill_id"] == "llm.aishperf_aiinfra_diagnosis"
    diagnosis = result["diagnosis"]
    assert diagnosis["fault_type"] == "LLM::distributed_training_or_rdma_fault"
    assert diagnosis["external_method"]["scenario"] == "alcor-ops-diagnosis-full"
    assert "diagnosis_pass_rate" in diagnosis["external_method"]["metrics"]
    assert "TTFT_Mean_ms" in diagnosis["external_method"]["inference_llm_metrics"]
    assert diagnosis["evaluation_gate"]["ready"] is True
    assert diagnosis["observed_metrics"]["TTFT_Mean_ms"] == 1280
    assert any("DCGM" in item for item in diagnosis["expected_evidence"])
    assert result["capability"]["requires_approval"] is True
    assert result["execution_plan"]["dry_run"] is True


def test_skill_runtime_api() -> None:
    client = TestClient(app)

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["skills"] >= 3

    skills = client.get("/api/skills").json()["skills"]
    service_skill = next(item for item in skills if item["id"] == "k8s.service_unavailable")
    assert service_skill["contract_status"]["label"]
    assert service_skill["source_status"]["source_of_truth"] in {"skill.yaml", "SKILL.md frontmatter"}

    run = client.post(
        "/api/skills/k8s.service_unavailable/run",
        json={
            "payload": {
                "message": "service frontend unavailable 503 no endpoints",
                "deployment": "frontend",
                "namespace": "default",
            },
            "dry_run": True,
        },
    )
    assert run.status_code == 200, run.text
    assert run.json()["stages"]["detect"]["status"] == "matched"


def test_skill_draft_api_generates_review_only_candidate() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/skills/draft",
        json={
            "source": "unit-test",
            "incident": {
                "fault_type": "GPUOOM",
                "title": "GPU OOM",
                "message": "vLLM GPU OOM causes request failures",
                "evidence": ["CUDA OOM in logs", "GPU memory full"],
            },
            "traces": [{"stage": "diagnose"}],
            "feedback": [{"accepted": True}],
        },
    )

    assert response.status_code == 200, response.text
    draft = response.json()
    assert draft["skill_id"] == "llm.gpuoom"
    assert draft["manifest"]["policy"]["requires_approval"] is True
    assert draft["manifest"]["policy"]["safe_auto_execute"] is False
    assert "不会自动启用" in "；".join(draft["safety_notes"])
    assert "def heal" in draft["skill_py"]
    assert draft["verification"]["status"] == "review_required"
    assert draft["verification"]["review_ready"] is True
    assert draft["verification"]["publish_ready"] is False


def test_skill_draft_verifier_blocks_dangerous_content() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/skills/draft/verify",
        json={
            "draft": {
                "manifest": {
                    "id": "k8s.bad",
                    "name": "Bad Skill",
                    "domain": "k8s",
                    "stages": ["detect", "diagnose", "heal", "verify"],
                    "risk": "high",
                    "permissions": ["k8s.admin"],
                    "policy": {"requires_approval": False, "safe_auto_execute": True},
                    "confidence": {"min_confidence_for_auto_heal": 0.5},
                },
                "skill_py": "def detect(context): return {}\ndef diagnose(context): return {}\ndef heal(context): os.system('rm -rf /')\ndef verify(context): return {}\n",
                "tests": ["detect only"],
                "safety_notes": [],
            }
        },
    )

    assert response.status_code == 200, response.text
    verification = response.json()
    codes = {finding["code"] for finding in verification["findings"]}
    assert verification["status"] == "blocked"
    assert verification["review_ready"] is False
    assert "approval_not_required" in codes
    assert "draft_auto_execute_enabled" in codes
    assert "dangerous_code_token" in codes
    assert "dangerous_command_token" in codes


def test_skill_draft_release_gate_requires_publish_evidence() -> None:
    client = TestClient(app)
    draft_response = client.post(
        "/api/skills/draft",
        json={
            "source": "unit-test",
            "incident": {
                "fault_type": "ProbeFailure",
                "message": "K8s readiness probe failed",
                "evidence": ["probe failed", "pod not ready"],
            },
        },
    )
    draft = draft_response.json()

    blocked = client.post(
        "/api/skills/draft/release-gate",
        json={"draft": draft, "evidence": {"unit_tests_passed": True}},
    )
    assert blocked.status_code == 200, blocked.text
    blocked_body = blocked.json()
    blocked_codes = {finding["code"] for finding in blocked_body["findings"]}
    assert blocked_body["status"] == "blocked"
    assert blocked_body["publish_ready"] is False
    assert "release_gate_dry_run_passed_missing" in blocked_codes
    assert "release_gate_human_approved_missing" in blocked_codes

    ready = client.post(
        "/api/skills/draft/release-gate",
        json={
            "draft": draft,
            "evidence": {
                "unit_tests_passed": True,
                "dry_run_passed": True,
                "forward_tests_passed": True,
                "rollback_verified": True,
                "human_approved": True,
                "publish_target": "skill_runtime/skills/k8s_probe_failure",
                "draft_sha256": blocked_body["draft_sha256"],
            },
        },
    )
    assert ready.status_code == 200, ready.text
    ready_body = ready.json()
    assert ready_body["status"] == "publish_ready"
    assert ready_body["publish_ready"] is True
    assert ready_body["publish_plan"]["auto_enable"] is False
    assert ready_body["publish_plan"]["mode"] == "manual_reviewed_copy"


def test_tool_skills_are_read_only_or_blocked() -> None:
    k8s = executor.run(
        "tool.k8s_readiness",
        {"message": "k8s service readiness probe failed", "namespace": "default", "deployment": "frontend"},
        dry_run=True,
    ).to_dict()
    db = executor.run(
        "tool.db_connection_pool",
        {"message": "postgres too many connections and lock wait timeout", "db_kind": "postgres"},
        dry_run=True,
    ).to_dict()

    assert k8s["policy"]["risk"] == "low"
    assert k8s["policy"]["supports_heal"] is False
    assert k8s["stages"]["diagnose"]["output"]["commands"]
    assert "heal" not in k8s["stages"]
    assert db["policy"]["heal_ready"] is False
    assert db["policy"]["supports_heal"] is False
    assert "heal_stage_not_declared" in db["policy"]["heal_blockers"]
    assert db["stages"]["diagnose"]["output"]["commands"]


def test_additional_k8s_skills_cover_common_faults() -> None:
    image_pull = executor.run(
        "k8s.image_pull_error",
        {
            "message": "Pod api is ImagePullBackOff because registry pull image failed",
            "namespace": "default",
            "deployment": "api",
            "pod": "api-abc",
            "container": "api",
            "current_image": "repo/api:bad",
            "last_good_image": "repo/api:stable",
        },
        dry_run=True, tool_runner=_fixture_image_tool,
    ).to_dict()
    node = executor.run(
        "k8s.node_not_ready",
        {"message": "node gpu-01 is NodeNotReady because kubelet not ready", "node": "gpu-01"},
        dry_run=True,
    ).to_dict()
    dns = executor.run(
        "k8s.dns_resolution",
        {"message": "CoreDNS SERVFAIL causes service name resolution timeout", "namespace": "default"},
        dry_run=True,
    ).to_dict()
    net = executor.run(
        "k8s.network_policy_deny",
        {
            "message": "NetworkPolicy egress denied causes connection timeout",
            "namespace": "default",
            "service": "checkout",
        },
        dry_run=True,
    ).to_dict()

    assert image_pull["stages"]["detect"]["status"] == "matched"
    assert any("set image deployment/api api=repo/api:stable" in item["command"] for item in image_pull["stages"]["heal"]["output"]["suggestions"])
    assert node["policy"]["risk"] == "high"
    assert any("cordon" in item["command"] for item in node["stages"]["heal"]["output"]["suggestions"])
    assert dns["stages"]["detect"]["status"] == "matched"
    assert any("coredns" in item["command"] for item in dns["stages"]["heal"]["output"]["suggestions"])
    assert net["policy"]["supports_heal"] is False
    assert "heal" not in net["stages"]
    assert net["skill"]["permissions"] == ["k8s.read"]
    assert all(command.startswith("kubectl get ") or command.startswith("kubectl describe ") for command in net["stages"]["diagnose"]["output"]["commands"])


def test_compare_legacy_api_runs_dual_path() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/compare/legacy",
        json={
            "payload": {
                "namespace": "default",
                "fault_type": "ServiceUnavailable",
                "message": "service frontend returns 503 because no endpoints",
                "service": "frontend",
                "deployment": "frontend",
            },
            "domain": "k8s",
            "dry_run": True,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["comparison"]["has_skill_match"] is True
    assert "k8s.service_unavailable" in body["comparison"]["skill_ids"]
    assert body["legacy"]["status"] == "ok"
    assert body["comparison"]["has_legacy_suggestions"] is True


def test_skill_workflow_uses_skill_primary_for_k8s_closed_loop() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "namespace": "default",
            "fault_type": "ServiceUnavailable",
            "message": "service frontend returns 503 because no endpoints and deployment scaled to zero",
            "service": "frontend",
            "deployment": "frontend",
            "diagnosis": {
                "fault_type": "ServiceUnavailable",
                "root_cause": "frontend deployment scaled to zero leaves service without endpoints",
                "root_cause_component": "frontend",
                "confidence": 0.93,
                "evidence_summary": {"deployment": "replicas=0", "endpoints": "empty"},
            },
        },
        domain="k8s",
        dry_run=True,
    )

    assert result["status"] == "completed"
    assert result["migration"]["mode"] == "skill_primary"
    assert result["diagnosis"]["source"] == "skill_runtime"
    assert result["diagnosis"]["skill_id"] == "k8s.service_unavailable"
    assert result["capability"]["available"] is True
    assert result["capability"]["requires_approval"] is True
    assert result["execution_plan"]["dry_run"] is True
    assert any("scale deployment/frontend" in cmd for cmd in result["execution_plan"]["commands"])
    assert result["verification_plan"]["checks"]
    assert result["heal_compat_payload"]["diagnosis_mode"] == "skill_workflow"
    assert result["heal_compat_payload"]["commands"] == result["execution_plan"]["commands"]
    assert result["heal_compat_payload"]["diagnosis"]["confidence"] == result["diagnosis"]["confidence"]
    assert result["diagnosis"]["confidence"] <= 0.60
    assert result["diagnosis"]["confidence_explanation"]["upstream_confidence_ignored"] == 0.93
    assert result["heal_compat_payload"]["diagnosis"]["confidence_raw_upstream"] == 0.93
    assert result["capability"]["heal_ready"] is False
    assert result["capability"]["confidence"] == result["diagnosis"]["confidence"]


def test_skill_workflow_uses_config_missing_primary_skill() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "namespace": "default",
            "fault_type": "ConfigMissing",
            "message": "configmap app-config not found secret not registered",
            "pod": "frontend-abc",
            "deployment": "frontend",
            "diagnosis": {
                "fault_type": "ConfigMissing",
                "root_cause": "missing configmap app-config",
                "root_cause_component": "frontend",
                "confidence": 0.91,
                "evidence_summary": {"event": "configmap not found"},
            },
        },
        domain="k8s",
        dry_run=True,
    )

    assert result["migration"]["mode"] == "skill_primary"
    assert result["migration"]["coverage_gap"] is False
    assert result["migration"]["bridge_skill_used"] is False
    assert result["skill_runtime"]["primary_skill_id"] == "k8s.config_missing"
    assert result["capability"]["source"] == "skill_runtime"
    assert result["execution_plan"]["commands"]
    assert result["diagnosis"]["source"] == "skill_runtime"
    assert result["diagnosis"]["confidence_explanation"]["method"] == "skill_evidence_calibrated_v2"
    assert any("rollout restart deployment/frontend" in cmd for cmd in result["execution_plan"]["commands"])


def test_skill_workflow_uses_volume_mount_primary_skill() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "namespace": "default",
            "fault_type": "VolumeMountFailed",
            "message": "FailedMount MountVolume FailedAttachVolume for pod frontend",
            "pod": "frontend-abc",
            "deployment": "frontend",
            "diagnosis": {
                "fault_type": "VolumeMountFailed",
                "root_cause": "volume mount failed",
                "root_cause_component": "frontend",
                "confidence": 0.9,
                "evidence_summary": {"event": "FailedMount"},
            },
        },
        domain="k8s",
        dry_run=True,
    )

    assert result["migration"]["mode"] == "skill_primary"
    assert result["migration"]["coverage_gap"] is False
    assert result["migration"]["bridge_skill_used"] is False
    assert result["skill_runtime"]["primary_skill_id"] == "k8s.volume_mount_failed"
    assert result["diagnosis"]["confidence_explanation"]["method"] == "skill_evidence_calibrated_v2"
    assert any("kubectl get pvc -n default -o wide" in cmd for cmd in result["execution_plan"]["commands"])
    assert any("kubectl delete pod frontend-abc" in cmd for cmd in result["execution_plan"]["commands"])


def test_skill_workflow_uses_hpa_scaling_primary_skill() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "namespace": "default",
            "fault_type": "HPAScalingFailed",
            "message": "FailedGetResourceMetric FailedGetScale unable to get target metrics",
            "deployment": "frontend",
            "diagnosis": {
                "fault_type": "HPAScalingFailed",
                "root_cause": "unable to get target metrics",
                "root_cause_component": "frontend",
                "confidence": 0.9,
                "evidence_summary": {"event": "FailedGetResourceMetric"},
            },
        },
        domain="k8s",
        dry_run=True,
    )

    assert result["migration"]["mode"] == "skill_primary"
    assert result["migration"]["coverage_gap"] is False
    assert result["migration"]["bridge_skill_used"] is False
    assert result["skill_runtime"]["primary_skill_id"] == "k8s.hpa_scaling_failed"
    assert result["diagnosis"]["confidence_explanation"]["method"] == "skill_evidence_calibrated_v2"
    assert any("kubectl get apiservice v1beta1.metrics.k8s.io" in cmd for cmd in result["execution_plan"]["commands"])
    assert any("kubectl scale deployment/frontend" in cmd for cmd in result["execution_plan"]["commands"])


def test_skill_workflow_uses_resource_quota_primary_skill() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "namespace": "default",
            "fault_type": "ResourceQuotaExceeded",
            "message": "exceeded quota forbidden: quota ResourceQuota denied the request",
            "deployment": "frontend",
            "diagnosis": {
                "fault_type": "ResourceQuotaExceeded",
                "root_cause": "namespace quota exceeded",
                "root_cause_component": "frontend",
                "confidence": 0.9,
                "evidence_summary": {"event": "forbidden quota"},
            },
        },
        domain="k8s",
        dry_run=True,
    )

    assert result["migration"]["mode"] == "skill_primary"
    assert result["migration"]["coverage_gap"] is False
    assert result["migration"]["bridge_skill_used"] is False
    assert result["skill_runtime"]["primary_skill_id"] == "k8s.resource_quota_exceeded"
    assert result["capability"]["requires_approval"] is False
    assert result["diagnosis"]["confidence_explanation"]["method"] == "skill_evidence_calibrated_v2"
    assert any("kubectl get resourcequota -n default" in cmd for cmd in result["execution_plan"]["commands"])
    assert not any("patch resourcequota" in cmd or "apply -f" in cmd for cmd in result["execution_plan"]["commands"])


def test_skill_workflow_uses_deployment_unhealthy_primary_skill() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "namespace": "default",
            "fault_type": "DeploymentUnhealthy",
            "message": "readiness probe failed replicas unavailable deployment frontend",
            "deployment": "frontend",
            "service": "frontend",
            "diagnosis": {
                "fault_type": "DeploymentUnhealthy",
                "root_cause": "readiness probe failed",
                "root_cause_component": "frontend",
                "confidence": 0.9,
                "evidence_summary": {"event": "readiness probe failed"},
            },
        },
        domain="k8s",
        dry_run=True,
    )

    assert result["migration"]["mode"] == "skill_primary"
    assert result["migration"]["coverage_gap"] is False
    assert result["migration"]["bridge_skill_used"] is False
    assert result["skill_runtime"]["primary_skill_id"] == "k8s.deployment_unhealthy"
    assert any("rollout restart deployment/frontend" in cmd for cmd in result["execution_plan"]["commands"])
    assert any("rollout undo deployment/frontend" in cmd for cmd in result["execution_plan"]["commands"])


def test_skill_workflow_uses_scheduling_failed_primary_skill() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "namespace": "default",
            "fault_type": "SchedulingFailed",
            "message": "FailedScheduling Unschedulable Insufficient cpu node(s) had taint",
            "pod": "frontend-abc",
            "deployment": "frontend",
            "diagnosis": {
                "fault_type": "SchedulingFailed",
                "root_cause": "pod is unschedulable",
                "root_cause_component": "frontend-abc",
                "confidence": 0.88,
                "evidence_summary": {"event": "FailedScheduling"},
            },
        },
        domain="k8s",
        dry_run=True,
    )

    assert result["migration"]["mode"] == "skill_primary"
    assert result["skill_runtime"]["primary_skill_id"] == "k8s.scheduling_failed"
    assert result["capability"]["requires_approval"] is False
    assert any("kubectl top node" in cmd for cmd in result["execution_plan"]["commands"])
    assert not any(" taint " in cmd or "patch" in cmd for cmd in result["execution_plan"]["commands"])


def test_skill_workflow_uses_oom_killed_primary_skill() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "namespace": "default",
            "fault_type": "OOMKilled",
            "message": "container OOMKilled exit code 137 memory limit exceeded",
            "pod": "frontend-abc",
            "deployment": "frontend",
            "container": "app",
            "diagnosis": {
                "fault_type": "OOMKilled",
                "root_cause": "container exceeded memory limit",
                "root_cause_component": "frontend",
                "confidence": 0.9,
                "evidence_summary": {"event": "OOMKilled"},
            },
        },
        domain="k8s",
        dry_run=True,
    )

    assert result["migration"]["mode"] == "skill_primary"
    assert result["skill_runtime"]["primary_skill_id"] == "k8s.oom_killed"
    assert result["capability"]["requires_approval"] is True
    assert any("kubectl top pod frontend-abc" in cmd for cmd in result["execution_plan"]["commands"])
    assert any("rollout restart deployment/frontend" in cmd for cmd in result["execution_plan"]["commands"])


def test_skill_workflow_prefers_node_not_ready_over_control_plane_kubelet_signal() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "domain": "k8s",
            "namespace": "default",
            "fault_type": "node_not_ready",
            "raw_fault_type": "NodeNotReady",
            "node": "worker-1",
            "message": "Node worker-1 NotReady kubelet stopped node condition false",
        },
        domain="k8s",
        stages=["detect", "diagnose", "heal", "verify"],
        dry_run=True,
    )

    assert result["skill_runtime"]["primary_skill_id"] == "k8s.node_not_ready"
    assert result["diagnosis"]["fault_type"] == "NodeNotReady"
    assert result["diagnosis"]["root_cause_component"] == "worker-1"


def test_skill_workflow_uses_resource_pressure_skill() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "namespace": "default",
            "fault_type": "DiskPressure",
            "message": "node DiskPressure filesystem pressure disk full",
            "node": "worker-1",
            "diagnosis": {
                "fault_type": "DiskPressure",
                "root_cause": "node disk full",
                "root_cause_component": "worker-1",
                "confidence": 0.88,
                "evidence_summary": {"condition": "DiskPressure"},
            },
        },
        domain="k8s",
        dry_run=True,
    )

    assert result["migration"]["mode"] == "skill_primary"
    assert result["skill_runtime"]["primary_skill_id"] == "k8s.resource_pressure"
    assert result["capability"]["requires_risk_ack"] is True
    assert any("kubectl describe node worker-1" in cmd for cmd in result["execution_plan"]["commands"])
    assert any("kubectl cordon worker-1" in cmd for cmd in result["execution_plan"]["commands"])


def test_k8s_faultlab_skill_matrix_diagnosis_and_healing_are_correct() -> None:
    workflow = SkillWorkflow(executor)
    cases = [
        {
            "name": "service_unavailable",
            "primary": "k8s.service_unavailable",
            "payload": {
                "namespace": "test-social-network",
                "fault_type": "service_unavailable",
                "raw_fault_type": "service_unavailable",
                "service": "home-timeline-service",
                "deployment": "home-timeline-service",
                "root_cause_component": "home-timeline-service",
                "message": "503 Service Unavailable, no ready endpoints for home-timeline-service",
            },
            "root_terms": ["home-timeline-service", "endpoints"],
            "command_terms": [
                "kubectl get endpoints -n test-social-network",
                "kubectl scale deployment/home-timeline-service -n test-social-network --replicas=1",
            ],
        },
        {
            "name": "pod_crashloop",
            "primary": "k8s.pod_crashloop",
            "payload": {
                "namespace": "test-social-network",
                "fault_type": "pod_crashloop",
                "raw_fault_type": "crash_loop",
                "pod": "crash-test",
                "deployment": "crash-test",
                "service": "crash-test",
                "root_cause_component": "crash-test",
                "message": "Pod restart count increasing, CrashLoopBackOff, back-off restarting failed container",
            },
            "root_terms": ["crash-test", "CrashLoopBackOff"],
            "command_terms": [
                "kubectl logs crash-test -n test-social-network --tail=120 --previous",
                "kubectl rollout undo deployment/crash-test -n test-social-network",
            ],
        },
        {
            "name": "oom_killed",
            "primary": "k8s.oom_killed",
            "payload": {
                "namespace": "test-social-network",
                "fault_type": "oom_killed",
                "raw_fault_type": "memory_pressure",
                "pod": "mem-stress",
                "deployment": "user-timeline-service",
                "service": "user-timeline-service",
                "root_cause_component": "user-timeline-service",
                "message": "Container terminated with exit code 137, OOMKilled, node memory pressure above 90%",
            },
            "root_terms": ["mem-stress", "OOMKilled"],
            "command_terms": [
                "kubectl top pod mem-stress -n test-social-network",
                "kubectl rollout restart deployment/user-timeline-service -n test-social-network",
            ],
        },
        {
            "name": "resource_pressure",
            "primary": "k8s.resource_pressure",
            "payload": {
                "namespace": "test-social-network",
                "fault_type": "resource_pressure",
                "raw_fault_type": "cpu_pressure",
                "deployment": "compose-post-service",
                "service": "compose-post-service",
                "node": "worker-1",
                "root_cause_component": "compose-post-service",
                "message": "High CPU usage, CPU throttling, resource pressure, API latency increased to 2000ms",
            },
            "root_terms": ["HighCPUUsage", "资源压力"],
            "command_terms": [
                "kubectl top pod -n test-social-network --sort-by=cpu",
                "kubectl rollout restart deployment/compose-post-service -n test-social-network",
            ],
        },
        {
            "name": "network_policy_deny",
            "primary": "k8s.network_policy_deny",
            "payload": {
                "namespace": "test-social-network",
                "fault_type": "network_policy_deny",
                "raw_fault_type": "network_partition",
                "service": "nginx-thrift",
                "deployment": "nginx-thrift",
                "source_service": "frontend",
                "target_service": "nginx-thrift",
                "root_cause_component": "test-social-network",
                "message": "NetworkPolicy deny or NetworkChaos blocks traffic from frontend to nginx-thrift, dependency timeout",
            },
            "root_terms": ["frontend", "nginx-thrift", "网络策略"],
            "command_terms": [
                "kubectl get networkpolicy -n test-social-network -o wide",
                "kubectl get networkchaos -n test-social-network -o wide",
            ],
        },
        {
            "name": "scheduling_failed",
            "primary": "k8s.scheduling_failed",
            "payload": {
                "namespace": "test-social-network",
                "fault_type": "scheduling_failed",
                "raw_fault_type": "scheduling_capacity_failure",
                "pod": "pending-worker",
                "deployment": "pending-worker",
                "service": "pending-worker",
                "root_cause_component": "pending-worker",
                "message": "FailedScheduling, pod is Unschedulable because insufficient cpu and node capacity",
            },
            "root_terms": ["pending-worker", "无法调度"],
            "command_terms": [
                "kubectl describe pod pending-worker -n test-social-network",
                "kubectl top node",
            ],
        },
        {
            "name": "config_missing",
            "primary": "k8s.config_missing",
            "payload": {
                "namespace": "test-social-network",
                "fault_type": "config_missing",
                "raw_fault_type": "config_missing",
                "pod": "config-test",
                "deployment": "config-test",
                "service": "config-test",
                "config_name": "missing-config",
                "root_cause_component": "config-test",
                "message": "ConfigMap missing-config not found, secret key missing, pod cannot start due to missing configuration",
            },
            "root_terms": ["config-test", "missing-config"],
            "command_terms": [
                "kubectl get configmap/missing-config -n test-social-network -o yaml",
                "kubectl rollout restart deployment/config-test -n test-social-network",
            ],
        },
    ]

    for case in cases:
        result = workflow.run(
            {"source": "faultlab", "domain": "k8s", **case["payload"]},
            domain="k8s",
            stages=["detect", "diagnose", "heal", "verify"],
            dry_run=True,
            allow_legacy_fallback=True,
        )
        commands = result["execution_plan"]["commands"]
        root_cause = result["diagnosis"]["root_cause"]
        assert result["status"] == "completed", case["name"]
        assert result["dry_run"] is True, case["name"]
        assert result["skill_runtime"]["primary_skill_id"] == case["primary"], case["name"]
        assert all(term in root_cause for term in case["root_terms"]), case["name"]
        assert all(any(term in cmd for cmd in commands) for term in case["command_terms"]), case["name"]
        assert result["verification_plan"]["checks"], case["name"]
        assert result["diagnosis"]["confidence"] <= 0.60, case["name"]
        assert result["diagnosis"]["confidence_explanation"]["evidence"]["observed_count"] == 0, case["name"]


def test_skill_workflow_uses_control_plane_skill() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "namespace": "kube-system",
            "fault_type": "ApiServerUnhealthy",
            "message": "apiserver target down API server health check failed",
            "diagnosis": {
                "fault_type": "ApiServerUnhealthy",
                "root_cause": "api server health check failed",
                "root_cause_component": "kube-apiserver",
                "confidence": 0.9,
                "evidence_summary": {"metric": "apiserver down"},
            },
        },
        domain="k8s",
        dry_run=True,
    )

    assert result["migration"]["mode"] == "skill_primary"
    assert result["skill_runtime"]["primary_skill_id"] == "k8s.control_plane_unhealthy"
    assert result["capability"]["requires_risk_ack"] is True
    assert any("component=kube-apiserver" in cmd for cmd in result["execution_plan"]["commands"])


def test_skill_workflow_uses_observability_signal_skill() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "namespace": "default",
            "fault_type": "TargetDown",
            "message": "Prometheus TargetDown scrape failed target disappeared",
            "service": "frontend",
            "deployment": "frontend",
            "diagnosis": {
                "fault_type": "TargetDown",
                "root_cause": "prometheus target disappeared",
                "root_cause_component": "frontend",
                "confidence": 0.82,
                "evidence_summary": {"metric": "TargetDown"},
            },
        },
        domain="k8s",
        dry_run=True,
    )

    assert result["migration"]["mode"] == "skill_primary"
    assert result["skill_runtime"]["primary_skill_id"] == "k8s.observability_signal"
    assert any("kubectl get svc,endpoints -n default -o wide" in cmd for cmd in result["execution_plan"]["commands"])
    assert not any("rollout restart" in cmd for cmd in result["execution_plan"]["commands"])


def test_skill_workflow_uses_bridge_skill_for_unmigrated_legacy_recipe() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "namespace": "default",
            "fault_type": "AppArmorProfileUnsupported",
            "message": "AppArmor vArmor CreateContainerError profile unsupported",
            "pod": "frontend-abc",
            "diagnosis": {
                "fault_type": "AppArmorProfileUnsupported",
                "root_cause": "security profile unsupported",
                "root_cause_component": "frontend-abc",
                "confidence": 0.88,
                "evidence_summary": {"event": "profile unsupported"},
            },
        },
        domain="k8s",
        dry_run=True,
    )

    assert result["migration"]["mode"] == "skill_bridge"
    assert result["migration"]["coverage_gap"] is True
    assert result["migration"]["bridge_skill_used"] is True
    assert result["skill_runtime"]["primary_skill_id"] == "k8s.legacy_recipe_bridge"
    assert result["diagnosis"]["confidence_explanation"]["cap"] <= 0.88


def test_skill_workflow_gate_uses_calibrated_confidence_and_diagnostic_evidence() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "namespace": "default",
            "fault_type": "ServiceUnavailable",
            "message": "service unavailable",
            "service": "frontend",
            "diagnosis": {
                "fault_type": "ServiceUnavailable",
                "root_cause": "unknown",
                "confidence": 0.97,
            },
        },
        domain="k8s",
        dry_run=True,
    )

    assert result["diagnosis"]["confidence"] <= 0.72
    assert result["diagnosis"]["skill_id"] == "k8s.service_unavailable"
    assert result["diagnosis"]["confidence_explanation"]["method"] == "skill_evidence_calibrated_v2"
    assert result["diagnosis"]["evidence"] == ["请求返回 503 或服务不可用"]
    assert result["capability"]["heal_ready"] is False
    assert "low_confidence_for_heal" in result["capability"]["heal_blockers"]
    assert "insufficient_evidence" in result["capability"]["heal_blockers"]
    assert result["capability"]["confidence"] == result["diagnosis"]["confidence"]
    assert result["capability"]["evidence_count"] == 1


def test_skill_workflow_handles_llm_host_readiness_without_legacy() -> None:
    workflow = SkillWorkflow(executor)

    result = workflow.run(
        {
            "host_id": "33.33.33.128",
            "message": "bare-metal GPU vLLM host readiness",
            "ssh_status": "ok",
            "gpu_count": 3,
            "process_count": 1,
            "endpoint_status": "ok",
            "metrics_status": "ok",
            "metrics_count": 30,
        },
        domain="llm",
        dry_run=True,
        allow_legacy_fallback=False,
    )

    assert result["migration"]["mode"] == "skill_primary"
    assert result["diagnosis"]["skill_id"] == "tool.llm_host_readiness"
    assert result["capability"]["requires_approval"] is False
    assert all(item["risk"] == "low" for item in result["execution_plan"]["suggestions"])
    assert any("nvidia-smi" in cmd for cmd in result["execution_plan"]["commands"])


def test_skill_workflow_api_operate_endpoint() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/workflows/operate",
        json={
            "payload": {
                "namespace": "default",
                "fault_type": "ServiceUnavailable",
                "message": "service frontend returns 503 because no endpoints",
                "service": "frontend",
                "deployment": "frontend",
                "diagnosis": {
                    "fault_type": "ServiceUnavailable",
                    "root_cause": "frontend has no endpoints",
                    "root_cause_component": "frontend",
                    "confidence": 0.93,
                    "evidence_summary": {"endpoints": "empty", "status": "503"},
                },
            },
            "domain": "k8s",
            "dry_run": True,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["migration"]["mode"] == "skill_primary"
    assert body["skill_runtime"]["primary_skill_id"] == "k8s.service_unavailable"
    assert body["execution_plan"]["commands"]
    assert body["heal_compat_payload"]["commands"] == body["execution_plan"]["commands"]
    assert body["workflow_run_id"].startswith("skillwf-")


def test_skill_workflow_api_records_runs() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/workflows/operate",
        json={
            "payload": {
                "namespace": "default",
                "fault_type": "ServiceUnavailable",
                "message": "service frontend returns 503 because no endpoints",
                "service": "frontend",
                "deployment": "frontend",
            },
            "domain": "k8s",
            "dry_run": True,
        },
    )

    assert response.status_code == 200, response.text
    run_id = response.json()["workflow_run_id"]
    history = client.get("/api/workflows/runs?limit=5")
    assert history.status_code == 200, history.text
    assert any(item["id"] == run_id for item in history.json()["runs"])
    detail = client.get(f"/api/workflows/runs/{run_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["id"] == run_id
    assert body["result"]["skill_runtime"]["runs"]


def test_legacy_dashboard_exposes_skill_workflow_endpoint() -> None:
    from web_app.app import app as dashboard_app

    client = TestClient(dashboard_app)
    response = client.post(
        "/api/skill-workflows/operate",
        json={
            "payload": {
                "namespace": "default",
                "fault_type": "ServiceUnavailable",
                "message": "service frontend returns 503 because no endpoints",
                "service": "frontend",
                "deployment": "frontend",
                "diagnosis": {
                    "fault_type": "ServiceUnavailable",
                    "root_cause": "frontend has no endpoints",
                    "root_cause_component": "frontend",
                    "confidence": 0.93,
                    "evidence_summary": {"endpoints": "empty", "status": "503"},
                },
            },
            "domain": "k8s",
            "dry_run": True,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["migration"]["mode"] == "skill_primary"
    assert body["heal_compat_payload"]["source"] == "skill_workflow"


def test_legacy_dashboard_exposes_skill_catalog(monkeypatch) -> None:
    import web_app.app as dashboard

    def fail_remote_catalog() -> dict:
        raise RuntimeError("runtime unavailable")

    monkeypatch.setattr(dashboard, "_get_skill_runtime_skills", fail_remote_catalog)
    client = TestClient(dashboard.app)
    response = client.get("/api/skill-workflows/skills")

    assert response.status_code == 200, response.text
    body = response.json()
    ids = {item["id"] for item in body["skills"]}
    assert "k8s.service_unavailable" in ids
    service_skill = next(item for item in body["skills"] if item["id"] == "k8s.service_unavailable")
    assert service_skill["source_status"]["has_markdown"] is True
    assert service_skill["source_status"]["documentation_label"] == "完整说明"
    assert body["dashboard_bridge"]["mode"] == "local_fallback"


def test_legacy_dashboard_exposes_skill_detail_sources(monkeypatch) -> None:
    import web_app.app as dashboard

    def fail_remote_detail(path: str) -> dict:
        raise RuntimeError(f"runtime unavailable: {path}")

    monkeypatch.setattr(dashboard, "_get_skill_runtime_json", fail_remote_detail)
    client = TestClient(dashboard.app)
    response = client.get("/api/skill-workflows/skills/llm.vllm_process_crash")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["metadata"]["id"] == "llm.vllm_process_crash"
    assert "vLLM 进程崩溃" in body["markdown"]
    assert body["markdown_source"] in {"SKILL.md", "generated_from_skill_yaml"}
    assert "id: llm.vllm_process_crash" in body["yaml"]
    assert "def " in body["python"]
    assert body["files"]["yaml"] == "skill.yaml"
    assert body["files"]["python"] == "skill.py"
    assert body["source_status"]["has_yaml"] is True
    assert body["source_files"]["python"]["exists"] is True
    assert body["dashboard_bridge"]["mode"] == "local_fallback"


def test_legacy_dashboard_proxies_standalone_skill_detail(monkeypatch) -> None:
    import web_app.app as dashboard

    def fake_runtime_json(path: str) -> dict:
        assert path == "/api/skills/llm.vllm_process_crash/detail"
        return {
            "metadata": {"id": "llm.vllm_process_crash", "name": "vLLM Process Crash"},
            "markdown": "# vLLM Process Crash\n\nRuntime detail.",
            "markdown_source": "SKILL.md",
            "yaml": "id: llm.vllm_process_crash\n",
            "python": "def diagnose(context):\n    return {}\n",
            "files": {"yaml": "skill.yaml", "python": "skill.py"},
        }

    monkeypatch.setattr(dashboard, "_get_skill_runtime_json", fake_runtime_json)
    client = TestClient(dashboard.app)
    response = client.get("/api/skill-workflows/skills/llm.vllm_process_crash")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["metadata"]["id"] == "llm.vllm_process_crash"
    assert body["markdown_source"] == "SKILL.md"
    assert body["dashboard_bridge"]["mode"] == "http_proxy"


def test_legacy_dashboard_rejects_unsafe_skill_and_run_ids() -> None:
    import web_app.app as dashboard

    client = TestClient(dashboard.app)
    skill_response = client.get("/api/skill-workflows/skills/bad%24id")
    run_response = client.get("/api/skill-workflows/runs/bad%24id")

    assert skill_response.status_code == 400, skill_response.text
    assert run_response.status_code == 400, run_response.text


def test_dashboard_groups_ai_clusters(monkeypatch) -> None:
    import web_app.app as dashboard

    cfg = dashboard._get_config()
    old_clusters = list(cfg.fault_targets.k8s_clusters)
    old_hosts = list(cfg.fault_targets.llm_hosts)
    cfg.fault_targets.k8s_clusters = [
        {"id": "general", "name": "通算", "kind": "general"},
        {"id": "ai-t4", "name": "T4 智算", "kind": "ai", "prometheus_url": "http://127.0.0.1:19090"},
    ]
    cfg.fault_targets.llm_hosts = [
        {"id": "gpu-1", "name": "GPU-1", "host": "33.33.33.127", "ai_cluster_id": "ai-t4"}
    ]
    try:
        client = TestClient(dashboard.app)
        response = client.get("/api/clusters")
        assert response.status_code == 200, response.text
        body = response.json()
        assert [item["id"] for item in body["ai_clusters"]] == ["ai-t4"]
        assert [item["id"] for item in body["general_clusters"]] == ["general"]
    finally:
        cfg.fault_targets.k8s_clusters = old_clusters
        cfg.fault_targets.llm_hosts = old_hosts


def test_ai_cluster_dashboard_degrades_without_prometheus(monkeypatch) -> None:
    import web_app.app as dashboard

    cfg = dashboard._get_config()
    old_clusters = list(cfg.fault_targets.k8s_clusters)
    old_hosts = list(cfg.fault_targets.llm_hosts)
    cfg.fault_targets.k8s_clusters = [
        {"id": "ai-t4", "name": "T4 智算", "kind": "ai", "prometheus_url": "http://127.0.0.1:19090"}
    ]
    cfg.fault_targets.llm_hosts = [
        {"id": "gpu-1", "name": "GPU-1", "host": "33.33.33.127", "ai_cluster_id": "ai-t4"}
    ]
    monkeypatch.setattr(dashboard, "_prometheus_api_ok", lambda _: False)
    try:
        client = TestClient(dashboard.app)
        response = client.get("/api/ai-clusters/ai-t4/dashboard?duration=5m")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["mode"] == "profile_only"
        assert body["storage"]["available"] is False
        assert body["hosts"][0]["id"] == "gpu-1"
    finally:
        cfg.fault_targets.k8s_clusters = old_clusters
        cfg.fault_targets.llm_hosts = old_hosts


def test_ai_cluster_compat_endpoints_expose_topology_and_metrics(monkeypatch) -> None:
    import web_app.app as dashboard

    cfg = dashboard._get_config()
    old_clusters = list(cfg.fault_targets.k8s_clusters)
    old_hosts = list(cfg.fault_targets.llm_hosts)
    cfg.fault_targets.k8s_clusters = [
        {"id": "general", "name": "通算", "kind": "general"},
        {"id": "ai-compat", "name": "智算兼容", "kind": "ai", "default_namespace": "llm-k8s"},
    ]
    cfg.fault_targets.llm_hosts = [
        {"id": "gpu-127", "name": "dds-127", "host": "33.33.33.127", "ai_cluster_id": "ai-compat"}
    ]

    async def fake_collect_ai_k8s_inventory(cluster):
        return {
            "available": True,
            "nodes": [{"name": "dds-127", "ready": True}],
            "pods": [{"name": "vllm-server-0", "namespace": "llm-k8s", "node": "dds-127", "phase": "Running", "labels": {"app": "vllm-server"}}],
            "services": [{"name": "vllm-server", "namespace": "llm-k8s", "selector": {"app": "vllm-server"}}],
        }

    monkeypatch.setattr(dashboard, "_prometheus_api_ok", lambda url: False)
    monkeypatch.setattr(dashboard, "_collect_ai_k8s_inventory", fake_collect_ai_k8s_inventory)
    monkeypatch.setattr(dashboard, "_summarize_teller_trace", lambda trace_dir: {"available": False, "summary": {}})
    try:
        client = TestClient(dashboard.app)
        catalog = client.get("/api/ai-clusters")
        assert catalog.status_code == 200, catalog.text
        assert [item["id"] for item in catalog.json()["clusters"]] == ["ai-compat"]
        assert catalog.json()["hosts"][0]["id"] == "gpu-127"

        topology = client.get("/api/ai-clusters/ai-compat/topology?duration=5m")
        assert topology.status_code == 200, topology.text
        assert topology.json()["topology"]["nodes"][0]["name"] == "dds-127"
        assert topology.json()["topology"]["pods"][0]["name"] == "vllm-server-0"

        metrics = client.get("/api/ai-clusters/ai-compat/metrics?duration=5m")
        assert metrics.status_code == 200, metrics.text
        assert "summary" in metrics.json()
        assert "charts" in metrics.json()
    finally:
        cfg.fault_targets.k8s_clusters = old_clusters
        cfg.fault_targets.llm_hosts = old_hosts


def test_ai_cluster_dashboard_includes_teller_trace_summary(monkeypatch, tmp_path) -> None:
    import json
    import web_app.app as dashboard

    trace_dir = tmp_path / "teller-trace"
    trace_dir.mkdir()
    (trace_dir / "rank0.jsonl").write_text(
        "\n".join([
            json.dumps({"t": "KERNEL", "n": "flash_attention_fwd", "gs": 1000, "ge": 41000, "c": 1}),
            json.dumps({"t": "KERNEL", "n": "flash_attention_fwd", "gs": 50000, "d": 20000, "c": 2}),
            json.dumps({"t": "NVTX_MARKER", "n": "step_execute_model", "ts": 900, "id": 11, "pid": 7, "tid": 8}),
            json.dumps({"t": "NVTX_MARKER", "n": "null", "ts": 80900, "id": 11, "pid": 7, "tid": 8}),
            json.dumps({"t": "RUNTIME", "n": "cudaLaunchKernel_v7000", "s": 100, "e": 500, "pid": 7, "tid": 8}),
        ]),
        encoding="utf-8",
    )
    (trace_dir / "events.csv").write_text(
        "type,name,start_us,duration_us,pid,tid\n"
        "NVTX,decode_step,100,2400,7,8\n"
        "DRIVER,cuLaunchKernel,120,350,7,8\n",
        encoding="utf-8",
    )
    (trace_dir / "teller_trace.json").write_text(
        json.dumps({
            "meta": {"run_id": "req-run", "task_type": "vllm", "tag": "ai-teller/vllm-server"},
            "trace": [
                {"type": "ENGINE", "name": "step_execute_model[request_ids:req-a;req-b]", "start": 1000, "dur": 90000, "pid": 77, "tid": 3},
                {"type": "KERNEL", "name": "paged_attention", "start": 2000, "dur": 30000, "pid": 77, "tid": 0},
            ],
        }),
        encoding="utf-8",
    )

    cfg = dashboard._get_config()
    old_clusters = list(cfg.fault_targets.k8s_clusters)
    old_hosts = list(cfg.fault_targets.llm_hosts)
    cfg.fault_targets.k8s_clusters = [
        {
            "id": "ai-teller",
            "name": "TELLER 智算",
            "kind": "ai",
            "prometheus_url": "http://127.0.0.1:19090",
            "teller_trace_dir": str(trace_dir),
        }
    ]
    cfg.fault_targets.llm_hosts = []
    monkeypatch.setattr(dashboard, "_prometheus_api_ok", lambda _: False)
    try:
        client = TestClient(dashboard.app)
        response = client.get("/api/ai-clusters/ai-teller/dashboard?duration=5m")
        assert response.status_code == 200, response.text
        trace = response.json()["fine_grained_trace"]
        assert trace["available"] is True
        assert trace["summary"]["kernel_events"] == 3
        assert trace["summary"]["nvtx_events"] == 2
        assert trace["summary"]["engine_events"] == 1
        assert trace["summary"]["runtime_events"] == 1
        assert trace["summary"]["driver_events"] == 1
        assert trace["top_kernels"][0]["name"] == "flash_attention_fwd"
        assert trace["top_kernels"][0]["count"] == 2
        assert trace["top_kernels"][0]["p95_ms"] > 0
        assert trace["top_runtime"]
        assert any(row["type"] == "KERNEL" and row["p95_ms"] > 0 for row in trace["event_type_stats"])
        assert trace["key_durations"][0]["duration_ms"] >= trace["key_durations"][-1]["duration_ms"]
        assert trace["bottlenecks"]
        assert trace["timeline"]["events"]
        assert trace["timeline"]["time_range_ns"][1] > trace["timeline"]["time_range_ns"][0]
        assert trace["timeline"]["display_time_range_ns"][1] > trace["timeline"]["display_time_range_ns"][0]
        assert trace["timeline"]["display_note"]
        assert all("display_start_ns" in event for event in trace["timeline"]["events"])
        assert trace["request_context"]["available"] is True
        assert trace["request_context"]["request_count"] >= 2
        assert trace["e2e_trace"]["available"] is True
        assert trace["e2e_trace"]["schema_version"] == "agenticsre.ai_trace.v1"
        assert trace["e2e_trace"]["request_count"] >= 2
        e2e_req = next(item for item in trace["e2e_trace"]["requests"] if item["request_id"] == "req-a")
        node_ids = {node["id"] for node in e2e_req["nodes"]}
        assert {"http_ingress", "vllm_engine", "cuda_runtime", "gpu_kernel"}.issubset(node_ids)
        assert e2e_req["coverage"] == "partial_e2e"
        assert trace["e2e_trace"]["gaps"]
        request_ids = {item["request_id"] for item in trace["request_context"]["requests"]}
        assert {"req-a", "req-b"}.issubset(request_ids)
        req_a = next(item for item in trace["request_context"]["requests"] if item["request_id"] == "req-a")
        assert any(ev["name"] == "paged_attention" for ev in req_a["path_events"])
        assert any(stage["type"] == "KERNEL" for stage in req_a["stages"])
        assert req_a["critical_path"]
        assert response.json()["teller_integration"]["mode"] == "real_teller_vllm_wrapper"
        assert "teller.cli run" in response.json()["teller_integration"]["wrapped_command"]
    finally:
        cfg.fault_targets.k8s_clusters = old_clusters
        cfg.fault_targets.llm_hosts = old_hosts


def test_teller_trace_summary_parses_native_steps_tree(tmp_path) -> None:
    import json
    import web_app.app as dashboard

    trace_dir = tmp_path / "teller-steps"
    trace_dir.mkdir()
    (trace_dir / "trace.json").write_text(
        json.dumps({
            "steps": [
                {
                    "step_name": "prefill_step",
                    "start": 1_000,
                    "end": 101_000,
                    "torch_frontend_ops": [
                        {
                            "op_name": "aten::matmul",
                            "start": 2_000,
                            "end": 92_000,
                            "torch_backend_ops": [
                                {
                                    "op_name": "cudaLaunchKernel",
                                    "start": 3_000,
                                    "end": 82_000,
                                    "runtime_calls": [
                                        {
                                            "name": "cudaLaunchKernel",
                                            "start": 4_000,
                                            "end": 8_000,
                                            "kernels": [
                                                {"name": "gemm_kernel", "gpu_start": 9_000, "duration": 51_000}
                                            ],
                                        }
                                    ],
                                    "driver_calls": [
                                        {
                                            "name": "cuLaunchKernel",
                                            "start": 8_500,
                                            "end": 9_500,
                                            "kernels": [
                                                {"name": "attention_kernel", "gpu_start": 61_000, "duration": 25_000}
                                            ],
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ]
        }),
        encoding="utf-8",
    )

    dashboard._TELLER_TRACE_SUMMARY_CACHE.clear()
    summary = dashboard._summarize_teller_trace(str(trace_dir))

    assert summary["available"] is True
    assert summary["hierarchy"]["available"] is True
    assert summary["hierarchy"]["step_count"] == 1
    assert summary["summary"]["frontend_events"] == 1
    assert summary["summary"]["backend_events"] == 1
    assert summary["summary"]["kernel_events"] == 2
    timeline_types = {event["type"] for event in summary["timeline"]["events"]}
    assert {"STEP", "FRONTEND", "BACKEND", "RUNTIME", "DRIVER", "KERNEL"}.issubset(timeline_types)
    assert any("prefill_step > aten::matmul" in (event.get("path") or "") for event in summary["timeline"]["events"])


def test_teller_trace_summary_keeps_native_engine_request_context(tmp_path) -> None:
    import json
    import web_app.app as dashboard

    trace_dir = tmp_path / "teller-native"
    trace_dir.mkdir()
    (trace_dir / "teller_trace.json").write_text(
        json.dumps({
            "meta": {"run_id": "native-run", "task_type": "vllm", "tag": "ai/vllm-server"},
            "trace": [
                {"name": "step_execute_model[request_ids:req-a_req-b]", "type": "ENGINE", "start": 10_000, "dur": 100_000, "pid": 42, "tid": 7},
                {"name": "torch.nn.modules.module.Module._call_impl", "type": "PYTORCH", "start": 12_000, "dur": 40_000, "pid": 42, "tid": 7},
                {"name": "aten::scaled_dot_product_attention", "type": "LIBTORCH", "start": 20_000, "dur": 30_000, "pid": 42, "tid": 7},
                {"name": "paged_attention_kernel", "type": "KERNEL", "start": 30_000, "dur": 18_000, "pid": 42, "tid": 0},
            ],
        }),
        encoding="utf-8",
    )

    dashboard._TELLER_TRACE_SUMMARY_CACHE.clear()
    summary = dashboard._summarize_teller_trace(str(trace_dir))

    assert summary["available"] is True
    assert summary["summary"]["engine_events"] == 1
    assert summary["summary"]["pytorch_events"] == 1
    assert summary["summary"]["libtorch_events"] == 1
    timeline_types = {event["type"] for event in summary["timeline"]["events"]}
    assert {"ENGINE", "PYTORCH", "LIBTORCH", "KERNEL"}.issubset(timeline_types)
    assert summary["request_context"]["available"] is True
    assert summary["e2e_trace"]["available"] is True
    assert summary["e2e_trace"]["requests"][0]["coverage"] == "partial_e2e"
    request_ids = {item["request_id"] for item in summary["request_context"]["requests"]}
    assert {"req-a", "req-b"}.issubset(request_ids)
    req_a = next(item for item in summary["request_context"]["requests"] if item["request_id"] == "req-a")
    assert any(stage["type"] == "ENGINE" for stage in req_a["stages"])
    assert any(event["type"] == "KERNEL" for event in req_a["path_events"])


def test_teller_trace_summary_builds_full_e2e_request_path_from_hooks(tmp_path) -> None:
    import json
    import time
    import web_app.app as dashboard

    trace_dir = tmp_path / "teller-full-e2e"
    trace_dir.mkdir()
    (trace_dir / "agenticsre_http_spans_pid100.jsonl").write_text(
        json.dumps({
            "type": "HTTP",
            "name": "POST /v1/chat/completions",
            "start": 1_000,
            "duration": 8_000_000,
            "pid": 100,
            "tid": 10,
            "request_id": "req-full",
            "method": "POST",
            "route": "/v1/chat/completions",
            "status_code": 200,
        }) + "\n",
        encoding="utf-8",
    )
    (trace_dir / "agenticsre_heartbeat_spans_pid100.jsonl").write_text(
        json.dumps({
            "type": "HEARTBEAT",
            "name": "agenticsre_teller_collector_alive",
            "start": time.time_ns(),
            "duration": 0,
            "pid": 100,
            "tid": 12,
        }) + "\n",
        encoding="utf-8",
    )
    (trace_dir / "teller_trace.json").write_text(
        json.dumps({
            "meta": {"run_id": "full-run", "task_type": "vllm", "tag": "ai/vllm-server"},
            "trace": [
                {"type": "ENGINE", "name": "step_execute_model[request_ids:req-full]", "start": 1_200, "dur": 6_500_000, "pid": 100, "tid": 11},
                {"type": "SCHEDULER", "name": "schedule_prefill", "start": 1_500, "dur": 300_000, "pid": 100, "tid": 11, "request_id": "req-full"},
                {"type": "PYTORCH", "name": "vllm.model_executor.models.qwen.QwenForCausalLM", "start": 2_000, "dur": 2_400_000, "pid": 101, "tid": 7, "request_id": "req-full"},
                {"type": "LIBTORCH", "name": "aten::scaled_dot_product_attention", "start": 2_400, "dur": 1_800_000, "pid": 101, "tid": 7, "request_id": "req-full"},
                {"type": "RUNTIME", "name": "cudaLaunchKernel_v7000", "start": 3_000, "dur": 200_000, "pid": 101, "tid": 7, "request_id": "req-full"},
                {"type": "KERNEL", "name": "flash_attention_fwd", "start": 3_500, "dur": 1_200_000, "pid": 101, "tid": 0, "request_id": "req-full"},
            ],
        }),
        encoding="utf-8",
    )

    dashboard._TELLER_TRACE_SUMMARY_CACHE.clear()
    summary = dashboard._summarize_teller_trace(str(trace_dir))

    assert summary["available"] is True
    assert summary["summary"]["http_events"] == 1
    assert summary["summary"]["scheduler_events"] == 1
    assert summary["request_context"]["available"] is True
    req = summary["request_context"]["requests"][0]
    assert req["request_id"] == "req-full"
    stage_types = {stage["type"] for stage in req["stages"]}
    assert {"HTTP", "ENGINE", "SCHEDULER", "PYTORCH", "LIBTORCH", "RUNTIME", "KERNEL"}.issubset(stage_types)
    e2e_req = summary["e2e_trace"]["requests"][0]
    assert summary["e2e_trace"]["coverage"] == "full_e2e"
    assert summary["e2e_trace"]["full_e2e_count"] == 1
    assert summary["trace_health"]["status"] in {"ready", "stale"}
    health_labels = {item["label"] for item in summary["trace_health"]["checks"] if item["ok"]}
    assert {"请求级上下文", "HTTP 入站", "PyTorch/框架层", "CUDA Runtime/Driver", "GPU Kernel"}.issubset(health_labels)
    assert e2e_req["coverage"] == "full_e2e"
    assert e2e_req["gaps"] == []
    node_status = {node["id"]: node["status"] for node in e2e_req["nodes"]}
    assert node_status == {
        "http_ingress": "observed",
        "vllm_engine": "observed",
        "pytorch_frontend": "observed",
        "cuda_runtime": "observed",
        "gpu_kernel": "observed",
    }
    assert "request" not in e2e_req["missing_layers"]
    assert not summary["e2e_trace"]["gaps"]
    timeline_types = {event["type"] for event in summary["timeline"]["events"]}
    assert {"HTTP", "ENGINE", "SCHEDULER", "PYTORCH", "LIBTORCH", "RUNTIME", "KERNEL"}.issubset(timeline_types)


def test_teller_trace_summary_uses_heartbeat_for_freshness(tmp_path) -> None:
    import json
    import time
    import web_app.app as dashboard

    trace_dir = tmp_path / "teller-heartbeat"
    trace_dir.mkdir()
    old_ns = int((time.time() - 900) * 1_000_000_000)
    now_ns = time.time_ns()
    (trace_dir / "agenticsre_http_spans_pid100.jsonl").write_text(
        json.dumps({
            "type": "HTTP",
            "name": "POST /v1/completions",
            "start": old_ns,
            "duration": 1_000_000,
            "request_id": "req-old",
            "pid": 100,
            "tid": 1,
            "status_code": 200,
        }) + "\n",
        encoding="utf-8",
    )
    (trace_dir / "agenticsre_heartbeat_pid100.jsonl").write_text(
        json.dumps({
            "type": "HEARTBEAT",
            "name": "agenticsre_teller_collector_alive",
            "start": now_ns,
            "duration": 0,
            "pid": 100,
            "tid": 2,
        }) + "\n",
        encoding="utf-8",
    )

    dashboard._TELLER_TRACE_SUMMARY_CACHE.clear()
    summary = dashboard._summarize_teller_trace(str(trace_dir))

    health = summary["trace_health"]
    assert health["status"] in {"ready", "partial"}
    assert any(item["label"] == "采集心跳" and item["ok"] for item in health["checks"])
    assert not any(item["label"] == "采集新鲜度" for item in health["checks"])


def test_agenticsre_teller_hook_writes_heartbeat(monkeypatch, tmp_path) -> None:
    import importlib
    import time

    monkeypatch.setenv("AGENTICSRE_TELLER_TRACE_DIR", str(tmp_path))
    monkeypatch.setenv("AGENTICSRE_TELLER_HEARTBEAT_INTERVAL", "5")
    hook = importlib.import_module("tools.teller.vllm_http_pytorch_hooks")
    monkeypatch.setattr(hook, "_INSTALLED", False)
    monkeypatch.setattr(hook, "_HEARTBEAT_STARTED", False)

    enabled = hook.install()
    deadline = time.time() + 2
    while time.time() < deadline and not list(tmp_path.glob("agenticsre_heartbeat_pid*.jsonl")):
        time.sleep(0.05)

    heartbeat_files = list(tmp_path.glob("agenticsre_heartbeat*_pid*.jsonl"))
    assert enabled["heartbeat"] is True
    assert heartbeat_files
    assert "HEARTBEAT" in heartbeat_files[0].read_text(encoding="utf-8")


def test_teller_trace_summary_merges_http_id_with_vllm_completion_id(tmp_path) -> None:
    import json
    import web_app.app as dashboard

    trace_dir = tmp_path / "teller-real-vllm"
    trace_dir.mkdir()
    (trace_dir / "agenticsre_http_spans_pid139.jsonl").write_text(
        json.dumps({
            "type": "HTTP",
            "name": "POST /v1/completions",
            "start": 1_000,
            "duration": 900_000_000,
            "pid": 139,
            "tid": 1,
            "request_id": "agenticsre-real-hook-1783391720-1",
            "method": "POST",
            "route": "/v1/completions",
            "status_code": 200,
        }) + "\n",
        encoding="utf-8",
    )
    (trace_dir / "request_markers_pid216.jsonl").write_text(
        "\n".join([
            json.dumps({"t": "NVTX_MARKER", "n": "step_execute_model[request_ids:cmpl-agenticsre-real-hook-1783391720-1-0]", "ts": 10_000, "id": 7, "pid": 216, "tid": 2}),
            json.dumps({"t": "NVTX_MARKER", "n": "[range_end]", "ts": 810_000_000, "id": 7, "pid": 216, "tid": 2}),
        ]) + "\n",
        encoding="utf-8",
    )
    (trace_dir / "output_pid216.tmp.jsonl").write_text(
        "\n".join([
            json.dumps({"t": "RUNTIME", "n": "cudaLaunchKernel_v7000", "s": 20_000, "e": 90_000, "pid": 216, "tid": 2}),
            json.dumps({"t": "KERNEL", "n": "flash_attention_fwd", "gs": 120_000, "ge": 500_000, "pid": 216, "tid": 0}),
        ]) + "\n",
        encoding="utf-8",
    )

    dashboard._TELLER_TRACE_SUMMARY_CACHE.clear()
    summary = dashboard._summarize_teller_trace(str(trace_dir))

    requests = summary["request_context"]["requests"]
    canonical = next(item for item in requests if item["request_id"] == "cmpl-agenticsre-real-hook-1783391720-1-0")
    assert not any(item["request_id"] == "agenticsre-real-hook-1783391720-1" for item in requests)
    assert any(stage["type"] == "HTTP" for stage in canonical["stages"])
    e2e = next(item for item in summary["e2e_trace"]["requests"] if item["request_id"] == canonical["request_id"])
    status_by_id = {node["id"]: node["status"] for node in e2e["nodes"]}
    assert status_by_id["http_ingress"] == "observed"
    assert status_by_id["vllm_engine"] == "observed"
    assert status_by_id["cuda_runtime"] == "observed"
    assert status_by_id["gpu_kernel"] == "observed"


def test_teller_trace_summary_splits_batched_request_ids_from_span_field(tmp_path) -> None:
    import json
    import web_app.app as dashboard

    trace_dir = tmp_path / "teller-batched-request-ids"
    trace_dir.mkdir()
    batch_ids = "cmpl-req-a-0_cmpl-req-b-0"
    (trace_dir / "agenticsre_http_spans_pid139.jsonl").write_text(
        "\n".join([
            json.dumps({
                "type": "HTTP",
                "name": "POST /v1/completions",
                "start": 1_000,
                "duration": 1_000_000,
                "request_id": "req-a",
                "pid": 139,
                "tid": 1,
                "status_code": 200,
            }),
            json.dumps({
                "type": "HTTP",
                "name": "POST /v1/completions",
                "start": 1_100,
                "duration": 1_000_000,
                "request_id": "req-b",
                "pid": 139,
                "tid": 2,
                "status_code": 200,
            }),
        ]) + "\n",
        encoding="utf-8",
    )
    (trace_dir / "request_markers_pid216.jsonl").write_text(
        "\n".join([
            json.dumps({"t": "NVTX_MARKER", "n": f"step_execute_model[request_ids:{batch_ids}]", "ts": 2_000, "id": 7, "pid": 216, "tid": 2}),
            json.dumps({"t": "NVTX_MARKER", "n": "[range_end]", "ts": 900_000, "id": 7, "pid": 216, "tid": 2}),
        ]) + "\n",
        encoding="utf-8",
    )
    (trace_dir / "agenticsre_pytorch_spans_pid216.jsonl").write_text(
        json.dumps({
            "type": "PYTORCH",
            "name": "worker_execute_model",
            "start": 4_000,
            "duration": 800_000,
            "request_ids": batch_ids,
            "pid": 216,
            "tid": 2,
        }) + "\n",
        encoding="utf-8",
    )

    dashboard._TELLER_TRACE_SUMMARY_CACHE.clear()
    summary = dashboard._summarize_teller_trace(str(trace_dir))

    requests = {item["request_id"]: item for item in summary["request_context"]["requests"]}
    assert "req-a" not in requests
    assert "req-b" not in requests
    assert {"cmpl-req-a-0", "cmpl-req-b-0"}.issubset(requests)
    for request_id in ("cmpl-req-a-0", "cmpl-req-b-0"):
        stage_types = {stage["type"] for stage in requests[request_id]["stages"]}
        assert {"HTTP", "NVTX", "PYTORCH"}.issubset(stage_types)
        e2e = next(item for item in summary["e2e_trace"]["requests"] if item["request_id"] == request_id)
        status_by_id = {node["id"]: node["status"] for node in e2e["nodes"]}
        assert status_by_id["http_ingress"] == "observed"
        assert status_by_id["vllm_engine"] == "observed"
        assert status_by_id["pytorch_frontend"] == "observed"


def test_teller_trace_summary_keeps_request_context_and_cupti_files(tmp_path) -> None:
    import json
    import web_app.app as dashboard

    trace_dir = tmp_path / "teller-priority"
    trace_dir.mkdir()
    (trace_dir / "output_pid216.tmp.jsonl").write_text(
        "\n".join(
            json.dumps({"t": "RUNTIME", "n": "cudaLaunchKernel_v7000", "s": 2_000 + i, "e": 2_100 + i, "pid": 216, "tid": 2})
            for i in range(50)
        ) + "\n",
        encoding="utf-8",
    )
    (trace_dir / "request_markers_pid216.jsonl").write_text(
        "\n".join([
            json.dumps({"t": "NVTX_MARKER", "n": "step_execute_model[request_ids:req-priority]", "ts": 1_000, "id": 9, "pid": 216, "tid": 2}),
            json.dumps({"t": "NVTX_MARKER", "n": "[range_end]", "ts": 9_000, "id": 9, "pid": 216, "tid": 2}),
        ]) + "\n",
        encoding="utf-8",
    )
    for i in range(8):
        (trace_dir / f"agenticsre_heartbeat_spans_pid{i}.jsonl").write_text(
            json.dumps({"type": "HEARTBEAT", "name": "heartbeat", "start": 10_000 + i, "duration": 0}) + "\n",
            encoding="utf-8",
        )

    files = dashboard._find_teller_event_files(str(trace_dir), limit=4)
    names = [p.name for p in files]
    assert files[0].name == "request_markers_pid216.jsonl"
    assert "output_pid216.tmp.jsonl" in names

    dashboard._TELLER_TRACE_SUMMARY_CACHE.clear()
    summary = dashboard._summarize_teller_trace(str(trace_dir), max_events=80, max_files=4)
    assert summary["summary"]["nvtx_events"] == 1
    assert summary["summary"]["runtime_events"] == 50
    assert summary["request_context"]["available"] is True
    req = summary["request_context"]["requests"][0]
    assert req["request_id"] == "req-priority"
    assert any(stage["type"] == "NVTX" for stage in req["stages"])
    assert any(stage["type"] == "RUNTIME" for stage in req["stages"])


def test_teller_trace_summary_joins_sidecar_request_markers_with_kernel_events(tmp_path) -> None:
    import json
    import web_app.app as dashboard

    trace_dir = tmp_path / "teller-sidecar"
    run_dir = trace_dir / "vllm-run"
    run_dir.mkdir(parents=True)
    (run_dir / "request_markers_pid89.jsonl").write_text(
        "\n".join([
            json.dumps({"t": "NVTX_MARKER", "n": "step_execute_model[request_ids:chatcmpl_abc_123]", "ts": 1_000, "id": 7, "pid": 89, "tid": 123}),
            json.dumps({"t": "NVTX_MARKER", "n": "[range_end]", "ts": 90_000, "id": 7, "pid": 89, "tid": 123}),
        ]) + "\n",
        encoding="utf-8",
    )
    (run_dir / "output_pid89.tmp.jsonl").write_text(
        "\n".join([
            json.dumps({"t": "RUNTIME", "n": "cudaLaunchKernel_v7000", "s": 2_000, "e": 3_000, "pid": 89, "tid": 123}),
            json.dumps({"t": "KERNEL", "n": "paged_attention_kernel", "gs": 5_000, "ge": 45_000, "c": 1}),
        ]) + "\n",
        encoding="utf-8",
    )

    dashboard._TELLER_TRACE_SUMMARY_CACHE.clear()
    summary = dashboard._summarize_teller_trace(str(trace_dir))

    assert summary["available"] is True
    assert summary["request_context"]["available"] is True
    req = summary["request_context"]["requests"][0]
    assert req["request_id"] == "chatcmpl_abc_123"
    assert req["kernel_events"] == 1
    assert any(event["name"] == "paged_attention_kernel" for event in req["path_events"])


def test_ai_dashboard_syncs_teller_trace_from_k8s_before_summary(monkeypatch, tmp_path) -> None:
    import json
    import web_app.app as dashboard

    trace_dir = tmp_path / "local-trace"
    cfg = dashboard._get_config()
    old_clusters = list(cfg.fault_targets.k8s_clusters)
    old_hosts = list(cfg.fault_targets.llm_hosts)
    cfg.fault_targets.k8s_clusters = [
        {
            "id": "ai-sync",
            "name": "TELLER Sync",
            "kind": "ai",
            "default_namespace": "llm-k8s",
            "vllm_deployment": "vllm-server",
            "vllm_container": "vllm",
            "teller_trace_dir": str(trace_dir),
            "teller_runtime_trace_root": "/opt/teller/data",
        }
    ]
    cfg.fault_targets.llm_hosts = []
    commands = []

    def fake_shell(cmd, timeout=90, cluster_id=None, env=None):
        commands.append(cmd)
        if "get pods" in cmd:
            return {
                "ok": True,
                "stdout": json.dumps({"items": [{"metadata": {"name": "vllm-server-0"}, "status": {"phase": "Running"}}]}),
                "stderr": "",
                "command": cmd,
            }
        if "find /opt/teller/data/trace" in cmd:
            return {"ok": True, "stdout": "/opt/teller/data/trace/run/request_markers_pid89.jsonl", "stderr": "", "command": cmd}
        if "kubectl" in cmd and " cp " in cmd:
            return {"ok": True, "stdout": "copied", "stderr": "", "command": cmd}
        return {"ok": True, "stdout": "", "stderr": "", "command": cmd}

    async def fake_inventory(cluster):
        return {"available": True, "nodes": [], "pods": [], "services": []}

    monkeypatch.setattr(dashboard, "_shell_sync", fake_shell)
    monkeypatch.setattr(dashboard, "_prometheus_api_ok", lambda _: False)
    monkeypatch.setattr(dashboard, "_collect_ai_k8s_inventory", fake_inventory)
    monkeypatch.setattr(dashboard, "_summarize_teller_trace", lambda trace, *args, **kwargs: {"available": True, "summary": {}, "request_context": {"available": True, "request_count": 1}})
    try:
        client = TestClient(dashboard.app)
        response = client.get("/api/ai-clusters/ai-sync/dashboard?duration=5m")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["teller_trace_sync"]["ok"] is True
        assert body["teller_trace_sync"]["copied_pods"] == 1
        assert body["teller_trace_sync"]["copied_dirs"] == [str(trace_dir / "vllm-server-0")]
        assert any("kubectl" in cmd and " cp " in cmd for cmd in commands)
    finally:
        cfg.fault_targets.k8s_clusters = old_clusters
        cfg.fault_targets.llm_hosts = old_hosts


def test_ai_dashboard_node_metric_specs_cover_basic_host_resources() -> None:
    import web_app.app as dashboard

    specs = dashboard._node_metric_specs()
    expected = {
        "node_cpu_usage",
        "node_memory_usage",
        "node_rootfs_usage",
        "node_disk_read",
        "node_disk_write",
        "node_network_receive",
        "node_network_transmit",
    }
    assert expected.issubset(specs)
    assert "node_cpu_seconds_total" in specs["node_cpu_usage"]["query"]
    assert "node_memory_MemAvailable_bytes" in specs["node_memory_usage"]["query"]
    assert "max by(instance)" in specs["node_rootfs_usage"]["query"]
    assert "node_disk_read_bytes_total" in specs["node_disk_read"]["query"]
    assert "node_disk_written_bytes_total" in specs["node_disk_write"]["query"]
    assert "node_network_receive_bytes_total" in specs["node_network_receive"]["query"]
    assert "node_network_transmit_bytes_total" in specs["node_network_transmit"]["query"]


def test_ai_dashboard_returns_node_resource_charts(monkeypatch, tmp_path) -> None:
    import web_app.app as dashboard

    cfg = dashboard._get_config()
    old_clusters = list(cfg.fault_targets.k8s_clusters)
    old_hosts = list(cfg.fault_targets.llm_hosts)
    cfg.fault_targets.k8s_clusters = [
        {
            "id": "ai-node-metrics",
            "name": "Node Metrics",
            "kind": "ai",
            "prometheus_url": "http://prometheus.example",
            "default_namespace": "llm-k8s",
            "teller_trace_dir": str(tmp_path),
        }
    ]
    cfg.fault_targets.llm_hosts = []

    async def fake_query(base_url, specs, *, start_ts, end_ts, step, label_key="model_name"):
        return {
            key: {
                "title": spec["title"],
                "unit": spec["unit"],
                "query": spec["query"],
                "series": [{"label": "dds-127", "timestamps": [1], "values": [12.5]}],
            }
            for key, spec in specs.items()
        }

    async def fake_inventory(cluster):
        return {
            "available": True,
            "namespace": "llm-k8s",
            "nodes": [{"name": "dds-127", "ready": True}],
            "pods": [{"name": "vllm-0", "phase": "Running", "ready": 1, "containers": 1}],
            "services": [],
        }

    monkeypatch.setattr(dashboard, "_reachable_cluster_prometheus_url", lambda cluster, url="": "http://prometheus.example")
    monkeypatch.setattr(dashboard, "_prometheus_api_ok", lambda _: True)
    monkeypatch.setattr(dashboard, "_query_metric_charts", fake_query)
    monkeypatch.setattr(dashboard, "_summarize_prometheus_inventory", lambda _: {"targets": {"available": True, "targets": []}, "gpu_devices": [], "vllm_series": []})
    monkeypatch.setattr(dashboard, "_collect_ai_k8s_inventory", fake_inventory)
    monkeypatch.setattr(dashboard, "_summarize_teller_trace", lambda *args, **kwargs: {"available": False, "summary": {}, "request_context": {"available": False}})
    try:
        client = TestClient(dashboard.app)
        response = client.get(f"/api/ai-clusters/ai-node-metrics/dashboard?duration=5m&teller_trace_dir={tmp_path}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["mode"] == "prometheus"
        assert "node" in body["charts"]
        assert "node_cpu_usage" in body["charts"]["node"]
        assert body["summary"]["node_cpu_usage"] == 12.5
        assert body["topology"]["pods"][0]["name"] == "vllm-0"
        assert "节点基础资源" in body["storage"]["message"]
    finally:
        cfg.fault_targets.k8s_clusters = old_clusters
        cfg.fault_targets.llm_hosts = old_hosts


def test_ai_event_thresholds_trigger_node_resource_pressure(monkeypatch) -> None:
    import web_app.app as dashboard

    cfg = dashboard._get_config()
    old_thresholds = dict(getattr(cfg.detection, "thresholds", {}) or {})
    cfg.detection.thresholds = {
        **old_thresholds,
        "ai": {
            **((old_thresholds.get("ai") if isinstance(old_thresholds.get("ai"), dict) else {}) or {}),
            "node_cpu_usage": 10,
            "node_memory_usage": 90,
            "node_rootfs_usage": 90,
            "node_disk_read": 500,
            "node_disk_write": 500,
            "node_network_receive": 500,
            "node_network_transmit": 500,
        },
    }
    payload = {
        "cluster": {"id": "ai-threshold", "name": "AI Threshold", "kind": "ai"},
        "storage": {"available": True},
        "summary": {
            "running_requests": 0,
            "waiting_requests": 0,
            "ttft_p95": 0,
            "e2e_latency_p95": 0,
            "kv_cache": 0,
            "gpu_util": 0,
            "gpu_memory_used": 0,
            "success_rate": 1,
            "node_cpu_usage": 35,
            "node_memory_usage": 20,
            "node_rootfs_usage": 10,
            "node_disk_read": 1,
            "node_disk_write": 1,
            "node_network_receive": 1,
            "node_network_transmit": 1,
        },
    }
    try:
        events = dashboard._generate_ai_events_from_dashboard_payload(payload)
        node_events = [event for event in events if event.get("event_type") == "node_resource_pressure"]
        assert len(node_events) == 1
        assert node_events[0]["metric"] == "node_resources"
        assert node_events[0]["value"]["node_cpu_usage"] == 35
        assert node_events[0]["threshold"]["node_cpu_usage"] == 10
        assert "基础资源压力" in node_events[0]["title"]
    finally:
        cfg.detection.thresholds = old_thresholds


def test_ai_event_threshold_zero_is_valid_configuration(monkeypatch) -> None:
    import web_app.app as dashboard

    cfg = dashboard._get_config()
    old_thresholds = dict(getattr(cfg.detection, "thresholds", {}) or {})
    cfg.detection.thresholds = {
        **old_thresholds,
        "ai": {
            **((old_thresholds.get("ai") if isinstance(old_thresholds.get("ai"), dict) else {}) or {}),
            "node_cpu_usage": 0,
        },
    }
    payload = {
        "cluster": {"id": "ai-zero-threshold", "name": "AI Zero Threshold", "kind": "ai"},
        "storage": {"available": True},
        "summary": {
            "running_requests": 0,
            "waiting_requests": 0,
            "ttft_p95": 0,
            "e2e_latency_p95": 0,
            "kv_cache": 0,
            "gpu_util": 0,
            "gpu_memory_used": 0,
            "success_rate": 1,
            "node_cpu_usage": 0,
        },
    }
    try:
        events = dashboard._generate_ai_events_from_dashboard_payload(payload)
        node_events = [event for event in events if event.get("event_type") == "node_resource_pressure"]
        assert len(node_events) == 1
        assert node_events[0]["threshold"]["node_cpu_usage"] == 0
    finally:
        cfg.detection.thresholds = old_thresholds


def test_ai_dashboard_explicit_teller_trace_dir_skips_k8s_sync(monkeypatch, tmp_path) -> None:
    import web_app.app as dashboard

    trace_dir = tmp_path / "explicit-trace"
    trace_dir.mkdir()
    cfg = dashboard._get_config()
    old_clusters = list(cfg.fault_targets.k8s_clusters)
    old_hosts = list(cfg.fault_targets.llm_hosts)
    cfg.fault_targets.k8s_clusters = [
        {
            "id": "ai-explicit",
            "name": "Explicit Trace",
            "kind": "ai",
            "default_namespace": "llm-k8s",
            "teller_trace_dir": str(tmp_path),
        }
    ]
    cfg.fault_targets.llm_hosts = []
    sync_called = {"value": False}

    def fake_sync(*args, **kwargs):
        sync_called["value"] = True
        return {"ok": True, "copied_dirs": [str(tmp_path / "synced")]}

    def fake_summary(trace, *args, **kwargs):
        return {
            "available": True,
            "trace_dir": trace,
            "summary": {},
            "request_context": {"available": False, "request_count": 0, "requests": []},
            "e2e_trace": {"available": False, "requests": [], "gaps": []},
        }

    monkeypatch.setattr(dashboard, "_prometheus_api_ok", lambda _: False)
    monkeypatch.setattr(dashboard, "_collect_ai_k8s_inventory", lambda cluster: {"available": False, "nodes": [], "pods": [], "services": []})
    monkeypatch.setattr(dashboard, "_sync_teller_trace_from_k8s", fake_sync)
    monkeypatch.setattr(dashboard, "_summarize_teller_trace", fake_summary)
    try:
        client = TestClient(dashboard.app)
        response = client.get(f"/api/ai-clusters/ai-explicit/dashboard?teller_trace_dir={trace_dir}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert sync_called["value"] is False
        assert body["teller_trace_sync"]["skipped"] is True
        assert body["fine_grained_trace"]["trace_dir"] == str(trace_dir)
    finally:
        cfg.fault_targets.k8s_clusters = old_clusters
        cfg.fault_targets.llm_hosts = old_hosts


def test_teller_trace_summary_filters_by_current_deployment_prefix(tmp_path) -> None:
    import json
    import web_app.app as dashboard

    trace_dir = tmp_path / "mixed-trace"
    canary = trace_dir / "vllm-teller-canary-20260706-010101"
    server = trace_dir / "vllm-server-20260706-020202"
    canary.mkdir(parents=True)
    server.mkdir(parents=True)
    (canary / "request_markers_pid89.jsonl").write_text(
        "\n".join([
            json.dumps({"t": "NVTX_MARKER", "n": "step_execute_model[request_ids:old-canary]", "ts": 1_000, "id": 1, "pid": 89, "tid": 1}),
            json.dumps({"t": "NVTX_MARKER", "n": "[range_end]", "ts": 20_000, "id": 1, "pid": 89, "tid": 1}),
        ]) + "\n",
        encoding="utf-8",
    )
    (canary / "output_pid89.tmp.jsonl").write_text(
        json.dumps({"t": "KERNEL", "n": "old_kernel", "gs": 2_000, "ge": 10_000, "pid": 89, "tid": 1}) + "\n",
        encoding="utf-8",
    )
    (server / "request_markers_pid90.jsonl").write_text(
        "\n".join([
            json.dumps({"t": "NVTX_MARKER", "n": "step_execute_model[request_ids:real-server]", "ts": 1_000, "id": 2, "pid": 90, "tid": 1}),
            json.dumps({"t": "NVTX_MARKER", "n": "[range_end]", "ts": 50_000, "id": 2, "pid": 90, "tid": 1}),
        ]) + "\n",
        encoding="utf-8",
    )
    (server / "output_pid90.tmp.jsonl").write_text(
        json.dumps({"t": "KERNEL", "n": "real_kernel", "gs": 2_000, "ge": 40_000, "pid": 90, "tid": 1}) + "\n",
        encoding="utf-8",
    )

    dashboard._TELLER_TRACE_SUMMARY_CACHE.clear()
    summary = dashboard._summarize_teller_trace(str(trace_dir), run_prefix="vllm-server")

    request_ids = {item["request_id"] for item in summary["request_context"]["requests"]}
    assert "real-server" in request_ids
    assert "old-canary" not in request_ids
    req = next(item for item in summary["request_context"]["requests"] if item["request_id"] == "real-server")
    assert any(event["name"] == "real_kernel" for event in req["path_events"])
    assert not any(event["name"] == "old_kernel" for event in req["path_events"])


def test_ai_event_generation_includes_teller_trace_signals(monkeypatch) -> None:
    import web_app.app as dashboard

    # The fixture describes request traces. Live host pressure is a separate
    # incident and must not change this synthetic event's routing assertion.
    monkeypatch.setattr(dashboard, "_ai_event_live_observation", lambda payload: {})

    payload = {
        "cluster": {"id": "ai-teller", "name": "TELLER 智算", "kind": "ai"},
        "summary": {},
        "storage": {"available": True},
        "fine_grained_trace": {
            "available": True,
            "trace_dir": "/data/teller/trace",
            "summary": {"total_events": 10, "kernel_events": 4, "runtime_events": 6, "kernel_p95_ms": 80, "hierarchy_events": 6},
            "request_context": {
                "available": True,
                "request_count": 1,
                "requests": [{"request_id": "req-a", "span_ms": 1500, "kernel_events": 4, "runtime_events": 6}],
            },
            "top_kernels": [{"name": "paged_attention", "count": 4, "p95_ms": 80}],
        },
    }

    events = dashboard._generate_ai_events_from_dashboard_payload(payload)
    event_types = {event["event_type"] for event in events}

    assert "teller_request_latency_high" in event_types
    assert "teller_kernel_bottleneck" in event_types
    event = next(item for item in events if item["event_type"] == "teller_request_latency_high")
    assert event["fine_grained_trace"]["request_context"]["requests"][0]["request_id"] == "req-a"
    skill_payload = dashboard._ai_event_to_skill_payload(event)
    assert skill_payload["fine_grained_trace_summary"]["kernel_p95_ms"] == 80
    assert skill_payload["request_context"]["request_count"] == 1
    assert skill_payload["teller_max_request_span_ms"] == 1500
    assert skill_payload["fault_type"] == "teller_request_latency_high"

    result = SkillWorkflow(executor).run(
        skill_payload,
        domain="llm",
        stages=["detect", "diagnose", "heal", "verify"],
        dry_run=True,
    )
    assert result["skill_runtime"]["primary_skill_id"] == "llm.teller_request_trace_diagnosis"
    assert result["diagnosis"]["fault_type"].startswith("LLM::teller")
    assert "TELLER" in result["diagnosis"]["root_cause"]


def test_ai_k8s_event_routes_to_k8s_orchestration_skill() -> None:
    import web_app.app as dashboard

    event = {
        "id": "aik8s-demo",
        "source": "k8s_event",
        "event_type": "k8s_failedscheduling",
        "severity": "critical",
        "title": "K8s Event: FailedScheduling",
        "description": "0/4 nodes are available: node(s) didn't match Pod's node affinity/selector.",
        "cluster_id": "t4-vllm-k8s",
        "cluster_name": "T4 vLLM 智算集群",
        "namespace": "llm-k8s",
        "deployment": "vllm-server",
        "service": "vllm-server",
        "model": "qwen2.5-coder-3b-k8s",
        "metric": "k8s_event",
        "fault_type": "scheduling_failed",
        "k8s_object_kind": "Pod",
        "k8s_object_name": "agenticsre-schedule-demo",
        "k8s_reason": "FailedScheduling",
        "evidence": [
            {"source": "k8s_event", "signal": "reason", "value": "FailedScheduling"},
            {"source": "k8s_event", "signal": "object", "value": "Pod/agenticsre-schedule-demo"},
        ],
    }

    skill_payload = dashboard._ai_event_to_skill_payload(event)
    assert skill_payload["domain"] == "k8s"
    assert skill_payload["fault_type"] == "scheduling_failed"
    assert skill_payload["pod"] == "agenticsre-schedule-demo"
    assert skill_payload["object"] == "Pod/agenticsre-schedule-demo"

    result = SkillWorkflow(executor).run(
        skill_payload,
        domain=skill_payload["domain"],
        stages=["detect", "diagnose"],
        dry_run=True,
    )
    assert result["skill_runtime"]["primary_skill_id"] == "k8s.scheduling_failed"
    assert result["diagnosis"]["fault_type"] == "SchedulingFailed"
    assert result["diagnosis"]["root_cause_component"] == "agenticsre-schedule-demo"


def test_ai_pod_health_generates_active_k8s_event(monkeypatch) -> None:
    import web_app.app as dashboard

    cluster = {
        "id": "ai-live",
        "name": "实时智算",
        "kind": "ai",
        "default_namespace": "observability",
        "deployment": "vllm-server",
    }
    seen_commands: list[tuple[str, str]] = []

    async def fake_kubectl_json(cmd, namespace="", cluster_id=None):
        seen_commands.append((cmd, namespace))
        if cmd == "get pods":
            return {"items": []}
        assert cmd == "get pods --all-namespaces"
        return {
            "items": [
                {
                    "metadata": {"name": "agenticsre-schedule-demo", "namespace": "llm-k8s"},
                    "spec": {
                        "nodeName": "",
                        "containers": [{"name": "vllm", "image": "vllm/vllm-openai:latest"}],
                    },
                    "status": {
                        "phase": "Pending",
                        "conditions": [
                            {
                                "type": "PodScheduled",
                                "status": "False",
                                "reason": "Unschedulable",
                                "message": "0/2 nodes are available: Insufficient nvidia.com/gpu.",
                            }
                        ],
                    },
                }
            ]
        }

    monkeypatch.setattr(dashboard, "_kubectl_json", fake_kubectl_json)
    events = asyncio.run(dashboard._generate_ai_events_from_pod_health(cluster, window_s=3600))

    assert len(events) == 1
    event = events[0]
    assert event["source"] == "k8s_pod_health"
    assert event["status"] == "active"
    assert event["fault_type"] == "scheduling_failed"
    assert event["k8s_object_kind"] == "Pod"
    assert event["k8s_object_name"] == "agenticsre-schedule-demo"
    assert "Pod 状态异常" in event["title"]
    assert ("get pods", "observability") in seen_commands
    assert ("get pods --all-namespaces", "") in seen_commands

    skill_payload = dashboard._ai_event_to_skill_payload(event)
    assert skill_payload["domain"] == "k8s"
    assert skill_payload["fault_type"] == "scheduling_failed"
    assert skill_payload["pod"] == "agenticsre-schedule-demo"


def test_ai_events_refresh_generates_realtime_metric_events(monkeypatch) -> None:
    import web_app.app as dashboard

    cfg = dashboard._get_config()
    old_clusters = list(cfg.fault_targets.k8s_clusters)
    cfg.fault_targets.k8s_clusters = [
        {"id": "ai-live", "name": "实时智算", "kind": "ai", "default_namespace": "llm-k8s"}
    ]
    stored_events: list[dict] = []

    async def fake_dashboard(cluster, duration="15m", prometheus_url="", **kwargs):
        return {
            "cluster": {"id": cluster.get("id"), "name": "实时智算", "kind": "ai"},
            "storage": {"available": True},
            "summary": {
                "waiting_requests": 3,
                "running_requests": 1,
                "ttft_p95": 2.4,
                "e2e_latency_p95": 4.2,
                "kv_cache": 45,
                "gpu_memory_used": 60,
                "success_rate": 1.0,
            },
        }

    seen_windows: dict[str, int] = {}

    async def fake_k8s_events(cluster, *, window_s=0, **kwargs):
        seen_windows["k8s"] = window_s
        return []

    async def fake_pod_health(cluster, *, window_s=0, **kwargs):
        seen_windows["pod_health"] = window_s
        return []

    monkeypatch.setattr(dashboard, "_ai_event_light_dashboard_payload", fake_dashboard)
    monkeypatch.setattr(dashboard, "_generate_ai_events_from_k8s_events", fake_k8s_events)
    monkeypatch.setattr(dashboard, "_generate_ai_events_from_pod_health", fake_pod_health)
    monkeypatch.setattr(
        dashboard,
        "_generate_ai_events_from_fault_runs",
        lambda cluster, *, window_s=0, **kwargs: seen_windows.setdefault("fault_runs", window_s) and [],
    )
    monkeypatch.setattr(dashboard, "_load_fault_experiments", lambda: [])
    monkeypatch.setattr(dashboard, "_load_ai_events", lambda: list(stored_events))

    def save_events(events):
        stored_events[:] = list(events)

    monkeypatch.setattr(dashboard, "_save_ai_events", save_events)
    try:
        client = TestClient(dashboard.app)
        response = client.post("/api/ai-events/refresh", json={"cluster_id": "ai-live", "duration": "5m"})
        assert response.status_code == 200, response.text
        body = response.json()
        event_types = {item["event_type"] for item in body["generated"]}
        assert "vllm_queue_backlog" in event_types
        assert "inference_latency_high" in event_types
        assert body["count"] >= 2
        assert body["window"] == {"duration": "5m", "seconds": 300}
        assert seen_windows == {"k8s": 300, "pod_health": 300, "fault_runs": 300}
        assert any(item["status"] in {"active", "observing"} for item in body["events"])
    finally:
        cfg.fault_targets.k8s_clusters = old_clusters


def test_ai_event_light_payload_does_not_block_on_range_or_teller(monkeypatch) -> None:
    import web_app.app as dashboard

    monkeypatch.setattr(dashboard, "_reachable_cluster_prometheus_url", lambda cluster, url="": "http://prom")
    monkeypatch.setattr(dashboard, "_prometheus_api_ok", lambda base_url: True)
    monkeypatch.setattr(dashboard, "_prom_instant_first_value", lambda base_url, query: 1.0)

    async def fail_range(*args, **kwargs):
        raise AssertionError("event refresh must not issue range chart queries")

    def fail_teller(*args, **kwargs):
        raise AssertionError("event refresh must not synchronously scan TELLER traces")

    monkeypatch.setattr(dashboard, "_query_metric_charts", fail_range)
    monkeypatch.setattr(dashboard, "_summarize_teller_trace", fail_teller)
    payload = asyncio.run(dashboard._ai_event_light_dashboard_payload(
        {"id": "ai-fast", "name": "AI Fast", "kind": "ai"},
        duration="5m",
    ))
    assert payload["mode"] == "event_light"
    assert payload["storage"]["available"] is True
    assert payload["summary"]["waiting_requests"] == 1.0
    assert payload["peaks"]["waiting_requests"] == 1.0
    assert "轻量检测" in payload["fine_grained_trace"]["request_context"]["message"]


def test_teller_request_trace_diagnosis_skill_matches_request_path() -> None:
    payload = {
        "domain": "llm",
        "namespace": "llm-k8s",
        "deployment": "vllm-server",
        "fault_type": "teller_request_latency_high",
        "message": "TELLER 请求级执行耗时升高",
        "fine_grained_trace": {
            "available": True,
            "summary": {
                "kernel_events": 23809,
                "runtime_events": 28918,
                "driver_events": 36,
                "kernel_p95_ms": 0.358,
                "request_count": 1,
            },
            "request_context": {
                "available": True,
                "request_count": 1,
                "coverage": "request_level",
                "requests": [
                    {
                        "request_id": "cmpl-test-0",
                        "span_ms": 3018.089,
                        "event_count": 47547,
                        "kernel_events": 21921,
                        "runtime_events": 25462,
                    }
                ],
            },
            "top_kernels": [{"name": "paged_attention_kernel", "count": 128, "p95_ms": 0.358}],
            "event_type_stats": [{"type": "RUNTIME", "count": 28918}, {"type": "KERNEL", "count": 23809}],
        },
    }

    runtime = SkillExecutor.from_dir(ROOT / "skill_runtime" / "skills")
    assert "llm.teller_request_trace_diagnosis" in runtime.registry.ids()
    run = runtime.run("llm.teller_request_trace_diagnosis", payload, dry_run=True).to_dict()

    assert run["status"] == "completed"
    assert run["stages"]["detect"]["status"] == "matched"
    diagnosis = run["stages"]["diagnose"]
    assert diagnosis["status"] == "ok"
    assert diagnosis["output"]["fault_type"] == "LLM::teller_runtime_overhead"
    assert diagnosis["output"]["trace_diagnosis"]["slowest_request"]["request_id"] == "cmpl-test-0"
    assert diagnosis["confidence"] >= 0.8


def test_teller_request_trace_thresholds_do_not_use_observed_payload_values() -> None:
    payload = {
        "domain": "llm",
        "namespace": "llm-k8s",
        "deployment": "vllm-server",
        "fault_type": "teller_request_latency_high",
        "metric": "teller.request_span_ms",
        "teller_kernel_p95_ms": 1.114,
        "teller_request_span_ms": 1578.694,
        "thresholds": {
            "teller_request_span_ms": 1000.0,
            "teller_kernel_p95_ms": 50.0,
        },
        "fine_grained_trace": {
            "available": True,
            "summary": {
                "kernel_events": 2006,
                "runtime_events": 3606,
                "driver_events": 144,
                "kernel_p95_ms": 1.114,
                "request_count": 29,
            },
            "request_context": {
                "available": True,
                "request_count": 29,
                "coverage": "request_level",
                "requests": [
                    {
                        "request_id": "cmpl-agenticsre-cupti",
                        "span_ms": 1578.694,
                        "event_count": 48,
                        "kernel_events": 0,
                        "runtime_events": 0,
                    }
                ],
            },
            "top_kernels": [{"name": "paged_attention_kernel", "count": 8, "p95_ms": 1.114}],
            "event_type_stats": [{"type": "RUNTIME", "count": 3606}, {"type": "KERNEL", "count": 2006}],
        },
    }

    runtime = SkillExecutor.from_dir(ROOT / "skill_runtime" / "skills")
    run = runtime.run("llm.teller_request_trace_diagnosis", payload, dry_run=True).to_dict()

    diagnosis = run["stages"]["diagnose"]
    assert diagnosis["status"] == "ok"
    assert diagnosis["output"]["fault_type"] == "LLM::teller_runtime_overhead"
    assert "Kernel P95 不高" in diagnosis["output"]["root_cause"]
    assert diagnosis["output"]["confidence_explanation"]["signals"]["kernel_p95_ms"] == 1.114


def test_teller_trace_dir_override_is_restricted(monkeypatch, tmp_path) -> None:
    import web_app.app as dashboard

    trace_dir = tmp_path / "allowed-trace"
    trace_dir.mkdir()
    cfg = dashboard._get_config()
    old_clusters = list(cfg.fault_targets.k8s_clusters)
    cfg.fault_targets.k8s_clusters = [
        {"id": "ai-teller-safe", "name": "TELLER Safe", "kind": "ai", "teller_trace_dir": str(trace_dir)}
    ]
    monkeypatch.setattr(dashboard, "_prometheus_api_ok", lambda _: False)
    try:
        client = TestClient(dashboard.app)
        ok = client.get(f"/api/ai-clusters/ai-teller-safe/dashboard?teller_trace_dir={trace_dir}")
        bad = client.get("/api/ai-clusters/ai-teller-safe/dashboard?teller_trace_dir=/etc")

        assert ok.status_code == 200, ok.text
        assert bad.status_code == 400, bad.text
    finally:
        cfg.fault_targets.k8s_clusters = old_clusters


def test_teller_request_id_keeps_underscore_as_part_of_id() -> None:
    import web_app.app as dashboard

    ids = dashboard._extract_teller_request_ids("step_execute_model[request_ids:chatcmpl_abc_123]")

    assert ids == ["chatcmpl_abc_123"]


def test_teller_trace_summary_cache_invalidates_on_file_change(monkeypatch, tmp_path) -> None:
    import json
    import web_app.app as dashboard

    trace_dir = tmp_path / "cache-trace"
    trace_dir.mkdir()
    trace_file = trace_dir / "rank0.jsonl"
    trace_file.write_text(
        json.dumps({"t": "KERNEL", "n": "kernel_one", "gs": 1000, "d": 20000, "c": 1}) + "\n",
        encoding="utf-8",
    )
    dashboard._TELLER_TRACE_SUMMARY_CACHE.clear()
    original_reader = dashboard._read_teller_events_from_file
    read_count = {"value": 0}

    def counting_reader(path, max_events):
        read_count["value"] += 1
        return original_reader(path, max_events)

    monkeypatch.setattr(dashboard, "_read_teller_events_from_file", counting_reader)

    first = dashboard._summarize_teller_trace(str(trace_dir))
    second = dashboard._summarize_teller_trace(str(trace_dir))
    assert first["summary"]["kernel_events"] == 1
    assert second["summary"]["kernel_events"] == 1
    assert read_count["value"] == 1

    trace_file.write_text(
        json.dumps({"t": "KERNEL", "n": "kernel_two", "gs": 1000, "d": 30000, "c": 1}) + "\n"
        + json.dumps({"t": "KERNEL", "n": "kernel_three", "gs": 40000, "d": 10000, "c": 2}) + "\n",
        encoding="utf-8",
    )
    third = dashboard._summarize_teller_trace(str(trace_dir))
    assert third["summary"]["kernel_events"] == 2
    assert read_count["value"] == 2


def test_ai_teller_integration_plan_endpoint(monkeypatch) -> None:
    import json
    import web_app.app as dashboard

    cfg = dashboard._get_config()
    old_clusters = list(cfg.fault_targets.k8s_clusters)
    cfg.fault_targets.k8s_clusters = [
        {
            "id": "ai-teller-plan",
            "name": "TELLER Plan",
            "kind": "ai",
            "default_namespace": "llm-k8s",
            "vllm_deployment": "vllm-server",
            "vllm_container": "vllm",
            "model": "Qwen/Qwen2.5-7B-Instruct",
            "teller_trace_dir": "/data/teller/trace",
            "teller_code_path": "/opt/teller/TELLER_Code",
            "teller_code_host_path": "/host/teller/TELLER_Code",
            "teller_runtime_trace_root": "/opt/teller/data",
            "teller_trace_host_path": "/host/teller/data",
            "teller_cupti_lib_paths": "/opt/cuda-cupti/lib:/opt/nvtx/lib",
        }
    ]
    def fake_shell(cmd, timeout=90, cluster_id=None, env=None):
        if "get deploy" in cmd:
            return {"ok": True, "stdout": "{}", "stderr": "", "command": cmd}
        if "get pods" in cmd:
            return {
                "ok": True,
                "stdout": json.dumps({
                    "items": [{
                        "metadata": {"name": "vllm-server-abc"},
                        "status": {"phase": "Running"},
                        "spec": {"nodeName": "dds-128"},
                    }]
                }),
                "stderr": "",
                "command": cmd,
            }
        if "test -d" in cmd:
            return {"ok": True, "stdout": "TELLER_CODE_OK", "stderr": "", "command": cmd}
        if "teller.cli run --help" in cmd:
            return {"ok": True, "stdout": "TELLER_LAUNCHER_OK", "stderr": "", "command": cmd}
        if "command -v uv" in cmd:
            return {"ok": True, "stdout": "", "stderr": "", "command": cmd}
        if "libcupti.so" in cmd:
            return {"ok": True, "stdout": "/opt/cuda-cupti/lib/libcupti.so.12", "stderr": "", "command": cmd}
        if "libnvToolsExt.so" in cmd:
            return {"ok": True, "stdout": "/opt/nvtx/lib/libnvToolsExt.so.1", "stderr": "", "command": cmd}
        if "libcupti_trace_injection_with_nvtx.so" in cmd:
            return {"ok": True, "stdout": "", "stderr": "", "command": cmd}
        if "TRACE_WRITABLE" in cmd:
            return {"ok": True, "stdout": "TRACE_WRITABLE", "stderr": "", "command": cmd}
        return {"ok": True, "stdout": "", "stderr": "", "command": cmd}

    monkeypatch.setattr(dashboard, "_shell_sync", fake_shell)
    monkeypatch.setattr(dashboard, "_summarize_teller_trace", lambda trace_dir: {"request_context": {"available": False, "message": "no request id"}})
    try:
        client = TestClient(dashboard.app)
        response = client.get("/api/ai-clusters/ai-teller-plan/teller/integration-plan")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["mode"] == "real_teller_vllm_wrapper"
        assert body["deployment"] == "vllm-server"
        assert body["launcher"] == "python_module"
        assert body["teller_code_host_path"] == "/host/teller/TELLER_Code"
        assert body["runtime_trace_root"] == "/opt/teller/data"
        assert body["trace_host_path"] == "/host/teller/data"
        assert body["agenticsre_teller_hooks_source"] == "configmap"
        assert body["agenticsre_teller_hooks_configmap"] == "vllm-server-agenticsre-teller-hooks"
        assert body["teller_cupti_lib_paths"][0] == "/opt/cuda-cupti/lib"
        assert body["teller_cupti_lib_paths"][1] == "/opt/nvtx/lib"
        assert "python3 -m teller.cli run" in body["wrapped_command"]
        assert "PYTHONPATH=/opt/teller/TELLER_Code/src" in body["wrapped_command"]
        assert "LD_LIBRARY_PATH=/opt/cuda-cupti/lib:/opt/nvtx/lib" in body["wrapped_command"]
        assert "TELLER_NVTX_LIB" in body["wrapped_command"]
        assert "/tmp/teller-nvtx/libnvToolsExt.so" in body["wrapped_command"]
        assert "--so /opt/teller/TELLER_Code/src/teller/lib/libcupti_trace_injection_with_nvtx.so" in body["wrapped_command"]
        assert any("uv run teller run" in cmd for cmd in body["alternate_commands"])
        patch_cmd = "\n".join(body["commands"])
        assert "create configmap vllm-server-agenticsre-teller-hooks" in patch_cmd
        assert "--from-file=vllm_http_pytorch_hooks.py=tools/teller/vllm_http_pytorch_hooks.py" in patch_cmd
        assert "/host/teller/TELLER_Code" in patch_cmd
        assert "/host/teller/data" in patch_cmd
        assert "\"configMap\": {\"name\": \"vllm-server-agenticsre-teller-hooks\"}" in patch_cmd
        assert "step_execute_model" in "\n".join(body["verification"])
        assert body["commands"]
        assert body["limitations"]
        assert body["preflight"]["status"] == "partial"
        failed = {item["id"] for item in body["preflight"]["checks"] if not item["ok"]}
        assert {"uv", "teller_injection"}.issubset(failed)
        assert "cupti_runtime" not in failed
        assert "nvtx_runtime" not in failed
        checks = {item["id"]: item for item in body["preflight"]["checks"]}
        assert checks["launcher"]["ok"] is True
        assert checks["uv"]["required"] is False
    finally:
        cfg.fault_targets.k8s_clusters = old_clusters


def test_ai_teller_preflight_endpoint_reports_missing_runtime(monkeypatch) -> None:
    import json
    import web_app.app as dashboard

    cfg = dashboard._get_config()
    old_clusters = list(cfg.fault_targets.k8s_clusters)
    cfg.fault_targets.k8s_clusters = [
        {
            "id": "ai-preflight",
            "name": "TELLER Preflight",
            "kind": "ai",
            "default_namespace": "llm-k8s",
            "vllm_deployment": "vllm-server",
            "teller_trace_dir": "/data/teller/trace",
        }
    ]

    def fake_shell(cmd, timeout=90, cluster_id=None, env=None):
        if "get deploy" in cmd:
            return {"ok": True, "stdout": "{}", "stderr": "", "command": cmd}
        if "get pods" in cmd:
            return {
                "ok": True,
                "stdout": json.dumps({
                    "items": [{
                        "metadata": {"name": "vllm-server-abc"},
                        "status": {"phase": "Running"},
                        "spec": {"nodeName": "dds-128"},
                    }]
                }),
                "stderr": "",
                "command": cmd,
            }
        return {"ok": True, "stdout": "", "stderr": "", "command": cmd}

    monkeypatch.setattr(dashboard, "_shell_sync", fake_shell)
    monkeypatch.setattr(dashboard, "_summarize_teller_trace", lambda trace_dir: {"request_context": {"available": False, "message": "no request id"}})
    try:
        client = TestClient(dashboard.app)
        response = client.get("/api/ai-clusters/ai-preflight/teller/preflight")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "partial"
        assert body["pod"] == "vllm-server-abc"
        checks = {item["id"]: item for item in body["checks"]}
        assert checks["deployment"]["ok"] is True
        assert checks["launcher"]["ok"] is False
        assert checks["uv"]["ok"] is False
        assert checks["uv"]["required"] is False
        assert checks["request_context"]["required"] is False
    finally:
        cfg.fault_targets.k8s_clusters = old_clusters


def test_ai_cluster_dashboard_does_not_substitute_another_prometheus(monkeypatch) -> None:
    import web_app.app as dashboard

    cfg = dashboard._get_config()
    old_clusters = list(cfg.fault_targets.k8s_clusters)
    old_hosts = list(cfg.fault_targets.llm_hosts)
    cfg.fault_targets.k8s_clusters = [
        {
            "id": "ai-prom-fallback",
            "name": "AI Prom Fallback",
            "kind": "ai",
            "prometheus_url": "http://127.0.0.1:30090",
            "default_namespace": "llm-k8s",
        }
    ]
    cfg.fault_targets.llm_hosts = []

    monkeypatch.setattr(dashboard, "_prometheus_api_ok", lambda url: url == "http://127.0.0.1:19090")
    async def fake_query_metric_charts(*args, **kwargs):
        raise AssertionError("unrelated Prometheus must not supply this cluster's charts")

    async def fake_collect_ai_k8s_inventory(cluster):
        return {"available": False, "nodes": [], "pods": [], "services": []}

    monkeypatch.setattr(dashboard, "_query_metric_charts", fake_query_metric_charts)
    monkeypatch.setattr(dashboard, "_summarize_prometheus_inventory", lambda base_url: {"targets": {"available": True, "targets": []}, "gpu_devices": [], "vllm_series": []})
    monkeypatch.setattr(dashboard, "_collect_ai_k8s_inventory", fake_collect_ai_k8s_inventory)
    monkeypatch.setattr(dashboard, "_summarize_teller_trace", lambda *a, **kw: {"available": False, "summary": {}})
    monkeypatch.setattr(dashboard, "_sync_teller_trace_from_k8s", lambda *a, **kw: {"ok": False})

    try:
        client = TestClient(dashboard.app)
        response = client.get("/api/ai-clusters/ai-prom-fallback/dashboard?duration=5m")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["mode"] == "profile_only"
        assert body["prometheus_url"] == "http://127.0.0.1:30090"
        assert body["storage"]["available"] is False
        assert body["summary"]["node_cpu_usage"] is None
        assert body["storage"]["warnings"]
    finally:
        cfg.fault_targets.k8s_clusters = old_clusters
        cfg.fault_targets.llm_hosts = old_hosts


def test_ai_cluster_dashboard_normalizes_gpu_nodes_and_models(monkeypatch) -> None:
    import web_app.app as dashboard

    cfg = dashboard._get_config()
    old_clusters = list(cfg.fault_targets.k8s_clusters)
    old_hosts = list(cfg.fault_targets.llm_hosts)
    cfg.fault_targets.k8s_clusters = [
        {
            "id": "ai-prom",
            "name": "AI Prometheus",
            "kind": "ai",
            "prometheus_url": "http://127.0.0.1:19090",
            "default_namespace": "llm-k8s",
        }
    ]
    cfg.fault_targets.llm_hosts = []

    async def fake_query_metric_charts(*args, **kwargs):
        return {}

    async def fake_collect_ai_k8s_inventory(cluster):
        return {"available": True, "nodes": [], "pods": [], "services": []}

    def fake_prom_query_url_sync(base_url, query, **kwargs):
        if query == "DCGM_FI_DEV_GPU_UTIL":
            return {
                "results": [
                    {"metric": {"instance": "dds-127", "Hostname": "dds-127", "gpu": "0"}, "value": [1, "10"]},
                    {"metric": {"instance": "dds-127", "Hostname": "dds-127", "gpu": "1"}, "value": [1, "20"]},
                    {"metric": {"instance": "dds-128", "node": "dds-128", "gpu": "0"}, "value": [1, "30"]},
                ]
            }
        if query == "DCGM_FI_DEV_FB_USED":
            return {"results": []}
        if query == "DCGM_FI_DEV_FB_TOTAL":
            return {"results": []}
        if query == "DCGM_FI_DEV_GPU_TEMP":
            return {"results": []}
        if "vllm:num_requests_running" in query:
            return {
                "results": [
                    {
                        "metric": {
                            "__name__": "vllm:num_requests_running",
                            "instance": "vllm-large",
                            "model": "qwen2.5-14b-instruct",
                        },
                        "value": [1, "0"],
                    }
                ]
            }
        return {"results": []}

    monkeypatch.setattr(dashboard, "_prometheus_api_ok", lambda url: True)
    monkeypatch.setattr(dashboard, "_query_metric_charts", fake_query_metric_charts)
    monkeypatch.setattr(dashboard, "_prom_query_url_sync", fake_prom_query_url_sync)
    monkeypatch.setattr(dashboard, "_prometheus_targets_sync", lambda base_url: {"available": True, "targets": []})
    monkeypatch.setattr(dashboard, "_collect_ai_k8s_inventory", fake_collect_ai_k8s_inventory)
    monkeypatch.setattr(dashboard, "_summarize_teller_trace", lambda trace_dir: {"available": False, "summary": {}})

    try:
        client = TestClient(dashboard.app)
        response = client.get("/api/ai-clusters/ai-prom/dashboard?duration=5m")
        assert response.status_code == 200, response.text
        body = response.json()
        gpu_devices = body["topology"]["gpu_devices"]
        assert sorted({item["node"] for item in gpu_devices}) == ["dds-127", "dds-128"]
        assert len(gpu_devices) == 3
        assert body["topology"]["models"][0]["name"] == "qwen2.5-14b-instruct"
    finally:
        cfg.fault_targets.k8s_clusters = old_clusters
        cfg.fault_targets.llm_hosts = old_hosts


def test_ai_teller_collect_endpoint_requires_confirmation(monkeypatch) -> None:
    import web_app.app as dashboard

    cfg = dashboard._get_config()
    old_clusters = list(cfg.fault_targets.k8s_clusters)
    old_hosts = list(cfg.fault_targets.llm_hosts)
    cfg.fault_targets.k8s_clusters = [
        {"id": "ai-teller", "name": "TELLER 智算", "kind": "ai", "default_namespace": "llm-k8s"}
    ]
    cfg.fault_targets.llm_hosts = []
    try:
        client = TestClient(dashboard.app)
        response = client.post("/api/ai-clusters/ai-teller/teller/collect", json={})
        assert response.status_code == 400
        assert "confirm=true" in response.text
    finally:
        cfg.fault_targets.k8s_clusters = old_clusters
        cfg.fault_targets.llm_hosts = old_hosts


def test_ai_teller_collect_endpoint_runs_probe_job(monkeypatch, tmp_path) -> None:
    import web_app.app as dashboard

    cfg = dashboard._get_config()
    old_clusters = list(cfg.fault_targets.k8s_clusters)
    old_hosts = list(cfg.fault_targets.llm_hosts)
    cfg.fault_targets.k8s_clusters = [
        {
            "id": "ai-teller",
            "name": "TELLER 智算",
            "kind": "ai",
            "default_namespace": "llm-k8s",
            "teller_trace_dir": str(tmp_path / "trace"),
        }
    ]
    cfg.fault_targets.llm_hosts = []

    def fake_collect(cluster, **kwargs):
        return {
            "status": "completed",
            "run_id": "teller-test",
            "job": "teller-probe-test",
            "namespace": kwargs["namespace"],
            "node": kwargs["node_name"],
            "trace_dir": kwargs["trace_dir"],
            "fine_grained_trace": {
                "available": True,
                "summary": {"total_events": 2, "kernel_events": 1, "nvtx_events": 1},
                "top_kernels": [{"name": "matmul", "count": 1}],
            },
        }

    monkeypatch.setattr(dashboard, "_find_vllm_node_for_cluster", lambda *_args: "dds-128")
    monkeypatch.setattr(dashboard, "_run_teller_probe_collection", fake_collect)
    try:
        client = TestClient(dashboard.app)
        response = client.post("/api/ai-clusters/ai-teller/teller/collect", json={"confirm": True})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "completed"
        assert body["node"] == "dds-128"
        assert body["fine_grained_trace"]["summary"]["kernel_events"] == 1
    finally:
        cfg.fault_targets.k8s_clusters = old_clusters
        cfg.fault_targets.llm_hosts = old_hosts


def test_legacy_dashboard_records_skill_workflow_runs(monkeypatch) -> None:
    import web_app.app as dashboard

    def fake_runtime_workflow(body: dict) -> dict:
        return {
            "status": "completed",
            "workflow_run_id": "skillwf-runtime-test",
            "domain": "k8s",
            "dry_run": True,
            "diagnosis": {
                "status": "ok",
                "source": "skill_runtime",
                "skill_id": "k8s.service_unavailable",
                "fault_type": "ServiceUnavailable",
                "root_cause": "frontend has no endpoints",
                "confidence": 0.91,
                "evidence": ["service has no endpoint"],
            },
            "capability": {"heal_ready": True},
            "execution_plan": {"command_count": 1, "commands": ["kubectl get endpoints frontend"], "suggestions": []},
            "verification_plan": {"status": "ready", "checks": ["kubectl get endpoints frontend"]},
            "migration": {"mode": "skill_primary", "primary_skill_id": "k8s.service_unavailable"},
            "skill_runtime": {"matched_count": 1, "primary_skill_id": "k8s.service_unavailable", "runs": []},
            "trace": [],
        }

    monkeypatch.setattr(dashboard, "_post_skill_runtime_workflow", fake_runtime_workflow)
    client = TestClient(dashboard.app)
    response = client.post(
        "/api/skill-workflows/operate",
        json={
            "payload": {
                "namespace": "default",
                "fault_type": "ServiceUnavailable",
                "message": "service frontend returns 503 because no endpoints",
            },
            "domain": "k8s",
            "dry_run": True,
        },
    )

    assert response.status_code == 200, response.text
    dashboard_run_id = response.json()["dashboard_run_id"]
    history = client.get("/api/skill-workflows/runs?limit=10")
    assert history.status_code == 200, history.text
    assert any(item["id"] == dashboard_run_id for item in history.json()["runs"])
    detail = client.get(f"/api/skill-workflows/runs/{dashboard_run_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["result"]["workflow_run_id"] == "skillwf-runtime-test"


def test_dashboard_imports_when_started_from_web_app_directory() -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib.util; spec=importlib.util.spec_from_file_location('container_app', 'app.py'); mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); print(any(getattr(r, 'path', '') == '/api/skill-workflows/operate' for r in mod.app.routes))",
        ],
        cwd=ROOT / "web_app",
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert "True" in result.stdout


def test_legacy_adapter_fallback_is_usable_without_web_app() -> None:
    from skill_runtime.adapters.legacy import _fallback_build_heal_suggestions

    result = _fallback_build_heal_suggestions(
        {
            "namespace": "default",
            "fault_type": "ServiceUnavailable",
            "message": "service frontend returns 503 because no endpoints",
            "service": "frontend",
            "deployment": "frontend",
        },
        adapter_error="unit-test import failure",
    )

    commands = [item["command"] for item in result["suggestions"]]
    assert result["fault_type"] == "ServiceUnavailable"
    assert "kubectl get endpoints frontend -n default -o wide" in commands
    assert "kubectl scale deployment/frontend -n default --replicas=1" in commands
    assert len(commands) == len(set(commands))


def test_legacy_adapter_fallback_materializes_image_recovery() -> None:
    from skill_runtime.adapters.legacy import _fallback_build_heal_suggestions

    result = _fallback_build_heal_suggestions(
        {
            "namespace": "default",
            "fault_type": "ImagePullError",
            "message": "ImagePullBackOff ErrImagePull",
            "pod": "api-abc",
            "deployment": "api",
            "container": "api",
            "current_image": "repo/api:bad",
            "last_good_image": "repo/api:stable",
        },
        adapter_error="unit-test import failure",
    )

    commands = [item["command"] for item in result["suggestions"]]
    assert "kubectl set image deployment/api api=repo/api:stable -n default" in commands


def test_hermes_community_skills_can_be_discovered_and_converted(monkeypatch, tmp_path) -> None:
    import importlib
    from skill_runtime.community import HermesCommunitySource

    # Exercise the real discovery/conversion adapter without requiring the
    # optional upstream Hermes checkout to be distributed with this project.
    community_root = tmp_path / "hermes-community-fixtures"
    fixture_dir = community_root / "fixtures" / "docker-fixture"
    fixture_dir.mkdir(parents=True)
    (fixture_dir / "SKILL.md").write_text(
        "# Docker discovery fixture\n\n"
        "Controlled test fixture for Docker skill discovery and review-only conversion.\n"
        "This fixture is not an installed upstream Hermes skill.\n",
        encoding="utf-8",
    )
    runtime = importlib.import_module("skill_runtime.app")
    monkeypatch.setattr(runtime, "hermes_community", HermesCommunitySource(community_root))
    client = TestClient(app)

    listed = client.get("/api/community/hermes/skills?query=docker&limit=10")
    assert listed.status_code == 200, listed.text
    skills = listed.json()["skills"]
    assert any("docker" in item["name"] or "docker" in item["description"].lower() for item in skills)
    target = next(item for item in skills if "docker" in item["relative_path"])
    assert target["identifier"] == "hermes:fixtures/docker-fixture"

    converted = client.post(
        "/api/community/hermes/convert",
        json={"relative_path": target["relative_path"], "target_domain": "k8s"},
    )
    assert converted.status_code == 200, converted.text
    draft = converted.json()
    assert draft["manifest"]["policy"]["requires_approval"] is True
    assert draft["manifest"]["policy"]["safe_auto_execute"] is False
    assert draft["manifest"]["permissions"] == ["review.only"]
    assert "community" in draft["manifest"]["tags"]
    assert draft["verification"]["status"] == "review_required"


def test_local_community_catalog_includes_skill_creator_and_alibaba_aiops() -> None:
    client = TestClient(app)

    listed = client.get("/api/community/sources")
    assert listed.status_code == 200, listed.text
    sources = listed.json()["sources"]
    names = {item["source"] for item in sources}
    assert "anthropic-skills" in names
    assert "alibabacloud-aiops-skills" in names
    assert "vmware-aiops-skill" in names
    assert "microsoft-sap-aiops-agent" in names
    assert "evoskills" in names

    by_name = {item["source"]: item for item in sources}
    assert by_name["vmware-aiops-skill"]["integration_mode"] == "reference_then_manual_mirror"
    assert by_name["microsoft-sap-aiops-agent"]["integration_mode"] == "reference_then_pattern_extract"
    assert by_name["evoskills"]["integration_mode"] == "method_reference"

    converted = client.post(
        "/api/community/local/convert",
        json={"catalog_path": "anthropic/skill-creator.yaml", "target_domain": "generic"},
    )
    assert converted.status_code == 200, converted.text
    draft = converted.json()
    assert draft["manifest"]["policy"]["safe_auto_execute"] is False
    assert draft["manifest"]["permissions"] == ["review.only"]
    assert "skill-creator" in draft["manifest"]["name"].lower()
    assert draft["verification"]["review_ready"] is True


def test_markdown_converter_allows_alibaba_aiops_urls_without_fetching() -> None:
    from skill_runtime.community import _allowed_skill_url

    raw = _allowed_skill_url(
        "https://github.com/aliyun/alibabacloud-aiops-skills/blob/main/skills/example/SKILL.md"
    )

    assert raw == "https://raw.githubusercontent.com/aliyun/alibabacloud-aiops-skills/main/skills/example/SKILL.md"


def test_executor_can_load_from_custom_dir(tmp_path) -> None:
    skill_dir = tmp_path / "skills" / "minimal"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(
        """
id: test.minimal
version: 0.1.0
name: Minimal Skill
domain: test
stages: [detect]
confidence:
  min_evidence_sources: 1
""".strip(),
        encoding="utf-8",
    )
    (skill_dir / "skill.py").write_text(
        "def detect(context):\n    return {'status': 'matched', 'confidence': 0.9, 'evidence': ['unit']}\n",
        encoding="utf-8",
    )

    custom = SkillExecutor.from_dir(tmp_path / "skills")
    run = custom.run("test.minimal", {}, stages=["detect"]).to_dict()

    assert run["stages"]["detect"]["status"] == "matched"


def test_core_skill_markdown_is_operational_not_only_contract_summary() -> None:
    required = [
        ROOT / "skill_runtime" / "skills" / "generic_evidence_collection" / "SKILL.md",
        ROOT / "skill_runtime" / "skills" / "k8s_service_unavailable" / "SKILL.md",
        ROOT / "skill_runtime" / "skills" / "tool_k8s_readiness" / "SKILL.md",
    ]
    for path in required:
        text = path.read_text(encoding="utf-8")
        for marker in ["## 适用场景", "## 输入信号", "## 执行阶段", "## 输出契约", "## 风险与门禁", "## 验证方式"]:
            assert marker in text, f"{path} missing {marker}"
        assert "Agent" in text
        assert "只读" in text or "dry-run" in text.lower()


def test_skill_markdown_uses_standard_frontmatter_and_yaml_manifest() -> None:
    import yaml

    forbidden = {
        "id", "version", "domain", "stages", "risk", "permissions",
        "tool_dependencies", "tags", "policy", "confidence", "entrypoint",
    }
    for path in sorted((ROOT / "skill_runtime" / "skills").glob("*/SKILL.md")):
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\n"), f"{path} missing YAML frontmatter"
        raw = text.split("---", 2)[1]
        frontmatter = yaml.safe_load(raw) or {}
        assert set(frontmatter) == {"name", "description"}, f"{path} has non-standard SKILL.md frontmatter"
        assert not forbidden.intersection(frontmatter), f"{path} leaks runtime manifest into SKILL.md"
        manifest = path.with_name("skill.yaml")
        assert manifest.exists(), f"{path.parent} missing skill.yaml runtime manifest"
        data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        for key in ("id", "version", "domain", "stages"):
            assert data.get(key), f"{manifest} missing {key}"


def test_every_skill_directory_has_markdown_manifest_and_adapter() -> None:
    for skill_dir in sorted((ROOT / "skill_runtime" / "skills").iterdir()):
        if not skill_dir.is_dir():
            continue
        assert (skill_dir / "SKILL.md").exists(), f"{skill_dir} missing SKILL.md"
        assert (skill_dir / "skill.yaml").exists(), f"{skill_dir} missing skill.yaml"
        assert (skill_dir / "skill.py").exists(), f"{skill_dir} missing skill.py"


def test_skill_manifest_keywords_support_registry_search() -> None:
    registry = SkillRegistry(ROOT / "skill_runtime" / "skills").load()
    matches = registry.search(
        "TargetDown HTTP 503 no endpoints service unavailable",
        domain="k8s",
        stages=["diagnose"],
        limit=3,
    )
    assert matches
    assert matches[0].metadata.id == "k8s.service_unavailable"
    assert "no endpoints" in {item.lower() for item in matches[0].metadata.matching_keywords}


def test_agent_skill_bridge_invokes_skill_runtime_workflow() -> None:
    payload = build_skill_payload(
        "Alert TargetDown service frontend returns 503 because no endpoints",
        namespace="default",
    )
    assert payload["source"] == "agent_rca"
    assert payload["invocation_mode"] == "agent_tool_bridge"
    assert payload["domain"] == "k8s"
    assert payload["fault_type"] == "service_unavailable"

    bridge = AgentSkillBridge(skills_dir=ROOT / "skill_runtime" / "skills", runtime_url="")
    result = bridge.run(
        "Alert TargetDown service frontend returns 503 because no endpoints",
        namespace="default",
        stages=["detect", "diagnose", "heal", "verify"],
    )
    summary = summarize_skill_result(result)
    assert result["skill_runtime"]["primary_skill_id"] == "k8s.service_unavailable"
    assert result["skill_runtime"]["runs"]
    primary_run = next(
        run for run in result["skill_runtime"]["runs"]
        if run["skill"]["id"] == result["skill_runtime"]["primary_skill_id"]
    )
    assert {"detect", "diagnose", "heal", "verify"}.issubset(
        set(primary_run["stages"])
    )
    assert summary["primary_skill_id"] == "k8s.service_unavailable"
    assert "Agent 调用 Skill Runtime" in summary["summary"]


def test_agent_skill_bridge_preserves_ai_event_structured_payload() -> None:
    payload = build_skill_payload(
        "[智算事件][vllm_process_crash] 智算故障注入进行中: sw-001; deployment=vllm-server",
        namespace="llm-k8s",
        evidence={
            "payload": {
                "domain": "llm",
                "fault_type": "vllm_process_crash",
                "scenario_id": "sw-001",
                "deployment": "vllm-server",
                "service": "vllm-server",
                "target_mode": "k8s",
            }
        },
    )

    assert payload["domain"] == "llm"
    assert payload["fault_type"] == "vllm_process_crash"
    assert payload["scenario_id"] == "sw-001"
    assert payload["deployment"] == "vllm-server"

    bridge = AgentSkillBridge(skills_dir=ROOT / "skill_runtime" / "skills", runtime_url="")
    result = bridge.run(
        "[智算事件][vllm_process_crash] 智算故障注入进行中: sw-001; deployment=vllm-server",
        namespace="llm-k8s",
        evidence={"payload": payload},
        stages=["detect", "diagnose", "heal"],
    )
    assert result["domain"] == "llm"
    assert result["skill_runtime"]["primary_skill_id"] == "llm.vllm_process_crash"


def test_agent_skill_bridge_extracts_payload_from_agent_evidence() -> None:
    evidence = {
        "agent_skill_context": {
            "agent": "agent_skill_context",
            "summary": "前端告警结构化上下文。",
            "payload": {
                "payload": {
                    "domain": "k8s",
                    "fault_type": "service_unavailable",
                    "namespace": "kube-system",
                    "service": "kube-prom-kube-prometheus-kube-proxy",
                    "deployment": "kube-proxy",
                    "message": "TargetDown and no endpoints",
                }
            },
        },
        "metric_agent": {
            "agent": "metric_agent",
            "summary": "Prometheus target down for kube-proxy.",
        },
        "agent_evidence_collection": {
            "agent": "agent_evidence_collection",
            "commands": [
                {
                    "tool": "kubectl",
                    "command": "get endpoints kube-prom-kube-prometheus-kube-proxy -o wide",
                    "success": True,
                    "summary": "kubectl get endpoints kube-prom-kube-prometheus-kube-proxy -o wide 执行成功",
                    "sample": "NAME ENDPOINTS AGE\nkube-prom-kube-prometheus-kube-proxy <none> 10d",
                }
            ],
        },
    }
    payload = build_skill_payload(
        "Alert TargetDown",
        namespace="kube-system",
        evidence=evidence,
    )

    assert payload["domain"] == "k8s"
    assert payload["fault_type"] == "service_unavailable"
    assert payload["service"] == "kube-prom-kube-prometheus-kube-proxy"
    assert payload["deployment"] == "kube-proxy"
    assert payload["agent_evidence"]["metric_agent"]["summary"].startswith("Prometheus")

    bridge = AgentSkillBridge(skills_dir=ROOT / "skill_runtime" / "skills", runtime_url="")
    result = bridge.run(
        "Alert TargetDown",
        namespace="kube-system",
        evidence=evidence,
        stages=["detect", "diagnose"],
    )
    assert result["skill_runtime"]["primary_skill_id"] == "k8s.service_unavailable"
    assert result["diagnosis"]["root_cause_component"] == "kube-proxy"
    assert any("kubectl" in item for item in result["diagnosis"]["evidence"])


def test_agent_skill_bridge_does_not_use_experiment_labels_as_endpoint_evidence() -> None:
    payload = build_skill_payload(
        "[智算事件][endpoint_timeout] 智算故障注入进行中: net-001; deployment=vllm-server",
        namespace="llm-k8s",
        evidence={
            "payload": {
                "source": "ai_event_center",
                "domain": "llm",
                "fault_type": "endpoint_timeout",
                "raw_fault_type": "fault_experiment_signal",
                "scenario": "net-001",
                "namespace": "llm-k8s",
                "deployment": "vllm-server",
                "service": "vllm-server",
                "model": "qwen2.5-coder-3b-k8s",
                "target_mode": "k8s",
                "metrics_status": "ok",
                "metrics_count": 14,
                "recovery_success": True,
                "evidence": [
                    {"source": "fault_run", "signal": "fault_type", "value": "endpoint_timeout"},
                    {"source": "fault_run", "signal": "layer", "value": "network"},
                    {"source": "fault_run", "signal": "injection_success", "value": True},
                ],
            }
        },
    )

    bridge = AgentSkillBridge(skills_dir=ROOT / "skill_runtime" / "skills", runtime_url="")
    result = bridge.run(
        "[智算事件][endpoint_timeout] 智算故障注入进行中: net-001; deployment=vllm-server",
        namespace="llm-k8s",
        evidence={"payload": payload},
        stages=["detect", "diagnose", "heal"],
    )

    assert result["domain"] == "llm"
    assert result["skill_runtime"]["primary_skill_id"] == "llm.k8s_vllm_observability"
    assert result["diagnosis"]["root_cause_component"] == "vllm-server"
    assert result["diagnosis"]["fault_type"] == "LLM::k8s_vllm_experiment_effect_unconfirmed"
    assert result["diagnosis"]["confidence"] <= 0.62


def test_agent_skill_bridge_diagnoses_ai_os_resource_pressure() -> None:
    payload = build_skill_payload(
        "[智算事件] 故障窗口内节点 CPU 从 18% 升至 93%",
        namespace="llm-k8s",
        evidence={
            "payload": {
                "source": "ai_event_center",
                "domain": "llm",
                "fault_type": "fault_experiment_signal",
                "raw_fault_type": "fault_experiment_signal",
                "namespace": "llm-k8s",
                "deployment": "vllm-server",
                "service": "vllm-server",
                "model": "qwen2.5-coder-3b-k8s",
                "target_mode": "k8s",
                "metrics_status": "ok",
                "metrics_count": 14,
                "node_cpu_usage": 93.0,
                "metric_baseline": {"node_cpu_usage": 18.0},
                "metric_fault": {"node_cpu_usage": 93.0},
                "metric_deltas": {"node_cpu_usage": 75.0},
                "observation_signals": [{
                    "signal": "node_cpu_usage",
                    "value": 93.0,
                    "threshold": 85.0,
                    "candidate_skill": "k8s.resource_pressure",
                    "candidate_fault": "HighCPUUsage",
                }],
                "candidate_skills": ["k8s.resource_pressure"],
                "candidate_faults": ["HighCPUUsage"],
                "evidence": [
                    {"source": "prometheus", "signal": "node_cpu_usage", "value": 93.0, "threshold": 85.0},
                ],
            }
        },
    )

    bridge = AgentSkillBridge(skills_dir=ROOT / "skill_runtime" / "skills", runtime_url="")
    result = bridge.run(
        "[智算事件] 故障窗口内节点 CPU 从 18% 升至 93%",
        namespace="llm-k8s",
        evidence={"payload": payload},
        stages=["detect", "diagnose", "heal"],
    )

    assert result["domain"] == "llm"
    assert result["skill_runtime"]["primary_skill_id"] == "k8s.resource_pressure"
    assert result["diagnosis"]["fault_type"] in {"K8S::resource_pressure", "HighCPUUsage"}
    assert "CPU" in result["diagnosis"]["root_cause"] or "资源压力" in result["diagnosis"]["root_cause"]


def test_agent_skill_bridge_diagnoses_ai_node_resource_pressure_metric() -> None:
    payload = build_skill_payload(
        "[智算事件][resource_pressure] dds-127 node_rootfs_usage 86 > 85; deployment=vllm-server",
        namespace="llm-k8s",
        evidence={
            "payload": {
                "source": "ai_event_center",
                "domain": "llm",
                "fault_type": "resource_pressure",
                "raw_fault_type": "node_resource_pressure",
                "namespace": "llm-k8s",
                "deployment": "vllm-server",
                "service": "vllm-server",
                "model": "qwen2.5-coder-3b-k8s",
                "target_mode": "k8s",
                "metrics": {"metric": "node_rootfs_usage", "value": 86, "threshold": 85},
                "evidence": [
                    {"source": "prometheus", "signal": "node", "value": "dds-127"},
                    {"source": "prometheus", "signal": "metric", "value": "node_rootfs_usage"},
                ],
            }
        },
    )

    bridge = AgentSkillBridge(skills_dir=ROOT / "skill_runtime" / "skills", runtime_url="")
    result = bridge.run(
        "[智算事件][resource_pressure] dds-127 node_rootfs_usage 86 > 85; deployment=vllm-server",
        namespace="llm-k8s",
        evidence={"payload": payload},
        stages=["detect", "diagnose", "heal"],
    )

    assert result["skill_runtime"]["primary_skill_id"] == "llm.k8s_vllm_observability"
    assert result["diagnosis"]["fault_type"] == "LLM::k8s_vllm_node_disk_pressure"
    assert "磁盘" in result["diagnosis"]["root_cause"] or "根分区" in result["diagnosis"]["root_cause"]


def test_agent_skill_bridge_diagnoses_ai_event_from_observable_evidence_only() -> None:
    payload = build_skill_payload(
        "[智算事件][fault_experiment_signal] 智算运行异常：推理服务观测信号异常; deployment=vllm-server",
        namespace="llm-k8s",
        evidence={
            "payload": {
                "source": "ai_event_center",
                "domain": "llm",
                "fault_type": "fault_experiment_signal",
                "raw_fault_type": "fault_experiment_signal",
                "namespace": "llm-k8s",
                "deployment": "vllm-server",
                "service": "vllm-server",
                "target_mode": "k8s",
                "metrics_status": "ok",
                "metrics_count": 14,
                "metric": "node_cpu_pressure",
                "message": "vLLM 所在节点 CPU pressure 持续升高，推理请求出现排队",
                "waiting_requests": 3,
                "evidence": [
                    {"source": "prometheus", "signal": "metric", "value": "node_cpu_pressure"},
                    {"source": "prometheus", "signal": "waiting_requests", "value": 3},
                ],
            }
        },
    )

    bridge = AgentSkillBridge(skills_dir=ROOT / "skill_runtime" / "skills", runtime_url="")
    result = bridge.run(
        "[智算事件][fault_experiment_signal] 智算运行异常：推理服务观测信号异常; deployment=vllm-server",
        namespace="llm-k8s",
        evidence={"payload": payload},
        stages=["detect", "diagnose", "heal"],
    )

    assert result["domain"] == "llm"
    assert result["skill_runtime"]["primary_skill_id"] == "llm.k8s_vllm_observability"
    assert result["diagnosis"]["fault_type"] == "LLM::k8s_vllm_node_cpu_pressure"
    assert "CPU" in result["diagnosis"]["root_cause"]


def test_agent_skill_bridge_diagnoses_ai_node_rootfs_pressure_as_disk() -> None:
    payload = build_skill_payload(
        "[智算事件][node_resource_pressure] 根分区使用率=95.9% 超过阈值 85%",
        namespace="llm-k8s",
        evidence={
            "payload": {
                "source": "ai_event_center",
                "domain": "llm",
                "fault_type": "resource_pressure",
                "raw_fault_type": "node_resource_pressure",
                "namespace": "llm-k8s",
                "deployment": "vllm-server",
                "service": "vllm-server",
                "metric": "node_rootfs_usage",
                "message": "根分区使用率=95.9% 超过阈值 85%，可能影响 vLLM 推理服务",
                "metrics": {"metric": "node_rootfs_usage", "value": 95.9, "threshold": 85.0},
                "evidence": [
                    {"source": "node", "signal": "node_rootfs_usage", "label": "根分区使用率", "value": 95.9},
                ],
            }
        },
    )

    bridge = AgentSkillBridge(skills_dir=ROOT / "skill_runtime" / "skills", runtime_url="")
    result = bridge.run(
        "[智算事件][node_resource_pressure] 根分区使用率=95.9% 超过阈值 85%",
        namespace="llm-k8s",
        evidence={"payload": payload},
        stages=["detect", "diagnose"],
    )

    assert result["skill_runtime"]["primary_skill_id"] == "llm.k8s_vllm_observability"
    assert result["diagnosis"]["fault_type"] == "LLM::k8s_vllm_node_disk_pressure"
    assert "磁盘" in result["diagnosis"]["root_cause"] or "根分区" in result["diagnosis"]["root_cause"]


def test_skill_runtime_is_registered_as_agent_tool() -> None:
    registry = build_tool_registry(include_skill_runtime=True, isolated=True)
    names = {tool["name"] for tool in registry.list_tools()}
    assert "skill_runtime_workflow" in names

    result = registry.execute(
        "skill_runtime_workflow",
        query="Alert TargetDown service frontend returns 503 because no endpoints",
        namespace="default",
        stages=["detect", "diagnose", "heal", "verify"],
    )
    assert result.success
    data = result.data
    assert data["skill_runtime"]["primary_skill_id"] == "k8s.service_unavailable"
    assert data["agent_tool_invocation"]["tool"] == "skill_runtime_workflow"
    assert data["agent_tool_invocation"]["mode"] == "tool_registry"


def test_skill_tool_context_executes_readonly_kubectl_inside_skill() -> None:
    calls = []

    def tool_runner(tool, kwargs, payload):
        calls.append((tool, kwargs, payload))
        assert tool == "kubectl"
        assert kwargs["command"].split()[0] in {"get", "describe"}
        return {
            "success": True,
            "executed": True,
            "stdout": "NAME READY STATUS\nfrontend-abc 1/1 Running",
            "summary": f"执行 {kwargs['command']} 成功",
        }

    runtime = SkillExecutor.from_dir(ROOT / "skill_runtime" / "skills")
    run = runtime.run(
        "tool.k8s_readiness",
        {
            "fault_type": "readiness",
            "namespace": "default",
            "service": "frontend",
            "message": "K8s service endpoint readiness check",
        },
        stages=["detect", "diagnose"],
        dry_run=True,
        tool_runner=tool_runner,
    ).to_dict()

    assert calls
    diagnose = run["stages"]["diagnose"]
    assert diagnose["status"] == "ok"
    assert "已实际执行" in " ".join(diagnose["evidence"])
    assert diagnose["output"]["tool_results"][0]["success"] is True
    assert run["trace"][-1]["tool_calls"]


def test_generic_evidence_collection_can_call_readonly_tools() -> None:
    calls = []

    def tool_runner(tool, kwargs, payload):
        calls.append((tool, kwargs, payload))
        return {
            "success": True,
            "executed": True,
            "stdout": "sample evidence",
            "summary": f"{tool} {kwargs['command']} ok",
        }

    runtime = SkillExecutor.from_dir(ROOT / "skill_runtime" / "skills")
    run = runtime.run(
        "generic.evidence_collection",
        {
            "fault_type": "service_unavailable",
            "namespace": "default",
            "service": "frontend",
            "message": "frontend no endpoints",
        },
        stages=["detect", "diagnose"],
        dry_run=True,
        tool_runner=tool_runner,
    ).to_dict()

    assert calls
    assert all(call[0] == "kubectl" for call in calls)
    assert "tool_results" in run["stages"]["diagnose"]["output"]
    assert run["trace"][-1]["tool_calls"]


def test_internet_search_skill_can_call_web_search_runner() -> None:
    calls = []

    def tool_runner(tool, kwargs, payload):
        calls.append((tool, kwargs, payload))
        assert tool == "web_search"
        assert "vLLM" in kwargs["query"] or "vllm" in kwargs["query"].lower()
        return {
            "success": True,
            "executed": True,
            "data": [
                {
                    "title": "vLLM documentation",
                    "url": "https://docs.vllm.ai/",
                    "summary": "Official vLLM docs",
                }
            ],
            "summary": "web search returned 1 result",
        }

    runtime = SkillExecutor.from_dir(ROOT / "skill_runtime" / "skills")
    run = runtime.run(
        "tool.internet_search_research",
        {
            "query": "vLLM metrics target down known issue",
            "purpose": "official_docs",
            "domains": ["docs.vllm.ai", "github.com"],
            "message": "需要互联网搜索官方文档和 GitHub issue",
        },
        stages=["detect", "diagnose", "verify"],
        dry_run=True,
        tool_runner=tool_runner,
    ).to_dict()

    assert calls
    assert run["declared_stages"] == ["diagnose"]
    assert run["attempted_stages"] == ["diagnose"]
    assert "detect" not in run["stages"]
    diagnose = run["stages"]["diagnose"]
    assert diagnose["status"] == "ok"
    assert diagnose["output"]["search_results"][0]["success"] is True
    assert run["trace"][-1]["tool_calls"]


def test_internet_search_skill_is_available_through_tool_domain_workflow() -> None:
    workflow = SkillWorkflow(SkillExecutor.from_dir(ROOT / "skill_runtime" / "skills"))
    result = workflow.run(
        {
            "query": "Kubernetes ImagePullBackOff official docs",
            "message": "需要互联网检索官方文档",
            "purpose": "official_docs",
        },
        domain="tool",
        stages=["detect", "diagnose"],
        dry_run=True,
        allow_legacy_fallback=False,
    )

    assert result["skill_runtime"]["primary_skill_id"] == "tool.internet_search_research"
    assert result["diagnosis"]["fault_type"] == "InternetSearchResearch"


def test_agent_skill_bridge_routes_internet_search_query_to_tool_skill() -> None:
    bridge = AgentSkillBridge(skills_dir=ROOT / "skill_runtime" / "skills", runtime_url="")
    result = bridge.run(
        "请互联网搜索 vLLM metrics TargetDown 的官方文档和 GitHub issue",
        namespace="default",
        evidence={
            "payload": {
                "query": "vLLM metrics TargetDown official docs GitHub issue",
                "purpose": "official_docs",
            }
        },
        stages=["detect", "diagnose"],
        dry_run=True,
    )

    assert result["domain"] == "tool"
    assert result["skill_runtime"]["primary_skill_id"] == "tool.internet_search_research"
    assert result["diagnosis"]["fault_type"] == "InternetSearchResearch"


def test_skill_agent_collects_evidence_then_invokes_skill_runtime() -> None:
    calls = []
    events = []

    class Registry:
        def get(self, name):
            return object() if name == "kubectl" else None

        def execute(self, name, **kwargs):
            calls.append((name, kwargs))
            if name == "kubectl":
                return type("ToolResult", (), {
                    "success": True,
                    "error": "",
                    "data": "NAME ENDPOINTS AGE\nfrontend <none> 1m",
                })()
            assert name == "skill_runtime_workflow"
            assert kwargs["evidence"]["agent_evidence_collection"]["commands"]
            return type("ToolResult", (), {
                "success": True,
                "error": "",
                "data": {
                    "status": "completed",
                    "skill_runtime": {
                        "primary_skill_id": "k8s.service_unavailable",
                        "matched_count": 2,
                    },
                    "diagnosis": {
                        "skill_id": "k8s.service_unavailable",
                        "confidence": 0.91,
                        "root_cause": "Service 后端不可用",
                    },
                    "execution_plan": {"command_count": 1, "commands": []},
                    "agent_tool_invocation": {
                        "summary": {"summary": "Agent 调用 Skill Runtime: 主 Skill=k8s.service_unavailable"}
                    },
                },
            })()

    agent = SkillAgent(
        registry=Registry(),
        skills_dir=ROOT / "skill_runtime" / "skills",
        event_sink=events.append,
        llm_client=FakeSkillLLM(),
    )
    result = agent.run(SkillAgentRequest(
        query="Alert TargetDown service frontend returns 503 because no endpoints",
        namespace="default",
        skill_workflow={
            "stages": ["detect", "diagnose"],
            "dry_run": True,
            "payload": {
                "fault_type": "service_unavailable",
                "namespace": "default",
                "service": "frontend",
            },
        },
    ))

    phase_names = [e.get("name") for e in events if e.get("event") == "phase_start"]
    assert phase_names == [
        "CONTEXT_RETRIEVAL",
        "HYPOTHESIS_GENERATION",
        "VERIFICATION_PLANNING",
        "AGENT_SKILL_CONTEXT",
        "INVESTIGATION",
        "AGENT_SKILL_TOOL_CALL",
        "REASONING_DECISION",
    ]
    assert any(e.get("event") == "hypotheses" and len(e.get("items") or []) >= 1 and all(h.get("llm_used") for h in e["items"]) for e in events)
    assert any(e.get("event") == "plan" and e.get("checks") for e in events)
    assert any(e.get("event") == "reasoning" and e.get("summary") for e in events)
    assert any(e.get("event") == "tool_call" and e.get("tool") == "agenticsre_skill_run" for e in events)
    assert result.status == "completed"
    assert result.agent_skill_bridge == {
        "mode": "agent_tool_call",
        "agent": "skill_agent",
        "tool": "agenticsre_skill_run",
    }
    assert any(e.get("event") == "tool_call" and e.get("agent") == "skill_agent" for e in events)
    assert result.skill_runtime_result["skill_runtime"]["selection_mode"] == "agent_llm_exact_skill_calls"


def test_skill_agent_can_skip_hypothesis_and_planning_phases() -> None:
    events = []

    class Registry:
        def get(self, name):
            return object() if name in {"kubectl", "skill_runtime_workflow"} else None

        def execute(self, name, **kwargs):
            if name == "kubectl":
                return type("ToolResult", (), {"success": True, "error": "", "data": "ok"})()
            return type("ToolResult", (), {
                "success": True,
                "error": "",
                "data": {
                    "status": "completed",
                    "skill_runtime": {"primary_skill_id": "k8s.service_unavailable", "matched_count": 1},
                    "diagnosis": {"root_cause": "Service endpoint unavailable", "confidence": 0.9},
                },
            })()

    agent = SkillAgent(
        registry=Registry(),
        skills_dir=ROOT / "skill_runtime" / "skills",
        event_sink=events.append,
        llm_client=FakeSkillLLM(),
    )
    result = agent.run(SkillAgentRequest(
        query="service unavailable",
        namespace="default",
        skill_workflow={
            "analysis_phases": ["context", "investigation", "reasoning"],
            "stages": ["detect", "diagnose"],
            "payload": {"fault_type": "service_unavailable", "namespace": "default", "service": "frontend"},
        },
    ))

    phase_names = [event.get("name") for event in events if event.get("event") == "phase_start"]
    assert "CONTEXT_RETRIEVAL" in phase_names
    assert "HYPOTHESIS_GENERATION" not in phase_names
    assert "VERIFICATION_PLANNING" not in phase_names
    assert "INVESTIGATION" in phase_names
    assert "AGENT_SKILL_TOOL_CALL" in phase_names
    assert "REASONING_DECISION" in phase_names
    assert not any(event.get("event") == "hypotheses" for event in events)
    assert result.status == "completed"
    assert any(e.get("event") == "tool_call" and e.get("agent") == "skill_agent" for e in events)
    assert result.skill_runtime_result["skill_runtime"]["primary_skill_id"] == "k8s.service_unavailable"


def test_skill_agent_uses_llm_for_hypothesis_plan_and_final_reasoning() -> None:
    calls = []
    events = []

    class LLM:
        def json_chat(self, messages, temperature=None, max_tokens=None):
            content = messages[-1]["content"]
            calls.append(content)
            if "skill_catalog" in content:
                return {"skill_calls": [{"skill_id": "k8s.service_unavailable", "stages": ["detect", "diagnose"], "reason": "服务端点异常"}]}
            if "reasoning_chain" in content:
                return {"root_cause": "Service 无可用端点", "confidence": 0.91, "reasoning_chain": "工具证据与 Skill 诊断一致", "evidence": ["Endpoints 为空"]}
            if "selected_command_indexes" in content:
                return {"summary": "先检查服务端点", "selected_command_indexes": [0], "reasoning": "最小化调查"}
            return {"hypotheses": [{"id": "H1", "description": "Service 端点异常", "confidence": 0.8}]}

    class Registry:
        def get(self, name):
            return object() if name in {"kubectl", "skill_runtime_workflow"} else None

        def execute(self, name, **kwargs):
            if name == "kubectl":
                return type("ToolResult", (), {"success": True, "error": "", "data": "ENDPOINTS <none>"})()
            return type("ToolResult", (), {
                "success": True,
                "error": "",
                "data": {
                    "status": "completed",
                    "skill_runtime": {"primary_skill_id": "k8s.service_unavailable", "matched_count": 1},
                    "diagnosis": {"root_cause": "Skill preliminary result", "confidence": 0.7},
                },
            })()

    result = SkillAgent(
        registry=Registry(),
        skills_dir=ROOT / "skill_runtime" / "skills",
        event_sink=events.append,
        llm_client=LLM(),
    ).run(SkillAgentRequest(
        query="service unavailable",
        namespace="default",
        skill_workflow={
            "analysis_phases": ["context", "hypothesis", "plan", "investigation", "reasoning"],
            "stages": ["detect", "diagnose"],
            "payload": {"fault_type": "service_unavailable", "namespace": "default", "service": "frontend"},
        },
    ))

    assert len(calls) == 3
    assert not any(event.get("event") == "llm_plan_result" for event in events)
    assert any(event.get("event") == "llm_reasoning_result" for event in events)
    assert result.skill_runtime_result["diagnosis"]["root_cause"] == "Service 无可用端点"
    assert result.skill_runtime_result["diagnosis"]["agent_llm_reasoning"] is True


def test_skill_agent_does_not_let_llm_discard_executed_skill_evidence() -> None:
    prompts = []
    events = []

    class LLM:
        def json_chat(self, messages, temperature=None, max_tokens=None):
            prompts.append(messages[-1]["content"])
            return {
                "root_cause": "evidence_insufficient",
                "confidence": 0.0,
                "reasoning_chain": "没有执行任何只读检查。",
                "evidence": [],
            }

    agent = SkillAgent(
        registry=object(),
        skills_dir=ROOT / "skill_runtime" / "skills",
        event_sink=events.append,
        llm_client=LLM(),
    )
    evidence = {
        "agent_workflow": {},
        "agent_evidence_collection": {
            "commands": [{
                "command": "get deployment/frontend -o json",
                "success": True,
                "executed": True,
                "sample": '{"metadata":{"name":"frontend","namespace":"default"},"spec":{"replicas":0},"status":{"readyReplicas":0}}',
            }],
        },
    }
    skill_result = {
        "skill_runtime": {
            "primary_skill_id": "k8s.service_unavailable",
            "runs": [{
                "skill": {"id": "k8s.service_unavailable"},
                "stages": {
                    "diagnose": {
                        "status": "ok",
                        "confidence": 0.91,
                        "summary": "Service 无可用副本。",
                        "output": {"root_cause": "frontend 无可用就绪副本"},
                        "evidence": ["Deployment/frontend spec.replicas=0"],
                    },
                },
            }],
        },
        "diagnosis": {
            "fault_type": "ServiceUnavailable",
            "root_cause": "frontend 无可用就绪副本",
            "confidence": 0.91,
            "evidence": ["Deployment/frontend spec.replicas=0"],
        },
    }
    agent.reason_about_result(
        SkillAgentRequest(
            query="frontend service unavailable",
            namespace="default",
            skill_workflow={"payload": {"service": "frontend", "deployment": "frontend"}},
        ),
        hypotheses=[{"id": "H1", "description": "Deployment replicas=0", "confidence": 0.9}],
        verification_plan={"summary": "检查副本", "checks": []},
        evidence=evidence,
        skill_result=skill_result,
    )

    assert skill_result["diagnosis"]["root_cause"] == "frontend 无可用就绪副本"
    assert skill_result["diagnosis"]["evidence"] == ["Deployment/frontend spec.replicas=0"]
    assert '"replicas":0' in json.loads(prompts[0])["readonly_checks"][0]["sample"]
    assert any(event.get("event") == "llm_reasoning_evidence_guard" for event in events)
    structured = agent._confidence_evidence({}, evidence, skill_result)["structured_evidence"]["items"]
    assert any(item.get("signal_type") == "readonly_command_result" for item in structured)


def test_skill_agent_uses_observed_fallback_when_llm_omits_mandatory_skill() -> None:
    events = []

    class LLM:
        def json_chat(self, messages, temperature=None, max_tokens=None):
            return {"skill_calls": [], "stop_reason": "只读证据已经充分证实 Top1，无需调用 Skill。"}

    class Registry:
        def get(self, _name):
            return None

        def execute(self, _name, **_kwargs):
            raise AssertionError("fallback test must use supplied read-only evidence")

    agent = SkillAgent(
        registry=Registry(),
        skills_dir=ROOT / "skill_runtime" / "skills",
        event_sink=events.append,
        llm_client=LLM(),
    )
    evidence = {
        "agent_evidence_collection": {
            "commands": [
                {
                    "command": "get deployment/frontend -o json",
                    "success": True,
                    "sample": '{"spec":{"replicas":0},"status":{"readyReplicas":0}}',
                },
                {
                    "command": "get endpoints frontend -o wide",
                    "success": True,
                    "sample": "NAME ENDPOINTS\nfrontend <none>",
                },
            ],
        },
    }
    result = agent.select_and_invoke_skills(
        SkillAgentRequest(
            query="frontend Service 无法访问",
            namespace="default",
            skill_workflow={
                "payload": {
                    "source": "remote_business_observation",
                    "domain": "k8s",
                    "cluster_id": "default",
                    "namespace": "default",
                    "deployment": "frontend",
                    "service": "frontend",
                },
            },
        ),
        evidence=evidence,
        hypotheses=[{"id": "H1", "description": "Deployment 副本数配置为 0", "confidence": 0.96}],
        stages=["detect", "diagnose"],
        dry_run=True,
    )

    assert result is not None
    assert result["skill_runtime"]["primary_skill_id"] == "k8s.service_unavailable"
    assert result["skill_runtime"]["selection_mode"] == "agent_evidence_validated_fallback"
    assert any(event.get("event") == "skill_selection_evidence_fallback" for event in events)


def test_skill_agent_prefers_specific_image_pull_skill_over_generic_deployment_skill() -> None:
    events = []

    class LLM:
        def json_chat(self, messages, temperature=None, max_tokens=None):
            return {
                "skill_calls": [{
                    "skill_id": "k8s.deployment_unhealthy",
                    "stages": ["detect", "diagnose"],
                    "reason": "Pod 未就绪",
                }],
            }

    class Registry:
        def get(self, _name):
            return None

        def execute(self, _name, **_kwargs):
            raise AssertionError("specificity test must use supplied read-only evidence")

    agent = SkillAgent(
        registry=Registry(),
        skills_dir=ROOT / "skill_runtime" / "skills",
        event_sink=events.append,
        llm_client=LLM(),
    )
    result = agent.select_and_invoke_skills(
        SkillAgentRequest(
            query="image-worker 长时间未 Ready",
            namespace="default",
            skill_workflow={
                "payload": {
                    "source": "remote_business_observation",
                    "domain": "k8s",
                    "cluster_id": "default",
                    "namespace": "default",
                    "deployment": "image-worker",
                    "pod": "image-worker-abc",
                },
            },
        ),
        evidence={
            "agent_evidence_collection": {
                "commands": [{
                    "command": "describe pod/image-worker-abc",
                    "success": True,
                    "sample": "State: Waiting Reason: ImagePullBackOff; Failed to pull image missing:tag",
                }],
            },
        },
        hypotheses=[{"id": "H1", "description": "Pod 当前为 ImagePullBackOff", "confidence": 0.96}],
        stages=["detect", "diagnose"],
        dry_run=True,
    )

    assert result is not None
    assert result["skill_runtime"]["primary_skill_id"] == "k8s.image_pull_error"
    assert result["skill_runtime"]["selection_mode"] == "agent_evidence_specificity_override"
    assert any(event.get("event") == "skill_selection_specificity_override" for event in events)


def test_skill_agent_does_not_route_recalled_image_hypothesis_over_live_scale_zero() -> None:
    events = []

    class LLM:
        def json_chat(self, messages, temperature=None, max_tokens=None):
            return {
                "skill_calls": [{
                    "skill_id": "k8s.service_unavailable",
                    "stages": ["detect", "diagnose"],
                    "reason": "Deployment 无副本且 Service 无端点",
                }],
            }

    agent = SkillAgent(
        registry=object(),
        skills_dir=ROOT / "skill_runtime" / "skills",
        event_sink=events.append,
        llm_client=LLM(),
    )
    result = agent.select_and_invoke_skills(
        SkillAgentRequest(
            query="frontend Service 无法访问",
            namespace="current-ns",
            skill_workflow={"payload": {
                "domain": "k8s",
                "namespace": "current-ns",
                "deployment": "frontend",
                "service": "frontend",
            }},
        ),
        evidence={"agent_evidence_collection": {"commands": [
            {
                "command": "get deployment/frontend -o json",
                "success": True,
                "sample": '{"spec":{"replicas":0},"status":{"readyReplicas":0}}',
            },
            {
                "command": "get endpoints frontend -o wide",
                "success": True,
                "sample": "NAME ENDPOINTS\nfrontend <none>",
            },
        ]}},
        hypotheses=[{
            "id": "H1",
            "description": "历史案例曾出现 ImagePullBackOff，但当前需验证",
            "confidence": 0.93,
        }],
        stages=["detect", "diagnose"],
        dry_run=True,
    )

    assert result is not None
    assert result["skill_runtime"]["primary_skill_id"] == "k8s.service_unavailable"
    assert result["skill_runtime"]["selection_mode"] == "agent_llm_exact_skill_calls"
    assert not any(event.get("event") == "skill_selection_specificity_override" for event in events)


def test_skill_agent_aligns_stale_image_fault_type_with_reasoned_scale_zero_root() -> None:
    events = []

    class LLM:
        def json_chat(self, messages, temperature=None, max_tokens=None):
            return {
                "root_cause": (
                    "Deployment frontend 的 spec.replicas 字段被设置为 0，"
                    "没有 Pod 被创建，Service Endpoints 为空。"
                ),
                "confidence": 0.95,
                "reasoning_chain": "当前只读输出证实 replicas=0。",
                "evidence": ["spec.replicas=0", "Endpoints: <none>"],
            }

    agent = SkillAgent(
        registry=object(),
        skills_dir=ROOT / "skill_runtime" / "skills",
        event_sink=events.append,
        llm_client=LLM(),
    )
    evidence = {"agent_evidence_collection": {"commands": [{
        "command": "get deployment/frontend -o json",
        "success": True,
        "sample": '{"spec":{"replicas":0},"status":{"readyReplicas":0}}',
    }]}}
    skill_result = {
        "skill_runtime": {
            "primary_skill_id": "k8s.image_pull_error",
            "runs": [{
                "skill": {"id": "k8s.image_pull_error"},
                "stages": {"diagnose": {
                    "status": "ok",
                    "confidence": 0.88,
                    "evidence": ["stale ImagePullError diagnosis"],
                    "output": {"fault_type": "ImagePullError"},
                }},
            }],
        },
        "diagnosis": {
            "skill_id": "k8s.image_pull_error",
            "fault_type": "ImagePullError",
            "root_cause": "镜像拉取失败",
            "confidence": 0.88,
            "evidence": ["stale ImagePullError diagnosis"],
        },
    }
    agent.reason_about_result(
        SkillAgentRequest(
            query="frontend Service 无法访问",
            namespace="default",
            skill_workflow={"payload": {"service": "frontend", "deployment": "frontend"}},
        ),
        hypotheses=[{"id": "H1", "description": "历史 ImagePullBackOff", "confidence": 0.9}],
        verification_plan={"summary": "查看副本", "checks": []},
        evidence=evidence,
        skill_result=skill_result,
    )

    assert skill_result["diagnosis"]["fault_type"] == "ServiceUnavailable"
    assert skill_result["diagnosis"]["fault_type_original"] == "ImagePullError"
    assert any(event.get("event") == "fault_type_evidence_alignment" for event in events)


def test_skill_agent_parses_nested_llm_tool_call_selection() -> None:
    agent = SkillAgent(
        registry=object(),
        skills_dir=ROOT / "skill_runtime" / "skills",
        llm_client=FakeSkillLLM(),
    )

    calls = agent._extract_skill_calls({
        "tool_calls": [{
            "function": {
                "name": "agenticsre_skill_run",
                "arguments": '{"calls":[{"id":"generic.evidence_collection","stages":["detect","diagnose"]}]}',
            }
        }]
    })

    assert calls == [{"id": "generic.evidence_collection", "skill_id": "generic.evidence_collection", "stages": ["detect", "diagnose"]}]
    assert agent._extract_skill_calls({
        "skills_invoked": [{"skill_id": "generic.root_cause_diagnosis", "phase": "diagnose"}]
    }) == [{"skill_id": "generic.root_cause_diagnosis", "phase": "diagnose"}]
    assert agent._normalize_reasoning_result({
        "result": {"diagnosis": {"根因": "Service endpoints 为空", "置信度": 0.88, "推理链": "证据一致"}}
    }) == {
        "root_cause": "Service endpoints 为空",
        "confidence": 0.88,
        "reasoning_chain": "证据一致",
        "evidence": [],
    }
    assert agent._normalize_reasoning_result({
        "root_cause": "DiskPressure",
        "confidence": "high",
        "confidence_score": 0.9,
    })["confidence"] == 0.9


def test_skill_agent_uses_cluster_aware_kubectl_runner_before_skill_runtime() -> None:
    runner_calls = []
    runtime_evidence = {}

    class Registry:
        def get(self, name):
            return None

        def execute(self, name, **kwargs):
            assert name == "skill_runtime_workflow"
            runtime_evidence.update(kwargs["evidence"])
            return type("ToolResult", (), {
                "success": True,
                "error": "",
                "data": {
                    "status": "completed",
                    "skill_runtime": {
                        "primary_skill_id": "llm.k8s_vllm_observability",
                        "matched_count": 1,
                    },
                    "diagnosis": {
                        "skill_id": "llm.k8s_vllm_observability",
                        "fault_type": "LLM::k8s_vllm_os_resource_pressure",
                        "confidence": 0.9,
                        "root_cause": "智算节点资源压力影响 vLLM 推理。",
                    },
                    "execution_plan": {"command_count": 1, "commands": []},
                    "agent_tool_invocation": {
                        "summary": {"summary": "Agent 调用 Skill Runtime: 主 Skill=llm.k8s_vllm_observability"}
                    },
                },
            })()

    def runner(command, namespace, payload):
        runner_calls.append((command, namespace, dict(payload)))
        return {
            "ok": True,
            "command": f"KUBECONFIG=/tmp/t4.yaml kubectl {command} -n {namespace}",
            "stdout": "NAME READY STATUS\nvllm-server 1/1 Running",
            "stderr": "",
            "returncode": 0,
        }

    events = []
    agent = SkillAgent(
        registry=Registry(),
        skills_dir=ROOT / "skill_runtime" / "skills",
        event_sink=events.append,
        kubectl_runner=runner,
        llm_client=FakeSkillLLM(),
    )
    result = agent.run(SkillAgentRequest(
        query="[智算事件][resource_pressure] dds-127 rootfs usage high",
        namespace="llm-k8s",
        skill_workflow={
            "stages": ["detect", "diagnose", "heal", "verify"],
            "dry_run": True,
            "payload": {
                "source": "ai_event_center",
                "domain": "llm",
                "cluster_id": "t4-vllm-k8s",
                "namespace": "llm-k8s",
                "deployment": "vllm-server",
                "service": "vllm-server",
                "fault_type": "resource_pressure",
            },
        },
    ))

    assert result.status == "completed", [(e.get("event"), e.get("summary"), e.get("candidate_skill_ids")) for e in result.events[-6:]]
    assert runner_calls
    assert runner_calls[0][2]["cluster_id"] == "t4-vllm-k8s"
    commands = result.agent_evidence["agent_evidence_collection"]["commands"]
    assert commands[0]["source"] == "cluster_kubectl"
    assert commands[0]["cluster_id"] == "t4-vllm-k8s"
    assert "vllm-server" in commands[0]["sample"]
    assert any(e.get("event") == "tool_call" and e.get("cluster_id") == "t4-vllm-k8s" for e in events)


def test_skill_agent_requires_llm_for_agent_skill_mode() -> None:
    class Registry:
        def get(self, name):
            return None

        def execute(self, name, **kwargs):
            raise AssertionError("Skill Runtime must not be called without LLM hypotheses")

    events = []
    agent = SkillAgent(
        registry=Registry(),
        skills_dir=ROOT / "skill_runtime" / "skills",
        event_sink=events.append,
    )
    result = agent.run(SkillAgentRequest(
        query="Alert TargetDown service frontend returns 503",
        namespace="default",
        skill_workflow={
            "stages": ["detect", "diagnose"],
            "payload": {"fault_type": "service_unavailable", "namespace": "default", "service": "frontend"},
        },
    ))

    assert result.status == "failed"
    assert result.agent_skill_bridge["reason"] == "llm_required"
    assert any(e.get("event") == "llm_required_missing" for e in events)


def test_skill_agent_generates_llm_hypotheses_with_historical_context() -> None:
    class FakeLLM:
        def json_chat(self, messages, temperature=None, max_tokens=None):
            if "skill_catalog" in messages[-1]["content"]:
                return {"skill_calls": [{"skill_id": "k8s.service_unavailable", "stages": ["detect", "diagnose"], "reason": "历史与当前证据匹配"}]}
            if "selected_command_indexes" in messages[-1]["content"]:
                return {"summary": "检查服务端点", "selected_command_indexes": [0]}
            if "reasoning_chain" in messages[-1]["content"]:
                return {"root_cause": "Service endpoints 为空。", "confidence": 0.9, "reasoning_chain": "证据一致"}
            assert "相似故障" in messages[-1]["content"]
            return {
                "hypotheses": [
                    {
                        "id": "H1",
                        "description": "历史案例显示 vLLM Service endpoints 为空导致请求 503。",
                        "confidence": 0.84,
                        "service": "vllm-server",
                        "fault_type": "service_unavailable",
                        "expected_evidence": ["endpoints 为空", "请求返回 503"],
                        "investigation_plan": ["kubectl get endpoints vllm-server -n llm-k8s"],
                        "supporting_evidence": ["命中相似历史故障 svc-empty-1"],
                    }
                ]
            }

    class Registry:
        def get(self, name):
            return None

        def execute(self, name, **kwargs):
            assert name == "skill_runtime_workflow"
            return type("ToolResult", (), {
                "success": True,
                "error": "",
                "data": {
                    "status": "completed",
                    "skill_runtime": {"primary_skill_id": "k8s.service_unavailable", "matched_count": 1},
                    "diagnosis": {
                        "skill_id": "k8s.service_unavailable",
                        "fault_type": "service_unavailable",
                        "confidence": 0.9,
                        "root_cause": "Service endpoints 为空。",
                    },
                    "execution_plan": {"command_count": 1, "commands": []},
                    "agent_tool_invocation": {"summary": {"summary": "Agent 调用 Skill Runtime"}},
                },
            })()

    def context_provider(_request):
        return {
            "rules": [{"condition": "TargetDown", "conclusion": "检查 Service endpoints"}],
            "faults": [{"id": "svc-empty-1", "fault_type": "service_unavailable", "root_cause": "Service endpoints 为空"}],
            "history_stats": {"sample_count": 8, "success_rate": 0.875, "sufficient_samples": True},
        }

    events = []
    agent = SkillAgent(
        registry=Registry(),
        kubectl_runner=_fixture_kubectl_runner,
        skills_dir=ROOT / "skill_runtime" / "skills",
        event_sink=events.append,
        context_provider=context_provider,
        llm_client=FakeLLM(),
    )
    result = agent.run(SkillAgentRequest(
        query="Alert TargetDown vllm-server returns 503",
        namespace="llm-k8s",
        skill_workflow={
            "stages": ["detect", "diagnose"],
            "payload": {
                "domain": "llm",
                "fault_type": "service_unavailable",
                "namespace": "llm-k8s",
                "service": "vllm-server",
            },
        },
    ))

    hypotheses = result.to_dict()["hypotheses"]
    assert result.status == "completed"
    assert hypotheses[0]["llm_used"] is True
    assert hypotheses[0]["source"] == "llm"
    assert hypotheses[0]["confidence"] > 0.84
    assert result.to_dict()["hypothesis_generation"]["llm_used"] is True
    assert any(e.get("event") == "context" and e.get("rules_count") == 1 for e in events)
    assert any(e.get("event") == "llm_hypotheses" and e.get("success") for e in events)


def test_skill_agent_penalizes_hypothesis_supported_only_by_background_signal() -> None:
    class Registry:
        def get(self, _name):
            return None

    agent = SkillAgent(
        registry=Registry(),
        skills_dir=ROOT / "skill_runtime" / "skills",
        llm_client=FakeSkillLLM(),
    )
    payload = {
        "fault_type": "fault_experiment_signal",
        "success_rate": 0.1,
        "observation_signals": [{
            "signal": "success_rate",
            "label": "vLLM 请求成功率",
            "value": 0.1,
            "candidate_skill": "llm.business_slo",
        }],
        "background_observation_signals": [{
            "signal": "node_rootfs_usage",
            "label": "节点根分区使用率",
            "value": 96.0,
            "candidate_skill": "k8s.resource_pressure",
        }],
        "node_rootfs_usage": 96.0,
    }
    context = {
        "observation_signals": payload["observation_signals"],
        "background_observation_signals": payload["background_observation_signals"],
        "faults": [],
        "rules": [],
        "recent_runs": [],
        "history_stats": {},
    }

    ranked = agent._merge_and_rank_hypotheses(
        [
            {
                "description": "节点根分区使用率过高导致 vLLM 成功率下降",
                "fault_type": "resource_exhaustion",
                "confidence": 0.95,
                "llm_used": True,
            },
            {
                "description": "vLLM 请求成功率下降，业务侧已受影响",
                "fault_type": "business_success_rate_drop",
                "confidence": 0.75,
                "llm_used": True,
            },
        ],
        payload=payload,
        context=context,
    )

    assert ranked[0]["fault_type"] == "business_success_rate_drop"
    background = next(item for item in ranked if item["fault_type"] == "resource_exhaustion")
    assert background["confidence"] <= 0.55
    assert any("背景" in item for item in background["supporting_evidence"])


def test_skill_agent_retries_invalid_llm_skill_selection_with_exact_catalog_ids() -> None:
    events = []

    class RetryLLM:
        def json_chat(self, messages, temperature=None, max_tokens=None):
            content = messages[-1]["content"]
            if '"valid_skills"' in content:
                return {
                    "skill_calls": [{
                        "skill_id": "llm.model_serving_endpoint",
                        "stages": ["detect", "diagnose"],
                        "reason": "Pod 已就绪但 Endpoint 为空",
                    }]
                }
            if '"skill_catalog"' in content:
                return {"raw_response": "", "parse_error": True}
            if "selected_command_indexes" in content:
                return {"summary": "检查 Service 与 Endpoint", "selected_command_indexes": [0]}
            if "reasoning_chain" in content:
                return {"root_cause": "Service 无可用 Endpoint", "confidence": 0.9}
            return {"hypotheses": [{
                "description": "Service selector 与 Pod 标签不匹配",
                "confidence": 0.88,
                "fault_type": "service_unavailable",
            }]}

    class Registry:
        def get(self, _name):
            return None

    result = SkillAgent(
        registry=Registry(),
        kubectl_runner=_fixture_kubectl_runner,
        skills_dir=ROOT / "skill_runtime" / "skills",
        event_sink=events.append,
        llm_client=RetryLLM(),
    ).run(SkillAgentRequest(
        query="vllm-server Pod Ready but Service has no endpoints",
        namespace="llm-k8s",
        skill_workflow={
            "stages": ["detect", "diagnose"],
            "payload": {
                "domain": "llm",
                "fault_type": "fault_experiment_signal",
                "namespace": "llm-k8s",
                "service": "vllm-server",
                "observation_signals": [{"signal": "endpoint_count", "value": 0}],
            },
        },
    ))

    assert result.status == "completed", [(e.get("event"), e.get("summary"), e.get("candidate_skill_ids")) for e in result.events[-6:]]
    assert result.skill_runtime_result["skill_runtime"]["primary_skill_id"] == "llm.model_serving_endpoint"
    assert any(event.get("event") == "skill_selection_retry" for event in events)
    assert any(event.get("event") == "llm_skill_selection_retry_result" for event in events)


def test_skill_agent_intersects_requested_stages_with_declared_capability() -> None:
    class StageLLM:
        def json_chat(self, messages, temperature=None, max_tokens=None):
            return {
                "skill_calls": [{
                    "skill_id": "generic.healing_planner",
                    "stages": ["detect", "diagnose", "heal", "verify"],
                    "reason": "根据诊断生成恢复计划",
                }]
            }

    class Registry:
        def get(self, _name):
            return None

    agent = SkillAgent(
        registry=Registry(),
        skills_dir=ROOT / "skill_runtime" / "skills",
        llm_client=StageLLM(),
    )
    request = SkillAgentRequest(
        query="为 frontend 服务不可用生成恢复计划",
        namespace="default",
        skill_workflow={
            "stages": ["detect", "diagnose", "heal", "verify"],
            "payload": {
                "domain": "k8s",
                "fault_type": "service_unavailable",
                "deployment": "frontend",
                "diagnosis": {"confidence": 0.92, "root_cause": "Service 无可用 Endpoint"},
            },
        },
    )

    result = agent.select_and_invoke_skills(
        request,
        evidence={},
        stages=["detect", "diagnose", "heal", "verify"],
        dry_run=True,
    )

    assert result is not None
    run = result["skill_runtime"]["runs"][0]
    assert run["skill"]["id"] == "generic.healing_planner"
    assert run["requested_stages"] == ["heal"]
    assert run["attempted_stages"] == ["heal"]


def test_skill_agent_accepts_llm_raw_json_response_for_hypotheses() -> None:
    class FakeLLM:
        def json_chat(self, messages, temperature=None, max_tokens=None):
            if "skill_catalog" in messages[-1]["content"]:
                return {"skill_calls": [{"skill_id": "k8s.service_unavailable", "stages": ["detect", "diagnose"], "reason": "观测证据匹配"}]}
            if "selected_command_indexes" in messages[-1]["content"]:
                return {"summary": "检查 endpoints", "selected_command_indexes": [0]}
            if "reasoning_chain" in messages[-1]["content"]:
                return {"root_cause": "selector mismatch", "confidence": 0.9, "reasoning_chain": "证据一致"}
            return {
                "raw_response": (
                    "```json\n"
                    "{\"candidates\":[{\"description\":\"Service selector 与 Pod 标签不匹配导致 endpoints 为空\","
                    "\"confidence\":0.87,\"service\":\"frontend\",\"fault_type\":\"service_unavailable\","
                    "\"expected_evidence\":[\"endpoints 为空\"],\"investigation_plan\":[\"kubectl get svc frontend -o yaml\"]}]}"
                    "\n```"
                ),
                "parse_error": True,
            }

    class Registry:
        def get(self, name):
            return None

        def execute(self, name, **kwargs):
            return type("ToolResult", (), {
                "success": True,
                "error": "",
                "data": {
                    "status": "completed",
                    "skill_runtime": {"primary_skill_id": "k8s.service_unavailable", "matched_count": 1},
                    "diagnosis": {"skill_id": "k8s.service_unavailable", "confidence": 0.9, "root_cause": "selector mismatch"},
                    "execution_plan": {"command_count": 1, "commands": []},
                    "agent_tool_invocation": {"summary": {"summary": "Agent 调用 Skill Runtime"}},
                },
            })()

    agent = SkillAgent(
        registry=Registry(),
        skills_dir=ROOT / "skill_runtime" / "skills",
        llm_client=FakeLLM(),
    )
    result = agent.run(SkillAgentRequest(
        query="Alert TargetDown frontend no endpoints",
        namespace="default",
        skill_workflow={
            "stages": ["detect", "diagnose"],
            "payload": {"fault_type": "service_unavailable", "namespace": "default", "service": "frontend"},
        },
    ))

    top = result.to_dict()["hypotheses"][0]
    assert top["llm_used"] is True
    assert top["source"] == "llm"
    assert "selector" in top["description"]


def test_rca_run_with_skill_workflow_emits_skill_runtime_event(monkeypatch) -> None:
    import time
    import web_app.app as dashboard

    dashboard._state["rca_runs"] = {}
    cfg = dashboard._get_config()
    old_api_key = cfg.llm.api_key
    cfg.llm.api_key = "test-key"
    monkeypatch.setattr(dashboard, "LLMClient", FakeSkillLLM)

    class FakeResult:
        status = "completed"

        def to_dict(self) -> dict:
            return {"status": "completed", "root_cause": "测试 RCA 完成"}

    captured: dict = {}

    def fake_tool_registry(_cfg, allow_write=False, **_kwargs):
        class Registry:
            def get(self, name):
                return None

            def execute(self, name, **kwargs):
                assert name == "skill_runtime_workflow"
                assert kwargs["stages"] == ["detect", "diagnose"]
                captured.update(kwargs)
                return type("ToolResult", (), {
                    "success": True,
                    "error": "",
                    "data": {
                        "status": "completed",
                        "skill_runtime": {
                            "primary_skill_id": "k8s.service_unavailable",
                            "matched_count": 1,
                        },
                        "diagnosis": {
                            "skill_id": "k8s.service_unavailable",
                            "confidence": 0.88,
                            "root_cause": "Service 后端不可用",
                        },
                        "execution_plan": {"command_count": 1, "commands": []},
                        "agent_tool_invocation": {
                            "summary": {"summary": "Agent 调用 Skill Runtime: 主 Skill=k8s.service_unavailable"}
                        },
                    },
                })()

        return Registry()

    monkeypatch.setattr(dashboard, "build_tool_registry", fake_tool_registry)

    client = TestClient(dashboard.app)
    response = client.post("/api/rca/run", json={
        "query": "Alert TargetDown service frontend returns 503 because no endpoints",
        "namespace": "default",
        "skill_workflow": {
            "domain": "k8s",
            "stages": ["detect", "diagnose"],
            "dry_run": True,
            "payload": {"fault_type": "service_unavailable", "namespace": "default"},
        },
    })
    assert response.status_code == 200
    run_id = response.json()["run_id"]

    deadline = time.time() + 3
    run = {}
    while time.time() < deadline:
        run = client.get(f"/api/rca/{run_id}").json()
        if run.get("status") in {"completed", "failed"}:
            break
        time.sleep(0.05)

    skill_event = next(e for e in run["events"] if e.get("event") == "skill_runtime")
    phase_names = [e.get("name") for e in run["events"] if e.get("event") == "phase_start"]
    skill_index = phase_names.index("AGENT_SKILL_TOOL_CALL")
    plan_index = phase_names.index("VERIFICATION_PLANNING")
    evidence_index = phase_names.index("INVESTIGATION")
    assert plan_index < evidence_index < skill_index
    assert skill_event["tool"] == "agenticsre_skill_run"
    assert skill_event["primary_skill_id"] == "k8s.service_unavailable"
    assert skill_event["result"]["skill_runtime"]["selection_mode"] == "agent_llm_exact_skill_calls"
    assert run["result"]["agent_skill_bridge"]["mode"] == "agent_tool_call"
    assert run["result"]["agent_skill_bridge"]["agent"] == "skill_agent"
    cfg.llm.api_key = old_api_key


def test_rca_run_reuses_only_matching_active_dedup_key(monkeypatch) -> None:
    import threading
    import time
    import web_app.app as dashboard

    dashboard._state["rca_runs"] = {}
    dashboard._state["rca_dedup"] = {}
    cfg = dashboard._get_config()
    old_api_key = cfg.llm.api_key
    cfg.llm.api_key = "test-key"
    started = threading.Event()
    release = threading.Event()

    class BlockingResult:
        status = "completed"
        skill_runtime_result = {"status": "completed"}
        agent_skill_bridge = {"mode": "test"}

        def to_dict(self) -> dict:
            return {"status": "completed", "root_cause": "dedup test"}

    class BlockingSkillAgent:
        def __init__(self, **_kwargs):
            pass

        def run(self, _request):
            started.set()
            release.wait(timeout=3)
            return BlockingResult()

    monkeypatch.setattr(dashboard, "SkillAgent", BlockingSkillAgent)
    monkeypatch.setattr(dashboard, "build_tool_registry", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(dashboard, "LLMClient", lambda *_args, **_kwargs: object())

    body = {
        "query": "diagnose AI inference queue",
        "namespace": "llm-k8s",
        "dedup_key": "ai-event:test:agent-skill:detect,diagnose:context,hypothesis,plan,investigation,reasoning",
        "skill_workflow": {
            "domain": "llm",
            "stages": ["detect", "diagnose"],
            "payload": {"fault_type": "request_queue_high", "namespace": "llm-k8s"},
        },
    }
    client = TestClient(dashboard.app)
    try:
        first = client.post("/api/rca/run", json=body)
        assert first.status_code == 200
        assert started.wait(timeout=1)

        repeated = client.post("/api/rca/run", json=body)
        assert repeated.status_code == 200
        assert repeated.json()["run_id"] == first.json()["run_id"]
        assert repeated.json()["reused"] is True
        assert len(dashboard._state["rca_runs"]) == 1

        release.set()
        deadline = time.time() + 2
        while time.time() < deadline and dashboard._state["rca_runs"][first.json()["run_id"]]["status"] == "running":
            time.sleep(0.02)

        restarted = client.post("/api/rca/run", json=body)
        assert restarted.status_code == 200
        assert restarted.json()["run_id"] != first.json()["run_id"]
        assert restarted.json().get("reused") is not True
    finally:
        release.set()
        cfg.llm.api_key = old_api_key


def test_rca_run_skill_workflow_rejects_unresolved_llm_placeholder(monkeypatch) -> None:
    import web_app.app as dashboard

    dashboard._state["rca_runs"] = {}
    cfg = dashboard._get_config()
    old_api_key = cfg.llm.api_key
    cfg.llm.api_key = "${LLM_API_KEY}"
    try:
        client = TestClient(dashboard.app)
        response = client.post("/api/rca/run", json={
            "query": "Alert TargetDown service frontend returns 503 because no endpoints",
            "namespace": "default",
            "skill_workflow": {
                "domain": "k8s",
                "stages": ["detect", "diagnose"],
                "dry_run": True,
                "payload": {"namespace": "default", "service": "frontend"},
            },
        })
        assert response.status_code == 503
        assert "必须由大模型参与推理" in response.text
        assert dashboard._state["rca_runs"] == {}
    finally:
        cfg.llm.api_key = old_api_key


def test_rca_run_agent_skill_collects_evidence_before_skill(monkeypatch) -> None:
    import time
    import web_app.app as dashboard

    dashboard._state["rca_runs"] = {}
    monkeypatch.setattr(dashboard, "_agent_skill_kubectl_runner", _fixture_kubectl_runner)
    cfg = dashboard._get_config()
    old_api_key = cfg.llm.api_key
    cfg.llm.api_key = "test-key"
    monkeypatch.setattr(dashboard, "LLMClient", FakeSkillLLM)

    def fake_tool_registry(_cfg, allow_write=False, **_kwargs):
        class Registry:
            def get(self, name):
                return object() if name == "kubectl" else None

            def execute(self, name, **kwargs):
                if name == "kubectl":
                    return type("ToolResult", (), {
                        "success": True,
                        "error": "",
                        "data": "NAME READY STATUS",
                    })()
                assert name == "skill_runtime_workflow"
                return type("ToolResult", (), {
                    "success": True,
                    "error": "",
                    "data": {
                        "status": "completed",
                        "skill_runtime": {
                            "primary_skill_id": "k8s.service_unavailable",
                            "matched_count": 2,
                        },
                        "diagnosis": {
                            "skill_id": "k8s.service_unavailable",
                            "confidence": 0.91,
                            "root_cause": "Service 后端不可用",
                        },
                        "execution_plan": {"command_count": 1, "commands": []},
                        "agent_tool_invocation": {
                            "summary": {"summary": "Agent 调用 Skill Runtime: 主 Skill=k8s.service_unavailable"}
                        },
                    },
                })()

        return Registry()

    monkeypatch.setattr(dashboard, "build_tool_registry", fake_tool_registry)

    client = TestClient(dashboard.app)
    response = client.post("/api/rca/run", json={
        "query": "Alert TargetDown service frontend returns 503 because no endpoints",
        "namespace": "default",
        "skill_workflow": {
            "domain": "k8s",
            "stages": ["detect", "diagnose", "heal", "verify"],
            "dry_run": True,
            "payload": {"fault_type": "service_unavailable", "namespace": "default"},
        },
    })
    assert response.status_code == 200
    run_id = response.json()["run_id"]

    deadline = time.monotonic() + 3
    run = {}
    while time.monotonic() < deadline:
        run = client.get(f"/api/rca/{run_id}").json()
        # A Skill event is intermediate; completion is published afterwards.
        if run.get("status") in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.05)

    assert run["status"] == "completed", [(e.get("event"), e.get("summary"), e.get("candidate_skill_ids")) for e in run.get("events", [])[-6:]]
    assert any(e.get("event") == "skill_runtime" for e in run["events"])
    phases = [e.get("name") for e in run["events"] if e.get("event") == "phase_start"]
    assert phases.index("VERIFICATION_PLANNING") < phases.index("INVESTIGATION") < phases.index("AGENT_SKILL_TOOL_CALL")
    assert any(e.get("event") == "tool_call" and e.get("tool") == "kubectl" for e in run["events"])
    assert any(e.get("event") == "evidence" and e.get("agent") == "skill_agent" for e in run["events"])
    assert run["result"]["agent_skill_bridge"]["mode"] == "agent_tool_call"
    assert run["result"]["agent_skill_bridge"]["agent"] == "skill_agent"
    cfg.llm.api_key = old_api_key


def test_rca_run_agent_skill_uses_payload_cluster_for_investigation(monkeypatch) -> None:
    import time
    import web_app.app as dashboard

    dashboard._state["rca_runs"] = {}
    # FakeSkillLLM handles every model call in this test. Keep the production
    # configured-model check intact and supply only a disposable test value.
    monkeypatch.setattr(dashboard._get_config().llm, "api_key", "offline-skill-fixture-placeholder")
    shell_calls = []

    def fake_shell_sync(cmd, timeout=90, cluster_id=None, env=None):
        shell_calls.append({"cmd": cmd, "timeout": timeout, "cluster_id": cluster_id})
        return {
            "ok": True,
            "returncode": 0,
            "command": f"KUBECONFIG=/tmp/t4.yaml {cmd}",
            "stdout": "NAME READY STATUS\nvllm-server 1/1 Running",
            "stderr": "",
        }

    def fake_tool_registry(_cfg, allow_write=False, **_kwargs):
        class Registry:
            def get(self, name):
                return None

            def execute(self, name, **kwargs):
                assert name == "skill_runtime_workflow"
                commands = kwargs["evidence"]["agent_evidence_collection"]["commands"]
                assert commands
                assert commands[0]["cluster_id"] == "t4-vllm-k8s"
                return type("ToolResult", (), {
                    "success": True,
                    "error": "",
                    "data": {
                        "status": "completed",
                        "skill_runtime": {
                            "primary_skill_id": "llm.k8s_vllm_observability",
                            "matched_count": 1,
                        },
                        "diagnosis": {
                            "skill_id": "llm.k8s_vllm_observability",
                            "fault_type": "LLM::k8s_vllm_os_resource_pressure",
                            "confidence": 0.9,
                            "root_cause": "智算节点资源压力影响 vLLM 推理。",
                        },
                        "execution_plan": {"command_count": 1, "commands": []},
                        "agent_tool_invocation": {
                            "summary": {"summary": "Agent 调用 Skill Runtime: 主 Skill=llm.k8s_vllm_observability"}
                        },
                    },
                })()

        return Registry()

    monkeypatch.setattr(dashboard, "_shell_sync", fake_shell_sync)
    monkeypatch.setattr(dashboard, "_agent_skill_kubectl_runner", lambda command, namespace, payload:
                        fake_shell_sync(command, cluster_id=payload.get("cluster_id")))
    monkeypatch.setattr(dashboard, "LLMClient", FakeSkillLLM)
    monkeypatch.setattr(dashboard, "build_tool_registry", fake_tool_registry)

    client = TestClient(dashboard.app)
    response = client.post("/api/rca/run", json={
        "query": "[智算事件][resource_pressure] dds-127 rootfs high",
        "namespace": "llm-k8s",
        "skill_workflow": {
            "domain": "llm",
            "stages": ["detect", "diagnose", "heal", "verify"],
            "dry_run": True,
            "payload": {
                "source": "ai_event_center",
                "domain": "llm",
                "cluster_id": "t4-vllm-k8s",
                "namespace": "llm-k8s",
                "deployment": "vllm-server",
                "service": "vllm-server",
                "fault_type": "resource_pressure",
            },
        },
    })
    assert response.status_code == 200
    run_id = response.json()["run_id"]

    deadline = time.time() + 3
    run = {}
    while time.time() < deadline:
        run = client.get(f"/api/rca/{run_id}").json()
        if run.get("status") == "completed":
            break
        time.sleep(0.05)

    assert run["status"] == "completed", [(e.get("event"), e.get("summary"), e.get("candidate_skill_ids")) for e in run.get("events", [])[-6:]]
    assert shell_calls
    assert {call["cluster_id"] for call in shell_calls} == {"t4-vllm-k8s"}
    assert any(
        e.get("event") == "tool_call" and e.get("cluster_id") == "t4-vllm-k8s" and e.get("success")
        for e in run["events"]
    )


def test_rca_run_agent_skill_uses_llm_and_history_context(monkeypatch, tmp_path) -> None:
    import time
    import web_app.app as dashboard

    dashboard._state["rca_runs"] = {}
    dashboard._state.pop("fault_store", None)

    class FakeLLM:
        def __init__(self, _cfg):
            pass

        def json_chat(self, messages, temperature=None, max_tokens=None):
            if "skill_catalog" in messages[-1]["content"]:
                return {"skill_calls": [{"skill_id": "k8s.service_unavailable", "stages": ["detect", "diagnose"], "reason": "观测证据匹配"}]}
            if "selected_command_indexes" in messages[-1]["content"]:
                return {"summary": "检查 endpoints", "selected_command_indexes": [0]}
            if "reasoning_chain" in messages[-1]["content"]:
                return {"root_cause": "service endpoints 为空", "confidence": 0.9, "reasoning_chain": "证据一致"}
            assert "历史规则" in messages[-1]["content"]
            return {
                "hypotheses": [
                    {
                        "description": "相似历史规则显示 service endpoints 为空导致 TargetDown。",
                        "confidence": 0.86,
                        "service": "frontend",
                        "fault_type": "service_unavailable",
                        "expected_evidence": ["endpoints 为空"],
                        "investigation_plan": ["kubectl get endpoints frontend -n default"],
                    }
                ]
            }

    from copy import deepcopy
    from memory.fault_context_store import FaultContextStore
    scoped_cfg = deepcopy(dashboard._get_config())
    scoped_cfg.memory.backend = 'json'
    scoped_cfg.memory.db_path = str(tmp_path / 'memory')
    store = FaultContextStore(scoped_cfg)
    store.add_rule({'condition': 'TargetDown', 'conclusion': '检查 endpoints',
                    'cluster_id': 'memory-test', 'namespace': 'default'})
    store.add_fault({'description': 'TargetDown', 'fault_type': 'service_unavailable',
                     'root_cause': 'endpoints empty', 'cluster_id': 'memory-test', 'namespace': 'default'})

    def fake_tool_registry(_cfg, allow_write=False, **_kwargs):
        class Registry:
            def get(self, name):
                return None

            def execute(self, name, **kwargs):
                assert name == "skill_runtime_workflow"
                hypotheses = kwargs["evidence"]["agent_workflow"]["hypotheses"]
                assert hypotheses[0]["llm_used"] is True
                return type("ToolResult", (), {
                    "success": True,
                    "error": "",
                    "data": {
                        "status": "completed",
                        "skill_runtime": {"primary_skill_id": "k8s.service_unavailable", "matched_count": 1},
                        "diagnosis": {
                            "skill_id": "k8s.service_unavailable",
                            "fault_type": "service_unavailable",
                            "confidence": 0.9,
                            "root_cause": "Service endpoints 为空。",
                        },
                        "execution_plan": {"command_count": 1, "commands": []},
                        "agent_tool_invocation": {"summary": {"summary": "Agent 调用 Skill Runtime"}},
                    },
                })()

        return Registry()

    cfg = dashboard._get_config()
    old_key = cfg.llm.api_key
    cfg.llm.api_key = "test-key"
    try:
        monkeypatch.setattr(dashboard, "LLMClient", FakeLLM)
        monkeypatch.setattr(dashboard, "_get_fault_store", lambda: store)
        monkeypatch.setattr(cfg.memory, 'enabled', True)
        monkeypatch.setattr(cfg.fault_targets, 'k8s_clusters', [
            {'id': 'memory-test', 'name': 'Memory Test', 'kind': 'general'}])
        monkeypatch.setattr(dashboard, "build_tool_registry", fake_tool_registry)
        monkeypatch.setattr(dashboard, "_agent_skill_kubectl_runner", _fixture_kubectl_runner)

        client = TestClient(dashboard.app)
        response = client.post("/api/rca/run", json={
            "query": "Alert TargetDown service frontend returns 503 because no endpoints",
            "cluster_id": "memory-test",
            "namespace": "default",
            "skill_workflow": {
                "domain": "k8s",
                "stages": ["detect", "diagnose"],
                "dry_run": True,
                "payload": {"fault_type": "service_unavailable", "namespace": "default", "service": "frontend",
                            "cluster_id": "memory-test"},
            },
        })
        assert response.status_code == 200
        run_id = response.json()["run_id"]

        deadline = time.time() + 3
        run = {}
        while time.time() < deadline:
            run = client.get(f"/api/rca/{run_id}").json()
            if run.get("status") == "completed":
                break
            time.sleep(0.05)

        assert run["status"] == "completed", [(e.get("event"), e.get("summary"), e.get("candidate_skill_ids")) for e in run.get("events", [])[-6:]]
        assert any(e.get("event") == "llm_hypotheses" and e.get("success") for e in run["events"])
        assert any(e.get("event") == "context" and e.get("rules_count") == 1 for e in run["events"])
        assert run["result"]["hypotheses"][0]["llm_used"] is True
    finally:
        cfg.llm.api_key = old_key


def test_rca_run_agent_skill_does_not_depend_on_workflow_tool(monkeypatch) -> None:
    import time
    import web_app.app as dashboard

    dashboard._state["rca_runs"] = {}
    monkeypatch.setattr(dashboard, "_agent_skill_kubectl_runner", _fixture_kubectl_runner)
    cfg = dashboard._get_config()
    old_api_key = cfg.llm.api_key
    cfg.llm.api_key = "test-key"
    monkeypatch.setattr(dashboard, "LLMClient", FakeSkillLLM)

    class MissingToolRegistry:
        def get(self, name):
            return None

        def execute(self, name, **kwargs):
            assert name == "skill_runtime_workflow"
            return type("ToolResult", (), {
                "success": False,
                "error": "Tool 'skill_runtime_workflow' not found",
                "data": None,
            })()

    monkeypatch.setattr(dashboard, "build_tool_registry", lambda _cfg, allow_write=False, **_kwargs: MissingToolRegistry())

    try:
        client = TestClient(dashboard.app)
        response = client.post("/api/rca/run", json={
            "query": "Alert TargetDown service frontend returns 503 because no endpoints",
            "namespace": "default",
            "skill_workflow": {
                "domain": "k8s",
                "stages": ["detect", "diagnose"],
                "dry_run": True,
                "payload": {"namespace": "default"},
            },
        })
        assert response.status_code == 200
        run_id = response.json()["run_id"]

        deadline = time.time() + 3
        run = {}
        while time.time() < deadline:
            run = client.get(f"/api/rca/{run_id}").json()
            if run.get("status") == "completed":
                break
            time.sleep(0.05)

        assert run["status"] == "completed"
        skill_event = next(e for e in run["events"] if e.get("event") == "skill_runtime")
        assert skill_event["status"] == "completed"
        assert skill_event["tool"] == "agenticsre_skill_run"
        assert run["result"]["agent_skill_bridge"]["tool"] == "agenticsre_skill_run"
    finally:
        cfg.llm.api_key = old_api_key


def test_skill_workflow_selects_specific_primary_for_common_alerts() -> None:
    cases = [
        ("Alert TargetDown service frontend returns 503 because no endpoints", "k8s.service_unavailable"),
        ("Pod checkout CrashLoopBackOff restart back-off in namespace default", "k8s.pod_crashloop"),
        ("Pod payment ImagePullBackOff failed to pull image registry timeout", "k8s.image_pull_error"),
        ("Node dds-127 NotReady kubelet stopped reporting status", "k8s.node_not_ready"),
        ("Container recommender OOMKilled out of memory after traffic spike", "k8s.oom_killed"),
        ("Service frontend cannot resolve redis DNS name intermittent lookup failure", "k8s.dns_resolution"),
        ("Pod llm-worker Pending FailedScheduling insufficient nvidia.com/gpu", "k8s.scheduling_failed"),
        ("Pod database volume mount failed MountVolume.SetUp failed", "k8s.volume_mount_failed"),
        ("NetworkPolicy deny blocks frontend to backend connection refused", "k8s.network_policy_deny"),
        ("HPA scaling failed unable to get cpu utilization metrics", "k8s.hpa_scaling_failed"),
        ("Pod api CreateContainerConfigError missing ConfigMap app-config", "k8s.config_missing"),
        ("vLLM process crash restart on T4 inference endpoint", "llm.vllm_process_crash"),
        ("GPU memory high KV cache pressure TTFT P95 latency increased", "llm.gpu_resource_pressure"),
        ("LLM inference endpoint timeout TTFT P95 high TPOT degraded", "llm.business_slo"),
        ("mysql db connection pool exhausted too many connections slow queries", "tool.db_connection_pool"),
    ]
    registry = build_tool_registry(include_skill_runtime=True, isolated=True)
    for query, expected in cases:
        result = registry.execute(
            "skill_runtime_workflow",
            query=query,
            namespace="default",
            stages=["detect", "diagnose", "heal", "verify"],
            evidence={"payload": {"pod": "payment"}} if expected == "k8s.image_pull_error" else None,
            tool_runner=_fixture_image_tool if expected == "k8s.image_pull_error" else None,
        )
        assert result.success, query
        data = result.data
        assert data["skill_runtime"]["primary_skill_id"] == expected, query
        assert data["skill_runtime"]["runs"], query
        assert data["diagnosis"]["confidence"] > 0, query
        assert data["verification_plan"]["checks"], query


def test_skill_workflow_routes_observable_k8s_and_ai_event_payloads() -> None:
    workflow = SkillWorkflow(executor)
    cases = [
        (
            "prometheus_scrape",
            "k8s.prometheus_scrape_target",
            {
                "domain": "k8s",
                "raw_fault_type": "TargetDown",
                "namespace": "kube-system",
                "service": "kube-prom-kube-prometheus-kube-proxy",
                "message": "Prometheus cannot scrape metrics endpoint scrape_error connection refused metrics endpoint may be bound to localhost",
                "evidence": [
                    {"source": "prometheus", "signal": "up", "value": "0"},
                    {"source": "prometheus", "signal": "scrape_error", "value": "connection refused"},
                ],
            },
        ),
        (
            "service_no_ready_endpoints",
            "k8s.service_unavailable",
            {
                "domain": "k8s",
                "alertname": "ServiceUnavailable",
                "namespace": "default",
                "service": "frontend",
                "deployment": "frontend",
                "message": "frontend returns HTTP 503. Kubernetes Service frontend has no ready endpoints and EndpointSlice is empty",
                "evidence": [
                    {"source": "kubernetes", "signal": "endpoints", "value": "empty"},
                    {"source": "http_probe", "signal": "status_code", "value": "503"},
                ],
            },
        ),
        (
            "image_pull_backoff",
            "k8s.image_pull_error",
            {
                "domain": "k8s",
                "namespace": "llm-k8s",
                "pod": "agenticsre-ai-imagepull-demo",
                "message": "Pod is Pending/ImagePullBackOff. kubelet reports Back-off pulling image registry.invalid/agenticsre/missing:demo",
                "evidence": [
                    {"source": "kubernetes_event", "signal": "reason", "value": "ImagePullBackOff"},
                    {"source": "kubelet", "signal": "message", "value": "Back-off pulling image registry.invalid/agenticsre/missing:demo"},
                ],
            },
        ),
        (
            "gpu_unschedulable",
            "k8s.scheduling_failed",
            {
                "domain": "k8s",
                "namespace": "llm-k8s",
                "pod": "agenticsre-ai-schedule-demo",
                "message": "Pod is Pending/Unschedulable. Scheduler says insufficient nvidia.com/gpu and node affinity mismatch",
                "evidence": [
                    {"source": "scheduler", "signal": "reason", "value": "Unschedulable"},
                    {"source": "scheduler", "signal": "message", "value": "insufficient nvidia.com/gpu; node affinity mismatch"},
                ],
            },
        ),
        (
            "vllm_queue_latency",
            "llm.vllm_scheduler_queue",
            {
                "domain": "llm",
                "namespace": "llm-k8s",
                "service": "vllm-gemma",
                "deployment": "vllm-server",
                "metric": "waiting_requests",
                "message": "vLLM 推理队列堆积 waiting_requests >= 8 TTFT P95 4.2s E2E P95 9.8s GPU 利用率正常但请求延迟恶化",
                "evidence": [
                    {"source": "prometheus", "signal": "waiting_requests", "value": "8"},
                    {"source": "prometheus", "signal": "ttft_p95", "value": "4.2s"},
                    {"source": "prometheus", "signal": "e2e_latency_p95", "value": "9.8s"},
                ],
            },
        ),
        (
            "dcgm_metrics_missing",
            "llm.dcgm_exporter_observability",
            {
                "domain": "llm",
                "namespace": "llm-k8s",
                "service": "gpu-dcgm-exporter",
                "node": "dds-127",
                "metric": "DCGM_FI_DEV_GPU_UTIL",
                "message": "GPU 利用率和显存曲线长时间为 0 或缺失，Prometheus target gpu-dcgm-exporter down，无法看到 dds-127 GPU 指标",
                "evidence": [
                    {"source": "prometheus", "signal": "target_health", "value": "down"},
                    {"source": "prometheus", "signal": "gpu_metric_absent", "value": "DCGM_FI_DEV_GPU_UTIL absent"},
                ],
            },
        ),
    ]
    for name, expected, payload in cases:
        result = workflow.run(payload, domain=payload["domain"], dry_run=True,
            tool_runner=_fixture_image_tool if expected == "k8s.image_pull_error" else None)
        assert result["skill_runtime"]["primary_skill_id"] == expected, name
        assert result["diagnosis"]["confidence"] > 0, name
        assert result["diagnosis"]["root_cause"], name


def test_agent_skill_llm_hypothesis_cannot_override_observed_fault_family() -> None:
    observed = {
        "agent_skill_context": {
            "payload": {
                "payload": {
                    "source": "alert_center",
                    "domain": "k8s",
                    "alertname": "ServiceUnavailable",
                    "namespace": "default",
                    "service": "frontend",
                    "deployment": "frontend",
                    "evidence": [
                        {"source": "kubernetes", "signal": "endpoints", "value": "empty"},
                        {"source": "http_probe", "signal": "status_code", "value": "503"},
                    ],
                }
            }
        },
        "agent_workflow": {
            "hypotheses": [
                {
                    "description": "历史案例提示 metricsBindAddress 绑定 127.0.0.1 也可能导致 TargetDown",
                    "fault_type": "misconfiguration",
                    "expected_evidence": ["metricsBindAddress=127.0.0.1", "Prometheus scrape failed"],
                    "source": "llm",
                    "llm_used": True,
                }
            ]
        },
    }
    payload = build_skill_payload(
        "Alert ServiceUnavailable: frontend returns HTTP 503. Kubernetes Service frontend has no ready endpoints.",
        namespace="default",
        evidence=observed,
    )
    result = SkillWorkflow(executor).run(payload, domain="k8s", dry_run=True)

    assert payload["fault_type"] == "service_unavailable"
    assert "metricsBindAddress" in payload["agent_evidence_summary"]
    assert "metricsBindAddress" not in payload["observed_evidence_summary"]
    assert result["skill_runtime"]["primary_skill_id"] == "k8s.service_unavailable"


def test_agent_skill_control_plane_hypothesis_cannot_override_service_observation() -> None:
    observed = {
        "agent_skill_context": {
            "payload": {
                "payload": {
                    "source": "alert_center",
                    "domain": "k8s",
                    "fault_type": "service_unavailable",
                    "namespace": "default",
                    "service": "frontend",
                    "deployment": "frontend",
                    "evidence": [
                        {"source": "kubernetes", "signal": "endpoints", "value": "empty"},
                        {"source": "http_probe", "signal": "status_code", "value": "503"},
                    ],
                }
            }
        },
        "agent_workflow": {
            "hypotheses": [
                {
                    "description": "kubelet 健康异常可能导致 Pod 状态失真",
                    "fault_type": "control_plane_unhealthy",
                    "expected_evidence": ["kubelet down", "node agent unhealthy"],
                    "source": "llm",
                    "llm_used": True,
                }
            ]
        },
    }
    payload = build_skill_payload(
        "Alert ServiceUnavailable: frontend returns HTTP 503. Kubernetes Service frontend has no ready endpoints.",
        namespace="default",
        evidence=observed,
    )
    result = SkillWorkflow(executor).run(payload, domain="k8s", dry_run=True)

    assert payload["fault_type"] == "service_unavailable"
    assert "kubelet" in payload["agent_evidence_summary"]
    assert "kubelet" not in payload["observed_evidence_summary"]
    assert result["skill_runtime"]["primary_skill_id"] == "k8s.service_unavailable"


def test_agent_skill_namespace_events_do_not_pollute_service_unavailable_routing() -> None:
    observed = {
        "agent_skill_context": {
            "payload": {
                "payload": {
                    "source": "alert_center",
                    "domain": "k8s",
                    "fault_type": "service_unavailable",
                    "namespace": "default",
                    "service": "frontend",
                    "deployment": "frontend",
                    "evidence": [
                        {"source": "kubernetes", "signal": "endpoints", "value": "empty"},
                        {"source": "http_probe", "signal": "status_code", "value": "503"},
                    ],
                }
            }
        },
        "agent_evidence_collection": {
            "commands": [
                {
                    "command": "kubectl get events --sort-by=.lastTimestamp",
                    "summary": "kubectl get events 在集群 default 执行成功",
                    "sample": (
                        "Warning Failed pod/agenticsre-general-imagepull-demo Error: ImagePullBackOff\n"
                        "Warning FailedScheduling pod/agenticsre-general-schedule-demo node affinity mismatch\n"
                    ),
                },
                {
                    "command": "kubectl describe svc frontend",
                    "summary": "kubectl describe svc frontend 在集群 default 执行成功",
                    "sample": "Name: frontend\nSelector: app=frontend\nEndpoints: <none>\n",
                },
            ]
        },
    }
    payload = build_skill_payload(
        "Alert ServiceUnavailable: frontend returns HTTP 503. Kubernetes Service frontend has no ready endpoints.",
        namespace="default",
        evidence=observed,
    )
    result = SkillWorkflow(executor).run(payload, domain="k8s", dry_run=True)

    assert "ImagePullBackOff" in payload["observed_evidence_summary"]
    assert result["skill_runtime"]["primary_skill_id"] == "k8s.service_unavailable"


def test_skill_workflow_prefers_prometheus_scrape_over_generic_targetdown() -> None:
    workflow = SkillWorkflow(executor)
    result = workflow.run(
        {
            "domain": "k8s",
            "fault_type": "service_unavailable",
            "raw_fault_type": "TargetDown",
            "namespace": "kube-system",
            "service": "kube-prom-kube-prometheus-kube-proxy",
            "message": (
                "[WARNING] Alert: TargetDown kube-proxy metrics target is down. "
                "Prometheus cannot scrape kube-proxy /metrics endpoint; service has no ready endpoints."
            ),
        },
        domain="k8s",
        dry_run=True,
    )

    assert result["skill_runtime"]["primary_skill_id"] == "k8s.prometheus_scrape_target"
    assert result["diagnosis"]["skill_id"] == "k8s.prometheus_scrape_target"
    assert result["diagnosis"]["fault_type"].startswith("K8S::prometheus_")


def test_skill_workflow_keeps_business_service_unavailable_without_scrape_signal() -> None:
    workflow = SkillWorkflow(executor)
    result = workflow.run(
        {
            "domain": "k8s",
            "fault_type": "service_unavailable",
            "raw_fault_type": "TargetDown",
            "namespace": "default",
            "service": "frontend",
            "deployment": "frontend",
            "message": "Alert TargetDown frontend returns HTTP 503 and service has no ready endpoints.",
        },
        domain="k8s",
        dry_run=True,
    )

    assert result["skill_runtime"]["primary_skill_id"] == "k8s.service_unavailable"
    assert result["diagnosis"]["skill_id"] == "k8s.service_unavailable"
    assert result["diagnosis"]["fault_type"] == "ServiceUnavailable"


def test_skill_workflow_routes_etcd_insufficient_members_to_control_plane() -> None:
    workflow = SkillWorkflow(executor)
    result = workflow.run(
        {
            "domain": "k8s",
            "fault_type": "control_plane_unhealthy",
            "raw_fault_type": "etcdInsufficientMembers",
            "alert_name": "etcdInsufficientMembers",
            "namespace": "kube-system",
            "service": "kube-prom-kube-prometheus-kube-etcd",
            "message": (
                "[WARNING] Alert: etcdInsufficientMembers "
                "kube-prom-kube-prometheus-kube-etcd reports insufficient members; "
                "etcd quorum may be unavailable."
            ),
        },
        domain="k8s",
        dry_run=True,
    )

    assert result["skill_runtime"]["primary_skill_id"] == "k8s.control_plane_unhealthy"
    assert result["diagnosis"]["skill_id"] == "k8s.control_plane_unhealthy"
    assert result["diagnosis"]["fault_type"] == "EtcdUnhealthy"
    assert "etcd" in result["diagnosis"]["root_cause"].lower()
    assert "endpoints" not in result["diagnosis"]["root_cause"].lower()


def test_agent_tool_routes_etcd_query_to_control_plane_skill() -> None:
    registry = build_tool_registry(include_skill_runtime=True, isolated=True)
    result = registry.execute(
        "skill_runtime_workflow",
        query=(
            "Alert: etcdInsufficientMembers on kube-prom-kube-prometheus-kube-etcd; "
            "etcd quorum has insufficient members"
        ),
        namespace="kube-system",
        stages=["detect", "diagnose", "heal", "verify"],
    )
    assert result.success
    data = result.data

    assert data["skill_runtime"]["primary_skill_id"] == "k8s.control_plane_unhealthy"
    assert data["diagnosis"]["skill_id"] == "k8s.control_plane_unhealthy"
    assert data["diagnosis"]["fault_type"] == "EtcdUnhealthy"
