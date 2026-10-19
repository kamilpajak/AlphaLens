"""CLI: ``alphalens cashbook`` — the account's own cash book (#1689, memo Phase 3).

    alphalens cashbook sync --since D --until D [--env sim|live] [--dry-run]
        [--format human|json]  — read the account's bookings, trades and
        balance for a window, fold them against the store, and append what is
        new. ``--dry-run`` plans and echoes, and writes nothing.

**What this group answers, and what it does not.** ``/edge`` asks whether the
pipeline SELECTS well, over every candidate of every brief; ``broker trades``
answers per PICK. Neither answers what the ACCOUNT's own money did, which is
what this store is for. ``sync`` is the acquisition half only: it publishes
SWEEP FACTS — the window asked for, the window the vendor served, rows read,
rows appended, rows already present, restated, malformed — and no money total,
no bucket and no profit-and-loss number. Those are memo Phase 4, and
publishing one now inside ``alphalens.cashbook.sync/v1`` would mean changing
the meaning of a published field inside a major version.

**The envelope and the failure contract are the broker group's** (#1379,
#1389), shared through :mod:`alphalens_cli.commands._cli_envelope`: ``--format
human|json``, exactly one strict JSON value on stdout or none at all, and a
refusal as one JSON object on stderr with stdout empty. Two codes are this
group's own, both CLI-owned (the contract package never learns a CLI concept,
#1122): ``cashbook_sync_refused`` with a closed ``details.reason`` vocabulary,
and ``cashbook_sweep_busy``, which is RETRYABLE because nothing was appended.

**The order of the body is load-bearing.** ``--format`` is settled first, so
every refusal after it renders the way the caller asked; the window is parsed
and bounded before a vendor is touched; and the JSON body is rendered BEFORE
the append, so a body that cannot be rendered strictly is a refusal rather than
a failure report for a write that already happened — the
``write_outcome_unknown`` shape.

**This command places nothing.** It holds a broker only to READ it, and
``tests/brokers/test_broker_cli_places_nothing.py`` walks this module to keep
it so.

Heavy imports stay inside the command body (lazy-CLI doctrine, see
``apps/alphalens-pipeline/CLAUDE.md``): the 15-minute edgar-detect timer pays
this binary's import cost on every fire.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import typer

from alphalens_cli.commands._cli_envelope import (
    _FORMAT_HUMAN,
    _FORMAT_JSON,
    _FORMAT_OPTION,
    _OUTPUT_FORMAT,
    _envelope,
    _fail_from_broker_error,
    _fail_with,
    _render_json,
    _resolve_format,
)

_SYNC_SCHEMA = "alphalens.cashbook.sync/v1"

cashbook_app = typer.Typer(
    name="cashbook",
    help=(
        "The account's own cash book — what the money did (#1689). Reads LIVE; "
        "on SIM the report endpoints answer with canned rows from other accounts, "
        "so a sweep refuses rather than store fiction."
    ),
    no_args_is_help=True,
)

_CASHBOOK_ENV_OPTION = typer.Option(
    None,
    "--env",
    help="Broker instance to read and store under: 'sim' or 'live' (ADR 0016). "
    "Default: $ALPHALENS_BROKER_ENVIRONMENT, else sim. 'live' additionally composes "
    "the rails + EnvironmentFile of the installed LIVE unit, so the command works in "
    "a plain shell. A sweep stores only under 'live'.",
)

_SINCE_OPTION = typer.Option(
    ...,
    "--since",
    help="First booking date to read, YYYY-MM-DD (inclusive).",
)

_UNTIL_OPTION = typer.Option(
    ...,
    "--until",
    help="Last booking date to read, YYYY-MM-DD (inclusive).",
)

_DRY_RUN_OPTION = typer.Option(
    False,
    "--dry-run",
    help="Read, fold and echo what WOULD be appended. Writes nothing.",
)

_SWEEP_ID_OPTION = typer.Option(
    None,
    "--sweep-id",
    help="Retry a half-written sweep under the id it already used, so the two journals "
    "keyed on that id fold onto ONE record each instead of two. The value is an instant "
    "(the id's own form, e.g. 2026-10-09T00:31:00+00:00); any offset of the same instant "
    "is the same sweep. Absent: a fresh sweep. Take it from the last balances.jsonl line "
    "whose sweep_id has no record in sweeps.jsonl.",
)


@cashbook_app.callback()
def _cashbook_callback() -> None:
    """Reset per-invocation state before routing to any cashbook subcommand.

    The handle is the one ``_cli_envelope`` object every emitter reads, so this
    reset reaches a refusal raised there. Without it a command that never
    declares ``--format`` would inherit whatever the previous in-process
    invocation left behind — which is every ``CliRunner`` test and any
    embedding caller.
    """
    _OUTPUT_FORMAT.set(_FORMAT_HUMAN)


def _booking_date(value: str, *, option: str) -> dt.date:
    """One ``YYYY-MM-DD`` bound, refused as a usage error rather than a traceback."""
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise _fail_with(
            "usage",
            f"{option} must be YYYY-MM-DD, got {value!r}",
            details={"option": option, "value": value},
        ) from exc


def _sweep_instant(value: str) -> dt.datetime:
    """One ``--sweep-id`` as the INSTANT it is, refused as a usage error.

    The id a sweep is keyed on is ``now.astimezone(UTC).isoformat()``, so the
    value an operator pastes back is parsed as an instant and never compared as
    a string: two offsets of one instant must be one sweep, which is the
    ``apply_sweep`` contract and the shape of the VPS, where ``now()`` is
    Europe/Warsaw.

    Both refusals are usage errors rather than exceptions out of the sweep:
    ``apply_sweep`` raises ``ValueError`` on a naive instant, and reaching it
    would mean an exit-1 traceback on the money path with the window already
    read.
    """
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise _fail_with(
            "usage",
            f"--sweep-id must be an ISO-8601 instant, got {value!r}; it is the id a "
            f"previous sweep used, e.g. 2026-10-09T00:31:00+00:00",
            details={"option": "--sweep-id", "value": value},
        ) from exc
    if parsed.tzinfo is None:
        raise _fail_with(
            "usage",
            f"--sweep-id {value!r} carries no offset; the sweep key is an instant and a "
            f"naive one is not comparable across sweeps",
            details={"option": "--sweep-id", "value": value},
        )
    return parsed


def _failed_file(exc: OSError) -> str | None:
    """The file a failed write names: the exception's own, else its cause's.

    ``JournalWriteError`` carries the path only inside its message, so its
    ``filename`` is empty and the wrapped ``OSError``'s is the one CPython set
    on ``open`` or ``mkdir``. A lock failure arrives unwrapped and carries its
    own. ``None`` rather than a guess when neither has one: a sweep touches
    five files in order, and naming the wrong one would send an operator to a
    file that is fine.
    """
    for candidate in (exc, exc.__cause__):
        filename = getattr(candidate, "filename", None)
        if filename is not None:
            return str(filename)
    return None


def _window(read_window: Any) -> dict[str, Any] | None:
    """One published read window: what the VENDOR served, or ``None``."""
    if read_window is None:
        return None
    return {
        "start": read_window.start,
        "end": read_window.end,
        "rows": read_window.rows,
    }


def _sync_body(plan: Any, *, dry_run: bool, journals: dict[str, str]) -> dict[str, Any]:
    """The sweep facts, from the PLAN and nothing else.

    There is no ``refused`` field. A refusal raises before this body is built,
    so the field could only ever be ``null`` — and a published ``/v1`` field a
    client can branch on but never observe is the same defect as a money total
    this schema does not own: it would have to mean something else later. A
    refusal is a non-zero exit with one failure object on stderr, and that is
    the only place a reason is published.

    From the plan rather than from the record, because the body is rendered
    before the append (see the module docstring) and a ``--dry-run`` writes no
    record at all. The counts therefore exist in two places —
    :func:`alphalens_pipeline.account_cash.sweep.apply_sweep` computes the same
    four from the same plan — and
    ``tests/account_cash/test_cashbook_sync_cli.py`` pins a real run's
    published counts against the record the sweep wrote, so the two cannot
    drift into two answers for one sweep.
    """
    balance = plan.read.balance
    return {
        "dry_run": dry_run,
        "synced": not dry_run,
        "asked": {"since": plan.since.isoformat(), "until": plan.until.isoformat()},
        "read": {
            "events": _window(plan.read.events_window),
            "trades": _window(plan.read.trades_window),
            "balance_asof": None if balance is None else balance.asof.isoformat(),
        },
        "appended": {
            "cash_events": len(plan.new_events) + len(plan.restated_events),
            "trade_rows": len(plan.new_trade_rows) + len(plan.restated_trade_rows),
            "balances": 0 if balance is None else 1,
            "sweeps": 1,
        },
        "already_present": {
            "cash_events": plan.already_present_events,
            "trade_rows": plan.already_present_trade_rows,
        },
        "restated": {
            "cash_events": len(plan.restated_events),
            "trade_rows": len(plan.restated_trade_rows),
        },
        "malformed": dict(plan.malformed),
        "journals": journals,
    }


#: Width of the prose's label column: the widest label this body publishes,
#: which is ``already_present``. It was 10, so that one row started its value
#: five characters late while every other row lined up.
_LABEL_WIDTH = len("already_present")

#: The word for a published boolean. ``true`` / ``false`` are what the JSON body
#: says, and a human renderer printing JSON literals is a serialiser wearing a
#: renderer's name.
_YES_NO = {True: "yes", False: "no"}


def _row(label: str, rest: str) -> None:
    typer.echo(f"{label:<{_LABEL_WIDTH}} {rest}")


def _render_sync_human(body: dict[str, Any]) -> None:
    """The operator's version of the same facts, in the same order.

    EVERY published fact, which is the CLI doctrine's own rule — one internal
    result rendered two ways, both carrying the same facts. ``journals`` and
    ``synced`` were missing: a sweep's prose named no file it wrote and did not
    say whether it wrote at all, and the subset the parity test listed could
    not see either gap. ``schema`` is the one key with no prose form: it is the
    machine contract's identity rather than a fact about this sweep.
    """
    asked = body["asked"]
    mode = " (dry run — nothing written)" if body["dry_run"] else ""
    typer.echo(f"cashbook sync {body['env']} {asked['since']} .. {asked['until']}{mode}")
    for flag in ("dry_run", "synced"):
        _row(flag, _YES_NO[body[flag]])
    read = body["read"]
    for name in ("events", "trades"):
        window = read[name]
        if window is None:
            _row("read", f"{name:<12} not read")
        else:
            served = f"served {window['start']} .. {window['end']}"
            _row("read", f"{name:<12} {window['rows']} row(s) {served}")
    _row("read", f"{'balance':<12} {read['balance_asof'] or 'not read'}")
    for label in ("appended", "already_present", "restated", "malformed"):
        counts = "  ".join(f"{name} {count}" for name, count in body[label].items())
        _row(label, counts)
    # One line per journal rather than one joined line: these are absolute
    # paths, so a single row would wrap in any terminal and the operator's own
    # next command is a copy of one of them.
    for name, path in body["journals"].items():
        _row("journals", f"{name:<12} {path}")


@cashbook_app.command("sync")
def sync_command(
    since: str = _SINCE_OPTION,
    until: str = _UNTIL_OPTION,
    dry_run: bool = _DRY_RUN_OPTION,
    sweep_id: str | None = _SWEEP_ID_OPTION,
    env: str | None = _CASHBOOK_ENV_OPTION,
    output_format: str | None = _FORMAT_OPTION,
) -> None:
    """Sweep one window of the account's cash into the store (#1689).

    Reads the bookings and trades reports UNFILTERED plus the balance, folds
    them against the store, and appends what is new. The output is SWEEP FACTS
    only — rows read, appended, already present, restated, malformed — and no
    money total: that is memo Phase 4.

    STEPS, in this order, and the order is load-bearing:

    \b
      format      settled first, so every refusal below renders as asked
      window      both bounds as dates, then --since <= --until; an inverted
                  or unbounded window must not reach the vendor
      sweep id    --sweep-id, when given, as an AWARE instant; a naive one
                  would reach `apply_sweep` as a ValueError after the read
      instance    the same `state_paths` seam the broker group resolves, so
                  the gateway read and the store root cannot disagree
      capability  a broker without `list_account_cash` is
                  `broker_unsupported` — a CONTRACT code, because the missing
                  capability is a fact about the broker, not about a command
      read        a read that answered nothing (SIM) refuses here; exit 0 with
                  a zero result would publish other accounts' canned rows as
                  this account's cash
      authorise   the store's own write guard, asked WHETHER OR NOT this is a
                  --dry-run: a dry run that plans a sweep the real run refuses
                  is a plan for something that cannot happen
      body        RENDERED before anything is written
      append      under the sweep's own lock, and only if not --dry-run

    On SIM nothing is stored, in both directions: the read refuses
    (`sim_reports_unusable`) and the store's write guard refuses
    (`sim_store_refused`). There is no ``--offline``: a sweep's whole job is
    the vendor read, and ``--dry-run`` still performs it.

    AFTER A ``queue_write_failed``: re-run the same window once the cause
    clears. The four appends are not atomic together, and the two keyed
    journals fold by their own keys, so they close themselves. ``balances.jsonl``
    is keyed on the SWEEP ID instead, so if its last line carries a ``sweep_id``
    that ``sweeps.jsonl`` has no record for, pass that id as ``--sweep-id``:
    without it the retry mints a fresh id and ADDS a second balance snapshot,
    leaving the first one with no sweep record for ever. A failure before the
    balance append needs nothing — no line is keyed on the lost id.
    """
    from alphalens_pipeline.account_cash import paths, store, sweep
    from alphalens_pipeline.brokers.account_cash import SupportsAccountCash
    from alphalens_pipeline.brokers.automanager import state_paths
    from alphalens_pipeline.brokers.journal import JournalWriteError
    from broker_contract.contract import BrokerError

    from alphalens_cli.commands.broker import _apply_env_option, _cli_broker

    resolved_format = _resolve_format(output_format)
    since_date = _booking_date(since, option="--since")
    until_date = _booking_date(until, option="--until")
    if since_date > until_date:
        raise _fail_with(
            "usage",
            f"--since {since} is after --until {until}; the window is inclusive on both ends",
            details={"option": "--since", "value": since},
        )
    swept_at = dt.datetime.now(dt.UTC) if sweep_id is None else _sweep_instant(sweep_id)

    _apply_env_option(env)
    try:
        resolved_env = state_paths.broker_environment()
    except ValueError as exc:
        raise _fail_with("usage", str(exc)) from exc

    built = _cli_broker()
    if not isinstance(built, SupportsAccountCash):
        raise _fail_with(
            "broker_unsupported",
            "the configured broker does not offer the account-cash read `cashbook sync` "
            "needs (the bookings and trades reports, read unfiltered, plus the balance)",
        )

    try:
        read = built.list_account_cash(since_date, until_date)
    except BrokerError as exc:
        raise _fail_from_broker_error(exc, "cashbook sync failed") from exc

    plan = sweep.plan_sweep(read, env=resolved_env, since=since_date, until=until_date)
    if plan.refused_reason is not None:
        raise _fail_with(
            "cashbook_sync_refused",
            f"the cash-book sweep read nothing storable for {resolved_env}: {plan.refused_reason}",
            details={"reason": plan.refused_reason, "env": resolved_env},
        )

    # AUTHORISE, in both directions. The CLI doctrine's own words for
    # `--dry-run`: everything except mutate — parse, validate, authorise,
    # resolve targets, plan. The appenders reach the store's write guard on
    # their way to a path, and they run only under `if not dry_run:`, so a dry
    # run used to publish a plan for a sweep the real run refuses. Asked
    # through the store's own guard rather than re-deciding live-only here: one
    # money-path rule, one function.
    #
    # AFTER the read, deliberately. The SIM adapter that ships today answers
    # nothing and says why, and `sim_reports_unusable` is the more specific
    # fact about that invocation — authorising first would tell an operator the
    # store refused a read that never happened. So in production this refuses
    # nothing the read has not already refused; what it fixes is the dry-run
    # branch under any broker that does answer rows on a non-live instance.
    try:
        store.assert_writable(resolved_env)
    except store.SimCashBookRefusedError as exc:
        raise _fail_with(
            "cashbook_sync_refused",
            str(exc),
            details={"reason": exc.reason, "env": resolved_env},
        ) from exc

    journals = {
        "cash_events": str(paths.cash_events_path(resolved_env)),
        "trade_rows": str(paths.trade_rows_path(resolved_env)),
        "balances": str(paths.balances_path(resolved_env)),
        "sweeps": str(paths.sweeps_path(resolved_env)),
    }
    body = _envelope(
        _SYNC_SCHEMA, resolved_env, **_sync_body(plan, dry_run=dry_run, journals=journals)
    )
    # Rendered BEFORE the append: an unrenderable body must be a refusal, never
    # a failure report for a write that already happened.
    rendered = _render_json(body) if resolved_format == _FORMAT_JSON else None

    if not dry_run:
        try:
            sweep.apply_sweep(plan, now=swept_at)
        # No `SimCashBookRefusedError` arm: the authorisation above has already
        # asked the same guard about the same `resolved_env`, so this call can
        # no longer raise it. A second handler for one exception is two
        # messages for one event, and a gate beside a delegate silently killing
        # that delegate's own arm is a defect this repo has a note on.
        except sweep.SweepBusyError as exc:
            raise _fail_with("cashbook_sweep_busy", str(exc), details={"lock": exc.lock}) from exc
        except JournalWriteError as exc:
            raise _fail_with(
                "queue_write_failed",
                f"could not append to the account cash book: {exc}",
                details={"journal": _failed_file(exc)},
            ) from exc
        except OSError as exc:
            # The lock, not a journal: it is taken before the first append, so
            # an unwritable store root fails HERE and nothing was written. Same
            # code, because it is the same failure mode — a store write did not
            # happen, nothing is in flight, and a re-run is safe once the cause
            # clears. Ordered after `JournalWriteError`, which is itself an
            # `OSError` subclass and names its journal.
            #
            # Published under `details.lock`, the key the lock already owns in
            # this contract (`cashbook_sweep_busy` publishes it), and
            # `details.journal` stays null: the published row says that key
            # names the file an append failed on, and this file is neither a
            # journal nor one anything appended to.
            raise _fail_with(
                "queue_write_failed",
                f"could not take the account cash book's sweep lock: {exc}",
                details={"journal": None, "lock": _failed_file(exc)},
            ) from exc

    if rendered is not None:
        typer.echo(rendered)
        return
    _render_sync_human(body)
