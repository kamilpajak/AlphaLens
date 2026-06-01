"""Opt-in live probe for PolygonClient.get_agg_range (Track A v2 PR-3).

Hermetic tests with fabricated bars pass even when the feature is dead in prod,
because Polygon's free / Basic plan does NOT serve intraday minute aggregates
(it returns empty for current-session windows, and may gate historical intraday
behind a paid plan). Per ``feedback_live_probe_external_ingest_adapters``, this
probe fetches ONE real PAST-session opening window for a liquid ticker and
asserts non-empty — the only thing that catches the Basic-tier block before the
shadow-return job is wired on the VPS.

Run explicitly (never in CI; needs POLYGON_API_KEY + network):

    POLYGON_LIVE_TEST=1 .venv/bin/python -m unittest \
        tests.test_polygon_live -v
"""

from __future__ import annotations

import datetime as dt
import os
import unittest

_LIVE = os.environ.get("POLYGON_LIVE_TEST") == "1"


@unittest.skipUnless(_LIVE, "set POLYGON_LIVE_TEST=1 to run the live Polygon probe")
class TestPolygonAggRangeLive(unittest.TestCase):
    def test_past_session_opening_window_has_minute_bars(self):
        from alphalens_pipeline.data.alt_data.polygon_client import get_default_polygon_client
        from alphalens_pipeline.paper.calendar import previous_trading_day, session_open_utc

        # Anchor to a session a few trading days back so it is unambiguously
        # closed regardless of the time of day the probe runs.
        session = dt.date.today()
        for _ in range(3):
            session = previous_trading_day(session)
        start = session_open_utc(session)
        end = start + dt.timedelta(minutes=30)

        bars = get_default_polygon_client().get_agg_range(ticker="AAPL", start=start, end=end)
        self.assertTrue(
            bars,
            "Polygon returned NO minute bars for a past AAPL opening window — the "
            "plan likely does not serve intraday aggregates (Basic-tier block). The "
            "shadow-return job would silently stamp nothing.",
        )
        # Sanity: each bar carries the close + volume the VWAP math needs.
        self.assertIn("c", bars[0])
        self.assertIn("v", bars[0])


if __name__ == "__main__":
    unittest.main()
