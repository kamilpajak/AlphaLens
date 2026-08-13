"""Hermetic tests for the pure trailing-entry geometry helper (PR-T2b).

``compute_trailing_order_geometry`` is the entry-side sibling of the exit
``clamp_reanchor_target`` — a pure leaf that turns (touch reference bid, running
trough, d_bps) into the native trailing-limit order parameters the executor
POSTs: the initial trigger ``order_price``, the absolute ``trailing_distance``,
the ratchet ``trailing_step`` (memo G10 coarse step), and the G1 ceiling
``ceiling_price`` = ``trough*(1+d)*(1+eps)``. NO tick alignment here (the broker
adapter tick-aligns at placement, probe fact 3); NO I/O.
"""

from __future__ import annotations

import math
import unittest

from alphalens_pipeline.brokers.automanager.entry_trail_geometry import (
    CEILING_EPS_FRAC,
    TRAILING_STEP_FRACTION,
    compute_trailing_order_geometry,
)


class TestTrailingOrderGeometry(unittest.TestCase):
    def test_basic_geometry_at_touch(self) -> None:
        # At touch reference == trough (the touch bid is the running low). d=50bps.
        geo = compute_trailing_order_geometry(reference=10.0, trough=10.0, d_bps=50)
        assert geo is not None
        d_frac = 0.005
        self.assertAlmostEqual(geo.trailing_distance, 10.0 * d_frac)  # 0.05
        self.assertAlmostEqual(geo.order_price, 10.0 * (1.0 + d_frac))  # 10.05
        self.assertAlmostEqual(geo.trailing_step, 10.0 * d_frac * TRAILING_STEP_FRACTION)
        self.assertAlmostEqual(geo.ceiling_price, 10.0 * (1.0 + d_frac) * (1.0 + CEILING_EPS_FRAC))

    def test_ceiling_is_always_at_or_above_the_trigger(self) -> None:
        # G1: the ceiling caps the fill AT or ABOVE the initial trigger, so the
        # broker's BUY directional clamp (ceiling >= trigger) never rejects a
        # legitimately-armed trail.
        for ref, trough, d_bps in ((10.0, 10.0, 50), (9.95, 9.90, 100), (250.0, 249.5, 150)):
            geo = compute_trailing_order_geometry(reference=ref, trough=trough, d_bps=d_bps)
            assert geo is not None
            self.assertGreaterEqual(geo.ceiling_price, geo.order_price)

    def test_trigger_sits_one_distance_above_the_reference(self) -> None:
        # The probe requested trigger = bid + distance (RIVN 16.03 + 0.05; MARA
        # 9.66 + 0.05). Mirror that: order_price = reference + trailing_distance.
        geo = compute_trailing_order_geometry(reference=9.66, trough=9.66, d_bps=50)
        assert geo is not None
        self.assertAlmostEqual(geo.order_price, 9.66 + geo.trailing_distance)

    def test_degenerate_inputs_return_none(self) -> None:
        for ref, trough, d_bps in (
            (0.0, 10.0, 50),
            (-1.0, 10.0, 50),
            (float("nan"), 10.0, 50),
            (float("inf"), 10.0, 50),
            (10.0, 0.0, 50),
            (10.0, -1.0, 50),
            (10.0, float("nan"), 50),
            (10.0, 10.0, 0),
            (10.0, 10.0, -50),
        ):
            with self.subTest(ref=ref, trough=trough, d_bps=d_bps):
                self.assertIsNone(
                    compute_trailing_order_geometry(reference=ref, trough=trough, d_bps=d_bps)
                )

    def test_all_outputs_finite_and_positive(self) -> None:
        geo = compute_trailing_order_geometry(reference=42.37, trough=41.80, d_bps=100)
        assert geo is not None
        for value in (
            geo.order_price,
            geo.trailing_distance,
            geo.trailing_step,
            geo.ceiling_price,
        ):
            self.assertTrue(math.isfinite(value) and value > 0.0)


if __name__ == "__main__":
    unittest.main()
