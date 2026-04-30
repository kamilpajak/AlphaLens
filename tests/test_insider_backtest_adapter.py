import unittest
from datetime import date
from unittest.mock import MagicMock

import pandas as pd


def _history(dates: list[str]) -> pd.DataFrame:
    idx = pd.DatetimeIndex(dates)
    return pd.DataFrame(
        {
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume": 1000,
        },
        index=idx,
    )


class TestAdapterContract(unittest.TestCase):
    def test_returns_dataframe_with_expected_columns(self):
        from alphalens.archive.screeners.insider.backtest_adapter import insider_scorer_adapter

        store = MagicMock()
        store.features_as_of.side_effect = lambda t, asof: {
            "AAPL": {
                "insider_count": 4,
                "aggregate_dollar": 1e6,
                "cluster_window_days": 30,
                "asof": str(asof),
            },
            "MSFT": {
                "insider_count": 3,
                "aggregate_dollar": 5e5,
                "cluster_window_days": 30,
                "asof": str(asof),
            },
            "NVDA": None,
        }.get(t)

        histories = {
            "AAPL": _history(["2025-03-18", "2025-03-19", "2025-03-20"]),
            "MSFT": _history(["2025-03-18", "2025-03-19", "2025-03-20"]),
            "NVDA": _history(["2025-03-18", "2025-03-19", "2025-03-20"]),
        }

        df = insider_scorer_adapter(histories, {"_insider_store": store})

        self.assertIn("ticker", df.columns)
        self.assertIn("score", df.columns)
        self.assertIn("insider_count", df.columns)
        self.assertIn("aggregate_dollar", df.columns)

    def test_excludes_tickers_without_cluster(self):
        from alphalens.archive.screeners.insider.backtest_adapter import insider_scorer_adapter

        store = MagicMock()
        store.features_as_of.side_effect = lambda t, asof: (
            {
                "insider_count": 3,
                "aggregate_dollar": 1.0,
                "cluster_window_days": 30,
                "asof": str(asof),
            }
            if t == "AAPL"
            else None
        )
        histories = {
            "AAPL": _history(["2025-03-20"]),
            "NOPE": _history(["2025-03-20"]),
        }

        df = insider_scorer_adapter(histories, {"_insider_store": store})

        self.assertEqual(list(df["ticker"]), ["AAPL"])

    def test_empty_histories_returns_empty_df_with_columns(self):
        from alphalens.archive.screeners.insider.backtest_adapter import insider_scorer_adapter

        df = insider_scorer_adapter({}, {"_insider_store": MagicMock()})

        self.assertTrue(df.empty)
        self.assertIn("ticker", df.columns)
        self.assertIn("score", df.columns)

    def test_no_cluster_anywhere_returns_empty_df(self):
        from alphalens.archive.screeners.insider.backtest_adapter import insider_scorer_adapter

        store = MagicMock()
        store.features_as_of.return_value = None

        histories = {
            "AAPL": _history(["2025-03-20"]),
            "MSFT": _history(["2025-03-20"]),
        }

        df = insider_scorer_adapter(histories, {"_insider_store": store})

        self.assertTrue(df.empty)
        self.assertIn("ticker", df.columns)

    def test_missing_store_raises_keyerror(self):
        """Explicit failure when wiring is wrong — better than silently empty."""
        from alphalens.archive.screeners.insider.backtest_adapter import insider_scorer_adapter

        with self.assertRaises(KeyError):
            insider_scorer_adapter(
                {"AAPL": _history(["2025-03-20"])},
                {},
            )


class TestAsofInference(unittest.TestCase):
    def test_uses_max_across_histories(self):
        from alphalens.archive.screeners.insider.backtest_adapter import insider_scorer_adapter

        captured_asofs = []

        def fake_features(ticker, asof):
            captured_asofs.append(asof)

        store = MagicMock()
        store.features_as_of.side_effect = fake_features

        histories = {
            "AAPL": _history(["2025-03-18", "2025-03-19"]),
            "MSFT": _history(["2025-03-19", "2025-03-20"]),  # extends 1 day further
        }

        insider_scorer_adapter(histories, {"_insider_store": store})

        # All features_as_of calls must use the max index across both frames.
        self.assertTrue(all(a == date(2025, 3, 20) for a in captured_asofs))

    def test_no_valid_asof_returns_empty(self):
        from alphalens.archive.screeners.insider.backtest_adapter import insider_scorer_adapter

        store = MagicMock()
        histories = {
            "AAPL": pd.DataFrame(columns=["open", "high", "low", "close", "volume"]),
        }

        df = insider_scorer_adapter(histories, {"_insider_store": store})

        self.assertTrue(df.empty)
        store.features_as_of.assert_not_called()


class TestScoringDesign(unittest.TestCase):
    def test_score_equals_insider_count(self):
        from alphalens.archive.screeners.insider.backtest_adapter import insider_scorer_adapter

        store = MagicMock()
        store.features_as_of.side_effect = lambda t, asof: {
            "A": {
                "insider_count": 3,
                "aggregate_dollar": 100,
                "cluster_window_days": 30,
                "asof": str(asof),
            },
            "B": {
                "insider_count": 5,
                "aggregate_dollar": 50,
                "cluster_window_days": 30,
                "asof": str(asof),
            },
            "C": {
                "insider_count": 4,
                "aggregate_dollar": 200,
                "cluster_window_days": 30,
                "asof": str(asof),
            },
        }[t]

        histories = {
            "A": _history(["2025-03-20"]),
            "B": _history(["2025-03-20"]),
            "C": _history(["2025-03-20"]),
        }

        df = insider_scorer_adapter(histories, {"_insider_store": store})

        # Sorted by score (= insider_count) descending: B(5), C(4), A(3).
        scores = dict(zip(df["ticker"], df["score"]))
        self.assertEqual(scores["B"], 5.0)
        self.assertEqual(scores["C"], 4.0)
        self.assertEqual(scores["A"], 3.0)


class TestMinBarsRequired(unittest.TestCase):
    def test_attribute_is_zero(self):
        from alphalens.archive.screeners.insider.backtest_adapter import insider_scorer_adapter

        self.assertEqual(insider_scorer_adapter.MIN_BARS_REQUIRED, 0)


class TestConfigPassthrough(unittest.TestCase):
    def test_benchmark_ticker_skipped(self):
        """If config declares a benchmark that appears in histories, skip it."""
        from alphalens.archive.screeners.insider.backtest_adapter import insider_scorer_adapter

        store = MagicMock()
        store.features_as_of.side_effect = lambda t, asof: (
            {
                "insider_count": 3,
                "aggregate_dollar": 1,
                "cluster_window_days": 30,
                "asof": str(asof),
            }
            if t != "SPY"
            else None
        )

        histories = {
            "SPY": _history(["2025-03-20"]),
            "AAPL": _history(["2025-03-20"]),
        }

        df = insider_scorer_adapter(histories, {"_insider_store": store, "benchmark": "SPY"})

        self.assertNotIn("SPY", list(df["ticker"]))
        self.assertIn("AAPL", list(df["ticker"]))
        store.features_as_of.assert_called_once()  # only AAPL queried


if __name__ == "__main__":
    unittest.main()
