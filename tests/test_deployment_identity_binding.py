import copy
import datetime
import unittest

from orchestrator.diagnosis_closure import bind_deployment_identity


class DeploymentIdentityBindingTests(unittest.TestCase):
    def setUp(self):
        self.report = {'root_entity': {'namespace': 'shop', 'kind': 'Deployment', 'name': 'api'},
                       'diagnosis_status': 'requires_review'}
        self.raw = {'kind': 'Deployment', 'projection': 'resource-topology/v1',
                    'topology_fields_complete': True,
                    'metadata': {'namespace': 'shop', 'name': 'api', 'uid': 'uid-a', 'resourceVersion': '17'},
                    'observed_at': datetime.datetime.fromtimestamp(101, datetime.timezone.utc).isoformat()}
        self.fact = {'signal_type': 'resource_topology_observed',
                     'source': 'kubectl get deployment,service,endpoints -o json', 'raw_ref': self.raw}
        self.payload = {'evidence_items': [self.fact], 'collection_errors': [],
                        'workload_state': {'topology_collection': {'workload_resources': {'status': 'complete'}}}}
        self.evidence = {'event_agent': self.payload}

    def bind(self, report=None, evidence=None, **kwargs):
        opts = {'cluster_id': 'east', 'namespace': 'shop', 'run_started_at': 100, 'now': 110}
        opts.update(kwargs)
        return bind_deployment_identity(report or self.report, evidence if evidence is not None else self.evidence, **opts)

    def test_unique_current_observation_binds_identity_without_approving_diagnosis(self):
        result = self.bind()
        self.assertEqual(result['root_entity']['uid'], 'uid-a')
        self.assertEqual(result['root_entity']['resource_version'], '17')
        self.assertEqual(result['root_entity']['cluster_id'], 'east')
        self.assertTrue(result['root_entity_validation']['observed'])
        self.assertEqual(result['diagnosis_status'], 'requires_review')
        self.assertNotIn('uid', self.report['root_entity'])

    def test_scope_collection_time_and_provenance_must_be_complete(self):
        cases = [dict(cluster_id=''), dict(namespace='other'), dict(run_started_at=102), dict(now=100)]
        for args in cases:
            with self.subTest(args=args):
                self.assertFalse(self.bind(**args)['root_entity_validation']['observed'])
        for mutate in ('partial', 'error', 'source', 'delete', 'missing_version', 'naive_time', 'other_cluster'):
            with self.subTest(mutate=mutate):
                evidence = copy.deepcopy(self.evidence)
                p = evidence['event_agent']; raw = p['evidence_items'][0]['raw_ref']
                if mutate == 'partial': p['workload_state']['topology_collection']['workload_resources']['status'] = 'partial'
                if mutate == 'error': p['collection_errors'] = [{'error_code': 'failed'}]
                if mutate == 'source': p['evidence_items'][0]['source'] = 'model summary'
                if mutate == 'delete': raw['metadata']['deletionTimestamp'] = '2026-01-01T00:00:00Z'
                if mutate == 'missing_version': raw['metadata'].pop('resourceVersion')
                if mutate == 'naive_time': raw['observed_at'] = '1970-01-01T00:01:41'
                if mutate == 'other_cluster': raw['cluster_id'] = 'west'
                self.assertFalse(self.bind(evidence=evidence)['root_entity_validation']['observed'])

    def test_ambiguous_incarnations_or_versions_are_rejected(self):
        for key, value in (('uid', 'uid-b'), ('resourceVersion', '18')):
            evidence = copy.deepcopy(self.evidence)
            second = copy.deepcopy(evidence['event_agent']['evidence_items'][0])
            second['raw_ref']['metadata'][key] = value
            evidence['event_agent']['evidence_items'].append(second)
            self.assertEqual(self.bind(evidence=evidence)['root_entity_validation']['status'], 'ambiguous_identity_or_version')

    def test_explicit_identity_conflict_and_failed_newer_read_do_not_bind(self):
        report = copy.deepcopy(self.report)
        report['root_entity']['uid'] = 'old-uid'
        self.assertEqual(self.bind(report=report)['root_entity_validation']['status'], 'deployment_incarnation_changed')
        evidence = {**self.evidence, 'event_agent_closure': {'error': 'unavailable'}}
        self.assertEqual(self.bind(evidence=evidence)['root_entity_validation']['status'], 'collector_incomplete')


if __name__ == '__main__':
    unittest.main()
