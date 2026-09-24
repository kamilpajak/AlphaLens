"""CLI tests for `alphalens thematic intent` — the brief producer (#1469).

Its stdout is piped into `alphalens broker arm -`, so stdout carries the
document and NOTHING else, on success; on failure it is empty and the message is
on stderr. Exit statuses follow the CLI convention: 2 usage, 4 not found, 1 any
other refusal.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import pandas as pd
from typer.testing import CliRunner

BRIEF_DATE = "2026-08-21"
EXIT_USAGE = 2
EXIT_NOT_FOUND = 4
EXIT_REFUSED = 1


def _setup(**overrides: object) -> dict:
    setup: dict = {
        "schema_version": "1.1.0",
        "status": "OK",
        "disaster_stop": 54.0,
        "suggested_size_pct": 3.0,
        "order_ttl_days": 7,
        "entry_tiers": [
            {"limit": 60.0, "alloc_pct": 60.0, "tag": "swing-low"},
            {"limit": 58.0, "alloc_pct": 40.0, "tag": "50-day MA"},
        ],
        "tp_tranches": [
            {"target": 66.0, "tranche_pct": 100.0, "r_multiple": 1.2, "tag": "overhead resistance"}
        ],
    }
    setup.update(overrides)
    return setup


def _write_brief(briefs_dir: Path, rows: dict[str, str | None]) -> None:
    """One brief parquet: ticker -> the `brief_trade_setup` JSON string (or None)."""
    briefs_dir.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(
        {"ticker": list(rows), "brief_trade_setup": list(rows.values())},
    )
    frame.to_parquet(briefs_dir / f"{BRIEF_DATE}.parquet")


class _IntentCase(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.briefs = Path(tmp.name) / "briefs"
        _write_brief(self.briefs, {"KBH": json.dumps(_setup())})

    def invoke(self, *argv: str, exit_policy: str | None = "trail"):
        from alphalens_cli.main import app

        stated = [] if exit_policy is None else ["--exit", exit_policy]
        return self.runner.invoke(
            app, ["thematic", "intent", *argv, *stated, "--briefs-dir", str(self.briefs)]
        )

    def assert_failed(self, result, exit_code: int, message: str) -> None:
        self.assertEqual(result.exit_code, exit_code, result.output)
        self.assertEqual(result.stdout, "")
        self.assertIn(message, result.stderr)


class TheDocumentIsTheWholeOutput(_IntentCase):
    def test_a_frame_sized_document(self) -> None:
        result = self.invoke("KBH", "--date", BRIEF_DATE, "--frame", "24000", "--currency", "pln")

        self.assertEqual(result.exit_code, 0, result.output)
        document = json.loads(result.stdout)
        self.assertEqual(document["spec"]["size"], {"notional_acct": 720.0, "currency": "PLN"})
        self.assertEqual(document["meta"], {"source": "brief", "trade_date": BRIEF_DATE})

    def test_a_given_amount(self) -> None:
        result = self.invoke("KBH", "--date", BRIEF_DATE, "--notional", "1500", "--currency", "EUR")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.stdout)["spec"]["size"]["notional_acct"], 1500.0)


class UsageErrors(_IntentCase):
    def test_each_is_exit_2_with_nothing_on_stdout(self) -> None:
        cases = {
            "no currency": ["KBH", "--date", BRIEF_DATE, "--frame", "24000"],
            "no size": ["KBH", "--date", BRIEF_DATE, "--currency", "PLN"],
            "both sizes": [
                "KBH",
                "--date",
                BRIEF_DATE,
                "--frame",
                "24000",
                "--notional",
                "1500",
                "--currency",
                "PLN",
            ],
            "zero frame": ["KBH", "--date", BRIEF_DATE, "--frame", "0", "--currency", "PLN"],
            "nan frame": ["KBH", "--date", BRIEF_DATE, "--frame", "nan", "--currency", "PLN"],
            "negative notional": [
                "KBH",
                "--date",
                BRIEF_DATE,
                "--notional",
                "-5",
                "--currency",
                "PLN",
            ],
            "bad date": ["KBH", "--date", "21/08/2026", "--frame", "24000", "--currency", "PLN"],
        }
        for name, argv in cases.items():
            with self.subTest(name):
                result = self.invoke(*argv)
                self.assertEqual(result.exit_code, EXIT_USAGE, result.output)
                self.assertEqual(result.stdout, "")

    def test_the_exit_must_be_stated(self) -> None:
        # #1530: no silent default. The trail used to be added without a word.
        argv = ["KBH", "--date", BRIEF_DATE, "--frame", "24000", "--currency", "PLN"]
        for name, choice in {"absent": None, "unknown": "chandelier"}.items():
            with self.subTest(name):
                result = self.invoke(*argv, exit_policy=choice)
                self.assertEqual(result.exit_code, EXIT_USAGE, result.output)
                self.assertEqual(result.stdout, "")


class TheExitIsStatedAndShown(_IntentCase):
    ARGV = ("KBH", "--date", BRIEF_DATE, "--frame", "24000", "--currency", "PLN")

    def test_trail_declares_the_trailing_stop_and_says_so(self) -> None:
        result = self.invoke(*self.ARGV, exit_policy="trail")

        self.assertEqual(result.exit_code, 0, result.output)
        plan = json.loads(result.stdout)["exit"]["reaction_plan"]
        self.assertEqual(plan, [{"kind": "trailing_stop", "arm_trigger_r": 0.5, "trail_frac": 0.6}])
        self.assertIn("exit: trailing stop", result.stderr)

    def test_none_declares_nothing_and_warns_about_the_lenses(self) -> None:
        result = self.invoke(*self.ARGV, exit_policy="none")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIsNone(json.loads(result.stdout)["exit"])
        self.assertIn("exit: none", result.stderr)
        self.assertIn("/edge", result.stderr)


class Refusals(_IntentCase):
    def test_a_ticker_not_in_the_brief_is_not_found(self) -> None:
        result = self.invoke("ZZZZ", "--date", BRIEF_DATE, "--frame", "24000", "--currency", "PLN")
        self.assert_failed(result, EXIT_NOT_FOUND, "not in the 2026-08-21 brief")

    def test_a_missing_brief_is_not_found(self) -> None:
        result = self.invoke("KBH", "--date", "2026-08-20", "--frame", "24000", "--currency", "PLN")
        self.assert_failed(result, EXIT_NOT_FOUND, "not found")

    def test_rows_that_cannot_be_written_are_refused(self) -> None:
        _write_brief(
            self.briefs,
            {
                "NOSETUP": None,
                "FLAT": json.dumps(_setup(status="NO_STRUCTURE")),
                "HUGE": json.dumps(_setup(suggested_size_pct=150.0)),
                # `json.loads` reads a bare NaN; strict JSON cannot carry it.
                "NANSTOP": json.dumps(_setup(disaster_stop=float("nan"))),
            },
        )
        messages = {
            "NOSETUP": "no plannable trade_setup",
            "FLAT": "not plannable",
            "HUGE": "outside (0, 100]",
            "NANSTOP": "strict JSON",
        }
        for ticker, message in messages.items():
            with self.subTest(ticker):
                result = self.invoke(
                    ticker, "--date", BRIEF_DATE, "--frame", "24000", "--currency", "PLN"
                )
                self.assert_failed(result, EXIT_REFUSED, message)


class ThroughTheDoor(_IntentCase):
    """The behaviour change the issue names: the door's key rules now apply to
    brief picks. A re-run is refused rather than replacing, and a disarmed pick
    is never resurrected: the next send is generation 2."""

    def test_rerun_refused_then_generation_two_after_disarm(self) -> None:
        from alphalens_cli.commands.broker import broker_app
        from alphalens_pipeline.brokers.automanager.picks import read_pick_fold

        home = self.briefs.parent / "home"
        home.mkdir()
        document = self.invoke(
            "KBH", "--date", BRIEF_DATE, "--frame", "24000", "--currency", "PLN"
        ).stdout
        moment = dt.datetime(2026, 9, 16, 15, 0, tzinfo=dt.UTC)
        with (
            mock.patch("pathlib.Path.home", return_value=home),
            mock.patch("alphalens_cli.commands.broker._arming_now", return_value=moment),
        ):
            first = self.runner.invoke(broker_app, ["arm", "-"], input=document)
            again = self.runner.invoke(broker_app, ["arm", "-", "--format", "json"], input=document)
            disarm = self.runner.invoke(broker_app, ["disarm", "KBH", "--date", BRIEF_DATE])
            after = self.runner.invoke(broker_app, ["arm", "-"], input=document)
            inbox = home / ".alphalens" / "broker_orders" / "sim" / "picks.jsonl"
            records = read_pick_fold(path=inbox).records

        self.assertEqual(first.exit_code, 0, first.output)
        self.assertEqual(
            json.loads(again.stderr.strip().splitlines()[-1])["code"], "pick_already_armed"
        )
        self.assertEqual(disarm.exit_code, 0, disarm.output)
        self.assertEqual(after.exit_code, 0, after.output)
        by_generation = {record.generation: record for record in records}
        self.assertEqual(by_generation[1].status, "disarmed")
        self.assertEqual(by_generation[2].status, "armed")
        self.assertEqual(by_generation[2].record["intent"]["intent_id"], "KBH:2026-08-21-g2")


class NoneThroughTheDoor(_IntentCase):
    def test_the_door_accepts_a_brief_pick_with_no_exit(self) -> None:
        from alphalens_cli.commands.broker import broker_app

        home = self.briefs.parent / "home"
        home.mkdir()
        document = self.invoke(
            "KBH",
            "--date",
            BRIEF_DATE,
            "--frame",
            "24000",
            "--currency",
            "PLN",
            exit_policy="none",
        ).stdout
        moment = dt.datetime(2026, 9, 16, 15, 0, tzinfo=dt.UTC)
        with (
            mock.patch("pathlib.Path.home", return_value=home),
            mock.patch("alphalens_cli.commands.broker._arming_now", return_value=moment),
        ):
            result = self.runner.invoke(
                broker_app, ["arm", "-", "--dry-run", "--format", "json"], input=document
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIsNone(json.loads(result.stdout)["intent"]["exit"])


class TheRealBinaryWritesOneJsonValue(unittest.TestCase):
    """Acceptance 2, on the process a shell pipe actually runs. `cwd` is a temp
    directory so `main()`'s `load_dotenv()` cannot load the repo's `.env`."""

    def test_stdout_parses_as_one_value(self) -> None:
        with TemporaryDirectory() as tmp:
            briefs = Path(tmp) / "briefs"
            _write_brief(briefs, {"KBH": json.dumps(_setup())})
            binary = Path(sys.executable).parent / "alphalens"
            completed = subprocess.run(
                [
                    str(binary),
                    "thematic",
                    "intent",
                    "KBH",
                    "--date",
                    BRIEF_DATE,
                    "--frame",
                    "24000",
                    "--currency",
                    "PLN",
                    "--exit",
                    "trail",
                    "--briefs-dir",
                    str(briefs),
                ],
                capture_output=True,
                text=True,
                cwd=tmp,
                env={**os.environ, "HOME": tmp},
                check=False,
                timeout=120,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["instrument"]["ticker"], "KBH")
        # The summary goes to stderr, so the pipe into `broker arm -` stays clean.
        self.assertIn("exit: trailing stop", completed.stderr)


if __name__ == "__main__":
    unittest.main()
