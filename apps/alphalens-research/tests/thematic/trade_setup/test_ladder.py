import unittest

from alphalens_pipeline.thematic.trade_setup import ladder


class TestBuildEntryTiers(unittest.TestCase):
    def test_monotone_descending_and_below_close(self):
        close, atr, stop = 420.0, 12.0, 374.0
        cands = [(414.0, "shallow"), (407.0, "swing-low"), (389.0, "200-day MA")]
        tiers = ladder.build_entry_tiers(close, atr, cands, stop)
        prices = [p for p, _ in tiers]
        self.assertEqual(prices, sorted(prices, reverse=True))
        self.assertTrue(all(p < close for p in prices))

    def test_drops_candidates_above_close(self):
        # A downtrend MA sitting ABOVE close must never become a tier
        # (the pathological max() inversion the geometry guard kills).
        close, atr, stop = 100.0, 5.0, 80.0
        cands = [(120.0, "200-day MA"), (95.0, "swing-low"), (88.0, "shallow")]
        tiers = ladder.build_entry_tiers(close, atr, cands, stop)
        self.assertTrue(all(p < close for p, _ in tiers))
        self.assertNotIn(120.0, [p for p, _ in tiers])

    def test_drops_tier_too_close_to_stop(self):
        # entry-stop must be >= 0.5*ATR = 2.5; 81 is only 1 above stop 80 -> dropped.
        close, atr, stop = 100.0, 5.0, 80.0
        cands = [(95.0, "a"), (81.0, "too-close"), (90.0, "b")]
        tiers = ladder.build_entry_tiers(close, atr, cands, stop)
        self.assertNotIn(81.0, [p for p, _ in tiers])
        self.assertTrue(all((p - stop) >= 0.5 * atr for p, _ in tiers))

    def test_a_stop_far_below_every_candidate_removes_nothing(self):
        # The builder picks its tiers against -inf and only then derives the
        # stop from them. That is safe because the stop sits >= 1.0*ATR
        # under every candidate, where the stop-distance filter never fires.
        close, atr = 100.0, 5.0
        cands = [(97.5, "shallow"), (93.0, "swing-low"), (90.0, "volatility"), (61.0, "old low")]
        far_stop = min(p for p, _ in cands) - 1.0 * atr
        self.assertEqual(
            ladder.build_entry_tiers(close, atr, cands, far_stop),
            ladder.build_entry_tiers(close, atr, cands, float("-inf")),
        )

    def test_raising_the_stop_removes_only_a_suffix_of_tiers(self):
        # 195 is skipped behind 200 (spacing, then max_tiers). When a higher
        # stop removes 200, 195 is even closer to that stop, so it can never
        # come in and pull the deepest tier DOWN.
        close, atr = 320.0, 60.0
        cands = [(280.0, "a"), (240.0, "b"), (200.0, "c"), (195.0, "d")]
        previous = ladder.build_entry_tiers(close, atr, cands, float("-inf"))
        for stop in (135.8, 164.5, 175.2):
            tiers = ladder.build_entry_tiers(close, atr, cands, stop)
            self.assertEqual(tiers, previous[: len(tiers)])
            previous = tiers
        self.assertEqual([p for p, _ in previous], [280.0, 240.0])

    def test_enforces_min_spacing(self):
        # 95 and 94.5 are 0.5 apart < 0.5*ATR(=2.5): only the first survives.
        close, atr, stop = 100.0, 5.0, 80.0
        cands = [(95.0, "a"), (94.5, "b"), (90.0, "c")]
        tiers = ladder.build_entry_tiers(close, atr, cands, stop)
        prices = [p for p, _ in tiers]
        self.assertIn(95.0, prices)
        self.assertNotIn(94.5, prices)

    def test_caps_at_max_tiers(self):
        close, atr, stop = 100.0, 5.0, 60.0
        cands = [(98.0, "a"), (90.0, "b"), (82.0, "c"), (74.0, "d")]
        tiers = ladder.build_entry_tiers(close, atr, cands, stop, max_tiers=3)
        self.assertEqual(len(tiers), 3)


class TestBuildTpTranches(unittest.TestCase):
    def test_uses_overhead_resistance_when_present(self):
        close, atr, blended, stop = 420.0, 12.0, 398.0, 374.0
        tp = ladder.build_tp_tranches(close, atr, [440.0, 456.0, 470.0], blended, stop)
        targets = [t for t, _, _ in tp]
        self.assertEqual(targets, sorted(targets))
        self.assertTrue(all(t > close for t in targets))
        self.assertTrue(all(tag == "overhead resistance" for _, _, tag in tp))

    def test_falls_back_to_r_multiples_when_no_resistance(self):
        close, atr, blended, stop = 420.0, 12.0, 398.0, 374.0
        tp = ladder.build_tp_tranches(close, atr, [], blended, stop)
        self.assertEqual(len(tp), 3)
        self.assertTrue(all("R" in tag for _, _, tag in tp))
        # R-multiples 2/3/4 of risk (24) above blended.
        self.assertAlmostEqual(tp[0][0], 398.0 + 2 * 24.0, places=2)

    def test_returns_empty_when_risk_nonpositive(self):
        self.assertEqual(ladder.build_tp_tranches(100.0, 5.0, [110.0], 90.0, 95.0), [])


if __name__ == "__main__":
    unittest.main()
