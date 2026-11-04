import unittest
from types import SimpleNamespace

from agents.planning_agent import PlanningAgent
from orchestrator.rca_engine import _apply_model_completion_gate, _planned_evidence_agents


class Model:
    def __init__(self, result):
        self.result = result
    def json_chat(self, messages, **kwargs):
        self.messages = messages
        return self.result


class PlanningOutputContractTests(unittest.TestCase):
    def plan(self, result):
        agent = PlanningAgent(Model(result), SimpleNamespace(list_tools=lambda: []))
        agent.bounded_plan = True
        return agent.generate_plan([], 'incident')

    def test_other_paradigms_keep_broader_plan_contract(self):
        steps = [{'agent': 'event_agent', 'action': 'inspect'}] * 3
        model = Model({'plan': steps})
        result = PlanningAgent(model, SimpleNamespace(list_tools=lambda: [])).generate_plan([], 'incident')
        self.assertEqual(result['plan'], steps)
        self.assertNotIn('no more than two plan steps', model.messages[0]['content'])
        self.assertIn('specific command or query', model.messages[0]['content'])

    def test_two_bounded_agent_steps_remain_dispatchable(self):
        raw = {'plan': [{'agent': 'event_agent', 'action': 'Inspect workload and endpoints', 'priority': 'high'},
                        {'agent': 'log_agent', 'action': 'Inspect application errors', 'priority': 'medium'}]}
        result = self.plan(raw)
        self.assertEqual(result['planning_status'], 'complete')
        self.assertEqual(_planned_evidence_agents(result), ['event_agent', 'log_agent'])

    def test_invalid_or_untrusted_plan_has_explicit_fallback(self):
        invalid = [{'parse_error': True}, {'plan': []},
                   {'plan': [{'agent': 'kubectl', 'action': 'execute'}]},
                   {'plan': [{'agent': 'event_agent', 'action': ''}]},
                   {'plan': [{'agent': 'event_agent', 'action': 'inspect'}] * 3}]
        for value in invalid:
            with self.subTest(value=value):
                result = self.plan(value)
                self.assertEqual(result['planning_status'], 'fallback')
                self.assertTrue(_planned_evidence_agents(result))
                self.assertNotIn('raw_response', result)

    def test_failed_plan_stage_forces_review_even_if_later_judge_passes(self):
        report, judge, incomplete = _apply_model_completion_gate(
            {'confidence': .99}, {'needs_review': False, 'review_blockers': []},
            SimpleNamespace(incomplete_responses=[], timeout_count=0, failed_calls=[]),
            failed_stages=[{'stage': 'investigation_plan', 'code': 'model_plan_unavailable_or_invalid'}])
        self.assertTrue(incomplete)
        self.assertTrue(judge['needs_review'])
        self.assertIn('incomplete_model_execution', judge['review_blockers'])
        self.assertLessEqual(report['confidence'], .65)


if __name__ == '__main__':
    unittest.main()
