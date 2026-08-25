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

from alphalens_pipeline.brokers.automanager.costs import min_profitable_exit_price
from alphalens_pipeline.brokers.automanager.entry_trail_geometry import (
    CEILING_EPS_FRAC,
    TRAILING_STEP_FRACTION,
    arms_inside_exit_region,
    compute_trailing_order_geometry,
    entry_fill_estimate,
)
from alphalens_pipeline.paper.sizing import build_exit_geometry_spec, planned_blended_entry
from broker_contract.exit_geometry import resolve_exit_policy

from tests.incident_1112_fixture import (
    SMG_ACTUAL_FILL,
    SMG_ATR,
    SMG_D_BPS,
    SMG_GEOMETRY_TP,
    SMG_TIERS,
    SMG_TOUCH_BID,
    smg_brief_trade_setup,
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
            arms_inside_exit_region(fill_estimate=estimate, exit_target=SMG_GEOMETRY_TP, qty=1.0)
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
            arms_inside_exit_region(fill_estimate=estimate, exit_target=SMG_GEOMETRY_TP, qty=1.0)
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
                    arms_inside_exit_region(fill_estimate=estimate, exit_target=target, qty=1.0)
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
                    arms_inside_exit_region(fill_estimate=estimate, exit_target=target, qty=1.0)
                )


class TestArmGateChargesRoundTripCostPlusBuffer(unittest.TestCase):
    """The issue's Goal is not ``target > fill``, it is
    ``T(s) > max_filled_price(s) + exit_cost + E_min``. A tier whose own target
    cannot pay the round trip must be refused at ARM time, not admitted and then
    refused again by the step-2 exit gate — that combination submits an entry
    and then strands it with no take-profit path until the disaster stop.
    """

    def _estimate(self, price: float) -> float:
        estimate = entry_fill_estimate(reference=price, trough=price, d_bps=SMG_D_BPS)
        assert estimate is not None
        return estimate

    def test_a_target_the_round_trip_cannot_pay_is_inside_the_exit_region(self) -> None:
        estimate = self._estimate(SMG_TOUCH_BID)
        target = 61.00
        # A bare "target above the fill" check admits this tier: 61.00 sits
        # 134.7 bps above the estimate. One share at about $60 pays roughly
        # 382 bps round trip, so the trade is a loss the moment it opens.
        self.assertGreater(target, estimate)
        self.assertTrue(
            arms_inside_exit_region(fill_estimate=estimate, exit_target=target, qty=1.0)
        )

    def test_a_target_that_clears_cost_plus_buffer_arms(self) -> None:
        estimate = self._estimate(SMG_TOUCH_BID)
        required = min_profitable_exit_price(entry_price=estimate, qty=1.0)
        assert required is not None
        self.assertAlmostEqual(required, 62.790877, places=5)
        self.assertFalse(
            arms_inside_exit_region(fill_estimate=estimate, exit_target=required + 0.01, qty=1.0)
        )
        self.assertTrue(
            arms_inside_exit_region(fill_estimate=estimate, exit_target=required - 0.01, qty=1.0)
        )

    def test_healthy_live_tiers_still_clear_cost_on_the_one_share_rail(self) -> None:
        # Regression on the LIVE rail size (1 share), the size the cost model is
        # harshest at: the $1 per-fill minimum dominates a $60 notional.
        for label, limit, target in (
            ("SMG E2", SMG_TIERS[1][0], SMG_GEOMETRY_TP),
            ("SMG E3", SMG_TIERS[2][0], SMG_GEOMETRY_TP),
            ("ETSY E3", 67.62, 77.33634),
        ):
            with self.subTest(tier=label):
                self.assertFalse(
                    arms_inside_exit_region(
                        fill_estimate=self._estimate(limit), exit_target=target, qty=1.0
                    )
                )

    def test_degenerate_qty_fails_open(self) -> None:
        estimate = self._estimate(SMG_TOUCH_BID)
        for qty in (0.0, -1.0, float("nan"), None):
            with self.subTest(qty=qty):
                self.assertFalse(
                    arms_inside_exit_region(
                        fill_estimate=estimate, exit_target=SMG_GEOMETRY_TP, qty=qty
                    )
                )


class TestValidityAcrossEveryPartialFillSubset(unittest.TestCase):
    """The issue's core requirement: validity must hold in EVERY partial-fill
    state of the ladder, not only the full-fill state.

    Table-driven over all seven non-empty subsets of the SMG three-tier ladder.
    Each subset's target comes from the REAL
    :func:`alphalens_pipeline.paper.sizing.build_exit_geometry_spec` over a brief
    whose ``entry_tiers`` are that subset — the same builder the router stamps
    ``geometry_tp`` from — so the table follows any change to how production
    picks a target instead of re-deriving one of its own.

    WHAT THIS COVERS, AND WHAT IT DOES NOT (#1116 round 2, point 5). The seven
    subsets are the complete state space only under an ATOMIC-TIER assumption:
    that each tier either fills in full or not at all. Our own incident refutes
    that assumption — on 2026-08-24 SMG carried ``reference_qty`` 6.0 and filled
    1 share, a state no subset of {E1, E2, E3} describes. So these seven rows
    are seven POINTS sampled from a much larger continuous space (any partial
    quantity on any subset of tiers), not an exhaustive enumeration of it.

    They are still the right seven points: the target each subset produces comes
    from the alloc-weighted blend of the tiers in it, and within a subset the
    shallowest tier's fill estimate is what the gate compares, neither of which
    moves with the filled QUANTITY. What a partial fill does change is the cost
    side — a smaller realised notional pays proportionally more of the per-fill
    USD minimum — and that is measured at the gates themselves
    (``TestArmGateChargesRoundTripCostPlusBuffer`` at one share, the exit-side
    cost gate in ``test_live_exit_decision.py``), not here.
    """

    def _subsets(self):
        for size in range(1, len(SMG_TIERS) + 1):
            yield from itertools.combinations(range(len(SMG_TIERS)), size)

    def _setup_for(self, indices: tuple[int, ...]) -> dict:
        setup = smg_brief_trade_setup()
        setup["entry_tiers"] = [setup["entry_tiers"][i] for i in indices]
        return setup

    def _shallowest_estimate(self, indices: tuple[int, ...]) -> float:
        # The highest-limit tier in the subset is the one that can fill inside
        # the bracket; a deeper tier only ever fills further below it.
        shallowest = max(SMG_TIERS[i][0] for i in indices)
        estimate = entry_fill_estimate(reference=shallowest, trough=shallowest, d_bps=SMG_D_BPS)
        assert estimate is not None
        return estimate

    def test_every_partial_fill_subset_is_valid_as_production_builds_it(self) -> None:
        invalid = []
        for indices in self._subsets():
            spec = build_exit_geometry_spec(self._setup_for(indices))
            assert spec is not None
            if arms_inside_exit_region(
                fill_estimate=self._shallowest_estimate(indices),
                exit_target=spec.initial_levels.tp,
                qty=1.0,
            ):
                invalid.append(indices)
        self.assertEqual(invalid, [], "no partial-fill state may open inside its own exit region")

    def test_without_the_brief_floor_four_of_the_seven_subsets_are_invalid(self) -> None:
        # The discriminator for the test above: run the SAME table against the
        # policy's RAW levels (no step-3 clamp) and the ladder fails in four
        # states, so an all-valid result is a property of the fix and not of a
        # gate that never fires.
        #
        # Note {E2,E3} is among them (raw target 58.5091 against a fill estimate
        # of 56.1449, which needs 58.7060): on the one-share LIVE rail the plain
        # 1.5*ATR bracket barely pays its own round trip, quite apart from the
        # top-tier defect this issue is about.
        policy = resolve_exit_policy("atr_bracket_1p5")
        invalid = []
        for indices in self._subsets():
            setup = self._setup_for(indices)
            blend = planned_blended_entry(setup)
            assert blend is not None
            levels = policy.decide_placement_geometry(blend, SMG_ATR, ceiling_price=None)
            assert levels is not None
            if arms_inside_exit_region(
                fill_estimate=self._shallowest_estimate(indices),
                exit_target=levels[1],
                qty=1.0,
            ):
                invalid.append(indices)
        self.assertEqual(invalid, [(0, 1), (0, 2), (1, 2), (0, 1, 2)])


if __name__ == "__main__":
    unittest.main()
