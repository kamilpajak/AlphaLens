import datetime as dt
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd
from alphalens_research.thematic.sources import earnings_calendar

# All happy-path tests run with asof = today so the PIT guard doesn't
# short-circuit. Calendar dates are pegged to today + delta so the tests
# don't go stale as the clock advances.
_TODAY = dt.date.today()
_DELTA_30 = _TODAY + dt.timedelta(days=30)
_DELTA_120 = _TODAY + dt.timedelta(days=120)


class TestFetchNextEarnings(unittest.TestCase):
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


class TestFetchNextEarningsPITGuard(unittest.TestCase):
    """``yfinance.calendar`` only exposes today's forward schedule. For
    asof < today the function would happily return a date 6 years out
    (verified empirically: AAPL asof=2020-06-15 returned 2026-07-30).
    Guard: return None for past asof so historical replay never reads
    a leaked forward date as factual."""

    def test_returns_none_for_past_asof_without_calling_yfinance(self):
        # Even if yfinance would return data, the guard fires first.
        fake_ticker = MagicMock()
        fake_ticker.calendar = {"Earnings Date": [_DELTA_30]}
        past = _TODAY - dt.timedelta(days=30)
        with patch("yfinance.Ticker", return_value=fake_ticker) as patched:
            result = earnings_calendar.fetch_next_earnings(ticker="QUBT", asof=past)
        self.assertIsNone(result)
        patched.assert_not_called()

    def test_asof_today_or_future_calls_yfinance(self):
        fake_ticker = MagicMock()
        fake_ticker.calendar = {"Earnings Date": [_DELTA_30]}
        with patch("yfinance.Ticker", return_value=fake_ticker) as patched:
            earnings_calendar.fetch_next_earnings(ticker="QUBT", asof=_TODAY)
        patched.assert_called_once_with("QUBT")


if __name__ == "__main__":
    unittest.main()
