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
from alphalens_pipeline.brokers.automanager import stop_journal as sj
from alphalens_pipeline.brokers.automanager import trades
from broker_contract.contract import InstrumentRef, Position
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
            _plan_view(sj._fold_planned_exits(journal)),
            _plan_view(sj._fold_planned_exits(compacted)),
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
            _plan_view(sj._fold_planned_exits(journal)),
            _plan_view(sj._fold_planned_exits(list(reversed(journal)))),
        )

    def test_the_conflict_facts_are_unchanged(self) -> None:
        # #1249: the never-naked cover under conflict reads these three.
        fold = sj._fold_planned_exits(
            [_planned("crid-B", stop=11.0, tp=21.0), _planned("crid-A", stop=10.0, tp=20.0)]
        )
        plan = fold[111]
        self.assertTrue(plan.conflicting)
        self.assertEqual(plan.n_plans, 2)
        self.assertEqual(plan.stop_price, 11.0)


def _tranche_plan(uic: int, pick_key: str) -> dict[str, Any]:
    return sj._build_tranche_plan_line(
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
        self.assertEqual(sj._fold_round_trip_closures_since_latest_plan(journal), {})
        self.assertEqual(
            sj._fold_round_trip_closures_since_latest_plan(journal),
            sj._fold_round_trip_closures_since_latest_plan(compacted),
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


def _newest_stop_placed_ts(lines: list[dict[str, Any]]) -> dict[int, float]:
    """Per uic, the ``ts`` of the newest ``stop_placed`` (a later line breaks a
    tie): the one stop the compactor keeps for the uic."""
    newest: dict[int, float] = {}
    for line in lines:
        if line.get("kind") != "stop_placed":
            continue
        uic = sj._coerce(line, "uic", int)
        ts = sj._coerce(line, "ts", float)
        if uic is not None and ts is not None and (uic not in newest or ts >= newest[uic]):
            newest[uic] = ts
    return newest


def _stop_moves(lines: list[dict[str, Any]]) -> dict[str, dict[int, Any]]:
    """What the stop-fill alert would name (#1669): the move of the newest
    ``stop_placed`` per uic, and the move of every standing stop the reconcile
    pass watches."""
    return {
        "stop_move": {
            uic: cl._latest_stop_move(lines, uic, since_ts=ts)
            for uic, ts in _newest_stop_placed_ts(lines).items()
        },
        "standing_stop_move": {
            uic: cl._latest_stop_move(lines, uic, since_ts=stop.placed_ts)
            for uic, stop in cl._fold_standing_stop_ids(lines).items()
        },
    }


def _every_fold(lines: list[dict[str, Any]]) -> dict[str, Any]:
    """Every reader of the standalone journal the compactor promises to preserve.

    One is left out on purpose: ``gen`` markers (the documented exception)."""
    out: dict[str, Any] = {
        "planned": _plan_view(sj._fold_planned_exits(lines)),
        "oco_unsupported": cl._fold_oco_unsupported(lines),
        "reanchored": cl._fold_reanchored_markers(lines),
        "reanchored_levels": cl._fold_reanchored_stop_levels(lines),
        "trailed": cl._fold_trailed_since_latest_plan(lines),
        "tranche_plans": sj.fold_tranche_plans(lines),
        "tranche_currencies": cl.fold_tranche_plan_currencies(lines),
        "fired": sj._fold_fired_since_latest_plan(lines),
        "governing_keys": sj._fold_governing_plan_pick_keys(lines),
        "closures": sj._fold_round_trip_closures_since_latest_plan(lines),
        "standing": cl._fold_standing_stop_ids(lines),
        "owed": cl._derive_owed_sibling_retires(lines),
        "moving": cl._uics_moving_their_stop(lines),
        **_stop_moves(lines),
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


# --- #1669: the stop's newest move survives compaction -----------------------------

_U = 333
_PLACED_TS = 102.0
_KEY_A = "C-D:2026-09-15"
_KEY_B = "OTHER:2026-10-01"
_CRID_A = f"{_U}-crid-A"
_CRID_B = f"{_U}-crid-B"


def _stop_placed(ts: float = _PLACED_TS, order_id: str = "O1") -> dict[str, Any]:
    return {
        "kind": "stop_placed",
        "uic": _U,
        "qty": 5.0,
        "order_id": order_id,
        "ref": "C-D-2026-09-15-entry-t0-stop-0",
        "stop_price": 9.0,
        "ts": ts,
    }


def _trailed(level: float, ts: float) -> dict[str, Any]:
    return {"kind": "trailed", "uic": _U, "level": level, "ts": ts, "peak": level + 2.0}


def _reanchored(stop: float, ts: float) -> dict[str, Any]:
    return {"kind": "reanchored", "uic": _U, "avg_price": 10.0, "stop_price": stop, "ts": ts}


def _keyed_planned(crid: str = _CRID_A, key: str | None = _KEY_A) -> dict[str, Any]:
    line = _planned(crid, uic=_U, stop=9.0, tp=None)
    if key is not None:
        line["pick_key"] = key
    return line


def _is_closer(line: dict[str, Any]) -> bool:
    """The compactor's crid-less ``planned_retracted`` line."""
    return line.get("kind") == "planned_retracted" and "client_request_id" not in line


def _dumps(lines: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(line, sort_keys=True) + "\n" for line in lines)


class TestTheStopMoveSurvivesCompaction(unittest.TestCase):
    """#1669: the stop-fill alert names the stop's newest move. A ``trailed``
    marker whose plan generation closed was dropped at boot, so after a restart
    the alert (and ``broker trades``) lost the move or named an older one."""

    def _assert_keeps_the_move(self, journal: list[dict[str, Any]], expected: Any) -> None:
        compacted = cl._compact_standalone_stop_journal_lines(journal)
        self.assertEqual(cl._latest_stop_move(journal, _U, since_ts=_PLACED_TS), expected)
        self.assertEqual(cl._latest_stop_move(compacted, _U, since_ts=_PLACED_TS), expected)
        self.assertEqual(_every_fold(journal), _every_fold(compacted))
        twice = cl._compact_standalone_stop_journal_lines(compacted)
        self.assertEqual(_every_fold(compacted), _every_fold(twice))
        self.assertEqual(_dumps(compacted), _dumps(twice), "compaction is not idempotent")

    def test_issue_smallest_case_keeps_trailed_13(self) -> None:
        journal = [_stop_placed(), _trailed(13.0, 104.0), _keyed_planned(key=None)]
        self._assert_keeps_the_move(journal, sj._StopMove("trailed", 13.0))

    def test_every_generation_closer_keeps_the_move_and_no_floor(self) -> None:
        closers = {
            "planned without a key": _keyed_planned(_CRID_B, key=None),
            "planned with another key": _keyed_planned(_CRID_B, key=_KEY_B),
            "tranche_plan with another key": _tranche_plan(_U, _KEY_B),
            "tranche_plan_retracted": {
                "kind": "tranche_plan_retracted",
                "uic": _U,
                "pick_key": _KEY_A,
            },
            "planned_retracted": {
                "kind": "planned_retracted",
                "client_request_id": _CRID_A,
                "uic": _U,
                "note": "closed",
            },
        }
        for name, closer in closers.items():
            with self.subTest(closer=name):
                journal = [_keyed_planned(), _stop_placed(), _trailed(13.0, 104.0), closer]
                self._assert_keeps_the_move(journal, sj._StopMove("trailed", 13.0))
                compacted = cl._compact_standalone_stop_journal_lines(journal)
                self.assertEqual(cl._fold_trailed_since_latest_plan(journal), {})
                self.assertEqual(cl._fold_trailed_since_latest_plan(compacted), {})

    def test_kept_marker_cannot_resurrect_a_ratchet_floor(self) -> None:
        # The VST shape: the stop trailed twice and filled, then both
        # retractions closed the pick. No plan line is left for the uic, so
        # nothing after the kept marker would reset the trailed fold (#1324).
        journal = [
            _keyed_planned(),
            _tranche_plan(_U, _KEY_A),
            _stop_placed(),
            _trailed(12.0, 104.0),
            _trailed(13.0, 106.0),
            {
                "kind": "stop_filled",
                "uic": _U,
                "order_id": "O1",
                "qty": 5.0,
                "avg_price": 12.9,
                "ref": "C-D-2026-09-15-entry-t0-stop-0",
                "partial": False,
                "ts": 108.0,
            },
            {"kind": "tranche_plan_retracted", "uic": _U, "pick_key": _KEY_A},
            {"kind": "planned_retracted", "client_request_id": _CRID_A, "uic": _U, "note": "x"},
        ]
        compacted = cl._compact_standalone_stop_journal_lines(journal)
        self._assert_keeps_the_move(journal, sj._StopMove("trailed", 13.0))
        self.assertEqual(cl._fold_trailed_since_latest_plan(journal), {})
        self.assertEqual(cl._fold_trailed_since_latest_plan(compacted), {})
        self.assertEqual(
            cl._fold_reanchored_stop_levels(journal), cl._fold_reanchored_stop_levels(compacted)
        )
        self.assertEqual(
            cl._fold_reanchored_markers(journal), cl._fold_reanchored_markers(compacted)
        )
        # A new position on the uic, armed after the boot, is managed from its
        # own plan stop on both files: the old 13.0 does not leak into it.
        new_pick = [_keyed_planned(_CRID_B, key=_KEY_B), _tranche_plan(_U, _KEY_B)]
        managed = [self._managed(lines + new_pick) for lines in (journal, compacted)]
        self.assertEqual(managed[0], managed[1])
        self.assertEqual([exit_.stop_price for exit_ in managed[1]], [9.0])

    @staticmethod
    def _managed(lines: list[dict[str, Any]]) -> list[Any]:
        position = Position(
            instrument=InstrumentRef(
                ticker="CD",
                exchange_mic="XNYS",
                asset_type="Stock",
                broker_instrument_id=str(_U),
                broker_symbol="CD:xnys",
            ),
            quantity=10.0,
            avg_price=10.0,
            market_value=None,
            unrealized_pnl=None,
            position_id="pos-1",
        )
        return cl._build_managed_exits(
            long_positions=[position],
            tranche_plans=sj.fold_tranche_plans(lines),
            fired=sj._fold_fired_since_latest_plan(lines),
            trailed=cl._fold_trailed_since_latest_plan(lines),
            reanchored=cl._fold_reanchored_stop_levels(lines),
        )

    def test_newer_trailed_beats_kept_reanchored(self) -> None:
        journal = [
            _stop_placed(),
            _reanchored(9.5, 103.0),
            _trailed(13.0, 104.0),
            _keyed_planned(key=None),
        ]
        self._assert_keeps_the_move(journal, sj._StopMove("trailed", 13.0))

    def test_a_kept_trailed_tied_with_a_later_reanchor_gives_the_reanchor(self) -> None:
        journal = [
            _keyed_planned(),
            _tranche_plan(_U, _KEY_A),
            _stop_placed(),
            _trailed(13.0, 104.0),
            _reanchored(9.5, 104.0),
        ]
        self._assert_keeps_the_move(journal, sj._StopMove("reanchored", 9.5))

    def test_a_dropped_trailed_tied_after_a_reanchor_gives_the_trail(self) -> None:
        journal = [
            _stop_placed(),
            _reanchored(9.5, 104.0),
            _trailed(13.0, 104.0),
            _keyed_planned(key=None),
        ]
        self._assert_keeps_the_move(journal, sj._StopMove("trailed", 13.0))

    def test_marker_older_than_kept_stop_is_not_written(self) -> None:
        # The alert never reads a marker older than the stop it names, so the
        # compactor does not keep one: the output stays minimal.
        journal = [_trailed(12.0, 100.0), _stop_placed(), _keyed_planned(key=None)]
        compacted = cl._compact_standalone_stop_journal_lines(journal)
        self.assertEqual(
            [line["kind"] for line in compacted], ["planned", "stop_placed"], compacted
        )
        self.assertEqual(_every_fold(journal), _every_fold(compacted))

    def test_a_reanchored_winner_is_written_once(self) -> None:
        # The kept reanchored line IS the stop's newest move here, so it goes in
        # the tail block only: the output stays minimal.
        journal = [_stop_placed(), _reanchored(9.5, 104.0)]
        compacted = cl._compact_standalone_stop_journal_lines(journal)
        self.assertEqual([line for line in compacted if line["kind"] == "reanchored"], [journal[1]])
        self.assertEqual(_every_fold(journal), _every_fold(compacted))

    def test_open_plan_trailed_level_is_still_the_live_floor(self) -> None:
        journal = [
            _keyed_planned(),
            _tranche_plan(_U, _KEY_A),
            _stop_placed(),
            _trailed(13.0, 104.0),
        ]
        compacted = cl._compact_standalone_stop_journal_lines(journal)
        self.assertEqual(cl._fold_trailed_since_latest_plan(compacted), {_U: 13.0})
        self.assertEqual(
            cl._latest_stop_move(compacted, _U, since_ts=_PLACED_TS),
            sj._StopMove("trailed", 13.0),
        )
        self.assertEqual([line for line in compacted if line["kind"] == "trailed"], [journal[3]])
        self.assertFalse(any(_is_closer(line) for line in compacted), compacted)


class TestTheCloserIsInvisibleToEveryOtherReader(unittest.TestCase):
    """The compactor hides a kept ``trailed`` marker from the trailed fold with a
    ``planned_retracted`` line that names no crid. Only the trailed selection may
    react to it: a reader that took it for a real retraction would retract
    nothing today, and something tomorrow."""

    _TRIALS = 300
    _SEED = 1669

    def test_closer_is_invisible_to_every_other_reader(self) -> None:
        rng = random.Random(self._SEED)
        for trial in range(self._TRIALS):
            journal = _random_journal(rng)
            uic = rng.choice(_UICS)
            closer = cl._stop_move_closer(uic)
            for where, with_closer in (
                ("front", [closer, *journal]),
                ("end", [*journal, closer]),
            ):
                before, after = _every_fold(journal), _every_fold(with_closer)
                before.pop("trailed")
                after.pop("trailed")
                self.assertEqual(before, after, f"trial {trial}, closer at the {where}")
                self.assertEqual(
                    sj._latest_planned_by_crid(journal), sj._latest_planned_by_crid(with_closer)
                )
                self.assertEqual(
                    trades._read_stop_facts(journal), trades._read_stop_facts(with_closer)
                )

    def test_the_closer_names_no_crid(self) -> None:
        # The line proven invisible above is the one the compactor writes: a
        # crid would let ``_latest_planned_by_crid`` retract a kept plan line.
        self.assertTrue(_is_closer(cl._stop_move_closer(_U)), cl._stop_move_closer(_U))


# --- #1669: repeated boots --------------------------------------------------------

_LIFE_PICKS = {111: "AAA", 222: "BBB", 333: "C-D"}


class _Lifecycle:
    """A journal written the way the daemon writes it, with boots between appends:
    a monotone clock (5% exact ties), unique order ids, and each pick going armed
    -> stop placed -> moved -> filled or retracted. Events are ``("line", line,
    fill)`` (``fill`` = ``(uic, placed_ts)`` on a ``stop_filled``) or
    ``("boot",)``."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.clock = 1000.0
        self.order_seq = 0
        self.pick_seq = 0
        self.events: list[tuple[Any, ...]] = []

    def _ts(self, *, tie_ok: bool = True) -> float:
        if not (tie_ok and self.rng.random() < 0.05):
            self.clock += round(self.rng.uniform(0.1, 3.0), 3)
        return self.clock

    def _emit(self, line: dict[str, Any], fill: tuple[int, float] | None = None) -> None:
        self.events.append(("line", line, fill))

    def build(self) -> list[tuple[Any, ...]]:
        rng = self.rng
        uics = rng.sample(sorted(_LIFE_PICKS), rng.randint(1, 3))
        state: dict[int, dict[str, Any]] = {
            u: {"pick": None, "crids": [], "tranche": None, "order": None, "level": 9.0}
            for u in uics
        }
        target = rng.randint(5, 40)
        while sum(1 for e in self.events if e[0] == "line") < target:
            if self.events and rng.random() < 0.12:
                self.events.append(("boot",))
            uic = rng.choice(uics)
            if rng.random() < 0.06:
                self._bookkeeping(uic)
            elif state[uic]["pick"] is None:
                self._arm(uic, state[uic])
            elif state[uic]["order"] is None:
                self._place_or_retract(uic, state[uic])
            else:
                self._standing(uic, state[uic])
        if rng.random() < 0.7:
            self.events.append(("boot",))
        return self.events

    def _bookkeeping(self, uic: int) -> None:
        kind = self.rng.choice(
            ("oco_placed", "amend_failed", "oco_too_far", "amend_seq", "gen", "oco_unsupported")
        )
        if kind == "amend_seq":
            self._emit({"kind": kind, "uic": uic, "seq": self.rng.randint(0, 5)})
        elif kind == "gen":
            self._emit({"kind": kind, "uic": uic, "gen": self.rng.randint(0, 3), "qty": 5.0})
        elif kind == "oco_unsupported":
            self._emit({"kind": kind, "uic": uic})
        else:
            self._emit({"kind": kind, "uic": uic, "ts": self._ts()})

    def _arm(self, uic: int, s: dict[str, Any]) -> None:
        rng = self.rng
        self.pick_seq += 1
        day = 10 + self.pick_seq
        key = f"{_LIFE_PICKS[uic]}:2026-09-{day:02d}"
        keyed = rng.random() < 0.9
        s["pick"], s["crids"], s["tranche"] = key, [], None
        for tier in range(rng.choice((1, 1, 2))):
            crid = f"{_LIFE_PICKS[uic]}-2026-09-{day:02d}-entry-t{tier}-fire"
            line = _planned(crid, uic=uic, stop=9.0, tp=rng.choice((None, 20.0)), tier=tier)
            if keyed:
                line["pick_key"] = key
            s["crids"].append(crid)
            self._emit(line)
        roll = rng.random()
        if roll < 0.5:
            self._emit(_tranche_plan(uic, key))
            s["tranche"] = key
        elif roll < 0.6:
            self._emit(_tranche_plan(uic, ""))
            s["tranche"] = ""

    def _retract(self, uic: int, s: dict[str, Any]) -> None:
        if s["tranche"]:
            self._emit({"kind": "tranche_plan_retracted", "uic": uic, "pick_key": s["pick"]})
        for crid in s["crids"]:
            self._emit(
                {"kind": "planned_retracted", "client_request_id": crid, "uic": uic, "note": "x"}
            )

    def _stop_ref(self, s: dict[str, Any]) -> str:
        return f"{s['pick'].replace(':', '-')}-entry-t0-stop-0"

    def _place(self, uic: int, s: dict[str, Any]) -> None:
        self.order_seq += 1
        s["order"] = f"O{self.order_seq}"
        s["placed_ts"] = self._ts()
        line = {
            "kind": "stop_placed",
            "uic": uic,
            "qty": 5.0,
            "order_id": s["order"],
            "ref": self._stop_ref(s),
            "stop_price": s["level"],
            "ts": s["placed_ts"],
        }
        self._emit(line)

    def _place_or_retract(self, uic: int, s: dict[str, Any]) -> None:
        if self.rng.random() < 0.15:  # the watch ended with no fill
            self._retract(uic, s)
            s["pick"] = None
            return
        s["level"] = 9.0
        self._place(uic, s)

    def _standing(self, uic: int, s: dict[str, Any]) -> None:
        rng = self.rng
        roll = rng.random()
        if roll < 0.30:
            s["level"] = round(s["level"] + rng.choice((0.5, 1.0)), 3)
            ts = self._ts()
            self._emit(
                {
                    "kind": "trailed",
                    "uic": uic,
                    "level": s["level"],
                    "ts": ts,
                    "peak": s["level"] + 2,
                }
            )
        elif roll < 0.38:
            s["level"] = round(s["level"] + 0.25, 3)
            self._emit(
                {
                    "kind": "reanchored",
                    "uic": uic,
                    "avg_price": 10.0,
                    "stop_price": s["level"],
                    "ts": self._ts(),
                }
            )
        elif roll < 0.45:
            self._emit({"kind": "amend_ok", "uic": uic, "qty": 5.0, "ts": self._ts()})
        elif roll < 0.50:  # the stop is replaced: a new order id at the current level
            self._place(uic, s)
        elif roll < 0.55:  # a crash re-drive: the SAME key re-appended
            if s["tranche"]:
                self._emit(_tranche_plan(uic, s["tranche"]))
            else:
                line = _planned(s["crids"][0], uic=uic, stop=9.0, tp=None)
                line["gen"] = 1
                line["pick_key"] = s["pick"]
                self._emit(line)
        elif roll < 0.58:  # #1669's hazard: a keyless or other-key plan line mid-position
            self._foreign_plan_line(uic)
        elif roll < 0.62:
            self._emit({"kind": "tranche_fired", "uic": uic, "tag": "tp1"})
        else:
            self._fill(uic, s)

    def _foreign_plan_line(self, uic: int) -> None:
        rng = self.rng
        roll = rng.random()
        if roll < 0.4:
            self._emit(_tranche_plan(uic, ""))
        elif roll < 0.7:
            self._emit(_planned(f"bracket-{rng.randint(0, 99)}", uic=uic, stop=9.0, tp=20.0))
        else:
            line = _planned(f"OTHER-{rng.randint(0, 99)}-entry-t0-fire", uic=uic, stop=9.0, tp=None)
            line["pick_key"] = f"OTHER:2026-10-{rng.randint(1, 9):02d}"
            self._emit(line)

    def _fill(self, uic: int, s: dict[str, Any]) -> None:
        rng = self.rng
        partial = rng.random() < 0.1
        line = {
            "kind": "stop_filled",
            "uic": uic,
            "order_id": s["order"],
            "qty": 5.0,
            "avg_price": s["level"],
            "ref": self._stop_ref(s),
            "partial": partial,
            "ts": self._ts(tie_ok=False),
        }
        self._emit(line, fill=(uic, s["placed_ts"]))
        s["order"] = None  # a partial terminal cancels the remainder: a new stop follows
        if partial:
            return
        if rng.random() < 0.8:
            if s["tranche"] is not None and rng.random() < 0.9:
                self._emit({"kind": "tranche_plan_retracted", "uic": uic, "pick_key": s["pick"]})
            for crid in s["crids"]:
                self._emit(
                    {
                        "kind": "planned_retracted",
                        "client_request_id": crid,
                        "uic": uic,
                        "note": "closed",
                    }
                )
        s["pick"] = None


def _trailed_generation(lines: list[dict[str, Any]]) -> dict[int, tuple[str, str | None]]:
    """Per uic, the kind of the last line that opened or closed the trailed
    selection's plan generation, and the pick key governing it afterwards."""
    governing: dict[int, str] = {}
    last_kind: dict[int, str] = {}
    for line in lines:
        uic = sj._coerce(line, "uic", int)
        if uic is None:
            continue
        if sj._apply_generation_reset(
            line.get("kind"), line, uic, governing, (), include_planned=True
        ):
            last_kind[uic] = str(line.get("kind"))
    return {uic: (kind, governing.get(uic)) for uic, kind in last_kind.items()}


_PLAN_KINDS = ("planned", sj._TRANCHE_PLAN_KIND)


def _is_plan_line_reorder(full: list[dict[str, Any]], live: list[dict[str, Any]], uic: int) -> bool:
    """The KNOWN pre-existing shape this test excludes, by name.

    The compactor writes the ``planned`` lines sorted by crid, all of them
    before every ``tranche_plan`` line. When the uic's last plan line in the
    full journal is not the one the compacted file ends on (a keyless bracket
    ``planned`` sorted after a keyed one, or a ``planned`` line with another key
    followed by a still-kept ``tranche_plan``), the governing key changes at the
    boot, and a later same-key re-append resets one file and not the other.
    Present before #1669 and not caused by it; tracked separately. A closer line
    is a retraction, never a plan line, so it can never be excused by this."""
    full_kind, full_key = _trailed_generation(full).get(uic, (None, None))
    live_kind, live_key = _trailed_generation(live).get(uic, (None, None))
    return full_kind in _PLAN_KINDS and live_kind in _PLAN_KINDS and full_key != live_key


def _boot_view(lines: list[dict[str, Any]], tainted: set[int]) -> dict[str, Any]:
    """Every fold, with the uics tainted by the excluded shape removed from the
    trailed fold, the one that shape moves."""
    view = _every_fold(lines)
    view["trailed"] = {uic: level for uic, level in view["trailed"].items() if uic not in tainted}
    return view


class TestCompactionAcrossRepeatedBoots(unittest.TestCase):
    """A single compaction cannot see a defect that needs two boots: the closer
    line of #1669's first design cleared a governing key that a kept ``planned``
    line had set, and only a re-append AFTER the boot showed it. This replays
    generated lifecycles with boots in between, and compares the full history
    with the live file after every boot (every fold) and at every fill (the
    alert)."""

    _TRIALS = 1500
    _SEED = 1669

    def test_every_reader_agrees_with_the_full_history_after_every_boot(self) -> None:
        rng = random.Random(self._SEED)
        for trial in range(self._TRIALS):
            self._replay(trial, _Lifecycle(rng).build())

    def _replay(self, trial: int, events: list[tuple[Any, ...]]) -> None:
        full: list[dict[str, Any]] = []
        live: list[dict[str, Any]] = []
        tainted: set[int] = set()
        for event in events:
            if event[0] == "boot":
                live = cl._compact_standalone_stop_journal_lines(live)
                tainted |= {uic for uic in _LIFE_PICKS if _is_plan_line_reorder(full, live, uic)}
                expected, actual = _boot_view(full, tainted), _boot_view(live, tainted)
                for name, value in expected.items():
                    if value != actual[name]:
                        self.fail(
                            f"trial {trial}: {name} differs after a boot\n"
                            f"  full: {value}\n  live: {actual[name]}\n"
                            + "\n".join(
                                "BOOT" if e[0] == "boot" else json.dumps(e[1], sort_keys=True)
                                for e in events
                            )
                        )
                continue
            _, line, fill = event
            if fill is not None:
                uic, placed_ts = fill
                self.assertEqual(
                    cl._latest_stop_move(full, uic, since_ts=placed_ts),
                    cl._latest_stop_move(live, uic, since_ts=placed_ts),
                    f"trial {trial}: the alert at the fill of uic {uic} differs",
                )
            full.append(line)
            live.append(line)

    def test_closer_does_not_clear_a_kept_plan_governing_key(self) -> None:
        # adv/case_gov_key: another pick's plan line arrives while the stop
        # stands; the boot keeps that planned line and drops the trail. If the
        # closer were written AFTER the planned block it would clear the key
        # that line set, and the same-key re-append after the boot would reset
        # the live file but not the full history: the 11.5 floor would be lost.
        other = _keyed_planned("C-D-2026-09-15-entry-t0-fire", key=_KEY_A)
        before_boot = [_stop_placed(ts=1001.1), _trailed(9.5, 1005.7), other]
        re_append = dict(other, gen=1)
        after_boot = [_trailed(11.5, 1013.8), re_append]
        full = before_boot + after_boot
        live = cl._compact_standalone_stop_journal_lines(before_boot) + after_boot
        self.assertEqual(cl._fold_trailed_since_latest_plan(full), {_U: 11.5})
        self.assertEqual(cl._fold_trailed_since_latest_plan(live), {_U: 11.5})
        self.assertEqual(
            cl._latest_stop_move(live, _U, since_ts=1001.1), sj._StopMove("trailed", 11.5)
        )


# --- #895: the growth bound ------------------------------------------------------

_BOUND_UIC = 777
_BOUND_PICK_A = "AAA:2026-09-01"
_BOUND_PICK_B = "BBB:2026-09-02"


def _every_keep_reason_journal() -> list[dict[str, Any]]:
    """One uic shaped so EVERY keep reason of the standalone compactor fires at
    once — the shape that REACHES the bound.

    The two ``trailed`` lines differ in ts and in file order on purpose: the
    ts=130 line is the stop's newest move (``select_stop_move_lines`` is
    time-scoped) while the plan line between them resets the trailed fold, so
    the ELECTED trailed line is the ts=120 one and the move has to be kept
    separately with its closer (#1669)."""
    ladder = _tranche_plan(_BOUND_UIC, _BOUND_PICK_B)
    return [
        # The fill matching the kept stop's order id. Written before any plan
        # line (so it is not closure evidence) with a ref that names no pick
        # (so it owes no sibling retire): this slot and nothing else.
        {
            "kind": "stop_filled",
            "uic": _BOUND_UIC,
            "order_id": "O1",
            "qty": 5.0,
            "avg_price": 9.0,
            "ref": "bracket-xyz-stop-0",
            "partial": False,
            "ts": 60.0,
        },
        # The owed-sibling-retire fill of pick A (#1327), also pre-plan.
        {
            "kind": "stop_filled",
            "uic": _BOUND_UIC,
            "order_id": "O2",
            "qty": 5.0,
            "avg_price": 9.0,
            "ref": _entry_stop_ref(_BOUND_PICK_A, 0),
            "partial": False,
            "ts": 61.0,
        },
        {"kind": "oco_unsupported", "uic": _BOUND_UIC},
        {"kind": "oco_placed", "uic": _BOUND_UIC, "ts": 70.0},
        {"kind": "amend_failed", "uic": _BOUND_UIC, "ts": 71.0},
        {"kind": "oco_too_far", "uic": _BOUND_UIC, "ts": 72.0},
        {"kind": "amend_ok", "uic": _BOUND_UIC, "qty": 5.0, "ts": 73.0},
        {"kind": "amend_seq", "uic": _BOUND_UIC, "seq": 4},
        {
            "kind": "reanchored",
            "uic": _BOUND_UIC,
            "avg_price": 10.0,
            "stop_price": 9.2,
            "ts": 115.0,
        },
        _planned(f"{_BOUND_UIC}-crid-A", uic=_BOUND_UIC, stop=9.0, tp=20.0),
        {
            "kind": "stop_placed",
            "uic": _BOUND_UIC,
            "qty": 5.0,
            "order_id": "O1",
            "ref": _entry_stop_ref(_BOUND_PICK_A, 0),
            "stop_price": 9.0,
            "ts": 100.0,
        },
        {"kind": "trailed", "uic": _BOUND_UIC, "level": 12.0, "peak": 14.0, "ts": 130.0},
        ladder,
        {"kind": "trailed", "uic": _BOUND_UIC, "level": 11.0, "peak": 13.0, "ts": 120.0},
        # A ladder-malformed re-append of the SAME pick key: no generation
        # reset, and a second kept plan line (it still carries the governing
        # key the retraction sweep reads).
        dict(ladder, tp_tranches="junk"),
        {"kind": "tranche_fired", "uic": _BOUND_UIC, "tag": "tp1"},
        # Closure evidence inside the open generation (#1223), whose ref also
        # makes it pick B's owed fill — one line, both reasons.
        {
            "kind": "stop_filled",
            "uic": _BOUND_UIC,
            "order_id": "O9",
            "qty": 5.0,
            "avg_price": 9.0,
            "ref": _entry_stop_ref(_BOUND_PICK_B, 0),
            "partial": False,
            "ts": 200.0,
        },
    ]


def _more_of_every_kind(
    journal: list[dict[str, Any]], copies: int, skip: str = ""
) -> list[dict[str, Any]]:
    """``journal`` with ``copies`` further rounds of every line prepended, each
    timestamped line carrying a FRESH ts and the same keys (crid, pick keys,
    tag, order ids) — more journal, no new keys. ``skip`` leaves one kind out."""
    grown: list[dict[str, Any]] = []
    ts = 300.0
    for _ in range(copies):
        for line in journal:
            if line["kind"] == skip:
                continue
            copy = dict(line)
            if "ts" in copy:
                ts += 1.0
                copy["ts"] = ts
            grown.append(copy)
    return grown + journal


class TestTheKeptSetGrowsWithKEYSNotWithTheJOURNAL(unittest.TestCase):
    """#895 is a growth bug, and the compactor had no stated bound and no test
    pinning one — unlike its sibling (``entry_trails.COMPACTED_LINES_PER_CRID_BOUND``,
    raised twice under a test that asserts the worst case EQUALS it).

    The bound is per uic, and it has two parts: the fixed slots
    (``cl.COMPACTED_FIXED_LINES_PER_UIC_BOUND``, one per keep reason) and terms
    that grow with the input's KEYS — one ``planned`` per client_request_id,
    one ``stop_filled`` per stopped-out pick key and per closure ref key, and
    one ``tranche_fired`` per fired line still counting."""

    _EXPECTED_KINDS = [
        "reanchored",  # 1 elected marker, front block
        "trailed",  # 2 the stop's newest move (#1669)
        "planned_retracted",  # 3 its closer (#1669)
        "planned",  # + one per client_request_id
        "oco_unsupported",  # 4
        "oco_placed",  # 5 newest
        "amend_failed",  # 6 newest
        "oco_too_far",  # 7 newest
        "stop_placed",  # 8 newest
        "stop_filled",  # 9 the fill matching the kept stop's order id
        "stop_filled",  # + one per stopped-out pick key (A)
        "amend_ok",  # 10 newest
        "amend_seq",  # 11 max seq
        "tranche_plan",  # 12 the governing ladder
        "tranche_plan",  # 13 the latest plan line, distinct
        "tranche_fired",  # + one per fired line still counting
        "stop_filled",  # + closure evidence, pick B's owed fill too
        "trailed",  # 14 the elected ratchet floor
    ]
    # The non-fixed slots in the shape above: one planned crid, one fired line,
    # two owed pick keys (A, and B on the closure line).
    _KEYED_SLOTS = 4

    def test_the_fixed_bound_is_REACHED_by_the_every_reason_shape(self) -> None:
        journal = _every_keep_reason_journal()
        kept = cl._compact_standalone_stop_journal_lines(journal)
        self.assertEqual([line["kind"] for line in kept], self._EXPECTED_KINDS)
        self.assertEqual(
            len(kept) - self._KEYED_SLOTS,
            cl.COMPACTED_FIXED_LINES_PER_UIC_BOUND,
            "the bound must be reached from below: a bound nobody reaches is not measured",
        )
        self.assertEqual(_every_fold(journal), _every_fold(kept), "and it still folds the same")

    def test_twenty_times_the_journal_keeps_the_same_number_of_lines(self) -> None:
        # Every kind but tranche_fired: the keep set is decided by the keys, so
        # more lines under the same keys cannot grow it.
        journal = _every_keep_reason_journal()
        counts = [
            len(
                cl._compact_standalone_stop_journal_lines(
                    _more_of_every_kind(journal, copies, skip="tranche_fired")
                )
            )
            for copies in (1, 5, 20)
        ]
        self.assertEqual(counts, [counts[0]] * 3, "no growth in the journal's length")

    def test_the_fired_slot_is_the_one_term_that_grows_per_LINE(self) -> None:
        # Measured, and stated rather than hidden: the fired election keeps
        # every tranche_fired line inside the fold's accumulator, while the
        # fold itself only needs the TAG. On the LIVE journal that term was 4
        # lines in the whole history on 2026-11-03, so it is a documented shape
        # and not a growth path anyone has seen.
        journal = _every_keep_reason_journal()
        for copies in (1, 5, 20):
            grown = _more_of_every_kind(journal, copies)
            kept = cl._compact_standalone_stop_journal_lines(grown)
            fired = [line for line in kept if line["kind"] == "tranche_fired"]
            self.assertEqual(len(fired), copies + 1, f"{copies} extra rounds")
            self.assertEqual(
                sj._fold_fired_since_latest_plan(grown),
                sj._fold_fired_since_latest_plan(kept),
                "the fold reads one tag however many lines carry it",
            )

    def test_no_generated_journal_exceeds_the_bound(self) -> None:
        rng = random.Random(895)
        for trial in range(1500):
            journal = _random_journal(rng)
            kept = cl._compact_standalone_stop_journal_lines(journal)
            per_uic: dict[int, int] = {}
            for line in kept:
                uic = sj._coerce(line, "uic", int)
                if uic is not None:
                    per_uic[uic] = per_uic.get(uic, 0) + 1
            for uic, count in per_uic.items():
                bound = cl.COMPACTED_FIXED_LINES_PER_UIC_BOUND + _keyed_slots(journal, uic)
                self.assertLessEqual(count, bound, f"trial {trial}: uic {uic} kept {count}")


def _keyed_slots(journal: list[dict[str, Any]], uic: int) -> int:
    """The bound's key-driven terms for ``uic``, read off the INPUT: one per
    ``planned`` crid, per ``tranche_fired`` line, per owed pick key and per
    closure ref key."""
    crids: set[Any] = set()
    fired = 0
    owed: set[str] = set()
    closure: set[str | None] = set()
    for line in journal:
        if sj._coerce(line, "uic", int) != uic:
            continue
        kind = line.get("kind")
        if kind == "planned":
            crids.add(line.get("client_request_id"))
        elif kind == "tranche_fired":
            fired += 1
        elif kind == "stop_filled":
            pick_key = sj._owed_pick_key_from_stop_fill(line)
            if pick_key:
                owed.add(pick_key)
            ref = line.get("ref")
            closure.add(sj._pick_key_from_stop_ref(ref if isinstance(ref, str) else None))
    return len(crids) + fired + len(owed) + len(closure)


if __name__ == "__main__":
    unittest.main()
