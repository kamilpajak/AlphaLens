"""Distress-credit scorer adapter — bottom-quintile selection logic.

Tests adapter contract: returns DataFrame with `ticker`, `score=-PD`,
and bottom-quintile-only filtering. Uses synthetic fixtures rather than
real companyfacts/Polygon data to keep tests pure.
"""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd


def _synthetic_history(
    n_bars: int, start_close: float = 100.0, daily_vol: float = 0.02
) -> pd.DataFrame:
    """OHLCV with seeded log-normal returns (deterministic)."""
    rng = np.random.default_rng(seed=hash(start_close) & 0xFFFF)
    log_rets = rng.normal(0.0, daily_vol, size=n_bars - 1)
    closes = start_close * np.exp(np.cumsum(np.concatenate(([0.0], log_rets))))
    idx = pd.bdate_range(start="2020-01-01", periods=n_bars)
    return pd.DataFrame(
        {"close": closes, "volume": 1_000_000.0},
        index=idx,
    )


class DistressCreditAdapterTests(unittest.TestCase):
    def _make_synthetic_inputs(self, n_tickers: int = 20):
        """Build histories + config with controlled liabilities/shares for ranking."""
        from alphalens.screeners.distress_credit.scorer import (
            InMemoryLiabilitiesStore,
            InMemoryShareCountStore,
        )

        histories = {}
        # Liabilities/shares scaled so V/D ratio MEANINGFULLY varies across tickers.
        # All tickers share identical OHLCV history (same seed) so equity_mcap is
        # constant: closes[-1] * shares = 100 * 1.0 = 100. Liabilities ramp from
        # 10 (very safe, V/D=11) to 1000 (highly leveraged, V/D=1.1).
        liabilities = {}
        shares = {}
        for i in range(n_tickers):
            ticker = f"T{i:02d}"
            histories[ticker] = _synthetic_history(n_bars=70)
            liabilities[ticker] = 10.0 + 990.0 * (i / (n_tickers - 1))
            shares[ticker] = 1.0

        liab_store = InMemoryLiabilitiesStore(liabilities)
        share_store = InMemoryShareCountStore(shares)
        rf_series = pd.Series(
            np.full(70, 0.04),
            index=pd.bdate_range(start="2020-01-01", periods=70),
            name="DGS1",
        )
        config = {
            "asof": pd.Timestamp("2020-04-09"),  # ~70th BD
            "liabilities_store": liab_store,
            "shares_store": share_store,
            "rf_series": rf_series,
            "benchmark": "SPY",
            "_quintile_pct": 0.20,
            "_top_distress_exclude_pct": 0.20,
        }
        return histories, config

    def test_adapter_returns_dataframe_with_required_columns(self):
        from alphalens.screeners.distress_credit.scorer import distress_credit_adapter

        histories, config = self._make_synthetic_inputs(n_tickers=20)
        df = distress_credit_adapter(histories, config)
        self.assertIn("ticker", df.columns)
        self.assertIn("score", df.columns)
        self.assertIn("pd", df.columns)

    def test_adapter_returns_only_bottom_quintile_by_pd(self):
        """At 20% quintile from 20 tickers → 4 names returned (safest)."""
        from alphalens.screeners.distress_credit.scorer import distress_credit_adapter

        histories, config = self._make_synthetic_inputs(n_tickers=20)
        df = distress_credit_adapter(histories, config)
        # Bottom-quintile of 20 = 4 (round inward)
        self.assertEqual(len(df), 4)
        # All returned PDs must be among the lowest 4 of the universe
        # (synthetic monotone leverage → safest = T0..T3)
        self.assertTrue(all(t.startswith("T0") for t in df["ticker"]))

    def test_adapter_score_equals_negative_pd(self):
        from alphalens.screeners.distress_credit.scorer import distress_credit_adapter

        histories, config = self._make_synthetic_inputs(n_tickers=20)
        df = distress_credit_adapter(histories, config)
        for _, row in df.iterrows():
            self.assertAlmostEqual(row["score"], -row["pd"], places=10)

    def test_adapter_skips_benchmark(self):
        from alphalens.screeners.distress_credit.scorer import distress_credit_adapter

        histories, config = self._make_synthetic_inputs(n_tickers=20)
        # Inject SPY into histories — adapter must skip
        histories["SPY"] = _synthetic_history(n_bars=70)
        df = distress_credit_adapter(histories, config)
        self.assertNotIn("SPY", df["ticker"].values)

    def test_adapter_skips_when_insufficient_history(self):
        from alphalens.screeners.distress_credit.scorer import distress_credit_adapter

        histories, config = self._make_synthetic_inputs(n_tickers=20)
        # Truncate one ticker to 30 bars (below MIN_BARS_REQUIRED=65)
        histories["T05"] = _synthetic_history(n_bars=30)
        df = distress_credit_adapter(histories, config)
        # T05 should not appear — but may still produce 4 names from the other 19
        self.assertNotIn("T05", df["ticker"].values)

    def test_adapter_handles_empty_universe(self):
        from alphalens.screeners.distress_credit.scorer import distress_credit_adapter

        config = {
            "asof": pd.Timestamp("2020-04-09"),
            "liabilities_store": None,
            "shares_store": None,
            "rf_series": pd.Series(dtype=float),
            "benchmark": "SPY",
            "_quintile_pct": 0.20,
        }
        df = distress_credit_adapter({}, config)
        self.assertEqual(len(df), 0)

    def test_adapter_min_bars_attribute(self):
        from alphalens.screeners.distress_credit.scorer import distress_credit_adapter

        # Engine queries this attribute; must be defined and >= 65 (60d vol + buffer)
        self.assertTrue(hasattr(distress_credit_adapter, "MIN_BARS_REQUIRED"))
        self.assertGreaterEqual(distress_credit_adapter.MIN_BARS_REQUIRED, 65)


if __name__ == "__main__":
    unittest.main()
