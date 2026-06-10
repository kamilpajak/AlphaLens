"""Tests for the canonical :class:`YFinanceClient`.

yfinance is unauthenticated (Yahoo, ToS-grey) so there is no Bearer header /
API key, but the same canonical-client doctrine applies: one throttle + retry
seam shared by every consumer so a 429 burst can't drain the implicit Yahoo
rate budget for the whole daily thematic pipeline.

These tests mock ``yfinance.Ticker`` (same style as ``test_mcap_filter.py`` and
``test_earnings_calendar.py``) and inject a no-op ``sleep`` so the retry /
throttle paths run instantly. They never hit Yahoo.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
from alphalens_pipeline.data.alt_data import yfinance_client as yc
from yfinance.exceptions import YFRateLimitError


def _history_frame() -> pd.DataFrame:
    """A yfinance-style (capitalized columns, tz-aware index) OHLCV frame."""
    return pd.DataFrame(
        {
            "Open": [10.0],
            "High": [11.0],
            "Low": [9.0],
            "Close": [10.5],
            "Volume": [5000.0],
        },
        index=pd.DatetimeIndex(["2026-04-10"], tz="America/New_York"),
    )


def _client(**kw) -> yc.YFinanceClient:
    """Build a client with an instant sleep + a temp stale-fallback cache dir."""
    kw.setdefault("sleep", lambda _s: None)
    kw.setdefault("min_interval_s", 0.0)
    return yc.YFinanceClient(**kw)


class TestDailyOhlcvSuccess(unittest.TestCase):
    def test_returns_normalized_lowercase_tz_naive_frame(self):
        fake = MagicMock()
        fake.history.return_value = _history_frame()
        with patch("yfinance.Ticker", return_value=fake) as patched:
            df = _client().daily_ohlcv("qubt", start=dt.date(2025, 3, 1), end=dt.date(2026, 4, 14))
        # yfinance.Ticker is called with the upper-cased symbol.
        patched.assert_called_once_with("QUBT")
        # auto_adjust=False matches the legacy scorer fetch.
        _, kwargs = fake.history.call_args
        self.assertFalse(kwargs["auto_adjust"])
        # Normalized: lowercase columns, exact set, tz-naive index.
        self.assertEqual(list(df.columns), ["open", "high", "low", "close", "volume"])
        self.assertIsInstance(df.index, pd.DatetimeIndex)
        self.assertIsNone(df.index.tz)
        self.assertEqual(float(df["close"].iloc[0]), 10.5)

    def test_empty_live_frame_returns_empty(self):
        fake = MagicMock()
        fake.history.return_value = pd.DataFrame()
        with patch("yfinance.Ticker", return_value=fake):
            df = _client().daily_ohlcv("DEAD", start=dt.date(2025, 3, 1), end=dt.date(2026, 4, 14))
        self.assertTrue(df.empty)


class TestDailyOhlcvRetry(unittest.TestCase):
    def test_retries_on_rate_limit_then_succeeds(self):
        # First call raises YFRateLimitError (transient), second returns data.
        fake = MagicMock()
        fake.history.side_effect = [YFRateLimitError(), _history_frame()]
        slept: list[float] = []
        with patch("yfinance.Ticker", return_value=fake):
            df = _client(sleep=slept.append).daily_ohlcv(
                "QUBT", start=dt.date(2025, 3, 1), end=dt.date(2026, 4, 14)
            )
        self.assertEqual(fake.history.call_count, 2)
        self.assertEqual(float(df["close"].iloc[0]), 10.5)
        # One backoff sleep happened between the two attempts.
        self.assertEqual(len(slept), 1)
        self.assertGreater(slept[0], 0)

    def test_retries_on_too_many_requests_message_string(self):
        # Not every transient surfaces as YFRateLimitError — a generic
        # exception whose message contains "Too Many Requests" is transient too.
        fake = MagicMock()
        fake.history.side_effect = [RuntimeError("HTTP 429 Too Many Requests"), _history_frame()]
        with patch("yfinance.Ticker", return_value=fake):
            df = _client().daily_ohlcv("QUBT", start=dt.date(2025, 3, 1), end=dt.date(2026, 4, 14))
        self.assertEqual(fake.history.call_count, 2)
        self.assertFalse(df.empty)

    def test_no_retry_on_permanent_404_delist(self):
        # A permanent failure (delisted / 404) must NOT retry — one attempt,
        # empty frame, no crash.
        fake = MagicMock()
        fake.history.side_effect = RuntimeError("404 Not Found: possibly delisted")
        with patch("yfinance.Ticker", return_value=fake):
            df = _client().daily_ohlcv("DEAD", start=dt.date(2025, 3, 1), end=dt.date(2026, 4, 14))
        self.assertEqual(fake.history.call_count, 1)
        self.assertTrue(df.empty)

    def test_rate_limit_exhausted_returns_empty_not_crash(self):
        # Every attempt rate-limits → never crash the batch, return empty.
        fake = MagicMock()
        fake.history.side_effect = YFRateLimitError()
        with patch("yfinance.Ticker", return_value=fake):
            df = _client().daily_ohlcv("QUBT", start=dt.date(2025, 3, 1), end=dt.date(2026, 4, 14))
        self.assertEqual(fake.history.call_count, yc.YFinanceClient._MAX_REQUEST_ATTEMPTS)
        self.assertTrue(df.empty)


class TestThrottle(unittest.TestCase):
    def test_calls_are_spaced_by_min_interval(self):
        fake = MagicMock()
        fake.history.return_value = _history_frame()
        slept: list[float] = []
        client = _client(min_interval_s=2.0, sleep=slept.append)
        # Drive the throttle's clock deterministically.
        clock = {"t": 100.0}
        with (
            patch("yfinance.Ticker", return_value=fake),
            patch.object(yc.time, "monotonic", side_effect=lambda: clock["t"]),
        ):
            client.daily_ohlcv("A", start=dt.date(2025, 3, 1), end=dt.date(2026, 4, 14))
            # Clock has not advanced — the second call must wait the full interval.
            client.daily_ohlcv("B", start=dt.date(2025, 3, 1), end=dt.date(2026, 4, 14))
        # First call has nothing to wait for; second waits ~min_interval.
        self.assertTrue(any(abs(s - 2.0) < 1e-6 for s in slept))


class TestNextEarnings(unittest.TestCase):
    def test_returns_calendar_attribute(self):
        today = dt.date.today()
        nxt = today + dt.timedelta(days=30)
        fake = MagicMock()
        fake.calendar = {"Earnings Date": [nxt]}
        with patch("yfinance.Ticker", return_value=fake) as patched:
            cal = _client().next_earnings("qubt")
        patched.assert_called_once_with("QUBT")
        self.assertEqual(cal, {"Earnings Date": [nxt]})

    def test_returns_none_on_permanent_exception(self):
        # ``calendar`` is an attribute access, not a method call — model the
        # raise-on-access with a tiny stand-in whose property throws (a
        # MagicMock auto-creates the attribute before any side_effect fires).
        class _Raises:
            @property
            def calendar(self):
                raise RuntimeError("404 delisted")

        with patch("yfinance.Ticker", return_value=_Raises()):
            self.assertIsNone(_client().next_earnings("DEAD"))

    def test_retries_calendar_on_rate_limit(self):
        nxt = dt.date.today() + dt.timedelta(days=30)
        calls = {"n": 0}

        class _Flaky:
            @property
            def calendar(self):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise YFRateLimitError()
                return {"Earnings Date": [nxt]}

        with patch("yfinance.Ticker", return_value=_Flaky()):
            cal = _client().next_earnings("QUBT")
        self.assertEqual(calls["n"], 2)
        self.assertEqual(cal, {"Earnings Date": [nxt]})


class TestOhlcvStaleFallback(unittest.TestCase):
    """On a rate-limited / empty live fetch the loader falls back to the
    newest existing ``{TICKER}_*.parquet`` so a Yahoo outage computes
    technicals off a slightly-stale ~251-row history rather than nothing."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._cache_dir = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def _write_cache(self, ticker: str, asof: dt.date, close: float) -> None:
        df = pd.DataFrame(
            {"open": [1.0], "high": [2.0], "low": [0.5], "close": [close], "volume": [1000.0]},
            index=pd.DatetimeIndex([pd.Timestamp(asof)]),
        )
        df.to_parquet(self._cache_dir / f"{ticker}_{asof.isoformat()}.parquet")

    def test_exact_asof_cache_hit_skips_live_fetch(self):
        asof = dt.date(2026, 4, 14)
        self._write_cache("QUBT", asof, close=1.5)
        client = _client(cache_dir=self._cache_dir)
        with patch("yfinance.Ticker", side_effect=AssertionError("must not fetch live")):
            df = client.cached_daily_ohlcv("QUBT", asof=asof)
        self.assertEqual(float(df["close"].iloc[0]), 1.5)

    def test_writes_parquet_after_live_fetch(self):
        asof = dt.date(2026, 4, 14)
        fake = MagicMock()
        fake.history.return_value = _history_frame()
        client = _client(cache_dir=self._cache_dir)
        with patch("yfinance.Ticker", return_value=fake):
            client.cached_daily_ohlcv("RGTI", asof=asof)
        self.assertTrue((self._cache_dir / f"RGTI_{asof.isoformat()}.parquet").exists())

    def test_rate_limited_live_fetch_falls_back_to_newest_parquet(self):
        # A stale parquet from an EARLIER asof exists; today's asof has no
        # exact-match cache and the live fetch 429s → use the stale one.
        self._write_cache("QUBT", dt.date(2026, 4, 1), close=7.0)
        asof = dt.date(2026, 4, 14)
        fake = MagicMock()
        fake.history.side_effect = YFRateLimitError()
        client = _client(cache_dir=self._cache_dir)
        with patch("yfinance.Ticker", return_value=fake):
            df = client.cached_daily_ohlcv("QUBT", asof=asof)
        self.assertFalse(df.empty)
        self.assertEqual(float(df["close"].iloc[0]), 7.0)

    def test_picks_the_newest_of_several_stale_parquets(self):
        self._write_cache("QUBT", dt.date(2026, 3, 1), close=3.0)
        self._write_cache("QUBT", dt.date(2026, 4, 1), close=7.0)
        asof = dt.date(2026, 4, 14)
        fake = MagicMock()
        fake.history.side_effect = YFRateLimitError()
        client = _client(cache_dir=self._cache_dir)
        with patch("yfinance.Ticker", return_value=fake):
            df = client.cached_daily_ohlcv("QUBT", asof=asof)
        # Newest (2026-04-01) wins over the older (2026-03-01) one.
        self.assertEqual(float(df["close"].iloc[0]), 7.0)

    def test_empty_live_fetch_with_no_cache_returns_empty(self):
        asof = dt.date(2026, 4, 14)
        fake = MagicMock()
        fake.history.return_value = pd.DataFrame()
        client = _client(cache_dir=self._cache_dir)
        with patch("yfinance.Ticker", return_value=fake):
            df = client.cached_daily_ohlcv("NEW", asof=asof)
        self.assertTrue(df.empty)

    def test_in_run_memo_cache_avoids_refetch(self):
        asof = dt.date(2026, 4, 14)
        fake = MagicMock()
        fake.history.return_value = _history_frame()
        client = _client(cache_dir=self._cache_dir)
        with patch("yfinance.Ticker", return_value=fake):
            client.cached_daily_ohlcv("AAA", asof=asof)
            client.cached_daily_ohlcv("AAA", asof=asof)
        # Second call served from the in-process memo, not a 2nd live fetch.
        self.assertEqual(fake.history.call_count, 1)


class TestSingleton(unittest.TestCase):
    def setUp(self):
        yc._reset_default_client_for_tests()

    def tearDown(self):
        yc._reset_default_client_for_tests()

    def test_get_default_returns_same_instance(self):
        a = yc.get_default_yfinance_client()
        b = yc.get_default_yfinance_client()
        self.assertIs(a, b)

    def test_reset_clears_singleton(self):
        a = yc.get_default_yfinance_client()
        yc._reset_default_client_for_tests()
        b = yc.get_default_yfinance_client()
        self.assertIsNot(a, b)


if __name__ == "__main__":
    unittest.main()


class TestMarketCap(unittest.TestCase):
    @staticmethod
    def _ticker(mcap):
        return SimpleNamespace(fast_info=SimpleNamespace(market_cap=mcap))

    def test_returns_market_cap(self):
        with patch("yfinance.Ticker", return_value=self._ticker(4.5e12)):
            self.assertEqual(_client().market_cap("aapl"), 4.5e12)

    def test_none_market_cap_returns_none(self):
        with patch("yfinance.Ticker", return_value=self._ticker(None)):
            self.assertIsNone(_client().market_cap("x"))

    def test_retries_rate_limit_then_returns(self):
        with patch(
            "yfinance.Ticker",
            side_effect=[YFRateLimitError(), self._ticker(1e9)],
        ):
            self.assertEqual(_client().market_cap("x"), 1e9)


class TestShares(unittest.TestCase):
    def test_pit_uses_latest_shares_on_or_before_asof(self):
        series = pd.Series([200e6, 224.5e6], index=pd.to_datetime(["2026-01-15", "2026-03-30"]))
        tk = SimpleNamespace(
            get_shares_full=MagicMock(return_value=series),
            fast_info=SimpleNamespace(shares=None),
        )
        with patch("yfinance.Ticker", return_value=tk):
            self.assertEqual(_client().shares("qubt", asof=dt.date(2026, 4, 14)), 224.5e6)

    def test_pit_skips_shares_after_asof(self):
        series = pd.Series([200e6, 999e6], index=pd.to_datetime(["2026-03-01", "2026-05-01"]))
        tk = SimpleNamespace(
            get_shares_full=MagicMock(return_value=series),
            fast_info=SimpleNamespace(shares=None),
        )
        with patch("yfinance.Ticker", return_value=tk):
            self.assertEqual(_client().shares("x", asof=dt.date(2026, 4, 14)), 200e6)

    def test_falls_back_to_fast_info_shares_when_series_empty(self):
        tk = SimpleNamespace(
            get_shares_full=MagicMock(return_value=pd.Series(dtype=float)),
            fast_info=SimpleNamespace(shares=500e6),
        )
        with patch("yfinance.Ticker", return_value=tk):
            self.assertEqual(_client().shares("x", asof=dt.date(2026, 4, 14)), 500e6)

    def test_live_snapshot_skips_dated_series_when_no_asof(self):
        tk = SimpleNamespace(
            get_shares_full=MagicMock(),
            fast_info=SimpleNamespace(shares=300e6),
        )
        with patch("yfinance.Ticker", return_value=tk):
            self.assertEqual(_client().shares("x"), 300e6)
        tk.get_shares_full.assert_not_called()

    def test_pit_handles_tz_aware_index_without_crashing(self):
        # The real get_shares_full index is UTC-aware; the normalisation must be
        # idempotent (a future tz-naive index must not raise either — covered by
        # the naive-index fixtures above).
        series = pd.Series([224.5e6], index=pd.to_datetime(["2026-03-30"]).tz_localize("UTC"))
        tk = SimpleNamespace(
            get_shares_full=MagicMock(return_value=series),
            fast_info=SimpleNamespace(shares=None),
        )
        with patch("yfinance.Ticker", return_value=tk):
            self.assertEqual(_client().shares("qubt", asof=dt.date(2026, 4, 14)), 224.5e6)

    def test_pit_warns_when_match_is_stale(self):
        # Match 200+ days before asof → still returned, but a staleness warning
        # makes the forward bias visible to a PIT analysis.
        series = pd.Series([200e6], index=pd.to_datetime(["2025-08-01"]))
        tk = SimpleNamespace(
            get_shares_full=MagicMock(return_value=series),
            fast_info=SimpleNamespace(shares=None),
        )
        with (
            patch("yfinance.Ticker", return_value=tk),
            self.assertLogs(yc.logger, level="WARNING") as logs,
        ):
            self.assertEqual(_client().shares("x", asof=dt.date(2026, 4, 14)), 200e6)
        self.assertTrue(any("stale" in m for m in logs.output))

    def test_pit_warns_on_fast_info_fallback_with_asof(self):
        # No get_shares_full point at all + a PIT asof → snapshot fallback logs a
        # forward-bias warning (silent on the asof=None live path).
        tk = SimpleNamespace(
            get_shares_full=MagicMock(return_value=pd.Series(dtype=float)),
            fast_info=SimpleNamespace(shares=500e6),
        )
        with (
            patch("yfinance.Ticker", return_value=tk),
            self.assertLogs(yc.logger, level="WARNING") as logs,
        ):
            self.assertEqual(_client().shares("x", asof=dt.date(2026, 4, 14)), 500e6)
        self.assertTrue(any("forward-biased" in m for m in logs.output))


class TestDividends(unittest.TestCase):
    @staticmethod
    def _dividend_series() -> pd.Series:
        """A yfinance-style per-share cash-dividend series (tz-aware ex-dates)."""
        return pd.Series(
            [0.22, 0.23, 0.24],
            index=pd.DatetimeIndex(
                ["2025-08-08", "2025-11-07", "2026-02-06"], tz="America/New_York"
            ),
        )

    def test_returns_full_series_when_no_asof(self):
        tk = SimpleNamespace(dividends=self._dividend_series())
        with patch("yfinance.Ticker", return_value=tk) as patched:
            series = _client().dividends("aapl")
        patched.assert_called_once_with("AAPL")
        self.assertEqual(len(series), 3)
        self.assertEqual(float(series.iloc[-1]), 0.24)

    def test_index_is_tz_naive(self):
        tk = SimpleNamespace(dividends=self._dividend_series())
        with patch("yfinance.Ticker", return_value=tk):
            series = _client().dividends("aapl")
        self.assertIsInstance(series.index, pd.DatetimeIndex)
        self.assertIsNone(series.index.tz)

    def test_asof_slices_out_later_ex_dates(self):
        # asof between the 2nd and 3rd ex-date drops the 2026-02-06 dividend.
        tk = SimpleNamespace(dividends=self._dividend_series())
        with patch("yfinance.Ticker", return_value=tk):
            series = _client().dividends("aapl", asof=dt.date(2025, 12, 31))
        self.assertEqual(len(series), 2)
        self.assertEqual(float(series.iloc[-1]), 0.23)

    def test_empty_series_when_no_dividends(self):
        # A non-payer returns an empty yfinance series.
        tk = SimpleNamespace(dividends=pd.Series(dtype=float))
        with patch("yfinance.Ticker", return_value=tk):
            series = _client().dividends("googl")
        self.assertTrue(series.empty)

    def test_permanent_failure_returns_empty_series_not_crash(self):
        # A permanent failure (delisted / 404) returns the empty default,
        # never raises.
        class _Raises:
            @property
            def dividends(self):
                raise RuntimeError("404 delisted")

        with patch("yfinance.Ticker", return_value=_Raises()):
            series = _client().dividends("DEAD")
        self.assertTrue(series.empty)

    def test_rate_limit_exhausted_returns_empty_series(self):
        # Every attempt rate-limits → never crash the batch, return the empty
        # default. ``dividends`` is an attribute access, so a property whose
        # getter always raises models the persistent transient failure.
        class _AlwaysRateLimited:
            @property
            def dividends(self):
                raise YFRateLimitError()

        slept: list[float] = []
        with patch("yfinance.Ticker", return_value=_AlwaysRateLimited()):
            series = _client(sleep=slept.append).dividends("QUBT")
        self.assertTrue(series.empty)
        # Retried up to the attempt cap before giving up.
        self.assertEqual(len(slept), yc.YFinanceClient._MAX_REQUEST_ATTEMPTS - 1)
