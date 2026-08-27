"""Hermetic tests for ``entry_trails.cancel_open_watches`` (the watch half of
`alphalens broker disarm`) and the path seam it rides on.

Retiring a pick from the queue does NOT stop an open entry-trail watch — the
watch pass reads only ``entry_trails.jsonl`` and would still arm a real broker
BUY. Disarm therefore appends one terminal ``cancelled`` line per open crid of
the pick, which releases the active set, the virtual gross reservation, and
the watch-capacity slot in one stroke (the daemon already folds ``cancelled``).

Refusal-first atomicity: a tier whose latest kind is ``trail_armed`` may have
a REAL resting Saxo BUY (or an in-flight POST on the null-id write-ahead) that
a bare ``cancelled`` would orphan — ``_resting_armed_tiers`` filters terminal
tiers, so nothing would ever cancel or reconcile it. ``cancel_open_watches``
refuses in that state and writes NOTHING.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from alphalens_pipeline.brokers.automanager import entry_trails as et

_PICK_KEY = "IBRX:2026-08-26"
_OTHER_KEY = "BAH:2026-08-26"


def _line(kind: str, crid: str, **payload: object) -> str:
    return json.dumps({"kind": kind, "crid": crid, **payload}, sort_keys=True)


def _watch_open(crid: str, pick_key: str) -> str:
    return _line("watch_open", crid, pick_key=pick_key, limit=10.0, qty=100)


class CancelOpenWatchesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "entry_trails.jsonl"

    def _write(self, *lines: str) -> None:
        self.path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")

    def test_cancels_every_open_tier_of_the_pick_and_only_those(self) -> None:
        self._write(
            _watch_open("IBRX-2026-08-26-entry-t0", _PICK_KEY),
            _watch_open("IBRX-2026-08-26-entry-t1", _PICK_KEY),
            _watch_open("BAH-2026-08-26-entry-t0", _OTHER_KEY),
        )
        cancelled = et.cancel_open_watches(_PICK_KEY, note="operator disarm", path=self.path)
        self.assertEqual(
            sorted(cancelled), ["IBRX-2026-08-26-entry-t0", "IBRX-2026-08-26-entry-t1"]
        )
        fold = et.read_entry_trail_fold(path=self.path)
        self.assertEqual(fold.tiers["IBRX-2026-08-26-entry-t0"].terminal_kind, "cancelled")
        self.assertEqual(fold.tiers["IBRX-2026-08-26-entry-t1"].terminal_kind, "cancelled")
        self.assertIsNone(fold.tiers["BAH-2026-08-26-entry-t0"].terminal_kind)
        appended = [json.loads(x) for x in self.path.read_text().splitlines()[3:]]
        self.assertTrue(all(rec["note"] == "operator disarm" for rec in appended))

    def test_already_terminal_tier_is_not_re_cancelled(self) -> None:
        self._write(
            _watch_open("IBRX-2026-08-26-entry-t0", _PICK_KEY),
            _line("expired", "IBRX-2026-08-26-entry-t0"),
        )
        cancelled = et.cancel_open_watches(_PICK_KEY, note="operator disarm", path=self.path)
        self.assertEqual(cancelled, [])
        self.assertEqual(len(self.path.read_text().splitlines()), 2, "nothing appended")

    def test_no_matching_pick_key_is_a_clean_noop(self) -> None:
        self._write(_watch_open("BAH-2026-08-26-entry-t0", _OTHER_KEY))
        cancelled = et.cancel_open_watches(_PICK_KEY, note="operator disarm", path=self.path)
        self.assertEqual(cancelled, [])

    def test_missing_journal_is_a_clean_noop(self) -> None:
        cancelled = et.cancel_open_watches(_PICK_KEY, note="operator disarm", path=self.path)
        self.assertEqual(cancelled, [])
        self.assertFalse(self.path.exists())

    def test_refuses_atomically_on_a_real_armed_order_id(self) -> None:
        self._write(
            _watch_open("IBRX-2026-08-26-entry-t0", _PICK_KEY),
            _watch_open("IBRX-2026-08-26-entry-t1", _PICK_KEY),
            _line("trail_armed", "IBRX-2026-08-26-entry-t1", order_id="5039570099"),
        )
        before = self.path.read_bytes()
        with self.assertRaises(et.DisarmRestingOrderError) as ctx:
            et.cancel_open_watches(_PICK_KEY, note="operator disarm", path=self.path)
        self.assertIn("IBRX-2026-08-26-entry-t1", str(ctx.exception))
        self.assertIn("5039570099", str(ctx.exception))
        self.assertEqual(self.path.read_bytes(), before, "refusal must write NOTHING")

    def test_refuses_on_an_in_flight_null_id_arm_too(self) -> None:
        # The G3 write-ahead line has a null order id while the POST is in
        # flight — the POST may have landed a real resting order, so a bare
        # cancelled would orphan it just the same. Conservative refusal.
        self._write(
            _watch_open("IBRX-2026-08-26-entry-t0", _PICK_KEY),
            _line("trail_armed", "IBRX-2026-08-26-entry-t0"),
        )
        before = self.path.read_bytes()
        with self.assertRaises(et.DisarmRestingOrderError):
            et.cancel_open_watches(_PICK_KEY, note="operator disarm", path=self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_rearm_after_touched_tier_still_cancels(self) -> None:
        # touched/trough are non-terminal, non-armed states — cancellable.
        self._write(
            _watch_open("IBRX-2026-08-26-entry-t0", _PICK_KEY),
            _line("touched", "IBRX-2026-08-26-entry-t0"),
            _line("trough", "IBRX-2026-08-26-entry-t0", trough=9.5),
        )
        cancelled = et.cancel_open_watches(_PICK_KEY, note="operator disarm", path=self.path)
        self.assertEqual(cancelled, ["IBRX-2026-08-26-entry-t0"])

    def test_cancelled_crid_stays_terminal_after_a_later_watch_open(self) -> None:
        # The sticky-terminal pin: the fold never clears terminal_kind, and
        # crids are deterministic per (ticker, date, tier) — so re-arming a
        # disarmed (ticker, date) will NOT re-open its watch. Documented on
        # both mark_disarmed and cancel_open_watches; a fresh brief date is
        # the path back.
        self._write(_watch_open("IBRX-2026-08-26-entry-t0", _PICK_KEY))
        et.cancel_open_watches(_PICK_KEY, note="operator disarm", path=self.path)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(_watch_open("IBRX-2026-08-26-entry-t0", _PICK_KEY) + "\n")
        fold = et.read_entry_trail_fold(path=self.path)
        self.assertEqual(fold.tiers["IBRX-2026-08-26-entry-t0"].terminal_kind, "cancelled")


class PathSeamTest(unittest.TestCase):
    def test_read_and_append_honor_an_explicit_path(self) -> None:
        with TemporaryDirectory() as d:
            target = Path(d) / "custom" / "entry_trails.jsonl"
            et.append_entry_trail_line({"kind": "watch_open", "crid": "X-t0"}, path=target)
            self.assertTrue(target.exists())
            fold = et.read_entry_trail_fold(path=target)
            self.assertIn("X-t0", fold.tiers)


if __name__ == "__main__":
    unittest.main()
