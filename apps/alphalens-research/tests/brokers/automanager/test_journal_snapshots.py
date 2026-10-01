"""Snapshots of a journal taken before boot compaction rewrites it (#1648).

Boot compaction keeps only the lines the daemon's folds still need. Some of the
lines it removes exist nowhere else: a closed position's ``tranche_fired``
carries the decision-side bid/ask/spread the daemon saw when a take-profit
fired, which Saxo's audit log does not have. On 29.09 and 30.09 SMMT's lines
were removed this way and lost. The compactors now snapshot the exact bytes
they compacted from, and readers rebuild the history from the snapshots plus
the current journal.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from alphalens_pipeline.brokers.automanager import journal_snapshots as js

_T0 = dt.datetime(2026, 9, 30, 20, 34, 51, tzinfo=dt.UTC)


def _line(record: dict) -> bytes:
    return (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")


class SnapshotBytes(unittest.TestCase):
    def test_writes_exactly_the_given_bytes_under_a_stamped_name(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            data = _line({"kind": "tranche_fired", "uic": 1640268}) + b"not json\n"
            path = js.snapshot_bytes(journal, data, clock=lambda: _T0)
            self.assertEqual(path.parent, Path(d) / js.SNAPSHOT_DIRNAME)
            self.assertEqual(path.name, f"standalone_stops.20260930T203451Z.{os.getpid()}.jsonl")
            self.assertEqual(path.read_bytes(), data, "a byte copy, malformed lines included")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_a_snapshot_is_never_overwritten(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "entry_trails.jsonl"
            first = js.snapshot_bytes(journal, b"first\n", clock=lambda: _T0)
            with self.assertRaises(FileExistsError):
                js.snapshot_bytes(journal, b"second\n", clock=lambda: _T0)
            self.assertEqual(first.read_bytes(), b"first\n")

    def test_an_unwritable_directory_raises_and_leaves_nothing_behind(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            snapshot_dir = Path(d) / js.SNAPSHOT_DIRNAME
            snapshot_dir.mkdir(mode=0o500)
            try:
                with self.assertRaises(OSError):
                    js.snapshot_bytes(journal, b"x\n", clock=lambda: _T0)
                self.assertEqual(list(snapshot_dir.iterdir()), [])
            finally:
                snapshot_dir.chmod(0o700)


class IterJournalHistory(unittest.TestCase):
    def test_snapshots_then_the_journal_each_record_once(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            fired = {"kind": "tranche_fired", "uic": 1640268, "tag": "tp1"}
            placed = {"kind": "stop_placed", "uic": 1640268, "order_id": "5446505166"}
            later = {"kind": "stop_placed", "uic": 7300542, "order_id": "5448023092"}
            # First boot: the snapshot holds both lines; compaction kept only
            # `placed`. Second boot appended `later` and snapshotted again.
            js.snapshot_bytes(journal, _line(placed) + _line(fired), clock=lambda: _T0)
            js.snapshot_bytes(
                journal,
                _line(placed) + b"{broken\n" + _line(later),
                clock=lambda: _T0 + dt.timedelta(days=1),
            )
            journal.write_bytes(_line(placed) + _line(later))

            records = list(js.iter_journal_history(journal))

        self.assertEqual(records, [placed, fired, later])

    def test_no_snapshots_means_just_the_journal(self) -> None:
        with TemporaryDirectory() as d:
            journal = Path(d) / "standalone_stops.jsonl"
            journal.write_bytes(_line({"kind": "stop_placed", "uic": 1}))
            self.assertEqual(
                list(js.iter_journal_history(journal)), [{"kind": "stop_placed", "uic": 1}]
            )

    def test_snapshots_of_another_journal_are_not_mixed_in(self) -> None:
        with TemporaryDirectory() as d:
            stops = Path(d) / "standalone_stops.jsonl"
            trails = Path(d) / "entry_trails.jsonl"
            js.snapshot_bytes(trails, _line({"kind": "trough", "crid": "X"}), clock=lambda: _T0)
            self.assertEqual(list(js.iter_journal_history(stops)), [])


if __name__ == "__main__":
    unittest.main()
