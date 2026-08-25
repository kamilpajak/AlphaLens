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

The dangerous shape (sell a quantity protection does not consider real) needs a
lattice FINER than the epsilon, i.e. a fractional venue. None exists, and
`_assert_rail_lattice` refuses to run on one until the epsilon sites migrate —
so it is unreachable by construction rather than by luck.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.brokers.automanager.live_exit_engine import (
    plan_tranche_exits,
    tranche_tag,
)
from alphalens_pipeline.brokers.execution import RAIL_LATTICE, assert_rail_lattice
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
        self.assertEqual(RAIL_LATTICE.step / 2.0, 0.5)

    def test_a_lattice_finer_than_the_epsilon_is_refused(self) -> None:
        # The measured-dangerous shape: a venue whose sellable quantity is
        # below what the protection pass considers a real position. Refused
        # until those sites migrate, so it cannot arrive by accident.
        with self.assertRaises(NotImplementedError):
            assert_rail_lattice(QuantityLattice(step=0.001, min_qty=0.001, precision=3))

    def test_the_whole_share_lattice_passes_the_guard(self) -> None:
        assert_rail_lattice(RAIL_LATTICE)


if __name__ == "__main__":
    unittest.main()
