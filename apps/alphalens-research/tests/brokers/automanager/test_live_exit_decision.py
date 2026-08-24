from __future__ import annotations

import unittest

from alphalens_pipeline.brokers.automanager.entry_trail_geometry import (
    arms_inside_exit_region,
    entry_fill_estimate,
)
from alphalens_pipeline.brokers.automanager.live_exit_engine import (
    EXIT_EDGE_MIN_BPS,
    TrancheExit,
    plan_tranche_exits,
    tranche_tag,
)
from broker_contract.costs import round_trip_fee_bps
from broker_contract.sizing import TpTranchePlan

from tests.incident_1112_fixture import (
    SMG_ACTUAL_FILL,
    SMG_D_BPS,
    SMG_EXIT_DECISION_BID,
    SMG_GEOMETRY_TP,
    SMG_ROUND_TRIP_FEE_BPS,
    SMG_TOUCH_BID,
    SMG_TP_TRANCHES,
)


def _tr(index, target, pct):
    return TpTranchePlan(
        tranche_index=index,
        target_price=target,
        tranche_pct=pct,
        r_multiple=1.0,
        tag=tranche_tag(index),
    )


_LADDER = (_tr(0, 16.0, 0.5), _tr(1, 18.0, 0.3), _tr(2, 20.0, 0.2))  # TP1/TP2/TP3 of 100 shares


class TestPlanTrancheExits(unittest.TestCase):
    def test_no_touch_no_exits(self):
        out = plan_tranche_exits(
            price=15.0, tp_tranches=_LADDER, reference_qty=100, owned=100, already_fired=frozenset()
        )
        self.assertEqual(out, [])

    def test_first_target_touched_fires_tp1_only(self):
        out = plan_tranche_exits(
            price=16.5, tp_tranches=_LADDER, reference_qty=100, owned=100, already_fired=frozenset()
        )
        self.assertEqual(out, [TrancheExit(tag="tp1", qty=50, target_price=16.0)])

    def test_gap_through_two_targets_fires_both_within_owned(self):
        out = plan_tranche_exits(
            price=18.5, tp_tranches=_LADDER, reference_qty=100, owned=100, already_fired=frozenset()
        )
        self.assertEqual([e.tag for e in out], ["tp1", "tp2"])
        self.assertEqual([e.qty for e in out], [50, 30])

    def test_already_fired_is_skipped(self):
        out = plan_tranche_exits(
            price=18.5,
            tp_tranches=_LADDER,
            reference_qty=100,
            owned=50,
            already_fired=frozenset({"tp1"}),
        )
        self.assertEqual(out, [TrancheExit(tag="tp2", qty=30, target_price=18.0)])

    def test_qty_clamped_to_available_owned(self):
        # owned only 20 left but tp1(50)+tp2(30) both triggered -> fire tp1=20, tp2=0(skip)
        out = plan_tranche_exits(
            price=18.5, tp_tranches=_LADDER, reference_qty=100, owned=20, already_fired=frozenset()
        )
        self.assertEqual(out, [TrancheExit(tag="tp1", qty=20, target_price=16.0)])

    def test_tranche_tag(self):
        self.assertEqual([tranche_tag(i) for i in range(3)], ["tp1", "tp2", "tp3"])


def _geometry_tranche(target):
    """The single 100%-of-position tranche the geometry policy places (the shape
    ``control_loop._geometry_tranche_ladder`` journals)."""
    return TpTranchePlan(
        tranche_index=0, target_price=target, tranche_pct=1.0, r_multiple=0.0, tag="geometry"
    )


class TestExitCostGate(unittest.TestCase):
    """Issue #1112 step 2: an exit whose distance from the REALISED entry is
    inside round-trip cost plus the declared ``EXIT_EDGE_MIN_BPS`` buffer is
    refused and logged, not fired.

    Pinned on the LIVE SMG round trip of 2026-08-24: bought 1 share at 59.9261,
    sold 62 seconds later at bid 59.89 against a target of 59.62762.
    """

    def _plan(self, *, price, target, realised_entry, owned=1, reference_qty=1):
        return plan_tranche_exits(
            price=price,
            tp_tranches=(_geometry_tranche(target),),
            reference_qty=reference_qty,
            owned=owned,
            already_fired=frozenset(),
            realised_entry=realised_entry,
        )

    def test_the_smg_exit_is_refused(self):
        out = self._plan(
            price=SMG_EXIT_DECISION_BID,
            target=SMG_GEOMETRY_TP,
            realised_entry=SMG_ACTUAL_FILL,
        )
        self.assertEqual(out, [])

    def test_the_cost_model_reproduces_the_measured_incident_cost(self):
        # 1 share x 59.9261, FX conversion applies, USD $1-per-side minimum.
        cost = round_trip_fee_bps(SMG_ACTUAL_FILL, fx_applies=True, min_commission_applies=True)
        self.assertAlmostEqual(cost, SMG_ROUND_TRIP_FEE_BPS, delta=0.1)
        realised_distance_bps = (SMG_EXIT_DECISION_BID / SMG_ACTUAL_FILL - 1.0) * 10_000.0
        self.assertAlmostEqual(realised_distance_bps, -6.02, delta=0.01)
        self.assertLess(realised_distance_bps, cost + EXIT_EDGE_MIN_BPS)

    def test_e_min_is_a_declared_positive_buffer(self):
        # The buffer is DECLARED, never derived from the fee: a filter set
        # exactly equal to round-trip cost is known to be suboptimal.
        self.assertIsInstance(EXIT_EDGE_MIN_BPS, float)
        self.assertGreater(EXIT_EDGE_MIN_BPS, 0.0)

    def test_a_genuinely_profitable_target_still_fires(self):
        # The brief's own second tranche (68.34) sits about 1404 bps above the
        # realised entry — far outside cost plus buffer. The gate is not a
        # blanket off-switch.
        out = self._plan(price=69.0, target=SMG_TP_TRANCHES[1], realised_entry=SMG_ACTUAL_FILL)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].tag, "tp1")

    def test_refusal_is_logged_with_the_numbers_an_operator_needs(self):
        with self.assertLogs(
            "alphalens_pipeline.brokers.automanager.live_exit_engine", level="WARNING"
        ) as captured:
            self._plan(
                price=SMG_EXIT_DECISION_BID,
                target=SMG_GEOMETRY_TP,
                realised_entry=SMG_ACTUAL_FILL,
            )
        self.assertEqual(len(captured.records), 1)
        message = captured.records[0].getMessage()
        for token in ("59.9261", "59.6277", "383.7", "E_min", "bps"):
            self.assertIn(token, message)

    def test_no_realised_entry_fails_open(self):
        # avg_price unknown (the SIM NoAccess sentinel, a NaN, or a broker read
        # that never happened): fire as before rather than strand the position.
        for realised_entry in (None, 0.0, -1.0, float("nan")):
            with self.subTest(realised_entry=realised_entry):
                out = self._plan(
                    price=SMG_EXIT_DECISION_BID,
                    target=SMG_GEOMETRY_TP,
                    realised_entry=realised_entry,
                )
                self.assertEqual(len(out), 1)

    def test_default_call_without_realised_entry_is_unchanged(self):
        # The six pre-existing callers pass no realised_entry; their behaviour
        # must be byte-identical.
        out = plan_tranche_exits(
            price=16.5, tp_tranches=_LADDER, reference_qty=100, owned=100, already_fired=frozenset()
        )
        self.assertEqual(out, [TrancheExit(tag="tp1", qty=50, target_price=16.0)])


class TestCostGateKeepsTheLadderInOrder(unittest.TestCase):
    """The cost threshold depends on the TRANCHE's own notional (the per-fill USD
    minimum weighs more on a small tranche), so a shallow small tranche can be
    refused while a deeper large one at a HIGHER target clears. Firing the deeper
    one first would advance ``already_fired`` and the stop-shrink accounting out
    of ladder order. A refused tranche stops the batch instead.
    """

    _SMALL_FIRST = (_tr(0, 10.1, 0.10), _tr(1, 10.15, 0.90))

    def _plan(self, price, realised_entry):
        return plan_tranche_exits(
            price=price,
            tp_tranches=self._SMALL_FIRST,
            reference_qty=100,
            owned=100,
            already_fired=frozenset(),
            realised_entry=realised_entry,
        )

    def test_a_refused_tranche_blocks_the_deeper_ones_in_the_same_pass(self):
        # TP1 is 10 shares at 10.10 (250 bps round trip on a $100 notional, so
        # 300 bps required against 200 bps of edge -> refused). TP2 is 90 shares
        # at 10.15, where the $1 minimum no longer binds, so on its own terms it
        # would fire.
        self.assertEqual(self._plan(price=10.2, realised_entry=10.0), [])

    def test_a_ladder_whose_first_tranche_clears_still_fires_both(self):
        # The block above is ordering, not a blanket refusal: once TP1 itself
        # clears, the whole touched batch goes out as before.
        out = self._plan(price=11.0, realised_entry=10.0)
        self.assertEqual([e.tag for e in out], ["tp1", "tp2"])
        self.assertEqual([e.qty for e in out], [10, 90])


class TestArmGateAndExitGateAgree(unittest.TestCase):
    """The two #1112 gates must draw the SAME line. If the arm gate admits a
    tier the exit gate would later refuse, the rail submits an entry and then
    has no take-profit path for it until the disaster stop — a worse state than
    the defect either gate was added to prevent.
    """

    def _fires(self, *, target, realised_entry):
        return bool(
            plan_tranche_exits(
                price=target,
                tp_tranches=(_geometry_tranche(target),),
                reference_qty=1,
                owned=1,
                already_fired=frozenset(),
                realised_entry=realised_entry,
            )
        )

    def test_every_target_the_arm_gate_admits_is_one_the_exit_gate_would_fire(self):
        estimate = entry_fill_estimate(
            reference=SMG_TOUCH_BID, trough=SMG_TOUCH_BID, d_bps=SMG_D_BPS
        )
        assert estimate is not None
        admitted = []
        for target in (60.5, 61.0, 62.0, 62.5, 62.7, 62.8, 63.0, 65.25, 70.0):
            with self.subTest(target=target):
                arms = not arms_inside_exit_region(
                    fill_estimate=estimate, exit_target=target, qty=1.0
                )
                fires = self._fires(target=target, realised_entry=estimate)
                self.assertEqual(
                    arms,
                    fires,
                    f"arm gate ({arms}) and exit gate ({fires}) disagree at target {target}",
                )
                if arms:
                    admitted.append(target)
        # The table has to contain BOTH verdicts, or the agreement above is
        # vacuous (a gate that always refuses would pass it too).
        self.assertEqual(admitted, [62.8, 63.0, 65.25, 70.0])


if __name__ == "__main__":
    unittest.main()
