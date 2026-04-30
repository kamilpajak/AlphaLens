import unittest

from alphalens.macro.scorer import MacroRegime


class TestOverlayAllocator(unittest.TestCase):
    CORE = {"SPY": 0.60, "QQQ": 0.30, "IWM": 0.10}

    def _make(self, core=None, max_tilt: float = 0.10):
        from alphalens.archive.rotation.allocator import OverlayAllocator

        return OverlayAllocator(core_weights=core or self.CORE, max_tilt=max_tilt)

    def test_zero_tilts_returns_core_weights(self):
        alloc = self._make()
        regime = MacroRegime(flags={}, tilt_sum={})

        weights = alloc.apply(regime)

        self.assertAlmostEqual(weights["SPY"], 0.60, places=9)
        self.assertAlmostEqual(weights["QQQ"], 0.30, places=9)
        self.assertAlmostEqual(weights["IWM"], 0.10, places=9)
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=9)

    def test_single_tilt_applied_and_still_sums_to_one(self):
        alloc = self._make()
        regime = MacroRegime(flags={}, tilt_sum={"QQQ": 0.10, "SPY": -0.10})

        weights = alloc.apply(regime)

        self.assertAlmostEqual(weights["SPY"], 0.50, places=9)
        self.assertAlmostEqual(weights["QQQ"], 0.40, places=9)
        self.assertAlmostEqual(weights["IWM"], 0.10, places=9)
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=9)

    def test_clamps_tilt_to_max_tilt(self):
        alloc = self._make(max_tilt=0.10)
        # rule wants +0.20 QQQ / -0.20 SPY; should clamp to ±0.10 each
        regime = MacroRegime(flags={}, tilt_sum={"QQQ": 0.20, "SPY": -0.20})

        weights = alloc.apply(regime)

        self.assertAlmostEqual(weights["QQQ"], 0.40, places=9)
        self.assertAlmostEqual(weights["SPY"], 0.50, places=9)
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=9)

    def test_clamps_negative_tilt_symmetrically(self):
        alloc = self._make(max_tilt=0.05)
        regime = MacroRegime(flags={}, tilt_sum={"QQQ": -0.30})

        weights = alloc.apply(regime)

        self.assertAlmostEqual(weights["QQQ"], 0.25, places=9)  # 0.30 - 0.05

    def test_rebalances_when_tilts_dont_net_zero(self):
        """If rules net to non-zero sum, residual is spread to keep total = 1."""
        alloc = self._make()
        regime = MacroRegime(flags={}, tilt_sum={"QQQ": 0.05})  # SPY/IWM untouched

        weights = alloc.apply(regime)

        self.assertAlmostEqual(sum(weights.values()), 1.0, places=9)
        self.assertAlmostEqual(weights["QQQ"], 0.35, places=9)
        # residual -0.05 spread pro-rata across SPY + IWM (60:10 ratio)
        self.assertAlmostEqual(weights["SPY"], 0.60 - 0.05 * (0.60 / 0.70), places=9)
        self.assertAlmostEqual(weights["IWM"], 0.10 - 0.05 * (0.10 / 0.70), places=9)

    def test_refuses_tilt_causing_negative_weight(self):
        """Sanity check: clamp + residual spread must never push a weight below zero."""
        from alphalens.archive.rotation.allocator import AllocationError

        alloc = self._make(max_tilt=0.25)
        # SPY would go to 0.60 - 0.25 - residual = very negative after residual spread
        regime = MacroRegime(flags={}, tilt_sum={"QQQ": 0.20, "IWM": 0.20, "SPY": -0.20})
        # Here all three tilts clamp to ±0.20 and net to +0.20 (needs residual
        # deducted from SPY/IWM). SPY already at -0.20 tilt → 0.60-0.20=0.40 base,
        # residual distribution might push further. We just assert sum=1 no negatives.
        weights = alloc.apply(regime)
        for t, w in weights.items():
            self.assertGreaterEqual(w, 0.0, f"{t} weight went negative: {w}")

        # Hard failure: tilt that cannot be satisfied even with clamp → AllocationError
        bad_core = {"SPY": 0.05, "QQQ": 0.90, "IWM": 0.05}
        alloc_bad = self._make(core=bad_core, max_tilt=0.10)
        regime_bad = MacroRegime(flags={}, tilt_sum={"SPY": -0.10})
        with self.assertRaises(AllocationError):
            alloc_bad.apply(regime_bad)

    def test_deterministic_for_same_regime(self):
        alloc = self._make()
        regime = MacroRegime(flags={"x": True}, tilt_sum={"QQQ": 0.05, "SPY": -0.05})

        self.assertEqual(alloc.apply(regime), alloc.apply(regime))


if __name__ == "__main__":
    unittest.main()
