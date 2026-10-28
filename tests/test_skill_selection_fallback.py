"""A failed specialist may be followed by actual collection, never invented diagnosis."""
from pathlib import Path

import pytest

from orchestrator.skill_agent import SkillAgent, SkillAgentRequest, _generic_followup_with_observations
from tools.base_tool import ToolRegistry


ROOT = Path(__file__).resolve().parents[1]


def run_record(skill, status='ok', observations=None):
    return {'run_id': skill + '-run', 'skill': {'id': skill}, 'stages': {
        'diagnose': {'status': status, 'output': {'root_cause': 'evidence still requires diagnosis',
            'tool_results': observations or []}}}, 'trace': []}


@pytest.mark.parametrize('observation,accepted', [
    ({'executed': True, 'success': True, 'data': 'observed'}, True),
    ({'executed': False, 'success': True, 'data': 'planned'}, False),
    ({'executed': True, 'success': False, 'data': 'error'}, False),
    ({'executed': True, 'success': True, 'data': 'observed', 'outcome_unknown': True}, False),
    ({'executed': True, 'success': True, 'data': ''}, False),
    ({'executed': True, 'success': True, 'data': 'No resources found'}, False),
])
def test_collection_requires_known_actual_nonempty_observation(observation, accepted):
    assert _generic_followup_with_observations(run_record('generic.evidence_collection',
        observations=[observation])) is accepted


@pytest.mark.parametrize('specialist_status,observed,expected', [
    ('not_matched', True, 'generic.evidence_collection'),
    ('not_matched', False, None),
    ('ok', True, 'k8s.service_unavailable'),
])
def test_selected_generic_can_continue_after_unmatched_specialist(monkeypatch, specialist_status, observed, expected):
    agent = SkillAgent(registry=ToolRegistry(), skills_dir=ROOT / 'skill_runtime' / 'skills')
    agent._skill_owned_investigation = True
    agent._last_decision = {'skill_calls': [
        {'skill_id': 'k8s.service_unavailable', 'stages': ['detect', 'diagnose']},
        {'skill_id': 'generic.evidence_collection', 'stages': ['detect', 'diagnose']}]}
    observation = {'executed': True, 'success': True, 'data': 'observed'} if observed else {}
    records = {'k8s.service_unavailable': run_record('k8s.service_unavailable', specialist_status),
               'generic.evidence_collection': run_record('generic.evidence_collection', observations=[observation])}
    class Run:
        def __init__(self, record): self.record = record
        def to_dict(self): return self.record
    monkeypatch.setattr(agent.skill_executor, 'run', lambda skill, *args, **kwargs: Run(records[skill]))
    request = SkillAgentRequest(query='investigate service', namespace='default', skill_workflow={'payload': {}})
    evidence = {'agent_evidence_collection': {'commands': [{'executed': True, 'success': True}]}}
    result = agent.select_and_invoke_skills(request, evidence=evidence, hypotheses=[{'id': 'H1'}],
        stages=['detect', 'diagnose'], dry_run=True)
    if expected is None:
        assert result is None
        assert any(e.get('event') == 'skill_not_confirmed' for e in agent.events)
    else:
        assert result['skill_runtime']['primary_skill_id'] == expected
        assert len(result['skill_runtime']['runs']) == 2
        assert any(e.get('event') == 'skill_collection_fallback' for e in agent.events) == (specialist_status != 'ok')
