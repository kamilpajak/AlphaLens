"""Kaplan-Meier time-to-fill + fill-rate (pure, no I/O).

Entry-quality metric: model time (in sessions) until the dip-buy ladder's E1 is
first touched within the entry-TTL window; orders never touched within the window
are right-censored at TTL. See the design spec.
"""

from __future__ import annotations

import random
from collections.abc import Sequence


def kaplan_meier(durations: Sequence[int], events: Sequence[int]) -> list[tuple[int, float]]:
    """Product-limit survival estimate S(t) = P(not yet filled by session t).

    ``durations[i]`` = session index of fill (event=1) or the censoring time (event=0).
    Returns ``[(t, S_t), ...]`` over the distinct event/censor times, ascending.
    """
    pairs = sorted(zip(durations, events, strict=True))
    n = len(pairs)
    if n == 0:
        return []
    at_risk = n
    surv = 1.0
    out: list[tuple[int, float]] = []
    for t in sorted({d for d, _ in pairs}):
        fills = sum(1 for d, e in pairs if d == t and e == 1)
        leaving = sum(1 for d, _ in pairs if d == t)
        if at_risk > 0 and fills > 0:
            surv *= 1.0 - fills / at_risk
        out.append((t, surv))
        at_risk -= leaving
    return out


def fill_rate_ci(
    n_touched: int,
    n_total: int,
    *,
    n_resamples: int = 10_000,
    ci: float = 0.90,
    seed: int = 0,
) -> tuple[float | None, float | None, float | None]:
    """Fraction filled within the window + percentile-bootstrap CI. Deterministic given ``seed``."""
    if n_total <= 0:
        return (None, None, None)
    rate = n_touched / n_total
    data = [1] * n_touched + [0] * (n_total - n_touched)
    rng = random.Random(seed)
    rates: list[float] = []
    for _ in range(n_resamples):
        rates.append(sum(data[rng.randrange(n_total)] for _ in range(n_total)) / n_total)
    rates.sort()
    lo_i = int((1.0 - ci) / 2.0 * n_resamples)
    hi_i = int((1.0 + ci) / 2.0 * n_resamples) - 1
    return (rates[lo_i], rate, rates[hi_i])
