"""The split-adjustment gate: can a corporate action reach the #1227 estimand?

The gate exists because a pre-run note claimed ATR is built from raw unadjusted bars
while the outcome label uses split-adjusted ones. Measuring that claim needs three
discriminations, and each one has a way of quietly failing:

- "is this step a split?" answered from a vendor's split list inherits that vendor's
  completeness, so the module also answers it from the DATA, by asking whether a step
  appears in one price source and not the other;
- "does a split reach ATR?" answered from the final ATR number has almost no power,
  because Wilder's ATR is an EWM whose weight on a bar 190 sessions back is 7.5e-7. It
  has to be answered on the TRUE RANGE of the split day itself;
- "does the guard catch it?" is arithmetic over a band, and the band has a hole.

Every case below is hermetic: the vendor seams are injected.
"""

from __future__ import annotations

import datetime as dt
import unittest

import numpy as np
from scripts.ml import split_adjustment_gate as gate


class TestTheGuardBand(unittest.TestCase):
    """What ``selection_label._out_of_bounds`` can and cannot see."""

    def test_a_two_for_one_split_is_caught(self):
        self.assertTrue(gate.guard_sees(2.0))

    def test_a_ten_to_one_reverse_split_is_caught(self):
        self.assertTrue(gate.guard_sees(0.1))

    def test_a_three_for_two_split_is_invisible(self):
        # The hole. A 3-for-2 leaves a step of 0.667, inside (0.55, 1.8), so neither
        # the label status nor the beta-window drop ever fires.
        self.assertFalse(gate.guard_sees(1.5))

    def test_a_five_for_four_split_is_invisible(self):
        self.assertFalse(gate.guard_sees(1.25))

    def test_the_band_edges_are_reported_as_split_ratios(self):
        lo, hi = gate.blind_band()
        self.assertAlmostEqual(lo, 1 / 1.8, places=6)
        self.assertAlmostEqual(hi, 1 / 0.55, places=6)
        self.assertFalse(gate.guard_sees((lo + hi) / 2))

    def test_the_margin_on_a_plain_two_for_one_is_reported(self):
        # 0.5 against a 0.55 bound: a split day on which the stock also rises 10%
        # lands exactly on 0.55 and stops being visible. The number is small enough
        # that the gate states it rather than leaving a reader to divide.
        self.assertAlmostEqual(gate.two_for_one_headroom_pct(), 10.0, places=6)

    def test_a_nonsense_ratio_is_not_reported_as_caught(self):
        self.assertFalse(gate.guard_sees(0.0))
        self.assertFalse(gate.guard_sees(-2.0))


class TestTrueRangeIsWhatAtrEats(unittest.TestCase):
    """ATR consumes high, low and the previous close - not the close alone."""

    def test_a_quiet_day_has_a_small_true_range(self):
        tr = gate.true_range_pct(
            high=np.array([10.0, 10.2]), low=np.array([9.8, 10.0]), close=np.array([10.0, 10.1])
        )
        self.assertEqual(len(tr), 1)
        self.assertLess(tr[0], 5.0)

    def test_an_unadjusted_ten_to_one_reverse_split_explodes_the_true_range(self):
        # The positive control for the whole ATR arm. A 1-for-10 reverse multiplies the
        # price by ten overnight, and the resulting bar's true range is about 90% of its
        # OWN close - not 900%, because the close grew with it. The measured LCID bar
        # came in at 13.8%, so the control is the difference between a passing gate and
        # a meaningless one.
        tr = gate.true_range_pct(
            high=np.array([10.0, 101.0]), low=np.array([9.8, 99.0]), close=np.array([10.0, 100.0])
        )
        self.assertAlmostEqual(float(tr[0]), gate.expected_raw_true_range_pct(0.1), delta=2.0)
        self.assertGreater(tr[0], 80.0)

    def test_a_series_shorter_than_two_bars_has_no_true_range(self):
        tr = gate.true_range_pct(high=np.array([10.0]), low=np.array([9.8]), close=np.array([10.0]))
        self.assertEqual(len(tr), 0)

    def test_a_non_positive_close_does_not_become_an_infinite_range(self):
        tr = gate.true_range_pct(
            high=np.array([10.0, 1.0]), low=np.array([9.8, 0.5]), close=np.array([10.0, 0.0])
        )
        self.assertTrue(np.all(np.isfinite(tr)) or len(tr) == 0)


class TestTheExpectedRawTrueRange(unittest.TestCase):
    """How big a split-unadjusted bar reads, which is direction-dependent."""

    def test_a_forward_four_for_one_reads_about_three_hundred_percent(self):
        self.assertAlmostEqual(gate.expected_raw_true_range_pct(4.0), 300.0)

    def test_a_reverse_one_for_ten_reads_about_ninety_percent(self):
        # Not 900%. The denominator grows with the price, and an earlier draft of this
        # work quoted the forward figure for a reverse split, overstating it 10x.
        self.assertAlmostEqual(gate.expected_raw_true_range_pct(0.1), 90.0)

    def test_a_plain_two_for_one_reads_about_one_hundred_percent(self):
        self.assertAlmostEqual(gate.expected_raw_true_range_pct(2.0), 100.0)

    def test_every_case_dwarfs_an_ordinary_bar(self):
        # The measured medians on this panel run 2.6% to 6.2%, so the smallest
        # expectation here is still an order of magnitude clear of a normal day.
        smallest = min(gate.expected_raw_true_range_pct(r) for r in (0.1, 0.25, 0.374, 2.0, 4.0))
        self.assertGreater(smallest, 60.0)

    def test_a_non_positive_ratio_is_refused(self):
        with self.assertRaises(ValueError):
            gate.expected_raw_true_range_pct(0.0)


class TestTheEwmWeightArgument(unittest.TestCase):
    """Why the final ATR number cannot adjudicate a split far back in the window."""

    def test_a_spike_one_session_back_still_carries_real_weight(self):
        self.assertGreater(gate.wilder_weight(1), 0.06)

    def test_a_spike_one_hundred_and_ninety_sessions_back_is_gone(self):
        # This is the measured case: LCID split 2025-09-02, episode 2026-06-01.
        self.assertLess(gate.wilder_weight(190), 1e-5)

    def test_the_weight_falls_monotonically(self):
        weights = [gate.wilder_weight(k) for k in range(1, 40)]
        self.assertEqual(weights, sorted(weights, reverse=True))

    def test_a_negative_lag_is_refused(self):
        with self.assertRaises(ValueError):
            gate.wilder_weight(-1)


class TestCrossSourceClassification(unittest.TestCase):
    """A step present in one source and absent from the other is an adjustment artefact."""

    def test_a_step_in_both_sources_is_a_market_move(self):
        self.assertEqual(gate.classify_step(store_step=0.5326, reference_step=0.5326), gate.MARKET)

    def test_a_step_only_the_patchwork_store_has_is_an_artefact(self):
        # The measured MQ case: Polygon 3.8852, yfinance 0.9713 on the same two dates.
        self.assertEqual(
            gate.classify_step(store_step=3.8852, reference_step=0.9713), gate.ARTEFACT
        )

    def test_a_missing_reference_is_not_silently_called_a_market_move(self):
        # Returning MARKET here would turn "could not check" into "checked and clean",
        # which is the failure this whole module is written against.
        self.assertEqual(gate.classify_step(store_step=0.5, reference_step=None), gate.UNKNOWN)
        self.assertEqual(
            gate.classify_step(store_step=0.5, reference_step=float("nan")), gate.UNKNOWN
        )

    def test_small_disagreement_between_sources_is_still_a_market_move(self):
        self.assertEqual(gate.classify_step(store_step=1.2355, reference_step=1.2360), gate.MARKET)


class TestSmallSplitTemplates(unittest.TestCase):
    def test_every_template_sits_inside_the_blind_band(self):
        # A template the guard already catches would be dead weight in the scan.
        for name, step in gate.SMALL_SPLIT_TEMPLATES.items():
            with self.subTest(template=name):
                self.assertFalse(gate.guard_sees(1.0 / step), f"{name} is already caught")

    def test_a_step_matching_a_template_is_flagged(self):
        self.assertEqual(gate.matching_template(0.667), "3-for-2")

    def test_a_step_far_from_every_template_is_not_flagged(self):
        self.assertIsNone(gate.matching_template(0.95))

    def test_the_tolerance_is_wide_enough_to_be_a_screen_not_a_test(self):
        # Deliberately loose: it must not miss a split, so it will also catch ordinary
        # earnings moves. The measured run flagged 9 label-window steps and the
        # cross-source check showed all 9 were market moves. Recording the looseness
        # here stops a later reader from reading a hit as a finding.
        self.assertIsNotNone(gate.matching_template(0.79))
        self.assertIsNotNone(gate.matching_template(0.81))


class TestWindowMembership(unittest.TestCase):
    """The store's discontinuity can precede the vendor ex-date by a session."""

    def test_a_split_inside_the_window_is_found(self):
        self.assertTrue(
            gate.touches_window(
                dt.date(2026, 7, 10), dt.date(2026, 7, 1), dt.date(2026, 7, 29), tolerance_days=0
            )
        )

    def test_a_split_after_the_window_is_not_found_without_tolerance(self):
        self.assertFalse(
            gate.touches_window(
                dt.date(2026, 7, 1), dt.date(2026, 6, 1), dt.date(2026, 6, 29), tolerance_days=0
            )
        )

    def test_the_tolerance_recovers_the_measured_edge_case(self):
        # MQ: ex-date 2026-07-01, the store's step landed on 2026-06-30, and the burnt
        # window ended 2026-06-29. Matching on the ex-date alone hides that by one day.
        self.assertTrue(
            gate.touches_window(
                dt.date(2026, 7, 1), dt.date(2026, 6, 1), dt.date(2026, 6, 29), tolerance_days=3
            )
        )

    def test_a_negative_tolerance_is_refused(self):
        with self.assertRaises(ValueError):
            gate.touches_window(
                dt.date(2026, 7, 1), dt.date(2026, 6, 1), dt.date(2026, 6, 29), tolerance_days=-1
            )


class TestTheVerdict(unittest.TestCase):
    def test_a_clean_census_passes(self):
        verdict = gate.verdict(label_window_artefacts=0, unknown_steps=0)
        self.assertTrue(verdict.passed)

    def test_one_artefact_in_a_label_window_fails(self):
        verdict = gate.verdict(label_window_artefacts=1, unknown_steps=0)
        self.assertFalse(verdict.passed)
        self.assertIn("artefact", verdict.reason)

    def test_a_step_that_could_not_be_checked_also_fails(self):
        # "Could not check" must not pass. The gate is an integrity gate; an unchecked
        # step is exactly the state it exists to refuse.
        verdict = gate.verdict(label_window_artefacts=0, unknown_steps=2)
        self.assertFalse(verdict.passed)
        self.assertIn("could not be checked", verdict.reason)


if __name__ == "__main__":
    unittest.main()
