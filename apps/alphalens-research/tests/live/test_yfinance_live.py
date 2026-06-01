"""Live yfinance probe — opt-in via YFINANCE_LIVE_TEST=1.

yfinance has NO canonical wrapper client (Yahoo is unauthenticated, ToS-grey),
so three independent raw seams each break on their own when Yahoo changes its
API or a ticker delists:
  - OHLCV daily bars  (``yfinance_cache._default_yfinance_fetcher`` — RAISES)
  - live market cap   (``verification.mcap_filter.fetch_mcap`` — SWALLOWS -> None)
  - next earnings date (``sources.earnings_calendar.fetch_next_earnings`` — SWALLOWS)

Shape-only: non-empty OHLCV frame with the exact lowercase columns + tz-naive
index; a positive-float live mcap; a ``dt.date`` next-earnings (or None between
cycles, which is inconclusive, not a failure). Never asserts the numbers.

Note: ``fetch_mcap`` / ``fetch_next_earnings`` swallow all exceptions and return
None by design, so the probe cannot distinguish a transient Yahoo blip from a
real shape break through them — a None mcap for a mega-cap is treated as a
PERMANENT signal (the seam is dead) and a None earnings as TRANSIENT (legitimate
between earnings cycles). Weekly + issue-on-failure, so a rare false page is
re-run, not merge-blocking.

    YFINANCE_LIVE_TEST=1 .venv/bin/python -m unittest tests.live.test_yfinance_live -v
"""

from __future__ import annotations

import datetime as dt
import os
import unittest

from tests.live import PermanentProbeError, TransientProbeError, run_probes

_LIVE = os.environ.get("YFINANCE_LIVE_TEST") == "1"
_TICKER = "AAPL"  # liquid mega-cap, never delisted, always has a live mcap
_OHLCV_COLUMNS = {"open", "high", "low", "close", "volume"}


def _classify(exc: Exception) -> Exception:
    msg = str(exc).lower()
    if "429" in msg or "timeout" in msg or "timed out" in msg or "connection" in msg:
        return TransientProbeError(str(exc))
    return PermanentProbeError(str(exc))


@unittest.skipUnless(_LIVE, "set YFINANCE_LIVE_TEST=1 to run the live yfinance probe")
class TestYfinanceLive(unittest.TestCase):
    def test_three_seams_return_expected_shapes(self):
        import pandas as pd

        def _ohlcv() -> None:
            from alphalens_pipeline.data.alt_data.yfinance_cache import _default_yfinance_fetcher

            end = dt.date.today()
            start = end - dt.timedelta(days=10)
            try:
                df = _default_yfinance_fetcher(_TICKER, start, end)
            except Exception as exc:  # this seam RAISES on error, so classify it
                raise _classify(exc) from exc
            if df.empty:
                raise PermanentProbeError("empty OHLCV frame for AAPL over a 10d window")
            if set(df.columns) != _OHLCV_COLUMNS:
                raise PermanentProbeError(f"unexpected OHLCV columns: {sorted(df.columns)}")
            if not isinstance(df.index, pd.DatetimeIndex) or df.index.tz is not None:
                raise PermanentProbeError("OHLCV index is not a tz-naive DatetimeIndex")

        def _mcap() -> None:
            from alphalens_pipeline.thematic.verification.mcap_filter import fetch_mcap

            mc = fetch_mcap(_TICKER, asof=None)  # live path -> fast_info.market_cap
            # SWALLOWS to None -> a None for a mega-cap means the live mcap seam
            # broke; treat as a permanent (loud) signal, not a flake.
            if mc is None or not isinstance(mc, float) or mc <= 0:
                raise PermanentProbeError(f"live mcap not a positive float: {mc!r}")

        def _earnings() -> None:
            from alphalens_pipeline.thematic.sources.earnings_calendar import fetch_next_earnings

            nxt = fetch_next_earnings(ticker=_TICKER, asof=dt.date.today())
            # The forward calendar is legitimately empty between cycles -> the
            # probe is inconclusive (transient), not failed (memo §3).
            if nxt is None:
                raise TransientProbeError("no forward earnings date (between cycles)")
            if not isinstance(nxt, dt.date):
                raise PermanentProbeError(f"earnings not a dt.date: {type(nxt).__name__}")

        run_probes(
            self,
            {"ohlcv": _ohlcv, "mcap": _mcap, "earnings": _earnings},
            label="yfinance",
        )


if __name__ == "__main__":
    unittest.main()
