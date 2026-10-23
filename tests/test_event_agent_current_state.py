import asyncio
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.event_agent import EventAgent
from tools.base_tool import ToolResult


def test_failed_job_container_exit_is_retained_as_structured_evidence():
    from types import SimpleNamespace
    payload = {'items': [{'metadata': {'name': 'job-pod', 'namespace': 'shop'}, 'status': {
        'phase': 'Failed', 'containerStatuses': [
            {'name': 'probe', 'state': {'terminated': {'exitCode': 7, 'reason': 'Error'}}},
            {'name': 'success', 'state': {'terminated': {'exitCode': 0, 'reason': 'Completed'}}}]}}]}
    registry = SimpleNamespace(get=lambda name: SimpleNamespace(execute=lambda **kw: ToolResult(success=True, data=payload)))
    agent = EventAgent(None, registry)
    pods = agent._fetch_problem_pods('shop')
    assert len(pods) == 1 and pods[0]['exit_code'] == 7
    evidence = agent._build_current_state_evidence_items({}, pods)
    assert evidence[0]['signal_type'] == 'container_terminated'
    assert 'exit code 7' in evidence[0]['observation']
    assert evidence[0]['raw_ref']['container'] == 'probe'


class _LLM:
    def __init__(self) -> None:
        self.prompt = ""

    async def async_chat(self, messages, max_tokens=None, **kwargs):
        self.prompt = messages[-1]["content"]
        return "当前 Deployment 副本数为 0，Service 没有可用 Endpoint。"


class _Kubectl:
    def execute(self, command, namespace=""):
        if command.startswith("get events"):
            return ToolResult(success=True, data=json.dumps({
                "items": [
                    {
                        "type": "Warning",
                        "reason": "Failed",
                        "message": "Failed to pull image nginx:1.27-alpine",
                        "lastTimestamp": "2026-08-06T02:00:00Z",
                        "involvedObject": {
                            "kind": "Pod",
                            "name": "frontend-deleted-pod",
                            "namespace": "diag-test",
                        },
                    }
                ]
            }))
        if command == "get deployment,service,endpoints -o json":
            return ToolResult(success=True, data=json.dumps({
                "items": [
                    {
                        "kind": "Deployment",
                        "metadata": {"name": "frontend", "namespace": "diag-test"},
                        "spec": {"replicas": 0},
                        "status": {},
                    },
                    {
                        "kind": "Service",
                        "metadata": {"name": "frontend", "namespace": "diag-test"},
                        "spec": {"selector": {"app": "frontend"}},
                    },
                    {
                        "kind": "Endpoints",
                        "metadata": {"name": "frontend", "namespace": "diag-test"},
                        "subsets": [],
                    },
                ]
            }))
        if command == "get nodes -o wide":
            return ToolResult(success=True, data="node-a Ready")
        return ToolResult(success=False, error=f"unexpected command: {command}")


class _K8sResources:
    def execute(self, **kwargs):
        return ToolResult(success=True, data=json.dumps({
            "items": [
                {
                    "metadata": {"name": "probe-client", "namespace": "diag-test"},
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [{"state": {"running": {}}, "restartCount": 0}],
                    },
                }
            ]
        }))


class _Registry:
    def __init__(self) -> None:
        self.tools = {"kubectl": _Kubectl(), "k8s_resources": _K8sResources()}

    def get(self, name):
        return self.tools.get(name)


def test_event_agent_prefers_current_scale_zero_state_over_deleted_pod_warning() -> None:
    llm = _LLM()
    result = asyncio.run(EventAgent(llm, _Registry()).analyze(
        "Service frontend cannot be reached",
        "diag-test",
    ))

    assert result["warning_events"] == ""
    assert "frontend-deleted-pod" in result["stale_warning_events"]
    assert result["problem_pods"] == []
    assert result["workload_state"]["deployments"][0]["desired_replicas"] == 0
    signal_types = {item["signal_type"] for item in result["evidence_items"]}
    assert signal_types == {"deployment_scaled_to_zero", "service_endpoints_empty"}
    assert "desired=0" in llm.prompt
    assert "do not use as the current root cause" in llm.prompt


def test_event_agent_keeps_warning_for_current_image_pull_pod() -> None:
    agent = EventAgent(_LLM(), _Registry())
    active, stale = agent._fetch_events(
        "diag-test",
        live_pods={("diag-test", "frontend-deleted-pod")},
    )

    assert "Failed to pull image" in active
    assert stale == ""
