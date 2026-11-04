import unittest

from diagnostic_strategy import hypothesis_review_context, validate_hypothesis_review


class ZeroReplicaHypothesisReviewTests(unittest.TestCase):
    def review(self, description, status='active', signal='deployment_scaled_to_zero'):
        hypotheses = [{'id': 'H1', 'description': description, 'status': status, 'confidence': .8}]
        catalog = [{'id': 'obs-zero', 'signal_type': signal, 'entity': {
            'cluster_id': 'east', 'namespace': 'shop', 'kind': 'Deployment', 'name': 'api', 'uid': 'uid'}}]
        response = {'hypothesis_reviews': [{'id': 'H1', 'status': 'contradicted',
            'supporting_evidence_ids': [], 'contradicting_evidence_ids': ['obs-zero'],
            'reason': 'Deployment 副本数为 0，其他机制不成立。'}]}
        return validate_hypothesis_review(hypothesis_review_context(hypotheses, catalog), response)

    def test_zero_replicas_do_not_disprove_selector_network_or_pod_state(self):
        for description in ('Service 选择器与 Pod 标签不匹配', '网络策略阻止端点发现', 'Pod 崩溃未就绪'):
            with self.subTest(description=description):
                item = self.review(description)['items'][0]
                self.assertEqual(item['reviewer_status'], 'contradicted')
                self.assertEqual(item['status'], 'uncertain')
                self.assertIn('zero_replicas_does_not_refute_other_mechanisms', item['rationale_limits'])

    def test_unverified_asserted_rejection_remains_blocked(self):
        result = self.review('Service 选择器与 Pod 标签不匹配', status='rejected')
        self.assertIn('asserted_hypothesis_not_independently_verified', result['blockers'])

    def test_guard_does_not_assert_unrelated_counterevidence_is_false(self):
        result = self.review('Service 选择器与 Pod 标签不匹配', signal='other_typed_observation')
        self.assertNotIn('zero_replicas_does_not_refute_other_mechanisms', result['items'][0]['rationale_limits'])


if __name__ == '__main__':
    unittest.main()
