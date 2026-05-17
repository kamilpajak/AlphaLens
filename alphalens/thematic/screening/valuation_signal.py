"""Layer 4 signal 3 — SimFin valuation panel.

Per design memo §12 (locked 2026-05-16) ships with **4 multiples** derived
from the SimFin features dict (gross margin + PEG deferred — SimFin schema
would need income-statement loader extension):

- ``P/S`` = market_cap / revenue_ttm
- ``P/E`` = market_cap / net_income_ttm (None if NI ≤ 0)
- ``EV/Rev`` = (market_cap + net_debt) / revenue_ttm
- ``FCF margin`` = effective FCFF / revenue_ttm (uses 5y median when actual
  is non-positive, mirroring paradigm #13 imputation rule)

The composite sector percentile is the average of the inverse-multiple
percentiles (cheaper candidate → higher composite rank). FCF margin is
treated as a quality proxy and contributes with its raw direction
(higher margin → higher rank).
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from collections.abc import Callable
from statistics import mean

from alphalens.screeners.ev_fcff_yield.scorer import (
    compute_fcff,
    effective_fcff,
    impute_fcff,
)
from alphalens.thematic.screening._common import clamp_tax, percentile_rank

logger = logging.getLogger(__name__)


def _safe_get(d: dict, key: str) -> float | None:
    """Return ``d[key]`` cast to float, or None on missing/NaN/non-numeric."""
    val = d.get(key) if d else None
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return f


def _market_cap(features: dict) -> float | None:
    p = _safe_get(features, "price")
    s = _safe_get(features, "shares_outstanding")
    if p is None or s is None or p <= 0 or s <= 0:
        return None
    return p * s


def _effective_fcf_margin(features: dict) -> float | None:
    revenue = _safe_get(features, "revenue_ttm")
    if revenue is None or revenue <= 0:
        return None
    ocf = _safe_get(features, "ocf_ttm")
    capex = _safe_get(features, "capex_ttm")
    interest = _safe_get(features, "interest_expense_ttm") or 0.0
    tax = _safe_get(features, "tax_rate")
    if ocf is None or capex is None or tax is None:
        # Fall back directly to the 5y median when we can't compute current FCFF.
        return _safe_get(features, "fcf_margin_5y_median")
    try:
        actual = compute_fcff(
            ocf_ttm=ocf,
            capex_ttm=capex,
            interest_expense_ttm=interest,
            tax_rate=clamp_tax(tax),
        )
    except (ValueError, TypeError):
        return _safe_get(features, "fcf_margin_5y_median")
    imputed = impute_fcff(
        revenue_ttm=revenue, fcf_margin_5y_median=_safe_get(features, "fcf_margin_5y_median")
    )
    eff = effective_fcff(fcff_actual=actual, fcff_imputed=imputed)
    if eff is None:
        return None
    return eff / revenue


def compute_multiples(features: dict | None) -> dict[str, float | None]:
    """Return ``{pe, ps, ev_rev, fcf_margin}`` for the SimFin features dict.

    Each multiple is ``None`` when the inputs are missing or yield an
    undefined ratio (negative earnings for P/E, zero revenue for P/S, etc.).
    """
    out: dict[str, float | None] = {"pe": None, "ps": None, "ev_rev": None, "fcf_margin": None}
    if not features:
        return out
    mc = _market_cap(features)
    revenue = _safe_get(features, "revenue_ttm")
    ni = _safe_get(features, "net_income_ttm")

    if mc is not None and revenue is not None and revenue > 0:
        out["ps"] = mc / revenue
        net_debt = (
            (_safe_get(features, "long_term_debt") or 0.0)
            + (_safe_get(features, "short_term_debt") or 0.0)
            - (_safe_get(features, "cash_and_equivalents") or 0.0)
        )
        out["ev_rev"] = (mc + net_debt) / revenue
    if mc is not None and ni is not None and ni > 0:
        out["pe"] = mc / ni
    out["fcf_margin"] = _effective_fcf_margin(features)
    return out


def _inverse_percentile(value: float | None, peers: list[float]) -> float | None:
    """Cheaper-is-better percentile: rank ``-value`` against ``-peers``.

    Returns ``None`` if ``value`` is missing.
    """
    if value is None:
        return None
    return percentile_rank(-value, [-v for v in peers])


def _quality_percentile(value: float | None, peers: list[float]) -> float | None:
    """Higher-is-better percentile (used for FCF margin)."""
    if value is None:
        return None
    return percentile_rank(value, peers)


def score_valuation(
    *,
    ticker: str,
    asof: dt.date,
    peers: list[str],
    feature_fetcher: Callable[[str, dt.date], dict | None],
) -> dict[str, float | None]:
    """Compute the 4 multiples + a composite sector percentile.

    Composite = average of available per-metric percentiles. ``None`` when
    every multiple is missing (no input data resolved for the candidate).
    """
    cand_features = feature_fetcher(ticker, asof)
    cand_multiples = compute_multiples(cand_features)

    if all(v is None for v in cand_multiples.values()):
        return {**cand_multiples, "composite_sector_percentile": None}

    # Build peer multiples once.
    peer_multiples: list[dict] = []
    for p in peers:
        if p.upper() == ticker.upper():
            continue
        m = compute_multiples(feature_fetcher(p, asof))
        peer_multiples.append(m)

    per_metric_pctl: list[float] = []
    for metric in ("pe", "ps", "ev_rev"):
        peer_vals = [pm[metric] for pm in peer_multiples if pm[metric] is not None]
        pctl = _inverse_percentile(cand_multiples[metric], peer_vals)
        if pctl is not None:
            per_metric_pctl.append(pctl)
    peer_margins = [pm["fcf_margin"] for pm in peer_multiples if pm["fcf_margin"] is not None]
    margin_pctl = _quality_percentile(cand_multiples["fcf_margin"], peer_margins)
    if margin_pctl is not None:
        per_metric_pctl.append(margin_pctl)

    composite = mean(per_metric_pctl) if per_metric_pctl else None
    return {**cand_multiples, "composite_sector_percentile": composite}


__all__ = ["compute_multiples", "score_valuation"]
