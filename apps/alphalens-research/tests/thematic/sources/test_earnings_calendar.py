import datetime as dt
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd
from alphalens_pipeline.data.alt_data import yfinance_client as yc
from alphalens_pipeline.thematic.sources import earnings_calendar

# All happy-path tests run with asof = today so the PIT guard doesn't
# short-circuit. Calendar dates are pegged to today + delta so the tests
# don't go stale as the clock advances.
_TODAY = dt.date.today()
_DELTA_30 = _TODAY + dt.timedelta(days=30)
_DELTA_120 = _TODAY + dt.timedelta(days=120)


class _FastClientMixin:
    """Reset the YFinanceClient singleton to a throttle-free instance so the
    earnings tests (which route through ``get_default_yfinance_client``) run
    instantly and don't leak state between cases."""

    def setUp(self):
        super().setUp()
        yc._reset_default_client_for_tests()
        yc._DEFAULT_CLIENT = yc.YFinanceClient(min_interval_s=0.0, sleep=lambda _s: None)

    def tearDown(self):
        yc._reset_default_client_for_tests()
        super().tearDown()


class TestFetchNextEarnings(_FastClientMixin, unittest.TestCase):
    def test_returns_next_date_after_asof(self):
        fake_ticker = MagicMock()
        fake_ticker.calendar = {"Earnings Date": [_DELTA_30]}
        with patch("yfinance.Ticker", return_value=fake_ticker):
            result = earnings_calendar.fetch_next_earnings(ticker="QUBT", asof=_TODAY)
        self.assertEqual(result, _DELTA_30)

    def test_skips_past_dates_pit_guard(self):
        # Calendar contains only ≤asof entries — return None.
        fake_ticker = MagicMock()
        fake_ticker.calendar = {"Earnings Date": [_TODAY]}
        with patch("yfinance.Ticker", return_value=fake_ticker):
            result = earnings_calendar.fetch_next_earnings(ticker="QUBT", asof=_TODAY)
        self.assertIsNone(result)

    def test_returns_none_on_yfinance_exception(self):
        with patch("yfinance.Ticker", side_effect=RuntimeError("network")):
            self.assertIsNone(earnings_calendar.fetch_next_earnings(ticker="QUBT", asof=_TODAY))

    def test_returns_none_when_no_calendar(self):
        fake_ticker = MagicMock()
        fake_ticker.calendar = None
        with patch("yfinance.Ticker", return_value=fake_ticker):
            self.assertIsNone(earnings_calendar.fetch_next_earnings(ticker="QUBT", asof=_TODAY))

    def test_handles_dataframe_calendar_shape(self):
        # Older yfinance versions return a DataFrame with date-typed Series.
        df = pd.DataFrame({"Earnings Date": [pd.Timestamp(_DELTA_30), pd.Timestamp(_DELTA_120)]})
        fake_ticker = MagicMock()
        fake_ticker.calendar = df
        with patch("yfinance.Ticker", return_value=fake_ticker):
            result = earnings_calendar.fetch_next_earnings(ticker="QUBT", asof=_TODAY)
        self.assertEqual(result, _DELTA_30)

    def test_handles_dataframe_with_earnings_date_in_index(self):
        df = pd.DataFrame({"col1": [pd.Timestamp(_DELTA_30)]}, index=["Earnings Date"])
        fake_ticker = MagicMock()
        fake_ticker.calendar = df
        with patch("yfinance.Ticker", return_value=fake_ticker):
            result = earnings_calendar.fetch_next_earnings(ticker="QUBT", asof=_TODAY)
        self.assertEqual(result, _DELTA_30)

    def test_returns_none_for_unknown_calendar_type(self):
        fake_ticker = MagicMock()
        fake_ticker.calendar = "unexpected string"
        with patch("yfinance.Ticker", return_value=fake_ticker):
            self.assertIsNone(earnings_calendar.fetch_next_earnings(ticker="QUBT", asof=_TODAY))

    def test_handles_datetime_values(self):
        # Coerces datetime instances to date via .date().
        fake_ticker = MagicMock()
        fake_ticker.calendar = {"Earnings Date": [dt.datetime.combine(_DELTA_30, dt.time(14, 30))]}
        with patch("yfinance.Ticker", return_value=fake_ticker):
            result = earnings_calendar.fetch_next_earnings(ticker="QUBT", asof=_TODAY)
        self.assertEqual(result, _DELTA_30)


class TestFreshnessWindowGuard(_FastClientMixin, unittest.TestCase):
    """Freshness-window guard. ``yfinance.calendar`` only exposes today's
    forward schedule, so a genuine historical replay (asof months/years ago)
    would read a leaked forward date as factual (verified empirically: AAPL
    asof=2020-06-15 returned 2026-07-30). But the daily T-1 brief is the live
    operator workflow, not a replay, and a forward earnings date carries no
    meaningful leak. Guard: surface earnings when asof is within the freshness
    window of today; suppress only when asof is genuinely far in the past."""

    def test_returns_none_for_asof_beyond_freshness_window(self):
        # asof 30 days old (genuine historical replay) — guard fires before
        # yfinance is touched, even though it would return data.
        fake_ticker = MagicMock()
        fake_ticker.calendar = {"Earnings Date": [_DELTA_30]}
        past = _TODAY - dt.timedelta(days=30)
        with patch("yfinance.Ticker", return_value=fake_ticker) as patched:
            result = earnings_calendar.fetch_next_earnings(ticker="QUBT", asof=past, today=_TODAY)
        self.assertIsNone(result)
        patched.assert_not_called()

    def test_asof_today_or_future_calls_yfinance(self):
        fake_ticker = MagicMock()
        fake_ticker.calendar = {"Earnings Date": [_DELTA_30]}
        with patch("yfinance.Ticker", return_value=fake_ticker) as patched:
            earnings_calendar.fetch_next_earnings(ticker="QUBT", asof=_TODAY, today=_TODAY)
        patched.assert_called_once_with("QUBT")


class TestFreshnessWindowBoundary(_FastClientMixin, unittest.TestCase):
    """Freshness window = 7 days: asof in [today-7, today] (exclusive of the
    -7 edge) calls yfinance; asof more than 7 days old returns None (genuine
    historical replay). ``today`` is injected so the boundary is pinnable."""

    def test_asof_exactly_7_days_old_returns_earnings(self):
        # Boundary: today - asof == 7 days. ``> 7`` is False here, so this is
        # the last day INSIDE the window — earnings surface, yfinance is hit.
        past_7 = _TODAY - dt.timedelta(days=7)
        fake_ticker = MagicMock()
        fake_ticker.calendar = {"Earnings Date": [_DELTA_30]}
        with patch("yfinance.Ticker", return_value=fake_ticker) as patched:
            result = earnings_calendar.fetch_next_earnings(ticker="QUBT", asof=past_7, today=_TODAY)
        self.assertEqual(result, _DELTA_30)
        patched.assert_called_once_with("QUBT")

    def test_asof_8_days_old_returns_none(self):
        # First day OUTSIDE the window — guard fires, yfinance untouched.
        past_8 = _TODAY - dt.timedelta(days=8)
        fake_ticker = MagicMock()
        fake_ticker.calendar = {"Earnings Date": [_DELTA_30]}
        with patch("yfinance.Ticker", return_value=fake_ticker) as patched:
            result = earnings_calendar.fetch_next_earnings(ticker="QUBT", asof=past_8, today=_TODAY)
        self.assertIsNone(result)
        patched.assert_not_called()

    def test_asof_6_days_old_within_window_returns_earnings(self):
        past_6 = _TODAY - dt.timedelta(days=6)
        fake_ticker = MagicMock()
        fake_ticker.calendar = {"Earnings Date": [_DELTA_30]}
        with patch("yfinance.Ticker", return_value=fake_ticker) as patched:
            result = earnings_calendar.fetch_next_earnings(ticker="QUBT", asof=past_6, today=_TODAY)
        self.assertEqual(result, _DELTA_30)
        patched.assert_called_once_with("QUBT")

    def test_asof_yesterday_returns_earnings_steady_state(self):
        # The daily pipeline runs at asof = today_UTC - 1; this is the case
        # the fix exists to unblock.
        yesterday = _TODAY - dt.timedelta(days=1)
        fake_ticker = MagicMock()
        fake_ticker.calendar = {"Earnings Date": [_DELTA_30]}
        with patch("yfinance.Ticker", return_value=fake_ticker) as patched:
            result = earnings_calendar.fetch_next_earnings(
                ticker="QUBT", asof=yesterday, today=_TODAY
            )
        self.assertEqual(result, _DELTA_30)
        patched.assert_called_once_with("QUBT")

    def test_injectable_today_parameter_pins_window(self):
        # Injected ``today`` controls the window, not the real clock: asof is
        # 4 days before injected_today → inside the window.
        asof = dt.date(2026, 6, 1)
        injected_today = dt.date(2026, 6, 5)
        fake_ticker = MagicMock()
        fake_ticker.calendar = {"Earnings Date": [dt.date(2026, 8, 5)]}
        with patch("yfinance.Ticker", return_value=fake_ticker) as patched:
            result = earnings_calendar.fetch_next_earnings(
                ticker="QUBT", asof=asof, today=injected_today
            )
        self.assertEqual(result, dt.date(2026, 8, 5))
        patched.assert_called_once_with("QUBT")

    def test_today_defaults_to_real_clock_when_omitted(self):
        # When ``today`` is omitted, the function falls back to the real clock;
        # a T-1 asof must still surface earnings (the production call path).
        yesterday = _TODAY - dt.timedelta(days=1)
        fake_ticker = MagicMock()
        fake_ticker.calendar = {"Earnings Date": [_DELTA_30]}
        with patch("yfinance.Ticker", return_value=fake_ticker) as patched:
            result = earnings_calendar.fetch_next_earnings(ticker="QUBT", asof=yesterday)
        self.assertEqual(result, _DELTA_30)
        patched.assert_called_once_with("QUBT")


if __name__ == "__main__":
    unittest.main()
