"""Lossless, schema-checked packing of historical aggregate observations."""
from __future__ import annotations

import json
from typing import Any

_METRICS = ('CpuUsage(m)', 'CpuUsageRate(%)', 'MemoryUsage(Mi)',
            'PodServerLatencyP95(s)', 'PodWorkload(Ops)')


def compact_domains(observations: dict[str, Any]) -> dict[str, str]:
    """Pack the supplied aggregate records; raise rather than silently drop fields.

    This is not a compression of raw telemetry: callers must retain the source
    files and report that these inputs are already time-window aggregates.
    """
    pods = observations.get('pods')
    quality = observations.get('quality')
    if not isinstance(pods, list) or not isinstance(quality, dict):
        raise ValueError('missing aggregate pods or quality')
    rows: dict[str, list[Any]] = {'metric_agent': [], 'log_agent': [], 'trace_agent': []}
    names: set[str] = set()
    for pod in pods:
        if not isinstance(pod, dict) or set(pod) != {'pod', 'metrics', 'logs', 'trace'}:
            raise ValueError('unsupported pod aggregate fields')
        name = pod['pod']
        if not isinstance(name, str) or not name or name in names:
            raise ValueError('invalid or duplicate pod')
        names.add(name)
        metrics = pod['metrics']
        if not isinstance(metrics, dict) or set(metrics) != {'before', 'after'}:
            raise ValueError('unsupported metric windows')
        packed = []
        for window in ('before', 'after'):
            values = metrics[window]
            if not isinstance(values, dict) or not set(values).issubset(_METRICS):
                raise ValueError('unsupported metrics')
            cols = []
            for key in _METRICS:
                if key not in values:
                    cols.append(None)
                    continue
                value = values[key]
                if not isinstance(value, dict) or set(value) != {'n', 'mean'}:
                    raise ValueError('unsupported metric value fields')
                cols.append([value['n'], value['mean']])
            packed.append(cols)
        rows['metric_agent'].append([name, *packed])
        for source, key in (('log_agent', 'logs'), ('trace_agent', 'trace')):
            if not isinstance(pod[key], dict):
                raise ValueError('unsupported source aggregate')
            rows[source].append([name, pod[key]])
    def encode(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return {
        'metric_agent': 'Window aggregates, not raw telemetry. Each pod row is [pod,before,after]; '
                        'each window lists [n,mean] by metric order; null means absent. '
                        + encode({'metric_order': _METRICS, 'pods': rows['metric_agent'], 'quality': quality}),
        'log_agent': 'Window log aggregates, not raw logs. Each row is [pod,window_counts]. '
                     + encode(rows['log_agent']),
        'trace_agent': 'Window trace aggregates, not raw spans. Each row is [pod,window_summary]. '
                       + encode(rows['trace_agent']),
    }
