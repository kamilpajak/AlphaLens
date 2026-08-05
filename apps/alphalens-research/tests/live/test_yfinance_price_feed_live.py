"""Live yfinance PriceFeed probe — opt-in via YFINANCE_LIVE_TEST=1.

Shape-only, NEVER values: builds a real ``YfinancePriceFeed`` over the default
yfinance client (no fake), resolves uic 211 -> AAPL, and asserts ``latest(211)``
returns a ``PricePoint`` with a positive price and a tz-aware UTC ``asof`` within
the last few minutes. A ``None`` (market closed / Yahoo hiccup) is TRANSIENT
(inconclusive, skipped by the >50% gate), not a shape break.

Reuses the existing yfinance live flag + the ``tests.live`` probe harness, so a
default ``unittest discover`` (no env flag) collects-but-skips it — NON-gating.

    YFINANCE_LIVE_TEST=1 .venv/bin/python -m unittest tests.live.test_yfinance_price_feed_live -v
"""

from __future__ import annotations

import datetime as dt
import os
import unittest

from tests.live import PermanentProbeError, TransientProbeError, run_probes

_LIVE = os.environ.get("YFINANCE_LIVE_TEST") == "1"
_UIC = 211  # arbitrary uic mapped to a liquid mega-cap for the probe
_TICKER = "AAPL"  # liquid, never delisted, always quotes during XNYS hours
_MAX_ASOF_AGE = dt.timedelta(minutes=5)  # the clock stamp must be recent


def _classify(exc: Exception) -> Exception:
    msg = str(exc).lower()
    if "429" in msg or "timeout" in msg or "timed out" in msg or "connection" in msg:
        return TransientProbeError(str(exc))
    return PermanentProbeError(str(exc))


@unittest.skipUnless(_LIVE, "set YFINANCE_LIVE_TEST=1 to run the live yfinance PriceFeed probe")
class TestYfinancePriceFeedLive(unittest.TestCase):
    def test_latest_returns_fresh_pricepoint_shape(self):
        from alphalens_pipeline.brokers.automanager.yfinance_price_feed import (
            YfinancePriceFeed,
        )
        from broker_contract.price_feed import PricePoint

        def _probe() -> None:
            feed = YfinancePriceFeed(resolve_ticker={_UIC: _TICKER}.get)
            try:
                pt = feed.latest(_UIC)
            except Exception as exc:
                raise _classify(exc) from exc

            # None -> market closed / Yahoo blip: inconclusive, not a shape break.
            if pt is None:
                raise TransientProbeError(f"latest({_UIC}) returned None (market closed / blip)")

            if not isinstance(pt, PricePoint):
                raise PermanentProbeError(
                    f"latest({_UIC}) is not a PricePoint: {type(pt).__name__}"
                )
            if pt.uic != _UIC:
                raise PermanentProbeError(f"PricePoint.uic {pt.uic!r} != requested {_UIC!r}")
            if not isinstance(pt.price, float) or pt.price <= 0:
                raise PermanentProbeError(f"PricePoint.price not a positive float: {pt.price!r}")
            if pt.asof.tzinfo is None:
                raise PermanentProbeError("PricePoint.asof is not tz-aware")
            age = dt.datetime.now(dt.UTC) - pt.asof
            if age > _MAX_ASOF_AGE:
                raise PermanentProbeError(f"PricePoint.asof is stale ({age})")

        run_probes(self, {f"{_TICKER}/latest": _probe}, label="yfinance-price-feed")


if __name__ == "__main__":
    unittest.main()
