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


def assess_workload_surge(*, before: dict[str, Any] | None, after: dict[str, Any] | None,
                          minimum_samples: int = 3, surge_ratio: float | None = None) -> dict[str, str]:
    """Check a workload-surge claim only with a caller-supplied meaningful ratio.

    The windows must use a comparable rate and units. This does not establish
    overload or causality and is not authorization for automated action.
    """
    if type(minimum_samples) is not int or minimum_samples < 1:
        raise ValueError('invalid sample minimum')
    def value(window: dict[str, Any] | None) -> tuple[int, float | None]:
        if not isinstance(window, dict):
            return 0, None
        count, mean = window.get('n'), window.get('mean')
        if type(count) is not int or count < 0 or type(mean) not in (int, float):
            return 0, None
        try:
            return (count, float(mean)) if math.isfinite(mean) else (0, None)
        except OverflowError:
            return 0, None
    n_before, a = value(before)
    n_after, b = value(after)
    if n_before < minimum_samples or a is None:
        return {'status': 'insufficient', 'reason': 'insufficient_baseline_samples'}
    if n_after < minimum_samples or b is None:
        return {'status': 'insufficient', 'reason': 'insufficient_after_samples'}
    if a <= 0 or b < 0:
        return {'status': 'insufficient', 'reason': 'rate_not_comparable'}
    if type(surge_ratio) not in (int, float):
        return {'status': 'insufficient', 'reason': 'verified_surge_ratio_missing'}
    try:
        threshold = float(surge_ratio)
        if not math.isfinite(threshold) or threshold <= 1:
            raise ValueError('invalid surge ratio')
    except OverflowError as exc:
        raise ValueError('invalid surge ratio') from exc
    return {'status': 'supported' if b / a >= threshold else 'contradicted', 'reason': 'configured_ratio_only'}
