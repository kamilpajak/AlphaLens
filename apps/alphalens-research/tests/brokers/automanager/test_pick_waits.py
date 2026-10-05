"""The append-only record of picks waiting for free capital (#1734).

A waiting pick stays ``armed`` in ``picks.jsonl``; this journal is how a
separate process (`broker status`, `broker picks`) learns WHY it is unplaced
without sizing it, and how a restarted daemon knows it already paged.
"""

from __future__ import annotations

import datetime as dt
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from alphalens_pipeline.brokers.automanager import pick_waits, state_paths

from tests.brokers.automanager.home_isolation import IsolatedHomeTestCase


def _wait(pick_key: str = "KO:2026-09-30", gate: str = pick_waits.GATE_GROSS_CAP) -> dict:
    ticker, _, token = pick_key.partition(":")
    return {
        "pick_key": pick_key,
        "ticker": ticker,
        "date": token[:10],
        "gate": gate,
        "message": f"{gate}: need more",
        "window_end": "2026-10-09T20:00:00+00:00",
    }


class PickWaitsJournalTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "pick_waits.jsonl"

    def test_append_writes_one_line_with_a_timestamp(self) -> None:
        pick_waits.append_wait(_wait(), path=self.path)
        (line,) = self.path.read_text(encoding="utf-8").splitlines()
        record = json.loads(line)
        self.assertEqual(record["pick_key"], "KO:2026-09-30")
        self.assertEqual(record["gate"], "gross_cap")
        self.assertIsNotNone(dt.datetime.fromisoformat(record["ts"]).tzinfo)

    def test_the_latest_line_per_pick_wins(self) -> None:
        pick_waits.append_wait(_wait(gate=pick_waits.GATE_GROSS_CAP), path=self.path)
        pick_waits.append_wait(_wait("MU:2026-09-30"), path=self.path)
        pick_waits.append_wait(_wait(gate=pick_waits.GATE_CASH_FLOOR), path=self.path)
        fold = pick_waits.read_waits(path=self.path)
        self.assertEqual(set(fold.latest), {"KO:2026-09-30", "MU:2026-09-30"})
        self.assertEqual(fold.latest["KO:2026-09-30"].gate, "cash_floor")

    def test_the_first_line_per_pick_is_kept_as_since(self) -> None:
        # `since` is when the pick started waiting, not when the binding gate
        # last changed: the operator asks "how long has this waited".
        pick_waits.append_wait(_wait(gate=pick_waits.GATE_GROSS_CAP), path=self.path)
        first_ts = pick_waits.read_waits(path=self.path).latest["KO:2026-09-30"].ts
        pick_waits.append_wait(_wait(gate=pick_waits.GATE_CASH_FLOOR), path=self.path)
        self.assertEqual(
            pick_waits.read_waits(path=self.path).latest["KO:2026-09-30"].since, first_ts
        )

    def test_malformed_lines_are_counted_and_skipped(self) -> None:
        self.path.write_text(
            "not json\n" + json.dumps(["a"]) + "\n" + json.dumps({"gate": "gross_cap"}) + "\n\n",
            encoding="utf-8",
        )
        pick_waits.append_wait(_wait(), path=self.path)
        fold = pick_waits.read_waits(path=self.path)
        self.assertEqual(fold.malformed, 3)
        self.assertEqual(list(fold.latest), ["KO:2026-09-30"])

    def test_a_missing_file_folds_empty(self) -> None:
        fold = pick_waits.read_waits(path=self.path)
        self.assertEqual((fold.latest, fold.malformed), ({}, 0))


class PickWaitsPathTest(IsolatedHomeTestCase):
    def test_the_journal_sits_beside_the_pick_queue(self) -> None:
        self.assertEqual(
            state_paths.pick_waits_path(env="live").parent,
            state_paths.picks_path(env="live").parent,
        )
        self.assertEqual(state_paths.pick_waits_path(env="live").name, "pick_waits.jsonl")

    def test_the_default_path_is_the_ambient_instance(self) -> None:
        pick_waits.append_wait(_wait())
        self.assertTrue(state_paths.pick_waits_path().exists())


if __name__ == "__main__":
    unittest.main()
