"""``alphalens cashbook sync``: the CLI half of one sweep (#1689, memo section 3).

The store, the folds and the sweep are tested in this package's other modules.
What is tested HERE is only what a command adds: the one JSON contract (#1379)
on a second group, the failure contract (#1389) on two new CLI codes, and the
one branch that makes ``--dry-run`` honest.

**The payload is sweep facts only.** No money total, no bucket, no
profit-and-loss number — those are memo Phase 4, and publishing one now inside
``alphalens.cashbook.sync/v1`` would mean changing the meaning of a field
inside a major version later.

Rows are the LIVE account's own, from
``tests/brokers/automanager/fixtures/trades/live_2026_10_03/venue.json``: 104
bookings rows and 41 trades rows.
"""

from __future__ import annotations

import ast
import datetime as dt
import fcntl
import json
import os
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from alphalens_pipeline.account_cash import paths, store, sweep
from alphalens_pipeline.brokers.account_cash import (
    AccountCashRead,
    BalanceSnapshot,
    CashEvent,
    TradeRow,
)
from typer.testing import CliRunner

from tests.brokers.automanager.home_isolation import IsolatedHomeTestCase

REGISTRY_SEAM = "alphalens_pipeline.brokers.registry.get_default_broker"
LIVE_FACTORY_SEAM = "alphalens_pipeline.brokers.saxo.broker.create_saxo_broker_live_from_env"
SHOW_SEAM = "alphalens_pipeline.brokers.automanager.unit_env._systemctl_show"
READ_SEAM = "alphalens_pipeline.brokers.automanager.unit_env._read_text"

#: The exit statuses this group answers, from the repo CLI doctrine. They are
#: asserted by VALUE rather than as "non-zero": the status is the one thing a
#: shell caller reads without parsing the JSON, and 7 vs 1 is what tells it
#: whether re-running is safe without reconciling anything first.
_EXIT_FAILED = 1
_EXIT_USAGE = 2
_EXIT_TRANSIENT = 7

_SINCE = "2026-08-01"
_UNTIL = "2026-10-03"
_NOW = dt.datetime(2026, 10, 9, 0, 31, tzinfo=dt.UTC)

_VENUE = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "brokers"
        / "automanager"
        / "fixtures"
        / "trades"
        / "live_2026_10_03"
        / "venue.json"
    ).read_text(encoding="utf-8")
)

_EVENTS = tuple(CashEvent.from_vendor_row(row) for row in _VENUE["bookings"])
_TRADES = tuple(TradeRow.from_vendor_row(row) for row in _VENUE["trades"])

_BALANCE = BalanceSnapshot(
    currency="PLN",
    cash_balance=20738.68,
    non_margin_positions_value=3422.56,
    total_value=24161.24,
    transactions_not_booked=0.0,
    calculation_reliability="Ok",
    asof=_NOW,
)

# The rails the LIVE boot-assert requires (ADR 0017), so `--env live` composes
# and resolves a gateway the way it does on the VPS. Copied from
# `tests/brokers/automanager/test_broker_json_contract_cli.py`; nothing is armed.
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


#: Every key ``alphalens.cashbook.sync/v1`` publishes, flattened. Stated here
#: rather than derived, so a new field is a line somebody writes in a test.
_PUBLISHED_KEYS = (
    "schema",
    "env",
    "dry_run",
    "synced",
    "asked",
    "asked.since",
    "asked.until",
    "read",
    "read.events",
    "read.events.start",
    "read.events.end",
    "read.events.rows",
    "read.trades",
    "read.trades.start",
    "read.trades.end",
    "read.trades.rows",
    "read.balance_asof",
    "appended",
    "appended.cash_events",
    "appended.trade_rows",
    "appended.balances",
    "appended.sweeps",
    "already_present",
    "already_present.cash_events",
    "already_present.trade_rows",
    "restated",
    "restated.cash_events",
    "restated.trade_rows",
    "malformed",
    "malformed.cash_events",
    "malformed.trade_rows",
    "journals",
    "journals.cash_events",
    "journals.trade_rows",
    "journals.balances",
    "journals.sweeps",
)


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

    stderr carries operator chrome too (the ``env=… gateway=…`` banner), so the
    machine rule has two halves: exactly one stderr line is a JSON object, AND
    it is the last line. Both are asserted here rather than in one test,
    because a caller that reads the last line is broken by chrome printed
    after the object no matter which refusal produced it.
    """
    objects = _json_lines(result.stderr)
    assert len(objects) == 1, f"expected exactly one JSON line on stderr, got {objects}"
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert _json_lines(lines[-1]) == objects, (
        f"the failure object must be the LAST stderr line, got {lines[-1]!r}"
    )
    return objects[0]


class _FakeCashBroker:
    """A broker that answers ``SupportsAccountCash`` from the fixture."""

    name = "fake"

    def __init__(self, read: AccountCashRead | None = None) -> None:
        self.read = read if read is not None else _live_read()
        self.calls: list[tuple[dt.date, dt.date]] = []

    def list_account_cash(self, since: dt.date, until: dt.date) -> AccountCashRead:
        self.calls.append((since, until))
        return self.read


class _FakeBrokerWithoutTheCapability:
    """A broker that offers everything except the cash read."""

    name = "fake-no-cash"


def _live_read() -> AccountCashRead:
    from alphalens_pipeline.brokers.fill_history import ReadWindow

    return AccountCashRead(
        events=_EVENTS,
        trades=_TRADES,
        balance=_BALANCE,
        events_window=ReadWindow(_SINCE, _UNTIL, len(_EVENTS)),
        trades_window=ReadWindow(_SINCE, _UNTIL, len(_TRADES)),
        skipped_reason=None,
    )


def _sim_read() -> AccountCashRead:
    """What the real adapter answers on SIM: nothing read, and why."""
    return AccountCashRead(
        events=None,
        trades=None,
        balance=None,
        skipped_reason="sim_reports_unusable",
    )


class _CashbookCliCase(IsolatedHomeTestCase):
    """Isolated home, a cleared environment, and a cash-capable broker."""

    def setUp(self) -> None:
        super().setUp()
        self.runner = CliRunner()
        self.broker: Any = _FakeCashBroker()
        env_patcher = mock.patch.dict("os.environ", {}, clear=True)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def invoke(self, argv: list[str]):
        """Run one cashbook command with the SIM and LIVE gateways both faked.

        The unit-composition seams are installed on every call, not just the
        LIVE ones: patching them is inert on a SIM read and it keeps one entry
        point for every test below.
        """
        from alphalens_cli.commands.cashbook import cashbook_app

        with (
            mock.patch(SHOW_SEAM, lambda unit, prop: _UNIT_PROPERTIES[prop]),
            mock.patch(READ_SEAM, lambda _path: ""),
            mock.patch(LIVE_FACTORY_SEAM, return_value=(self.broker, mock.Mock())),
            mock.patch(REGISTRY_SEAM, return_value=self.broker),
        ):
            return self.runner.invoke(cashbook_app, argv)

    def sync(self, *extra: str, env: str = "live"):
        return self.invoke(["sync", "--since", _SINCE, "--until", _UNTIL, "--env", env, *extra])

    def journal_files(self) -> dict[str, bytes]:
        root = paths.account_cash_root("live")
        if not root.exists():
            return {}
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }


class ADryRunPlansAndWritesNothing(_CashbookCliCase):
    def test_dry_run_renders_the_plan_and_appends_nothing(self) -> None:
        result = self.sync("--dry-run", "--format", "json")

        self.assertEqual(result.exit_code, 0, result.output)
        payload = _strict_json(result.stdout)
        self.assertTrue(payload["dry_run"])
        self.assertFalse(payload["synced"])
        self.assertEqual(payload["appended"]["cash_events"], len(_EVENTS))
        self.assertEqual(self.journal_files(), {})

    def test_the_human_default_renders_prose_and_appends_nothing(self) -> None:
        result = self.sync("--dry-run")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("dry run", result.stdout)
        self.assertEqual(self.journal_files(), {})

    def test_the_human_rendering_carries_the_same_facts_as_the_json_one(self) -> None:
        """The repo CLI doctrine: one internal result rendered two ways, and
        the two must carry the same facts. Asserted against the JSON body of
        the same invocation rather than against literals, so the claim is
        "nothing was lost in the prose" and not "the prose says these words".
        Without it the renderer could print a window BOUND where its row count
        belongs and only the substring 'dry run' would be checked.
        """
        payload = _strict_json(self.sync("--dry-run", "--format", "json").stdout)

        human = self.sync("--dry-run").stdout

        self.assertIn(payload["asked"]["since"], human)
        self.assertIn(payload["asked"]["until"], human)
        for name in ("events", "trades"):
            window = payload["read"][name]
            with self.subTest(window=name):
                self.assertIn(f"{window['rows']} row(s)", human)
                self.assertIn(window["start"], human)
                self.assertIn(window["end"], human)
        self.assertIn(payload["read"]["balance_asof"], human)
        for label in ("appended", "already_present", "restated", "malformed"):
            for journal, count in payload[label].items():
                with self.subTest(counts=label, journal=journal):
                    self.assertIn(f"{journal} {count}", human)


class TheEnvelopeIsTheSameContract(_CashbookCliCase):
    def test_the_envelope_leads_with_schema_and_env(self) -> None:
        payload = _strict_json(self.sync("--dry-run", "--format", "json").stdout)

        self.assertEqual(list(payload)[:2], ["schema", "env"])
        self.assertEqual(payload["schema"], "alphalens.cashbook.sync/v1")
        self.assertEqual(payload["env"], "live")

    def test_stdout_is_exactly_one_strict_json_value(self) -> None:
        result = self.sync("--dry-run", "--format", "json")

        self.assertEqual(len(result.stdout.strip().splitlines()), 1)
        self.assertIsInstance(_strict_json(result.stdout), dict)

    def test_the_published_body_keys_are_exactly_this_set(self) -> None:
        """An ALLOWLIST, because a blocklist cannot see the next field's name.

        ``test_the_payload_publishes_no_money_total`` below refuses five
        substrings; it let ``realized_cash_acct`` through, which is the memo's
        own word for the number Phase 4 owns. The required-keys table in
        ``test_broker_json_contract_cli.py`` is a subset check, so it allows
        extra keys too. This states the whole published body instead, so adding
        a field or dropping one is a decision somebody has to write down here.
        """
        payload = _strict_json(self.sync("--dry-run", "--format", "json").stdout)

        self.assertEqual(sorted(_every_key(payload)), sorted(_PUBLISHED_KEYS))

    def test_the_payload_publishes_no_money_total(self) -> None:
        """Memo decision D3: sweep facts only, so Phase 4 can add totals with
        their own basis instead of redefining a field already published.

        Kept beside the allowlist above and not replaced by it: this one says
        WHY the set is closed, and it reads the payload a real invocation
        printed rather than the set a test states.
        """
        payload = _strict_json(self.sync("--dry-run", "--format", "json").stdout)

        forbidden = ("total", "pnl", "net", "amount", "bucket", "realized")
        for key in _every_key(payload):
            for token in forbidden:
                self.assertNotIn(token, key, f"{key!r} reads as a money field")

    def test_the_window_published_is_the_one_the_vendor_served(self) -> None:
        """Not the one the caller asked for: the two differ exactly when the
        read did not answer the question."""
        from alphalens_pipeline.brokers.fill_history import ReadWindow

        self.broker = _FakeCashBroker(
            AccountCashRead(
                events=_EVENTS,
                trades=_TRADES,
                balance=_BALANCE,
                events_window=ReadWindow("2026-09-01", "2026-09-30", 7),
                trades_window=ReadWindow("2026-09-01", "2026-09-30", 3),
            )
        )
        payload = _strict_json(self.sync("--dry-run", "--format", "json").stdout)

        self.assertEqual(payload["asked"], {"since": _SINCE, "until": _UNTIL})
        self.assertEqual(payload["read"]["events"]["start"], "2026-09-01")
        self.assertEqual(payload["read"]["events"]["end"], "2026-09-30")
        self.assertEqual(payload["read"]["events"]["rows"], 7)
        # Both windows, not just the first: they carry different row counts
        # here, so publishing the events window under `read.trades` would be
        # invisible to an assertion on one of them.
        self.assertEqual(payload["read"]["trades"]["start"], "2026-09-01")
        self.assertEqual(payload["read"]["trades"]["end"], "2026-09-30")
        self.assertEqual(payload["read"]["trades"]["rows"], 3)

    def test_the_balance_stamp_published_is_the_one_the_read_carried(self) -> None:
        """``read.balance_asof`` is the only published fact about the balance in
        this schema, and the cross-check that will use it is Phase 4's. A
        hard-coded ``None`` here would read as "the account has no balance
        snapshot", which is a different sweep."""
        payload = _strict_json(self.sync("--dry-run", "--format", "json").stdout)

        self.assertEqual(payload["read"]["balance_asof"], _NOW.isoformat())

    def test_a_line_the_fold_cannot_read_is_published_as_malformed(self) -> None:
        """Counted, never dropped: a corrupt line in a money journal is the one
        thing a sweep must not pass over quietly. Asserted on a VALUE, because
        the key alone survives a hard-coded empty mapping."""
        journal = paths.cash_events_path("live")
        journal.parent.mkdir(parents=True)
        journal.write_text("not json at all\n", encoding="utf-8")

        payload = _strict_json(self.sync("--dry-run", "--format", "json").stdout)

        self.assertEqual(payload["malformed"], {"cash_events": 1, "trade_rows": 0})


def _every_key(value: Any, prefix: str = "") -> list[str]:
    """Every key in a nested payload, so a money field cannot hide one level down."""
    keys: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            keys.append(f"{prefix}{key}")
            keys.extend(_every_key(child, f"{prefix}{key}."))
    elif isinstance(value, list):
        for child in value:
            keys.extend(_every_key(child, prefix))
    return keys


class AUsageRefusalRendersTheWayTheCallerAsked(_CashbookCliCase):
    def test_an_unknown_format_exits_two_with_stdout_empty(self) -> None:
        result = self.sync("--dry-run", "--format", "xml")

        self.assertEqual(result.exit_code, _EXIT_USAGE)
        self.assertEqual(result.stdout, "")

    def test_a_bad_date_exits_two_in_the_format_the_caller_asked_for(self) -> None:
        result = self.invoke(
            ["sync", "--since", "nope", "--until", _UNTIL, "--format", "json", "--dry-run"]
        )

        self.assertEqual(result.exit_code, _EXIT_USAGE)
        self.assertEqual(result.stdout, "")
        failure = _failure_from(result)
        self.assertEqual(failure["code"], "usage")
        self.assertEqual(failure["details"]["option"], "--since")

    def test_since_after_until_is_refused(self) -> None:
        result = self.invoke(
            ["sync", "--since", _UNTIL, "--until", _SINCE, "--format", "json", "--dry-run"]
        )

        self.assertEqual(result.exit_code, _EXIT_USAGE)
        self.assertEqual(result.stdout, "")
        failure = _failure_from(result)
        self.assertEqual(failure["code"], "usage")
        self.assertEqual(failure["details"]["option"], "--since")

    def test_an_unknown_instance_name_is_refused_before_a_vendor_is_touched(self) -> None:
        """``--env prod`` dies in ``_apply_env_option``, the broker group's own
        door, and keeps that door's message: ``state_paths`` names the VARIABLE
        and the offending value (D1), and the same sentence is what an explicit
        option gets. A second spelling here would make this the only command in
        the family that answers differently for one refusal."""
        result = self.invoke(
            ["sync", "--since", _SINCE, "--until", _UNTIL, "--env", "prod", "--format", "json"]
        )

        self.assertEqual(result.exit_code, _EXIT_USAGE)
        self.assertEqual(result.stdout, "")
        failure = _failure_from(result)
        self.assertEqual(failure["code"], "usage")
        self.assertIn("prod", failure["message"])
        self.assertEqual(self.broker.calls, [])

    def test_an_unknown_ambient_instance_is_refused_by_this_command_itself(self) -> None:
        """The other door, and the one this module owns: with no ``--env`` the
        instance comes from the ambient variable, which ``_apply_env_option``
        never validates. This command resolves it itself, before any broker,
        because it needs the value for the store root — so without its own
        guard the ``ValueError`` escapes as a traceback at exit 1 instead of a
        usage object, which is the mutation this test exists to catch."""
        os.environ["ALPHALENS_BROKER_ENVIRONMENT"] = "prod"

        result = self.invoke(
            ["sync", "--since", _SINCE, "--until", _UNTIL, "--format", "json", "--dry-run"]
        )

        self.assertEqual(result.exit_code, _EXIT_USAGE)
        self.assertEqual(result.stdout, "")
        failure = _failure_from(result)
        self.assertEqual(failure["code"], "usage")
        self.assertIn("prod", failure["message"])
        self.assertEqual(self.broker.calls, [])


class TheRefusalsCarryTheirOwnCode(_CashbookCliCase):
    def test_on_sim_it_refuses_and_prints_nothing_on_stdout(self) -> None:
        """Memo section 3: the SIM report endpoints answer with canned rows from
        OTHER accounts, so the read answers nothing and the sweep refuses. Exit
        0 with a zero result would publish fiction as this account's cash."""
        self.broker = _FakeCashBroker(_sim_read())
        result = self.sync("--format", "json", env="sim")

        self.assertEqual(result.exit_code, _EXIT_FAILED)
        self.assertEqual(result.stdout, "")
        failure = _failure_from(result)
        self.assertEqual(failure["code"], "cashbook_sync_refused")
        self.assertEqual(failure["details"]["reason"], "sim_reports_unusable")
        self.assertFalse(failure["retryable"])

    def test_a_write_aimed_at_sim_is_refused_with_the_store_reason(self) -> None:
        """The other half of the same code: the read answered rows (a fake can),
        and the WRITE guard refuses them. One code, two reasons."""
        result = self.sync("--format", "json", env="sim")

        self.assertEqual(result.exit_code, _EXIT_FAILED)
        self.assertEqual(result.stdout, "")
        failure = _failure_from(result)
        self.assertEqual(failure["code"], "cashbook_sync_refused")
        self.assertEqual(failure["details"]["reason"], "sim_store_refused")
        self.assertEqual(paths.account_cash_root("sim").exists(), False)

    def test_a_dry_run_on_sim_refuses_exactly_as_the_real_run_does(self) -> None:
        """A dry run AUTHORISES. The repo CLI doctrine spells the step out —
        ``--dry-run`` does everything except mutate: parse, validate,
        authorise, resolve targets, plan — and the docstring promises nothing
        is stored on SIM "in both directions".

        Measured before the fix: this invocation exited 0 and published
        ``appended: {cash_events: 104, …}`` plus four
        ``account_cash/sim/*.jsonl`` paths the store guarantees never exist,
        while the same invocation without ``--dry-run`` exited 1 with
        ``sim_store_refused``. The two renderings of one sweep disagreeing
        about whether it can happen is the whole defect, so the assertion is
        that the two answers are the SAME object rather than two literals.
        """
        dry = self.sync("--dry-run", "--format", "json", env="sim")
        wet = self.sync("--format", "json", env="sim")

        self.assertEqual(dry.exit_code, wet.exit_code)
        self.assertEqual(dry.exit_code, _EXIT_FAILED)
        self.assertEqual(dry.stdout, "")
        self.assertEqual(_failure_from(dry), _failure_from(wet))
        self.assertEqual(_failure_from(dry)["details"]["reason"], "sim_store_refused")
        self.assertFalse(paths.account_cash_root("sim").exists())

    def test_the_authorisation_runs_after_the_read_so_sim_still_names_the_read(self) -> None:
        """Order, not just presence. The real SIM adapter answers nothing and
        says why (``sim_reports_unusable``), which is the more specific fact
        about that invocation; authorising before the read would replace it
        with ``sim_store_refused`` and tell an operator the store refused a
        read that never happened."""
        self.broker = _FakeCashBroker(_sim_read())

        result = self.sync("--dry-run", "--format", "json", env="sim")

        self.assertEqual(result.exit_code, _EXIT_FAILED)
        self.assertEqual(_failure_from(result)["details"]["reason"], "sim_reports_unusable")

    def test_a_broker_without_the_capability_is_refused_as_broker_unsupported(self) -> None:
        """A contract concept keeps the contract's code (#1122)."""
        self.broker = _FakeBrokerWithoutTheCapability()
        result = self.sync("--format", "json")

        self.assertEqual(result.exit_code, _EXIT_FAILED)
        self.assertEqual(result.stdout, "")
        self.assertEqual(_failure_from(result)["code"], "broker_unsupported")

    def test_a_held_lock_refuses_as_sweep_busy_with_nothing_appended(self) -> None:
        """Retryable, because nothing was appended: re-running is safe without
        reconciling anything first."""
        lock = paths.sweep_lock_path("live")
        lock.parent.mkdir(parents=True, exist_ok=True)
        holder = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)

        with mock.patch.object(sweep, "_LOCK_ACQUIRE_TIMEOUT_S", 0.0):
            result = self.sync("--format", "json")

        self.assertEqual(result.exit_code, _EXIT_TRANSIENT)
        self.assertEqual(result.stdout, "")
        failure = _failure_from(result)
        self.assertEqual(failure["code"], "cashbook_sweep_busy")
        self.assertTrue(failure["retryable"])
        self.assertEqual(failure["details"]["lock"], str(lock))
        self.assertEqual(sorted(self.journal_files()), [".sweep.lock"])

    def test_an_unrenderable_envelope_refuses_without_appending(self) -> None:
        """Rendering happens BEFORE the append, so an unrenderable body is a
        refusal rather than a failure report for something that already
        happened — the ``write_outcome_unknown`` shape."""
        from alphalens_cli.commands import cashbook as cashbook_module

        with mock.patch.object(cashbook_module, "_render_json", side_effect=_cannot_render):
            result = self.sync("--format", "json")

        self.assertEqual(result.exit_code, _EXIT_FAILED)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.journal_files(), {})

    def test_a_failed_append_reports_queue_write_failed_not_a_traceback(self) -> None:
        """The first journal of the sequence is unwritable, the lock is not: the
        refusal must name the file it could not append to.

        The MESSAGE is asserted, not only the code: a sweep touches the lock and
        then four journals, both failures report ``queue_write_failed``, and
        ``details.journal`` resolves to the same file through the wrapped
        ``OSError``'s own ``filename``. So the message is the only thing that
        tells an operator whether to look at the lock or at the journal, and
        without this assertion the append branch can be deleted outright.
        """
        journal = paths.cash_events_path("live")
        journal.parent.mkdir(parents=True)
        journal.touch()
        journal.chmod(0o400)
        self.addCleanup(journal.chmod, 0o600)

        result = self.sync("--format", "json")

        self.assertEqual(result.exit_code, _EXIT_TRANSIENT)
        self.assertEqual(result.stdout, "")
        failure = _failure_from(result)
        self.assertEqual(failure["code"], "queue_write_failed")
        self.assertTrue(failure["retryable"])
        self.assertEqual(failure["details"]["journal"], str(journal))
        self.assertIn("could not append to the account cash book", failure["message"])

    def test_an_unwritable_store_root_refuses_at_the_lock_with_the_same_code(self) -> None:
        """The lock is taken BEFORE the first append, so an unwritable root
        fails there. Same failure mode, same code: no write happened, nothing is
        in flight, and a re-run is safe once the cause clears.

        The lock path is published under ``details.lock``, which is the key the
        lock already owns in this contract (``cashbook_sweep_busy`` publishes
        it), and ``details.journal`` stays null — the published row says that
        key names the journal an append failed on, and the lock is neither a
        journal nor a file anything appended to. A machine consumer branching
        on ``details.journal``, or an operator reading only that field, was
        being pointed at the lock.
        """
        root = paths.account_cash_root("live")
        root.mkdir(parents=True)
        root.chmod(0o500)
        self.addCleanup(root.chmod, 0o700)

        result = self.sync("--format", "json")

        self.assertEqual(result.exit_code, _EXIT_TRANSIENT)
        self.assertEqual(result.stdout, "")
        failure = _failure_from(result)
        self.assertEqual(failure["code"], "queue_write_failed")
        self.assertEqual(failure["details"]["lock"], str(paths.sweep_lock_path("live")))
        self.assertIsNone(failure["details"]["journal"])
        self.assertIn("sweep lock", failure["message"])

    def test_a_failed_append_names_no_lock(self) -> None:
        """The other half: the two arms must not both publish both keys, or
        neither field tells an operator which of the two failed."""
        journal = paths.cash_events_path("live")
        journal.parent.mkdir(parents=True)
        journal.touch()
        journal.chmod(0o400)
        self.addCleanup(journal.chmod, 0o600)

        failure = _failure_from(self.sync("--format", "json"))

        self.assertEqual(failure["details"]["journal"], str(journal))
        self.assertNotIn("lock", failure["details"])


def _cannot_render(payload: Any) -> None:
    from alphalens_cli.commands._cli_envelope import _fail_with

    raise _fail_with("unclassified", "cannot render this result as JSON: boom")


class ARealRunAppendsWhatItPublished(_CashbookCliCase):
    def test_the_published_counts_are_the_record_the_sweep_wrote(self) -> None:
        """The body is rendered from the PLAN and the record is written by the
        sweep, so the two arithmetics must agree. Without this the CLI could
        publish a count the journal does not hold."""
        payload = _strict_json(self.sync("--format", "json").stdout)

        records = store.read_sweeps(env="live")
        self.assertEqual(len(records), 1)
        self.assertEqual(payload["appended"], dict(records[0].appended))
        self.assertEqual(payload["already_present"], dict(records[0].already_present))
        self.assertEqual(payload["restated"], dict(records[0].restated))
        self.assertTrue(payload["synced"])
        self.assertFalse(payload["dry_run"])

    def test_a_sweep_that_restates_a_row_publishes_the_restatement_it_appended(self) -> None:
        """The shape the journal exists for, and the one every other test here
        misses: each of them runs a FIRST sweep over an empty store, where
        ``restated`` is zero, so the restatement term of this command's own
        ``appended`` arithmetic was never exercised. The vendor correcting one
        booking it already served is the memo's own reason the store is
        append-only rather than a parquet rewrite.
        """
        self.sync("--format", "json")
        corrected = replace(_EVENTS[0], amount_account_currency=-123.45)
        corrected_trade = replace(_TRADES[0], booked_amount_account_currency=-99.5)
        self.broker = _FakeCashBroker(
            replace(
                _live_read(),
                events=(corrected, *_EVENTS[1:]),
                trades=(corrected_trade, *_TRADES[1:]),
            )
        )

        payload = _strict_json(self.sync("--format", "json").stdout)

        self.assertEqual(payload["restated"], {"cash_events": 1, "trade_rows": 1})
        self.assertEqual(payload["appended"]["cash_events"], 1)
        self.assertEqual(payload["appended"]["trade_rows"], 1)
        self.assertEqual(payload["already_present"]["cash_events"], len(_EVENTS) - 1)
        self.assertEqual(payload["already_present"]["trade_rows"], len(_TRADES) - 1)
        records = store.read_sweeps(env="live")
        self.assertEqual(len(records), 2)
        self.assertEqual(payload["appended"], dict(records[-1].appended))
        self.assertEqual(payload["restated"], dict(records[-1].restated))
        folded = {event.bk_amount_id: event for event in store.read_cash_events(env="live").events}
        self.assertEqual(folded[corrected.bk_amount_id].amount_account_currency, -123.45)

    def test_a_second_sweep_of_the_same_window_appends_nothing_new(self) -> None:
        self.sync("--format", "json")
        payload = _strict_json(self.sync("--format", "json").stdout)

        self.assertEqual(payload["appended"]["cash_events"], 0)
        self.assertEqual(payload["already_present"]["cash_events"], len(_EVENTS))

    def test_the_journal_paths_it_names_are_the_ones_it_wrote(self) -> None:
        payload = _strict_json(self.sync("--format", "json").stdout)

        for name in ("cash_events", "trade_rows", "balances", "sweeps"):
            with self.subTest(journal=name):
                self.assertTrue(Path(payload["journals"][name]).is_file())


class ARetryCanCarryThePreviousSweepsId(_CashbookCliCase):
    """``apply_sweep``'s own contract, made reachable (#1689 F2).

    It takes ``now`` rather than reading the clock *because* ``now`` IS the
    ``sweep_id``, so a retry of the same sweep can carry the same one and fold
    onto one record instead of two. The only caller minted a fresh id on every
    invocation, so the fold the store was designed around could not happen and
    a half-written sweep left a balance snapshot with no sweep record.
    """

    def _fail_the_sweep_record(self):
        """Run one sweep whose LAST append fails: the orphan-making shape.

        The four appends are not atomic together, and ``sweeps.jsonl`` is
        written last on purpose, so this is the window the design accepts and
        the one a retry has to close.
        """
        from alphalens_pipeline.brokers.journal import JournalWriteError

        with mock.patch.object(store, "append_sweep", side_effect=JournalWriteError("boom")):
            return self.sync("--format", "json")

    def _orphan_sweep_id(self) -> str:
        """The id of a balance snapshot that no sweep record accounts for."""
        swept = {record.sweep_id for record in store.read_sweeps(env="live")}
        lines = paths.balances_path("live").read_text(encoding="utf-8").splitlines()
        ids = [json.loads(line)["sweep_id"] for line in lines if line.strip()]
        orphans = [value for value in ids if value not in swept]
        self.assertEqual(len(orphans), 1, ids)
        return orphans[0]

    def test_the_failed_sweep_leaves_a_balance_with_no_sweep_record(self) -> None:
        """The defect ``--sweep-id`` exists for, measured rather than asserted
        from the design. Counted on the FOLDS: the journals are append-only, so
        a line count says nothing about what the store holds."""
        result = self._fail_the_sweep_record()

        self.assertEqual(result.exit_code, _EXIT_TRANSIENT)
        self.assertEqual(len(store.read_balances(env="live")), 1)
        self.assertEqual(store.read_sweeps(env="live"), [])

    def test_a_plain_re_run_closes_the_rows_and_doubles_the_balance(self) -> None:
        """Why a plain retry is not enough. The two keyed journals fold by
        their own keys, so they close themselves; ``balances.jsonl`` is keyed
        on the sweep id, so a fresh id ADDS a snapshot rather than closing the
        window, and one of the two then has no sweep record for ever."""
        self._fail_the_sweep_record()

        second = self.sync("--format", "json")

        self.assertEqual(second.exit_code, 0, second.output)
        self.assertEqual(len(store.read_balances(env="live")), 2)
        self.assertEqual(len(store.read_sweeps(env="live")), 1)

    def test_a_retry_under_the_same_id_folds_onto_one_balance_and_one_sweep(self) -> None:
        self._fail_the_sweep_record()
        orphan = self._orphan_sweep_id()

        second = self.sync("--format", "json", "--sweep-id", orphan)

        self.assertEqual(second.exit_code, 0, second.output)
        self.assertEqual(len(store.read_balances(env="live")), 1)
        records = store.read_sweeps(env="live")
        self.assertEqual([record.sweep_id for record in records], [orphan])

    def test_the_same_instant_in_another_offset_is_the_same_sweep(self) -> None:
        """The id is read as an INSTANT, not as a rendering of one (the
        ``apply_sweep`` contract), and the VPS runs Europe/Warsaw — so the
        offset an operator pastes must not decide whether the retry folds."""
        self._fail_the_sweep_record()
        orphan = self._orphan_sweep_id()
        warsaw = dt.timezone(dt.timedelta(hours=2))
        shifted = dt.datetime.fromisoformat(orphan).astimezone(warsaw)

        second = self.sync("--format", "json", "--sweep-id", shifted.isoformat())

        self.assertEqual(second.exit_code, 0, second.output)
        self.assertEqual(len(store.read_balances(env="live")), 1)
        self.assertEqual([r.sweep_id for r in store.read_sweeps(env="live")], [orphan])

    def test_absent_the_option_nothing_changes_for_a_first_sweep(self) -> None:
        """A fresh sweep is still a fresh sweep: the default is no default
        behaviour change, only a door that was not there."""
        payload = _strict_json(self.sync("--format", "json").stdout)

        self.assertEqual(payload["appended"]["sweeps"], 1)
        self.assertEqual(len(store.read_sweeps(env="live")), 1)

    def test_an_id_that_is_not_an_instant_is_a_usage_refusal(self) -> None:
        result = self.sync("--dry-run", "--format", "json", "--sweep-id", "yesterday")

        self.assertEqual(result.exit_code, _EXIT_USAGE)
        self.assertEqual(result.stdout, "")
        failure = _failure_from(result)
        self.assertEqual(failure["code"], "usage")
        self.assertEqual(failure["details"]["option"], "--sweep-id")

    def test_a_naive_id_is_refused_as_usage_and_not_as_a_traceback(self) -> None:
        """``apply_sweep`` raises ``ValueError`` on a naive instant, which would
        reach a caller as an exit-1 traceback with the append half done."""
        result = self.sync("--dry-run", "--format", "json", "--sweep-id", "2026-10-09T00:31:00")

        self.assertEqual(result.exit_code, _EXIT_USAGE)
        self.assertEqual(result.stdout, "")
        self.assertEqual(_failure_from(result)["details"]["option"], "--sweep-id")


class TheGroupHelpIsPlainWhenPiped(unittest.TestCase):
    def test_the_group_declares_no_rich_markup_mode_of_its_own(self) -> None:
        """One ``_help_markup_mode()`` assignment on the ROOT app covers every
        group; a per-group setting would be a second source of truth.

        Read off the SOURCE, not off the object. ``typer.Typer()`` already
        carries ``rich_markup_mode='rich'`` on typer 0.26.8 (measured), so
        comparing this app's attribute against a fresh app's compares 'rich'
        with 'rich' and a hard-coded ``rich_markup_mode="rich"`` here would
        pass. The keyword's absence in the call is the claim.
        """
        keywords = _group_app_keywords()

        self.assertNotIn("rich_markup_mode", keywords)

    def test_the_group_app_call_was_actually_found(self) -> None:
        """Positive control: an empty scan would make the assertion above
        vacuous, which is the failure mode of every AST gate in this repo."""
        self.assertIn("no_args_is_help", _group_app_keywords())

    def test_the_root_app_registers_the_group(self) -> None:
        import typer.main
        from alphalens_cli.main import app

        group = typer.main.get_command(app)

        self.assertIn("cashbook", group.commands)  # type: ignore[attr-defined]

    def test_the_piped_help_carries_no_box_drawing_characters(self) -> None:
        """No ``isatty`` patch: ``_help_markup_mode()`` is evaluated once, when
        ``main.py`` is imported, so patching the stream here changes nothing —
        measured by removing it. What gives this test its power is the root
        app's own mode, which hard-coding 'rich' there turns red."""
        from alphalens_cli.main import app

        result = CliRunner().invoke(app, ["cashbook", "sync", "--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        for char in "─│┌┐└┘╭╮╰╯":
            self.assertNotIn(char, result.stdout)


def _group_app_keywords() -> set[str]:
    """The keyword names ``cashbook.py`` passes to its own ``typer.Typer()``."""
    source = (
        Path(__file__).resolve().parents[2]
        / ".."
        / "alphalens-pipeline"
        / "alphalens_cli"
        / "commands"
        / "cashbook.py"
    ).resolve()
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "cashbook_app"
                for target in node.targets
            )
            and isinstance(node.value, ast.Call)
        ):
            return {keyword.arg for keyword in node.value.keywords if keyword.arg is not None}
    return set()


if __name__ == "__main__":
    unittest.main()
