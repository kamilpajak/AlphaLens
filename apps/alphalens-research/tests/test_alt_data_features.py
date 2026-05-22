"""Tests for v4 v2 alt-data feature joiner.

Validates per-feature construction matches the locked pre-reg JSON at
docs/research/preregistration/params_alt_data_screener_v2_2026_04_30.json.

Strategy: mock the underlying stores (HistoryStore, ParquetInsiderScorer,
FosterSUEStore, PolygonShortInterestClient, EDGAR companyfacts dir) and verify
that the joiner correctly applies decays, ranks, truncations, and cross-
sectional standardization per the pre-reg.
"""

from __future__ import annotations

import json
import math
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd


def _build_history_store(closes_per_ticker: dict[str, pd.Series]) -> MagicMock:
    """Mock HistoryStore.truncate_to to return per-ticker OHLCV up to asof."""
    store = MagicMock()

    def _truncate_to(ticker, asof):
        s = closes_per_ticker.get(ticker.upper())
        if s is None or s.empty:
            return pd.DataFrame(columns=["close", "volume"])
        cut = s.loc[: pd.Timestamp(asof)]
        df = pd.DataFrame(
            {"close": cut.values, "volume": np.full(len(cut), 1_000_000)}, index=cut.index
        )
        return df

    store.truncate_to.side_effect = _truncate_to
    return store


class TestFeatureSchema(unittest.TestCase):
    """The feature whitelist must match the v4 v2 pre-reg JSON exactly."""

    def test_feature_names_match_pre_reg_v2(self):
        from alphalens_research.screeners.alt_data.features import FEATURE_NAMES

        expected = (
            "earnings_sue_naive_4q_decayed",
            "earnings_pead_5d_post_decayed",
            "earnings_recency_days",
            "short_interest_pct_float_change_60d",
            "rank_short_interest_pct_float",
            "log1p_days_to_cover",
            "insider_log_count",
            "insider_log_dollar",
            "rank_realized_downside_skew_60d",
            "filing_density_4q",
        )
        self.assertEqual(FEATURE_NAMES, expected)

    def test_feature_names_match_pre_reg_json(self):
        """Read the pre-reg JSON directly and assert byte-for-byte alignment."""
        from alphalens_research.screeners.alt_data.features import FEATURE_NAMES

        repo = Path(__file__).resolve().parent.parent
        json_path = (
            repo / "docs/research/preregistration/params_alt_data_screener_v2_2026_04_30.json"
        )
        spec = json.loads(json_path.read_text())
        self.assertEqual(
            list(FEATURE_NAMES),
            spec["params_frozen"]["feature_whitelist"],
        )


class TestDecayMultiplier(unittest.TestCase):
    """SUE and PEAD are multiplied by exp(-recency/30) per pre-reg."""

    def test_decay_zero_recency_equals_one(self):
        from alphalens_research.screeners.alt_data.features import _decay_multiplier

        self.assertAlmostEqual(_decay_multiplier(0), 1.0)

    def test_decay_30d_equals_e_inverse(self):
        from alphalens_research.screeners.alt_data.features import _decay_multiplier

        self.assertAlmostEqual(_decay_multiplier(30), 1.0 / math.e, places=6)

    def test_decay_60d_equals_e_inverse_squared(self):
        from alphalens_research.screeners.alt_data.features import _decay_multiplier

        self.assertAlmostEqual(_decay_multiplier(60), 1.0 / (math.e * math.e), places=6)


class TestPEADTruncation(unittest.TestCase):
    """PEAD requires (filing_date + 5 BD) <= asof — per zen Objection 2A."""

    def test_pead_returns_zero_when_no_eligible_filing(self):
        from alphalens_research.screeners.alt_data.features import _pead_5d_post

        # Filing 3 days ago — not yet 5 BD elapsed
        recent_filing = date(2024, 6, 28)  # Friday
        asof = date(2024, 7, 1)  # Monday
        # 5 BD from Fri 6/28 = Fri 7/5. asof=7/1 < 7/5 → not eligible.
        store = _build_history_store(
            {
                "AAPL": pd.Series(
                    [100.0, 101.0, 102.0, 103.0, 104.0],
                    index=pd.date_range("2024-06-25", "2024-06-29"),
                )
            }
        )
        result = _pead_5d_post(
            history_store=store,
            ticker="AAPL",
            asof=asof,
            most_recent_filing=recent_filing,
        )
        self.assertEqual(result, 0.0)

    def test_pead_returns_zero_when_no_filing_in_lookback(self):
        from alphalens_research.screeners.alt_data.features import _pead_5d_post

        # Filing >90 days before asof — not eligible (out of lookback window)
        store = _build_history_store({})
        result = _pead_5d_post(
            history_store=store,
            ticker="AAPL",
            asof=date(2024, 7, 1),
            most_recent_filing=date(2024, 1, 1),
        )
        self.assertEqual(result, 0.0)

    def test_pead_returns_5d_return_when_eligible(self):
        from alphalens_research.screeners.alt_data.features import _pead_5d_post

        # Filing 2024-05-01 (Wednesday). +5 BD = 2024-05-08 (Wed).
        # asof 2024-05-15 — gate satisfied. 5d return is from close[2024-05-01]
        # to close[2024-05-08].
        # Build a fake price series where close on 5/1=100 and close on 5/8=110 → 10% return
        idx = pd.date_range("2024-04-29", "2024-05-15")
        # Map dates to closes: 5/1 -> 100, 5/8 -> 110, others linear interp doesn't matter
        closes = pd.Series(np.linspace(95, 115, len(idx)), index=idx)
        # Force exact values at filing date and +5BD
        closes.loc[pd.Timestamp("2024-05-01")] = 100.0
        closes.loc[pd.Timestamp("2024-05-08")] = 110.0
        store = _build_history_store({"AAPL": closes})
        result = _pead_5d_post(
            history_store=store,
            ticker="AAPL",
            asof=date(2024, 5, 15),
            most_recent_filing=date(2024, 5, 1),
        )
        # 5BD return Wed 5/1 → Wed 5/8: (110/100 - 1) = 0.10
        self.assertAlmostEqual(result, 0.10, places=4)


class TestDownsideSkew(unittest.TestCase):
    """Realized downside-skew = std(neg returns) / std(pos returns) over 60d."""

    def test_downside_skew_equals_one_when_symmetric(self):
        from alphalens_research.screeners.alt_data.features import _realized_downside_skew_60d

        # Symmetric returns: alternating +1% / -1%
        rets = np.array([0.01, -0.01] * 30)
        skew = _realized_downside_skew_60d(rets)
        self.assertAlmostEqual(skew, 1.0, places=4)

    def test_downside_skew_greater_than_one_when_left_skewed(self):
        from alphalens_research.screeners.alt_data.features import _realized_downside_skew_60d

        # Bigger negatives than positives
        rets = np.array([0.005] * 30 + [-0.05] * 30)
        skew = _realized_downside_skew_60d(rets)
        self.assertGreater(skew, 1.0)

    def test_downside_skew_returns_nan_on_short_window(self):
        from alphalens_research.screeners.alt_data.features import _realized_downside_skew_60d

        rets = np.array([0.01] * 10)
        skew = _realized_downside_skew_60d(rets)
        self.assertTrue(np.isnan(skew))


class TestFilingDensity(unittest.TestCase):
    """Filing density = count of distinct filing dates in trailing 252 BD."""

    def test_filing_density_counts_distinct_dates(self):
        from alphalens_research.screeners.alt_data.features import _filing_density_4q

        # 6 distinct filing dates within the lookback window
        filings = [
            date(2023, 11, 15),  # within trailing 252 BD of 2024-08-30
            date(2024, 1, 12),
            date(2024, 1, 12),  # duplicate same day → counted once
            date(2024, 4, 15),
            date(2024, 5, 20),
            date(2024, 7, 1),
            date(2024, 8, 1),
            date(2023, 1, 1),  # outside lookback → excluded
        ]
        result = _filing_density_4q(filings, asof=date(2024, 8, 30))
        # Distinct dates within lookback: 2023-11-15, 2024-01-12, 2024-04-15,
        # 2024-05-20, 2024-07-01, 2024-08-01 = 6
        self.assertEqual(result, 6)

    def test_filing_density_caps_at_30(self):
        from alphalens_research.screeners.alt_data.features import _filing_density_4q

        filings = [date(2024, 1, 1) + timedelta(days=i * 5) for i in range(50)]
        result = _filing_density_4q(filings, asof=date(2024, 12, 31))
        self.assertLessEqual(result, 30)


class TestBuildFeatureFrame(unittest.TestCase):
    """End-to-end joiner test with mocked stores."""

    def _mocks_for_universe(self, universe, asof):
        """Build a self-consistent set of mocks for an end-to-end joiner test."""
        from alphalens_research.data.alt_data.polygon_short_interest import (
            ShortInterestRecord,
        )

        # Mock HistoryStore: 252 days of synthetic OHLCV per ticker
        idx = pd.date_range(end=pd.Timestamp(asof), periods=252, freq="B")
        closes = {}
        for i, t in enumerate(universe):
            # Generate a simple price series with some downside skew variation
            base = 100.0 + i * 5
            noise = np.random.RandomState(i).normal(0, base * 0.01, 252)
            # Inject mild downside skew: slightly larger negatives
            noise = np.where(noise < 0, noise * (1 + i * 0.1), noise)
            prices = base + np.cumsum(noise)
            prices = np.clip(prices, base * 0.5, base * 2.0)
            closes[t] = pd.Series(prices, index=idx)
        history_store = _build_history_store(closes)

        # Mock insider scorer
        insider_scorer = MagicMock()
        insider_scorer.features_as_of.side_effect = lambda t, a: {
            "insider_count": (hash(t) % 5),
            "aggregate_dollar": (hash(t) % 5) * 100_000.0,
            "cluster_window_days": 30.0,
        }

        # Mock SUE store
        sue_store = MagicMock()
        sue_store.sue.side_effect = lambda t, a: float(hash(t) % 7) - 3.0  # range [-3, +3]

        # Mock Polygon SI client
        si_client = MagicMock()

        def _features_as_of(t, a):
            shares = 1_000_000_000
            si = (hash(t) % 100) * 1_000_000  # 0 to 99M shares
            return ShortInterestRecord(
                settlement_date=a - timedelta(days=15),
                ticker=t.upper(),
                short_interest=si,
                avg_daily_volume=10_000_000,
                days_to_cover=si / 10_000_000,
            )

        si_client.features_as_of.side_effect = _features_as_of

        # Provide history for change-60d feature
        def _fetch_ticker(t, refresh=False):
            return pd.DataFrame(
                {
                    "short_interest": [(hash(t) % 100) * 800_000, (hash(t) % 100) * 1_000_000],
                    "avg_daily_volume": [9_000_000, 10_000_000],
                    "days_to_cover": [(hash(t) % 100) * 0.08, (hash(t) % 100) * 0.10],
                },
                index=pd.DatetimeIndex(
                    [
                        pd.Timestamp(asof) - pd.Timedelta(days=80),
                        pd.Timestamp(asof) - pd.Timedelta(days=15),
                    ],
                    name="settlement_date",
                ),
            )

        si_client.fetch_ticker.side_effect = _fetch_ticker

        # Mock shares_outstanding lookup
        shares_lookup = MagicMock()
        shares_lookup.return_value = 1_000_000_000  # constant 1B shares

        # Mock filing-date stream
        filings_lookup = MagicMock()
        filings_lookup.side_effect = lambda t, a: [
            a - timedelta(days=20),  # most recent filing 20 days ago
            a - timedelta(days=120),  # prior quarter
        ]

        return history_store, insider_scorer, sue_store, si_client, shares_lookup, filings_lookup

    def test_joiner_produces_expected_schema(self):
        from alphalens_research.screeners.alt_data.features import (
            FEATURE_NAMES,
            build_feature_frame,
        )

        universe = ["AAPL", "MSFT", "NVDA"]
        asof = date(2024, 6, 28)
        hs, ins, sue, si, shares, fil = self._mocks_for_universe(universe, asof)

        df = build_feature_frame(
            history_store=hs,
            insider_scorer=ins,
            sue_store=sue,
            polygon_si_client=si,
            shares_lookup=shares,
            filings_lookup=fil,
            universe=universe,
            asof_dates=[asof],
        )

        # Schema check
        expected_cols = ["asof", "ticker", *FEATURE_NAMES]
        self.assertEqual(list(df.columns), expected_cols)
        # One row per ticker
        self.assertEqual(len(df), 3)
        # All tickers present
        self.assertEqual(set(df["ticker"]), set(universe))

    def test_joiner_computes_cross_sectional_ranks(self):
        from alphalens_research.screeners.alt_data.features import build_feature_frame

        universe = ["AAPL", "MSFT", "NVDA", "TSLA", "META"]
        asof = date(2024, 6, 28)
        hs, ins, sue, si, shares, fil = self._mocks_for_universe(universe, asof)

        df = build_feature_frame(
            history_store=hs,
            insider_scorer=ins,
            sue_store=sue,
            polygon_si_client=si,
            shares_lookup=shares,
            filings_lookup=fil,
            universe=universe,
            asof_dates=[asof],
        )

        # Ranks must be in [0, 1]
        for col in ("rank_short_interest_pct_float", "rank_realized_downside_skew_60d"):
            self.assertTrue(df[col].between(0, 1).all(), f"{col} out of [0,1]")

        # Ranks should differ across tickers (variation present)
        self.assertGreater(df["rank_short_interest_pct_float"].nunique(), 1)


if __name__ == "__main__":
    unittest.main()
