"""Magic Formula cohort ranker — Greenblatt-style multi-factor value+quality.

Operates on the daily thematic candidate basket (5-15 names spanning 2-4
themes) and assigns each survivor a relative rank 1..N. Lower is better.

Composition:
- Value factors (4, "lower is cheaper"): P/E, EV/EBITDA, P/S, FCFF yield⁻¹
  (we use FCFF yield % ascending=False — higher yield = cheaper)
- Quality factors (2, "higher is better"): ROIC, ROE
- Health gate (drop pre-rank): EBIT > 0 AND net_debt / EBIT < 5
- Aggregation: rank-sum across the 6 metrics, then dense re-rank to 1..N

Per-day cohort intentionally mixes themes — operator decision is which
1-2 names to cherry-pick across the full daily basket, so the comparison
universe is the basket itself.

Small-cohort guard: when fewer than 3 survivors pass the health gate, all
ranks return NaN (n=2 rank is binary, n=1 is meaningless).
"""

from __future__ import annotations

import math
from typing import Any, TypeGuard

import numpy as np
import pandas as pd

_HEALTH_GATE_MAX_LEVERAGE = 5.0
_MIN_COHORT_FOR_RANKING = 3

_VALUE_ASCENDING_LOWER_BETTER = ["valuation_pe", "valuation_ev_ebitda", "valuation_ps"]
_HIGHER_BETTER_METRICS = ["fcff_yield_pct", "roic_pct", "roe_pct"]


def _is_finite_number(v: Any) -> TypeGuard[float | int]:
    if v is None:
        return False
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def compute_ebit_ttm(features: dict[str, Any]) -> float | None:
    """EBIT proxy — SimFin's Operating Income (Loss) TTM."""
    ebit = features.get("operating_income_ttm")
    if not _is_finite_number(ebit):
        return None
    return float(ebit)


def compute_net_debt(features: dict[str, Any]) -> float | None:
    """Net debt = LT debt + ST debt − cash. None if any component missing."""
    ltd = features.get("long_term_debt")
    std = features.get("short_term_debt")
    cash = features.get("cash_and_equivalents")
    if not (_is_finite_number(ltd) and _is_finite_number(std) and _is_finite_number(cash)):
        return None
    return float(ltd) + float(std) - float(cash)


def compute_ev_ebitda(features: dict[str, Any], *, market_cap: float) -> float | None:
    """EV/EBITDA = (market_cap + net_debt) / (EBIT + D&A)."""
    ebit = compute_ebit_ttm(features)
    net_debt = compute_net_debt(features)
    da = features.get("da_ttm")
    if ebit is None or net_debt is None or not _is_finite_number(da):
        return None
    ebitda = ebit + float(da)
    if ebitda <= 0:
        return None
    if not _is_finite_number(market_cap):
        return None
    return (float(market_cap) + net_debt) / ebitda


def compute_roic(features: dict[str, Any]) -> float | None:
    """ROIC = EBIT / invested_capital, where invested = total_debt + equity − cash.

    Returned as a percentage (0..100+). Invested capital must be > 0.
    """
    ebit = compute_ebit_ttm(features)
    equity = features.get("total_equity")
    ltd = features.get("long_term_debt")
    std = features.get("short_term_debt")
    cash = features.get("cash_and_equivalents")
    if ebit is None or not (
        _is_finite_number(equity)
        and _is_finite_number(ltd)
        and _is_finite_number(std)
        and _is_finite_number(cash)
    ):
        return None
    invested = float(ltd) + float(std) + float(equity) - float(cash)
    if invested <= 0:
        return None
    return 100.0 * ebit / invested


def compute_roe(features: dict[str, Any]) -> float | None:
    """ROE = net_income / total_equity, as a percentage. Equity must be > 0."""
    ni = features.get("net_income_ttm")
    equity = features.get("total_equity")
    if not _is_finite_number(ni) or not _is_finite_number(equity):
        return None
    if float(equity) <= 0:
        return None
    return 100.0 * float(ni) / float(equity)


def passes_health_gate(features: dict[str, Any]) -> bool:
    """EBIT > 0 AND net_debt / EBIT < 5 (or net cash position).

    Conservative: missing data → fail closed. The gate exists to drop
    "zombie" firms before ranking pollutes the cohort.
    """
    ebit = compute_ebit_ttm(features)
    if ebit is None or ebit <= 0:
        return False
    net_debt = compute_net_debt(features)
    if net_debt is None:
        return False
    if net_debt <= 0:
        # Net cash position — no leverage concern.
        return True
    return (net_debt / ebit) < _HEALTH_GATE_MAX_LEVERAGE


def compute_cohort_rank(df: pd.DataFrame) -> pd.Series:
    """Compute Magic Formula rank for a candidate basket.

    Input ``df`` must contain these columns (NaN allowed per row):
    ``valuation_pe``, ``valuation_ev_ebitda``, ``valuation_ps``,
    ``fcff_yield_pct``, ``roic_pct``, ``roe_pct``,
    ``magic_formula_health_pass``.

    Returns: integer rank Series aligned to ``df.index``. NaN for rows that
    failed the health gate. NaN for ALL rows when survivors < 3 (small-cohort
    guard — rank-sum at n=2 is binary, no information).
    """
    n_rows = len(df)
    out = pd.Series(np.full(n_rows, np.nan), index=df.index, dtype=float)
    if n_rows == 0:
        return out

    health_mask = df["magic_formula_health_pass"].astype(bool)
    survivors = df[health_mask]
    if len(survivors) < _MIN_COHORT_FOR_RANKING:
        return out

    # Cheaper-is-better mults — ascending=True so lower value gets rank 1.
    # Higher-is-better metrics — ascending=False so higher value gets rank 1.
    # ``na_option='bottom'`` pushes missing values to the worst rank, so a
    # candidate with NaN P/E still appears in the cohort but loses on that
    # axis (consistent with "we can't verify this is cheap").
    rank_components = []
    for col in _VALUE_ASCENDING_LOWER_BETTER:
        rank_components.append(
            survivors[col].rank(method="average", ascending=True, na_option="bottom")
        )
    for col in _HIGHER_BETTER_METRICS:
        rank_components.append(
            survivors[col].rank(method="average", ascending=False, na_option="bottom")
        )
    rank_sum: pd.Series = sum(rank_components)  # pyright: ignore[reportAssignmentType]
    # Dense re-rank — collapses fractional rank sums into contiguous 1..N integers.
    dense = rank_sum.rank(method="dense", ascending=True).astype(int)

    out.loc[survivors.index] = dense.astype(float)
    return out


def is_top_quartile(*, rank: float, cohort_n: int) -> bool:
    """True if rank ≤ ceil(cohort_n / 4) — Greenblatt's classic top-quartile gate.

    Used by the weighted-score composer as a boolean "valuation strong"
    proxy. Returns False on NaN rank (cohort too small / health-gate fail).
    """
    if rank is None or not _is_finite_number(rank):
        return False
    if cohort_n < _MIN_COHORT_FOR_RANKING:
        return False
    threshold = max(1, math.ceil(cohort_n / 4))
    return int(rank) <= threshold


__all__ = [
    "compute_cohort_rank",
    "compute_ebit_ttm",
    "compute_ev_ebitda",
    "compute_net_debt",
    "compute_roe",
    "compute_roic",
    "is_top_quartile",
    "passes_health_gate",
]
