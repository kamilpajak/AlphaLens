"""Layer 4 signal 2 — FCFF yield + sector-percentile (paradigm #13 reuse).

Wraps the pure scorer primitives from :mod:`alphalens_research.scorers.fcff_yield`
(``compute_fcff``, ``compute_ev``, ``impute_fcff``, ``effective_fcff``,
``compute_fcff_yield``) to derive a single FCFF/EV yield (%) per ticker, then
ranks within an industry peer cohort.

Returned shape: ``{"yield_pct": float | None, "sector_percentile": float | None}``.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable

from alphalens_research.scorers.fcff_yield import (
    compute_ev,
    compute_fcff,
    compute_fcff_yield,
    effective_fcff,
    impute_fcff,
)
from alphalens_research.thematic.screening._common import clamp_tax, percentile_rank

logger = logging.getLogger(__name__)


def compute_fcff_yield_pct(features: dict | None) -> float | None:
    """Convert an SimFin features dict to a single FCFF/EV yield in percent.

    Returns ``None`` whenever any required input is missing or yields an
    ill-defined ratio (negative EV, non-positive effective FCFF, etc.).
    """
    if not features:
        return None
    try:
        ocf = features.get("ocf_ttm")
        capex = features.get("capex_ttm")
        interest = features.get("interest_expense_ttm") or 0.0
        tax = clamp_tax(features.get("tax_rate"))
        revenue = features.get("revenue_ttm")
        fcf_margin = features.get("fcf_margin_5y_median")
        price = features.get("price")
        shares = features.get("shares_outstanding")
        ltd = features.get("long_term_debt") or 0.0
        std = features.get("short_term_debt") or 0.0
        cash = features.get("cash_and_equivalents") or 0.0
    except AttributeError:
        return None

    if any(v is None for v in (ocf, capex, tax, price, shares)):
        return None

    try:
        fcff_actual = compute_fcff(
            ocf_ttm=float(ocf),
            capex_ttm=float(capex),
            interest_expense_ttm=float(interest),
            tax_rate=float(tax),
        )
    except (ValueError, TypeError):
        return None

    fcff_imputed = impute_fcff(
        revenue_ttm=float(revenue) if revenue is not None else float("nan"),
        fcf_margin_5y_median=float(fcf_margin) if fcf_margin is not None else None,
    )
    fcff_eff = effective_fcff(fcff_actual=fcff_actual, fcff_imputed=fcff_imputed)
    if fcff_eff is None:
        return None

    try:
        ev = compute_ev(
            price=float(price),
            shares_outstanding=float(shares),
            long_term_debt=float(ltd),
            short_term_debt=float(std),
            cash_and_equivalents=float(cash),
        )
    except (ValueError, TypeError):
        return None

    ratio = compute_fcff_yield(fcff_effective=fcff_eff, ev=ev)
    if ratio is None:
        return None
    return float(ratio) * 100.0


def score_fcff(
    *,
    ticker: str,
    asof: dt.date,
    peers: list[str],
    feature_fetcher: Callable[[str, dt.date], dict | None],
) -> dict[str, float | None]:
    """Rank ``ticker``'s FCFF yield within its industry ``peers`` cohort.

    ``feature_fetcher`` should mirror
    :meth:`EdgarFundamentalsStore.ev_fcff_features_as_of` — called once per
    ticker (candidate + each peer that participates in percentile).
    """
    candidate_features = feature_fetcher(ticker, asof)
    candidate_yield = compute_fcff_yield_pct(candidate_features)
    if candidate_yield is None:
        return {"yield_pct": None, "sector_percentile": None}

    peer_yields: list[float] = []
    for p in peers:
        if p.upper() == ticker.upper():
            continue
        py = compute_fcff_yield_pct(feature_fetcher(p, asof))
        if py is not None:
            peer_yields.append(py)

    percentile = percentile_rank(candidate_yield, peer_yields)
    return {"yield_pct": candidate_yield, "sector_percentile": percentile}


__all__ = ["compute_fcff_yield_pct", "score_fcff"]
