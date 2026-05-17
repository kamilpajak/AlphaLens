import datetime as dt
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from alphalens.thematic.sources import earnings_calendar


class TestFetchNextEarnings(unittest.TestCase):
    def test_returns_next_date_after_asof(self):
        # yfinance.calendar returns a DataFrame OR dict; this test covers the
        # common DataFrame shape where index includes 'Earnings Date'.
        fake_calendar = {"Earnings Date": [dt.date(2026, 5, 11)]}
        fake_ticker = MagicMock()
        fake_ticker.calendar = fake_calendar
        with patch("yfinance.Ticker", return_value=fake_ticker):
            result = earnings_calendar.fetch_next_earnings(ticker="QUBT", asof=dt.date(2026, 4, 14))
        self.assertEqual(result, dt.date(2026, 5, 11))

    def test_skips_past_dates_pit_guard(self):
        # Dates ≤ asof must be skipped (PIT correctness — operator running
        # at asof shouldn't see a past earnings as "next").
        fake_ticker = MagicMock()
        fake_ticker.calendar = {"Earnings Date": [dt.date(2026, 4, 14)]}
        with patch("yfinance.Ticker", return_value=fake_ticker):
            result = earnings_calendar.fetch_next_earnings(ticker="QUBT", asof=dt.date(2026, 4, 14))
        self.assertIsNone(result)

    def test_returns_none_on_yfinance_exception(self):
        with patch("yfinance.Ticker", side_effect=RuntimeError("network")):
            self.assertIsNone(
                earnings_calendar.fetch_next_earnings(ticker="QUBT", asof=dt.date(2026, 4, 14))
            )

    def test_returns_none_when_no_calendar(self):
        fake_ticker = MagicMock()
        fake_ticker.calendar = None
        with patch("yfinance.Ticker", return_value=fake_ticker):
            self.assertIsNone(
                earnings_calendar.fetch_next_earnings(ticker="QUBT", asof=dt.date(2026, 4, 14))
            )

    def test_handles_dataframe_calendar_shape(self):
        # Older yfinance versions return a DataFrame with date-typed Series.
        df = pd.DataFrame(
            {"Earnings Date": [pd.Timestamp("2026-05-11"), pd.Timestamp("2026-08-12")]}
        )
        fake_ticker = MagicMock()
        fake_ticker.calendar = df
        with patch("yfinance.Ticker", return_value=fake_ticker):
            result = earnings_calendar.fetch_next_earnings(ticker="QUBT", asof=dt.date(2026, 4, 14))
        self.assertEqual(result, dt.date(2026, 5, 11))

    def test_handles_dataframe_with_earnings_date_in_index(self):
        # Some yfinance versions return Earnings Date in the index, not columns.
        df = pd.DataFrame({"col1": [pd.Timestamp("2026-05-11")]}, index=["Earnings Date"])
        fake_ticker = MagicMock()
        fake_ticker.calendar = df
        with patch("yfinance.Ticker", return_value=fake_ticker):
            result = earnings_calendar.fetch_next_earnings(ticker="QUBT", asof=dt.date(2026, 4, 14))
        self.assertEqual(result, dt.date(2026, 5, 11))

    def test_returns_none_for_unknown_calendar_type(self):
        # Non-dict, non-DataFrame → _extract_earnings_dates returns [].
        fake_ticker = MagicMock()
        fake_ticker.calendar = "unexpected string"
        with patch("yfinance.Ticker", return_value=fake_ticker):
            self.assertIsNone(
                earnings_calendar.fetch_next_earnings(ticker="QUBT", asof=dt.date(2026, 4, 14))
            )

    def test_handles_datetime_values(self):
        # Coerces datetime instances to date via .date().
        fake_ticker = MagicMock()
        fake_ticker.calendar = {"Earnings Date": [dt.datetime(2026, 5, 11, 14, 30)]}
        with patch("yfinance.Ticker", return_value=fake_ticker):
            result = earnings_calendar.fetch_next_earnings(ticker="QUBT", asof=dt.date(2026, 4, 14))
        self.assertEqual(result, dt.date(2026, 5, 11))


if __name__ == "__main__":
    unittest.main()
