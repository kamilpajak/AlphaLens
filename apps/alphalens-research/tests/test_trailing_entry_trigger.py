"""The trailing-entry trigger, in both forms the study needs to compare.

The point of the module under test is that the two forms differ in ONE line, so
a measured difference between them is attributable to the arithmetic and to
nothing else. These tests pin that, and they pin the additive form against the
LIVE geometry function that builds the order actually sent.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.brokers.automanager.entry_trail_geometry import (
    compute_trailing_order_geometry,
)
from alphalens_research.diagnostics.trailing_entry_trigger import (
    ADDITIVE,
    PROPORTIONAL,
    arming_reference,
    trail_distance,
    trail_trigger,
)

FAR_FUTURE = 10**13


def _bar(t: int, o: float, h: float, low: float) -> dict:
    return {"t": t, "o": o, "h": h, "l": low, "c": o}


class ArmingReferenceTest(unittest.TestCase):
    def test_a_touch_bar_that_opens_above_the_limit_references_the_limit(self) -> None:
        # The touch happens AT the limit, because the order rests there.
        self.assertEqual(arming_reference(_bar(0, 70.0, 70.0, 67.9), 68.0), 68.0)

    def test_a_touch_bar_that_opens_BELOW_the_limit_references_the_open(self) -> None:
        # The first print is already through the limit, so that is where the
        # touch is, and the distance is frozen from it.
        self.assertEqual(arming_reference(_bar(0, 67.5, 68.2, 67.0), 68.0), 67.5)


class TrailDistanceTest(unittest.TestCase):
    def test_the_distance_comes_from_the_live_geometry_function(self) -> None:
        # Not a reimplementation: the same number the adapter sends.
        for reference, d in ((68.0, 0.005), (10.08, 0.03), (41.37, 0.015)):
            live = compute_trailing_order_geometry(
                reference=reference, trough=reference, d_bps=round(d * 10_000)
            )
            assert live is not None
            self.assertAlmostEqual(trail_distance(reference, d), live.trailing_distance, places=12)

    def test_a_degenerate_reference_has_no_distance(self) -> None:
        self.assertIsNone(trail_distance(0.0, 0.005))
        self.assertIsNone(trail_distance(68.0, 0.0))


class TheTwoFormsTest(unittest.TestCase):
    """Where they agree, where they part, and by how much."""

    def test_they_agree_while_the_trough_has_not_fallen(self) -> None:
        # At the touch the trough IS the reference, so both price the same level.
        # They agree to float tolerance and NOT bit-for-bit: 68.0 + 0.34 is
        # exactly 68.34 while 68.0 * 1.005 is 68.33999999999999. Worth pinning,
        # because a paired comparison of the two forms will show a difference of
        # about 1e-14 on every untroughed touch, and that is noise, not signal.
        bars = [_bar(0, 68.0, 68.0, 68.0), _bar(1, 68.0, 68.5, 68.0)]
        add = trail_trigger(bars, 0, 0.005, FAR_FUTURE, limit=68.0, form=ADDITIVE)
        prop = trail_trigger(bars, 0, 0.005, FAR_FUTURE, limit=68.0, form=PROPORTIONAL)
        self.assertIsNotNone(add)
        self.assertIsNotNone(prop)
        assert add is not None and prop is not None
        self.assertEqual(add[0], prop[0])
        self.assertAlmostEqual(add[1], prop[1], places=10)
        self.assertNotEqual(add[1], prop[1], "the two forms are not bit-identical even here")

    def test_the_additive_distance_does_not_shrink_as_the_trough_falls(self) -> None:
        # This is the whole defect. Touch at 68.00, trough down to 66.00, d=50bps.
        # Proportional level 66.00*1.005 = 66.33; additive 66.00 + 0.34 = 66.34.
        bars = [
            _bar(0, 68.0, 68.0, 68.0),
            _bar(1, 67.0, 67.0, 66.0),
            _bar(2, 66.2, 66.335, 66.1),
        ]
        prop = trail_trigger(bars, 0, 0.005, FAR_FUTURE, limit=68.0, form=PROPORTIONAL)
        add = trail_trigger(bars, 0, 0.005, FAR_FUTURE, limit=68.0, form=ADDITIVE)
        # The proportional trail fires on bar 2; the additive one does not reach
        # its higher level, so it is still waiting.
        self.assertIsNotNone(prop)
        assert prop is not None
        self.assertEqual(prop[0], 2)
        self.assertAlmostEqual(prop[1], 66.33, places=4)
        self.assertIsNone(add)

    def test_a_gap_open_above_the_level_fills_at_the_open(self) -> None:
        bars = [_bar(0, 68.0, 68.0, 68.0), _bar(1, 67.0, 67.0, 66.0), _bar(2, 67.5, 67.6, 67.4)]
        got = trail_trigger(bars, 0, 0.005, FAR_FUTURE, limit=68.0, form=ADDITIVE)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got, (2, 67.5))

    def test_the_same_bar_bounce_is_never_triggered(self) -> None:
        # Deliberate, and a stated divergence from the daily-bar replay: the
        # intra-bar order of a minute bar is unknown, so the touch bar's own
        # high cannot fire the trail.
        bars = [_bar(0, 68.0, 69.0, 68.0)]
        self.assertIsNone(trail_trigger(bars, 0, 0.005, FAR_FUTURE, limit=68.0, form=ADDITIVE))

    def test_a_bar_at_or_past_the_deadline_ends_the_watch(self) -> None:
        bars = [_bar(0, 68.0, 68.0, 68.0), _bar(5000, 68.4, 68.5, 68.3)]
        self.assertIsNone(trail_trigger(bars, 0, 0.005, 5000, limit=68.0, form=ADDITIVE))

    def test_a_rung_the_live_geometry_refuses_arms_nothing(self) -> None:
        # The live function returns None on a non-positive reference, and a trail
        # that cannot be placed never fires. The proportional control has no such
        # gate, which is one more reason it is a control and not a policy.
        bars = [_bar(0, 0.0, 0.0, 0.0), _bar(1, 1.0, 2.0, 0.5)]
        self.assertIsNone(trail_trigger(bars, 0, 0.005, FAR_FUTURE, limit=0.0, form=ADDITIVE))

    def test_an_unknown_form_is_refused_rather_than_guessed(self) -> None:
        bars = [_bar(0, 68.0, 68.0, 68.0), _bar(1, 68.0, 68.5, 68.0)]
        with self.assertRaises(ValueError):
            trail_trigger(bars, 0, 0.005, FAR_FUTURE, limit=68.0, form="fractional")


if __name__ == "__main__":
    unittest.main()
