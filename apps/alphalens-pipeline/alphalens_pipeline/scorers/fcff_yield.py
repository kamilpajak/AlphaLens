"""Pure scoring functions for the EV/FCFF-yield value screener.

Decomposed into atomic pure functions so each one is testable in isolation
without vendor / network / cache fixtures. The integration layer that pulls
fundamentals from ``alphalens_pipeline.data.store.edgar_fundamentals.EdgarFundamentalsStore``
lives separately and composes these primitives at audit time.

Sign conventions:
- ``ocf`` positive when the firm generated operating cash.
- ``capex`` positive when the firm invested (EDGAR ``PaymentsToAcquirePropertyPlantAndEquipment``
  is already positive; legacy SimFin path used to flip the sign).
- ``interest_expense`` positive (cost to the firm).
- ``tax_rate`` in [0, 0.35], clamped on the caller side.
- ``fcff_actual`` positive when the firm has unlevered free cash flow.
- ``fcff_yield`` positive for value-attractive firms (high FCFF / EV).

The ranking is intentionally based on yield (not on g_implied) per
adversarial review verdict 2026-05-12: g_implied with fixed WACC is a
monotonic transformation of FCF/EV → ranking is identical. The module
exposes the yield directly to avoid false-precision framing.
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

from alphalens_pipeline.scorers._common import rank_zscore, winsorize

__all__ = [
    "compute_ev",
    "compute_fcff",
    "compute_fcff_yield",
    "effective_fcff",
    "impute_fcff",
    "rank_zscore",
    "score_ev_fcff_yield",
    "winsorize",
]

# Imputation is allowed only when both inputs are finite and the
# 5y FCF margin median is positive. Negative imputed FCFF → drop ticker.
_TAX_RATE_FLOOR = 0.0
_TAX_RATE_CEILING = 0.35


def compute_fcff(
    *,
    ocf_ttm: float,
    capex_ttm: float,
    interest_expense_ttm: float,
    tax_rate: float,
) -> float:
    """Unlevered Free Cash Flow (FCFF), TTM.

    Formula: ``FCFF = OCF + InterestExpense × (1 − τ) − Capex``.

    The interest tax-shield add-back is what makes FCFF *unlevered* — we
    remove the capital-structure effect that levered FCF (FCFE) carries.
    This neutralizes leverage bias in the cross-sectional ranking per
    adversarial review verdict 2026-05-12.

    Raises ``ValueError`` if ``tax_rate`` is outside [0, 0.35]. Caller is
    expected to clamp before calling.
    """
    if not (_TAX_RATE_FLOOR <= tax_rate <= _TAX_RATE_CEILING):
        raise ValueError(
            f"tax_rate {tax_rate} outside clamped range [{_TAX_RATE_FLOOR}, {_TAX_RATE_CEILING}]. "
            "Clamp on the caller side."
        )
    return ocf_ttm + interest_expense_ttm * (1.0 - tax_rate) - capex_ttm


def compute_ev(
    *,
    price: float,
    shares_outstanding: float,
    long_term_debt: float,
    short_term_debt: float,
    cash_and_equivalents: float,
) -> float:
    """Enterprise Value = MarketCap + LongTermDebt + ShortTermDebt − Cash.

    Standard Damodaran convention. Net debt is debt minus cash + short-term
    investments. Negative EV is possible (cash-rich firms with low market
    cap); caller should handle by dropping these rare cases or flagging
    them — this function returns the raw value.
    """
    market_cap = price * shares_outstanding
    net_debt = long_term_debt + short_term_debt - cash_and_equivalents
    return market_cap + net_debt


def impute_fcff(
    *,
    revenue_ttm: float,
    fcf_margin_5y_median: float | None,
) -> float | None:
    """Impute FCFF as ``Sales_TTM × median(quarterly FCF margin, 5y)``.

    Returns ``None`` when the 5y median is missing or non-positive — a
    firm whose 5y median FCF margin is negative is structurally
    cash-burning, not "temporarily distressed", and should be excluded.

    Per Gemini 3 Pro 2026-05-12: imputation cuts the hard 30% R2000
    attrition that a strict ``FCFF<0 → drop`` rule would cause. Only true
    zombies (both current and historical FCF margins ≤ 0) get dropped.
    """
    if fcf_margin_5y_median is None:
        return None
    if not np.isfinite(fcf_margin_5y_median):
        return None
    if fcf_margin_5y_median <= 0:
        return None
    if not np.isfinite(revenue_ttm) or revenue_ttm <= 0:
        return None
    return revenue_ttm * fcf_margin_5y_median


def effective_fcff(
    *,
    fcff_actual: float,
    fcff_imputed: float | None,
) -> float | None:
    """Pick FCFF for yield computation.

    Decision tree:
    - if ``fcff_actual > 0`` → return ``fcff_actual``.
    - else if ``fcff_imputed`` is positive → return ``fcff_imputed``.
    - else → return ``None`` (drop ticker).

    Imputation only fires when actual is non-positive. This ensures we
    don't replace good current FCFF with stale historical-margin estimates.
    """
    if np.isfinite(fcff_actual) and fcff_actual > 0:
        return fcff_actual
    if fcff_imputed is None:
        return None
    if not np.isfinite(fcff_imputed) or fcff_imputed <= 0:
        return None
    return fcff_imputed


def compute_fcff_yield(
    *,
    fcff_effective: float | None,
    ev: float,
) -> float | None:
    """FCFF / EV.

    Returns ``None`` when ``fcff_effective`` is ``None`` or ``ev`` is
    non-positive (cash-rich firm with negative EV — yield is ill-defined,
    drop).
    """
    if fcff_effective is None:
        return None
    if not np.isfinite(ev) or ev <= 0:
        return None
    return fcff_effective / ev


def score_ev_fcff_yield(
    snapshots: Mapping[str, Mapping[str, float | None]],
    *,
    winsorize_pct: tuple[float, float] = (0.01, 0.99),
) -> pd.Series:
    """High-level: build the cross-sectional score Series for one rebalance.

    ``snapshots`` is a mapping ``ticker → snapshot dict`` with keys:
    ``ocf_ttm``, ``capex_ttm``, ``interest_expense_ttm``, ``tax_rate``,
    ``revenue_ttm``, ``fcf_margin_5y_median``, ``price``,
    ``shares_outstanding``, ``long_term_debt``, ``short_term_debt``,
    ``cash_and_equivalents``.

    Returns a ``pd.Series`` indexed by ticker with the winsorized
    cross-sectional z-score of FCFF yield. Tickers with non-computable
    yield (e.g. both FCFF actual and imputed ≤ 0, or EV ≤ 0) are dropped
    from the output, not kept as NaN — they failed the universe filter at
    this rebalance.

    Higher score = higher FCFF yield = more value-attractive (long-decile
    candidate).
    """
    yields: dict[str, float] = {}
    for ticker, snap in snapshots.items():
        ocf = snap.get("ocf_ttm")
        capex = snap.get("capex_ttm")
        interest = snap.get("interest_expense_ttm")
        tax = snap.get("tax_rate")
        rev = snap.get("revenue_ttm")
        margin = snap.get("fcf_margin_5y_median")
        price = snap.get("price")
        shares = snap.get("shares_outstanding")
        ltd = snap.get("long_term_debt")
        std = snap.get("short_term_debt")
        cash = snap.get("cash_and_equivalents")

        # Any missing required input → drop ticker at this rebalance.
        if any(
            v is None or not np.isfinite(v)
            for v in (
                ocf,
                capex,
                interest,
                tax,
                price,
                shares,
                ltd,
                std,
                cash,
            )
        ):
            continue

        tax_clamped = max(_TAX_RATE_FLOOR, min(_TAX_RATE_CEILING, float(tax)))
        fcff_actual = compute_fcff(
            ocf_ttm=float(ocf),
            capex_ttm=float(capex),
            interest_expense_ttm=float(interest),
            tax_rate=tax_clamped,
        )
        fcff_imputed = None
        if rev is not None and np.isfinite(rev):
            fcff_imputed = impute_fcff(
                revenue_ttm=float(rev),
                fcf_margin_5y_median=margin if margin is None else float(margin),
            )
        fcff_eff = effective_fcff(fcff_actual=fcff_actual, fcff_imputed=fcff_imputed)
        ev = compute_ev(
            price=float(price),
            shares_outstanding=float(shares),
            long_term_debt=float(ltd),
            short_term_debt=float(std),
            cash_and_equivalents=float(cash),
        )
        y = compute_fcff_yield(fcff_effective=fcff_eff, ev=ev)
        if y is None or not np.isfinite(y):
            continue
        yields[ticker] = y

    if not yields:
        return pd.Series(dtype=float)

    raw = pd.Series(yields, name="fcff_yield_zscore")
    winsorized = winsorize(raw, lower_pct=winsorize_pct[0], upper_pct=winsorize_pct[1])
    return rank_zscore(winsorized)
