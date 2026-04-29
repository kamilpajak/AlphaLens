"""Tests for `quality_momentum_adapter` z-score guards.

The adapter z-scores cross-sectional `mom` and `roe` columns. Without a
zero-variance guard, a degenerate input (all tickers with identical mom
or roe — common in tiny universes / synthetic tests) divides by zero
and crashes mid-backtest. This regression test plus a non-degenerate
sanity-check pin the contract.
"""

from __future__ import annotations

import unittest
from datetime import date

import numpy as np
import pandas as pd

from scripts.experiment_quality_momentum_combo import quality_momentum_adapter


class _ConstantROE:
    """Stub fundamentals store returning a fixed ROE for every ticker."""

    def __init__(self, value: float):
        self._value = value

    def roe_ttm(self, ticker: str, asof: date) -> float:
        return self._value


def _flat_history(constant_close: float = 100.0, n_days: int = 260) -> pd.DataFrame:
    """Trivial price history — flat close, constant volume — long enough that
    the adapter's `len(df) >= 253` and 12-1m momentum lookbacks are satisfied."""
    end = pd.Timestamp("2024-01-31")
    idx = pd.bdate_range(end=end, periods=n_days)
    return pd.DataFrame(
        {"close": np.full(n_days, constant_close), "volume": np.full(n_days, 1_000_000.0)},
        index=idx,
    )


def _trended_history(
    end_close: float, daily_growth: float = 0.001, n_days: int = 260
) -> pd.DataFrame:
    """Geometric trend so `mom_12_1m = closes[-22] / closes[-253] - 1` is
    non-zero and varies across tickers."""
    end = pd.Timestamp("2024-01-31")
    idx = pd.bdate_range(end=end, periods=n_days)
    closes = end_close * np.exp(np.linspace(-daily_growth * n_days, 0, n_days))
    return pd.DataFrame(
        {"close": closes, "volume": np.full(n_days, 1_000_000.0)},
        index=idx,
    )


class QualityMomentumAdapterGuardTests(unittest.TestCase):
    def test_zero_variance_inputs_handled_gracefully(self):
        """All tickers identical → std=0 for both mom and roe → without the
        guard the adapter divides by zero. Post-fix: scores are finite (z=0
        for the flat columns), no exception."""
        histories = {f"T{i}": _flat_history() for i in range(3)}
        config = {
            "benchmark": "SPY",
            "_adv_min_usd": 0.0,
            "_fundamentals": _ConstantROE(0.10),
        }
        df = quality_momentum_adapter(histories, config)
        self.assertEqual(len(df), 3)
        # All scores must be finite (no NaN/Inf from div-by-zero).
        self.assertTrue(np.isfinite(df["score"]).all())
        # With both columns degenerate, every ticker's score is identical.
        self.assertEqual(df["score"].nunique(), 1)

    def test_non_degenerate_inputs_still_z_scored(self):
        """Sanity: when momentum genuinely varies across tickers, z-scoring
        works as before — non-zero spread, winsorisation at ±3 inactive on
        moderate inputs, score is sum of z_mom + z_roe."""
        histories = {
            "A": _trended_history(end_close=120.0, daily_growth=0.002),
            "B": _trended_history(end_close=110.0, daily_growth=0.001),
            "C": _trended_history(end_close=100.0, daily_growth=0.0005),
        }

        # Vary ROE across tickers so z_roe is also informative.
        class _PerTickerROE:
            def roe_ttm(self, ticker, asof):
                return {"A": 0.20, "B": 0.10, "C": 0.05}[ticker]

        config = {
            "benchmark": "SPY",
            "_adv_min_usd": 0.0,
            "_fundamentals": _PerTickerROE(),
        }
        df = quality_momentum_adapter(histories, config)
        self.assertEqual(len(df), 3)
        # Scores should differ — both factors carry signal.
        self.assertGreater(df["score"].nunique(), 1)
        # A has highest momentum AND highest ROE → must rank top.
        self.assertEqual(df.iloc[0]["ticker"], "A")
        self.assertTrue(np.isfinite(df["score"]).all())


if __name__ == "__main__":
    unittest.main()
