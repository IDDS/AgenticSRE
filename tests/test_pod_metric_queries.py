import unittest
from unittest.mock import Mock

from agents.metric_anomaly_detector import MetricAnomalyDetector
from agents.pod_metric_queries import pod_cpu_cores, pod_cpu_limit_ratio, pod_memory_limit_ratio
from agents.detection_agent import DetectionAgent
from tools.base_tool import ToolResult


class PodMetricQueryTests(unittest.TestCase):
    def test_cpu_fallback_binds_current_pod_uid_and_deduplicates(self):
        query = pod_cpu_cores('train-ticket', 'ts-station-service.*')
        self.assertIn('container!=""', query)
        self.assertIn('container=""', query)
        self.assertIn('kube_pod_info', query)
        self.assertIn('on (namespace,pod,pod_uid)', query)
        self.assertIn('max by (namespace,pod,id,cpu)', query)
        self.assertIn('namespace="train-ticket"', query)
        self.assertNotIn('namespace="train-ticket"}}', query)

    def test_ratios_require_positive_matching_uid_limit(self):
        for query in (pod_cpu_limit_ratio('train-ticket'), pod_memory_limit_ratio('train-ticket')):
            with self.subTest(query=query):
                self.assertIn('on (namespace,pod,pod_uid)', query)
                self.assertIn('kube_pod_container_resource_limits', query)
                self.assertIn(' > 0', query)
                self.assertIn('kube_pod_info', query)

    def test_builtin_application_uses_scoped_valid_queries(self):
        detector = MetricAnomalyDetector(prom_tool=Mock(), metric_checks=[], detection_cfg={})
        captured = {}
        detector._threshold_check = lambda name, query, *a, **kw: captured.setdefault(name, query) and []
        detector._zscore_check = lambda name, query, *a, **kw: captured.setdefault(name, query) and []
        detector._check_application('train-ticket')
        self.assertEqual(captured['ContainerCPUSpike'], pod_cpu_cores('train-ticket'))
        self.assertEqual(captured['ContainerMemoryHigh'], pod_memory_limit_ratio('train-ticket'))
        self.assertNotIn('namespace="train-ticket"}}', captured['ContainerCPUSpike'])

    def test_fallback_checks_use_same_queries(self):
        checks = {x['name']: x for x in DetectionAgent._default_metric_checks()}
        self.assertEqual(checks['container_cpu']['query'], f'({pod_cpu_limit_ratio()}) * 100')
        self.assertEqual(checks['container_memory']['query'], f'({pod_memory_limit_ratio()}) * 100')

    def test_namespace_is_quoted_not_interpolated_as_promql(self):
        query = pod_cpu_cores('n"} or vector(1) or {x="')
        self.assertIn('namespace="n\\\"} or vector(1) or {x=\\\""', query)

    def test_nonfinite_range_samples_are_not_treated_as_anomalies(self):
        values, timestamps, labels = MetricAnomalyDetector._parse_series({
            'metric': {'pod': 'app'}, 'values': [[1, '2'], [2, 'NaN'], ['+Inf', '3'], [3, '+Inf'], [4, '4']]
        })
        self.assertEqual(values, [2.0, 4.0])
        self.assertEqual(timestamps, [1.0, 4.0])
        self.assertEqual(labels['pod'], 'app')

    def test_detector_requests_more_than_50_series_and_reports_truncation(self):
        prom = Mock()
        prom.execute.return_value = ToolResult(success=True, data={'results': [{'metric': {'pod': str(i)}} for i in range(70)],
                                                                    'truncated': True})
        detector = MetricAnomalyDetector(prom_tool=prom, metric_checks=[], detection_cfg={})
        with self.assertLogs('agents.metric_anomaly_detector', level='WARNING') as captured:
            result = detector._instant_query('up')
        self.assertEqual(len(result), 70)
        self.assertEqual(prom.execute.call_args.kwargs['max_results'], 1000)
        self.assertIn('truncated', captured.output[0])
        with self.assertLogs('agents.metric_anomaly_detector', level='WARNING'):
            detector._range_query('up', 10)
        self.assertEqual(prom.execute.call_args.kwargs['max_results'], 1000)


if __name__ == '__main__':
    unittest.main()
