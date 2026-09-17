"""Market-model beta over a pre-event window (#996), shared by pipeline and research.

Moved here from ``alphalens_research.diagnostics.fixed_horizon`` when a pipeline
stamper (``feedback/selection_label.py``) became its second user: the pipeline may
not import the research lab (ADR 0011). Pure standard library, no I/O.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

BETA_ESTIMATED = "estimated"
BETA_FALLBACK_THIN = "fallback_thin_window"  # too few usable return pairs
BETA_FALLBACK_DEGENERATE = "fallback_degenerate"  # one leg never moved
MIN_BETA_OBSERVATIONS = 30  # fewer usable daily-return pairs than this -> fall back to beta = 1
BETA_FALLBACK_VALUE = 1.0  # what a failed estimate reverts to: the historical beta=1 form
# A daily return under this is a rounding artefact of dividing one close by an equal one,
# not a session in which the ticker traded. Well below any real tick on a priced instrument.
FLAT_RETURN_TOL = 1e-12


class BetaEstimate(NamedTuple):
    """``beta`` with the provenance needed to filter on it later.

    ``source`` is :data:`BETA_ESTIMATED`, :data:`BETA_FALLBACK_THIN` or
    :data:`BETA_FALLBACK_DEGENERATE` -- the two failure modes are tagged apart
    because they mean different things: a thin window may fill in later, a
    degenerate one says the ticker or the market never moved.

    ``n_observations`` counts the usable daily-return pairs behind the estimate
    and is reported even when the estimate fell back. ``n_zero_returns`` counts
    how many of those sessions the STOCK did not move at all: the degeneracy
    guard only catches a perfectly flat series, so a partially stale ticker
    still gets an estimate, and this is the number that exposes it.
    """

    beta: float
    source: str
    n_observations: int
    n_zero_returns: int


def _paired_daily_returns(
    stock_closes: Sequence[float | None],
    market_closes: Sequence[float | None],
) -> list[tuple[float, float]]:
    """``(stock_return, market_return)`` for every session both series can price.

    A return is kept only when all four closes bracketing it are present and
    positive, so a gap never turns into a multi-session return quietly priced
    as a one-session one.
    """
    pairs: list[tuple[float, float]] = []
    for i in range(1, len(stock_closes)):
        s0, s1 = stock_closes[i - 1], stock_closes[i]
        m0, m1 = market_closes[i - 1], market_closes[i]
        if s0 is None or s1 is None or m0 is None or m1 is None:
            continue
        if s0 <= 0.0 or s1 <= 0.0 or m0 <= 0.0 or m1 <= 0.0:
            continue
        pairs.append((s1 / s0 - 1.0, m1 / m0 - 1.0))
    return pairs


def estimate_beta(
    stock_closes: Sequence[float | None],
    market_closes: Sequence[float | None],
    *,
    min_observations: int = MIN_BETA_OBSERVATIONS,
) -> BetaEstimate:
    """OLS beta of daily stock returns on daily market returns over a pre-event window.

    Both series are chronological closes of the SAME sessions and must be the
    same length -- a length mismatch means the caller aligned them wrong and
    raises ``ValueError`` rather than silently regressing offset days.

    Falls back to ``beta = 1`` (tagged :data:`BETA_FALLBACK_ONE`) when fewer
    than ``min_observations`` usable return pairs survive, or when the market
    leg has no variance. No shrinkage and no clamp: the raw estimate plus
    ``n_observations`` lets a caller decide, and a silently clamped beta would
    be indistinguishable from a real one.
    """
    if len(stock_closes) != len(market_closes):
        raise ValueError(
            f"close series must be aligned; got {len(stock_closes)} vs {len(market_closes)}"
        )
    pairs = _paired_daily_returns(stock_closes, market_closes)
    n = len(pairs)
    n_zero = sum(1 for s, _ in pairs if abs(s) <= FLAT_RETURN_TOL)
    if n < min_observations:
        return BetaEstimate(BETA_FALLBACK_VALUE, BETA_FALLBACK_THIN, n, n_zero)

    mean_s = sum(s for s, _ in pairs) / n
    mean_m = sum(m for _, m in pairs) / n
    covariance = sum((s - mean_s) * (m - mean_m) for s, m in pairs)
    market_variance = sum((m - mean_m) ** 2 for _, m in pairs)
    stock_variance = sum((s - mean_s) ** 2 for s, _ in pairs)
    # A flat market makes the slope undefined; a flat STOCK makes it exactly zero, which
    # would silently strip the market adjustment out and score raw exposure as skill.
    if market_variance <= 0.0 or stock_variance <= 0.0:
        return BetaEstimate(BETA_FALLBACK_VALUE, BETA_FALLBACK_DEGENERATE, n, n_zero)
    return BetaEstimate(covariance / market_variance, BETA_ESTIMATED, n, n_zero)


__all__ = [
    "BETA_ESTIMATED",
    "BETA_FALLBACK_DEGENERATE",
    "BETA_FALLBACK_THIN",
    "BETA_FALLBACK_VALUE",
    "FLAT_RETURN_TOL",
    "MIN_BETA_OBSERVATIONS",
    "BetaEstimate",
    "estimate_beta",
]
