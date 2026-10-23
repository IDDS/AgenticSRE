import json
from types import SimpleNamespace

import pytest

from agents.event_agent import EventAgent
from memory.rca_judge import RCAJudge
from orchestrator.collaboration_optimizer import root_scoped_evidence
from tools.base_tool import ToolResult


def collect(selector):
    pods = [{'metadata': {'name': name, 'namespace': 'shop', 'uid': name + '-uid'},
             'spec': {'nodeSelector': value}, 'status': {'phase': 'Pending'}}
            for name, value in [('target', selector), ('peer', {'other': 'label'})]]
    tool = SimpleNamespace(execute=lambda **kwargs: ToolResult(success=True, data=json.dumps({'items': pods})))
    agent = EventAgent(None, SimpleNamespace(get=lambda _: tool))
    problems = agent._fetch_problem_pods('shop')
    return agent._build_current_state_evidence_items({}, problems)


def test_declared_selector_reaches_targeted_reasoning_and_independent_reviewer():
    facts = collect({'agenticsre-general-demo': 'no-such-node'})
    scoped = root_scoped_evidence({'root_entity': {'namespace': 'shop', 'kind': 'Pod', 'name': 'target'}},
                                  {'event_agent': {'evidence_items': facts}})
    review = RCAJudge._review_facts(scoped)
    constraints = [f for f in review if f['signal_type'] == 'pod_scheduling_constraints']
    assert len(constraints) == 1
    fact = constraints[0]
    assert 'no-such-node' in fact['observation'] and 'other' not in fact['observation']
    assert fact['raw_ref']['pod_uid'] == 'target-uid'
    assert fact['raw_ref']['node_selector'] == {'agenticsre-general-demo': 'no-such-node'}
    assert fact['source'] == 'kubectl get pods -o json'


@pytest.mark.parametrize('selector', [{}, None, 'not-an-object'])
def test_absent_or_invalid_selector_does_not_become_a_configuration_fact(selector):
    facts = collect(selector)
    assert not any(f['signal_type'] == 'pod_scheduling_constraints' and f['service'] == 'target' for f in facts)
