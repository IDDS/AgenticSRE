import unittest

from tools import MultimetricAnomalyTool, build_tool_registry


class MultimetricAnomalyTests(unittest.TestCase):
    def test_trailing_baseline_detects_multiple_series_without_looking_ahead(self):
        tool = MultimetricAnomalyTool()
        result = tool.execute(series={'a': [9, 10, 11, 10, 50, 10],
                                      'b': [19, 20, 21, 20, 50, 20]},
                              lookback=4, threshold=3, min_signals=2)
        self.assertTrue(result.success)
        first, second = result.data['results']
        self.assertTrue(first['detected'])
        self.assertEqual(first['signal_metrics'], ['a', 'b'])
        self.assertFalse(second['detected'])
        prefix = tool.execute(series={'a': [9, 10, 11, 10, 50], 'b': [19, 20, 21, 20, 50]},
                              lookback=4, threshold=3, min_signals=2)
        self.assertEqual(prefix.data['results'][0], first)

    def test_insufficient_evidence_is_not_reported_healthy(self):
        result = MultimetricAnomalyTool().execute(series={'a': [1, 1, 1, 1, 10],
                                                           'b': [1, None, 2, 1, 10]},
                                                   lookback=4, min_signals=2)
        point = result.data['results'][0]
        self.assertEqual(point['assessment'], 'indeterminate')
        self.assertIsNone(point['detected'])

    def test_invalid_inputs_are_rejected(self):
        tool = MultimetricAnomalyTool()
        for series, kwargs in [({'a': [1, 2, 3, 4], 'b': [1, 2]}, {'lookback': 3}),
                               ({'a': [1, 2, float('nan'), 4]}, {'lookback': 3, 'min_signals': 1}),
                               ({'a': [1, 2, 3, 4]}, {'lookback': 3, 'min_signals': 2})]:
            with self.subTest(series=series, kwargs=kwargs):
                self.assertFalse(tool.execute(series=series, **kwargs).success)

    def test_registration_is_discoverable(self):
        registry = build_tool_registry(isolated=True)
        self.assertIsInstance(registry.get('multimetric_anomaly_detection'), MultimetricAnomalyTool)


if __name__ == '__main__':
    unittest.main()
