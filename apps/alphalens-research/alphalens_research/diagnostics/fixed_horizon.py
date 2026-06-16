"""Fixed-horizon market-adjusted CAR + percentile bootstrap (pure, no I/O).

Selection-quality metric: per-event buy-and-hold abnormal return over a fixed
k-session window from the event, market-adjusted (beta=1) against SPY. See
docs/superpowers/specs/2026-06-16-fixed-horizon-car-survival-fill-design.md.
"""

from __future__ import annotations

import random
from collections.abc import Sequence

K_WINDOWS: tuple[int, ...] = (5, 10, 20)
LOW_N_WARN = 30  # below this, the CI is wide / estimate anecdotal (warning only, not a gate)


def car_for_event(
    *,
    stock_anchor: float | None,
    stock_horizon: float | None,
    spy_anchor: float | None,
    spy_horizon: float | None,
) -> float | None:
    """Market-adjusted BHAR = (stock buy-hold) - (SPY buy-hold) over the window.

    ``None`` when any of the four closes is missing or non-positive.
    """
    if (
        stock_anchor is None
        or stock_anchor <= 0.0
        or stock_horizon is None
        or stock_horizon <= 0.0
        or spy_anchor is None
        or spy_anchor <= 0.0
        or spy_horizon is None
        or spy_horizon <= 0.0
    ):
        return None
    stock_bhar = stock_horizon / stock_anchor - 1.0
    spy_bhar = spy_horizon / spy_anchor - 1.0
    return stock_bhar - spy_bhar


def bootstrap_ci(
    values: Sequence[float | None],
    *,
    n_resamples: int = 10_000,
    ci: float = 0.90,
    seed: int = 0,
) -> tuple[float | None, float | None, float | None]:
    """Percentile bootstrap ``(lo, mean, hi)`` of the mean. Deterministic given ``seed``.

    ``None`` values are dropped. Returns ``(None, None, None)`` for an empty input and
    ``(x, x, x)`` for a singleton.
    """
    vals = [float(v) for v in values if v is not None]
    n = len(vals)
    if n == 0:
        return (None, None, None)
    mean = sum(vals) / n
    if n == 1:
        return (vals[0], vals[0], vals[0])
    # Seeded -> deterministic. Statistical resampling, not security-sensitive (Sonar S2245).
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(n_resamples):
        sample = [vals[rng.randrange(n)] for _ in range(n)]  # NOSONAR
        means.append(sum(sample) / n)
    means.sort()
    lo_i = int((1.0 - ci) / 2.0 * n_resamples)
    hi_i = int((1.0 + ci) / 2.0 * n_resamples) - 1
    return (means[lo_i], mean, means[hi_i])
