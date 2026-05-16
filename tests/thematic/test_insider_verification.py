import datetime as dt
import unittest
from unittest.mock import patch

import pandas as pd

from alphalens.thematic.verification import insider as insider_v


def _record(insider_cik, txn_date, code, shares, price, ticker="BEEM"):
    return {
        "issuer_cik": "0001000",
        "ticker": ticker,
        "accession_number": f"a-{insider_cik}-{txn_date}",
        "filed_date": txn_date,
        "reporting_owner_cik": insider_cik,
        "reporting_owner_name": f"INSIDER {insider_cik}",
        "transaction_date": txn_date,
        "transaction_code": code,
        "transaction_shares": float(shares),
        "transaction_price_per_share": float(price),
        "is_director": True,
        "is_officer": False,
        "is_ten_percent_owner": False,
        "acquired_disposed": "A" if code == "P" else "D",
        "is_amendment": False,
    }


class TestLoadFilteredForm4(unittest.TestCase):
    def test_filters_to_ticker_and_window(self):
        records = pd.DataFrame(
            [
                _record("1", dt.date(2026, 4, 15), "P", 100, 10, ticker="BEEM"),
                _record("1", dt.date(2026, 5, 10), "P", 200, 12, ticker="BEEM"),
                _record("2", dt.date(2026, 5, 10), "P", 50, 15, ticker="OTHER"),  # other ticker
                _record("3", dt.date(2025, 1, 1), "P", 99, 5, ticker="BEEM"),  # too old
            ]
        )
        filtered = insider_v.filter_records(
            records, ticker="BEEM", asof=dt.date(2026, 5, 15), lookback_days=60
        )
        self.assertEqual(len(filtered), 2)
        self.assertEqual(set(filtered["reporting_owner_cik"]), {"1"})


class TestHasOpportunisticBuy(unittest.TestCase):
    def test_returns_true_when_net_buy_above_threshold(self):
        # Insider with 3y prior history (eligible, opportunistic)
        history = pd.DataFrame(
            [
                _record("ins1", dt.date(2023, 3, 5), "P", 10, 1, "BEEM"),
                _record("ins1", dt.date(2024, 6, 15), "P", 10, 1, "BEEM"),
                _record("ins1", dt.date(2025, 9, 20), "P", 10, 1, "BEEM"),
                # Recent buy
                _record("ins1", dt.date(2026, 5, 1), "P", 1000, 50, "BEEM"),
            ]
        )
        with patch.object(insider_v, "_load_form4_for_ticker", return_value=history):
            result = insider_v.has_opportunistic_buy(
                ticker="BEEM",
                asof=dt.date(2026, 5, 15),
                lookback_days=30,
                usd_threshold=10_000,
            )
        self.assertTrue(result)

    def test_returns_false_when_below_threshold(self):
        history = pd.DataFrame(
            [
                _record("ins1", dt.date(2023, 3, 5), "P", 10, 1, "BEEM"),
                _record("ins1", dt.date(2024, 6, 15), "P", 10, 1, "BEEM"),
                _record("ins1", dt.date(2025, 9, 20), "P", 10, 1, "BEEM"),
                _record("ins1", dt.date(2026, 5, 1), "P", 10, 50, "BEEM"),  # only $500
            ]
        )
        with patch.object(insider_v, "_load_form4_for_ticker", return_value=history):
            result = insider_v.has_opportunistic_buy(
                ticker="BEEM",
                asof=dt.date(2026, 5, 15),
                lookback_days=30,
                usd_threshold=10_000,
            )
        self.assertFalse(result)

    def test_returns_false_when_no_records(self):
        with patch.object(insider_v, "_load_form4_for_ticker", return_value=pd.DataFrame()):
            self.assertFalse(
                insider_v.has_opportunistic_buy(
                    ticker="UNKN",
                    asof=dt.date(2026, 5, 15),
                    lookback_days=30,
                    usd_threshold=10_000,
                )
            )

    def test_returns_false_when_net_is_sales(self):
        history = pd.DataFrame(
            [
                _record("ins1", dt.date(2023, 3, 5), "P", 10, 1, "BEEM"),
                _record("ins1", dt.date(2024, 6, 15), "P", 10, 1, "BEEM"),
                _record("ins1", dt.date(2025, 9, 20), "P", 10, 1, "BEEM"),
                _record("ins1", dt.date(2026, 5, 1), "S", 1000, 50, "BEEM"),  # sale
            ]
        )
        with patch.object(insider_v, "_load_form4_for_ticker", return_value=history):
            self.assertFalse(
                insider_v.has_opportunistic_buy(
                    ticker="BEEM",
                    asof=dt.date(2026, 5, 15),
                    lookback_days=30,
                    usd_threshold=10_000,
                )
            )

    def test_routine_traders_excluded(self):
        # Insider trades March every year -> routine, not opportunistic
        history = pd.DataFrame(
            [
                _record("routine", dt.date(2023, 3, 5), "P", 10, 1, "BEEM"),
                _record("routine", dt.date(2024, 3, 6), "P", 10, 1, "BEEM"),
                _record("routine", dt.date(2025, 3, 7), "P", 10, 1, "BEEM"),
                _record("routine", dt.date(2026, 5, 1), "P", 1000, 50, "BEEM"),  # huge buy
            ]
        )
        with patch.object(insider_v, "_load_form4_for_ticker", return_value=history):
            # Wait — March is the routine month, but a May trade IS still opportunistic for
            # this insider IF we lock classification to year start. Per Cohen-Malloy paper,
            # classification IS at start of year — so the routine insider's classification
            # for 2026 is ROUTINE (from history), and routine insiders are EXCLUDED
            # entirely from the signal regardless of trade month.
            self.assertFalse(
                insider_v.has_opportunistic_buy(
                    ticker="BEEM",
                    asof=dt.date(2026, 5, 15),
                    lookback_days=30,
                    usd_threshold=10_000,
                )
            )

    def test_fails_closed_on_loader_error(self):
        with patch.object(insider_v, "_load_form4_for_ticker", side_effect=RuntimeError("IO")):
            self.assertFalse(
                insider_v.has_opportunistic_buy(
                    ticker="BEEM",
                    asof=dt.date(2026, 5, 15),
                    lookback_days=30,
                    usd_threshold=10_000,
                )
            )


if __name__ == "__main__":
    unittest.main()
