"""Bounded, auditable summaries of recorded trace operations and parent links."""
from __future__ import annotations

from collections import defaultdict
import math
import re
from typing import Any, Iterable


def summarize_trace_spans(rows: Iterable[dict[str, Any]], *, event_time: int,
                          before_s: int, after_s: int, max_rows: int = 200_000) -> dict[str, Any]:
    """Aggregate observed spans without treating a slow span as a root cause.

    Input timestamps are nanoseconds; duration is derived from start/end. A
    parent link is reported only for unique IDs found in the supplied window.
    Sampling, missing spans and partial baselines remain explicit limitations.
    """
    if any(type(x) is not int for x in (event_time, before_s, after_s, max_rows)):
        raise ValueError('invalid bounds')
    if before_s <= 0 or after_s <= 0 or max_rows <= 0:
        raise ValueError('invalid bounds')
    spans: list[dict[str, Any]] = []
    index: dict[tuple[str, str], dict[str, Any] | None] = {}
    invalid = outside = duplicate = 0
    total = 0
    for row in rows:
        total += 1
        if total > max_rows:
            raise ValueError('trace row limit exceeded')
        try:
            if not isinstance(row, dict):
                raise ValueError('invalid row')
            trace, span = row['TraceID'], row['SpanID']
            parent, pod, operation = row['ParentID'], row['PodName'], row['OperationName']
            if any(not isinstance(x, str) or not x.strip() for x in (trace, span, parent, pod, operation)):
                raise ValueError('missing identity')
            start, end = int(row['StartTimeUnixNano']), int(row['EndTimeUnixNano'])
            if start < 0 or end < start:
                raise ValueError('invalid time')
            seconds = start // 1_000_000_000
            if not event_time - before_s <= seconds < event_time + after_s:
                outside += 1
                continue
            item = {'trace': trace, 'span': span, 'parent': parent, 'pod': pod,
                    'operation': operation, 'window': 'before' if seconds < event_time else 'after',
                    'start_ns': start, 'end_ns': end, 'duration_ms': (end - start) / 1_000_000}
            key = (trace, span)
            if key in index:
                index[key] = None
                duplicate += 1
            else:
                index[key] = item
            spans.append(item)
        except (KeyError, TypeError, ValueError, OverflowError):
            invalid += 1
    operations: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    edges: dict[tuple[str, str, str], int] = defaultdict(int)
    residuals: dict[tuple[str, str, str, str, str], list[float]] = defaultdict(list)
    missing_parent = ambiguous = non_nested = 0
    for span in spans:
        own = index[(span['trace'], span['span'])]
        if own is None:
            ambiguous += 1
            continue
        operations[(span['window'], span['pod'], span['operation'])].append(span['duration_ms'])
        if span['parent'].lower() in ('root', '0', '0000000000000000'):
            continue
        parent = index.get((span['trace'], span['parent']))
        if parent is None:
            missing_parent += 1
            continue
        edges[(span['window'], parent['pod'], span['pod'])] += 1
        if parent['pod'] != span['pod']:
            if (parent['window'] != span['window'] or span['start_ns'] < parent['start_ns']
                    or span['end_ns'] > parent['end_ns']):
                non_nested += 1
                continue
            residuals[(span['window'], parent['pod'], span['pod'], parent['operation'], span['operation'])].append(
                ((parent['end_ns'] - parent['start_ns']) - (span['end_ns'] - span['start_ns'])) / 1_000_000)
    def p95(values: list[float]) -> float:
        ordered = sorted(values)
        return ordered[math.ceil(len(ordered) * .95) - 1]
    op_rows = [{'window': w, 'pod': pod, 'operation': op, 'count': len(values), 'p95_ms': p95(values)}
               for (w, pod, op), values in sorted(operations.items())]
    edge_rows = [{'window': w, 'parent_pod': a, 'child_pod': b, 'count': count}
                 for (w, a, b), count in sorted(edges.items())]
    residual_rows = [{'window': w, 'parent_pod': a, 'child_pod': b, 'parent_operation': po,
                      'child_operation': co, 'count': len(values), 'p95_ms': p95(values)}
                     for (w, a, b, po, co), values in sorted(residuals.items())]
    return {'operations': op_rows, 'parent_edges': edge_rows,
            'cross_pod_nested_duration_residuals': residual_rows,
            'quality': {'input_rows': total, 'invalid_rows': invalid, 'outside_window': outside,
                        'duplicate_ids': duplicate, 'ambiguous_spans_excluded': ambiguous,
                        'missing_or_ambiguous_parents': missing_parent,
                        'cross_pod_non_nested_or_cross_window': non_nested},
            'windows_seconds': {'before': before_s, 'after': after_s},
            'limitations': ['parent links are observed spans, not verified service dependencies',
                            'different window lengths and sampling affect counts',
                            'parent minus child duration may include unrelated or concurrent work; not network latency',
                            'clock skew or cross-window spans can invalidate nesting and are excluded',
                            'latency changes do not prove causation']}


def select_residual_comparisons(summary: dict[str, Any], *, source_sha256: str, limit: int = 4) -> dict[str, Any]:
    """Select largest signed P95 changes; explicitly retain omission metadata.

    This is a bounded presentation of observations, not a root-cause ranking.
    The supplied hash must identify the complete frozen summary available for
    audit. Missing baselines remain unpaired and never become zero.
    """
    if not isinstance(source_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', source_sha256):
        raise ValueError('invalid source hash')
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('invalid selection limit')
    rows = summary.get('cross_pod_nested_duration_residuals') if isinstance(summary, dict) else None
    quality = summary.get('quality') if isinstance(summary, dict) else None
    windows = summary.get('windows_seconds') if isinstance(summary, dict) else None
    if not isinstance(rows, list) or not isinstance(quality, dict) or not isinstance(windows, dict):
        raise ValueError('missing residual observations')
    for field in ('ambiguous_spans_excluded', 'cross_pod_non_nested_or_cross_window'):
        if type(quality.get(field)) is not int or quality[field] < 0:
            raise ValueError('missing quality counts')
    if any(type(windows.get(field)) is not int or windows[field] <= 0 for field in ('before', 'after')):
        raise ValueError('invalid window lengths')
    groups: dict[tuple[str, str, str, str], dict[str, tuple[int, float]]] = defaultdict(dict)
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('invalid residual row')
        key = tuple(row.get(x) for x in ('parent_pod', 'child_pod', 'parent_operation', 'child_operation'))
        window, count, value = row.get('window'), row.get('count'), row.get('p95_ms')
        if (any(not isinstance(x, str) or not x for x in key) or window not in ('before', 'after')
                or type(count) is not int or count <= 0 or type(value) not in (int, float)):
            raise ValueError('invalid residual row')
        try:
            if not math.isfinite(value) or value < 0:
                raise ValueError('invalid residual value')
        except OverflowError as exc:
            raise ValueError('invalid residual value') from exc
        if window in groups[key]:
            raise ValueError('duplicate residual group')
        groups[key][window] = (count, value)
    comparable = []
    for key, values in groups.items():
        if 'before' in values and 'after' in values:
            before, after = values['before'], values['after']
            comparable.append((after[1] - before[1], key, before, after))
    comparable.sort(key=lambda entry: (-entry[0], entry[1]))
    selected = [[*key, before[0], before[1], after[0], after[1]]
                for _, key, before, after in comparable[:limit]]
    return {'columns': ['parent_pod', 'child_pod', 'parent_op', 'child_op', 'before_n', 'before_p95_ms',
                        'after_n', 'after_p95_ms'], 'rows': selected,
            'selection': f'top {limit} signed P95 increases, no fault labels',
            'omitted_comparable': max(0, len(comparable) - limit), 'unpaired': len(groups) - len(comparable),
            'excluded_duplicate_spans': quality['ambiguous_spans_excluded'],
            'excluded_non_nested_or_cross_window': quality['cross_pod_non_nested_or_cross_window'],
            'before_s': windows['before'], 'after_s': windows['after'], 'source_sha256': source_sha256,
            'warning': 'Not network latency or proof of cause; omitted groups and other quality counts in source.'}
