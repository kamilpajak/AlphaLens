import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock

import pandas as pd


def _history(closes_by_date: dict[str, float]) -> pd.DataFrame:
    idx = pd.DatetimeIndex(sorted(closes_by_date.keys()))
    return pd.DataFrame(
        {
            "open": [closes_by_date[d.strftime("%Y-%m-%d")] for d in idx],
            "high": [closes_by_date[d.strftime("%Y-%m-%d")] for d in idx],
            "low": [closes_by_date[d.strftime("%Y-%m-%d")] for d in idx],
            "close": [closes_by_date[d.strftime("%Y-%m-%d")] for d in idx],
            "volume": [1_000_000] * len(idx),
        },
        index=idx,
    )


def _shares_fact(shares: int, filed: str):
    from alphalens_research.data.alt_data.shares_outstanding import SharesFact

    return SharesFact(
        cik="0000000001",
        end_date=date.fromisoformat(filed) - timedelta(days=45),
        filed_date=date.fromisoformat(filed),
        shares=shares,
        form_type="10-Q",
        accession="a",
    )


def _cik_map(mapping: dict[str, str]):
    m = MagicMock()
    m.lookup.side_effect = lambda t: mapping.get(t.upper())
    return m


class TestCloseAsOf(unittest.TestCase):
    def test_returns_close_on_exact_date(self):
        from alphalens_research.data.alt_data.pit_universe import close_as_of

        hist = _history({"2024-06-03": 100.0, "2024-06-04": 101.0, "2024-06-05": 102.0})

        self.assertEqual(close_as_of(hist, date(2024, 6, 4)), 101.0)

    def test_returns_latest_before_asof(self):
        from alphalens_research.data.alt_data.pit_universe import close_as_of

        hist = _history({"2024-06-03": 100.0, "2024-06-04": 101.0, "2024-06-05": 102.0})

        # Weekend / non-trading day: take the most recent prior close.
        self.assertEqual(close_as_of(hist, date(2024, 6, 8)), 102.0)

    def test_asof_before_data_returns_none(self):
        from alphalens_research.data.alt_data.pit_universe import close_as_of

        hist = _history({"2024-06-03": 100.0})

        self.assertIsNone(close_as_of(hist, date(2024, 1, 1)))

    def test_empty_history_returns_none(self):
        from alphalens_research.data.alt_data.pit_universe import close_as_of

        self.assertIsNone(close_as_of(pd.DataFrame(), date(2024, 6, 3)))


class TestBuildPitUniverse(unittest.TestCase):
    def test_ticker_in_cap_band_included(self):
        from alphalens_research.data.alt_data.pit_universe import build_pit_universe

        # shares=10M, close=$50 → mcap=$500M, in band $300M-$3B
        tickers = build_pit_universe(
            asof=date(2024, 6, 30),
            shares_by_cik={"0000000001": [_shares_fact(10_000_000, "2024-05-01")]},
            histories={"AAPL": _history({"2024-06-28": 50.0})},
            cik_map=_cik_map({"AAPL": "0000000001"}),
        )

        self.assertEqual(tickers, ["AAPL"])

    def test_below_cap_band_excluded(self):
        from alphalens_research.data.alt_data.pit_universe import build_pit_universe

        # shares=1M, close=$10 → mcap=$10M, below $300M floor
        tickers = build_pit_universe(
            asof=date(2024, 6, 30),
            shares_by_cik={"0000000001": [_shares_fact(1_000_000, "2024-05-01")]},
            histories={"TINY": _history({"2024-06-28": 10.0})},
            cik_map=_cik_map({"TINY": "0000000001"}),
        )

        self.assertEqual(tickers, [])

    def test_above_cap_band_excluded(self):
        from alphalens_research.data.alt_data.pit_universe import build_pit_universe

        # shares=1B, close=$100 → mcap=$100B, above $3B ceiling
        tickers = build_pit_universe(
            asof=date(2024, 6, 30),
            shares_by_cik={"0000000001": [_shares_fact(1_000_000_000, "2024-05-01")]},
            histories={"HUGE": _history({"2024-06-28": 100.0})},
            cik_map=_cik_map({"HUGE": "0000000001"}),
        )

        self.assertEqual(tickers, [])

    def test_missing_cik_excluded(self):
        from alphalens_research.data.alt_data.pit_universe import build_pit_universe

        tickers = build_pit_universe(
            asof=date(2024, 6, 30),
            shares_by_cik={},
            histories={"NOPE": _history({"2024-06-28": 50.0})},
            cik_map=_cik_map({}),  # empty map
        )

        self.assertEqual(tickers, [])

    def test_missing_shares_data_excluded(self):
        from alphalens_research.data.alt_data.pit_universe import build_pit_universe

        tickers = build_pit_universe(
            asof=date(2024, 6, 30),
            shares_by_cik={"0000000001": []},  # known CIK but no shares facts
            histories={"AAPL": _history({"2024-06-28": 50.0})},
            cik_map=_cik_map({"AAPL": "0000000001"}),
        )

        self.assertEqual(tickers, [])

    def test_missing_price_excluded(self):
        from alphalens_research.data.alt_data.pit_universe import build_pit_universe

        tickers = build_pit_universe(
            asof=date(2024, 6, 30),
            shares_by_cik={"0000000001": [_shares_fact(10_000_000, "2024-05-01")]},
            histories={"AAPL": pd.DataFrame()},  # empty history
            cik_map=_cik_map({"AAPL": "0000000001"}),
        )

        self.assertEqual(tickers, [])

    def test_pit_filter_excludes_future_filed_shares(self):
        """Shares fact filed AFTER asof shouldn't leak back in time."""
        from alphalens_research.data.alt_data.pit_universe import build_pit_universe

        asof = date(2024, 3, 1)

        tickers = build_pit_universe(
            asof=asof,
            # Fact filed 2024-05-01 is AFTER asof 2024-03-01; must be excluded.
            shares_by_cik={"0000000001": [_shares_fact(10_000_000, "2024-05-01")]},
            histories={"AAPL": _history({"2024-02-28": 50.0})},
            cik_map=_cik_map({"AAPL": "0000000001"}),
        )

        self.assertEqual(tickers, [])

    def test_multiple_tickers_sorted(self):
        from alphalens_research.data.alt_data.pit_universe import build_pit_universe

        shares_by_cik = {
            "0000000001": [_shares_fact(10_000_000, "2024-05-01")],
            "0000000002": [_shares_fact(20_000_000, "2024-05-01")],
        }
        histories = {
            "ZZZ": _history({"2024-06-28": 50.0}),
            "AAA": _history({"2024-06-28": 50.0}),
        }
        cik_map = _cik_map({"ZZZ": "0000000001", "AAA": "0000000002"})

        tickers = build_pit_universe(
            asof=date(2024, 6, 30),
            shares_by_cik=shares_by_cik,
            histories=histories,
            cik_map=cik_map,
        )

        self.assertEqual(tickers, ["AAA", "ZZZ"])

    def test_custom_cap_band(self):
        from alphalens_research.data.alt_data.pit_universe import UniverseConfig, build_pit_universe

        # Custom narrow band: $100M-$500M
        tickers = build_pit_universe(
            asof=date(2024, 6, 30),
            shares_by_cik={"0000000001": [_shares_fact(10_000_000, "2024-05-01")]},
            histories={"AAPL": _history({"2024-06-28": 50.0})},  # $500M
            cik_map=_cik_map({"AAPL": "0000000001"}),
            config=UniverseConfig(cap_min_usd=100_000_000, cap_max_usd=500_000_000),
        )

        # Inclusive upper bound: $500M == max → keep.
        self.assertEqual(tickers, ["AAPL"])


if __name__ == "__main__":
    unittest.main()
