"""Causal multimetric anomaly detection for aligned telemetry series."""

import math
from typing import Dict, List, Optional

import numpy as np

from tools.base_tool import SRETool, ToolResult


class MultimetricAnomalyTool(SRETool):
    """Score each point against preceding observations and count metric signals."""

    name = "multimetric_anomaly_detection"
    description = "Detect aligned metric anomalies using a trailing baseline and signal count"

    def _execute(self, series: Dict[str, List[Optional[float]]], lookback: int = 60,
                 threshold: float = 3.0, min_signals: int = 2) -> ToolResult:
        if not isinstance(series, dict) or not series or len(series) > 256:
            return ToolResult(success=False, error="series must contain 1 to 256 named metrics")
        if not isinstance(lookback, int) or isinstance(lookback, bool) or not 3 <= lookback <= 1440:
            return ToolResult(success=False, error="lookback must be an integer between 3 and 1440")
        if (not isinstance(threshold, (int, float)) or isinstance(threshold, bool)
                or not math.isfinite(threshold) or threshold <= 0):
            return ToolResult(success=False, error="threshold must be finite and positive")
        if (not isinstance(min_signals, int) or isinstance(min_signals, bool)
                or not 1 <= min_signals <= len(series)):
            return ToolResult(success=False, error="min_signals must be between 1 and the metric count")
        if any(not isinstance(name, str) or not name for name in series):
            return ToolResult(success=False, error="metric names must be nonempty strings")
        names = sorted(series)
        arrays = [series[name] for name in names]
        if any(not isinstance(values, list) for values in arrays):
            return ToolResult(success=False, error="each metric must contain an aligned value list")
        length = len(arrays[0])
        if length <= lookback or length > 20000 or any(len(values) != length for values in arrays):
            return ToolResult(success=False, error="series must have equal lengths greater than lookback and at most 20000")
        for values in arrays:
            for value in values:
                if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)
                                          or not math.isfinite(value)):
                    return ToolResult(success=False, error="metric values must be finite numbers or null")
        matrix = np.asarray([[np.nan if value is None else value for value in values] for values in arrays],
                            dtype=np.float64).T
        output = []
        for start in range(lookback, length, 256):
            end = min(length, start + 256)
            block = matrix[start - lookback:end]
            history = np.lib.stride_tricks.sliding_window_view(block, lookback, axis=0)[:-1]
            current = matrix[start:end]
            with np.errstate(over='ignore', invalid='ignore', divide='ignore'):
                mean = np.mean(history, axis=2)
                std = np.std(history, axis=2)
                valid = (np.all(np.isfinite(history), axis=2) & np.isfinite(current)
                         & np.isfinite(mean) & np.isfinite(std) & (std > 0))
                zscore = np.zeros_like(current)
                np.divide(np.abs(current - mean), std, out=zscore, where=valid)
                valid &= np.isfinite(zscore)
                flags = valid & (zscore > threshold)
            for offset, (flag_row, valid_row) in enumerate(zip(flags, valid)):
                assessed_count = int(np.sum(valid_row))
                indices = np.flatnonzero(flag_row)
                status = 'evaluated' if assessed_count >= min_signals else 'indeterminate'
                output.append({'index': start + offset, 'assessment': status,
                               'detected': bool(len(indices) >= min_signals) if status == 'evaluated' else None,
                               'assessed_metrics': assessed_count, 'signal_count': len(indices),
                               'signal_metrics': [names[i] for i in indices]})
        return ToolResult(success=True, data={'lookback': lookback, 'threshold': threshold,
                                             'min_signals': min_signals, 'metric_count': len(names),
                                             'warmup_points': lookback, 'results': output})

    def _parameters_schema(self):
        return {'type': 'object', 'properties': {
            'series': {'type': 'object', 'additionalProperties': {'type': 'array',
                       'items': {'type': ['number', 'null']}}},
            'lookback': {'type': 'integer', 'minimum': 3, 'maximum': 1440, 'default': 60},
            'threshold': {'type': 'number', 'exclusiveMinimum': 0, 'default': 3.0},
            'min_signals': {'type': 'integer', 'minimum': 1, 'default': 2},
        }, 'required': ['series']}
