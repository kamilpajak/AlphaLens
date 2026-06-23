"""Per-arm execution-cost haircut for the entry-grid (Faza 0).

Resting-limit arms keep their touch price (price-improvement preserved, 0 haircut).
Always-fill arms (market_at_arrival, vwap_arrival) pay a half-spread + market-impact
haircut, charged ONE-WAY (entry only) in return space. All constants are an
UNVALIDATED proxy — no real fills exist post-ADR-0012; the offline script reports
pre- and post-haircut so the sensitivity is visible.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

RESTING_LIMIT_ARMS: frozenset[str] = frozenset({"baseline", "narrow_tiers", "single_at_close"})
ALWAYS_FILL_ARMS: frozenset[str] = frozenset({"market_at_arrival", "vwap_arrival"})

# (lower_bound_usd, impact_bps) descending; first match wins.
_MCAP_BUCKETS: tuple[tuple[float, float], ...] = (
    (1e11, 2.0),  # mega
    (1e10, 5.0),  # mid
    (1e9, 12.0),  # small
)
_MICRO_IMPACT_BPS = 25.0
_DEFAULT_IMPACT_BPS = 12.0  # conservative when mcap unknown (NOT cheapest)
_DEFAULT_HALF_SPREAD_BPS = 25.0  # conservative when the bar proxy is unusable


def impact_bps_for_mcap(market_cap: float | None) -> float:
    if (
        market_cap is None
        or (isinstance(market_cap, float) and math.isnan(market_cap))
        or market_cap <= 0.0
    ):
        return _DEFAULT_IMPACT_BPS
    for lower, bps in _MCAP_BUCKETS:
        if market_cap >= lower:
            return bps
    return _MICRO_IMPACT_BPS


def half_spread_bps_from_bar(first_bar: Mapping[str, Any] | None) -> float:
    if not first_bar:
        return _DEFAULT_HALF_SPREAD_BPS
    try:
        h = float(first_bar["h"])
        low = float(first_bar["l"])
    except (KeyError, TypeError, ValueError):
        return _DEFAULT_HALF_SPREAD_BPS
    if math.isnan(h) or math.isnan(low) or h < low:
        return _DEFAULT_HALF_SPREAD_BPS
    mid = (h + low) / 2.0
    if mid <= 0.0:
        return _DEFAULT_HALF_SPREAD_BPS
    return 10_000.0 * 0.5 * (h - low) / mid


def arm_haircut_bps(
    arm: str, *, market_cap: float | None, first_rth_bar: Mapping[str, Any] | None
) -> float:
    if arm in RESTING_LIMIT_ARMS:
        return 0.0
    if arm in ALWAYS_FILL_ARMS:
        return half_spread_bps_from_bar(first_rth_bar) + impact_bps_for_mcap(market_cap)
    raise ValueError(f"unknown arm: {arm!r}")


def apply_haircut_to_excess(
    raw_excess: float | None,
    *,
    arm: str,
    market_cap: float | None,
    first_rth_bar: Mapping[str, Any] | None,
) -> float | None:
    if raw_excess is None:
        return None
    return (
        raw_excess
        - arm_haircut_bps(arm, market_cap=market_cap, first_rth_bar=first_rth_bar) / 10_000.0
    )
