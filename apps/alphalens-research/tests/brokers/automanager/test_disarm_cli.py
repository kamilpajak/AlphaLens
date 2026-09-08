"""CLI tests for `alphalens broker disarm`.

The operator counterpart of `arm`: retire an armed (ticker, date) pick from the
queue AND cancel its open entry-trail watch tiers, in that strict order —
watch-refusal first (a tier with a resting/in-flight armed BUY refuses the
whole disarm and writes NOTHING), queue write second. Lazy-import doctrine:
patches target the SOURCE modules, exactly like test_arm_cli.py.
"""

from __future__ import annotations

import datetime as dt
import json
import unittest
from pathlib import Path

from typer.testing import CliRunner

from tests.brokers.automanager.test_arm_cli import _isolate_home, _seed_legacy_flat_state


def _seed_watch(home: Path, env: str, crid: str, pick_key: str, *extra_lines: str) -> Path:
    journal = home / ".alphalens" / "broker_orders" / env / "entry_trails.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {"kind": "watch_open", "crid": crid, "pick_key": pick_key, "limit": 10.0, "qty": 5}
        )
    ]
    lines.extend(extra_lines)
    journal.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return journal


class DisarmCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        self.home = _isolate_home(self)

    def test_disarm_cancels_watch_and_retires_queue(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        journal = _seed_watch(self.home, "sim", "IBRX-2026-08-26-entry-t0", "IBRX:2026-08-26")
        result = self.runner.invoke(broker_app, ["disarm", "ibrx", "--date", "2026-08-26"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("disarmed IBRX:2026-08-26", result.output)
        self.assertIn("cancelled 1 watch tier", result.output)
        journal_kinds = [json.loads(x)["kind"] for x in journal.read_text().splitlines()]
        self.assertEqual(journal_kinds, ["watch_open", "cancelled"])
        picks = self.home / ".alphalens" / "broker_orders" / "sim" / "picks.jsonl"
        record = json.loads(picks.read_text().splitlines()[-1])
        self.assertEqual(
            (record["ticker"], record["date"], record["status"]),
            ("IBRX", "2026-08-26", "disarmed"),
        )

    def test_disarm_defaults_to_the_latest_generation(self) -> None:
        # #1371: with generation 1 already retired and generation 2 live, a
        # bare disarm must retire generation 2 and leave generation 1's tier
        # (already terminal) alone; the queue line carries the generation.
        from alphalens_cli.commands.broker import broker_app
        from alphalens_pipeline.brokers.automanager.picks import arm_pick, mark_disarmed

        from tests.brokers.automanager.test_picks import _intent

        journal = _seed_watch(
            self.home,
            "sim",
            "IBRX-2026-08-26-entry-t0",
            "IBRX:2026-08-26",
            json.dumps({"kind": "cancelled", "crid": "IBRX-2026-08-26-entry-t0"}),
            json.dumps(
                {
                    "kind": "watch_open",
                    "crid": "IBRX-2026-08-26-g2-entry-t0",
                    "pick_key": "IBRX:2026-08-26-g2",
                    "limit": 10.0,
                    "qty": 5,
                }
            ),
        )
        picks = self.home / ".alphalens" / "broker_orders" / "sim" / "picks.jsonl"
        arm_pick(_intent("IBRX", "2026-08-26"), path=picks)
        mark_disarmed("IBRX", dt.date(2026, 8, 26), note="wrong size", path=picks)
        arm_pick(_intent("IBRX", "2026-08-26", generation=2), path=picks)
        result = self.runner.invoke(broker_app, ["disarm", "ibrx", "--date", "2026-08-26"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("disarmed IBRX:2026-08-26-g2", result.output)
        self.assertIn("cancelled 1 watch tier", result.output)
        cancelled = [
            json.loads(x)["crid"]
            for x in journal.read_text().splitlines()
            if json.loads(x)["kind"] == "cancelled"
        ]
        self.assertEqual(cancelled, ["IBRX-2026-08-26-entry-t0", "IBRX-2026-08-26-g2-entry-t0"])
        record = json.loads(picks.read_text().splitlines()[-1])
        self.assertEqual((record["status"], record["generation"]), ("disarmed", 2))

    def test_disarm_generation_option_targets_that_generation_only(self) -> None:
        from alphalens_cli.commands.broker import broker_app
        from alphalens_pipeline.brokers.automanager.picks import arm_pick

        from tests.brokers.automanager.test_picks import _intent

        journal = _seed_watch(
            self.home,
            "sim",
            "IBRX-2026-08-26-entry-t0",
            "IBRX:2026-08-26",
            json.dumps(
                {
                    "kind": "watch_open",
                    "crid": "IBRX-2026-08-26-g2-entry-t0",
                    "pick_key": "IBRX:2026-08-26-g2",
                    "limit": 10.0,
                    "qty": 5,
                }
            ),
        )
        picks = self.home / ".alphalens" / "broker_orders" / "sim" / "picks.jsonl"
        arm_pick(_intent("IBRX", "2026-08-26"), path=picks)
        arm_pick(_intent("IBRX", "2026-08-26", generation=2), path=picks)
        result = self.runner.invoke(
            broker_app, ["disarm", "ibrx", "--date", "2026-08-26", "--generation", "1"]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("disarmed IBRX:2026-08-26 ", result.output)
        cancelled = [
            json.loads(x)["crid"]
            for x in journal.read_text().splitlines()
            if json.loads(x)["kind"] == "cancelled"
        ]
        self.assertEqual(cancelled, ["IBRX-2026-08-26-entry-t0"])
        record = json.loads(picks.read_text().splitlines()[-1])
        self.assertEqual(record["status"], "disarmed")
        self.assertNotIn("generation", record)

    def test_disarm_without_open_watch_still_retires_queue(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        result = self.runner.invoke(broker_app, ["disarm", "RIOT", "--date", "2026-08-25"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("no open watch", result.output)
        picks = self.home / ".alphalens" / "broker_orders" / "sim" / "picks.jsonl"
        record = json.loads(picks.read_text().splitlines()[-1])
        self.assertEqual((record["ticker"], record["status"]), ("RIOT", "disarmed"))

    def test_disarm_env_live_targets_the_live_inbox(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        result = self.runner.invoke(
            broker_app, ["disarm", "KO", "--date", "2026-08-26", "--env", "live"]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        live_picks = self.home / ".alphalens" / "broker_orders" / "live" / "picks.jsonl"
        self.assertTrue(live_picks.exists())
        sim_picks = self.home / ".alphalens" / "broker_orders" / "sim" / "picks.jsonl"
        self.assertFalse(sim_picks.exists())

    def test_disarm_unknown_env_refuses_via_state_paths(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        result = self.runner.invoke(
            broker_app, ["disarm", "KO", "--date", "2026-08-26", "--env", "prod"]
        )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("prod", result.output)

    def test_disarm_bad_date_refuses(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        result = self.runner.invoke(broker_app, ["disarm", "KO", "--date", "not-a-date"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("invalid --date", result.output)

    def test_disarm_legacy_flat_layout_refuses(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        _seed_legacy_flat_state(self.home)
        result = self.runner.invoke(broker_app, ["disarm", "KO", "--date", "2026-08-26"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("legacy flat broker state", result.output)

    def test_disarm_overlong_note_refuses(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        result = self.runner.invoke(
            broker_app, ["disarm", "KO", "--date", "2026-08-26", "--note", "x" * 501]
        )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("note", result.output)
        picks = self.home / ".alphalens" / "broker_orders" / "sim" / "picks.jsonl"
        self.assertFalse(picks.exists())

    def test_disarm_refuses_on_resting_armed_order_and_writes_nothing(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        journal = _seed_watch(
            self.home,
            "sim",
            "IBRX-2026-08-26-entry-t0",
            "IBRX:2026-08-26",
            json.dumps(
                {"kind": "trail_armed", "crid": "IBRX-2026-08-26-entry-t0", "order_id": "42"}
            ),
        )
        before = journal.read_bytes()
        result = self.runner.invoke(broker_app, ["disarm", "IBRX", "--date", "2026-08-26"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("resting", result.output)
        self.assertIn("broker cancel", result.output)
        self.assertEqual(journal.read_bytes(), before, "refusal must write nothing")
        picks = self.home / ".alphalens" / "broker_orders" / "sim" / "picks.jsonl"
        self.assertFalse(picks.exists(), "queue must stay untouched on refusal")


if __name__ == "__main__":
    unittest.main()
