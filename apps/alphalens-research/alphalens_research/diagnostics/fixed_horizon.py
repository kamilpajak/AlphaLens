"""Fixed-horizon market-adjusted CAR + percentile bootstrap (pure, no I/O).

Selection-quality metric: per-event buy-and-hold abnormal return over a fixed
k-session window from the event, market-adjusted against SPY. See
docs/superpowers/specs/2026-06-16-fixed-horizon-car-survival-fill-design.md.

Two adjustments live here side by side:

* :func:`car_for_event` subtracts the market return one-for-one (beta = 1). It
  is the historical form and is kept byte-identical so past analyses reproduce.
* :func:`car_for_event_market_model` subtracts ``beta`` times the market return,
  with ``beta`` estimated over a pre-event window by
  ``alphalens_pipeline.feedback.market_beta.estimate_beta``. The
  mapper proposes high-beta names, so under beta = 1 an up-market window leaves
  a systematic positive residual that is exposure, not selection skill.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence

K_WINDOWS: tuple[int, ...] = (5, 10, 20)
LOW_N_WARN = 30  # below this, the CI is wide / estimate anecdotal (warning only, not a gate)

DEFAULT_BETA_WINDOW = 60  # pre-event sessions read to estimate beta


def _bhar(anchor: float | None, horizon: float | None) -> float | None:
    """Buy-and-hold return, or ``None`` when either close is missing or non-positive."""
    if anchor is None or anchor <= 0.0 or horizon is None or horizon <= 0.0:
        return None
    return horizon / anchor - 1.0


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
    stock_bhar = _bhar(stock_anchor, stock_horizon)
    spy_bhar = _bhar(spy_anchor, spy_horizon)
    if stock_bhar is None or spy_bhar is None:
        return None
    return stock_bhar - spy_bhar


def car_for_event_market_model(
    *,
    stock_anchor: float | None,
    stock_horizon: float | None,
    spy_anchor: float | None,
    spy_horizon: float | None,
    beta: float,
) -> float | None:
    """BHAR net of ``beta`` times the market BHAR over the same window.

    ``beta = 1`` reproduces :func:`car_for_event` exactly. ``None`` under the
    same missing-or-non-positive-close rule.
    """
    stock_bhar = _bhar(stock_anchor, stock_horizon)
    spy_bhar = _bhar(spy_anchor, spy_horizon)
    if stock_bhar is None or spy_bhar is None:
        return None
    return stock_bhar - beta * spy_bhar


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


def day_block_bootstrap_ci(
    values_by_day: Mapping[object, Sequence[float | None]],
    *,
    n_resamples: int = 10_000,
    ci: float = 0.90,
    seed: int = 0,
) -> tuple[float | None, float | None, float | None]:
    """Percentile bootstrap ``(lo, mean, hi)`` with day-level resampling.

    Each replicate resamples ``len(days)`` days WITH REPLACEMENT, pools all drawn
    days' non-None values, and takes the pooled mean.  The point estimate (middle
    element) is the GRAND MEAN over all non-None rows across all days — identical
    to ``bootstrap_ci(flattened)[1]`` on the same data.

    ``None`` values within a day are dropped before pooling.  Days that become
    empty after dropping ``None`` are excluded from the day list.

    Returns ``(None, None, None)`` when no non-None values exist.  Returns a
    degenerate ``(m, m, m)`` when only one non-empty day exists — resampling one
    day with replacement always draws the same day, so all replicate means equal
    the grand mean (n_eff = 1, the whole point of day-blocking).
    """
    # Build per-day value lists with None dropped; keep only non-empty days.
    days_vals: list[list[float]] = []
    for seq in values_by_day.values():
        cleaned = [float(v) for v in seq if v is not None]
        if cleaned:
            days_vals.append(cleaned)

    if not days_vals:
        return (None, None, None)

    # Grand mean = mean over ALL non-None rows (NOT mean of day-means).
    all_vals: list[float] = [v for day in days_vals for v in day]
    n_total = len(all_vals)
    mean = sum(all_vals) / n_total

    n_days = len(days_vals)
    if n_days == 1:
        # Degenerate: only one day; every resample draws it, CI collapses to (m, m, m).
        return (mean, mean, mean)

    # Seeded -> deterministic. Statistical resampling, not security-sensitive (Sonar S2245).
    rng = random.Random(seed)
    replicate_means: list[float] = []
    for _ in range(n_resamples):
        pooled: list[float] = []
        for _ in range(n_days):
            pooled.extend(days_vals[rng.randrange(n_days)])  # NOSONAR
        replicate_means.append(sum(pooled) / len(pooled))
    replicate_means.sort()
    lo_i = int((1.0 - ci) / 2.0 * n_resamples)
    hi_i = int((1.0 + ci) / 2.0 * n_resamples) - 1
    return (replicate_means[lo_i], mean, replicate_means[hi_i])
