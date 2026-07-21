"""Hermetic tests for the append-only pick queue.

Mirrors submission_log.py: one JSON line per arm, file never rewritten,
malformed/undated lines skipped not fatal, missing file yields nothing.
"""

from __future__ import annotations

import datetime as dt
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from alphalens_pipeline.brokers.automanager.picks import (
    STATUS_ARMED,
    Pick,
    arm_pick,
    iter_picks,
)


class ArmPickTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "picks.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_arm_pick_appends_one_armed_line(self) -> None:
        arm_pick("ko", dt.date(2026, 7, 20), path=self.path)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["ticker"], "KO")
        self.assertEqual(record["date"], "2026-07-20")
        self.assertEqual(record["status"], STATUS_ARMED)
        self.assertTrue(record["armed_ts"])

    def test_arm_pick_never_rewrites_appends_second_line(self) -> None:
        arm_pick("KO", dt.date(2026, 7, 20), path=self.path)
        arm_pick("MU", dt.date(2026, 7, 21), path=self.path)
        self.assertEqual(len(self.path.read_text().splitlines()), 2)

    def test_arm_pick_creates_parent_dir(self) -> None:
        nested = Path(self._tmp.name) / "broker_orders" / "picks.jsonl"
        arm_pick("KO", dt.date(2026, 7, 20), path=nested)
        self.assertTrue(nested.exists())


class IterPicksTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "picks.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_iter_missing_file_yields_nothing(self) -> None:
        self.assertEqual(list(iter_picks(path=self.path)), [])

    def test_iter_round_trips_in_append_order(self) -> None:
        arm_pick("KO", dt.date(2026, 7, 20), path=self.path)
        arm_pick("MU", dt.date(2026, 7, 21), path=self.path)
        picks = list(iter_picks(path=self.path))
        self.assertEqual([p.ticker for p in picks], ["KO", "MU"])
        self.assertEqual(picks[0].date, dt.date(2026, 7, 20))
        self.assertIsInstance(picks[0], Pick)
        self.assertEqual(picks[0].status, STATUS_ARMED)

    def test_iter_skips_malformed_and_undated_lines(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            "not json\n"
            + json.dumps(["a", "list"])
            + "\n"
            + json.dumps({"ticker": "NODATE", "status": "armed"})
            + "\n"
            + json.dumps(
                {
                    "ticker": "GOOD",
                    "date": "2026-07-20",
                    "armed_ts": "2026-07-20T00:00:00+00:00",
                    "status": "armed",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        picks = list(iter_picks(path=self.path))
        self.assertEqual([p.ticker for p in picks], ["GOOD"])


if __name__ == "__main__":
    unittest.main()
