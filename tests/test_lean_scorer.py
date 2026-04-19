import unittest

import numpy as np
import pandas as pd


def _history(close_series, volume_series=None):
    """Build an OHLCV DataFrame treating close as a flat OHLC."""
    n = len(close_series)
    vol = volume_series if volume_series is not None else [100_000] * n
    return pd.DataFrame(
        {
            "open": close_series,
            "high": close_series,
            "low": close_series,
            "close": close_series,
            "volume": vol,
        }
    )


def _default_config(**overrides):
    cfg = {
        "weight_roc20": 0.2,
        "weight_roc60": 0.2,
        "weight_volume_surprise": 0.2,
        "weight_trend_strength": 0.2,
        "weight_breakout": 0.1,
        "weight_near_high": 0.1,
        "roc_short": 5,
        "roc_medium": 20,
        "roc_long": 60,
        "sma_short": 20,
        "sma_medium": 50,
        "sma_long": 200,
        "volume_window": 20,
        "breakout_window": 20,
        "near_high_window": 60,
        "breakout_volume_multiple": 1.5,
        "min_price": 5.0,
        "max_price": 200.0,
        "min_avg_dollar_volume": 1_000_000.0,
        "top_n": 30,
    }
    cfg.update(overrides)
    return cfg


class TestGuardrails(unittest.TestCase):
    def test_passes_when_price_and_volume_ok(self):
        from alphalens.lean_screener.lean_project.scorer import guardrails_pass

        df = _history([50.0] * 30, [100_000] * 30)
        self.assertTrue(
            guardrails_pass(df, min_price=5, max_price=100, min_avg_dollar_volume=1_000_000)
        )

    def test_rejects_below_min_price(self):
        from alphalens.lean_screener.lean_project.scorer import guardrails_pass

        df = _history([2.0] * 30, [100_000] * 30)
        self.assertFalse(
            guardrails_pass(df, min_price=5, max_price=100, min_avg_dollar_volume=1)
        )

    def test_rejects_above_max_price(self):
        from alphalens.lean_screener.lean_project.scorer import guardrails_pass

        df = _history([500.0] * 30, [1000] * 30)
        self.assertFalse(
            guardrails_pass(df, min_price=5, max_price=100, min_avg_dollar_volume=1)
        )

    def test_rejects_thin_liquidity(self):
        from alphalens.lean_screener.lean_project.scorer import guardrails_pass

        df = _history([50.0] * 30, [100] * 30)  # $5k/day
        self.assertFalse(
            guardrails_pass(df, min_price=5, max_price=100, min_avg_dollar_volume=1_000_000)
        )

    def test_rejects_short_history(self):
        from alphalens.lean_screener.lean_project.scorer import guardrails_pass

        df = _history([50.0] * 5)
        self.assertFalse(
            guardrails_pass(df, min_price=5, max_price=100, min_avg_dollar_volume=1)
        )


class TestRankUniverse(unittest.TestCase):
    def _strong_uptrend(self):
        # Strong uptrend + volume surge on the last bar.
        close = list(np.linspace(10, 80, 250))
        volume = [100_000] * 249 + [500_000]
        return _history(close, volume)

    def _flat(self):
        close = [50.0] * 250
        return _history(close, [100_000] * 250)

    def _downtrend(self):
        close = list(np.linspace(80, 20, 250))
        return _history(close, [100_000] * 250)

    def test_empty_input_returns_empty_frame(self):
        from alphalens.lean_screener.lean_project.scorer import rank_universe

        df = rank_universe({}, _default_config())
        self.assertTrue(df.empty)
        self.assertIn("rank", df.columns)

    def test_uptrend_ranks_above_downtrend(self):
        from alphalens.lean_screener.lean_project.scorer import rank_universe

        histories = {
            "UP": self._strong_uptrend(),
            "DOWN": self._downtrend(),
            "FLAT": self._flat(),
        }
        ranked = rank_universe(histories, _default_config())

        self.assertEqual(ranked.iloc[0]["ticker"], "UP")
        # DOWN should be worst of the three.
        self.assertEqual(ranked.iloc[-1]["ticker"], "DOWN")

    def test_adds_monotonic_rank(self):
        from alphalens.lean_screener.lean_project.scorer import rank_universe

        histories = {
            "A": self._strong_uptrend(),
            "B": self._flat(),
            "C": self._downtrend(),
        }
        ranked = rank_universe(histories, _default_config())
        self.assertEqual(list(ranked["rank"]), [1, 2, 3])

    def test_guardrails_drop_names_before_scoring(self):
        from alphalens.lean_screener.lean_project.scorer import rank_universe

        cfg = _default_config(min_avg_dollar_volume=10_000_000_000)  # impossible
        histories = {
            "UP": self._strong_uptrend(),
            "DOWN": self._downtrend(),
        }
        self.assertTrue(rank_universe(histories, cfg).empty)

    def test_score_in_sane_range(self):
        from alphalens.lean_screener.lean_project.scorer import rank_universe

        histories = {
            "A": self._strong_uptrend(),
            "B": self._flat(),
            "C": self._downtrend(),
        }
        ranked = rank_universe(histories, _default_config())
        for s in ranked["score"]:
            self.assertGreaterEqual(s, 0.0)
            self.assertLessEqual(s, 1.0)


class TestComputeMetrics(unittest.TestCase):
    def test_returns_known_values_for_clean_input(self):
        from alphalens.lean_screener.lean_project.scorer import compute_metrics

        close = list(np.linspace(10, 100, 250))
        volume = [100_000] * 249 + [300_000]
        metrics = compute_metrics("TEST", _history(close, volume), _default_config())

        self.assertGreater(metrics.roc60, 0.0)   # uptrend
        self.assertGreater(metrics.trend_strength, 0.6)
        self.assertGreater(metrics.volume_surprise, 2.0)
        self.assertAlmostEqual(metrics.last_close, 100.0, places=3)


if __name__ == "__main__":
    unittest.main()
