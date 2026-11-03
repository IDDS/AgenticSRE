"""Build separately bounded retrospective windows for distinct data sources."""
from __future__ import annotations

from typing import Any


def build_windows(*, event_time: int, available_since: dict[str, int | None],
                  baseline_seconds: int = 120, observation_seconds: int = 90) -> dict[str, Any]:
    """Clip each baseline to its own known availability; never invent samples."""
    if type(event_time) is not int or type(baseline_seconds) is not int or type(observation_seconds) is not int:
        raise ValueError('times must be integers')
    if not 1 <= baseline_seconds <= 86400 or not 1 <= observation_seconds <= 86400:
        raise ValueError('invalid window duration')
    if not isinstance(available_since, dict) or not available_since or set(available_since) - {'metric', 'log', 'trace'}:
        raise ValueError('invalid source availability')
    result = {}
    for source, since in available_since.items():
        if since is not None and type(since) is not int:
            raise ValueError('invalid availability time')
        if since is None:
            result[source] = {'before': None, 'after': [event_time, event_time + observation_seconds],
                              'baseline_coverage_seconds': None, 'baseline_complete': False}
            continue
        start = min(event_time, max(event_time - baseline_seconds, since))
        result[source] = {'before': [start, event_time], 'after': [event_time, event_time + observation_seconds],
                          'baseline_coverage_seconds': event_time - start,
                          'baseline_complete': start == event_time - baseline_seconds}
    return result
