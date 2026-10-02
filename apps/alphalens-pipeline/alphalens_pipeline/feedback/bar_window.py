"""Shared broker-free bar-fetch primitive and replay guard constants.

These are the price-path replay building blocks consumed by the surviving
broker-free feedback engine — the population monitor
(:mod:`alphalens_pipeline.feedback.population_ladder_monitor`). They were
formerly housed in ``shadow_return.py`` (deleted with the broker chain); the
implausible-move guard threshold, the holding-horizon constant and the
canonical Polygon bar fetcher are all broker-agnostic, so they live here.

The arrival opening-window VWAP arithmetic moved to
:mod:`alphalens_pipeline.market.bars` — it is shared with the selection tier,
which must not import this measurement-tier package.

None of these primitives reads any paper ledger / broker — they take a ticker
and a UTC window and return Polygon minute aggregates.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from typing import Any

# Holding horizon, in trading sessions, between the arrival anchor and the
# exit anchor. A single global constant keeps the metric homogeneous across
# rows; a per-plan ``order_ttl_days`` variant is a possible refinement but
# would make the cross-row comparison heterogeneous.
HOLDING_HORIZON_TRADING_DAYS = 5

# Above this absolute move the 5-session window almost certainly spans a split
# / special dividend (bars are adjusted=false) rather than a real return — skip
# and flag rather than stamp a corrupted value.
IMPLAUSIBLE_RETURN_THRESHOLD = 0.60

# The fixed 5-session maturity window (calendar days). A brief matures ~6-8
# calendar days after its build (5 trading sessions + the (D-1) dating), so 14
# days gives margin. Referenced by the population monitor as the contrast against
# its own much larger ``MONITOR_LOOKBACK_DAYS``. (No longer fed to a CLI option:
# the per-decision ladder replay that used it was removed with the click ledger,
# #465; the population monitor uses its own lookback.)
DEFAULT_LOOKBACK_DAYS = 14

# A bar (dict) → ticker, window start, window end → list of Polygon agg bars.
BarFetch = Callable[[str, dt.datetime, dt.datetime], Sequence[dict[str, Any]]]


def _default_bar_fetch(
    ticker: str, start: dt.datetime, end: dt.datetime
) -> Sequence[dict[str, Any]]:
    """Production bar source: the canonical Polygon client minute aggregates."""
    from alphalens_pipeline.data.alt_data.polygon_client import get_default_polygon_client

    return get_default_polygon_client().get_agg_range(ticker=ticker, start=start, end=end)


__all__ = [
    "DEFAULT_LOOKBACK_DAYS",
    "HOLDING_HORIZON_TRADING_DAYS",
    "IMPLAUSIBLE_RETURN_THRESHOLD",
    "BarFetch",
    "_default_bar_fetch",
]
