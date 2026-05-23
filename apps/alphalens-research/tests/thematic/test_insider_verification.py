import datetime as dt
import unittest
from unittest.mock import patch

import pandas as pd
from alphalens_pipeline.thematic.verification import insider as insider_v


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
        with (
            patch.object(insider_v, "_load_form4_for_ticker", return_value=history),
            patch.object(insider_v, "_load_form4_for_insiders", return_value=history),
        ):
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
        with (
            patch.object(insider_v, "_load_form4_for_ticker", return_value=history),
            patch.object(insider_v, "_load_form4_for_insiders", return_value=history),
        ):
            result = insider_v.has_opportunistic_buy(
                ticker="BEEM",
                asof=dt.date(2026, 5, 15),
                lookback_days=30,
                usd_threshold=10_000,
            )
        self.assertFalse(result)

    def test_returns_none_when_no_form4_history_for_ticker(self):
        # No Form-4 records anywhere for this ticker = "we have no data" not
        # "insider activity was checked and absent". The orchestrator records
        # unknown so an operator can distinguish from real-no-signal cases.
        with patch.object(insider_v, "_load_form4_for_ticker", return_value=pd.DataFrame()):
            self.assertIsNone(
                insider_v.has_opportunistic_buy(
                    ticker="UNKN",
                    asof=dt.date(2026, 5, 15),
                    lookback_days=30,
                    usd_threshold=10_000,
                )
            )

    def test_returns_false_when_history_exists_but_window_empty(self):
        # Ticker has Form-4 history but no trades in the lookback window.
        # That IS a real "no recent insider activity" signal — gate FAILED,
        # not UNKNOWN. Distinguished from the no-data-at-all case above.
        old_history = pd.DataFrame(
            [
                _record("ins1", dt.date(2023, 3, 5), "P", 10, 1, "BEEM"),
                _record("ins1", dt.date(2024, 6, 15), "P", 10, 1, "BEEM"),
            ]
        )
        with patch.object(insider_v, "_load_form4_for_ticker", return_value=old_history):
            self.assertFalse(
                insider_v.has_opportunistic_buy(
                    ticker="BEEM",
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
        with (
            patch.object(insider_v, "_load_form4_for_ticker", return_value=history),
            patch.object(insider_v, "_load_form4_for_insiders", return_value=history),
        ):
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
        with (
            patch.object(insider_v, "_load_form4_for_ticker", return_value=history),
            patch.object(insider_v, "_load_form4_for_insiders", return_value=history),
        ):
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

    def test_cross_ticker_routine_trader_classified_routine(self):
        # Insider trades March across DIFFERENT tickers each year -> routine,
        # but on BEEM alone they have only one trade (May 2026). Previously
        # this would mislabel them as opportunistic; with cross-ticker loading
        # they classify ROUTINE and contribute zero to the signal.
        ticker_history = pd.DataFrame(
            [
                _record("multi", dt.date(2026, 5, 1), "P", 1000, 50, "BEEM"),
            ]
        )
        full_history = pd.DataFrame(
            [
                # 3y of March trades across various tickers
                _record("multi", dt.date(2023, 3, 5), "P", 10, 1, "AAPL"),
                _record("multi", dt.date(2024, 3, 6), "P", 10, 1, "MSFT"),
                _record("multi", dt.date(2025, 3, 7), "P", 10, 1, "GOOG"),
                # ticker history also visible in full set
                _record("multi", dt.date(2026, 5, 1), "P", 1000, 50, "BEEM"),
            ]
        )
        with (
            patch.object(insider_v, "_load_form4_for_ticker", return_value=ticker_history),
            patch.object(insider_v, "_load_form4_for_insiders", return_value=full_history),
        ):
            self.assertFalse(
                insider_v.has_opportunistic_buy(
                    ticker="BEEM",
                    asof=dt.date(2026, 5, 15),
                    lookback_days=30,
                    usd_threshold=10_000,
                )
            )

    def test_returns_none_on_loader_error(self):
        with patch.object(insider_v, "_load_form4_for_ticker", side_effect=RuntimeError("IO")):
            self.assertIsNone(
                insider_v.has_opportunistic_buy(
                    ticker="BEEM",
                    asof=dt.date(2026, 5, 15),
                    lookback_days=30,
                    usd_threshold=10_000,
                )
            )


class TestLoadFormFourPartitions(unittest.TestCase):
    def _seed(self, root, year: int, rows: list[dict]):
        part = root / f"transaction_year={year}"
        part.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_parquet(part / "compacted.parquet", index=False)

    def test_load_form4_partitions_year_filter(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._seed(root, 2022, [_record("ins", dt.date(2022, 6, 1), "P", 10, 1, "BEEM")])
            self._seed(root, 2026, [_record("ins", dt.date(2026, 6, 1), "P", 10, 1, "BEEM")])

            df = insider_v._load_form4_partitions(form4_root=root, years={2026})
            self.assertEqual(len(df), 1)
            self.assertEqual(df.iloc[0]["transaction_date"].year, 2026)

    def test_load_form4_partitions_ticker_filter(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._seed(
                root,
                2026,
                [
                    _record("a", dt.date(2026, 1, 1), "P", 10, 1, "BEEM"),
                    _record("a", dt.date(2026, 2, 1), "P", 10, 1, "OTHER"),
                ],
            )
            df = insider_v._load_form4_partitions(form4_root=root, years={2026}, ticker="BEEM")
            self.assertEqual(len(df), 1)

    def test_load_form4_partitions_insider_filter(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._seed(
                root,
                2026,
                [
                    _record("alice", dt.date(2026, 1, 1), "P", 10, 1, "BEEM"),
                    _record("bob", dt.date(2026, 2, 1), "P", 10, 1, "BEEM"),
                ],
            )
            df = insider_v._load_form4_partitions(
                form4_root=root, years={2026}, insider_ciks={"alice"}
            )
            self.assertEqual(len(df), 1)
            self.assertEqual(df.iloc[0]["reporting_owner_cik"], "alice")

    def test_load_form4_partitions_missing_root(self):
        from pathlib import Path

        df = insider_v._load_form4_partitions(form4_root=Path("/nonexistent/path"), years={2026})
        self.assertTrue(df.empty)

    def test_load_form4_partitions_empty_years(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            df = insider_v._load_form4_partitions(form4_root=Path(tmpdir), years=set())
            self.assertTrue(df.empty)

    def test_classification_years_includes_lookback(self):
        self.assertEqual(
            insider_v._classification_years(dt.date(2026, 5, 15)),
            {2023, 2024, 2025, 2026},
        )

    def test_load_form4_for_ticker_with_year_filter_uses_partitions(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._seed(
                root,
                2026,
                [_record("a", dt.date(2026, 6, 1), "P", 10, 1, "BEEM")],
            )
            df = insider_v._load_form4_for_ticker("BEEM", form4_root=root, years={2026})
            self.assertEqual(len(df), 1)

    def test_load_form4_for_ticker_legacy_full_scan(self):
        # When years=None, falls back to old glob-all behaviour
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._seed(
                root,
                2024,
                [_record("a", dt.date(2024, 1, 1), "P", 10, 1, "BEEM")],
            )
            df = insider_v._load_form4_for_ticker("BEEM", form4_root=root)
            self.assertEqual(len(df), 1)


if __name__ == "__main__":
    unittest.main()
