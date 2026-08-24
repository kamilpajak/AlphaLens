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

import itertools
import math
import unittest

from alphalens_pipeline.brokers.automanager.entry_trail_geometry import (
    CEILING_EPS_FRAC,
    TRAILING_STEP_FRACTION,
    arms_inside_exit_region,
    compute_trailing_order_geometry,
    entry_fill_estimate,
)

from tests.incident_1112_fixture import (
    SMG_ACTUAL_FILL,
    SMG_ATR,
    SMG_D_BPS,
    SMG_GEOMETRY_TP,
    SMG_TIERS,
    SMG_TOUCH_BID,
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


_K_ATR = 1.5  # atr_bracket_1p5 pinned take-profit multiple


def _alloc_weighted_blend(subset: tuple[tuple[float, float], ...]) -> float:
    wsum = sum(w for _p, w in subset)
    return sum(p * w for p, w in subset) / wsum


class TestEntryFillEstimate(unittest.TestCase):
    """Issue #1112 step 1: the validity test must use a REALISTIC fill estimate,
    never the nominal tier limit. The estimate is the broker-enforced StopLimit
    ceiling on the armed native trail — the hard upper bound on any fill of that
    order."""

    def test_estimate_bounds_the_measured_smg_overshoot(self) -> None:
        # LIVE 2026-08-24: the trail armed off touch bid 59.77 filled at 59.9261,
        # ABOVE its own tier limit 59.786017. A nominal-limit check would have
        # compared 59.786017 against the target and seen no problem in the tier's
        # own terms; the estimate has to sit above the price that actually filled.
        estimate = entry_fill_estimate(
            reference=SMG_TOUCH_BID, trough=SMG_TOUCH_BID, d_bps=SMG_D_BPS
        )
        assert estimate is not None
        self.assertGreaterEqual(estimate, SMG_ACTUAL_FILL)
        top_tier_limit = SMG_TIERS[0][0]
        self.assertGreater(estimate, top_tier_limit)
        # 59.77 * 1.005 * 1.002 — pinned so a change to the ceiling formula that
        # silently narrows the estimate has to be argued for here.
        self.assertAlmostEqual(estimate, 60.1889877, places=6)

    def test_estimate_is_the_native_trail_ceiling(self) -> None:
        geo = compute_trailing_order_geometry(reference=42.37, trough=41.80, d_bps=100)
        assert geo is not None
        self.assertAlmostEqual(
            entry_fill_estimate(reference=42.37, trough=41.80, d_bps=100), geo.ceiling_price
        )

    def test_degenerate_inputs_return_none(self) -> None:
        for ref, trough, d_bps in ((0.0, 10.0, 50), (10.0, float("nan"), 50), (10.0, 10.0, 0)):
            with self.subTest(ref=ref, trough=trough, d_bps=d_bps):
                self.assertIsNone(entry_fill_estimate(reference=ref, trough=trough, d_bps=d_bps))


class TestArmsInsideExitRegion(unittest.TestCase):
    def test_smg_top_tier_is_inside_the_exit_region(self) -> None:
        estimate = entry_fill_estimate(
            reference=SMG_TOUCH_BID, trough=SMG_TOUCH_BID, d_bps=SMG_D_BPS
        )
        assert estimate is not None
        self.assertTrue(
            arms_inside_exit_region(fill_estimate=estimate, exit_target=SMG_GEOMETRY_TP)
        )

    def test_a_nominal_limit_check_would_miss_this_tier(self) -> None:
        # A tier whose LIMIT (59.60) sits just BELOW the target (59.6277): a
        # build-time `limit < target` check passes, yet a realistic fill above the
        # limit lands past the target. This is the case the issue names.
        limit = 59.60
        self.assertLess(limit, SMG_GEOMETRY_TP)
        estimate = entry_fill_estimate(reference=limit, trough=limit, d_bps=SMG_D_BPS)
        assert estimate is not None
        self.assertGreater(estimate, SMG_GEOMETRY_TP)
        self.assertTrue(
            arms_inside_exit_region(fill_estimate=estimate, exit_target=SMG_GEOMETRY_TP)
        )

    def test_healthy_live_tiers_are_not_inside_the_exit_region(self) -> None:
        # Regression, from the same live journal: the three tiers that were fine.
        for limit, target in (
            (SMG_TIERS[1][0], SMG_GEOMETRY_TP),  # SMG E2 55.754064
            (SMG_TIERS[2][0], SMG_GEOMETRY_TP),  # SMG E3 53.599998
            (67.62, 77.33634),  # ETSY E3
        ):
            with self.subTest(limit=limit):
                estimate = entry_fill_estimate(reference=limit, trough=limit, d_bps=SMG_D_BPS)
                assert estimate is not None
                self.assertFalse(
                    arms_inside_exit_region(fill_estimate=estimate, exit_target=target)
                )

    def test_degenerate_inputs_fail_open(self) -> None:
        # A missing / non-finite / non-positive input must never refuse an arm:
        # a silent gate that stops the whole entry rail is worse than the defect.
        for estimate, target in (
            (None, SMG_GEOMETRY_TP),
            (SMG_ACTUAL_FILL, None),
            (float("nan"), SMG_GEOMETRY_TP),
            (SMG_ACTUAL_FILL, float("nan")),
            (SMG_ACTUAL_FILL, 0.0),
            (-1.0, SMG_GEOMETRY_TP),
        ):
            with self.subTest(estimate=estimate, target=target):
                self.assertFalse(
                    arms_inside_exit_region(fill_estimate=estimate, exit_target=target)
                )


class TestValidityAcrossEveryPartialFillSubset(unittest.TestCase):
    """The issue's core requirement: validity must hold in EVERY partial-fill
    state of the ladder, not only the full-fill state. Table-driven over all
    seven non-empty subsets of the SMG three-tier ladder — for each subset the
    bracket is anchored on that subset's alloc-weighted blend and the SHALLOWEST
    (highest-limit) tier in the subset is the one that can fill inside it."""

    def _subsets(self):
        for size in range(1, len(SMG_TIERS) + 1):
            yield from itertools.combinations(range(len(SMG_TIERS)), size)

    def test_exactly_the_two_e1_bearing_deep_subsets_are_invalid(self) -> None:
        invalid = []
        for indices in self._subsets():
            subset = tuple(SMG_TIERS[i] for i in indices)
            blend = _alloc_weighted_blend(subset)
            target = blend + _K_ATR * SMG_ATR
            shallowest = max(p for p, _w in subset)
            estimate = entry_fill_estimate(reference=shallowest, trough=shallowest, d_bps=SMG_D_BPS)
            assert estimate is not None
            if arms_inside_exit_region(fill_estimate=estimate, exit_target=target):
                invalid.append(indices)
        # {E1,E3} (blend 55.5207, target 59.5527) and {E1,E2,E3} (blend 55.5957,
        # target 59.6277) both put the target below the top tier at 59.786017.
        self.assertEqual(invalid, [(0, 2), (0, 1, 2)])

    def test_e1_alone_is_valid(self) -> None:
        # A lone top-tier fill anchors the bracket on its own limit, so the
        # target (59.786017 + 1.5*2.688 = 63.818) is comfortably above any fill.
        limit = SMG_TIERS[0][0]
        estimate = entry_fill_estimate(reference=limit, trough=limit, d_bps=SMG_D_BPS)
        assert estimate is not None
        self.assertFalse(
            arms_inside_exit_region(fill_estimate=estimate, exit_target=limit + _K_ATR * SMG_ATR)
        )


if __name__ == "__main__":
    unittest.main()
