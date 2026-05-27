"""Volatility-normalized equal-risk tier sizing.

This is the one component with genuine empirical support (design memo §2):
volatility-normalized risk units lower drawdown / raise Sharpe. The value
is in the SIZING, not in any predictive property of the levels.

Per tier ``i``: ``shares_i = (B * q_i) / (E_i - S)`` where ``B`` is the risk
budget (fraction of book risked if all tiers fill), ``q_i`` the
risk-distribution weight (default equal), ``E_i`` the entry, ``S`` the stop.
Allocation % is by notional: ``alloc_i ∝ q_i * E_i / (E_i - S)``.

The ladder guarantees ``E_i - S >= 0.5*ATR`` before sizing, so the divisor
never approaches zero (zen §3). A ``max_exposure_pct`` cap is a redundant
safety net on total book exposure.
"""

from __future__ import annotations

from collections.abc import Sequence

_MAX_EXPOSURE_PCT = 25.0  # hard cap on total book exposure if all tiers fill


def equal_risk_allocations(
    entries: Sequence[float],
    stop: float,
    *,
    weights: Sequence[float] | None = None,
) -> list[float]:
    """Allocation % per tier (summing to ~100), by equal-risk notional weight.

    ``weights`` defaults to equal; passing e.g. ``[0.4, 0.3, 0.3]`` lets a
    caller bias risk toward shallower tiers (decision #4 — configurable,
    equal default). Raises ``ValueError`` if any ``entry <= stop`` (caller
    must filter via the ladder first).
    """
    n = len(entries)
    if n == 0:
        return []
    q = list(weights) if weights is not None else [1.0 / n] * n
    if len(q) != n:
        raise ValueError(f"weights length {len(q)} != entries length {n}")

    notional = []
    for qi, e in zip(q, entries, strict=True):
        risk_per_share = e - stop
        if risk_per_share <= 0:
            raise ValueError(f"entry {e} <= stop {stop}; filter via ladder before sizing")
        notional.append(qi * e / risk_per_share)
    total = sum(notional)
    if total <= 0:
        return []
    return [100.0 * no / total for no in notional]


def suggested_size_pct(
    entries: Sequence[float],
    stop: float,
    risk_budget_pct: float,
    *,
    weights: Sequence[float] | None = None,
    max_exposure_pct: float = _MAX_EXPOSURE_PCT,
) -> float:
    """Total book exposure % if ALL tiers fill, given a risk budget.

    ``risk_budget_pct`` is the % of book risked to the stop when fully
    filled. Exposure = ``risk_budget_pct * Σ q_i * E_i/(E_i - S)``, capped at
    ``max_exposure_pct`` (redundant guard against oversizing).
    """
    n = len(entries)
    if n == 0:
        return 0.0
    q = list(weights) if weights is not None else [1.0 / n] * n
    multiplier = 0.0
    for qi, e in zip(q, entries, strict=True):
        risk_per_share = e - stop
        if risk_per_share <= 0:
            raise ValueError(f"entry {e} <= stop {stop}; filter via ladder before sizing")
        multiplier += qi * e / risk_per_share
    return min(risk_budget_pct * multiplier, max_exposure_pct)


def blended_entry(entries: Sequence[float], allocations_pct: Sequence[float]) -> float:
    """Allocation-weighted average entry price."""
    if not entries:
        return 0.0
    total = sum(allocations_pct)
    if total <= 0:
        return sum(entries) / len(entries)
    return sum(e * a for e, a in zip(entries, allocations_pct, strict=True)) / total


__all__ = ["blended_entry", "equal_risk_allocations", "suggested_size_pct"]
