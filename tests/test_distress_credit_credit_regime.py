"""Credit-regime overlay (HY OAS z-score → exposure mapping).

Layer 4 sibling to vol_target. Linear interpolation between exposure=1.0
at z<=-1 (full equity) and exposure=0.5 at z>=+1 (half cash). Strict-history
contract — z-score at asof t uses spread observations < t only.
"""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd


class CreditExposureFromZTests(unittest.TestCase):
    def test_exposure_at_z_minus_one_is_full(self):
        from alphalens.overlays.credit_regime import credit_exposure_from_z

        self.assertAlmostEqual(credit_exposure_from_z(-1.0), 1.0, places=6)

    def test_exposure_at_z_plus_one_is_half(self):
        from alphalens.overlays.credit_regime import credit_exposure_from_z

        self.assertAlmostEqual(credit_exposure_from_z(1.0), 0.5, places=6)

    def test_exposure_at_z_zero_is_three_quarters(self):
        """Linear midpoint: 0.75."""
        from alphalens.overlays.credit_regime import credit_exposure_from_z

        self.assertAlmostEqual(credit_exposure_from_z(0.0), 0.75, places=6)

    def test_exposure_clipped_below_minus_one(self):
        from alphalens.overlays.credit_regime import credit_exposure_from_z

        self.assertEqual(credit_exposure_from_z(-2.0), 1.0)
        self.assertEqual(credit_exposure_from_z(-100.0), 1.0)

    def test_exposure_clipped_above_plus_one(self):
        from alphalens.overlays.credit_regime import credit_exposure_from_z

        self.assertEqual(credit_exposure_from_z(2.0), 0.5)
        self.assertEqual(credit_exposure_from_z(100.0), 0.5)

    def test_exposure_linear_interp_at_minus_half(self):
        """z=-0.5 → 0.875 (linear between 1.0 at z=-1 and 0.75 at z=0)."""
        from alphalens.overlays.credit_regime import credit_exposure_from_z

        self.assertAlmostEqual(credit_exposure_from_z(-0.5), 0.875, places=6)

    def test_exposure_linear_interp_at_plus_half(self):
        """z=0.5 → 0.625 (linear between 0.75 at z=0 and 0.5 at z=+1)."""
        from alphalens.overlays.credit_regime import credit_exposure_from_z

        self.assertAlmostEqual(credit_exposure_from_z(0.5), 0.625, places=6)

    def test_exposure_none_returns_neutral_full(self):
        """Warmup / insufficient history: neutral 1.0."""
        from alphalens.overlays.credit_regime import credit_exposure_from_z

        self.assertEqual(credit_exposure_from_z(None), 1.0)


class CreditRegimeOverlayTests(unittest.TestCase):
    def _make_spread_series(self, n: int = 300, base: float = 4.0) -> pd.Series:
        """Synthetic HY OAS series: 300 BD, mean 4.0, std 0.5."""
        rng = np.random.default_rng(7)
        idx = pd.bdate_range(start="2020-01-01", periods=n)
        values = base + rng.normal(0.0, 0.5, size=n)
        return pd.Series(values, index=idx, name="spread")

    def test_overlay_strict_history_no_lookahead(self):
        """exposure(asof) must NOT see spread observations on or after asof."""
        from alphalens.overlays.credit_regime import CreditRegimeOverlay

        spreads = self._make_spread_series(n=300)
        # Fake a huge anomaly at t=last day; exposure at t=last must IGNORE it.
        anomaly_date = spreads.index[-1]
        spreads_with_anomaly = spreads.copy()
        spreads_with_anomaly.iloc[-1] = 100.0  # spike

        overlay = CreditRegimeOverlay(spread_series=spreads_with_anomaly, lookback=252)
        exposure_at_anomaly = overlay.exposure(anomaly_date)

        # Without anomaly:
        overlay_clean = CreditRegimeOverlay(spread_series=spreads, lookback=252)
        exposure_clean = overlay_clean.exposure(anomaly_date)

        # Both should be identical because anomaly is at t (excluded) and history < t is the same.
        self.assertAlmostEqual(exposure_at_anomaly, exposure_clean, places=6)

    def test_overlay_returns_neutral_when_lookback_insufficient(self):
        """Fewer than `lookback` past obs → neutral 1.0."""
        from alphalens.overlays.credit_regime import CreditRegimeOverlay

        spreads = self._make_spread_series(n=10)
        overlay = CreditRegimeOverlay(spread_series=spreads, lookback=252)
        # Day 5 has only 4 prior obs; far fewer than 252 → neutral.
        exposure = overlay.exposure(spreads.index[5])
        self.assertEqual(exposure, 1.0)

    def test_overlay_widening_regime_yields_min_exposure(self):
        """When the latest pre-asof spread is >>1 std above its trailing mean → min exposure."""
        from alphalens.overlays.credit_regime import CreditRegimeOverlay

        # 253 prior obs (BEFORE asof), then asof itself.
        # First 252 of prior obs: tight normal around 4 with std≈0.5.
        # Last pre-asof obs: spike to 8.0 (≈8 sigma above trailing mean).
        idx = pd.bdate_range(start="2020-01-01", periods=254)
        rng = np.random.default_rng(7)
        prior_252 = 4.0 + rng.normal(0.0, 0.5, size=252)
        spike = np.array([8.0])
        asof_val = np.array([99.0])  # ignored by strict-history rule
        spreads = pd.Series(np.concatenate([prior_252, spike, asof_val]), index=idx, name="spread")

        overlay = CreditRegimeOverlay(spread_series=spreads, lookback=252)
        exposure = overlay.exposure(idx[-1])
        # z >> +1 → clipped to min_exposure 0.5
        self.assertAlmostEqual(exposure, 0.5, places=6)

    def test_overlay_tightening_regime_yields_full_exposure(self):
        """When the latest pre-asof spread is <<-1 std below trailing mean → full exposure."""
        from alphalens.overlays.credit_regime import CreditRegimeOverlay

        idx = pd.bdate_range(start="2020-01-01", periods=254)
        rng = np.random.default_rng(7)
        prior_252 = 4.0 + rng.normal(0.0, 0.5, size=252)
        dip = np.array([1.0])  # ≈-6 sigma below mean of prior 252
        asof_val = np.array([99.0])  # ignored
        spreads = pd.Series(np.concatenate([prior_252, dip, asof_val]), index=idx, name="spread")

        overlay = CreditRegimeOverlay(spread_series=spreads, lookback=252)
        exposure = overlay.exposure(idx[-1])
        # z << -1 → clipped to full_exposure 1.0
        self.assertAlmostEqual(exposure, 1.0, places=6)


class HyOasZTests(unittest.TestCase):
    def test_hy_oas_z_uses_strict_history(self):
        """z(t) must use observations strictly before t."""
        from alphalens.data.macro.signals import hy_oas_z_from_series

        idx = pd.bdate_range(start="2020-01-01", periods=300)
        rng = np.random.default_rng(11)
        values = 4.0 + rng.normal(0.0, 0.5, size=300)
        # Inject anomaly at t=last; must NOT influence z(t=last)
        values[-1] = 100.0
        spreads = pd.Series(values, index=idx, name="hy_oas")

        z_with_anomaly = hy_oas_z_from_series(spreads, idx[-1], lookback=252)
        # Recompute without the anomaly — same z.
        spreads_clean = spreads.copy()
        spreads_clean.iloc[-1] = 4.0
        z_clean = hy_oas_z_from_series(spreads_clean, idx[-1], lookback=252)
        self.assertAlmostEqual(z_with_anomaly, z_clean, places=6)

    def test_hy_oas_z_returns_none_on_short_history(self):
        from alphalens.data.macro.signals import hy_oas_z_from_series

        idx = pd.bdate_range(start="2020-01-01", periods=50)
        spreads = pd.Series(np.full(50, 4.0), index=idx, name="hy_oas")
        z = hy_oas_z_from_series(spreads, idx[-1], lookback=252)
        self.assertIsNone(z)


if __name__ == "__main__":
    unittest.main()
