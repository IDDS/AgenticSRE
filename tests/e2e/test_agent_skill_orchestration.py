from __future__ import annotations

import ast
import pathlib
import sys

from fastapi.testclient import TestClient


ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestrator.skill_agent import SkillAgent, SkillAgentRequest  # noqa: E402
from integrations.hermes_skill_runtime import HERMES_SKILL_TOOL_NAMES  # noqa: E402
from tools.base_tool import ToolRegistry  # noqa: E402
from tools.k8s_tools import KubectlTool  # noqa: E402
import web_app.app as dashboard  # noqa: E402


def test_agent_skill_dry_run_does_not_shorten_llm_timeout() -> None:
    assert dashboard._resolve_agent_skill_llm_timeout(90, None) == 90
    assert dashboard._resolve_agent_skill_llm_timeout(90, 60) == 60


def test_agent_source_has_no_deterministic_runtime_workflow_dependency() -> None:
    source_path = ROOT / "orchestrator" / "skill_agent.py"
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    string_literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }

    assert "skill_runtime_workflow" not in string_literals
    assert "skill_runtime.workflow" not in imported_modules
    assert "self.skill_executor.run(" in source


def test_assistant_public_tools_cannot_delegate_to_deterministic_workflow() -> None:
    app_source = (ROOT / "web_app" / "app.py").read_text(encoding="utf-8")
    frontend_source = (ROOT / "web_app" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    assert HERMES_SKILL_TOOL_NAMES == (
        "agenticsre_skills_list",
        "agenticsre_skill_detail",
        "agenticsre_skill_run",
    )
    assert "- agenticsre_skill_workflow:" not in app_source
    assert "_run_skill_bridge" not in app_source
    assert "skill_runtime_workflow" not in frontend_source


def test_request_scoped_tool_registries_do_not_share_tools() -> None:
    class Tool:
        name = "probe"

    first = ToolRegistry.isolated()
    second = ToolRegistry.isolated()
    first.register(Tool(), "test")

    assert first.get("probe") is not None
    assert second.get("probe") is None
    assert first is not second


def test_kubectl_tool_binds_kubeconfig_and_context(monkeypatch) -> None:
    captured = {}

    class Completed:
        returncode = 0
        stdout = "ok"
        stderr = ""

    monkeypatch.setattr(
        "tools.k8s_tools.subprocess.run",
        lambda command, **kwargs: captured.update({"command": command, **kwargs}) or Completed(),
    )
    result = KubectlTool(
        kubeconfig="/clusters/general-a.yaml",
        context="general-a-admin",
        namespace="production",
    ).execute(command="get pods")

    assert result.success is True
    assert captured["command"] == [
        "kubectl", "--kubeconfig=/clusters/general-a.yaml", "--context=general-a-admin",
        "get", "pods", "-n", "production", "-o", "wide",
    ]
    assert captured["shell"] is False


def test_agent_context_rejects_legacy_provider_and_filters_recent_runs(monkeypatch) -> None:
    class Store:
        def get_historical_context(self, _query):
            return {
                "rules": [
                    {"id": "generic-rule"},
                    {"id": "ai-rule", "cluster_id": "ai-t4"},
                ],
                "faults": [
                    {"id": "general-fault", "cluster_id": "general-a", "fault_type": "service_unavailable"},
                    {"id": "ai-fault", "cluster_id": "ai-t4", "fault_type": "service_unavailable"},
                    {"id": "legacy-unscoped", "fault_type": "service_unavailable"},
                ],
            }

        def resolution_stats(self, **_kwargs):
            raise AssertionError("cluster-scoped context must not use global recovery statistics")

    class FeedbackStore:
        def diagnosis_stats(self, **kwargs):
            return {
                "history_kind": "diagnosis_correctness",
                "cluster_id": kwargs.get("cluster_id", ""),
                "diagnosis_sample_count": 0,
                "diagnosis_correct_count": 0,
                "diagnosis_accuracy": None,
                "diagnosis_accuracy_lower_bound": None,
            }

    old_runs = dashboard._state.get("rca_runs", {})
    dashboard._state["rca_runs"] = {
        "general-run": {
            "id": "general-run",
            "cluster_id": "general-a",
            "namespace": "default",
            "started_at": 2,
            "finished_at": 2,
            "query": "frontend service unavailable",
            "status": "completed",
            "result": {"fault_type": "service_unavailable"},
        },
        "ai-run": {
            "id": "ai-run",
            "cluster_id": "ai-t4",
            "started_at": 3,
            "finished_at": 3,
            "query": "vllm service unavailable",
            "status": "completed",
            "result": {"fault_type": "service_unavailable"},
        },
        "other-namespace-run": {
            "id": "other-namespace-run",
            "cluster_id": "general-a",
            "namespace": "other-namespace",
            "started_at": 4,
            "finished_at": 4,
            "query": "frontend service unavailable ImagePullBackOff",
            "status": "completed",
            "result": {"fault_type": "service_unavailable"},
        },
    }
    monkeypatch.setattr(dashboard, "_get_fault_store", lambda: Store())
    monkeypatch.setattr(dashboard, "_get_feedback_store", lambda: FeedbackStore())
    try:
        context = dashboard._agent_skill_context_provider(SkillAgentRequest(
            query="frontend service unavailable",
            namespace="default",
            skill_workflow={
                "domain": "k8s",
                "payload": {
                    "source": "alert_center",
                    "domain": "k8s",
                    "cluster_id": "general-a",
                    "cluster_kind": "general",
                    "namespace": "default",
                    "fault_type": "service_unavailable",
                    "service": "frontend",
                },
            },
        ))
    finally:
        dashboard._state["rca_runs"] = old_runs

    assert context["faults"] == context["rules"] == []
    assert context['history_retrieval']['error_code'] == 'memory_scope_unsupported'
    assert [item["id"] for item in context["recent_runs"]] == ["general-run"]
    assert context["history_stats"]["history_kind"] == "diagnosis_correctness"
    assert context["history_stats"]["cluster_id"] == "general-a"
    assert context["history_stats"]["diagnosis_sample_count"] == 0


def test_general_alert_agent_skill_rejects_ai_cluster_scope() -> None:
    cfg = dashboard._get_config()
    old_clusters = list(cfg.fault_targets.k8s_clusters)
    cfg.fault_targets.k8s_clusters = [
        {"id": "general-a", "name": "General A", "kind": "general"},
        {"id": "ai-t4", "name": "AI T4", "kind": "ai"},
    ]
    try:
        response = TestClient(dashboard.app).post("/api/rca/run", json={
            "query": "frontend unavailable",
            "namespace": "default",
            "cluster_id": "ai-t4",
            "skill_workflow": {
                "domain": "k8s",
                "payload": {
                    "source": "alert_center",
                    "cluster_id": "ai-t4",
                    "namespace": "default",
                    "fault_type": "service_unavailable",
                },
            },
        })
    finally:
        cfg.fault_targets.k8s_clusters = old_clusters

    assert response.status_code == 409


def test_cluster_scoped_config_binds_kubernetes_and_prometheus_backends() -> None:
    cfg = dashboard._get_config()
    old_clusters = list(cfg.fault_targets.k8s_clusters)
    cfg.fault_targets.k8s_clusters = [{
        "id": "general-a",
        "name": "General A",
        "kind": "general",
        "kubeconfig": "/clusters/general-a.yaml",
        "context": "general-a-admin",
        "default_namespace": "production",
        "prometheus_url": "http://general-prometheus:9090",
    }]
    try:
        scoped = dashboard._cluster_scoped_config("general-a")
    finally:
        cfg.fault_targets.k8s_clusters = old_clusters

    assert scoped.kubernetes.kubeconfig == "/clusters/general-a.yaml"
    assert scoped.kubernetes.context == "general-a-admin"
    assert scoped.kubernetes.namespace == "production"
    assert scoped.observability.prometheus_url == "http://general-prometheus:9090"


def test_general_alert_list_attaches_cluster_scope_and_uses_isolated_registry(monkeypatch) -> None:
    captured = {}

    class Signal:
        def to_dict(self):
            return {
                "signal_id": "sig-1",
                "source": "k8s_event",
                "severity": "warning",
                "title": "FailedScheduling",
                "description": "insufficient cpu",
                "timestamp": 1.0,
                "namespace": "production",
                "service": "frontend",
            }

    class Agent:
        def __init__(self, _llm, registry, _cfg):
            captured["registry"] = registry

        def detect(self, namespace):
            captured["namespace"] = namespace
            return [Signal()]

    class Registry:
        pass

    cfg = dashboard._get_config()
    old_clusters = list(cfg.fault_targets.k8s_clusters)
    cfg.fault_targets.k8s_clusters = [{
        "id": "general-a",
        "name": "General A",
        "kind": "general",
        "kubeconfig": "/clusters/general-a.yaml",
        "default_namespace": "production",
        "prometheus_url": "http://general-prometheus:9090",
    }]
    monkeypatch.setattr(dashboard, "DetectionAgent", Agent)
    monkeypatch.setattr(
        dashboard,
        "build_tool_registry",
        lambda scoped_cfg, **kwargs: (
            captured.update({
                "isolated": kwargs.get("isolated"),
                "kubeconfig": scoped_cfg.kubernetes.kubeconfig,
                "prometheus_url": scoped_cfg.observability.prometheus_url,
            })
            or Registry()
        ),
    )
    monkeypatch.setattr(dashboard, "_record_alert_events", lambda _alerts: None)
    try:
        response = TestClient(dashboard.app).get(
            "/api/alerts/list?cluster_id=general-a&namespace=production&duration=current"
        )
    finally:
        cfg.fault_targets.k8s_clusters = old_clusters

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["cluster_id"] == "general-a"
    assert payload["alerts"][0]["cluster_id"] == "general-a"
    assert payload["alerts"][0]["cluster_kind"] == "general"
    assert captured["isolated"] is True
    assert captured["kubeconfig"] == "/clusters/general-a.yaml"
    assert captured["prometheus_url"] == "http://general-prometheus:9090"


def test_agent_selects_and_invokes_exact_skill_without_runtime_workflow() -> None:
    events = []

    class LLM:
        def json_chat(self, messages, temperature=None, max_tokens=None):
            content = messages[-1]["content"]
            if '"skill_catalog"' in content:
                return {"skill_calls": [{
                    "skill_id": "llm.model_serving_endpoint",
                    "stages": ["detect", "diagnose"],
                    "reason": "Pod Ready 但 Endpoint 为空",
                }]}
            if "reasoning_chain" in content:
                return {
                    "root_cause": "Service selector 未匹配就绪 Pod，因此没有可用 Endpoint",
                    "confidence": 0.92,
                    "evidence": ["Pod Ready", "Endpoint 为空"],
                }
            return {"summary": "直接调查服务发现链路", "selected_command_indexes": [0]}

    class Registry:
        def get(self, name):
            assert name != "skill_runtime_workflow"
            return None

        def execute(self, name, **kwargs):
            raise AssertionError(f"Agent 不应调用确定性 Runtime 工具: {name}")

    result = SkillAgent(
        registry=Registry(),
        skills_dir=ROOT / "skill_runtime" / "skills",
        event_sink=events.append,
        llm_client=LLM(),
    ).run(SkillAgentRequest(
        query="vllm-server Pod Ready but Service has no endpoints",
        namespace="llm-k8s",
        skill_workflow={
            "analysis_phases": ["investigation", "reasoning"],
            "stages": ["detect", "diagnose"],
            "payload": {
                "domain": "llm",
                "namespace": "llm-k8s",
                "service": "vllm-server",
                "observation_signals": [{"signal": "endpoint_count", "value": 0}],
            },
        },
    ))

    assert result.status == "completed"
    runtime = result.skill_runtime_result["skill_runtime"]
    assert runtime["selection_mode"] == "agent_llm_exact_skill_calls"
    assert runtime["primary_skill_id"] == "llm.model_serving_endpoint"
    diagnosis = result.skill_runtime_result["diagnosis"]
    assert diagnosis["confidence_explanation"]["method"] == "evidence_calibrated_v2"
    assert diagnosis["confidence"] == diagnosis["confidence_explanation"]["calibrated_confidence"]
    assert any(event.get("event") == "confidence_calibration" for event in events)
    assert any(
        event.get("event") == "tool_call"
        and event.get("tool") == "agenticsre_skill_run"
        and event.get("skill_id") == "llm.model_serving_endpoint"
        for event in events
    )
    assert not any(event.get("tool") == "skill_runtime_workflow" for event in events)
    assert not any(event.get("name") in {"HYPOTHESIS_GENERATION", "VERIFICATION_PLANNING"} for event in events)


def test_general_k8s_agent_catalog_excludes_llm_skills() -> None:
    observed_domains = set()

    class LLM:
        def json_chat(self, messages, temperature=None, max_tokens=None):
            content = messages[-1]["content"]
            if '"skill_catalog"' in content:
                import json
                payload = json.loads(content)
                observed_domains.update(item["domain"] for item in payload["skill_catalog"])
                return {"skill_calls": [{
                    "skill_id": "k8s.service_unavailable",
                    "stages": ["detect", "diagnose"],
                    "reason": "service has no endpoint",
                }]}
            if "reasoning_chain" in content:
                return {"root_cause": "Service 没有可用 Endpoint", "confidence": 0.9}
            return {"summary": "check service endpoints", "selected_command_indexes": [0]}

    class Registry:
        def get(self, _name):
            return None

    result = SkillAgent(
        registry=Registry(),
        skills_dir=ROOT / "skill_runtime" / "skills",
        llm_client=LLM(),
    ).run(SkillAgentRequest(
        query="frontend service unavailable",
        namespace="default",
        skill_workflow={
            "analysis_phases": ["investigation", "reasoning"],
            "stages": ["detect", "diagnose"],
            "payload": {
                "source": "alert_center",
                "domain": "k8s",
                "cluster_id": "general-a",
                "namespace": "default",
                "service": "frontend",
                "fault_type": "service_unavailable",
            },
        },
    ))

    assert result.status == "completed"
    assert observed_domains <= {"k8s", "generic", "tool"}
    assert "llm" not in observed_domains
    assert result.skill_runtime_result["skill_runtime"]["primary_skill_id"] == "k8s.service_unavailable"


def test_agent_retries_final_reasoning_that_uses_experiment_metadata() -> None:
    events = []

    class LLM:
        def json_chat(self, messages, temperature=None, max_tokens=None):
            content = messages[-1]["content"]
            if '"skill_catalog"' in content:
                return {"skill_calls": [{"skill_id": "llm.business_slo", "stages": ["detect", "diagnose"]}]}
            if "上述结论不合格" in content:
                return {
                    "root_cause": "长上下文请求增多导致运行请求积压和端到端延迟升高，KV Cache 指标未达资源耗尽门槛。",
                    "confidence": 0.78,
                }
            if "reasoning_chain" in content:
                return {
                    "root_cause": "fault_experiment_signal 证明实验注入造成性能退化",
                    "confidence": 0.95,
                }
            return {"summary": "直接调查性能指标", "selected_command_indexes": [0]}

    class Registry:
        def get(self, _name):
            return None

    result = SkillAgent(
        registry=Registry(),
        skills_dir=ROOT / "skill_runtime" / "skills",
        event_sink=events.append,
        llm_client=LLM(),
    ).run(SkillAgentRequest(
        query="inference latency increased",
        namespace="llm-k8s",
        skill_workflow={
            "analysis_phases": ["investigation", "reasoning"],
            "stages": ["detect", "diagnose"],
            "payload": {
                "domain": "llm",
                "namespace": "llm-k8s",
                "observation_signals": [
                    {"signal": "e2e_p95_seconds", "value": 38.8},
                    {"signal": "kv_cache_usage", "value": 0.39},
                ],
            },
        },
    ))

    diagnosis = result.skill_runtime_result["diagnosis"]
    assert result.status == "completed"
    assert "fault_experiment_signal" not in diagnosis["root_cause"]
    assert "实验注入" not in diagnosis["root_cause"]
    assert "KV Cache 指标未达" in diagnosis["root_cause"]
    assert any(event.get("event") == "llm_reasoning_retry" for event in events)


def test_agent_hypotheses_never_expose_experiment_metadata_as_fault_type() -> None:
    class Registry:
        def get(self, _name):
            return None

    agent = SkillAgent(
        registry=Registry(),
        skills_dir=ROOT / "skill_runtime" / "skills",
        llm_client=None,
    )
    agent.require_llm = False  # Explicit offline rule ablation, not the runtime path.
    ranked = agent.generate_hypotheses(SkillAgentRequest(
        query="model serving anomaly",
        namespace="llm-k8s",
        skill_workflow={
            "payload": {"fault_type": "fault_experiment_signal", "service": "model-server"},
        },
    ), include_context=False)

    assert ranked[0]["fault_type"] == "observable_anomaly"
    assert "fault_experiment_signal" not in ranked[0]["description"]
    assert agent._parse_confidence_value("中等") == 0.6
    assert agent._parse_confidence_value("88%") == 0.88
    assert agent._unsupported_specificity("疑似 max_num_seqs 配置过低")
    assert not agent._unsupported_specificity("观测到 KV Cache 高水位和等待队列积压")


def test_agent_retries_reasoning_that_contradicts_ready_replicas() -> None:
    events = []

    class LLM:
        def json_chat(self, messages, temperature=None, max_tokens=None):
            content = messages[-1]["content"]
            if '"skill_catalog"' in content:
                return {"skill_calls": [{
                    "skill_id": "llm.model_serving_endpoint",
                    "stages": ["detect", "diagnose"],
                }]}
            if "证据一致性问题" in content:
                return {
                    "root_cause": "Service selector 与 Pod 标签不匹配，Pod 已就绪但 Endpoint 为空",
                    "confidence": 0.9,
                }
            if "reasoning_chain" in content:
                return {
                    "root_cause": "readiness probe 持续失败导致 Pod 未达到就绪",
                    "confidence": 0.9,
                }
            return {"summary": "调查 Service 与 Endpoint", "selected_command_indexes": [0]}

    class Registry:
        def get(self, _name):
            return None

    result = SkillAgent(
        registry=Registry(),
        skills_dir=ROOT / "skill_runtime" / "skills",
        event_sink=events.append,
        llm_client=LLM(),
    ).run(SkillAgentRequest(
        query="Service has no endpoints",
        namespace="llm-k8s",
        skill_workflow={
            "analysis_phases": ["investigation", "reasoning"],
            "stages": ["detect", "diagnose"],
            "payload": {
                "domain": "llm",
                "namespace": "llm-k8s",
                "service": "vllm-server",
                "observation": {
                    "deployment_desired_replicas": 1,
                    "deployment_ready_replicas": 1,
                    "endpoint_count": 0,
                    "pod_restart_count": 0,
                },
                "observation_signals": [{"signal": "endpoint_count", "value": 0, "severity": "critical"}],
            },
        },
    ))

    root = result.skill_runtime_result["diagnosis"]["root_cause"]
    assert "selector" in root
    assert "readiness probe" not in root
    assert any(event.get("event") == "llm_reasoning_evidence_retry" for event in events)
