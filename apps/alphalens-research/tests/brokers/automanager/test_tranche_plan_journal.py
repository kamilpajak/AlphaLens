"""Unit coverage for the ``tranche_plan`` journal line (INC-5 Task 1) --
the per-uic TP ladder the live-exit engine needs at placement time. The
``planned`` line journals only a scalar ``take_profit``; ``tranche_plan``
persists the FULL tp_tranches tuple + the sizing base (reference_qty) + the
stop price, so a later tick can rebuild a ``ManagedExit`` without re-reading
the brief. Purely additive telemetry: nothing reads the fold yet (INERT)."""

from __future__ import annotations

import json
import unittest

from alphalens_pipeline.brokers.automanager.control_loop import (
    _build_tranche_plan_line,
    fold_tranche_plans,
)
from broker_contract.sizing import TpTranchePlan


def _tr(
    index: int, target: float, pct: float, *, r: float = 1.5, tag: str | None = None
) -> TpTranchePlan:
    return TpTranchePlan(
        tranche_index=index,
        target_price=target,
        tranche_pct=pct,
        r_multiple=r,
        tag=tag or f"tp{index + 1}",
    )


class TestBuildTranchePlanLine(unittest.TestCase):
    def test_pick_key_is_stamped_when_given(self) -> None:
        # 2026-08-19 adjudication finding 4: the watch path stamps its pick
        # identity so an identity-idempotent re-append never resets the
        # fired-tranche fold.
        line = _build_tranche_plan_line(
            uic=486,
            tp_tranches=(_tr(0, 16.0, 1.0),),
            reference_qty=100.0,
            stop_price=13.0,
            pick_key="KO:2026-07-20",
        )
        self.assertEqual(line["pick_key"], "KO:2026-07-20")

    def test_pick_key_is_omitted_when_not_given(self) -> None:
        # Bracket-path byte-identity: no pick_key kwarg -> no pick_key field.
        line = _build_tranche_plan_line(
            uic=486, tp_tranches=(_tr(0, 16.0, 1.0),), reference_qty=100.0, stop_price=13.0
        )
        self.assertNotIn("pick_key", line)

    def test_builds_a_json_serializable_tranche_plan_line(self) -> None:
        tranches = (_tr(0, 16.0, 0.5), _tr(1, 18.0, 0.3), _tr(2, 20.0, 0.2))
        line = _build_tranche_plan_line(
            uic=486, tp_tranches=tranches, reference_qty=100.0, stop_price=13.0
        )
        self.assertEqual(line["kind"], "tranche_plan")
        self.assertEqual(line["uic"], 486)
        self.assertEqual(line["reference_qty"], 100.0)
        self.assertEqual(line["stop_price"], 13.0)
        self.assertEqual(
            line["tp_tranches"],
            [
                {
                    "tranche_index": 0,
                    "target_price": 16.0,
                    "tranche_pct": 0.5,
                    "r_multiple": 1.5,
                    "tag": "tp1",
                },
                {
                    "tranche_index": 1,
                    "target_price": 18.0,
                    "tranche_pct": 0.3,
                    "r_multiple": 1.5,
                    "tag": "tp2",
                },
                {
                    "tranche_index": 2,
                    "target_price": 20.0,
                    "tranche_pct": 0.2,
                    "r_multiple": 1.5,
                    "tag": "tp3",
                },
            ],
        )
        # The journal writer round-trips every line through json.dumps(..., default=str).
        json.dumps(line, sort_keys=True, default=str)

    def test_empty_tranches_builds_an_empty_list(self) -> None:
        line = _build_tranche_plan_line(uic=1, tp_tranches=(), reference_qty=0.0, stop_price=9.0)
        self.assertEqual(line["tp_tranches"], [])


class TestFoldTranchePlans(unittest.TestCase):
    def test_round_trips_through_fold_to_the_exact_tuple(self) -> None:
        tranches = (_tr(0, 16.0, 0.5), _tr(1, 18.0, 0.3))
        line = _build_tranche_plan_line(
            uic=486, tp_tranches=tranches, reference_qty=100.0, stop_price=13.0
        )
        out = fold_tranche_plans([line])
        got_tranches, got_ref_qty, got_stop = out[486]
        self.assertEqual(got_tranches, tranches)
        self.assertEqual(got_ref_qty, 100.0)
        self.assertEqual(got_stop, 13.0)

    def test_malformed_line_is_skipped(self) -> None:
        # Missing reference_qty/stop_price/tp_tranches entirely.
        out = fold_tranche_plans([{"kind": "tranche_plan", "uic": 486}])
        self.assertEqual(out, {})

    def test_malformed_tranche_shape_skips_the_whole_line(self) -> None:
        bad = {
            "kind": "tranche_plan",
            "uic": 486,
            "reference_qty": 100.0,
            "stop_price": 13.0,
            "tp_tranches": [{"tranche_index": 0}],  # missing target_price etc.
        }
        out = fold_tranche_plans([bad])
        self.assertEqual(out, {})

    def test_a_retraction_removes_the_uic_from_the_fold(self) -> None:
        # 2026-08-19 adjudication finding 3: a watch that ends unfired retracts
        # its plan — the fold must stop governing the uic.
        line = _build_tranche_plan_line(
            uic=486,
            tp_tranches=(_tr(0, 16.0, 1.0),),
            reference_qty=100.0,
            stop_price=13.0,
            pick_key="KO:2026-07-20",
        )
        retraction = {"kind": "tranche_plan_retracted", "uic": 486, "pick_key": "KO:2026-07-20"}
        self.assertEqual(fold_tranche_plans([line, retraction]), {})

    def test_a_plan_after_a_retraction_governs_again(self) -> None:
        old = _build_tranche_plan_line(
            uic=486, tp_tranches=(_tr(0, 16.0, 1.0),), reference_qty=100.0, stop_price=13.0
        )
        retraction = {"kind": "tranche_plan_retracted", "uic": 486}
        new = _build_tranche_plan_line(
            uic=486, tp_tranches=(_tr(0, 17.0, 1.0),), reference_qty=80.0, stop_price=14.0
        )
        out = fold_tranche_plans([old, retraction, new])
        got_tranches, _ref, _stop = out[486]
        self.assertEqual(got_tranches, (_tr(0, 17.0, 1.0),))

    def test_a_malformed_retraction_is_skipped(self) -> None:
        line = _build_tranche_plan_line(
            uic=486, tp_tranches=(_tr(0, 16.0, 1.0),), reference_qty=100.0, stop_price=13.0
        )
        out = fold_tranche_plans([line, {"kind": "tranche_plan_retracted"}])  # no uic
        self.assertIn(486, out)

    def test_newest_line_wins_for_the_same_uic(self) -> None:
        old_line = _build_tranche_plan_line(
            uic=486, tp_tranches=(_tr(0, 16.0, 0.5),), reference_qty=100.0, stop_price=13.0
        )
        new_line = _build_tranche_plan_line(
            uic=486, tp_tranches=(_tr(0, 17.0, 1.0),), reference_qty=80.0, stop_price=14.0
        )
        out = fold_tranche_plans([old_line, new_line])
        got_tranches, got_ref_qty, got_stop = out[486]
        self.assertEqual(got_tranches, (_tr(0, 17.0, 1.0),))
        self.assertEqual(got_ref_qty, 80.0)
        self.assertEqual(got_stop, 14.0)

    def test_non_tranche_plan_line_contributes_nothing(self) -> None:
        out = fold_tranche_plans([{"kind": "planned", "uic": 486, "stop_price": 13.0}])
        self.assertEqual(out, {})

    def test_distinct_uics_fold_independently(self) -> None:
        line_a = _build_tranche_plan_line(
            uic=1, tp_tranches=(_tr(0, 10.0, 1.0),), reference_qty=10.0, stop_price=8.0
        )
        line_b = _build_tranche_plan_line(
            uic=2, tp_tranches=(_tr(0, 20.0, 1.0),), reference_qty=20.0, stop_price=18.0
        )
        out = fold_tranche_plans([line_a, line_b])
        self.assertEqual(set(out.keys()), {1, 2})


if __name__ == "__main__":
    unittest.main()
