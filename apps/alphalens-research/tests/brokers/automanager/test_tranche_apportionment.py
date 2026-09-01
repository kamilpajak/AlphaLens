"""Integer apportionment of TP-tranche quantities (issue #1112, breakeven_trail
follow-up).

Since #1183 the placed exit plan is the BRIEF's own multi-tranche ladder, whose
``tranche_frac`` values are fractions like 1/3 — and per-tranche
``round(reference_qty * frac)`` silently rounds a small position's every tranche
to zero shares. Measured shapes on the live rail scale (whole-share lattice,
one-digit share counts):

- ``reference_qty = 1``: round(1/3) = 0 for all three tranches -> the position
  has NO take-profit path at all; it can only exit through the disaster stop or
  a trail rescue.
- ``reference_qty = 7`` (the NOV 2026-08-31 pick): 2 + 2 + 2 = 6 -> a permanent
  one-share stop-only remainder after tp3.

The fix is largest-remainder (Hamilton) apportionment over the whole ladder:
floor every quota, then hand the leftover shares out by largest fractional
remainder, ties to the SHALLOWEST tranche. The apportioned total follows the
plan's own declared coverage (``round(reference_qty * sum(frac))``) — a partial
plan is realized as declared, never silently normalized to 100%; full coverage
is enforced at arm time by ``costs.apportioned_coverage_violation``, not here.
"""

from __future__ import annotations

import unittest

from alphalens_pipeline.brokers.automanager.live_exit_engine import (
    apportion_tranche_quantities,
    plan_tranche_exits,
)
from alphalens_pipeline.brokers.execution import RAIL_LATTICE
from broker_contract.sizing import TpTranchePlan

_THIRD = 1.0 / 3.0


def _tr(index: int, target: float, frac: float) -> TpTranchePlan:
    return TpTranchePlan(
        tranche_index=index,
        target_price=target,
        tranche_frac=frac,
        r_multiple=float(index + 2),
        tag=f"tp{index + 1}",
    )


def _brief_ladder(*targets: float) -> tuple[TpTranchePlan, ...]:
    """The equal-thirds TP ladder every brief publishes."""
    return tuple(_tr(i, t, _THIRD) for i, t in enumerate(targets))


class TestApportionTrancheQuantities(unittest.TestCase):
    def test_one_share_goes_whole_to_the_shallowest_tranche(self) -> None:
        self.assertEqual(
            apportion_tranche_quantities(reference_qty=1.0, tranche_fracs=(_THIRD,) * 3),
            (1.0, 0.0, 0.0),
        )

    def test_seven_shares_apportion_3_2_2(self) -> None:
        # The NOV 2026-08-31 shape. Equal remainders tie -> the odd share lands
        # on tp1 (earliest realization, shortest stop-only exposure).
        self.assertEqual(
            apportion_tranche_quantities(reference_qty=7.0, tranche_fracs=(_THIRD,) * 3),
            (3.0, 2.0, 2.0),
        )

    def test_four_shares_apportion_2_1_1(self) -> None:
        self.assertEqual(
            apportion_tranche_quantities(reference_qty=4.0, tranche_fracs=(_THIRD,) * 3),
            (2.0, 1.0, 1.0),
        )

    def test_the_total_is_always_the_declared_coverage(self) -> None:
        for ref in (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 11.0, 27.0, 100.0):
            with self.subTest(reference_qty=ref):
                quantities = apportion_tranche_quantities(
                    reference_qty=ref, tranche_fracs=(_THIRD,) * 3
                )
                self.assertEqual(sum(quantities), round(ref), "no stop-only remainder")

    def test_the_largest_remainder_wins_when_remainders_differ(self) -> None:
        # Quotas 1.0 / 2.6 / 1.4: floors 1/2/1, one leftover share, and tp2's
        # remainder (0.6) beats tp3's (0.4).
        self.assertEqual(
            apportion_tranche_quantities(reference_qty=5.0, tranche_fracs=(0.2, 0.52, 0.28)),
            (1.0, 3.0, 1.0),
        )

    def test_the_single_geometry_tranche_is_unchanged(self) -> None:
        self.assertEqual(
            apportion_tranche_quantities(reference_qty=27.0, tranche_fracs=(1.0,)),
            (27.0,),
        )

    def test_a_partial_coverage_plan_is_realized_as_declared(self) -> None:
        # Declared 80% coverage sells 80 of 100 — apportionment must NOT
        # normalize the fractions to 100%. Full coverage is a separate arm-time
        # contract, not this function's job.
        self.assertEqual(
            apportion_tranche_quantities(reference_qty=100.0, tranche_fracs=(0.5, 0.3)),
            (50.0, 30.0),
        )


class TestPlanTrancheExitsApportions(unittest.TestCase):
    """The engine plans the APPORTIONED quantity, so a small position keeps a
    take-profit path and a 7-share ladder leaves no remainder."""

    def _plan(
        self,
        *,
        price: float,
        reference_qty: float,
        owned: float,
        already_fired: frozenset[str] = frozenset(),
    ):
        return plan_tranche_exits(
            price=price,
            tp_tranches=_brief_ladder(16.0, 18.0, 20.0),
            reference_qty=reference_qty,
            owned=owned,
            already_fired=already_fired,
            lattice=RAIL_LATTICE,
        )

    def test_a_one_share_position_sells_whole_at_tp1(self) -> None:
        exits = self._plan(price=16.5, reference_qty=1.0, owned=1.0)
        self.assertEqual([(e.tag, e.qty) for e in exits], [("tp1", 1.0)])

    def test_seven_shares_fully_covered_across_the_ladder(self) -> None:
        exits = self._plan(price=21.0, reference_qty=7.0, owned=7.0)
        self.assertEqual(
            [(e.tag, e.qty) for e in exits],
            [("tp1", 3.0), ("tp2", 2.0), ("tp3", 2.0)],
            "the odd share belongs to tp1; nothing is left stop-only after tp3",
        )

    def test_eleven_shares_keep_their_existing_split(self) -> None:
        # 11 was already fully covered through the `available` clamp (4+4+3);
        # apportionment must not change a healthy split.
        exits = self._plan(price=21.0, reference_qty=11.0, owned=11.0)
        self.assertEqual([e.qty for e in exits], [4.0, 4.0, 3.0])

    def test_a_fired_tranche_does_not_reshuffle_the_others(self) -> None:
        # Apportionment is a function of (reference_qty, fracs) alone — tp2/tp3
        # keep the same share counts whether or not tp1 already fired.
        exits = self._plan(
            price=21.0, reference_qty=7.0, owned=4.0, already_fired=frozenset({"tp1"})
        )
        self.assertEqual([(e.tag, e.qty) for e in exits], [("tp2", 2.0), ("tp3", 2.0)])

    def test_owned_still_caps_the_batch(self) -> None:
        # A partial fill (owned < reference) must never plan more than owned.
        exits = self._plan(price=21.0, reference_qty=7.0, owned=3.0)
        self.assertEqual(sum(e.qty for e in exits), 3.0)


if __name__ == "__main__":
    unittest.main()
