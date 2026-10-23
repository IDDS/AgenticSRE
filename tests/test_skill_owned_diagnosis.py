"""Evidence and failure contracts for Skill-owned diagnosis."""
import json
from pathlib import Path
import pytest

from orchestrator.skill_agent import SkillAgent, SkillAgentRequest
from skill_runtime.executor import SkillExecutor

SKILLS = Path(__file__).resolve().parents[1] / "skill_runtime" / "skills"


class Registry:
    def get(self, name):
        return None


class Model:
    def __init__(self, empty=False):
        self.calls = []
        self.empty = empty

    def json_chat(self, messages, **options):
        self.calls.append((messages, options))
        if self.empty:
            return {"parse_error": True, "raw_response": ""}
        if "skill_diagnose_stages" in messages[-1]["content"]:
            return {"root_cause": "Pod/demo 镜像不存在，仓库返回 not found。", "confidence": 0.85,
                    "reasoning_chain": "当前目标容器的只读状态确认镜像不存在。", "evidence": ["kubectl 当前容器错误 not found"]}
        return {"hypotheses": [{"description": "Pod/demo 镜像拉取失败待验证", "confidence": 0.7}],
                "skill_calls": [{"skill_id": "k8s.image_pull_error", "stages": ["detect", "diagnose"], "target": {"pod": "demo"}}]}


def request():
    return SkillAgentRequest(query="请调查 Pod/demo 未就绪原因", namespace="audit", skill_workflow={
        "stages": ["detect", "diagnose"], "payload": {"pod": "demo", "namespace": "audit", "domain": "k8s", "cluster_id": "default"}})


def waiting_status():
    return [{"name": "worker", "image": "registry/app:missing", "state": {"waiting": {
        "reason": "ImagePullBackOff", "message": "failed to resolve registry/app:missing: not found"}}}]


def test_agent_decides_then_skill_collects_then_agent_reasons():
    model = Model()
    commands = []

    def runner(command, namespace, payload):
        commands.append((command, namespace))
        assert namespace == "audit"
        assert command.startswith("get pod/demo -o jsonpath=")
        assert all(field in command for field in (".metadata.uid", ".metadata.resourceVersion", ".status.containerStatuses"))
        return {"ok": True, "stdout": "demo\naudit\nlive-pod-uid\n100\nnode-a\n" + json.dumps(waiting_status())}

    result = SkillAgent(registry=Registry(), skills_dir=SKILLS, llm_client=model, kubectl_runner=runner).run(request())
    assert result.status == "completed", [(e.get("event"), e.get("summary")) for e in result.events[-6:]]
    assert len(model.calls) == 2
    assert all(options["thinking"] is False for _, options in model.calls)
    assert len(commands) == 1
    assert result.to_dict()["hypothesis_generation"]["llm_used"] is True
    raw = result.skill_runtime_result["skill_runtime"]["runs"][0]
    assert raw["stages"]["diagnose"]["output"]["root_cause_component"] == "demo"
    assert "不存在" in raw["stages"]["diagnose"]["output"]["root_cause"]
    assert result.agent_evidence["agent_evidence_collection"]["commands"][0]["executed"] is True
    assert all("timestamp" in event for event in result.events)


def test_empty_hypothesis_stops_before_any_skill_or_tool():
    model = Model(empty=True)

    def forbidden(*args):
        raise AssertionError("无有效模型假设时禁止调查或用规则回退")

    result = SkillAgent(registry=Registry(), skills_dir=SKILLS, llm_client=model, kubectl_runner=forbidden).run(request())
    assert result.status == "failed"
    assert len(model.calls) == 1
    assert result.skill_runtime_result is None
    assert result.agent_skill_bridge["reason"] == "llm_hypothesis_failed"
    assert not any(e.get("event") == "llm_hypothesis_fallback" for e in result.events)


def test_live_healthy_state_overrides_stale_image_alert_and_skips_diagnosis():
    executor = SkillExecutor.from_dir(SKILLS)

    def runner(tool, arguments, payload):
        return {"success": True, "executed": True, "data": json.dumps([
            {"name": "worker", "state": {"running": {"startedAt": "2026-10-13T00:00:00Z"}}}])}

    run = executor.run("k8s.image_pull_error", {"pod": "demo", "namespace": "audit", "message": "ImagePullBackOff"},
        stages=["detect", "diagnose"], tool_runner=runner).to_dict()
    assert run["stages"]["detect"]["status"] == "skipped"
    assert run["stages"]["diagnose"]["status"] == "skipped"
    assert not run["stages"]["diagnose"].get("output", {}).get("root_cause")


def test_alert_text_without_live_observation_does_not_prove_image_failure():
    run = SkillExecutor.from_dir(SKILLS).run("k8s.image_pull_error",
        {"pod": "demo", "namespace": "audit", "message": "ImagePullBackOff"}, stages=["detect", "diagnose"]).to_dict()
    assert run["stages"]["detect"]["status"] == "skipped"
    assert run["stages"]["diagnose"]["status"] == "skipped"


def test_large_skill_output_preserves_reasoning_json_and_schema():
    class EvidenceModel(Model):
        def json_chat(self, messages, **options):
            self.calls.append((messages, options))
            if "skill_diagnose_stages" in messages[-1]["content"]:
                content = json.loads(messages[-1]["content"])
                assert content["output_schema"]["reasoning_chain"]
                assert len(messages[-1]["content"]) < 30000
                assert "tool_results" not in content["skill_diagnosis"]
                assert "current dependency FAILED" in content["readonly_checks"][0]["sample"]
                return {"root_cause": "Pod/demo 当前依赖响应 FAILED，内部原因未证实。", "confidence": 0.7,
                        "reasoning_chain": "只读日志显示当前依赖返回 FAILED。"}
            return {"hypotheses": [{"description": "依赖异常待验证", "confidence": 0.6}],
                    "skill_calls": [{"skill_id": "generic.evidence_collection", "stages": ["detect", "diagnose"],
                                     "commands": ["logs demo --tail=40"]}]}

    def runner(*args):
        return {"ok": True, "stdout": "current dependency FAILED\n" + "large observation\n" * 5000}

    result = SkillAgent(registry=Registry(), skills_dir=SKILLS, llm_client=EvidenceModel(), kubectl_runner=runner).run(request())
    assert result.status == "completed", [(e.get("event"), e.get("summary")) for e in result.events[-6:]]


@pytest.mark.parametrize("command", [
    "get pods -n other", "get pods -A", "get pods --all-namespaces=true", "delete pod demo",
    "get secrets", "get pods,secrets", "get secrets.v1", "get --raw=/api/v1/secrets",
    "get -f https://example.invalid/resource", "logs demo; delete pod demo",
])
def test_model_supplied_collection_commands_stay_readonly_and_scoped(command):
    def forbidden(*args):
        raise AssertionError("unsafe command reached tool runner")

    run = SkillExecutor.from_dir(SKILLS).run("generic.evidence_collection",
        {"namespace": "audit", "message": "调查当前异常", "investigation_commands": [command]},
        stages=["detect", "diagnose"], tool_runner=forbidden).to_dict()
    assert run["stages"]["diagnose"]["output"]["tool_results"] == []


def test_empty_model_selector_discovers_real_objects_before_concluding():
    commands = []

    def runner(tool, arguments, payload):
        command = arguments["command"]
        commands.append(command)
        assert arguments["namespace"] == "audit"
        if "-l app=guessed" in command:
            sample = "No resources found in audit namespace."
        elif "jsonpath" in command:
            sample = "real-client real-server"
        else:
            assert command.startswith("logs real-")
            sample = '{"component":"real-server","state":"FAILED","code":503}'
        return {"executed": True, "success": True, "data": sample}

    run = SkillExecutor.from_dir(SKILLS).run("generic.evidence_collection",
        {"namespace": "audit", "message": "503", "investigation_commands": ["logs -l app=guessed --tail=40"]},
        stages=["detect", "diagnose"], tool_runner=runner).to_dict()
    assert len(commands) == 4
    assert commands[-1] == "logs real-server --tail=40"
    results = run["stages"]["diagnose"]["output"]["tool_results"]
    assert any('"state":"FAILED"' in item.get("data", "") for item in results)


def test_disabled_memory_never_initializes_or_queries_history(monkeypatch):
    import importlib
    dashboard = importlib.import_module("web_app.app")
    config = dashboard._get_config()
    monkeypatch.setattr(config.memory, "enabled", False)

    def forbidden():
        raise AssertionError("disabled history must not initialize external stores")

    monkeypatch.setattr(dashboard, "_get_fault_store", forbidden)
    monkeypatch.setattr(dashboard, "_get_feedback_store", forbidden)
    result = dashboard._agent_skill_context_provider(request())
    assert result["history_disabled"] is True
    assert result["rules"] == result["faults"] == []


def test_current_ready_state_rejects_stale_probe_root_and_retries_model():
    class ContradictingModel(Model):
        def json_chat(self, messages, **options):
            self.calls.append((messages, options))
            if len(self.calls) == 1:
                return {"hypotheses": [{"description": "应用状态异常待验证", "confidence": 0.6}],
                        "skill_calls": [{"skill_id": "generic.evidence_collection", "stages": ["detect", "diagnose"],
                                         "commands": ["get pods -o wide", "logs demo --tail=40"]}]}
            if len(self.calls) == 2:
                return {"root_cause": "Pod/demo readiness probe 持续失败导致未就绪", "confidence": 0.9}
            assert "当前 Running" in messages[-1]["content"]
            return {"root_cause": "Pod/demo 当前应用状态 FAILED，返回 503；内部触发原因未证实。", "confidence": 0.7,
                    "reasoning_chain": "Pod Ready，旧 readiness 事件不支持当前未就绪；日志表明应用状态异常。"}

    def runner(command, namespace, payload):
        return {"ok": True, "stdout": ("NAME READY STATUS RESTARTS\ndemo 1/1 Running 0" if command.startswith("get")
                                        else '{"state":"FAILED","code":503}')}

    model = ContradictingModel()
    result = SkillAgent(registry=Registry(), skills_dir=SKILLS, llm_client=model, kubectl_runner=runner).run(request())
    assert result.status == "completed"
    assert len(model.calls) == 3
    assert "FAILED" in result.to_dict()["root_cause"]
    assert any(e.get("event") == "llm_reasoning_evidence_retry" for e in result.events)
