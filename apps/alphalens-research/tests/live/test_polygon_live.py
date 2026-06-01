"""Opt-in live probe for PolygonClient.get_agg_range (Track A v2 PR-3; now L4).

Hermetic tests with fabricated bars pass even when the feature is dead in prod,
because Polygon's free / Basic plan does NOT serve intraday minute aggregates
(it returns empty for current-session windows, and may gate historical intraday
behind a paid plan). Per ``feedback_live_probe_external_ingest_adapters``, this
probe fetches ONE real PAST-session opening window for a liquid ticker and
asserts non-empty — the only thing that catches the Basic-tier block before the
shadow-return job is wired on the VPS.

Routes through the shared L4 classifier (``tests.live.run_probes``) so an empty
window FAILs (permanent) while a 429 / timeout is tolerated (transient).

Run explicitly (never in CI's blocking path; needs POLYGON_API_KEY + network):

    POLYGON_LIVE_TEST=1 .venv/bin/python -m unittest \
        tests.live.test_polygon_live -v
"""

from __future__ import annotations

import datetime as dt
import os
import unittest

from tests.live import PermanentProbeError, TransientProbeError, run_probes

_LIVE = os.environ.get("POLYGON_LIVE_TEST") == "1"


def _classify(exc: Exception) -> Exception:
    """Map a raw client error to the transient/permanent contract."""
    msg = str(exc).lower()
    if "429" in msg or "timeout" in msg or "timed out" in msg or "connection" in msg:
        return TransientProbeError(str(exc))
    return PermanentProbeError(f"agg fetch failed: {exc}")


@unittest.skipUnless(_LIVE, "set POLYGON_LIVE_TEST=1 to run the live Polygon probe")
class TestPolygonAggRangeLive(unittest.TestCase):
    def test_past_session_opening_window_has_minute_bars(self):
        from alphalens_pipeline.data.alt_data.polygon_client import get_default_polygon_client
        from alphalens_pipeline.paper.calendar import previous_trading_day, session_open_utc

        def _probe() -> None:
            # Anchor to a session a few trading days back so it is unambiguously
            # closed regardless of the time of day the probe runs.
            session = dt.date.today()
            for _ in range(3):
                session = previous_trading_day(session)
            start = session_open_utc(session)
            end = start + dt.timedelta(minutes=30)

            try:
                bars = get_default_polygon_client().get_agg_range(
                    ticker="AAPL", start=start, end=end
                )
            except Exception as exc:  # classify any client error, don't leak it raw
                raise _classify(exc) from exc

            if not bars:
                raise PermanentProbeError(
                    "Polygon returned NO minute bars for a past AAPL opening window — "
                    "the plan likely does not serve intraday aggregates (Basic-tier "
                    "block). The shadow-return job would silently stamp nothing."
                )
            # Shape-only: each bar must carry the close + volume the VWAP math
            # needs. We never assert WHAT the close / volume are.
            if "c" not in bars[0] or "v" not in bars[0]:
                raise PermanentProbeError(f"bar missing c/v keys: {sorted(bars[0])}")

        run_probes(self, {"AAPL/minute-bars": _probe}, label="polygon")


if __name__ == "__main__":
    unittest.main()
