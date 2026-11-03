import pytest

from integrations.incident_replay.trace_evidence import summarize_trace_spans


def span(trace, sid, parent, pod, op, second, milliseconds):
    start = second * 1_000_000_000
    return {'TraceID': trace, 'SpanID': sid, 'ParentID': parent, 'PodName': pod,
            'OperationName': op, 'StartTimeUnixNano': str(start),
            'EndTimeUnixNano': str(start + milliseconds * 1_000_000)}


def test_preserves_operation_duration_and_observed_parent_child_identity():
    rows = [span('t', 'a', 'root', 'frontend', 'get', 99, 5),
            span('t', 'b', 'a', 'currency', 'convert', 99, 12),
            span('u', 'c', 'missing', 'currency', 'convert', 101, 33)]
    out = summarize_trace_spans(rows, event_time=100, before_s=10, after_s=10)
    assert {'window': 'before', 'pod': 'currency', 'operation': 'convert', 'count': 1, 'p95_ms': 12.0} in out['operations']
    assert {'window': 'before', 'parent_pod': 'frontend', 'child_pod': 'currency', 'count': 1} in out['parent_edges']
    assert out['quality']['missing_or_ambiguous_parents'] == 1


def test_duplicate_span_ids_are_not_attributed_and_limit_is_enforced():
    row = span('t', 'a', 'root', 'p', 'get', 99, 1)
    out = summarize_trace_spans([row, row], event_time=100, before_s=10, after_s=10)
    assert out['quality']['duplicate_ids'] == 1 and not out['operations']
    with pytest.raises(ValueError):
        summarize_trace_spans([row, row], event_time=100, before_s=10, after_s=10, max_rows=1)
