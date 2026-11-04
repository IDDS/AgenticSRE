import copy
import unittest
from types import SimpleNamespace

from agents.hpa_evidence import observe_hpa_targets
from agents.event_agent import EventAgent


class Tool:
    def __init__(self, data=None, success=True):
        self.data, self.success, self.calls = data, success, []
    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(success=self.success, data=self.data)


class HpaEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.target = {'namespace': 'shop', 'name': 'api', 'uid': 'deploy-uid', 'resource_version': '12'}
        self.empty = {'kind': 'List', 'metadata': {}, 'items': []}

    def test_empty_complete_list_is_current_configuration_only(self):
        tool = Tool(self.empty)
        result = observe_hpa_targets(tool, 'shop', [self.target])
        self.assertEqual(result['status'], 'complete')
        raw = result['evidence_items'][0]['raw_ref']
        self.assertEqual(raw['matching_hpas'], [])
        self.assertEqual(raw['hpa_target_reference_binding'], 'name_only')
        self.assertIn('does not establish historical', result['evidence_items'][0]['observation'])
        self.assertEqual(tool.calls[0]['namespace'], 'shop')

    def test_matching_and_unrelated_hpas_are_distinguished(self):
        hpa = {'kind': 'HorizontalPodAutoscaler', 'metadata': {'namespace': 'shop', 'name': 'scale',
               'uid': 'hpa-uid', 'resourceVersion': '33'}, 'spec': {'minReplicas': 1, 'maxReplicas': 5,
               'scaleTargetRef': {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'name': 'api'}}}
        other = copy.deepcopy(hpa)
        other['metadata'].update(name='other', uid='other-uid')
        other['spec']['scaleTargetRef']['name'] = 'other'
        result = observe_hpa_targets(Tool({**self.empty, 'items': [hpa, other]}), 'shop', [self.target])
        links = result['evidence_items'][0]['raw_ref']['matching_hpas']
        self.assertEqual([x['uid'] for x in links], ['hpa-uid'])
        hpa['spec']['scaleTargetRef']['apiVersion'] = 'extensions/v1beta1'
        self.assertEqual(observe_hpa_targets(Tool({**self.empty, 'items': [hpa]}), 'shop', [self.target])['status'], 'unknown')

    def test_failure_partial_or_malformed_never_proves_absence(self):
        for tool in (Tool(success=False), Tool({'kind': 'List', 'items': [], 'metadata': {'continue': 'next'}}),
                     Tool({'kind': 'List', 'items': [None], 'metadata': {}}), Tool({})):
            with self.subTest(data=tool.data):
                result = observe_hpa_targets(tool, 'shop', [self.target])
                self.assertEqual(result['status'], 'unknown')
                self.assertEqual(result['evidence_items'], [])

    def test_missing_or_ambiguous_identity_never_queries(self):
        tool = Tool(self.empty)
        self.assertEqual(observe_hpa_targets(tool, 'shop', [{**self.target, 'uid': ''}])['status'], 'unknown')
        self.assertEqual(observe_hpa_targets(tool, 'shop', [self.target, {**self.target, 'uid': 'other'}])['status'], 'unknown')
        self.assertEqual(tool.calls, [])


class HpaEventAgentIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_replica_deployment_reaches_event_evidence(self):
        tool = Tool({'kind': 'List', 'metadata': {}, 'items': []})
        agent = EventAgent(None, SimpleNamespace(get=lambda _: tool))
        agent.summarize = False
        deployment = {'namespace': 'shop', 'name': 'api', 'uid': 'deploy-uid',
                      'resource_version': '12', 'desired_replicas': 0, 'ready_replicas': 0}
        resource = {'kind': 'Deployment', 'topology_fields_complete': True, 'metadata': {'namespace': 'shop', 'name': 'api',
                    'uid': 'deploy-uid', 'resourceVersion': '12'}, 'spec': {'replicas': 0}}
        agent._fetch_workload_state = lambda _: {'deployments': [deployment], 'topology_resources': [resource]}
        agent._fetch_live_pods = lambda _: {}
        agent._fetch_events = lambda *args, **kwargs: ('', '')
        agent._fetch_node_status = lambda: ''
        agent._fetch_problem_pods = lambda _: []
        agent._fetch_network_objects = lambda *args: {}
        result = await agent.analyze('investigate', 'shop')
        self.assertEqual(result['hpa_observation']['status'], 'complete')
        self.assertTrue(any(x['signal_type'] == 'deployment_hpa_configuration_observed'
                            for x in result['evidence_items']))
        self.assertEqual([x['signal_type'] for x in result['evidence_items'][:2]],
                         ['deployment_scaled_to_zero', 'deployment_hpa_configuration_observed'])


if __name__ == '__main__':
    unittest.main()
