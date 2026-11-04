import unittest
from types import SimpleNamespace

from agents.event_agent import EventAgent
from tools.base_tool import ToolResult


class DeploymentReplicaEvidenceTests(unittest.TestCase):
    def collect(self, spec):
        resource = {'kind': 'Deployment', 'metadata': {'namespace': 'shop', 'name': 'api',
                    'uid': 'uid-a', 'resourceVersion': '7'}, 'spec': spec, 'status': {}}
        tool = SimpleNamespace(execute=lambda **kwargs: ToolResult(success=True, data={'items': [resource], 'metadata': {}}))
        agent = EventAgent(None, SimpleNamespace(get=lambda _: tool))
        state = agent._fetch_workload_state('shop')
        return state, agent._build_current_state_evidence_items(state, [])

    def test_explicit_zero_is_observed_and_identifies_source_field(self):
        state, facts = self.collect({'replicas': 0})
        record = state['deployments'][0]
        self.assertEqual(record['desired_replicas'], 0)
        self.assertEqual(record['desired_replicas_source'], 'spec.replicas')
        self.assertEqual(facts[0]['signal_type'], 'deployment_scaled_to_zero')
        self.assertIn('spec.replicas is 0', facts[0]['observation'])

    def test_missing_or_malformed_replicas_never_becomes_zero_evidence(self):
        for spec in ({}, {'replicas': None}, {'replicas': False}, {'replicas': '0'}, {'replicas': -1}):
            with self.subTest(spec=spec):
                state, facts = self.collect(spec)
                self.assertIsNone(state['deployments'][0]['desired_replicas'])
                self.assertEqual(state['deployments'][0]['desired_replicas_source'], 'unknown')
                self.assertFalse(any(item['signal_type'] == 'deployment_scaled_to_zero' for item in facts))


if __name__ == '__main__':
    unittest.main()
