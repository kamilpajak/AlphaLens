import unittest

import numpy as np
import pandas as pd


class TestClassifyRegime(unittest.TestCase):
    def test_bull_trend(self):
        from alphalens.attribution.regime import classify_regime

        # 70 days of steady +0.3%/day → trailing 60d return ~20% → bull
        closes = pd.Series(100 * np.exp(np.cumsum(np.full(70, 0.003))))
        closes.index = pd.date_range("2024-01-01", periods=70)
        labels = classify_regime(closes, lookback=60)
        self.assertEqual(labels.iloc[-1], "bull")

    def test_bear_trend(self):
        from alphalens.attribution.regime import classify_regime

        closes = pd.Series(100 * np.exp(np.cumsum(np.full(70, -0.003))))
        closes.index = pd.date_range("2024-01-01", periods=70)
        labels = classify_regime(closes, lookback=60)
        self.assertEqual(labels.iloc[-1], "bear")

    def test_flat_trend(self):
        from alphalens.attribution.regime import classify_regime

        closes = pd.Series([100.0] * 70)  # perfectly flat
        closes.index = pd.date_range("2024-01-01", periods=70)
        labels = classify_regime(closes, lookback=60)
        self.assertEqual(labels.iloc[-1], "flat")

    def test_lookback_too_small_raises(self):
        from alphalens.attribution.regime import classify_regime

        with self.assertRaises(ValueError):
            classify_regime(pd.Series([100.0, 101.0]), lookback=1)

    def test_custom_thresholds(self):
        from alphalens.attribution.regime import classify_regime

        # +3% over 60d — normally "flat" with default 5% threshold;
        # lower threshold to 2% → should become "bull".
        closes = pd.Series(100 * np.exp(np.cumsum(np.full(70, 0.0005))))
        closes.index = pd.date_range("2024-01-01", periods=70)
        labels = classify_regime(closes, lookback=60, bull_threshold=0.02)
        self.assertEqual(labels.iloc[-1], "bull")


class TestRegimeBreakdown(unittest.TestCase):
    def _synthetic(self, regimes: list[str]):
        idx = pd.date_range("2024-01-01", periods=len(regimes))
        # Returns: positive in bull, negative in bear, small in flat.
        mapping = {"bull": 0.01, "bear": -0.005, "flat": 0.0005}
        port = pd.Series([mapping[r] for r in regimes], index=idx)
        ic = pd.Series([0.03 if r == "bull" else -0.01 for r in regimes], index=idx)
        median = pd.Series(
            [mapping[r] * 0.5 for r in regimes],
            index=idx,  # portfolio beats median
        )
        labels = pd.Series(regimes, index=idx)
        return port, ic, median, labels

    def test_each_regime_has_its_own_sharpe(self):
        from alphalens.attribution.regime import regime_breakdown

        regimes = ["bull"] * 100 + ["bear"] * 100 + ["flat"] * 100
        port, ic, median, labels = self._synthetic(regimes)
        result = regime_breakdown(port, ic, median, labels)

        self.assertIn("bull", result)
        self.assertIn("bear", result)
        self.assertIn("flat", result)
        # Bull days are all +1% constant — Sharpe should be 0 (zero variance).
        # Build more realistic distribution.

    def test_realistic_distributions(self):
        from alphalens.attribution.regime import regime_breakdown

        rng = np.random.default_rng(0)
        n = 100
        regimes = ["bull"] * n + ["bear"] * n + ["flat"] * n
        idx = pd.date_range("2024-01-01", periods=3 * n)
        port = pd.Series(
            np.concatenate(
                [
                    rng.normal(0.005, 0.01, n),  # bull
                    rng.normal(-0.002, 0.015, n),  # bear
                    rng.normal(0.0002, 0.008, n),  # flat
                ]
            ),
            index=idx,
        )
        ic = pd.Series(
            np.concatenate(
                [
                    rng.normal(0.05, 0.1, n),
                    rng.normal(-0.02, 0.15, n),
                    rng.normal(0.0, 0.1, n),
                ]
            ),
            index=idx,
        )
        median = pd.Series(rng.normal(0.0, 0.005, 3 * n), index=idx)
        labels = pd.Series(regimes, index=idx)

        result = regime_breakdown(port, ic, median, labels)
        self.assertGreater(result["bull"].sharpe, result["bear"].sharpe)
        self.assertEqual(result["bull"].days, n)

    def test_missing_regime_omitted(self):
        from alphalens.attribution.regime import regime_breakdown

        regimes = ["bull"] * 10  # no bear / flat days
        port, ic, median, labels = self._synthetic(regimes)
        result = regime_breakdown(port, ic, median, labels)

        self.assertIn("bull", result)
        self.assertNotIn("bear", result)
        self.assertNotIn("flat", result)


if __name__ == "__main__":
    unittest.main()
