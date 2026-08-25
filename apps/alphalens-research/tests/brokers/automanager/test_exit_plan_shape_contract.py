"""The exit plan must be ONE tranche selling the WHOLE position (#1116 round 2,
point 2).

The #1112 arm gate prices the round trip at the quantity of the position it is
about to open. That is only conservative while the exit side sells that same
quantity in one go. LIVE today it does: all three ``tranche_plan`` records in
``~/.alphalens/broker_orders/live/standalone_stops.jsonl`` (SMG 2026-08-19,
uic 23474, ETSY 2026-08-18) carry exactly one ``geometry`` tranche at
``tranche_pct`` 1.0. Nothing in the code says it must stay that way, so this
turns the assumption into a checked contract.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager import costs
from broker_contract.constants import QTY_PRECISION
from broker_contract.contract import _QTY_EPS

_LIVE_POSITION_QTY = 27.0
"""The live 27-share position (uic 23474) the restored ladder is measured
against — the one a reviewer's ``always price at qty=1`` proposal would have
refused."""

_RESTORED_TRANCHE_PCTS: tuple[float, ...] = (0.33, 0.33, 0.34)
"""The brief's own three-way take-profit split, the shape a restored
multi-tranche exit plan would carry."""


class TestQuantityPrecisionHasOneSource(unittest.TestCase):
    """The guard resolves ACTIVE tranches against the broker's share-quantity
    precision, not an arbitrary float epsilon. There must be exactly one such
    number in the tree."""

    def test_the_contract_tier_epsilon_is_the_shared_precision(self) -> None:
        self.assertIs(_QTY_EPS, QTY_PRECISION)


class TestSingleFullPositionTrancheContract(unittest.TestCase):
    def _restored_quantities(self) -> tuple[float, ...]:
        # Exactly how live_exit_engine.plan_tranche_exits sizes a tranche.
        return tuple(round(_LIVE_POSITION_QTY * pct) for pct in _RESTORED_TRANCHE_PCTS)

    def test_todays_single_full_tranche_plan_is_accepted(self) -> None:
        self.assertIsNone(
            costs.single_full_position_tranche_violation(
                tranche_quantities=(_LIVE_POSITION_QTY,), position_qty=_LIVE_POSITION_QTY
            )
        )

    def test_a_restored_three_tranche_plan_is_refused(self) -> None:
        violation = costs.single_full_position_tranche_violation(
            tranche_quantities=self._restored_quantities(), position_qty=_LIVE_POSITION_QTY
        )
        self.assertIsNotNone(violation)
        self.assertIn("3 active", str(violation))

    def test_a_zero_sized_tranche_is_not_an_active_tranche(self) -> None:
        self.assertIsNone(
            costs.single_full_position_tranche_violation(
                tranche_quantities=(_LIVE_POSITION_QTY, 0.0), position_qty=_LIVE_POSITION_QTY
            )
        )

    def test_a_sub_precision_tranche_is_not_an_active_tranche(self) -> None:
        # Below the broker's share-quantity precision nothing can be sold, so it
        # must not turn a healthy plan into a refusal.
        self.assertIsNone(
            costs.single_full_position_tranche_violation(
                tranche_quantities=(_LIVE_POSITION_QTY, QTY_PRECISION / 2.0),
                position_qty=_LIVE_POSITION_QTY,
            )
        )

    def test_one_tranche_that_does_not_sell_the_whole_position_is_refused(self) -> None:
        violation = costs.single_full_position_tranche_violation(
            tranche_quantities=(_LIVE_POSITION_QTY - 10.0,), position_qty=_LIVE_POSITION_QTY
        )
        self.assertIsNotNone(violation)
        self.assertIn("whole position", str(violation))

    def test_a_rounding_sized_difference_is_still_the_whole_position(self) -> None:
        # Owned quantities arrive as floats on the wire; a sub-precision delta is
        # the same position, not a partial tranche.
        self.assertIsNone(
            costs.single_full_position_tranche_violation(
                tranche_quantities=(_LIVE_POSITION_QTY - QTY_PRECISION / 2.0,),
                position_qty=_LIVE_POSITION_QTY,
            )
        )

    def test_an_empty_plan_is_refused_rather_than_treated_as_healthy(self) -> None:
        self.assertIsNotNone(
            costs.single_full_position_tranche_violation(
                tranche_quantities=(), position_qty=_LIVE_POSITION_QTY
            )
        )

    def test_a_degenerate_position_quantity_is_refused(self) -> None:
        for bad in (0.0, -1.0, float("nan"), float("inf")):
            with self.subTest(position_qty=bad):
                self.assertIsNotNone(
                    costs.single_full_position_tranche_violation(
                        tranche_quantities=(1.0,), position_qty=bad
                    )
                )

    def test_a_non_finite_tranche_quantity_is_refused(self) -> None:
        for bad in (float("nan"), float("inf")):
            with self.subTest(tranche_qty=bad):
                self.assertIsNotNone(
                    costs.single_full_position_tranche_violation(
                        tranche_quantities=(bad,), position_qty=_LIVE_POSITION_QTY
                    )
                )


class TestArmGateResolvesThePlanFromTheWatchRecord(unittest.TestCase):
    """``control_loop._exit_plan_shape_refusal`` at the unit level, for the two
    record shapes the end-to-end pass cannot produce."""

    def _record(self, **overrides: object) -> dict[str, object]:
        record: dict[str, object] = {
            "uic": 307,
            "geometry": {"applied": True, "geometry_tp": 90.0},
        }
        record.update(overrides)
        return record

    def test_a_record_without_a_uic_refuses_terminally(self) -> None:
        # Unreachable through _run_entry_watch_pass (a record with no uic never
        # gets a price feed), so it is pinned here rather than end to end.
        refusal = cl._exit_plan_shape_refusal(self._record(uic=None), _LIVE_POSITION_QTY)
        self.assertIsNotNone(refusal)
        assert refusal is not None
        self.assertTrue(refusal.terminal)
        self.assertIn("no uic", refusal.note)

    def test_no_applied_geometry_target_is_not_gated(self) -> None:
        self.assertIsNone(
            cl._exit_plan_shape_refusal(
                self._record(geometry={"applied": False, "geometry_tp": 90.0}),
                _LIVE_POSITION_QTY,
            )
        )


if __name__ == "__main__":
    unittest.main()
