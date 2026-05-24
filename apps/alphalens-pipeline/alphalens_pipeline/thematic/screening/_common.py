"""Shared helpers for the Layer 4 signal modules.

These were previously private to ``insider_signal`` / ``fcff_signal`` /
``valuation_signal`` with cross-module imports of underscore-prefixed
names. Promoting them to a dedicated module keeps the "underscore =
internal" convention honest and gives downstream callers (e.g. a future
sector-relative ranking layer) a single import surface.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable
from typing import Any

from alphalens_pipeline.scorers.fcff_yield import (
    _TAX_RATE_CEILING,
    _TAX_RATE_FLOOR,
)

# Permissive end of the literature range for institutional small/mid-cap
# filters (Hou-Xue-Zhang 2015 uses $50-200M; the thematic event-driven
# use case can accept smaller names but still needs to drop shells).
DEFAULT_MIN_PEER_MCAP_USD = 30_000_000.0
# Penny-stock floor — tiny float × tiny price can clear a $30M mcap on
# paper but the instrument is functionally untradable for the basket
# sizes the brief surfaces. Keeps peers economically comparable.
DEFAULT_MIN_PEER_PRICE_USD = 1.0


def percentile_rank(value: float, peers: list[float]) -> float:
    """Return the ``≤``-percentile of ``value`` within ``peers`` (0..100).

    Includes ``value`` itself in the cohort so a single-element cohort is
    always at the top. Empty peer list → 50.0 ("no information" midpoint).
    """
    if not peers:
        return 50.0
    cohort = peers if value in peers else [*peers, value]
    le_count = sum(1 for v in cohort if v <= value)
    return 100.0 * le_count / len(cohort)


def clamp_tax(value: float | None) -> float | None:
    """Clamp a tax rate to the paradigm #13 ``[0, 0.35]`` window.

    Returns ``None`` unchanged so callers can short-circuit on missing
    data. Bounds reused directly from
    :mod:`alphalens_pipeline.scorers.fcff_yield` so they stay in sync
    with the paradigm spec.
    """
    if value is None:
        return None
    if value < _TAX_RATE_FLOOR:
        return _TAX_RATE_FLOOR
    if value > _TAX_RATE_CEILING:
        return _TAX_RATE_CEILING
    return value


def _safe_float(value: object) -> float | None:
    """Return value as float, or None on missing/NaN/non-numeric."""
    if value is None:
        return None
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return f


def filter_peers_by_mcap_price(
    peers: list[str],
    *,
    feature_fetcher: Callable[[str, dt.date], dict[str, Any] | None] | None,
    asof: dt.date,
    min_mcap_usd: float = DEFAULT_MIN_PEER_MCAP_USD,
    min_price_usd: float = DEFAULT_MIN_PEER_PRICE_USD,
) -> list[str]:
    """Drop peers whose mcap or price falls below the cleanup-bundle floors.

    Peers without resolvable price + shares (no EDGAR cache hit) are
    dropped as well — they cannot anchor a percentile honestly.

    ``feature_fetcher=None`` returns ``peers`` unchanged: keeps the
    helper safely no-op when callers (notably tests) do not have a
    fetcher wired in. The orchestrator (``scorer.score_candidates``)
    always passes a fetcher; the pre-existing
    ``_build_feature_fetcher`` populates EDGAR cache for the whole
    universe before any signal runs, so this filter has zero extra HTTP
    cost.
    """
    if feature_fetcher is None:
        return peers
    out: list[str] = []
    for ticker in peers:
        features = feature_fetcher(ticker, asof)
        if not features:
            continue
        price = _safe_float(features.get("price"))
        shares = _safe_float(features.get("shares_outstanding"))
        if price is None or shares is None:
            continue
        if price < min_price_usd:
            continue
        if price * shares < min_mcap_usd:
            continue
        out.append(ticker)
    return out


__all__ = [
    "DEFAULT_MIN_PEER_MCAP_USD",
    "DEFAULT_MIN_PEER_PRICE_USD",
    "clamp_tax",
    "filter_peers_by_mcap_price",
    "percentile_rank",
]
