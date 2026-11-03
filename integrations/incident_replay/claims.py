"""Conservative checks for structured CPU claims over aggregate observations."""
from __future__ import annotations

import math
from typing import Any


def assess_cpu_claim(*, claim: str, before: dict[str, Any] | None, after: dict[str, Any] | None,
                     saturation_threshold: float | None = None, minimum_samples: int = 3) -> dict[str, str]:
    """Assess a typed claim; never infer causal links or invent a threshold.

    Means and thresholds must be expressed in the same verified units. Samples
    are counts, not evidence that observations are independent. This rule is an
    advisory check, not a generic natural-language or causal verifier.
    """
    if claim not in ('increase', 'saturation'):
        raise ValueError('unsupported claim')
    if type(minimum_samples) is not int or minimum_samples < 1:
        raise ValueError('invalid sample minimum')

    def read(window: dict[str, Any] | None) -> tuple[int, float | None]:
        if not isinstance(window, dict):
            return 0, None
        count, value = window.get('n'), window.get('mean')
        if type(count) is not int or count < 0 or type(value) not in (int, float):
            return 0, None
        try:
            return (count, float(value)) if math.isfinite(value) else (0, None)
        except OverflowError:
            return 0, None

    n_before, value_before = read(before)
    n_after, value_after = read(after)
    if n_after < minimum_samples or value_after is None:
        return {'status': 'insufficient', 'reason': 'insufficient_after_samples'}
    if claim == 'increase':
        if n_before < minimum_samples or value_before is None:
            return {'status': 'insufficient', 'reason': 'insufficient_baseline_samples'}
        return {'status': 'supported' if value_after > value_before else 'contradicted', 'reason': 'mean_comparison_only'}
    if type(saturation_threshold) not in (int, float):
        return {'status': 'insufficient', 'reason': 'verified_saturation_threshold_missing'}
    try:
        threshold = float(saturation_threshold)
        if not math.isfinite(threshold) or threshold <= 0:
            raise ValueError('invalid saturation threshold')
    except OverflowError as exc:
        raise ValueError('invalid saturation threshold') from exc
    return {'status': 'supported' if value_after >= threshold else 'contradicted', 'reason': 'configured_threshold_only'}
