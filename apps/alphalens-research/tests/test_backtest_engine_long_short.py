"""TDD red-phase tests for engine L/S decile diagnostic (Blocker #3, plan
2026-05-01).

Adds `bottom_n` parameter to `BacktestEngine` and L/S series accessors to
`BacktestReport`. Engine stays scorer-agnostic — the v7 Xing 2010 PRIMARY
(long-only bottom decile by Lasso-fitted return) is implemented at the
SCORER level (multiply score by -1), not the engine. Engine just exposes
both top-N and bottom-N legs when `bottom_n` is set.

Tests exercise `_build_rebalance_snapshot()` directly with synthesized
DataFrames — avoids HistoryStore plumbing.
"""

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import Mock

import pandas as pd
from alphalens_research.backtest.engine import BacktestEngine, BacktestReport


def _stub_scorer(_histories, _config) -> pd.DataFrame:
    return pd.DataFrame(columns=["ticker", "score"])


def _make_engine(top_n: int = 2, bottom_n: int | None = None) -> BacktestEngine:
    """Real callable scorer + Mock store; engine never invokes either in
    these unit tests (they target `_build_rebalance_snapshot()` directly).
    Mock without spec returns Mock for `MIN_BARS_REQUIRED` lookup which
    `int()` rejects — use a real function instead."""
    return BacktestEngine(
        history_store=Mock(),
        scorer=_stub_scorer,
        scorer_config={},
        holding_period=5,
        top_n=top_n,
        bottom_n=bottom_n,
        benchmark="SPY",
    )


def _scored_df(rows: list[tuple[str, float, float, float]]) -> pd.DataFrame:
    """Helper: rows = [(ticker, score, fwd_1d, fwd_holding), ...]."""
    return pd.DataFrame(rows, columns=["ticker", "score", "fwd_1d", "fwd_holding"])


class BottomNLegacyBackwardCompatTests(unittest.TestCase):
    def test_bottom_n_none_keeps_legacy_behavior(self):
        engine = _make_engine(top_n=2, bottom_n=None)
        scored = _scored_df(
            [
                ("AAA", 1.0, 0.01, 0.05),
                ("BBB", 2.0, 0.02, 0.06),
                ("CCC", 0.5, -0.01, 0.0),
            ]
        )
        snap = engine._build_rebalance_snapshot(date(2024, 1, 2), scored, scored)

        # Legacy fields populated as before
        self.assertEqual(snap.top_n_tickers, ["BBB", "AAA"])
        # New fields default to None when bottom_n is None
        self.assertIsNone(snap.bottom_n_tickers)
        self.assertIsNone(snap.bottom_n_scores)
        self.assertIsNone(snap.bottom_n_forward_returns)
        self.assertIsNone(snap.portfolio_return_short)

    def test_long_only_report_long_short_series_empty_when_bottom_n_none(self):
        report = BacktestReport(
            scorer_config={},
            holding_period=5,
            top_n=2,
            start=date(2024, 1, 1),
            end=date(2024, 1, 10),
            benchmark="SPY",
            universe_ticker_count=3,
        )
        self.assertTrue(report.portfolio_returns_long_short.empty)
        self.assertTrue(report.portfolio_returns_short.empty)


class BottomNSelectionTests(unittest.TestCase):
    def test_bottom_n_selects_ascending_score(self):
        engine = _make_engine(top_n=2, bottom_n=2)
        scored = _scored_df(
            [
                ("AAA", 5.0, 0.01, 0.05),
                ("BBB", 1.0, -0.02, -0.04),
                ("CCC", 3.0, 0.0, 0.01),
                ("DDD", 2.0, -0.01, -0.02),
                ("EEE", 4.0, 0.005, 0.03),
            ]
        )
        snap = engine._build_rebalance_snapshot(date(2024, 1, 2), scored, scored)

        # Top-2 by score desc: AAA (5.0), EEE (4.0)
        self.assertEqual(snap.top_n_tickers, ["AAA", "EEE"])
        # Bottom-2 by score asc: BBB (1.0), DDD (2.0)
        self.assertEqual(snap.bottom_n_tickers, ["BBB", "DDD"])

    def test_bottom_n_disjoint_from_top_n(self):
        engine = _make_engine(top_n=2, bottom_n=2)
        scored = _scored_df(
            [
                ("A", 1.0, 0.0, 0.0),
                ("B", 2.0, 0.0, 0.0),
                ("C", 3.0, 0.0, 0.0),
                ("D", 4.0, 0.0, 0.0),
                ("E", 5.0, 0.0, 0.0),
            ]
        )
        snap = engine._build_rebalance_snapshot(date(2024, 1, 2), scored, scored)
        overlap = set(snap.top_n_tickers) & set(snap.bottom_n_tickers)
        self.assertEqual(overlap, set())


class LongShortReturnTests(unittest.TestCase):
    def test_long_short_portfolio_return_subtracts_short_from_long(self):
        engine = _make_engine(top_n=1, bottom_n=1)
        scored = _scored_df(
            [
                ("HIGH_SCORE", 10.0, 0.02, 0.10),  # top, fwd_1d=0.02
                ("LOW_SCORE", 1.0, -0.01, -0.05),  # bottom, fwd_1d=-0.01
                ("MID", 5.0, 0.0, 0.0),  # filler
            ]
        )
        snap = engine._build_rebalance_snapshot(date(2024, 1, 2), scored, scored)

        # Long leg fwd_1d = 0.02; Short leg fwd_1d = -0.01
        # L/S = 0.02 - (-0.01) = 0.03
        self.assertAlmostEqual(snap.portfolio_return, 0.02, places=12)
        self.assertAlmostEqual(snap.portfolio_return_short, -0.01, places=12)
        ls = snap.portfolio_return - snap.portfolio_return_short
        self.assertAlmostEqual(ls, 0.03, places=12)

    def test_long_short_returns_series_alongside_long_only(self):
        # Two rebalance dates, both with long+short legs populated.
        engine = _make_engine(top_n=1, bottom_n=1)
        rows_d1 = _scored_df(
            [("H", 10.0, 0.02, 0.05), ("L", 1.0, -0.01, -0.03), ("M", 5.0, 0.0, 0.0)]
        )
        rows_d2 = _scored_df(
            [("H", 8.0, 0.015, 0.04), ("L", 2.0, -0.005, -0.02), ("M", 5.0, 0.0, 0.0)]
        )
        snap1 = engine._build_rebalance_snapshot(date(2024, 1, 2), rows_d1, rows_d1)
        snap2 = engine._build_rebalance_snapshot(date(2024, 1, 9), rows_d2, rows_d2)

        report = BacktestReport(
            scorer_config={},
            holding_period=5,
            top_n=1,
            start=date(2024, 1, 1),
            end=date(2024, 1, 10),
            benchmark="SPY",
            universe_ticker_count=3,
            rebalance_results=[snap1, snap2],
        )

        long_series = report.portfolio_returns
        ls_series = report.portfolio_returns_long_short
        short_series = report.portfolio_returns_short

        self.assertEqual(len(long_series), 2)
        self.assertEqual(len(ls_series), 2)
        self.assertEqual(len(short_series), 2)
        self.assertListEqual(list(long_series.index), list(ls_series.index))
        # First date: long=0.02, short=-0.01, L/S = 0.03
        self.assertAlmostEqual(ls_series.iloc[0], 0.03, places=12)
        # Second date: long=0.015, short=-0.005, L/S = 0.020
        self.assertAlmostEqual(ls_series.iloc[1], 0.020, places=12)


class LegacyApproximatelyUnchangedTests(unittest.TestCase):
    def test_legacy_top_n_only_path_approximately_unchanged(self):
        """With bottom_n=None, both portfolio_return and snapshot fields must
        match what the legacy engine produced bit-for-bit on the same input.
        Per zen 2026-05-01 use rel-tolerance 1e-6 for float drift safety
        (here arithmetic is straight-line so we expect bit-exact, but the
        contract is approximate equality).
        """
        engine = _make_engine(top_n=2, bottom_n=None)
        scored = _scored_df(
            [
                ("X", 1.5, 0.01, 0.04),
                ("Y", 2.5, 0.02, 0.05),
                ("Z", 0.5, -0.01, -0.02),
                ("W", 3.5, 0.03, 0.07),
            ]
        )
        snap = engine._build_rebalance_snapshot(date(2024, 1, 2), scored, scored)

        # Top-2 by score: W (3.5), Y (2.5). Equal-weight 1d return:
        # (0.03 + 0.02) / 2 = 0.025
        self.assertAlmostEqual(snap.portfolio_return, 0.025, delta=1e-6)
        self.assertEqual(snap.top_n_tickers, ["W", "Y"])
        # Holding return: (0.07 + 0.05) / 2 = 0.060
        self.assertAlmostEqual(snap.portfolio_return_holding, 0.060, delta=1e-6)


if __name__ == "__main__":
    unittest.main()
