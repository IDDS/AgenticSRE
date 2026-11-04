import copy
import unittest

from skill_runtime.fault_target_selector import recommend_targets
from tools import build_tool_registry


def resource(kind, name, uid, **extra):
    return {'kind': kind, 'metadata': {'name': name, 'uid': uid, 'resourceVersion': '42',
                                       'namespace': 'experiment', **extra}}


def topology():
    dep = resource('Deployment', 'checkout', 'dep')
    dep['status'] = {'readyReplicas': 1}
    rs = resource('ReplicaSet', 'checkout-rs', 'rs', ownerReferences=[{'kind': 'Deployment', 'name': 'checkout', 'uid': 'dep', 'controller': True}])
    pod = resource('Pod', 'checkout-pod', 'pod', labels={'app': 'checkout'},
                   ownerReferences=[{'kind': 'ReplicaSet', 'name': 'checkout-rs', 'uid': 'rs', 'controller': True}])
    pod['status'] = {'phase': 'Running'}
    svc = resource('Service', 'checkout-svc', 'svc')
    svc['spec'] = {'selector': {'app': 'checkout'}}
    sl = resource('EndpointSlice', 'checkout-slice', 'slice', labels={'kubernetes.io/service-name': 'checkout-svc'},
                  ownerReferences=[{'kind': 'Service', 'name': 'checkout-svc', 'uid': 'svc', 'controller': True}])
    sl['endpoints'] = [{'conditions': {'ready': True}, 'targetRef': {'kind': 'Pod', 'uid': 'pod',
                                                                    'name': 'checkout-pod', 'namespace': 'experiment'}}]
    return [dep, rs, pod, svc, sl]


def run(resources, **kwargs):
    return recommend_targets(resources, cluster_id='test', allowed_namespaces=['experiment'],
                             allowed_deployment_uids=['dep'], observed_at='2026-11-04T00:00:00Z', **kwargs)


def cpu_observation(uid='pod', name='checkout-pod', value=0.1, at='2026-11-03T23:59:00Z'):
    return {'pod_uid': uid, 'pod_name': name, 'namespace': 'experiment', 'cpu_cores': value,
            'cgroup_id': '/kubepods.slice/kubepods-burstable-pod' + uid + '.slice',
            'window_seconds': 300, 'observed_at': at}


class FaultTargetSelectionTests(unittest.TestCase):
    def test_uid_bound_candidate_requires_independent_authorization(self):
        result = run(topology())
        self.assertEqual(result['status'], 'proposal_only')
        candidate = result['candidates'][0]
        self.assertEqual(candidate['uid'], 'dep')
        self.assertEqual(candidate['services'][0]['pod_uids'], ['pod'])
        self.assertFalse(candidate['execution_authorized'])
        self.assertTrue(candidate['requires_fresh_observation'])

    def test_forged_owner_and_selector_do_not_produce_candidates(self):
        for mutate in ('owner', 'owner_name', 'selector', 'namespace', 'unready'):
            data = topology()
            if mutate == 'owner': data[1]['metadata']['ownerReferences'][0]['uid'] = 'other'
            if mutate == 'owner_name': data[1]['metadata']['ownerReferences'][0]['name'] = 'other'
            if mutate == 'selector': data[3]['spec']['selector']['app'] = 'other'
            if mutate == 'namespace': data[2]['metadata']['namespace'] = 'other'
            if mutate == 'unready': data[4]['endpoints'][0]['conditions']['ready'] = False
            with self.subTest(mutate=mutate):
                self.assertEqual(run(data)['candidates'], [])

    def test_unknown_or_excess_service_backends_are_not_recommended(self):
        data = topology()
        data[4]['endpoints'].append({'conditions': {'ready': True},
                                     'targetRef': {'kind': 'Pod', 'uid': 'other', 'name': 'other', 'namespace': 'experiment'}})
        self.assertEqual(run(data, max_ready_backends=1)['candidates'], [])
        data[4]['endpoints'][1]['conditions'] = {}
        self.assertEqual(run(data)['candidates'], [])
        data = topology()
        uncertain = copy.deepcopy(data[4])
        uncertain['metadata']['name'] = 'other-slice'
        uncertain['metadata']['uid'] = 'other-slice-uid'
        uncertain['metadata']['ownerReferences'] = []
        data.append(uncertain)
        self.assertEqual(run(data)['candidates'], [])

    def test_duplicate_uid_or_bad_scope_is_rejected(self):
        data = topology()
        data.append(copy.deepcopy(data[0]))
        with self.assertRaises(ValueError):
            run(data)
        with self.assertRaises(ValueError):
            recommend_targets(topology(), cluster_id='test', allowed_namespaces=[], allowed_deployment_uids=['dep'], observed_at='2026-11-04T00:00:00Z')
        with self.assertRaises(ValueError):
            recommend_targets(topology(), cluster_id='test', allowed_namespaces=['experiment'], allowed_deployment_uids=[], observed_at='2026-11-04T00:00:00Z')
        denied = recommend_targets(topology(), cluster_id='test', allowed_namespaces=['experiment'],
                                   allowed_deployment_uids=['different'], observed_at='2026-11-04T00:00:00Z')
        self.assertEqual(denied['candidates'], [])

    def test_registered_tool_is_read_only(self):
        tool = build_tool_registry(isolated=True).get('fault_target_recommendation')
        self.assertIsNotNone(tool)
        result = tool.execute(resources=topology(), cluster_id='test', allowed_namespaces=['experiment'],
                              allowed_deployment_uids=['dep'], observed_at='2026-11-04T00:00:00Z')
        self.assertTrue(result.success)
        self.assertEqual(result.data['status'], 'proposal_only')

    def test_cpu_gate_rejects_stale_missing_or_busy_evidence(self):
        ok = run(topology(), max_cpu_cores=0.2, pod_cpu_observations=[cpu_observation()])
        self.assertEqual(ok['candidates'][0]['max_pod_cpu_cores_5m'], 0.1)
        self.assertEqual(run(topology(), max_cpu_cores=0.05, pod_cpu_observations=[cpu_observation()])['candidates'], [])
        self.assertEqual(run(topology(), max_cpu_cores=0.2, pod_cpu_observations=[])['candidates'], [])
        stale = cpu_observation(at='2026-11-03T23:00:00Z')
        self.assertEqual(run(topology(), max_cpu_cores=0.2, pod_cpu_observations=[stale])['candidates'], [])
        wrong = cpu_observation()
        wrong['cgroup_id'] = '/kubepods.slice/other-podother.slice'
        with self.assertRaises(ValueError):
            run(topology(), max_cpu_cores=0.2, pod_cpu_observations=[wrong])
        with self.assertRaises(ValueError):
            run(topology(), max_cpu_cores=0.2, pod_cpu_observations=[cpu_observation(), cpu_observation()])

    def test_cpu_breaks_coverage_ties_using_uid_bound_observations(self):
        replacement = {'checkout': 'checkout2', 'checkout-rs': 'checkout2-rs', 'checkout-pod': 'checkout2-pod',
                       'checkout-svc': 'checkout2-svc', 'checkout-slice': 'checkout2-slice',
                       'dep': 'dep2', 'rs': 'rs2', 'pod': 'pod2', 'svc': 'svc2', 'slice': 'slice2'}
        def rename(value):
            if isinstance(value, dict):
                return {k: rename(v) for k, v in value.items()}
            if isinstance(value, list):
                return [rename(v) for v in value]
            return replacement.get(value, value) if isinstance(value, str) else value
        observations = [cpu_observation(value=0.15), cpu_observation(uid='pod2', name='checkout2-pod', value=0.05)]
        result = recommend_targets(topology() + rename(topology()), cluster_id='test', allowed_namespaces=['experiment'],
                                   allowed_deployment_uids=['dep', 'dep2'], observed_at='2026-11-04T00:00:00Z',
                                   max_cpu_cores=0.2, pod_cpu_observations=observations)
        self.assertEqual([c['uid'] for c in result['candidates']], ['dep2', 'dep'])


if __name__ == '__main__':
    unittest.main()
