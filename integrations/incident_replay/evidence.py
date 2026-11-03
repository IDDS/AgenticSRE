"""Summarize observable changes without inferring or accepting ground truth."""
from __future__ import annotations

import math
from typing import Any


def _number(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def _comparison(before: Any, after: Any) -> dict[str, Any]:
    a, b = _number(before), _number(after)
    return {'before': a, 'after': b, 'delta': None if a is None or b is None else b - a,
            'ratio': None if a is None or b is None or a <= 0 else b / a}


def _descending(row: dict[str, Any]) -> tuple[bool, float, str]:
    delta = row['change']['delta']
    return delta is None, -(delta or 0), row['pod']


def summarize_changes(observations: dict[str, Any]) -> dict[str, Any]:
    """Return separate ordered evidence lists; never read truth or assert a cause.

    Input observations contain per-pod before/after aggregates. Missing values are
    kept as unknown, decreases keep their sign, and sample counts are retained.
    Ordering highlights absolute increases, not causal confidence or an RCA ranking.
    """
    pods = observations.get('pods')
    if not isinstance(pods, list) or len(pods) > 1000:
        raise ValueError('pods must be a bounded list')
    metric_rows, trace_rows = [], []
    seen = set()
    for pod in pods:
        if not isinstance(pod, dict) or not isinstance(pod.get('pod'), str) or not pod['pod']:
            raise ValueError('invalid pod record')
        name = pod['pod']
        if name in seen:
            raise ValueError('duplicate pod record')
        seen.add(name)
        metric = pod.get('metrics') or {}
        before = ((metric.get('before') or {}).get('CpuUsageRate(%)') or {})
        after = ((metric.get('after') or {}).get('CpuUsageRate(%)') or {})
        bn, an = before.get('n'), after.get('n')
        metric_rows.append({'pod': name, 'change': _comparison(before.get('mean'), after.get('mean')),
                            'before_samples': bn, 'after_samples': an,
                            'sparse': type(bn) is not int or type(an) is not int or bn < 3 or an < 3})
        trace = pod.get('trace') or {}
        before, after = trace.get('before') or {}, trace.get('after') or {}
        trace_rows.append({'pod': name, 'change': _comparison(before.get('p95_ms'), after.get('p95_ms')),
                           'before_spans': before.get('spans'), 'after_spans': after.get('spans')})
    metric_rows.sort(key=_descending)
    trace_rows.sort(key=_descending)
    return {'cpu_usage_rate_change_percentage_points': metric_rows, 'trace_p95_change_ms': trace_rows,
            'quality': observations.get('quality', {}),
            'limitations': ['ordering is not a root-cause ranking', 'sparse samples do not establish statistical significance',
                            'missing baseline or observations remain unknown', 'trace P95 differences may reflect changed request mix']}
