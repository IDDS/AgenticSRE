"""Regression for the remote run whose empty summaries hid live K8s facts."""
from agents.hypothesis_agent import Hypothesis, HypothesisAgent
from orchestrator.collaboration_optimizer import CollaborationOptimizer
from orchestrator.diagnosis_closure import DiagnosisClosureManager


def observations():
    return {"event_agent": {"summary": "", "evidence_items": [{
        "signal_type": "deployment_scaled_to_zero", "service": "probe",
        "source": "kubectl get deployment -o json",
        "observation": "Deployment/probe current desired replicas is 0.",
        "raw_ref": {"namespace": "case-ns", "name": "probe", "desired_replicas": 0},
    }]}}


class CaptureLLM:
    def json_chat(self, messages, **kwargs):
        self.prompt = messages[-1]["content"]
        return {"hypotheses": []}


def test_rerank_receives_live_facts_when_summary_is_empty():
    llm = CaptureLLM()
    HypothesisAgent(llm).rerank([Hypothesis("H1", "selector mismatch")], observations())
    assert "deployment_scaled_to_zero" in llm.prompt
    assert "case-ns" in llm.prompt
    assert '"desired_replicas": 0' in llm.prompt


def test_critic_receives_live_facts_when_summary_is_empty():
    optimizer = object.__new__(CollaborationOptimizer)
    assert "deployment_scaled_to_zero" in optimizer._format_evidence(observations())


def test_closure_receives_live_facts_when_summary_is_empty():
    assert "deployment_scaled_to_zero" in DiagnosisClosureManager()._format_evidence(observations())


def test_observations_survive_long_earlier_summaries():
    from agents.evidence import format_evidence
    data = {"metric_agent": {"summary": "uninformative " * 2000}, **observations()}
    prompt = format_evidence(data, limit=900)
    assert "deployment_scaled_to_zero" in prompt
    assert '"desired_replicas": 0' in prompt
    assert len(prompt) <= 900


def test_summarization_timeout_keeps_current_state(monkeypatch):
    import asyncio
    from agents.event_agent import EventAgent

    class SlowLLM:
        async def async_chat(self, *args, **kwargs):
            await asyncio.sleep(1)

    agent = EventAgent(SlowLLM(), None)
    agent.summary_timeout_s = 0.01
    state = {"deployments": [{"name": "probe", "namespace": "case-ns", "desired_replicas": 0}],
             "summary": "Deployment/probe desired=0"}
    monkeypatch.setattr(agent, "_fetch_workload_state", lambda *a: state)
    monkeypatch.setattr(agent, "_fetch_live_pods", lambda *a: set())
    monkeypatch.setattr(agent, "_fetch_events", lambda *a, **k: ("", ""))
    monkeypatch.setattr(agent, "_fetch_node_status", lambda: "")
    monkeypatch.setattr(agent, "_fetch_problem_pods", lambda *a: [])
    monkeypatch.setattr(agent, "_fetch_network_objects", lambda *a: {})
    result = asyncio.run(agent.analyze("service unreachable", "case-ns"))
    assert result["summary_error"] == "TimeoutError"
    assert result["evidence_items"][0]["raw_ref"]["desired_replicas"] == 0
    assert "desired=0" in result["summary"]


def test_deadline_rejects_late_results_and_stops_new_calls(monkeypatch):
    import time
    import pytest
    from tools.llm_client import DeadlineLLMClient, LLMClient
    calls = []

    def chat(_self, messages, **kwargs):
        calls.append(messages)
        if messages == "slow":
            time.sleep(0.2)
        kwargs['_receipt']['completion_status'] = 'complete'
        return messages

    monkeypatch.setattr(LLMClient, "chat", chat)
    client = object.__new__(DeadlineLLMClient)
    client.deadline = time.monotonic() + 10
    client.call_timeout_s = 0.02
    client.timeout_count = 0
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        client.chat("slow")
    assert time.monotonic() - started < 0.15
    assert client.chat("fresh") == "fresh"
    client.deadline = time.monotonic() - 1
    with pytest.raises(TimeoutError, match="budget exhausted"):
        client.chat("never run")
    assert calls == ["slow", "fresh"]
    assert client.timeout_count == 1
