"""yfinance-backed PriceFeed — interim + fallback live-price source for the
live-market execution engine (INC-2a).

Saxo SIM serves NO stock market data (NoAccess) and the current Polygon plan
serves no current-session data, so the live-market E/TP engine's price signal
comes from yfinance (~1 min lag on liquid US names, keyless). Source-agnostic by
the ``PriceFeed`` Protocol: a Saxo streaming feed (INC-2b, once the linked live
account carries the entitlement) becomes primary and this stays as a fallback +
test double. Price-source probe: see
``docs/research/live_market_execution_model_design_2026_08_05.md``.

INERT: no daemon caller wires this in yet (INC-5 supplies the uic->ticker
resolver from live positions and calls ``run_live_exits``).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import TYPE_CHECKING

from broker_contract.price_feed import PricePoint

if TYPE_CHECKING:
    from alphalens_pipeline.data.alt_data.yfinance_client import YFinanceClient


class YfinancePriceFeed:
    """A ``PriceFeed`` (structural) reading the latest trade price per uic.

    Fetch-on-``latest()`` — no cache — for a small managed set polled per ~45s
    tick. ``latest()`` returns ``None`` (the engine's stream-health veto) when
    the uic has no ticker, the fetch fails / returns NaN (halt / thin name), or
    the price is non-positive. ``fast_info.last_price`` exposes no tick
    timestamp, so freshness is best-effort (~1 min observed); real
    ``LastUpdated`` staleness gating arrives with the Saxo stream (INC-2b).
    """

    def __init__(
        self,
        *,
        resolve_ticker: Callable[[int], str | None],
        yf_client: YFinanceClient | None = None,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._resolve_ticker = resolve_ticker
        if yf_client is None:
            from alphalens_pipeline.data.alt_data.yfinance_client import (
                get_default_yfinance_client,
            )

            yf_client = get_default_yfinance_client()
        self._yf = yf_client
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))

    def latest(self, uic: int) -> PricePoint | None:
        ticker = self._resolve_ticker(uic)
        if not ticker:
            return None
        price = self._yf.last_price(ticker)
        # last_price already maps NaN -> None; guard non-positive defensively.
        if price is None or price <= 0.0:
            return None
        return PricePoint(uic=uic, price=float(price), asof=self._clock())
