"""Shared broker-free VWAP-anchor / bar-fetch primitives.

These are the price-path replay building blocks consumed by the surviving
broker-free feedback engines — the ladder replay
(:mod:`alphalens_pipeline.feedback.ladder_backfill`) and the population monitor
(:mod:`alphalens_pipeline.feedback.population_ladder_monitor`). They were
formerly housed in ``shadow_return.py`` (deleted with the broker chain); the
arrival opening-window VWAP arithmetic, the implausible-move guard threshold,
the holding-horizon constant and the canonical Polygon bar fetcher are all
broker-agnostic, so they live here as the single anchor-arithmetic source.

None of these primitives reads any paper ledger / broker — they take a ticker
and a UTC window and return Polygon minute aggregates (or a VWAP over them).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from typing import Any

# Opening window (minutes from the session open) over which the arrival /
# horizon VWAP is taken. 30 min damps opening-auction noise vs the single open
# print; cheap to retune (one constant).
ARRIVAL_VWAP_WINDOW_MIN = 30

# Holding horizon, in trading sessions, between the arrival anchor and the
# exit anchor. A single global constant keeps the metric homogeneous across
# rows; a per-plan ``order_ttl_days`` variant is a possible refinement but
# would make the cross-row comparison heterogeneous.
HOLDING_HORIZON_TRADING_DAYS = 5

# Above this absolute move the 5-session window almost certainly spans a split
# / special dividend (bars are adjusted=false) rather than a real return — skip
# and flag rather than stamp a corrupted value.
IMPLAUSIBLE_RETURN_THRESHOLD = 0.60

# Default look-back window (calendar days) for the nightly sweep. A brief matures
# ~6-8 calendar days after its build (5 trading sessions + the (D-1) dating), so
# 14 days gives margin to re-price after VPS downtime or a rate-limit timeout.
# Duplicated CLI-side as ``_DEFAULT_LOOKBACK_DAYS`` (typer evaluates Option
# defaults at import time and the CLI lazy-imports this module) — parity pinned
# by ``test_cli_lookback_default_in_sync_with_module`` (against ``bar_window``).
DEFAULT_LOOKBACK_DAYS = 14

# A bar (dict) → ticker, window start, window end → list of Polygon agg bars.
BarFetch = Callable[[str, dt.datetime, dt.datetime], Sequence[dict[str, Any]]]


def _window_vwap(
    bars: Sequence[dict[str, Any]],
    start: dt.datetime,
    end: dt.datetime,
) -> float | None:
    """Volume-weighted close over bars whose start ``t`` is in ``[start, end)``.

    Returns ``None`` when no bar falls in the window. Degrades to the simple
    mean of closes when total volume is zero (an all-zero-volume thin-name
    window) so a VWAP is still produced rather than a divide-by-zero.
    """
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    pairs: list[tuple[float, float]] = []
    for bar in bars:
        t = bar.get("t")
        close = bar.get("c")
        if t is None or close is None or not (start_ms <= t < end_ms):
            continue
        pairs.append((float(close), float(bar.get("v") or 0.0)))
    if not pairs:
        return None
    total_vol = sum(v for _, v in pairs)
    if total_vol == 0:
        return sum(c for c, _ in pairs) / len(pairs)
    return sum(c * v for c, v in pairs) / total_vol


def _default_bar_fetch(
    ticker: str, start: dt.datetime, end: dt.datetime
) -> Sequence[dict[str, Any]]:
    """Production bar source: the canonical Polygon client minute aggregates."""
    from alphalens_pipeline.data.alt_data.polygon_client import get_default_polygon_client

    return get_default_polygon_client().get_agg_range(ticker=ticker, start=start, end=end)


__all__ = [
    "ARRIVAL_VWAP_WINDOW_MIN",
    "DEFAULT_LOOKBACK_DAYS",
    "HOLDING_HORIZON_TRADING_DAYS",
    "IMPLAUSIBLE_RETURN_THRESHOLD",
    "BarFetch",
    "_default_bar_fetch",
    "_window_vwap",
]
