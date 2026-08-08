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
import logging
import math
from collections.abc import Callable
from typing import TYPE_CHECKING

from broker_contract.price_feed import PricePoint

if TYPE_CHECKING:
    from alphalens_pipeline.data.alt_data.yfinance_client import YFinanceClient

_logger = logging.getLogger(__name__)


class YfinancePriceFeed:
    """A ``PriceFeed`` (structural) reading the latest trade price per uic.

    UNWIRED: no daemon caller uses this feed (see module docstring — the Saxo
    LIVE stream is the wired source). Kept as a fallback + test double.

    Fetch-on-``latest()`` — no cache — for a small managed set polled per ~45s
    tick. ``latest()`` returns ``None`` (the engine's stream-health veto) when
    the uic has no ticker, the fetch fails / returns NaN (halt / thin name), or
    the price is non-positive. ``fast_info.last_price`` exposes no tick
    timestamp, so every ``PricePoint`` this feed produces carries
    ``event_time=None`` and :func:`broker_contract.price_feed.is_fresh`
    therefore ALWAYS vetoes it — this feed can never drive a live order.
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
        try:
            price = self._yf.last_price(ticker)
        except Exception:
            # Any fetch failure degrades to the veto (do-not-fire); a raising
            # non-canonical client must never propagate into a market-order tick.
            _logger.warning(
                "yfinance last_price failed for %s (uic %s)", ticker, uic, exc_info=True
            )
            return None
        # The canonical client maps NaN -> None; guard NaN / inf / non-positive
        # defensively so a non-canonical injected client cannot produce a bad
        # PricePoint that reaches a market-order decision.
        if price is None or not math.isfinite(price) or price <= 0.0:
            return None
        now = self._clock()
        return PricePoint(
            uic=uic,
            bid=float(price),
            ask=float(price),
            # fast_info.last_price publishes NO tick timestamp. Reporting None
            # (rather than stamping `now`) makes this feed structurally unable
            # to pass is_fresh, which is the correct outcome: it is a last trade
            # of unverifiable age, not an executable quote.
            event_time=None,
            received_at=now,
            source="yfinance-last",
        )
