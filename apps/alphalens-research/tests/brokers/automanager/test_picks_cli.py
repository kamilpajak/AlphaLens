"""CLI tests for `alphalens broker picks`.

The read-only view over the pick queue (#1197). ``picks.jsonl`` has no
``placed`` status — a pick submitted a month ago still reads ``armed`` as its
latest line forever — so the queue is only readable by performing the SAME
(ticker, trade_date) join against ``submissions.jsonl`` that
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

from tests.brokers.automanager.home_isolation import isolate_home
from tests.brokers.automanager.test_picks import _intent, _torn_utf8_bytes


def _picks_path(home: Path, env: str = "sim") -> Path:
    return home / ".alphalens" / "broker_orders" / env / "picks.jsonl"


def _submissions_path(home: Path, env: str = "sim") -> Path:
    return home / ".alphalens" / "broker_orders" / env / "submissions.jsonl"


def _arm(home: Path, ticker: str, trade_date: str, env: str = "sim") -> None:
    from alphalens_pipeline.brokers.automanager.picks import arm_pick

    arm_pick(_intent(ticker, trade_date), path=_picks_path(home, env))


def _submit(home: Path, ticker: str, trade_date: str, env: str = "sim") -> None:
    path = _submissions_path(home, env)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ticker": ticker.upper(), "trade_date": trade_date}) + "\n")


def _submit_now(
    home: Path,
    ticker: str,
    trade_date: str,
    outcome: str = "placed",
    env: str = "sim",
) -> None:
    """A submission record for an IMMEDIATE tier.

    The shape ``submitted_pick_keys`` skips on purpose (#1247 — the now half
    must not retire a pick before its pullback half is placed) and
    ``keys_with_any_submission`` counts. No test in this file built one before
    the two-fold view, which is why nothing here caught the 2026-10-30 LIVE
    incident.
    """
    path = _submissions_path(home, env)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "ticker": ticker.upper(),
                    "trade_date": trade_date,
                    "tranche": "now",
                    "tranche_meta": {"outcome": outcome},
                }
            )
            + "\n"
        )


def _append_raw(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def _append_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as fh:
        fh.write(payload)


def _run(runner: CliRunner, *args: str):
    from alphalens_cli.commands.broker import broker_app

    return runner.invoke(broker_app, ["picks", *args])


def _rows(result) -> dict[str, dict]:
    payload = json.loads(result.stdout)
    return {row["ticker"]: row for row in payload["picks"]}


class PicksCommandStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        self.home = isolate_home(self)

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

    def test_generations_of_one_ticker_are_separate_rows(self) -> None:
        # #1371: generation 1 disarmed, generation 2 armed and not yet joined —
        # two rows, two states; the human view renders the identity token in
        # the date column so the operator can tell them apart.
        from alphalens_pipeline.brokers.automanager.picks import arm_pick, mark_disarmed

        path = _picks_path(self.home)
        _arm(self.home, "KO", "2026-07-20")
        mark_disarmed("KO", dt.date(2026, 7, 20), note="wrong size", path=path)
        arm_pick(_intent("KO", "2026-07-20", generation=2), path=path)
        _submit(self.home, "KO", "2026-07-20")  # generation 1's record
        result = _run(self.runner, "--format", "json")
        self.assertEqual(result.exit_code, 0, result.output)
        rows = sorted(
            (row["generation"], row["trade_date"], row["state"])
            for row in json.loads(result.stdout)["picks"]
            if row["ticker"] == "KO"
        )
        self.assertEqual(rows, [(1, "2026-07-20", "DISARMED"), (2, "2026-07-20", "PENDING")])
        human = _run(self.runner)
        self.assertIn("2026-07-20-g2  PENDING", human.output)

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


def _wait(home: Path, ticker: str, trade_date: str, gate: str = "gross_cap") -> None:
    from alphalens_pipeline.brokers.automanager.pick_waits import append_wait

    append_wait(
        {
            "pick_key": f"{ticker}:{trade_date}",
            "ticker": ticker,
            "date": trade_date,
            "gate": gate,
            "message": f"{gate}: {ticker} needs more than is free",
            "window_end": "2026-07-29T20:00:00+00:00",
        },
        now=dt.datetime(2026, 7, 20, 15, 0, tzinfo=dt.UTC),
        path=home / ".alphalens" / "broker_orders" / "sim" / "pick_waits.jsonl",
    )


class PicksCommandWaitAndExpiryTest(unittest.TestCase):
    """#1734: an armed pick that does not fit in free capital WAITS, and one
    still unplaced when its window ends EXPIRES. Before v2 a waiting pick read
    PENDING, which promised a placement the gates were refusing every tick."""

    def setUp(self) -> None:
        self.runner = CliRunner()
        self.home = isolate_home(self)

    def test_an_armed_pick_with_a_wait_line_reads_waiting_and_says_why(self) -> None:
        _arm(self.home, "KO", "2026-07-20")
        _wait(self.home, "KO", "2026-07-20")
        row = _rows(_run(self.runner, "--format", "json"))["KO"]
        self.assertEqual(row["state"], "WAITING")
        self.assertEqual(row["wait"]["gate"], "gross_cap")
        self.assertEqual(row["wait"]["since"], "2026-07-20T15:00:00+00:00")
        self.assertIn("needs more than is free", row["detail"])
        self.assertIn("2026-07-20T15:00:00+00:00", row["detail"], "the message says how old it is")

    def test_a_placed_pick_ignores_its_stale_wait_line(self) -> None:
        _arm(self.home, "KO", "2026-07-20")
        _wait(self.home, "KO", "2026-07-20")
        _submit(self.home, "KO", "2026-07-20")
        row = _rows(_run(self.runner, "--format", "json"))["KO"]
        self.assertEqual(row["state"], "PLACED")
        self.assertIsNone(row["wait"])

    def test_an_expired_pick_reads_expired_with_its_reason(self) -> None:
        from alphalens_pipeline.brokers.automanager.picks import mark_expired

        _arm(self.home, "KO", "2026-07-20")
        mark_expired(
            "KO",
            dt.date(2026, 7, 20),
            window_end=dt.datetime(2026, 7, 29, 20, 0, tzinfo=dt.UTC),
            reason="waiting for capital: gross cap",
            path=_picks_path(self.home),
        )
        row = _rows(_run(self.runner, "--format", "json"))["KO"]
        self.assertEqual(row["state"], "EXPIRED")
        self.assertEqual(row["detail"], "waiting for capital: gross cap")

    def test_an_armed_unplaced_row_carries_its_window_and_whether_it_is_overdue(self) -> None:
        # Under KILL or a dead chain the drain does not run, so nothing expires
        # it: the view must not keep calling such a pick waiting as if it could
        # still be placed.
        from unittest import mock

        from alphalens_cli.commands import broker as broker_cli

        _arm(self.home, "KO", "2026-07-20")
        with mock.patch.object(
            broker_cli,
            "_reading_now",
            return_value=dt.datetime(2026, 7, 29, 20, 0, tzinfo=dt.UTC),
        ):
            row = _rows(_run(self.runner, "--format", "json"))["KO"]
        self.assertEqual(row["window_end"], "2026-07-29T20:00:00+00:00")
        self.assertTrue(row["overdue"])

    def test_the_new_states_filter_and_count(self) -> None:
        from alphalens_pipeline.brokers.automanager.picks import mark_expired

        _arm(self.home, "KO", "2026-07-20")
        _wait(self.home, "KO", "2026-07-20")
        mark_expired(
            "MU",
            dt.date(2026, 7, 20),
            window_end=dt.datetime(2026, 7, 29, 20, 0, tzinfo=dt.UTC),
            reason="unplaced at window end",
            path=_picks_path(self.home),
        )
        for state, ticker in (("waiting", "KO"), ("expired", "MU")):
            with self.subTest(state=state):
                result = _run(self.runner, "--format", "json", "--state", state)
                self.assertEqual(result.exit_code, 0, result.output)
                payload = json.loads(result.stdout)
                self.assertEqual([row["ticker"] for row in payload["picks"]], [ticker])
                self.assertEqual(payload["counts"][state], 1)


class PicksCommandOutputTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        self.home = isolate_home(self)

    def test_json_stdout_is_exactly_one_value(self) -> None:
        _arm(self.home, "KO", "2026-07-20")
        result = _run(self.runner, "--format", "json")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema"], "alphalens.broker.picks/v3")
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


class PicksWhileAPickLineCannotBeReadTest(unittest.TestCase):
    """#1788: the drain withholds EVERY placement while one line is unreadable.

    That gate lives in ``iter_picks``, and this view asks the same module which
    keys decode. Two facts must stay apart, or the view lies:

    - whether THIS pick's own line decodes -- the row's state;
    - whether the drain is placing anything at all -- one fact about the queue.

    Reproduced before the split: a readable KO pick rendered UNREADABLE because
    an unrelated ``ZZZ`` line was torn mid-multibyte.
    """

    def setUp(self) -> None:
        self.runner = CliRunner()
        self.home = isolate_home(self)
        self.path = _picks_path(self.home)

    def _tear(self, ticker: str = "ZZZ", trade_date: str = "2026-07-31") -> None:
        _append_bytes(
            self.path,
            _torn_utf8_bytes({"ticker": ticker, "date": trade_date, "status": "armed"}),
        )

    def test_a_readable_pick_keeps_its_own_state_while_another_line_is_torn(self) -> None:
        _arm(self.home, "KO", "2026-07-20")
        self._tear()
        result = _run(self.runner, "--format", "json")
        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["malformed"], 1, "the fixture must reach the counter")
        self.assertEqual(_rows(result)["KO"]["state"], "PENDING")
        self.assertEqual(payload["counts"]["unreadable"], 0)

    def test_a_pick_whose_own_intent_does_not_decode_still_reads_unreadable(self) -> None:
        # The two facts, in one journal: BAD's own line is undecodable, KO's is
        # fine, and a third line is torn. Each row must carry its own verdict.
        _arm(self.home, "KO", "2026-07-20")
        _append_raw(
            self.path,
            {
                "ticker": "BAD",
                "date": "2026-07-02",
                "status": "armed",
                "intent": {"not": "a trade intent"},
            },
        )
        self._tear()
        rows = _rows(_run(self.runner, "--format", "json"))
        self.assertEqual(rows["KO"]["state"], "PENDING")
        self.assertEqual(rows["BAD"]["state"], "UNREADABLE")

    def test_the_report_says_the_queue_is_withheld_in_both_renderings(self) -> None:
        _arm(self.home, "KO", "2026-07-20")
        self._tear()
        payload = json.loads(_run(self.runner, "--format", "json").stdout)
        self.assertIs(payload["placement_withheld"], True)
        human = _run(self.runner)
        self.assertEqual(human.exit_code, 0, human.output)
        self.assertIn("withheld", human.stdout.lower())

    def test_a_queue_that_folds_to_nothing_still_says_it_is_withheld(self) -> None:
        """The state where the operator needs it most, and where the human
        rendering used to drop it.

        A journal whose ONLY line is unreadable folds to zero records, so the
        table has nothing to print and the human view answered `no picks in
        <path>` -- an empty queue, which is not what this is. The JSON carried
        `placement_withheld` all along, so the two renderings disagreed on a
        fact (repo CLI doctrine: they must carry the same ones).
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"not json\n")
        payload = json.loads(_run(self.runner, "--format", "json").stdout)
        self.assertEqual(payload["picks"], [], "the fixture must fold to no records")
        self.assertIs(payload["placement_withheld"], True)

        human = _run(self.runner)
        self.assertEqual(human.exit_code, 0, human.output)
        self.assertIn("withheld", human.stdout.lower())

    def test_a_clean_queue_does_not_claim_the_placements_are_withheld(self) -> None:
        """Positive control: the new fact must be invisible on a real journal."""
        _arm(self.home, "KO", "2026-07-20")
        payload = json.loads(_run(self.runner, "--format", "json").stdout)
        self.assertIs(payload["placement_withheld"], False)
        self.assertEqual(payload["counts"]["pending"], 1)
        human = _run(self.runner)
        self.assertNotIn("withheld", human.stdout.lower())

    def test_the_help_names_the_withholding_and_how_it_clears(self) -> None:
        """The published remedy for UNREADABLE is re-arming, and re-arming does
        NOT release a queue the unreadable-line gate holds -- only a hand
        repair of the journal does. The help must not send the operator down a
        path that cannot work."""
        from alphalens_cli.commands.broker import picks_command

        doc = (picks_command.__doc__ or "").lower()
        self.assertIn("withh", doc, "the help must state that placements stop")
        self.assertIn("repair", doc, "and that a hand repair of the journal is the way out")


class PicksCommandEnvTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        self.home = isolate_home(self)

    def test_without_the_option_the_environment_variable_selects_the_instance(self) -> None:
        # #1377: the default flipped from a hardcoded "sim" to the shared
        # $ALPHALENS_BROKER_ENVIRONMENT seam, so `picks` and `watches` can no
        # longer disagree about which instance a bare invocation reads.
        from unittest import mock

        _arm(self.home, "SIMONLY", "2026-07-20", env="sim")
        _arm(self.home, "LIVEONLY", "2026-08-27", env="live")
        with mock.patch.dict("os.environ", {"ALPHALENS_BROKER_ENVIRONMENT": "live"}):
            payload = json.loads(_run(self.runner, "--format", "json").stdout)
        self.assertEqual(payload["env"], "live")
        self.assertEqual([row["ticker"] for row in payload["picks"]], ["LIVEONLY"])

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


class PicksCommandNowTrancheRecordTest(unittest.TestCase):
    """What the view says when the only submission record for a pick is an
    immediate tier's own ``tranche == "now"`` line.

    The fixtures below vary the submission RECORD, not the pick document: the
    view never decodes a document's entry tiers, it joins keys, so the record's
    shape is the whole input. (``_intent`` builds a single default-``pullback``
    tier; naming these tests after an immediate PICK would describe a fixture
    that does not exist.)

    Measured on LIVE 2026-10-30: NVDA, AMAT and AVGO were each armed with one
    immediate tier, placed and FILLED at 17:49 UTC with resting disaster stops,
    and this view reported PENDING for all three because the state came from the
    drain's retirement fold, which skips exactly that record.
    """

    def setUp(self) -> None:
        self.runner = CliRunner()
        self.home = isolate_home(self)

    def test_a_placed_now_tranche_reads_placed_and_keeps_its_window(self) -> None:
        # The 2026-10-30 incident, reduced, plus the half the one-line swap
        # would have cost. Red before the two-fold view on the state; red on
        # `window_end` if the window is keyed on the existence fold instead.
        _arm(self.home, "NVDA", "2026-10-30")
        _submit_now(self.home, "NVDA", "2026-10-30")
        result = _run(self.runner, "--format", "json")
        self.assertEqual(result.exit_code, 0, result.output)
        row = _rows(result)["NVDA"]
        self.assertEqual(row["state"], "PLACED")
        self.assertIsNotNone(
            row["window_end"],
            "the drain has not retired it, so its window still stands",
        )
        self.assertIn("overdue", row, "the row must still publish the field")
        self.assertFalse(row["overdue"], "its window has not ended yet")

    def test_a_placed_now_tranche_reads_placed_in_human_mode_too(self) -> None:
        # Every other test here reads JSON, and the incident was a person
        # reading a terminal. Kills a renderer that resolves the state twice.
        _arm(self.home, "NVDA", "2026-10-30")
        _submit_now(self.home, "NVDA", "2026-10-30")
        result = _run(self.runner)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertRegex(result.stdout, r"NVDA\s+2026-10-30\s+PLACED")
        self.assertNotIn("PENDING", result.stdout)

    def test_a_pick_the_capital_gate_holds_keeps_its_message_when_placed(self) -> None:
        # Kills passing the existence fold to `open_waits`. The pick is PLACED
        # because a record exists, and the gate that still holds the rest of it
        # must stay on the row — the swap drops the whole message.
        _arm(self.home, "KO", "2026-07-20")
        _wait(self.home, "KO", "2026-07-20")
        _submit_now(self.home, "KO", "2026-07-20")
        row = _rows(_run(self.runner, "--format", "json"))["KO"]
        self.assertEqual(row["state"], "PLACED")
        self.assertIsNotNone(row["wait"], "the drain still owes the rest of it")
        self.assertIn("needs more than is free", row["detail"])

    def test_a_now_tranche_refused_above_cap_stays_pending(self) -> None:
        # `_NO_ORDER_OUTCOMES` is what keeps a tranche that was never submitted
        # out of PLACED. `test_picks.py` already pins the fold itself; this
        # pins that the VIEW inherits it, which is the new coupling.
        _arm(self.home, "NVDA", "2026-10-30")
        _submit_now(self.home, "NVDA", "2026-10-30", outcome="refused_cap")
        row = _rows(_run(self.runner, "--format", "json"))["NVDA"]
        self.assertEqual(row["state"], "PENDING")

    def test_an_unhashable_outcome_does_not_crash_the_view(self) -> None:
        # The existence fold tests membership in a frozenset, so a non-string
        # outcome off DISK used to raise `TypeError: unhashable type: 'list'`.
        # Before the two-fold view this command never called that fold, so the
        # record rendered harmlessly; a read-only command must not start
        # crashing with an empty stdout on a journal line it can skip.
        _arm(self.home, "NVDA", "2026-10-30")
        _append_raw(
            _submissions_path(self.home),
            {
                "ticker": "NVDA",
                "trade_date": "2026-10-30",
                "tranche": "now",
                "tranche_meta": {"outcome": ["placed"]},
            },
        )
        result = _run(self.runner, "--format", "json")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            _rows(result)["NVDA"]["state"],
            "PLACED",
            "an outcome we cannot read means an order MAY exist",
        )

    def test_the_retirement_fold_never_holds_a_key_the_existence_fold_lacks(self) -> None:
        # The split keys the state on one fold and the window on the other, and
        # nothing in either fold enforces an order between them: retirement
        # filters on `tranche`, existence on `tranche_meta.outcome`. A record
        # with no `tranche` and a refusal outcome would sit in the retirement
        # set alone, and such a pick reads PENDING — "the drain will place it" —
        # about a pick the drain has retired, with no window to hint at it.
        # No writer emits that shape today; this fails the moment one does.
        from alphalens_pipeline.brokers.automanager.picks import (
            keys_with_any_submission,
            submitted_pick_keys,
        )

        shapes = [
            {"ticker": "KO", "trade_date": "2026-07-20", "tranche": "now"},
            {
                "ticker": "KO",
                "trade_date": "2026-07-20",
                "tranche": "now",
                "tranche_meta": {"outcome": "placed"},
            },
            {
                "ticker": "KO",
                "trade_date": "2026-07-20",
                "tranche": "now",
                "tranche_meta": {"outcome": "attempt"},
            },
            {"ticker": "KO", "trade_date": "2026-07-20"},
            {
                "ticker": "KO",
                "trade_date": "2026-07-20",
                "tranche_meta": {"outcome": "placed"},
            },
            # The shape that actually broke the inclusion, and the reason
            # `_records_an_order` consults the outcome only for a now tranche:
            # `refused_cap` is the now vocabulary, and a pullback rung has no
            # operator cap to refuse. Without this row the test passes while
            # proving nothing.
            {
                "ticker": "KO",
                "trade_date": "2026-07-20",
                "tranche_meta": {"outcome": "refused_cap"},
            },
            {
                "ticker": "KO",
                "trade_date": "2026-07-20",
                "tranche_meta": {"outcome": ["refused_cap"]},
            },
        ]
        for record in shapes:
            with self.subTest(record=record):
                retired = submitted_pick_keys([record])
                at_broker = keys_with_any_submission([record])
                self.assertLessEqual(
                    retired,
                    at_broker,
                    "a retired key the existence fold lacks reads PENDING about a "
                    "pick the drain is done with",
                )

    def test_the_view_calls_both_folds_and_its_help_names_them(self) -> None:
        # Drift in BOTH directions: code that stops calling one fold, and help
        # that keeps claiming the view performs only the drain's join. The body
        # is stripped of comments first — a comment naming a fold must not
        # satisfy "the view calls it".
        import inspect

        from alphalens_cli.commands.broker import picks_command

        doc = picks_command.__doc__ or ""
        source = inspect.getsource(picks_command).replace(doc, "")
        body = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        for fold in ("keys_with_any_submission", "submitted_pick_keys"):
            self.assertIn(f"{fold}(", body, f"the view must CALL {fold}")
            self.assertIn(fold, doc, f"the published help must name {fold}")

    def test_the_help_refuses_to_call_placed_a_venue_claim(self) -> None:
        # The paragraph that does the real work of this change was unpinned:
        # deleting it left the whole suite green. It is the only thing standing
        # between PLACED and an operator reading it as "my order is at the
        # venue", which it is not, in three separate ways.
        from alphalens_cli.commands.broker import picks_command

        doc = picks_command.__doc__ or ""
        self.assertIn("NOT A VENUE CLAIM", doc.upper())
        self.assertIn("entry_trails.jsonl", doc, "the journal this command does not read")
        self.assertIn("broker orders", doc, "where the venue question IS answered")
        self.assertNotIn(
            "use ``broker reconcile``",
            doc,
            "reconcile reads submissions.jsonl, so it cannot answer it either",
        )


if __name__ == "__main__":
    unittest.main()
