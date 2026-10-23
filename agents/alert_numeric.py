"""Retain explicit millisecond extrema from log text and labels, with raw IDs.

These are scalar observations, not independently sampled metric time series.
The six bounded channels distinguish text/labels and duration/latency/waits.
Repeated occurrences of a field count as occurrences, not independent requests.
"""
from __future__ import annotations

import math
import re


_NUMBER = r'[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?'
_FIELD = re.compile(r'\b(duration_ms|latency_ms)=(' + _NUMBER + r')(?=$|[\s,;])', re.I)
_ELAPSED = re.compile(r'\b(after|waiting)\s+(' + _NUMBER + r')\s+(?:ms|milliseconds)\b', re.I)


def observations(row):
    values = {}

    def add(channel, value):
        if isinstance(value, bool):
            return
        try:
            value = float(value)
        except (ValueError, TypeError, OverflowError):
            return
        if not math.isfinite(value) or value < 0:
            return
        current = values.setdefault(channel, {'min': value, 'max': value, 'observations': 0, 'unit': 'ms'})
        current['min'] = min(current['min'], value)
        current['max'] = max(current['max'], value)
        current['observations'] += 1

    message = str(row.get('message') or '')
    for field, value in _FIELD.findall(message):
        add('message.' + field.lower(), value)
    for field, value in _ELAPSED.findall(message):
        add('message.' + field.lower() + '_ms', value)
    for field in ('duration_ms', 'latency_ms'):
        if field in (row.get('labels') or {}):
            add('labels.' + field, row['labels'][field])
    return values


def summarize(members):
    summary, by_alert = {}, {}
    for row in members:
        numeric = observations(row)
        if numeric:
            by_alert[row['alert_id']] = numeric
        for channel, value in numeric.items():
            if channel not in summary:
                summary[channel] = dict(value, min_alert_id=row['alert_id'], max_alert_id=row['alert_id'])
                continue
            current = summary[channel]
            current['observations'] += value['observations']
            if value['min'] < current['min']:
                current.update(min=value['min'], min_alert_id=row['alert_id'])
            if value['max'] > current['max']:
                current.update(max=value['max'], max_alert_id=row['alert_id'])
    return summary, by_alert
