import math
import unittest

from tools.anomaly_detection import AnomalyDetectionTool


class BaselineZscoreTests(unittest.TestCase):
    def test_fixed_baseline_detects_a_shift_in_short_window(self):
        tool = AnomalyDetectionTool()
        values = [9, 10, 11, 10, 10, 50, 51]
        self.assertEqual(tool.execute(values=values, method="zscore").data["anomaly_count"], 0)
        result = tool.execute(values=values, method="baseline_zscore", baseline_count=5)
        self.assertTrue(result.success)
        self.assertEqual(result.data["assessment"], "evaluated")
        self.assertEqual([item["index"] for item in result.data["anomalies"]], [5, 6])

    def test_constant_baseline_is_explicitly_indeterminate(self):
        result = AnomalyDetectionTool().execute(values=[1, 1, 1, 100], method="baseline_zscore", baseline_count=3)
        self.assertTrue(result.success)
        self.assertEqual(result.data["assessment"], "indeterminate_zero_baseline_variance")
        self.assertEqual(result.data["anomaly_count"], 0)
        self.assertIsNone(result.data["anomaly_ratio"])
        self.assertEqual(result.data["evaluated_count"], 0)
        self.assertEqual(result.data["unassessed_count"], 1)

    def test_invalid_baseline_boundaries_fail(self):
        for baseline_count in (None, 2, 4, True, 3.0):
            with self.subTest(baseline_count=baseline_count):
                result = AnomalyDetectionTool().execute(values=[1, 2, 3, 10], method="baseline_zscore", baseline_count=baseline_count)
                self.assertFalse(result.success)

    def test_nonfinite_values_and_invalid_threshold_fail(self):
        for values, threshold in (([1, 2, math.nan, 5], 3), ([1, 2, 3, 5], math.inf), ([1, 2, 3, 5], 0)):
            with self.subTest(values=values, threshold=threshold):
                result = AnomalyDetectionTool().execute(values=values, method="baseline_zscore", baseline_count=3, threshold=threshold)
                self.assertFalse(result.success)

    def test_method_is_discoverable(self):
        schema = AnomalyDetectionTool().get_schema()["function"]["parameters"]["properties"]
        self.assertIn("baseline_zscore", schema["method"]["enum"])
        self.assertEqual(schema["baseline_count"]["minimum"], 3)


if __name__ == "__main__":
    unittest.main()
