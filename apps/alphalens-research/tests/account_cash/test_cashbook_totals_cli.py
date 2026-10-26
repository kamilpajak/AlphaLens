"""``alphalens cashbook totals``: the CLI half of the cash book's answer (#1689).

The numbers themselves are tested in ``tests/account_cash/test_report.py``,
against literals re-derived from the capture. What is tested HERE is only what
a command adds: the one JSON contract (#1379) on this group's second command,
the failure contract (#1389) on one new CLI code, the prose/JSON parity the
repo CLI doctrine requires, and the five properties memo section 6 calls
load-bearing.

**The command computes no money.** Every number it publishes comes off the
record :func:`~alphalens_pipeline.account_cash.report.build_totals` returns,
and two tests below are what make that refutable: one compares the whole
published body against that record's own ``to_jsonable()``, and one hands the
command a record whose lines were replaced with values the store cannot
produce and requires those to be what it prints.

Rows are the LIVE account's own, from
``tests/brokers/automanager/fixtures/trades/live_2026_10_03/venue.json``: 104
bookings rows and 41 trades rows, swept under one sweep covering
2026-08-01..2026-10-03.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from alphalens_pipeline.account_cash import money, report, sweep
from alphalens_pipeline.brokers.account_cash import AccountCashRead
from alphalens_pipeline.brokers.fill_history import ReadWindow
from typer.testing import CliRunner

from tests.account_cash.cashbook_fixture import BALANCE, BOOKINGS, TRADES
from tests.brokers.automanager.home_isolation import IsolatedHomeTestCase

SHOW_SEAM = "alphalens_pipeline.brokers.automanager.unit_env._systemctl_show"
READ_SEAM = "alphalens_pipeline.brokers.automanager.unit_env._read_text"

#: The command module itself, for the gates that read its SOURCE rather than
#: its imported object. One definition, because two spellings of one path is
#: how one of them comes to point at nothing and pass.
_CASHBOOK_SOURCE = (
    Path(__file__).resolve().parents[2]
    / ".."
    / "alphalens-pipeline"
    / "alphalens_cli"
    / "commands"
    / "cashbook.py"
).resolve()

_EXIT_FAILED = 1
_EXIT_USAGE = 2

_LIVE = "live"
#: The window the one seeded sweep ASKED for -- the whole capture.
_SWEPT_SINCE = "2026-08-01"
_SWEPT_UNTIL = "2026-10-03"
_SWEPT_AT = dt.datetime(2026, 10, 4, 0, 31, tzinfo=dt.UTC)

#: The rails a LIVE composition would carry. ``totals`` composes BEST-EFFORT
#: (it holds no broker and places nothing), so these only keep the composed
#: branch exercised rather than the warning one.
_LIVE_RAILS = {
    "ALPHALENS_BROKER_PORTFOLIO_GROSS_FRAC": "1.0",
    "ALPHALENS_BROKER_DAILY_LOSS_LIMIT_R": "1.0",
    "ALPHALENS_BROKER_MAX_PICK_NOTIONAL": "15000",
    "ALPHALENS_BROKER_SIZING_EQUITY_MODE": "declared",
    "ALPHALENS_BROKER_EXIT_POLICY": "breakeven_trail",
    "ALPHALENS_BROKER_MAX_FEE_BPS": "1000",
    "ALPHALENS_BROKER_ENTRY_TRAIL_BPS": "50",
}
_UNIT_PROPERTIES = {
    "LoadState": "loaded",
    "Environment": " ".join(f"{key}={value}" for key, value in _LIVE_RAILS.items()),
    "EnvironmentFiles": "/etc/alphalens/env (ignore_errors=no)",
    "DropInPaths": "",
    "NeedDaemonReload": "no",
}

#: Every top-level key ``alphalens.cashbook.totals/v1`` publishes, stated here
#: rather than derived: an added field inside a major version is a line
#: somebody has to write down. An ALLOWLIST, because a blocklist cannot see
#: the next field's name.
_PUBLISHED_TOP_LEVEL_KEYS = (
    "schema",
    "env",
    "basis",
    "coverage",
    "cost_basis_open_asof",
    "cost_basis_open_excluded",
    "buckets",
    "balance_checks",
    "net_closed_acct",
    "cost_basis_open_acct",
    "income_acct",
    "unclassified_acct",
    "residual_acct",
    "subtotal_acct",
    "deposits_acct",
    "cash_book_sum_acct",
)

#: The key set of one published money value. Stated, not read off
#: :class:`~alphalens_pipeline.account_cash.money.AcctAmount`: reading it back
#: off the dataclass would make the assertion production against production.
_MONEY_LEAF_KEYS = {"value", "currency", "rows", "null_reason", "value_rendered"}

#: How many money values the body carries at least. Eight published lines plus
#: five bucket totals plus three per balance check; a walk that found nothing
#: would satisfy every per-leaf assertion below without it.
_MONEY_LEAF_FLOOR = 10


def _reject_json_constant(token: str) -> None:
    """``json.loads`` accepts NaN / Infinity; a strict reader does not."""
    raise ValueError(f"non-strict JSON constant {token!r} on stdout")


def _strict_json(text: str) -> Any:
    return json.loads(text, parse_constant=_reject_json_constant)


def _json_lines(stderr: str) -> list[dict]:
    found = []
    for line in stderr.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            found.append(value)
    return found


def _failure_from(result) -> dict:
    """The one failure object on a JSON-mode stderr, and it is the LAST line.

    stderr carries operator chrome too (the composition banner, a best-effort
    warning), so the machine rule has two halves: exactly one stderr line is a
    JSON object, AND it is the last line.
    """
    objects = _json_lines(result.stderr)
    assert len(objects) == 1, f"expected exactly one JSON line on stderr, got {objects}"
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert _json_lines(lines[-1]) == objects, (
        f"the failure object must be the LAST stderr line, got {lines[-1]!r}"
    )
    return objects[0]


def _money_leaves(value: Any, prefix: str = "") -> list[tuple[str, dict]]:
    """Every published money value, as ``(dotted path, the object)``."""
    found: list[tuple[str, dict]] = []
    if isinstance(value, dict):
        if set(value) == _MONEY_LEAF_KEYS:
            return [(prefix.rstrip("."), value)]
        for key, child in value.items():
            found.extend(_money_leaves(child, f"{prefix}{key}."))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_money_leaves(child, f"{prefix}{index}."))
    return found


class _CashbookTotalsCase(IsolatedHomeTestCase):
    """Isolated home, a cleared environment, and one seeded sweep."""

    def setUp(self) -> None:
        super().setUp()
        self.runner = CliRunner()
        env_patcher = mock.patch.dict("os.environ", {}, clear=True)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def seed(self, *, since: str = _SWEPT_SINCE, until: str = _SWEPT_UNTIL) -> None:
        """Store the whole capture under one sweep, through the real sweep path."""
        read = AccountCashRead(
            events=tuple(BOOKINGS),
            trades=tuple(TRADES),
            balance=BALANCE,
            events_window=ReadWindow(since, until, len(BOOKINGS)),
            trades_window=ReadWindow(since, until, len(TRADES)),
            skipped_reason=None,
        )
        plan = sweep.plan_sweep(
            read,
            env=_LIVE,
            since=dt.date.fromisoformat(since),
            until=dt.date.fromisoformat(until),
        )
        sweep.apply_sweep(plan, now=_SWEPT_AT)

    def invoke(self, argv: list[str]):
        """Run one cashbook command with the unit-composition seams faked."""
        from alphalens_cli.commands.cashbook import cashbook_app

        with (
            mock.patch(SHOW_SEAM, lambda unit, prop: _UNIT_PROPERTIES[prop]),
            mock.patch(READ_SEAM, lambda _path: ""),
        ):
            return self.runner.invoke(cashbook_app, argv)

    def totals(self, *extra: str, env: str = _LIVE, since: str = _SWEPT_SINCE):
        return self.invoke(
            ["totals", "--since", since, "--until", _SWEPT_UNTIL, "--env", env, *extra]
        )

    def record(self, *, since: str = _SWEPT_SINCE, until: str = _SWEPT_UNTIL):
        """The record the command must render, built by the same function."""
        return report.build_totals(
            env=_LIVE,
            since=dt.date.fromisoformat(since),
            until=dt.date.fromisoformat(until),
        )


class TheEnvelopeIsTheSameContract(_CashbookTotalsCase):
    def setUp(self) -> None:
        super().setUp()
        self.seed()

    def test_the_envelope_leads_with_schema_and_env(self) -> None:
        payload = _strict_json(self.totals("--format", "json").stdout)

        self.assertEqual(list(payload)[:2], ["schema", "env"])
        self.assertEqual(payload["schema"], "alphalens.cashbook.totals/v1")
        self.assertEqual(payload["env"], _LIVE)

    def test_stdout_is_exactly_one_strict_json_value(self) -> None:
        result = self.totals("--format", "json")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(result.stdout.strip().splitlines()), 1)
        self.assertIsInstance(_strict_json(result.stdout), dict)

    def test_the_published_body_keys_are_exactly_this_set(self) -> None:
        payload = _strict_json(self.totals("--format", "json").stdout)

        self.assertEqual(sorted(payload), sorted(_PUBLISHED_TOP_LEVEL_KEYS))

    def test_the_body_is_the_records_own_answer_and_not_a_second_computation(self) -> None:
        """**Mutation it catches:** any number recomputed in the CLI.

        The body is compared against the record's own wire form, so a CLI that
        re-derived one line -- a subtotal, a residual, a bucket -- from the
        others would have to reproduce it exactly or be red here. The envelope
        owns ``schema`` and ``env``, so those two are the only difference.
        """
        payload = _strict_json(self.totals("--format", "json").stdout)

        expected = self.record().to_jsonable()
        expected.pop("env")

        self.assertEqual({key: payload[key] for key in expected}, expected)


class TheCommandRendersTheRecordAndComputesNothing(_CashbookTotalsCase):
    """A record the store cannot produce, to show which side the numbers come from.

    **Mutation it catches:** a CLI that computes a line of its own. Every
    number here disagrees with the seeded store and with every identity over
    it, so a command that recomputed anything would print the store's figure
    and go red.
    """

    ABSURD = -123456.78

    def setUp(self) -> None:
        super().setUp()
        self.seed()
        real = self.record()
        self.doctored = replace(
            real,
            net_closed_acct=replace(real.net_closed_acct, value=self.ABSURD),
            cash_book_sum_acct=replace(real.cash_book_sum_acct, value=self.ABSURD),
        )

    def totals_of_the_doctored_record(self, *extra: str):
        with mock.patch.object(report, "build_totals", return_value=self.doctored):
            return self.totals(*extra)

    def test_the_json_body_carries_the_records_number(self) -> None:
        payload = _strict_json(self.totals_of_the_doctored_record("--format", "json").stdout)

        self.assertEqual(payload["net_closed_acct"]["value"], self.ABSURD)
        self.assertEqual(payload["cash_book_sum_acct"]["value"], self.ABSURD)


class EveryPublishedMoneyValueCarriesItsBasis(_CashbookTotalsCase):
    def setUp(self) -> None:
        super().setUp()
        self.seed()
        self.payload = _strict_json(self.totals("--format", "json").stdout)
        self.leaves = _money_leaves(self.payload)

    def test_the_walk_found_the_bodys_money_values(self) -> None:
        """Positive control: a walk that found nothing would make both
        assertions below vacuous, which is how every such gate rots."""
        self.assertGreaterEqual(len(self.leaves), _MONEY_LEAF_FLOOR)

    def test_every_money_value_carries_its_currency_and_its_rows(self) -> None:
        for path, leaf in self.leaves:
            with self.subTest(field=path):
                self.assertEqual(set(leaf), _MONEY_LEAF_KEYS)

    def test_a_null_money_value_carries_a_reason_and_a_number_does_not(self) -> None:
        """The ``oneOf`` discipline, on the wire rather than in the dataclass.

        **Mutation it catches:** a body hand-built from the record's parts,
        which can publish a ``null`` with no reason (prints as an empty cell)
        or a number beside one (prints as a figure that is also a refusal).
        """
        for path, leaf in self.leaves:
            with self.subTest(field=path):
                self.assertEqual(leaf["value"] is None, leaf["null_reason"] is not None)
                if leaf["null_reason"] is not None:
                    self.assertIn(leaf["null_reason"], money.NULL_REASONS)


class AUsageRefusalRendersTheWayTheCallerAsked(_CashbookTotalsCase):
    def test_an_unknown_format_exits_two_with_stdout_empty(self) -> None:
        result = self.totals("--format", "xml")

        self.assertEqual(result.exit_code, _EXIT_USAGE)
        self.assertEqual(result.stdout, "")

    def test_a_bad_date_exits_two_in_the_format_the_caller_asked_for(self) -> None:
        result = self.invoke(
            [
                "totals",
                "--since",
                "nope",
                "--until",
                _SWEPT_UNTIL,
                "--env",
                _LIVE,
                "--format",
                "json",
            ]
        )

        self.assertEqual(result.exit_code, _EXIT_USAGE)
        self.assertEqual(result.stdout, "")
        failure = _failure_from(result)
        self.assertEqual(failure["code"], "usage")
        self.assertEqual(failure["details"]["option"], "--since")

    def test_an_inverted_window_is_a_usage_error_before_the_store_is_read(self) -> None:
        """**Mutation it catches:** the window check lost, which sends an
        inverted window into ``build_totals`` as a ``ValueError`` traceback."""
        result = self.invoke(
            [
                "totals",
                "--since",
                _SWEPT_UNTIL,
                "--until",
                _SWEPT_SINCE,
                "--env",
                _LIVE,
                "--format",
                "json",
            ]
        )

        self.assertEqual(result.exit_code, _EXIT_USAGE)
        self.assertEqual(result.stdout, "")
        self.assertEqual(_failure_from(result)["code"], "usage")


class AWindowTheStoreCannotAnswerIsAFailureAndNotASmallerNumber(_CashbookTotalsCase):
    """The three refusals ``build_totals`` makes, as one CLI code with a reason.

    A refusal is a non-zero exit with one failure object on stderr and nothing
    on stdout -- the shape ``cashbook sync`` already answers with, and the
    reason it is not a rendered answer carrying a refusal flag: this schema
    publishes no field a client could branch on but never observe, and a
    report whose lines were all null would still read as an answer.

    Each of the three names a different reason from a different branch, so
    none of them dies only together with another.
    """

    def test_a_sim_window_refuses_rather_than_summing_an_empty_store(self) -> None:
        """The store refuses SIM writes, so a SIM store is empty by
        construction and a sum over it answers 0.00 for an account nobody
        read. ``build_totals`` refuses before the first read; this is that
        refusal reaching a caller.
        """
        self.seed()

        result = self.totals("--format", "json", env="sim")

        self.assertEqual(result.exit_code, _EXIT_FAILED)
        self.assertEqual(result.stdout, "")
        failure = _failure_from(result)
        self.assertEqual(failure["code"], "cashbook_totals_refused")
        self.assertEqual(failure["details"], {"reason": "sim_reports_unusable", "env": "sim"})
        self.assertFalse(failure["retryable"])

    def test_a_since_before_the_first_sweep_refuses_rather_than_clamping(self) -> None:
        self.seed()

        result = self.totals("--format", "json", since="2026-07-01")

        self.assertEqual(result.exit_code, _EXIT_FAILED)
        self.assertEqual(result.stdout, "")
        failure = _failure_from(result)
        self.assertEqual(failure["code"], "cashbook_totals_refused")
        self.assertEqual(failure["details"], {"reason": "before_first_sweep", "env": _LIVE})

    def test_an_unswept_store_refuses_nothing_swept(self) -> None:
        result = self.totals("--format", "json")

        self.assertEqual(result.exit_code, _EXIT_FAILED)
        self.assertEqual(result.stdout, "")
        failure = _failure_from(result)
        self.assertEqual(failure["code"], "cashbook_totals_refused")
        self.assertEqual(failure["details"], {"reason": "nothing_swept", "env": _LIVE})

    def test_a_refusal_renders_as_prose_when_the_caller_asked_for_prose(self) -> None:
        result = self.totals()

        self.assertEqual(result.exit_code, _EXIT_FAILED)
        self.assertEqual(result.stdout, "")
        self.assertIn("nothing_swept", result.stderr)

    def test_the_published_reasons_are_the_reports_own_vocabulary(self) -> None:
        """**Mutation it catches:** a fourth reason added to the report and
        left unpublished, or one of these three renamed on one side only. The
        table in the contract README is checked against this same tuple by
        ``tests/brokers/test_broker_failure_reasons_published.py``; this says
        the vocabulary is closed and spells it out, so neither side can be
        read back off the other.
        """
        self.assertEqual(
            report.TOTALS_REFUSALS,
            ("sim_reports_unusable", "before_first_sweep", "nothing_swept"),
        )


class TheCommandHoldsNoBroker(unittest.TestCase):
    """``totals`` answers offline, off the local journals.

    **Mutation it catches:** a vendor read grown into a command that must
    answer with no network and no credentials -- which is also what keeps the
    places-nothing gate with nothing to find here.
    """

    def setUp(self) -> None:
        tree = ast.parse(_CASHBOOK_SOURCE.read_text(encoding="utf-8"))
        self.bodies = {
            node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        }

    def test_the_scan_found_both_commands(self) -> None:
        """Positive control: a renamed function would make the assertion below
        vacuous rather than red."""
        self.assertIn("sync_command", self.bodies)
        self.assertIn("totals_command", self.bodies)

    def test_the_sync_command_does_hold_one(self) -> None:
        """The other direction: the scan can see a broker where there is one,
        so its silence on ``totals`` is a measurement."""
        self.assertIn("_cli_broker", self._names_called("sync_command"))

    def test_the_totals_command_calls_no_broker_builder(self) -> None:
        self.assertNotIn("_cli_broker", self._names_called("totals_command"))

    def _names_called(self, function: str) -> set[str]:
        return {
            node.func.id
            for node in ast.walk(self.bodies[function])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }


if __name__ == "__main__":
    unittest.main()
