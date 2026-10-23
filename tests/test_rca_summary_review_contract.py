"""Completion and evidence handoff regressions from real frontend RCA runs."""
import asyncio
import json
from types import SimpleNamespace

from agents.event_agent import EventAgent
from agents.metric_agent import MetricAgent
from memory.rca_judge import RCAJudge
from tools.base_tool import ToolResult


def event_agent():
    rows = [{"metadata": {"uid": "event-1"}, "reason": "FailedScheduling",
             "message": "0/4 nodes are available: 4 nodes did not match selector",
             "lastTimestamp": "2026-10-19T11:00:00Z", "count": 7,
             "involvedObject": {"namespace": "shop", "kind": "Pod", "name": "api", "uid": "pod-new"}}]
    tool = SimpleNamespace(execute=lambda **kw: ToolResult(success=True, data={"items": rows}))
    return EventAgent(None, SimpleNamespace(get=lambda name: tool)), rows


def test_live_scheduler_event_reaches_independent_evidence_reviewer():
    agent, _ = event_agent()
    facts = []
    agent._fetch_events('shop', live_pods={('shop', 'api'): 'pod-new'}, evidence_items=facts)
    review = RCAJudge._review_facts({'event_agent': {'evidence_items': facts}})
    assert len(review) == 1 and 'did not match selector' in review[0]['observation']
    assert review[0]['timestamp'] == '2026-10-19T11:00:00Z'
    assert review[0]['raw_ref']['namespace'] == 'shop'
    assert review[0]['raw_ref']['pod_uid'] == 'pod-new'
    assert review[0]['raw_ref']['event_uid'] == 'event-1'


def test_deleted_or_recreated_pod_event_cannot_be_current_evidence():
    agent, _ = event_agent()
    for live in ({}, {('shop', 'api'): 'pod-recreated'}, {('other', 'api'): 'pod-new'}):
        facts = []
        active, stale = agent._fetch_events('shop', live_pods=live, evidence_items=facts)
        assert not active and 'FailedScheduling' in stale and facts == []


def test_failed_live_lookup_does_not_certify_warning_as_current():
    agent, _ = event_agent()
    facts = []
    agent._fetch_events('shop', live_pods=None, evidence_items=facts)
    assert facts == []


def test_event_summary_disables_thinking_and_bounds_verbosity():
    class Model:
        async def async_chat(self, messages, **kwargs):
            self.options, self.messages = kwargs, messages
            return 'No mechanism confirmed.'
    model = Model()
    tool = SimpleNamespace(execute=lambda **kw: ToolResult(success=True, data=json.dumps({'items': []})))
    agent = EventAgent(model, SimpleNamespace(get=lambda _: tool))
    asyncio.run(agent.analyze('Pod/api is Pending', 'shop'))
    assert model.options['thinking'] is False
    assert 'at most 250' in model.messages[0]['content']
    assert 'at most 250' in MetricAgent.SYSTEM_PROMPT


def test_review_json_example_includes_every_required_annotation():
    # Reproduce the real reviewer following the displayed JSON example exactly.
    prompt = RCAJudge.LLM_JUDGE_PROMPT.format(reasoning='observed', root_cause='pending', confidence=.7)
    example = json.loads(prompt[prompt.index('{'):prompt.rindex('}') + 1])
    reviewer = SimpleNamespace(json_chat=lambda *args, **kw: example)
    cfg = SimpleNamespace(memory=SimpleNamespace(judge_threshold=.65))
    facts = {'event_agent': {'evidence_items': [{'source': 'kubectl', 'observation': 'pending'}]}}
    result = RCAJudge(reviewer, cfg).judge('observed', 'pending', .7, evidence=facts)
    assert 'reviewer_failed_or_invalid' not in result['review_blockers']
    assert all(isinstance(example[k], list) for k in (
        'supported_evidence_ids', 'unsupported_claims', 'contradictions'))
