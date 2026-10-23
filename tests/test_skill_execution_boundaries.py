"""Regression tests for review findings; no shell/cluster/LLM calls."""
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.skill_agent import SkillAgent, SkillAgentRequest
from skill_runtime.executor import SkillExecutor
from skill_runtime.kubectl_safety import readonly_kubectl_args
from tools.k8s_tools import KubectlTool
import web_app.app as dashboard

SKILLS = Path(__file__).resolve().parents[1] / "skill_runtime/skills"


@pytest.mark.parametrize("value", ["default; echo BAD #", "default$(id)", "default`id`", "default\nversion", "default|cat", "default && id"])
def test_readonly_skill_rejects_namespace_before_runner(value):
    calls = []
    result = SkillExecutor.from_dir(SKILLS).run(
        "tool.k8s_readiness", {"namespace": value}, stages=["diagnose"],
        tool_runner=lambda *args: calls.append(args), dry_run=True)
    assert not calls
    assert all(item["error"] == "tool_policy_denied" for item in result.stages["diagnose"].output["tool_results"])


@pytest.mark.parametrize("command", ["get pods; id", "get pods | cat", "get pods --context=other", "get pods -n other", "get pods -shttps://other", "get pods -s=https://other", "get pods -Ashttps://other", "delete pod x"])
def test_readonly_parser_rejects_programs_and_scope_override(command):
    with pytest.raises(ValueError):
        readonly_kubectl_args(command, "default")


def test_web_runner_uses_argv_and_cluster_scope(monkeypatch):
    calls = []
    monkeypatch.setattr(dashboard, "_find_k8s_cluster", lambda _: {"context": "chosen"})
    monkeypatch.setattr(dashboard, "_cluster_kubeconfig_path", lambda _: "/tmp/chosen config")
    monkeypatch.setattr(dashboard, "_cluster_kubectl_path", lambda _: "kubectl")
    monkeypatch.setattr(dashboard, "_get_config", lambda: SimpleNamespace(kubernetes=SimpleNamespace(use_ssh=False)))
    monkeypatch.setattr(dashboard.subprocess, "run", lambda argv, **kw: calls.append((argv, kw)) or SimpleNamespace(returncode=0, stdout="{}", stderr=""))
    result = dashboard._agent_skill_kubectl_runner("get pods -o json", "default", {"cluster_id": "selected"})
    assert result["ok"]
    assert calls[0][0] == ["kubectl", "--kubeconfig=/tmp/chosen config", "--context=chosen", "get", "pods", "-o", "json", "-n", "default"]
    assert calls[0][1]["shell"] is False
    assert not dashboard._agent_skill_kubectl_runner("get pods; id", "default", {})["ok"]
    assert len(calls) == 1


def test_fallback_kubectl_rejects_injection(monkeypatch):
    monkeypatch.setattr("tools.k8s_tools.subprocess.run", lambda *a, **k: pytest.fail("must not execute"))
    assert not KubectlTool().execute(command="get pods; id").success


@pytest.mark.parametrize("healthy", [False, True])
def test_pod_delete_requires_controller_recovery(monkeypatch, healthy):
    calls = []
    def check(command, **kwargs):
        calls.append(command)
        if command.startswith("get pod "):
            return {"ok": True, "stdout": ""}
        if command.startswith("rollout"):
            return {"ok": healthy}
        return {"ok": True, "json": {"spec": {"replicas": 1}, "status": {"replicas": 1, "readyReplicas": int(healthy), "availableReplicas": int(healthy)}}}
    monkeypatch.setattr(dashboard, "_kubectl_heal_check", check)
    result = dashboard._verify_heal_recovery(namespace="default", suggestion={"target": {"pod": "old", "deployment": "frontend"}}, commands=["kubectl delete pod old"], cluster_id=None, dry_run=False, executed_success=True)
    assert result["recovered"] is healthy
    assert any(cmd.startswith("rollout") for cmd in calls)


def test_pod_delete_without_controller_is_unknown(monkeypatch):
    monkeypatch.setattr(dashboard, "_kubectl_heal_check", lambda *a, **k: {"ok": True, "stdout": ""})
    result = dashboard._verify_heal_recovery(namespace="default", suggestion={"target": {"pod": "old"}}, commands=["kubectl delete pod old"], cluster_id=None, dry_run=False, executed_success=True)
    assert result["recovered"] is None
    assert result["action_completed"] is True


@pytest.mark.parametrize("message", ["Pod is healthy; no OOMKilled events observed", "没有 OOMKilled", "historical OOMKilled", "memory limit is configured"])
def test_oom_negation_and_configuration_are_not_faults(message):
    result = SkillExecutor.from_dir(SKILLS).run("k8s.oom_killed", {"message": message}, stages=["detect", "diagnose"])
    assert result.stages["detect"].status == "skipped"
    assert result.stages["diagnose"].status == "skipped"


def test_structured_oom_reason_is_detected():
    result = SkillExecutor.from_dir(SKILLS).run("k8s.oom_killed", {"last_terminated_reason": "OOMKilled"}, stages=["detect", "diagnose"])
    assert result.stages["detect"].status == "matched"


def test_unrelated_command_does_not_override_abstention_or_credit_template():
    agent = SkillAgent.__new__(SkillAgent)
    agent.events, agent.event_sink, agent._last_context = [], None, {}
    agent._call_llm_json = lambda *a, **kw: {"root_cause": "证据不足", "confidence": 0.1, "evidence": []}
    diagnosis = {"skill_id": "k8s.oom_killed", "fault_type": "OOMKilled", "root_cause": "frontend 容器内存超过限制", "confidence": 0.86, "evidence": ["目标 Pod frontend"]}
    result = {"diagnosis": copy.deepcopy(diagnosis), "skill_runtime": {"primary_skill_id": "k8s.oom_killed", "runs": [{"skill": {"id": "k8s.oom_killed"}, "stages": {"diagnose": {"status": "ok", "evidence": diagnosis["evidence"]}}}]}}
    evidence = {"agent_evidence_collection": {"commands": [{"command": "version", "success": True, "sample": "Client version v1.30.0"}]}}
    request = SkillAgentRequest(query="frontend alert", skill_workflow={"payload": {"deployment": "frontend"}})
    agent.reason_about_result(request, hypotheses=[], verification_plan={}, evidence=evidence, skill_result=result)
    assert result["diagnosis"]["root_cause"] == "证据不足"
    assert not any(e["event"] == "llm_reasoning_evidence_guard" for e in agent.events)
    calibrated = agent._confidence_evidence({}, evidence, result)
    assert not any(k.startswith("skill_") for k in calibrated)


@pytest.mark.parametrize("mismatch", ["namespace", "cluster", "metadata", "execution"])
def test_verified_observation_requires_exact_scope(mismatch):
    payload = {"deployment": "frontend", "namespace": "production", "cluster_id": "c1"}
    obj = {"metadata": {"name": "frontend", "namespace": "production"}, "spec": {"replicas": 0}, "status": {"readyReplicas": 0}}
    item = {"command": "get deployment/frontend -o json", "executed": True, "cluster_id": "c1"}
    if mismatch == "namespace":
        obj["metadata"]["namespace"] = "other"
    elif mismatch == "cluster":
        item["cluster_id"] = "other"
    elif mismatch == "metadata":
        obj.pop("metadata")
    else:
        item["executed"] = False
    item["sample"] = json.dumps(obj)
    assert not SkillAgent._has_verified_skill_observation("k8s.service_unavailable", payload, [item])
