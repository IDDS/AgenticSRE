"""Small, dependency-free statistical helpers for confidence gates.

The project deliberately keeps these functions separate from heuristic RCA
scoring. They are used where a statistical quantity is required, such as a
lower confidence bound for confirmed diagnosis accuracy or recovery success.
"""

from __future__ import annotations

import math


def _binomial_upper_tail(*, trials: int, successes: int, probability: float) -> float:
    """Return P[X >= successes] for X ~ Binomial(trials, probability)."""
    if successes <= 0:
        return 1.0
    if successes > trials or probability <= 0.0:
        return 0.0
    if probability >= 1.0:
        return 1.0

    log_p = math.log(probability)
    log_q = math.log1p(-probability)
    terms = []
    for count in range(successes, trials + 1):
        log_term = (
            math.lgamma(trials + 1)
            - math.lgamma(count + 1)
            - math.lgamma(trials - count + 1)
            + count * log_p
            + (trials - count) * log_q
        )
        terms.append(math.exp(log_term))
    return min(1.0, math.fsum(terms))


def clopper_pearson_lower_bound(
    successes: int,
    trials: int,
    *,
    confidence_level: float = 0.95,
) -> float:
    """Return the one-sided Clopper-Pearson lower bound.

    The bound is the solution of
    ``P[Binomial(trials, p) >= successes] = 1 - confidence_level``.
    It is exact up to the numerical bisection tolerance and requires no SciPy
    dependency.  Invalid or empty samples conservatively return zero.
    """
    try:
        trials = int(trials)
        successes = int(successes)
        confidence_level = float(confidence_level)
    except (TypeError, ValueError):
        return 0.0

    if trials <= 0 or successes <= 0 or successes > trials:
        return 0.0
    if not 0.0 < confidence_level < 1.0:
        confidence_level = 0.95

    alpha = 1.0 - confidence_level
    low = 0.0
    high = successes / trials
    for _ in range(80):
        midpoint = (low + high) / 2.0
        tail = _binomial_upper_tail(
            trials=trials,
            successes=successes,
            probability=midpoint,
        )
        if tail < alpha:
            low = midpoint
        else:
            high = midpoint
    return max(0.0, min(1.0, (low + high) / 2.0))
