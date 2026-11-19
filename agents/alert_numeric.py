"""Retain explicit millisecond extrema from log text and labels, with raw IDs.

These are scalar observations, not independently sampled metric time series.
The six bounded channels distinguish text/labels and duration/latency/waits.
Repeated occurrences of a field count as occurrences, not independent requests.
"""
from __future__ import annotations

import math
import re


_NUMBER = r'[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?'
FIELD_START = r'(?<![^\s,;?&])'
FIELD_END = r'(?=$|[\s,;&])'
MS_FIELD = re.compile(FIELD_START + r'(?P<ms_field>duration_ms|latency_ms)=(?P<ms_value>' + _NUMBER + ')' + FIELD_END, re.I)
MS_WAIT = re.compile(r'\b(?P<wait>after|waiting)\s+(?P<elapsed>' + _NUMBER + r')\s+(?P<unit>ms|milliseconds)\b', re.I)


def milliseconds(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def observations(row):
    values = {}

    def add(channel, value):
        value = milliseconds(value)
        if value is None:
            return
        current = values.setdefault(channel, {'min': value, 'max': value, 'observations': 0, 'unit': 'ms'})
        current['min'] = min(current['min'], value)
        current['max'] = max(current['max'], value)
        current['observations'] += 1

    message = str(row.get('message') or '')
    for field, value in MS_FIELD.findall(message):
        add('message.' + field.lower(), value)
    for field, value, _ in MS_WAIT.findall(message):
        add('message.' + field.lower() + '_ms', value)
    for field, value in (row.get('labels') or {}).items():
        if str(field).lower() in ('duration_ms', 'latency_ms'):
            add('labels.' + field.lower(), value)
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
