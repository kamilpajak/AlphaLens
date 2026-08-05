from __future__ import annotations

import unittest

from alphalens_pipeline.brokers.automanager.live_exit_engine import (
    TrancheExit,
    plan_tranche_exits,
    tranche_tag,
)
from broker_contract.sizing import TpTranchePlan


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


if __name__ == "__main__":
    unittest.main()
