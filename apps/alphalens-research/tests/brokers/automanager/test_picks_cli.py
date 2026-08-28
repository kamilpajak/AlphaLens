"""CLI tests for `alphalens broker picks`.

The read-only view over the pick queue (#1197). ``picks.jsonl`` has no
``placed`` status — a pick submitted a month ago still reads ``armed`` as its
latest line forever — so the queue is only readable by performing the SAME
(ticker, brief_date) join against ``submissions.jsonl`` that
``_run_placement_drain`` performs. This command does that join and nothing
else: no broker, no auth, no mutation.

Measured on the VPS 2026-08-28 before this command existed: LIVE 3 armed / 3
joined / 0 pending; SIM 42 armed / 42 joined / 0 pending. Reading picks.jsonl
alone suggested 45 pending placements, and a session reported two of them as a
live anomaly. The states below are what makes that unambiguous.

Lazy-import doctrine: patches target the SOURCE modules, exactly like
test_arm_cli.py / test_disarm_cli.py.
"""

from __future__ import annotations

import datetime as dt
import json
import unittest
from pathlib import Path

from typer.testing import CliRunner

from tests.brokers.automanager.test_arm_cli import _isolate_home
from tests.brokers.automanager.test_picks import _intent


def _picks_path(home: Path, env: str = "sim") -> Path:
    return home / ".alphalens" / "broker_orders" / env / "picks.jsonl"


def _submissions_path(home: Path, env: str = "sim") -> Path:
    return home / ".alphalens" / "broker_orders" / env / "submissions.jsonl"


def _arm(home: Path, ticker: str, brief_date: str, env: str = "sim") -> None:
    from alphalens_pipeline.brokers.automanager.picks import arm_pick

    arm_pick(_intent(ticker, brief_date), path=_picks_path(home, env))


def _submit(home: Path, ticker: str, brief_date: str, env: str = "sim") -> None:
    path = _submissions_path(home, env)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ticker": ticker.upper(), "brief_date": brief_date}) + "\n")


def _append_raw(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def _run(runner: CliRunner, *args: str):
    from alphalens_cli.commands.broker import broker_app

    return runner.invoke(broker_app, ["picks", *args])


def _rows(result) -> dict[str, dict]:
    payload = json.loads(result.stdout)
    return {row["ticker"]: row for row in payload["picks"]}


class PicksCommandStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        self.home = _isolate_home(self)

    def test_armed_and_joined_to_submissions_reads_placed(self) -> None:
        # The reproduction measured on the VPS: every armed pick there was
        # already joined, so "armed" meant "long since placed", not "pending".
        _arm(self.home, "KO", "2026-07-20")
        _submit(self.home, "KO", "2026-07-20")
        result = _run(self.runner, "--format", "json")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(_rows(result)["KO"]["state"], "PLACED")

    def test_armed_and_not_joined_reads_pending(self) -> None:
        _arm(self.home, "KO", "2026-07-20")
        result = _run(self.runner, "--format", "json")
        self.assertEqual(_rows(result)["KO"]["state"], "PENDING")

    def test_refused_carries_its_reason_as_detail(self) -> None:
        from alphalens_pipeline.brokers.automanager.picks import mark_refused

        mark_refused(
            "GME", dt.date(2026, 8, 27), "gross cap: exceeds limit", path=_picks_path(self.home)
        )
        row = _rows(_run(self.runner, "--format", "json"))["GME"]
        self.assertEqual(row["state"], "REFUSED")
        self.assertEqual(row["detail"], "gross cap: exceeds limit")

    def test_disarmed_carries_its_note_as_detail(self) -> None:
        from alphalens_pipeline.brokers.automanager.picks import mark_disarmed

        mark_disarmed(
            "SMG", dt.date(2026, 8, 19), note="retired to free it", path=_picks_path(self.home)
        )
        row = _rows(_run(self.runner, "--format", "json"))["SMG"]
        self.assertEqual(row["state"], "DISARMED")
        self.assertEqual(row["detail"], "retired to free it")

    def test_armed_line_without_intent_reads_unreadable_not_pending(self) -> None:
        # The drain skips this line FOREVER (iter_picks logs it at DEBUG and
        # moves on), so calling it PENDING would promise a placement that can
        # never happen.
        _append_raw(
            _picks_path(self.home),
            {"ticker": "OLD", "date": "2026-07-01", "status": "armed"},
        )
        row = _rows(_run(self.runner, "--format", "json"))["OLD"]
        self.assertEqual(row["state"], "UNREADABLE")

    def test_armed_line_with_undecodable_intent_reads_unreadable(self) -> None:
        _append_raw(
            _picks_path(self.home),
            {
                "ticker": "BAD",
                "date": "2026-07-02",
                "status": "armed",
                "intent": {"not": "a trade intent"},
            },
        )
        row = _rows(_run(self.runner, "--format", "json"))["BAD"]
        self.assertEqual(row["state"], "UNREADABLE")

    def test_rearm_after_refusal_wins_latest(self) -> None:
        # Today's real GME shape: armed 16:38 -> refused 16:39 -> armed 16:42.
        from alphalens_pipeline.brokers.automanager.picks import mark_refused

        _arm(self.home, "GME", "2026-08-27")
        mark_refused("GME", dt.date(2026, 8, 27), "gross cap", path=_picks_path(self.home))
        _arm(self.home, "GME", "2026-08-27")
        rows = _rows(_run(self.runner, "--format", "json"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows["GME"]["state"], "PENDING")

    def test_placed_join_bridges_the_date_and_string_key_forms(self) -> None:
        # The fold keys on dt.date; the drain's join keys on str. A view that
        # forgot to bridge them would call every placed pick PENDING.
        _arm(self.home, "MU", "2026-07-21")
        _submit(self.home, "mu", "2026-07-21")
        self.assertEqual(_rows(_run(self.runner, "--format", "json"))["MU"]["state"], "PLACED")


class PicksCommandOutputTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        self.home = _isolate_home(self)

    def test_json_stdout_is_exactly_one_value(self) -> None:
        _arm(self.home, "KO", "2026-07-20")
        result = _run(self.runner, "--format", "json")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema"], "alphalens.broker.picks/v1")
        self.assertEqual(payload["env"], "sim")

    def test_counts_cover_every_pick_even_when_state_filters_rows(self) -> None:
        from alphalens_pipeline.brokers.automanager.picks import mark_refused

        _arm(self.home, "KO", "2026-07-20")
        _arm(self.home, "MU", "2026-07-21")
        _submit(self.home, "MU", "2026-07-21")
        mark_refused("GME", dt.date(2026, 8, 27), "gross cap", path=_picks_path(self.home))
        result = _run(self.runner, "--format", "json", "--state", "pending")
        payload = json.loads(result.stdout)
        self.assertEqual([row["ticker"] for row in payload["picks"]], ["KO"])
        self.assertEqual(payload["counts"]["pending"], 1)
        self.assertEqual(payload["counts"]["placed"], 1)
        self.assertEqual(payload["counts"]["refused"], 1)

    def test_limit_truncates_and_says_so(self) -> None:
        for index in range(4):
            _arm(self.home, f"T{index}", "2026-07-20")
        result = _run(self.runner, "--format", "json", "--limit", "2")
        payload = json.loads(result.stdout)
        self.assertEqual(len(payload["picks"]), 2)
        self.assertTrue(payload["truncated"])
        self.assertIn("2 of 4", result.stderr)

    def test_human_output_names_state_and_counts(self) -> None:
        _arm(self.home, "KO", "2026-07-20")
        _submit(self.home, "KO", "2026-07-20")
        result = _run(self.runner)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("KO", result.stdout)
        self.assertIn("PLACED", result.stdout)
        self.assertIn("placed 1", result.stdout)

    def test_an_unknown_status_reaches_both_renderings(self) -> None:
        # Repo CLI doctrine: the human and JSON renderings carry the SAME facts.
        # A status this build does not know about is counted in JSON, so the
        # human summary must name it too rather than iterate a fixed list.
        _append_raw(
            _picks_path(self.home),
            {"ticker": "NEW", "date": "2026-08-27", "status": "quarantined"},
        )
        payload = json.loads(_run(self.runner, "--format", "json").stdout)
        self.assertEqual(payload["counts"]["quarantined"], 1)
        human = _run(self.runner).stdout
        self.assertIn("QUARANTINED", human)
        self.assertIn("quarantined 1", human)

    def test_a_row_without_detail_carries_no_trailing_padding(self) -> None:
        # The state column is padded so details line up; a row that has no
        # detail must not ship that padding as trailing whitespace.
        _arm(self.home, "KO", "2026-07-20")
        _submit(self.home, "KO", "2026-07-20")
        body = [line for line in _run(self.runner).stdout.splitlines() if line.startswith("KO")]
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0], body[0].rstrip())

    def test_missing_journal_is_an_honest_empty_read(self) -> None:
        result = _run(self.runner, "--format", "json")
        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["picks"], [])
        self.assertEqual(payload["counts"]["pending"], 0)

    def test_malformed_lines_are_reported_not_fatal(self) -> None:
        path = _picks_path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json\n", encoding="utf-8")
        _arm(self.home, "KO", "2026-07-20")
        result = _run(self.runner, "--format", "json")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.stdout)["malformed"], 1)
        self.assertIn("malformed", result.stderr)


class PicksCommandEnvTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        self.home = _isolate_home(self)

    def test_env_live_reads_the_live_journals_only(self) -> None:
        _arm(self.home, "SIMONLY", "2026-07-20", env="sim")
        _arm(self.home, "LIVEONLY", "2026-08-27", env="live")
        rows = _rows(_run(self.runner, "--env", "live", "--format", "json"))
        self.assertEqual(list(rows), ["LIVEONLY"])

    def test_unknown_env_is_a_usage_refusal(self) -> None:
        result = _run(self.runner, "--env", "paper")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("paper", result.stderr)

    def test_unknown_format_is_a_usage_refusal(self) -> None:
        result = _run(self.runner, "--format", "yaml")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("yaml", result.stderr)

    def test_unknown_state_is_a_usage_refusal(self) -> None:
        result = _run(self.runner, "--state", "queued")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("queued", result.stderr)


if __name__ == "__main__":
    unittest.main()
