"""Bound final-adjudication context without hiding omitted observations."""
from __future__ import annotations

from typing import Any


_MARKER = '\n[TRUNCATED: omitted evidence is unknown, not proof of absence.]'


def build_evidence_context(evidence: dict[str, Any], max_chars: int = 6000) -> tuple[str, list[str]]:
    """Allocate a shared character budget fairly, reporting every truncation.

    Preserves source order, redistributes unused capacity, and never treats an
    omitted suffix as evidence that no other observations exist.
    """
    if type(max_chars) is not int or max_chars < 256:
        raise ValueError('evidence budget is too small')
    entries = [(str(name), str(value.get('summary', value)) if isinstance(value, dict) else str(value))
               for name, value in evidence.items() if name != 'correlation']
    if not entries:
        return '', []
    headers = [f'\n[{name}]: ' for name, _ in entries]
    if sum(len(header) + len(text) + 1 for header, (_, text) in zip(headers, entries)) <= max_chars:
        return ''.join(header + text + '\n' for header, (_, text) in zip(headers, entries)), []
    overhead = sum(len(header) + 1 + len(_MARKER) for header in headers)
    if overhead >= max_chars:
        raise ValueError('too many evidence sources for the budget')
    remaining = max_chars - overhead
    allocated = [0] * len(entries)
    active = [i for i, (_, text) in enumerate(entries) if text]
    while remaining and active:
        share = max(1, remaining // len(active))
        next_active = []
        for i in active:
            grant = min(share, len(entries[i][1]) - allocated[i], remaining)
            allocated[i] += grant
            remaining -= grant
            if allocated[i] < len(entries[i][1]):
                next_active.append(i)
            if not remaining:
                break
        active = next_active
    segments, truncated = [], []
    for (name, text), header, limit in zip(entries, headers, allocated):
        clipped = len(text) > limit
        if clipped:
            truncated.append(name)
        segments.append(header + text[:limit] + (_MARKER if clipped else '') + '\n')
    result = ''.join(segments)
    assert len(result) <= max_chars
    return result, truncated
