"""The exit sizer asks the venue how many shares it can sell.

THE MEASURED INCIDENT. `available = round(owned)` rounds UP. At 0.669 shares
held the rail computed `round(0.669) == 1`, tried to sell a share it did not
have, and then read `round(0.669) - 1 == 0` as "nothing remains" and CANCELLED
the standalone disaster stop. A position could lose its stop on the strength of
a share that was never there.

`round()` is also banker's rounding, so `round(2.5) == 2` — the sizer was not
even consistently generous.

WHY THIS SHIPS WITHOUT THE FULL 48-SITE REFACTOR. The design memo assumed the
two halves could not be split, because a planner on a lattice and a protection
pass on a bare 0.5 epsilon would disagree. Measured on a whole-share venue —
the only one connected — the disagreement is entirely in the SAFE direction:

    owned   exit planner   protection   outcome
    0.669   available 0    real         nothing sold, stop kept
    0.51    available 0    real         nothing sold, stop kept
    1.4     available 1    real         consistent

The dangerous shapes need a lattice that is not whole shares, and there are TWO
of them — the first version of this guard only caught one:

- FINER than the epsilon (a fractional venue): the sizer could sell a quantity
  the protection pass does not treat as a live position.
- COARSER than one share (a round-lot venue): `quantize_down` under-counts the
  holding, so at 150 owned with a 100-share step `max(100 - 100, 0) == 0` reads
  as a full close and CANCELS the disaster stop over 50 shares it cannot see.

Neither is connected, and `assert_rail_lattice` refuses both at all three
entry points, so they are unreachable by construction rather than by luck.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from alphalens_pipeline.brokers.automanager.live_exit_engine import (
    TrancheExit,
    execute_tranche_exit,
    plan_tranche_exits,
    run_live_exits,
    tranche_tag,
)
from alphalens_pipeline.brokers.execution import RAIL_LATTICE, assert_rail_lattice
from broker_contract.constants import QTY_PRECISION
from broker_contract.contract import BrokerCapabilityError, BrokerError
from broker_contract.quantity import QuantityLattice
from broker_contract.sizing import TpTranchePlan


def _tr(index: int, target: float, frac: float) -> TpTranchePlan:
    return TpTranchePlan(
        tranche_index=index,
        target_price=target,
        tranche_frac=frac,
        r_multiple=1.0,
        tag=tranche_tag(index),
    )


_WHOLE_POSITION = (_tr(0, 16.0, 1.0),)


@dataclass
class _Leg:
    """The standalone disaster stop the executor would amend or cancel."""

    order_id: str = "sl-1"
    side: str = "SELL"
    order_type: str = "StopIfTraded"
    amount: float = 150.0


class _NeverCalled:
    """Fails loudly if the guard lets execution reach the broker at all."""

    def __getattr__(self, name: str):
        def _boom(*args: object, **kwargs: object) -> None:
            raise AssertionError(f"broker.{name} was called past the lattice guard")

        return _boom


class TestTheIncidentQuantity(unittest.TestCase):
    def _plan(self, owned: float) -> list:
        return plan_tranche_exits(
            price=20.0,  # far past the target, so a healthy position WOULD fire
            tp_tranches=_WHOLE_POSITION,
            reference_qty=owned,  # the whole position is the tranche base
            owned=owned,
            already_fired=frozenset(),
            lattice=RAIL_LATTICE,
        )

    def test_a_sub_share_holding_sells_nothing(self) -> None:
        # `round(0.669)` said 1. The venue cannot express 0.669 shares, so the
        # only honest answer is that there is nothing to sell.
        self.assertEqual(self._plan(0.669), [])

    def test_the_half_share_boundary_sells_nothing_either(self) -> None:
        # 0.51 rounded UP under the old arithmetic too. Half a share is still
        # not a share.
        self.assertEqual(self._plan(0.51), [])

    def test_a_holding_that_can_be_sold_still_fires(self) -> None:
        # Guard against a fix that refuses everything.
        out = self._plan(6.0)
        self.assertEqual([e.tag for e in out], ["tp1"])
        self.assertEqual(out[0].qty, 6)

    def test_a_fractional_excess_is_floored_never_rounded_up(self) -> None:
        # 1.4 shares is one sellable share, not two. Banker's rounding would
        # have said 1 here but 2 at 2.5; flooring is unambiguous.
        self.assertEqual(self._plan(1.4)[0].qty, 1)
        self.assertEqual(self._plan(2.5)[0].qty, 2)


class TestTheRailLatticeGuard(unittest.TestCase):
    def test_the_rail_lattice_is_whole_shares_and_matches_the_epsilon(self) -> None:
        # Half a step is 0.5 — the value the protection epsilon has always
        # been. That identity is why the two halves can be migrated separately.
        self.assertEqual(RAIL_LATTICE.step, 1.0)
        self.assertEqual(RAIL_LATTICE.step / 2.0, QTY_PRECISION)

    def test_a_lattice_finer_than_the_epsilon_is_refused(self) -> None:
        # The measured-dangerous shape: a venue whose sellable quantity is
        # below what the protection pass considers a real position. Refused
        # until those sites migrate, so it cannot arrive by accident.
        with self.assertRaises(BrokerCapabilityError):
            assert_rail_lattice(QuantityLattice(step=0.001, min_qty=0.001, precision=3))

    def test_a_lattice_that_matches_only_on_step_is_refused(self) -> None:
        # The guard pins the ARITHMETIC, not one field of it. A lattice with the
        # right step but a 100-share minimum is a different policy than the one
        # the rail declared, and comparing `step / 2` alone would admit it.
        with self.assertRaises(BrokerCapabilityError):
            assert_rail_lattice(QuantityLattice(step=1.0, min_qty=100.0, precision=0))

    def test_the_refusal_is_containable_by_the_live_exits_boundary(self) -> None:
        # `_run_live_exits_pass` catches `BrokerError` and nothing else, and the
        # protection pass runs right after it in the same tick. A refusal that
        # is not a BrokerError would kill the tick and take the never-naked
        # backstop with it — the guard would cause the class of harm it exists
        # to prevent.
        with self.assertRaises(BrokerError):
            assert_rail_lattice(QuantityLattice(step=0.001, min_qty=0.001, precision=3))

    def test_a_lattice_COARSER_than_one_share_is_refused_too(self) -> None:
        # The other side, and the one a "finer than the epsilon" guard misses.
        # A round-lot venue (step 100) is dangerous in the opposite direction:
        # `quantize_down` UNDER-counts the holding, so the residual becomes
        # invisible to the stop arithmetic. Measured on this code before the
        # guard was tightened, at owned=150 with a 100-share step:
        #
        #     quantize_down(150) -> 100
        #     new_sl_qty = max(100 - 100, 0) = 0  ->  CANCEL the disaster stop
        #
        # while 50 shares were still held. Same naked position as the incident
        # this PR fixes, reached from the other direction.
        for step in (10.0, 100.0):
            with self.subTest(step=step), self.assertRaises(BrokerCapabilityError):
                assert_rail_lattice(QuantityLattice(step=step, min_qty=step, precision=0))

    def test_the_whole_share_lattice_passes_the_guard(self) -> None:
        assert_rail_lattice(RAIL_LATTICE)

    def test_the_guard_cannot_be_stepped_around_by_call_order(self) -> None:
        # A guard that only fires on ONE entry point is a guard you can walk
        # past. `execute_tranche_exit` is module-level and mutates broker state
        # (it cancels and amends stops), so it must refuse a lattice it cannot
        # reason about on its own, not trust that a planner ran first.
        # Demonstrated: calling it directly with a coarse lattice used to run
        # happily and cancel the stop.
        coarse = QuantityLattice(step=100.0, min_qty=100.0, precision=0)
        with self.assertRaises(BrokerCapabilityError):
            execute_tranche_exit(
                broker=_NeverCalled(),
                uic=1,
                exit=TrancheExit(tag=tranche_tag(0), qty=100, target_price=16.0),
                sl_leg=_Leg(),
                stop_price=10.0,
                request_ref="r",
                lattice=coarse,
            )

    def test_run_live_exits_refuses_the_pass_before_touching_a_position(self) -> None:
        # The production entry point refuses once per pass, so a bad lattice
        # never reaches per-position work at all.
        coarse = QuantityLattice(step=100.0, min_qty=100.0, precision=0)
        with self.assertRaises(BrokerCapabilityError):
            run_live_exits(_NeverCalled(), _NeverCalled(), (), lattice=coarse)


if __name__ == "__main__":
    unittest.main()


class TestMalformedReferenceQuantity(unittest.TestCase):
    """`owned` was guarded in #1126; `reference_qty` — its sibling in the same
    expression — was not, and it is the one that comes off DISK.

    `fold_tranche_plans` folds a journal line's `reference_qty` with a bare
    `float()`, and JSON spells non-finite values `NaN` / `Infinity`, so both
    survive the fold. `round(nan)` raises ValueError and `round(inf)` raises
    OverflowError — neither is a `BrokerError`, so neither is caught by
    `_run_live_exits_pass`, and the statement after it in the tick is the
    never-naked protection pass. One malformed journal line would starve
    protection for every position, not just the one it describes.
    """

    def _plan(self, reference_qty: object) -> list:
        return plan_tranche_exits(
            price=20.0,
            tp_tranches=_WHOLE_POSITION,
            reference_qty=reference_qty,  # type: ignore[arg-type]
            owned=10.0,
            already_fired=frozenset(),
            lattice=RAIL_LATTICE,
        )

    def test_non_finite_reference_qty_plans_nothing_instead_of_raising(self) -> None:
        for bad in (float("nan"), float("inf"), float("-inf"), None):
            with self.subTest(reference_qty=bad):
                self.assertEqual(self._plan(bad), [])

    def test_a_healthy_reference_qty_still_fires(self) -> None:
        # Guard against a fix that refuses everything.
        self.assertEqual(self._plan(10.0)[0].qty, 10)
