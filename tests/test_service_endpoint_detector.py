import copy
import json
import unittest
from types import SimpleNamespace

from agents.detection_agent import DetectionAgent
from agents.service_endpoint_detector import inspect_service_endpoints
from configs.config_loader import _load_detection_config
from tools.base_tool import ToolResult


def fixture():
    service = {'kind': 'Service', 'metadata': {'name': 'station', 'namespace': 'testbed', 'uid': 'service-uid',
                                                'resourceVersion': '2'}, 'spec': {'selector': {'app': 'station'}}}
    sl = {'kind': 'EndpointSlice', 'metadata': {'name': 'station-1', 'namespace': 'testbed', 'uid': 'slice-uid',
           'resourceVersion': '3', 'labels': {'kubernetes.io/service-name': 'station'},
           'ownerReferences': [{'kind': 'Service', 'name': 'station', 'uid': 'service-uid', 'controller': True}]},
          'endpoints': [{'conditions': {'ready': True}}]}
    return service, {'metadata': {}, 'items': [sl]}


class FakeKubectl:
    def __init__(self, service, slices):
        self.service, self.slices = service, slices

    def execute(self, *, command, namespace, timeout):
        if namespace != 'testbed':
            return ToolResult(success=False, error='unexpected namespace')
        return ToolResult(success=True, data=json.dumps(self.service if command.startswith('get service ') else self.slices))


class ServiceEndpointDetectorTests(unittest.TestCase):
    def check(self, service, slices):
        return inspect_service_endpoints(FakeKubectl(service, slices), 'testbed', 'station', 'service-uid')

    def test_healthy_and_empty_endpoints_are_distinct(self):
        service, slices = fixture()
        self.assertEqual(self.check(service, slices)['status'], 'healthy')
        slices['items'][0]['endpoints'] = []
        result = self.check(service, slices)
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['service_uid'], 'service-uid')
        slices['items'][0]['endpoints'] = None
        self.assertEqual(self.check(service, slices)['status'], 'unavailable')

    def test_missing_or_untrusted_evidence_is_unknown(self):
        base_service, base_slices = fixture()
        for case in ('no_slices', 'wrong_service_uid', 'wrong_owner', 'missing_endpoint_array', 'unknown_ready', 'pagination'):
            service, slices = copy.deepcopy(base_service), copy.deepcopy(base_slices)
            if case == 'no_slices': slices['items'] = []
            if case == 'wrong_service_uid': service['metadata']['uid'] = 'other'
            if case == 'wrong_owner': slices['items'][0]['metadata']['ownerReferences'][0]['uid'] = 'other'
            if case == 'missing_endpoint_array': del slices['items'][0]['endpoints']
            if case == 'unknown_ready': slices['items'][0]['endpoints'][0]['conditions'] = {}
            if case == 'pagination': slices['metadata']['continue'] = 'more'
            with self.subTest(case=case):
                self.assertEqual(self.check(service, slices)['status'], 'unknown')

    def test_bad_scope_cannot_be_used_as_command(self):
        service, slices = fixture()
        result = inspect_service_endpoints(FakeKubectl(service, slices), 'testbed;id', 'station', 'service-uid')
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['reason'], 'invalid_watched_identity')
        class FailedKubectl:
            def execute(self, **kwargs):
                raise TimeoutError('unavailable')
        self.assertEqual(inspect_service_endpoints(FailedKubectl(), 'testbed', 'station', 'service-uid')['status'], 'unknown')

    def test_actual_detection_agent_emits_only_for_configured_uid(self):
        service, slices = fixture()
        slices['items'][0]['endpoints'] = []
        agent = object.__new__(DetectionAgent)
        agent.registry = SimpleNamespace(get=lambda name: FakeKubectl(service, slices) if name == 'kubectl' else None)
        agent.watched_service_endpoints = [{'namespace': 'testbed', 'name': 'station', 'uid': 'service-uid'}]
        agent.sources_enabled = {'service_endpoints': True}
        signals = agent.detect('testbed')
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].source, 'service_endpoints')
        self.assertEqual(signals[0].raw_data['service_uid'], 'service-uid')
        agent.watched_service_endpoints[0]['uid'] = 'other'
        self.assertEqual(agent.detect('testbed'), [])
        self.assertEqual(agent.detect('different'), [])

    def test_configuration_is_opt_in(self):
        self.assertFalse(_load_detection_config({}).sources_enabled['service_endpoints'])
        watches = [{'namespace': 'testbed', 'name': 'station', 'uid': 'service-uid'}]
        config = _load_detection_config({'sources_enabled': {'service_endpoints': True}, 'watched_service_endpoints': watches})
        self.assertTrue(config.sources_enabled['service_endpoints'])
        self.assertEqual(config.watched_service_endpoints, watches)


if __name__ == '__main__':
    unittest.main()
