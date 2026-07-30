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
    STATUS_REFUSED,
    Pick,
    arm_pick,
    iter_picks,
    mark_refused,
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


class MarkRefusedTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "picks.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_mark_refused_appends_terminal_refused_line(self) -> None:
        arm_pick("ko", dt.date(2026, 7, 29), path=self.path)
        mark_refused("ko", dt.date(2026, 7, 29), "portfolio cap exceeded", path=self.path)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2, "append-only: the armed line must stay")
        record = json.loads(lines[1])
        self.assertEqual(record["ticker"], "KO")
        self.assertEqual(record["date"], "2026-07-29")
        self.assertEqual(record["status"], STATUS_REFUSED)
        self.assertEqual(record["reason"], "portfolio cap exceeded")
        parsed_ts = dt.datetime.fromisoformat(record["refused_ts"])
        self.assertIsNotNone(parsed_ts.tzinfo, "refused_ts must be timezone-aware UTC")

    def test_mark_refused_creates_parent_dir(self) -> None:
        nested = Path(self._tmp.name) / "broker_orders" / "picks.jsonl"
        mark_refused("KO", dt.date(2026, 7, 29), "cap", path=nested)
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

    def test_iter_yields_only_armed_status_lines(self) -> None:
        # A non-armed status line (cancelled / filled / expired) must NEVER be
        # yielded — the drain places whatever iter_picks emits, so the ARMED
        # filter belongs inside iter_picks (defence in depth against re-placing a
        # retired intent).
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "ticker": "ARMEDX",
                    "date": "2026-07-20",
                    "armed_ts": "2026-07-20T00:00:00+00:00",
                    "status": "armed",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "ticker": "CANCELLEDX",
                    "date": "2026-07-20",
                    "armed_ts": "2026-07-20T00:00:00+00:00",
                    "status": "cancelled",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "ticker": "FILLEDX",
                    "date": "2026-07-21",
                    "armed_ts": "2026-07-21T00:00:00+00:00",
                    "status": "filled",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        picks = list(iter_picks(path=self.path))
        self.assertEqual([p.ticker for p in picks], ["ARMEDX"])

    def test_refused_latest_line_retires_the_armed_pick(self) -> None:
        # Terminal refusal: the latest status line per (ticker, date) wins, so a
        # refused line appended AFTER the armed line stops the drain from ever
        # retrying the pick (the live 2026-07-30 every-45s retry hazard). Other
        # armed tickers are untouched.
        arm_pick("KO", dt.date(2026, 7, 29), path=self.path)
        arm_pick("MU", dt.date(2026, 7, 29), path=self.path)
        mark_refused("KO", dt.date(2026, 7, 29), "portfolio cap exceeded", path=self.path)
        picks = list(iter_picks(path=self.path))
        self.assertEqual([p.ticker for p in picks], ["MU"])

    def test_rearm_after_refusal_yields_the_pick_again(self) -> None:
        # `alphalens broker arm` is the explicit human path back: a NEW armed
        # line after the refusal makes armed the latest status again.
        arm_pick("KO", dt.date(2026, 7, 29), path=self.path)
        mark_refused("KO", dt.date(2026, 7, 29), "portfolio cap exceeded", path=self.path)
        arm_pick("KO", dt.date(2026, 7, 29), path=self.path)
        picks = list(iter_picks(path=self.path))
        self.assertEqual([p.ticker for p in picks], ["KO"])
        self.assertEqual(picks[0].status, STATUS_ARMED)

    def test_refusal_scoped_to_its_brief_date(self) -> None:
        # A refusal for one brief date must not retire the same ticker armed for
        # a different brief date — the queue key is (ticker, date).
        arm_pick("KO", dt.date(2026, 7, 28), path=self.path)
        arm_pick("KO", dt.date(2026, 7, 29), path=self.path)
        mark_refused("KO", dt.date(2026, 7, 28), "portfolio cap exceeded", path=self.path)
        picks = list(iter_picks(path=self.path))
        self.assertEqual([(p.ticker, p.date) for p in picks], [("KO", dt.date(2026, 7, 29))])

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
