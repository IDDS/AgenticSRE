import copy
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from agents.endpoint_followup import observe_related_service_endpoints
from agents.event_agent import EventAgent


class EndpointFollowupTests(unittest.TestCase):
    def setUp(self):
        self.target = {'namespace': 'shop', 'name': 'api', 'uid': 'dep-uid', 'resource_version': '11'}
        workload = {'kind': 'Deployment', 'topology_fields_complete': True,
                    'metadata': {'namespace': 'shop', 'name': 'api', 'uid': 'dep-uid', 'resourceVersion': '11'},
                    'spec': {'replicas': 0, 'template': {'metadata': {'labels': {'app': 'api'}}}}}
        service = {'kind': 'Service', 'topology_fields_complete': True,
                   'metadata': {'namespace': 'shop', 'name': 'gateway', 'uid': 'svc-uid', 'resourceVersion': '21'},
                   'spec': {'selector': {'app': 'api'}}}
        self.state = {'topology_collection': {'workload_resources': {'status': 'complete'}},
                      'topology_resources': [workload, service]}

    def test_matching_selector_produces_uid_bound_but_noncausal_observation(self):
        endpoint = {'status': 'unavailable', 'service_resource_version': '21',
                    'ready_endpoints': 0, 'slice_uids': ['slice-uid'],
                    'slice_versions': [{'uid': 'slice-uid', 'resource_version': '33'}]}
        with patch('agents.endpoint_followup.inspect_service_endpoints', return_value=endpoint) as inspect:
            out = observe_related_service_endpoints(object(), 'shop', self.state, [self.target])
        self.assertEqual(out['status'], 'complete')
        raw = out['evidence_items'][0]['raw_ref']
        self.assertEqual((raw['name'], raw['uid']), ('gateway', 'svc-uid'))
        self.assertEqual(raw['related_workload']['uid'], 'dep-uid')
        self.assertEqual(raw['association'], 'selector_matches_workload_template_not_ownership')
        self.assertFalse(raw['atomic_snapshot'])
        self.assertIn('not proof of ownership or causation', out['evidence_items'][0]['observation'])
        inspect.assert_called_once()

    def test_failed_endpoint_read_does_not_prove_empty(self):
        with patch('agents.endpoint_followup.inspect_service_endpoints', return_value={'status': 'unknown'}):
            out = observe_related_service_endpoints(object(), 'shop', self.state, [self.target])
        self.assertEqual(out['status'], 'partial')
        self.assertEqual(out['evidence_items'], [])

    def test_changed_service_version_does_not_carry_stale_association(self):
        endpoint = {'status': 'unavailable', 'service_resource_version': '22', 'ready_endpoints': 0,
                    'slice_uids': ['slice-uid'], 'slice_versions': [{'uid': 'slice-uid', 'resource_version': '33'}]}
        with patch('agents.endpoint_followup.inspect_service_endpoints', return_value=endpoint):
            out = observe_related_service_endpoints(object(), 'shop', self.state, [self.target])
        self.assertEqual(out['status'], 'partial')
        self.assertEqual(out['evidence_items'], [])

    def test_incomplete_or_ambiguous_scope_never_queries(self):
        variants = []
        v = copy.deepcopy(self.state); v['topology_collection']['workload_resources']['status'] = 'partial'; variants.append(v)
        v = copy.deepcopy(self.state); v['topology_resources'][1]['metadata'].pop('uid'); variants.append(v)
        v = copy.deepcopy(self.state); v['topology_resources'][1]['spec']['selector'] = {'app': 'different'}; variants.append(v)
        v = copy.deepcopy(self.state); v['topology_resources'].append(copy.deepcopy(v['topology_resources'][1])); variants.append(v)
        with patch('agents.endpoint_followup.inspect_service_endpoints') as inspect:
            for state in variants:
                self.assertEqual(observe_related_service_endpoints(object(), 'shop', state, [self.target])['status'], 'unknown')
            self.assertEqual(observe_related_service_endpoints(object(), 'shop', self.state, [self.target, self.target])['status'], 'unknown')
        inspect.assert_not_called()


class EndpointFollowupIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_followup_is_near_workload_evidence_in_event_agent(self):
        fixture = EndpointFollowupTests()
        fixture.setUp()
        target = {**fixture.target, 'desired_replicas': 0, 'ready_replicas': 0}
        state = {**fixture.state, 'deployments': [target]}
        tool = SimpleNamespace(execute=lambda **kwargs: SimpleNamespace(success=True,
                               data={'kind': 'List', 'metadata': {}, 'items': []}))
        agent = EventAgent(None, SimpleNamespace(get=lambda _: tool))
        agent.summarize = False
        agent._fetch_workload_state = lambda _: state
        agent._fetch_live_pods = lambda _: {}
        agent._fetch_events = lambda *args, **kwargs: ('', '')
        agent._fetch_node_status = lambda: ''
        agent._fetch_problem_pods = lambda _: []
        agent._fetch_network_objects = lambda *args: {}
        endpoint = {'status': 'unavailable', 'service_resource_version': '21', 'ready_endpoints': 0,
                    'slice_uids': ['slice-uid'], 'slice_versions': [{'uid': 'slice-uid', 'resource_version': '33'}]}
        with patch('agents.endpoint_followup.inspect_service_endpoints', return_value=endpoint):
            result = await agent.analyze('investigate', 'shop')
        self.assertEqual(result['endpoint_followup']['status'], 'complete')
        self.assertEqual([x['signal_type'] for x in result['evidence_items'][:3]], [
            'deployment_scaled_to_zero', 'deployment_hpa_configuration_observed', 'service_endpoint_slice_followup'])


if __name__ == '__main__':
    unittest.main()
