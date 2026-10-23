"""Tests for deterministic alignment between live evidence and RCA prose."""

from types import SimpleNamespace

from orchestrator.rca_engine import _align_root_cause_with_targeted_evidence, _lifecycle_ranking_evidence
from orchestrator.collaboration_optimizer import CollaborationOptimizer
import pytest


def target(name="probe", namespace="default", kind="Pod"):
    return {"namespace": namespace, "kind": kind, "name": name}


def evidence(*items):
    return {"event_agent": {"evidence_items": list(items)}}


def hypothesis(description: str):
    return [SimpleNamespace(id="H1", description=description)]


@pytest.mark.parametrize('warning_name,warning_ns,state,expected', [
    ('probe', 'default', 'Pending', 'scheduling_failure'),
    ('peer', 'default', 'Pending', '配置错误'),
    ('probe', 'other', 'Pending', '配置错误'),
    ('probe', 'default', 'Running', '配置错误'),
])
def test_localized_fault_category_requires_matching_current_scheduling_observations(warning_name, warning_ns, state, expected):
    from configs.config_loader import AppConfig
    from orchestrator.collaboration_optimizer import CollaborationPolicy
    report = {'root_entity': target(), 'root_cause': '目标 Pod 的选择器配置与节点标签不匹配。', 'fault_type': '配置错误'}
    facts = evidence(
        {'signal_type': 'pod_waiting_state', 'source': 'kubectl', 'service': 'probe',
         'observation': state, 'raw_ref': {'name': 'probe', 'namespace': 'default', 'phase': state}},
        {'signal_type': 'pod_warning_event', 'source': 'kubectl', 'service': warning_name,
         'observation': 'scheduler reported selector mismatch',
         'raw_ref': {'kind': 'Pod', 'name': warning_name, 'namespace': warning_ns, 'reason': 'FailedScheduling'}},
    )
    result = CollaborationOptimizer(AppConfig()).calibrate_fault_type(report, facts, CollaborationPolicy())
    assert result['fault_type'] == expected
    assert result['root_cause'] == report['root_cause']


def test_scale_zero_overrides_unsupported_selector_mismatch() -> None:
    original = {
        "root_entity": target(kind="Deployment"),
        "root_cause": "Service 标签选择器与 Pod 标签不匹配，导致 Endpoints 为空。",
        "reasoning_chain": "模型生成的解释。",
    }
    aligned = _align_root_cause_with_targeted_evidence(
        original,
        evidence=evidence(
            {"signal_type": "deployment_scaled_to_zero", "service": "probe", "raw_ref": {"namespace": "default", "name": "probe"}, "observation": "desired replicas is 0"},
            {"signal_type": "service_endpoints_empty", "service": "probe", "raw_ref": {"namespace": "default", "name": "probe", "deployment": "probe"}, "observation": "0 ready addresses"},
        ),
        hypotheses=hypothesis("Deployment 副本数被设置为 0，导致 Service 没有端点。"),
        targeted_evidence_complete=True,
    )

    assert "副本数被设置为 0" in aligned["root_cause"]
    assert aligned["root_cause_original"] == original["root_cause"]
    assert aligned["root_cause_alignment"]["reason"] == "deployment_scaled_to_zero_with_empty_endpoints"


def test_current_image_pull_state_overrides_unrelated_report() -> None:
    aligned = _align_root_cause_with_targeted_evidence(
        {"root_entity": target(), "root_cause": "Service selector mismatch caused no endpoints."},
        evidence=evidence({
            "signal_type": "pod_waiting_state",
            "observation": "Pod/probe current state is ImagePullBackOff.",
            "raw_ref": {"name": "probe", "namespace": "default", "reason": "ImagePullBackOff"},
        }),
        hypotheses=hypothesis("Pod 使用了不存在的镜像，当前处于 ImagePullBackOff。"),
        targeted_evidence_complete=True,
    )

    assert "ImagePullBackOff" in aligned["root_cause"]
    assert aligned["root_cause_alignment"]["reason"] == "current_pod_image_pull_waiting_state"


def test_current_image_pull_state_overrides_replica_zero_history_hypothesis() -> None:
    aligned = _align_root_cause_with_targeted_evidence(
        {"root_entity": target("image-worker"), "root_cause": "Deployment replicas=0 caused no endpoints."},
        evidence=evidence({
            "signal_type": "pod_waiting_state",
            "service": "image-worker",
            "observation": "Pod/image-worker current state is ImagePullBackOff.",
            "raw_ref": {"name": "image-worker", "namespace": "default", "reason": "ImagePullBackOff"},
        }),
        hypotheses=hypothesis("Deployment replicas=0"),
        targeted_evidence_complete=True,
    )

    assert "ImagePullBackOff" in aligned["root_cause"]
    assert "replicas=0" not in aligned["root_cause"]


def test_alignment_does_not_run_without_completed_targeted_evidence() -> None:
    original = {"root_cause": "Service 标签选择器不匹配。"}
    aligned = _align_root_cause_with_targeted_evidence(
        original,
        evidence=evidence(
            {"signal_type": "deployment_scaled_to_zero"},
            {"signal_type": "service_endpoints_empty"},
        ),
        hypotheses=hypothesis("Deployment 副本数为 0。"),
        targeted_evidence_complete=False,
    )

    assert aligned is original


def test_other_pod_cannot_override_scheduling_root_or_fault_type():
    original = {"root_entity": target("schedule-demo"),
                "root_cause": "Pod schedule-demo nodeSelector does not match any node.",
                "fault_type": "scheduling_failure", "affected_services": ["schedule-demo"]}
    facts = evidence({"signal_type": "pod_waiting_state", "service": "image-demo",
        "observation": "Pod/image-demo current state is ImagePullBackOff.",
        "raw_ref": {"namespace": "default", "name": "image-demo", "reason": "ImagePullBackOff"}})
    aligned = _align_root_cause_with_targeted_evidence(original, evidence=facts,
        hypotheses=hypothesis("Unrelated image pull failure"), targeted_evidence_complete=True)
    assert aligned == original
    calibrated = object.__new__(CollaborationOptimizer).calibrate_fault_type(aligned, facts, None)
    assert calibrated["root_cause"] == original["root_cause"]
    assert calibrated["fault_type"] == "scheduling_failure"
    unclassified = {**original, "fault_type": "pending_pod"}
    assert object.__new__(CollaborationOptimizer).calibrate_fault_type(unclassified, facts, None)["fault_type"] == "pending_pod"


def test_same_name_in_other_namespace_cannot_override_root():
    original = {"root_entity": target("probe", "payments"), "root_cause": "Pod probe has a scheduling constraint."}
    aligned = _align_root_cause_with_targeted_evidence(original,
        evidence=evidence({"signal_type": "pod_waiting_state", "service": "probe",
            "raw_ref": {"namespace": "other", "name": "probe", "reason": "ImagePullBackOff"}}),
        hypotheses=[], targeted_evidence_complete=True)
    assert aligned == original


def test_unknown_identity_cannot_authorize_deterministic_override():
    original = {"root_cause": "Evidence is insufficient to identify the root object."}
    aligned = _align_root_cause_with_targeted_evidence(original,
        evidence=evidence({"signal_type": "pod_waiting_state", "observation": "ImagePullBackOff"}),
        hypotheses=[], targeted_evidence_complete=True)
    assert aligned == original


def test_matching_image_state_still_corrects_report_without_using_other_hypothesis():
    original = {"root_entity": target("image-demo"), "root_cause": "Deployment replicas=0."}
    aligned = _align_root_cause_with_targeted_evidence(original,
        evidence=evidence({"signal_type": "pod_waiting_state", "service": "image-demo",
            "raw_ref": {"namespace": "default", "name": "image-demo", "reason": "ImagePullBackOff"}}),
        hypotheses=hypothesis("Other deployment has replicas=0"), targeted_evidence_complete=True)
    assert "image-demo" in aligned["root_cause"]
    assert "ImagePullBackOff" in aligned["root_cause"]


def test_late_skill_host_success_cannot_publish_success_after_stage_timeout(tmp_path, monkeypatch):
    import time
    from orchestrator.skill_agent import SkillAgent
    from skill_runtime.executor import SkillExecutor
    from skill_runtime.registry import SkillRegistry

    monkeypatch.setenv("AGENTICSRE_SKILL_STAGE_TIMEOUT_S", ".2")
    folder = tmp_path / "probe"
    folder.mkdir()
    (folder / "skill.yaml").write_text("id: test.probe\nversion: 1.0\nname: probe\ndomain: test\nstages: [diagnose]\n")
    (folder / "skill.py").write_text("def diagnose(context):\n    context['tools'].execute('kubectl', command='get pods')\n    return {'status':'ok'}\n")
    agent = object.__new__(SkillAgent)
    agent.events, agent.event_sink, agent.registry = [], None, None
    def slow_runner(command, namespace, payload):
        time.sleep(.6)
        return {"success": True, "executed": True, "summary": "late success", "data": "pods"}
    agent.kubectl_runner = slow_runner
    executor = SkillExecutor(SkillRegistry(tmp_path).load())
    run = executor.run("test.probe", {}, stages=["diagnose"], tool_runner=agent._skill_tool_runner("default"))
    assert run.status == "failed"
    assert run.trace[0]["tool_calls"][0]["outcome_unknown"] is True
    time.sleep(.7)
    assert not any(e.get("event") == "evidence" and e.get("success") for e in agent.events)


def test_single_pod_ranking_excludes_peer_faults_but_keeps_exact_warning():
    facts = evidence(
        {"signal_type": "pod_waiting_state", "service": "target", "observation": "Pending",
         "raw_ref": {"namespace": "default", "name": "target", "phase": "Pending"}},
        {"signal_type": "pod_waiting_state", "service": "peer", "observation": "ImagePullBackOff",
         "raw_ref": {"namespace": "default", "name": "peer", "reason": "ImagePullBackOff"}})
    facts["event_agent"]["summary"] = "peer ImagePullBackOff; unrelated namespace summary"
    facts["event_agent"]["warning_events"] = (
        "2026-10-19T00:00:00Z default Pod/target FailedScheduling count=1: node selector mismatch\n"
        "2026-10-19T00:00:00Z default Pod/target-copy Failed count=1: ImagePullBackOff\n"
        "2026-10-19T00:00:00Z other Pod/target Failed count=1: ImagePullBackOff")
    ranked = _lifecycle_ranking_evidence("FailedScheduling: Pod/target", "default", facts)
    assert "ImagePullBackOff" not in str(ranked)
    assert "node selector mismatch" in ranked["event_agent"]["summary"]
    assert len(ranked["event_agent"]["evidence_items"]) == 1
    assert len(facts["event_agent"]["evidence_items"]) == 2


def test_multi_object_incident_keeps_full_investigation_scope():
    facts = evidence({"signal_type": "pod_waiting_state", "observation": "Pending"})
    assert _lifecycle_ranking_evidence("Pod/one and Pod/two Pending", "default", facts) is facts


@pytest.mark.parametrize('query', ['请调查 Pod/target 当前状态', 'Inspect pod/target without presuming a fault',
                                  '请核对 Pod: target 是否健康'])
def test_neutral_single_pod_request_keeps_target_facts_before_prompt_budget(query):
    from agents.evidence import format_evidence
    peers = [{'signal_type':'pod_waiting_state','service':f'peer-{i}',
              'observation':'unrelated measurement ' * 100,
              'raw_ref':{'namespace':'default','name':f'peer-{i}','phase':'Pending'}} for i in range(12)]
    facts=evidence(*peers,{'signal_type':'pod_waiting_state','service':'target',
        'observation':'target observed state','raw_ref':{'namespace':'default','name':'target','phase':'Pending'}})
    ranked=_lifecycle_ranking_evidence(query,'default',facts)
    assert len(ranked['event_agent']['evidence_items'])==1
    text=format_evidence(ranked,limit=6000)
    assert 'target observed state' in text and 'unrelated measurement' not in text
    assert len(facts['event_agent']['evidence_items'])==13


def test_explicit_pod_and_service_investigation_retains_multi_object_evidence():
    facts=evidence({'signal_type':'pod_waiting_state','observation':'Pending'})
    assert _lifecycle_ranking_evidence('Investigate Pod/one and Service/backend Pending','default',facts) is facts
