import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
from alphalens_pipeline.thematic.verification import mcap_filter


def _live_ticker(mcap: float | None) -> SimpleNamespace:
    fi = MagicMock(spec=["market_cap"])
    fi.market_cap = mcap
    return SimpleNamespace(fast_info=fi)


class TestFilterByMcap(unittest.TestCase):
    def test_keeps_tickers_within_bracket(self):
        with patch.object(
            mcap_filter,
            "fetch_mcap",
            side_effect=lambda t, **_: {
                "QUBT": 1_780_000_000,
                "IONQ": 1_800_000_000,
            }.get(t),
        ):
            kept = mcap_filter.filter_by_mcap(
                ["QUBT", "IONQ"], min_cap=500_000_000, max_cap=10_000_000_000
            )
        self.assertEqual(set(kept), {"QUBT", "IONQ"})
        self.assertEqual(kept["QUBT"], 1_780_000_000)

    def test_drops_below_floor(self):
        with patch.object(mcap_filter, "fetch_mcap", return_value=100_000_000):
            kept = mcap_filter.filter_by_mcap(
                ["MICRO"], min_cap=500_000_000, max_cap=10_000_000_000
            )
        self.assertEqual(kept, {})

    def test_drops_above_ceiling(self):
        with patch.object(mcap_filter, "fetch_mcap", return_value=50_000_000_000):
            kept = mcap_filter.filter_by_mcap(["MEGA"], min_cap=500_000_000, max_cap=10_000_000_000)
        self.assertEqual(kept, {})

    def test_drops_when_mcap_unknown(self):
        # yfinance can return None for delisted/odd tickers — drop, don't crash.
        with patch.object(mcap_filter, "fetch_mcap", return_value=None):
            kept = mcap_filter.filter_by_mcap(["DEAD"], min_cap=500_000_000, max_cap=10_000_000_000)
        self.assertEqual(kept, {})

    def test_handles_empty_input(self):
        kept = mcap_filter.filter_by_mcap([], min_cap=500_000_000, max_cap=10_000_000_000)
        self.assertEqual(kept, {})


class TestFetchMcapErrorPaths(unittest.TestCase):
    def setUp(self):
        # Isolate the persistent mcap cache so these "→ None" assertions are
        # hermetic (an empty cache → no fallback value).
        self._td = tempfile.TemporaryDirectory()
        self._patch = patch.object(
            mcap_filter, "_MCAP_CACHE_PATH", Path(self._td.name) / "mcap_cache.json"
        )
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._td.cleanup()

    def test_returns_none_when_yfinance_returns_none_mcap(self):
        with patch("yfinance.Ticker", return_value=_live_ticker(None)):
            self.assertIsNone(mcap_filter.fetch_mcap("UNKNOWN"))

    def test_returns_none_on_yfinance_exception(self):
        # Network errors, delisted tickers, parse failures — all collapsed
        # to None so the caller can drop the candidate cleanly.
        with patch("yfinance.Ticker", side_effect=RuntimeError("network")):
            self.assertIsNone(mcap_filter.fetch_mcap("DEAD"))


class TestMcapCacheFallback(unittest.TestCase):
    """The live mcap path persists each success and falls back to a recent
    cached value on a transient yfinance failure, so a Yahoo outage does not
    silently empty the whole brief (a candidate seen on a prior run survives).
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.cache = Path(self._td.name) / "mcap_cache.json"
        self._patch = patch.object(mcap_filter, "_MCAP_CACHE_PATH", self.cache)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._td.cleanup()

    def test_live_success_caches_and_returns(self):
        with patch("yfinance.Ticker", return_value=_live_ticker(1_000_000_000.0)):
            self.assertEqual(mcap_filter.fetch_mcap("NVDA"), 1_000_000_000.0)
        stored = json.loads(self.cache.read_text())
        self.assertEqual(stored["NVDA"]["mcap"], 1_000_000_000.0)

    def test_live_failure_falls_back_to_recent_cache(self):
        with patch("yfinance.Ticker", return_value=_live_ticker(2_000_000_000.0)):
            mcap_filter.fetch_mcap("AAPL")
        with patch("yfinance.Ticker", side_effect=RuntimeError("network")):
            self.assertEqual(mcap_filter.fetch_mcap("AAPL"), 2_000_000_000.0)

    def test_live_none_mcap_falls_back_to_recent_cache(self):
        with patch("yfinance.Ticker", return_value=_live_ticker(3_000_000_000.0)):
            mcap_filter.fetch_mcap("MSFT")
        with patch("yfinance.Ticker", return_value=_live_ticker(None)):
            self.assertEqual(mcap_filter.fetch_mcap("MSFT"), 3_000_000_000.0)

    def test_live_failure_no_cache_returns_none(self):
        with patch("yfinance.Ticker", side_effect=RuntimeError("network")):
            self.assertIsNone(mcap_filter.fetch_mcap("BRANDNEW"))

    def test_live_failure_stale_cache_returns_none(self):
        stale_ts = (
            dt.datetime.now(dt.UTC) - dt.timedelta(days=mcap_filter._MCAP_CACHE_MAX_STALE_DAYS + 1)
        ).isoformat()
        self.cache.write_text(json.dumps({"OLD": {"mcap": 5e9, "ts": stale_ts}}))
        with patch("yfinance.Ticker", side_effect=RuntimeError("network")):
            self.assertIsNone(mcap_filter.fetch_mcap("OLD"))

    def test_pit_path_does_not_use_live_cache(self):
        # A historical (asof past) fetch must NOT fall back to the live-mcap
        # cache — PIT mcap is price(asof) × shares(asof), not today's value.
        with patch("yfinance.Ticker", return_value=_live_ticker(9_000_000_000.0)):
            mcap_filter.fetch_mcap("PIT")  # seeds the live cache
        with patch("yfinance.Ticker", side_effect=RuntimeError("network")):
            self.assertIsNone(mcap_filter.fetch_mcap("PIT", asof=dt.date(2020, 1, 2)))


class TestFetchMcapYfinanceContract(unittest.TestCase):
    """Pin the yfinance fast_info attribute-access pattern.

    yfinance's FastInfo exposes `market_cap` as an attribute, NOT as a
    `.get("market_cap")` dict key (which silently returns None). If anyone
    clones fetch_mcap to add fetch_volume / fetch_pe / similar helpers and
    regresses to `.get(...)`, this test fails.
    """

    def test_uses_attribute_access_not_dict_get(self):
        # FastInfo stand-in: attribute access returns the real value;
        # .get("market_cap") returns None (regression target). If the
        # implementation uses .get(), it gets None -> fetch_mcap returns
        # None -> assertion fails.
        fake_fast_info = MagicMock(spec=["market_cap"])
        fake_fast_info.market_cap = 1_780_000_000.0
        fake_fast_info.get = MagicMock(return_value=None)
        fake_ticker = SimpleNamespace(fast_info=fake_fast_info)

        with patch("yfinance.Ticker", return_value=fake_ticker):
            mc = mcap_filter.fetch_mcap("QUBT")
        self.assertEqual(mc, 1_780_000_000.0)
        # Sanity: the regression path (dict-get) was NOT used.
        fake_fast_info.get.assert_not_called()


class TestFetchMcapPITPath(unittest.TestCase):
    """When ``asof`` is in the past, fetch_mcap must return
    ``close_on_asof × shares_outstanding_on_asof`` instead of yfinance's
    live ``fast_info.market_cap`` (which is always today's price × today's
    shares — a look-ahead bias for historical replay)."""

    def _fake_ticker(self, *, hist_df, shares_series, fast_mcap=None):
        # Stand-in for ``yfinance.Ticker(t)`` that the PIT path drives.
        fake_fast_info = SimpleNamespace(market_cap=fast_mcap, shares=None)
        return SimpleNamespace(
            history=MagicMock(return_value=hist_df),
            get_shares_full=MagicMock(return_value=shares_series),
            fast_info=fake_fast_info,
        )

    def test_pit_mcap_uses_close_times_shares_at_asof(self):
        # QUBT-like PIT example: on 2026-04-14 close was $8.11 and shares
        # outstanding was 224.5M → mcap = $1.82B (not the live $2.37B from
        # fast_info, which uses today's $10.50 × 225.5M).
        asof = dt.date(2026, 4, 14)
        hist = pd.DataFrame(
            {"Close": [7.50, 8.11]},
            index=pd.to_datetime(["2026-04-13", "2026-04-14"]),
        )
        shares = pd.Series(
            [220_000_000.0, 224_500_000.0],
            index=pd.to_datetime(["2026-01-15", "2026-03-30"]),
        )
        fake = self._fake_ticker(hist_df=hist, shares_series=shares, fast_mcap=999e9)
        with patch("yfinance.Ticker", return_value=fake):
            mc = mcap_filter.fetch_mcap("QUBT", asof=asof)
        self.assertAlmostEqual(mc, 8.11 * 224_500_000.0, places=2)

    def test_pit_path_skips_shares_after_asof(self):
        # If a shares-outstanding update lands AFTER asof, it must not be
        # used — that would be look-ahead bias on the shares count.
        asof = dt.date(2026, 4, 14)
        hist = pd.DataFrame({"Close": [10.0]}, index=pd.to_datetime(["2026-04-14"]))
        shares = pd.Series(
            [200_000_000.0, 999_000_000.0],
            index=pd.to_datetime(["2026-03-01", "2026-05-01"]),  # 2nd is after asof
        )
        fake = self._fake_ticker(hist_df=hist, shares_series=shares)
        with patch("yfinance.Ticker", return_value=fake):
            mc = mcap_filter.fetch_mcap("X", asof=asof)
        self.assertEqual(mc, 10.0 * 200_000_000.0)

    def test_pit_path_uses_last_close_on_or_before_asof_when_market_closed(self):
        # Saturday asof → use Friday close.
        asof = dt.date(2026, 4, 18)  # Saturday
        hist = pd.DataFrame(
            {"Close": [9.00, 10.00]},
            index=pd.to_datetime(["2026-04-16", "2026-04-17"]),  # Thu, Fri
        )
        shares = pd.Series([100_000_000.0], index=pd.to_datetime(["2026-01-01"]))
        fake = self._fake_ticker(hist_df=hist, shares_series=shares)
        with patch("yfinance.Ticker", return_value=fake):
            mc = mcap_filter.fetch_mcap("X", asof=asof)
        self.assertEqual(mc, 10.0 * 100_000_000.0)

    def test_pit_path_returns_none_when_no_history(self):
        asof = dt.date(2026, 4, 14)
        empty = pd.DataFrame({"Close": []}, index=pd.to_datetime([]))
        shares = pd.Series([100e6], index=pd.to_datetime(["2026-01-01"]))
        fake = self._fake_ticker(hist_df=empty, shares_series=shares)
        with patch("yfinance.Ticker", return_value=fake):
            self.assertIsNone(mcap_filter.fetch_mcap("X", asof=asof))

    def test_pit_path_falls_back_to_fast_info_shares_when_get_shares_full_empty(self):
        # Some tickers have no SC-13D/G filings → get_shares_full returns
        # empty. fast_info.shares is the last-resort proxy.
        asof = dt.date(2026, 4, 14)
        hist = pd.DataFrame({"Close": [10.0]}, index=pd.to_datetime(["2026-04-14"]))
        empty_shares = pd.Series(dtype=float)
        fake_fast_info = SimpleNamespace(market_cap=None, shares=500_000_000.0)
        fake = SimpleNamespace(
            history=MagicMock(return_value=hist),
            get_shares_full=MagicMock(return_value=empty_shares),
            fast_info=fake_fast_info,
        )
        with patch("yfinance.Ticker", return_value=fake):
            mc = mcap_filter.fetch_mcap("X", asof=asof)
        self.assertEqual(mc, 10.0 * 500_000_000.0)

    def test_asof_today_or_future_uses_fast_info(self):
        # Live flow (asof == today) skips the slower PIT path and uses
        # fast_info.market_cap directly — preserves prior behavior.
        fake_fast_info = SimpleNamespace(market_cap=1_780_000_000.0, shares=None)
        fake = SimpleNamespace(
            history=MagicMock(),
            get_shares_full=MagicMock(),
            fast_info=fake_fast_info,
        )
        with patch("yfinance.Ticker", return_value=fake):
            mc = mcap_filter.fetch_mcap("QUBT", asof=dt.date.today())
        self.assertEqual(mc, 1_780_000_000.0)
        fake.history.assert_not_called()
        fake.get_shares_full.assert_not_called()


class TestFilterByMcapPITPath(unittest.TestCase):
    def test_filter_passes_asof_through_to_fetch(self):
        captured = {}

        def fake_fetch(t, *, asof=None):
            captured[t] = asof
            return 1_000_000_000.0

        with patch.object(mcap_filter, "fetch_mcap", side_effect=fake_fetch):
            mcap_filter.filter_by_mcap(
                ["QUBT"],
                min_cap=500_000_000,
                max_cap=10_000_000_000,
                asof=dt.date(2026, 4, 14),
            )
        self.assertEqual(captured["QUBT"], dt.date(2026, 4, 14))

    def test_filter_asof_none_preserves_live_behaviour(self):
        captured = {}

        def fake_fetch(t, *, asof=None):
            captured[t] = asof
            return 1_000_000_000.0

        with patch.object(mcap_filter, "fetch_mcap", side_effect=fake_fetch):
            mcap_filter.filter_by_mcap(["QUBT"], min_cap=1, max_cap=10_000_000_000)
        self.assertIsNone(captured["QUBT"])


if __name__ == "__main__":
    unittest.main()
