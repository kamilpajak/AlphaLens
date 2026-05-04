"""Tests for the drawdown-control overlay (Layer 4 risk-overlay).

Sister to ``test_risk_overlay`` (vol-target). Where vol-target may lever up
to ``max_leverage``, drawdown-control is hard-capped at 1.0 — no leverage,
ever. Tests guard:
  - causality contract (scale[t] uses returns[<t] only)
  - hard cap [0.0, 1.0] under all inputs
  - step-function thresholds
  - hysteresis on recovery (no whipsaw at band boundary)
  - identity behavior on never-in-drawdown inputs
"""

from __future__ import annotations

import unittest

import pandas as pd

from alphalens.overlays import (
    DrawdownControlConfig,
    DrawdownControlOverlay,
    apply_drawdown_control,
)


def _returns(values: list[float], start: str = "2020-01-06") -> pd.Series:
    idx = pd.date_range(start=start, periods=len(values), freq="W-MON")
    return pd.Series(values, index=idx, name="portfolio")


class DrawdownControlConfigTests(unittest.TestCase):
    def test_rejects_invalid_thresholds(self):
        with self.assertRaises(ValueError):
            DrawdownControlConfig(light_dd=0.10, heavy_dd=0.05)
        with self.assertRaises(ValueError):
            DrawdownControlConfig(light_dd=-0.01, heavy_dd=0.10)
        with self.assertRaises(ValueError):
            DrawdownControlConfig(half_weight=1.5)
        with self.assertRaises(ValueError):
            DrawdownControlConfig(recovery_band_pct=-0.01)
        with self.assertRaises(ValueError):
            DrawdownControlConfig(peak_lookback=-1)

    def test_default_config_passes(self):
        cfg = DrawdownControlConfig()
        self.assertEqual(cfg.light_dd, 0.05)
        self.assertEqual(cfg.heavy_dd, 0.10)
        self.assertEqual(cfg.half_weight, 0.5)
        self.assertEqual(cfg.peak_lookback, 0)


class ScaleSeriesContractTests(unittest.TestCase):
    def test_empty_returns_empty(self):
        overlay = DrawdownControlOverlay()
        scaled = overlay.scale_series(pd.Series([], dtype=float))
        self.assertTrue(scaled.empty)
        self.assertEqual(scaled.dtype, float)

    def test_first_period_always_one(self):
        """Causality: scale[0] uses no returns, must be 1.0."""
        overlay = DrawdownControlOverlay()
        rets = _returns([-0.20, 0.0, 0.0])  # immediate -20% would trigger heavy_dd
        scaled = overlay.scale_series(rets)
        self.assertEqual(scaled.iloc[0], 1.0)

    def test_scale_uses_history_only(self):
        """scale[t] is determined by returns[<t]. Verify by changing returns[t]
        and checking scale[t] is unchanged."""
        overlay = DrawdownControlOverlay()

        rets1 = _returns([0.01, 0.01, -0.20, 0.01, 0.01])
        rets2 = _returns([0.01, 0.01, -0.20, -0.99, 0.01])  # change [3]
        scales1 = overlay.scale_series(rets1)
        scales2 = overlay.scale_series(rets2)
        # scale[0..3] depends only on returns[0..2], identical for both.
        pd.testing.assert_series_equal(scales1.iloc[:4], scales2.iloc[:4], check_names=False)


class ScaleBoundsTests(unittest.TestCase):
    def test_weight_never_above_one(self):
        """No leverage permitted — cap at 1.0 under any input."""
        overlay = DrawdownControlOverlay()
        rets = _returns([0.20, 0.20, 0.20, 0.20, 0.20])  # huge gains, tiny vol
        scaled = overlay.scale_series(rets)
        self.assertTrue((scaled <= 1.0 + 1e-12).all())

    def test_weight_never_below_zero(self):
        overlay = DrawdownControlOverlay()
        rets = _returns([-0.30, -0.30, -0.30, -0.30, -0.30])  # catastrophic
        scaled = overlay.scale_series(rets)
        self.assertTrue((scaled >= 0.0 - 1e-12).all())

    def test_weight_in_unit_interval_under_random_input(self):
        import numpy as np

        rng = np.random.default_rng(42)
        rets = pd.Series(
            rng.normal(loc=0.0, scale=0.05, size=200),
            index=pd.date_range("2020-01-06", periods=200, freq="W-MON"),
            name="portfolio",
        )
        overlay = DrawdownControlOverlay()
        scaled = overlay.scale_series(rets)
        self.assertTrue((scaled >= 0.0).all())
        self.assertTrue((scaled <= 1.0).all())


class StepFunctionThresholdsTests(unittest.TestCase):
    def test_no_drawdown_yields_full_exposure(self):
        """Always-positive returns → no drawdown → weight=1.0 throughout."""
        overlay = DrawdownControlOverlay()
        rets = _returns([0.01] * 10)
        scaled = overlay.scale_series(rets)
        self.assertTrue((scaled == 1.0).all())

    def test_heavy_drawdown_triggers_zero(self):
        """A single -15% return puts the curve below heavy_dd=10% → next scale=0."""
        overlay = DrawdownControlOverlay(
            DrawdownControlConfig(light_dd=0.05, heavy_dd=0.10, recovery_band_pct=0.02)
        )
        rets = _returns([0.0, -0.15, 0.0, 0.0])
        scaled = overlay.scale_series(rets)
        # scale[0] = 1.0 (warmup)
        # scale[1] uses returns[<1]=[0.0] → no drawdown → 1.0
        # scale[2] uses returns[<2]=[0.0, -0.15] → equity 0.85, peak 1.0, dd=15% > heavy_dd → 0.0
        self.assertEqual(scaled.iloc[0], 1.0)
        self.assertEqual(scaled.iloc[1], 1.0)
        self.assertEqual(scaled.iloc[2], 0.0)

    def test_mid_band_triggers_half_weight(self):
        """A -7% return puts the curve in (light_dd, heavy_dd) → next scale=0.5."""
        overlay = DrawdownControlOverlay(
            DrawdownControlConfig(
                light_dd=0.05, heavy_dd=0.10, half_weight=0.5, recovery_band_pct=0.02
            )
        )
        rets = _returns([0.0, -0.07, 0.0, 0.0])
        scaled = overlay.scale_series(rets)
        # scale[2] uses returns[<2]=[0.0, -0.07] → dd=7% in mid band → 0.5
        self.assertEqual(scaled.iloc[2], 0.5)


class HysteresisTests(unittest.TestCase):
    def test_recovery_to_within_band_re_arms_full_exposure(self):
        """Once equity recovers to within recovery_band of peak, scale goes back to 1.0."""
        overlay = DrawdownControlOverlay(
            DrawdownControlConfig(light_dd=0.05, heavy_dd=0.10, recovery_band_pct=0.01)
        )
        # Peak at t=0 (equity=1.0). -7% drawdown then near-full recovery.
        rets = _returns([0.0, -0.07, 0.075, 0.0, 0.0])
        scaled = overlay.scale_series(rets)
        # scale[2] from returns[<2]=[0.0, -0.07] → dd=7% mid → 0.5
        # scale[3] from returns[<3]=[0.0, -0.07, 0.075] → equity ≈ 1.0×0.93×1.075 = 0.9998
        #   peak still 1.0, dd ≈ 0.02% < light_dd AND within recovery band → 1.0
        self.assertEqual(scaled.iloc[2], 0.5)
        self.assertEqual(scaled.iloc[3], 1.0)

    def test_no_premature_re_arm_at_band_boundary(self):
        """Equity bouncing back to just above light_dd but still below recovery
        threshold should NOT re-arm to 1.0 — must stay at half until recovery."""
        overlay = DrawdownControlOverlay(
            DrawdownControlConfig(light_dd=0.05, heavy_dd=0.10, recovery_band_pct=0.02)
        )
        # -7% then small bounce to -4% (still 4% below peak — not within 2% recovery).
        rets = _returns([0.0, -0.07, 0.032, 0.0])
        scaled = overlay.scale_series(rets)
        # equity[2] = 0.93 * 1.032 ≈ 0.9598; dd ≈ 4.0%
        # 4.0% < light_dd=5% (out of mid-band on absolute drawdown)
        # BUT recovery threshold is 2% from peak → we're at 4% below peak → NOT recovered.
        # Hysteresis says: stay at half_weight.
        self.assertEqual(scaled.iloc[2], 0.5)
        # NOTE: scale[3] uses returns[<3] including the +0.032 bounce. Equity ~0.9598.
        # Drawdown ~4% — still below light_dd absolutely, still outside recovery band.
        # Hysteresis keeps weight at half.
        self.assertEqual(scaled.iloc[3], 0.5)


class ApplyOverlayTests(unittest.TestCase):
    def test_apply_multiplies_returns_by_scales(self):
        overlay = DrawdownControlOverlay()
        rets = _returns([0.01, -0.07, 0.005, 0.005])
        scales = overlay.scale_series(rets)
        applied = apply_drawdown_control(rets, overlay)
        pd.testing.assert_series_equal(applied, rets * scales, check_names=False)

    def test_apply_preserves_index_and_dtype(self):
        overlay = DrawdownControlOverlay()
        rets = _returns([0.01, -0.07, 0.005, 0.005])
        applied = apply_drawdown_control(rets, overlay)
        self.assertEqual(list(applied.index), list(rets.index))
        self.assertEqual(applied.dtype, float)

    def test_apply_empty_passthrough(self):
        overlay = DrawdownControlOverlay()
        empty = pd.Series([], dtype=float)
        applied = apply_drawdown_control(empty, overlay)
        self.assertTrue(applied.empty)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
