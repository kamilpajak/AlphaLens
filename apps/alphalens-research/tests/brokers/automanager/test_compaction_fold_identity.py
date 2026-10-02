"""#1327 / #1328: every reader of ``standalone_stops.jsonl`` folds the same on the
journal and on its boot-compacted form.

``_compact_standalone_stop_journal_lines`` promises "the MINIMAL set of lines that
folds IDENTICALLY". Two readers broke that promise:

- #1328: ``_fold_planned_exits`` resolved two plans at one ``tier_index`` by
  iteration order, and the compactor re-sorts planned lines by crid, so a boot
  could change which plan governs a uic.
- #1327: ``_derive_owed_sibling_retires`` reads EVERY full ``stop_filled`` with an
  entry-trail ref, but the compactor dropped the ones no other fold needed, so a
  boot forgot that a stopped-out pick still owed its sibling tiers a retire.

The randomized test at the end compares every reader of the journal on 1500
generated journals. The battery not covering a reader is how #1324 and
both of these stayed invisible.
"""

from __future__ import annotations

import json
import random
import unittest
from typing import Any

from alphalens_pipeline.brokers.automanager import control_loop as cl
from broker_contract.sizing import TpTranchePlan

_TRAIL = {"kind": "trailing_stop", "arm_trigger_r": 0.5, "trail_frac": 0.6}


def _planned(
    crid: str,
    *,
    uic: int = 111,
    stop: float = 10.0,
    tp: float | None = 20.0,
    tier: int = 0,
    side: str = "SELL",
    reaction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    line: dict[str, Any] = {
        "kind": "planned",
        "client_request_id": crid,
        "uic": uic,
        "side": side,
        "stop_price": stop,
        "take_profit": tp,
        "tier_index": tier,
        "gen": 0,
    }
    if reaction is not None:
        line["reaction"] = reaction
    return line


def _plan_view(fold: dict[int, Any]) -> dict[int, tuple[Any, ...]]:
    """Every PlannedExit field that the journal decides (the two counters are
    closures and compare by identity)."""
    return {
        uic: (
            p.entry_crid,
            p.side,
            p.stop_price,
            p.tp_price,
            p.conflicting,
            p.n_plans,
            repr(p.reaction),
        )
        for uic, p in fold.items()
    }


class TestTheGoverningPlanIsAPropertyOfTheData(unittest.TestCase):
    """#1328: the issue's three reproductions, each written in reverse crid order
    so the compactor's sort actually changes the order."""

    def _assert_compaction_keeps(self, journal: list[dict[str, Any]]) -> None:
        compacted = cl._compact_standalone_stop_journal_lines(journal)
        self.assertEqual(
            _plan_view(cl._fold_planned_exits(journal)),
            _plan_view(cl._fold_planned_exits(compacted)),
        )

    def test_two_plans_at_one_tier(self) -> None:
        self._assert_compaction_keeps(
            [_planned("crid-B", stop=11.0, tp=21.0), _planned("crid-A", stop=10.0, tp=20.0)]
        )

    def test_three_plans_at_one_tier(self) -> None:
        self._assert_compaction_keeps(
            [
                _planned("crid-C", tp=22.0),
                _planned("crid-B", tp=21.0),
                _planned("crid-A", tp=20.0),
            ]
        )

    def test_the_side_does_not_flip(self) -> None:
        self._assert_compaction_keeps(
            [_planned("crid-B", side="BUY"), _planned("crid-A", side="SELL")]
        )

    def test_the_declared_reaction_does_not_flip(self) -> None:
        # The reaction decides whether the position trails at all.
        self._assert_compaction_keeps([_planned("crid-B", reaction=_TRAIL), _planned("crid-A")])

    def test_the_fold_does_not_depend_on_line_order(self) -> None:
        journal = [
            _planned("crid-C", tp=22.0, reaction=_TRAIL),
            _planned("crid-A", tp=20.0, side="BUY"),
            _planned("crid-B", tp=21.0),
            _planned("crid-A1", tier=1, tp=19.0),
        ]
        self.assertEqual(
            _plan_view(cl._fold_planned_exits(journal)),
            _plan_view(cl._fold_planned_exits(list(reversed(journal)))),
        )

    def test_the_conflict_facts_are_unchanged(self) -> None:
        # #1249: the never-naked cover under conflict reads these three.
        fold = cl._fold_planned_exits(
            [_planned("crid-B", stop=11.0, tp=21.0), _planned("crid-A", stop=10.0, tp=20.0)]
        )
        plan = fold[111]
        self.assertTrue(plan.conflicting)
        self.assertEqual(plan.n_plans, 2)
        self.assertEqual(plan.stop_price, 11.0)


def _tranche_plan(uic: int, pick_key: str) -> dict[str, Any]:
    return cl._build_tranche_plan_line(
        uic=uic,
        tp_tranches=(
            TpTranchePlan(
                tranche_index=0, target_price=20.0, tranche_frac=1.0, r_multiple=1.0, tag="tp1"
            ),
        ),
        reference_qty=10.0,
        stop_price=9.0,
        pick_key=pick_key,
    )


class TestAStopFillThatOwesARetireSurvivesCompaction(unittest.TestCase):
    """#1327: the issue's reproduction, asserted as fold identity."""

    def test_an_orphan_entry_trail_fill_still_owes_its_pick(self) -> None:
        journal = [
            {
                "kind": "stop_filled",
                "uic": 111,
                "order_id": "orphan",
                "ts": 11.0,
                "ref": "AAA-2026-09-01-entry-t0-stop-0",
            }
        ]
        compacted = cl._compact_standalone_stop_journal_lines(journal)
        self.assertEqual(
            cl._derive_owed_sibling_retires(journal), {"AAA:2026-09-01": "stop fill on record"}
        )
        self.assertEqual(
            cl._derive_owed_sibling_retires(journal), cl._derive_owed_sibling_retires(compacted)
        )

    def test_a_kept_fill_does_not_become_closure_evidence(self) -> None:
        # The fill predates the open generation of BBB, so it must not count as
        # BBB's round-trip closure after compaction either.
        journal = [
            {
                "kind": "stop_filled",
                "uic": 111,
                "order_id": "old",
                "ts": 5.0,
                "ref": "AAA-2026-09-01-entry-t0-stop-0",
            },
            _tranche_plan(111, "BBB:2026-09-10"),
        ]
        compacted = cl._compact_standalone_stop_journal_lines(journal)
        self.assertEqual(cl._fold_round_trip_closures_since_latest_plan(journal), {})
        self.assertEqual(
            cl._fold_round_trip_closures_since_latest_plan(journal),
            cl._fold_round_trip_closures_since_latest_plan(compacted),
        )
        self.assertEqual(
            cl._derive_owed_sibling_retires(journal), cl._derive_owed_sibling_retires(compacted)
        )


# --- randomized property ---------------------------------------------------------

_UICS = (111, 222, 333)
_PICKS = {
    111: ("AAA:2026-09-01", "AAA:2026-09-02-g1"),
    222: ("BBB:2026-09-03",),
    333: ("CCC:2026-09-04", "C-D:2026-09-05"),
}
_KINDS = (
    "planned",
    "planned_retracted",
    "tranche_plan",
    "tranche_plan_retracted",
    "tranche_fired",
    "stop_placed",
    "stop_filled",
    "trailed",
    "reanchored",
    "oco_placed",
    "oco_unsupported",
    "amend_failed",
    "oco_too_far",
    "amend_seq",
    "amend_ok",
    "gen",
)
_WEIGHTS = (6, 1, 3, 1, 3, 4, 5, 2, 2, 1, 1, 1, 1, 1, 1, 1)


def _entry_stop_ref(pick_key: str, tier: int) -> str:
    ticker, rest = pick_key.split(":", 1)
    return f"{ticker}-{rest}-entry-t{tier}-stop-0"


def _random_ref(rng: random.Random, pick_key: str) -> str | None:
    roll = rng.random()
    if roll < 0.6:
        return _entry_stop_ref(pick_key, rng.randint(0, 1))
    return "bracket-xyz-stop-0" if roll < 0.8 else None


def _random_line(rng: random.Random, uic: int) -> dict[str, Any]:
    """One line in a shape a writer emits (every timestamped kind carries ``ts``)."""
    pick_key = rng.choice(_PICKS[uic])
    ts = float(rng.randrange(100, 140))
    kind = rng.choices(_KINDS, weights=_WEIGHTS)[0]
    if kind == "planned":
        line = _planned(
            f"{uic}-crid-{rng.choice('ABCD')}",
            uic=uic,
            stop=rng.choice((9.0, 10.0, 11.0)),
            tp=rng.choice((None, 20.0, 21.0, 22.0)),
            tier=rng.choice((0, 0, 1, 2)),
            side=rng.choice(("SELL", "BUY")),
            reaction=_TRAIL if rng.random() < 0.3 else None,
        )
        line["gen"] = rng.choice((0, 0, 1))
        if rng.random() < 0.5:
            line["pick_key"] = pick_key
        return line
    if kind == "planned_retracted":
        return {"kind": kind, "client_request_id": f"{uic}-crid-{rng.choice('ABCD')}", "uic": uic}
    if kind == "tranche_plan":
        return _tranche_plan(uic, pick_key) if rng.random() < 0.7 else _tranche_plan(uic, "")
    if kind == "tranche_plan_retracted":
        return {"kind": kind, "uic": uic, "pick_key": pick_key}
    if kind == "tranche_fired":
        line = {"kind": kind, "uic": uic, "tag": rng.choice(("tp1", "tp2"))}
        if rng.random() < 0.3:
            line["position_closed"] = True
        return line
    if kind == "stop_placed":
        return {
            "kind": kind,
            "uic": uic,
            "ts": ts,
            "stop_price": rng.choice((9.0, 9.5)),
            "order_id": rng.choice(("o1", "o2", "o3", "o4", "o5")),
            "ref": _random_ref(rng, pick_key),
        }
    if kind == "stop_filled":
        return {
            "kind": kind,
            "uic": uic,
            "order_id": rng.choice(("o1", "o2", "o3", "o4", "o5")),
            "qty": 5.0,
            "avg_price": 9.0,
            "ref": _random_ref(rng, pick_key),
            "partial": rng.random() < 0.25,
            "ts": ts,
        }
    if kind == "trailed":
        return {"kind": kind, "uic": uic, "level": rng.choice((12.0, 13.0)), "ts": ts}
    if kind == "reanchored":
        return {"kind": kind, "uic": uic, "avg_price": 10.0, "stop_price": 9.2, "ts": ts}
    if kind in ("oco_placed", "amend_failed", "oco_too_far", "amend_ok"):
        return {"kind": kind, "uic": uic, "ts": ts}
    if kind == "oco_unsupported":
        return {"kind": kind, "uic": uic}
    if kind == "amend_seq":
        return {"kind": kind, "uic": uic, "seq": rng.randint(0, 5)}
    return {"kind": "gen", "uic": uic, "gen": rng.randint(0, 3), "qty": 5.0}


def _random_journal(rng: random.Random) -> list[dict[str, Any]]:
    uics = rng.sample(_UICS, rng.randint(1, 3))
    return [_random_line(rng, rng.choice(uics)) for _ in range(rng.randint(5, 40))]


def _every_fold(lines: list[dict[str, Any]]) -> dict[str, Any]:
    """Every reader of the standalone journal the compactor promises to preserve.

    Two are left out on purpose: ``gen`` markers (the documented exception) and
    ``_latest_stop_move`` (the stop-fill alert text; it can read a ``trailed``
    marker the compactor drops — #1669, which removes this exclusion)."""
    out: dict[str, Any] = {
        "planned": _plan_view(cl._fold_planned_exits(lines)),
        "oco_unsupported": cl._fold_oco_unsupported(lines),
        "reanchored": cl._fold_reanchored_markers(lines),
        "reanchored_levels": cl._fold_reanchored_stop_levels(lines),
        "trailed": cl._fold_trailed_since_latest_plan(lines),
        "tranche_plans": cl.fold_tranche_plans(lines),
        "tranche_currencies": cl.fold_tranche_plan_currencies(lines),
        "fired": cl._fold_fired_since_latest_plan(lines),
        "governing_keys": cl._fold_governing_plan_pick_keys(lines),
        "closures": cl._fold_round_trip_closures_since_latest_plan(lines),
        "standing": cl._fold_standing_stop_ids(lines),
        "owed": cl._derive_owed_sibling_retires(lines),
        "moving": cl._uics_moving_their_stop(lines),
    }
    for kind in ("oco_placed", "amend_failed", "oco_too_far"):
        for now in (110.0, 125.0, 150.0):
            out[f"ttl:{kind}:{now}"] = cl._fold_ttl_markers(lines, kind, now, 10.0)
    return out


class TestCompactionPreservesEveryReader(unittest.TestCase):
    """A seeded property check. A failure prints the journal so the
    counterexample can become a fixed test."""

    _TRIALS = 1500
    _SEED = 1327

    def test_every_reader_folds_the_same_and_compaction_is_idempotent(self) -> None:
        rng = random.Random(self._SEED)
        for trial in range(self._TRIALS):
            journal = _random_journal(rng)
            compacted = cl._compact_standalone_stop_journal_lines(journal)
            twice = cl._compact_standalone_stop_journal_lines(compacted)
            original_folds = _every_fold(journal)
            compacted_folds = _every_fold(compacted)
            for name, value in original_folds.items():
                if value != compacted_folds[name]:
                    self.fail(
                        f"trial {trial}: {name} differs after compaction\n"
                        f"  original : {value}\n  compacted: {compacted_folds[name]}\n"
                        + "\n".join(json.dumps(line, sort_keys=True) for line in journal)
                    )
            self.assertEqual(compacted_folds, _every_fold(twice), f"trial {trial}: not idempotent")


if __name__ == "__main__":
    unittest.main()
