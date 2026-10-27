"""CLI: ``alphalens cashbook`` — the account's own cash book (#1689, memo 3-4).

    alphalens cashbook sync --since D --until D [--env sim|live] [--dry-run]
        [--format human|json]  — read the account's bookings, trades and
        balance for a window, fold them against the store, and append what is
        new. ``--dry-run`` plans and echoes, and writes nothing.

    alphalens cashbook totals --since D --until D [--env sim|live]
        [--format human|json]  — what the money did over one window, off the
        STORE alone. Reads no vendor, writes nothing.

**What this group answers, and what it does not.** ``/edge`` asks whether the
pipeline SELECTS well, over every candidate of every brief; ``broker trades``
answers per PICK. Neither answers what the ACCOUNT's own money did, which is
what this store is for.

**The two commands are the two halves, and each publishes only its own.**
``sync`` is acquisition: SWEEP FACTS — the window asked for, the window the
vendor served, rows read, appended, already present, restated, malformed — and
no money total, no bucket and no profit-and-loss number, because publishing
one inside ``alphalens.cashbook.sync/v1`` would mean changing the meaning of a
published field inside a major version. ``totals`` is the answer: one set of
money lines, each with the basis it rests on, rendered off the record
:func:`~alphalens_pipeline.account_cash.report.build_totals` returns. It
computes no money of its own, and it refuses a window the store cannot speak
for rather than answering a smaller number.

**The envelope and the failure contract are the broker group's** (#1379,
#1389), shared through :mod:`alphalens_cli.commands._cli_envelope`: ``--format
human|json``, exactly one strict JSON value on stdout or none at all, and a
refusal as one JSON object on stderr with stdout empty. Five codes are this
group's own, all CLI-owned (the contract package never learns a CLI concept,
#1122): ``cashbook_sync_refused`` and ``cashbook_totals_refused``, each with
its own closed ``details.reason`` vocabulary; ``cashbook_sweep_busy``, which is
RETRYABLE because nothing was appended; ``cashbook_store_unreadable``, also
retryable, for a journal this process cannot read; and
``cashbook_identity_broken``, which is not, because a store that contradicts
itself answers the same way on every re-run.

**The order of the body is load-bearing.** ``--format`` is settled first, so
every refusal after it renders the way the caller asked; the window is parsed
and bounded before a vendor is touched; and ``sync``'s JSON body is rendered
BEFORE the append, so a body that cannot be rendered strictly is a refusal
rather than a failure report for a write that already happened — the
``write_outcome_unknown`` shape.

**Neither command places anything, and ``totals`` holds no broker at all.**
``sync`` holds one only to READ it;
``tests/brokers/test_broker_cli_places_nothing.py`` walks this module to keep
it so, and ``tests/account_cash/test_cashbook_totals_cli.py`` pins that
``totals`` builds none, because a window it cannot answer offline is a window
it refuses.

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
    _emit_json,
    _envelope,
    _fail_from_broker_error,
    _fail_with,
    _render_json,
    _resolve_format,
)

_SYNC_SCHEMA = "alphalens.cashbook.sync/v1"
_TOTALS_SCHEMA = "alphalens.cashbook.totals/v1"

cashbook_app = typer.Typer(
    name="cashbook",
    help=(
        "The account's own cash book — what the money did (#1689). `sync` reads the "
        "account and stores the window; `totals` answers one window off the store. "
        "LIVE only: on SIM the report endpoints serve canned rows from other "
        "accounts, so a sweep refuses rather than store fiction and a total refuses "
        "rather than sum an empty store."
    ),
    no_args_is_help=True,
)

_CASHBOOK_ENV_OPTION = typer.Option(
    None,
    "--env",
    help="Broker instance to read and store under: 'sim' or 'live' (ADR 0016). "
    "Default: $ALPHALENS_BROKER_ENVIRONMENT, else sim. 'live' also composes the "
    "installed LIVE unit's environment, so either command works in a plain shell: "
    "required by 'sync', best-effort for 'totals', which holds no broker. The cash "
    "book exists only under 'live' — a sweep stores nothing else, and a total "
    "refuses anything else rather than sum an empty store.",
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

_TOTALS_SINCE_OPTION = typer.Option(
    ...,
    "--since",
    help="First booking date the report covers, YYYY-MM-DD (inclusive). Earlier than the "
    "store's own first swept day is refused, never answered smaller.",
)

_TOTALS_UNTIL_OPTION = typer.Option(
    ...,
    "--until",
    help="Last booking date the report covers, YYYY-MM-DD (inclusive). Later than the "
    "last swept day is answered, with the shortfall on the coverage line; a window no "
    "sweep covers a single day of is refused, never answered as zeros.",
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


#: Width of the TOTALS fact rows' label column: the widest label that body
#: publishes. Its own constant rather than ``_LABEL_WIDTH``, which is the
#: widest label of the SYNC body -- one character narrower, so sharing it
#: would start the ``fills_unassigned`` row a character late while every other
#: row lined up.
_TOTALS_LABEL_WIDTH = len("fills_unassigned")

#: The word for a published null and for an empty list. ``null`` and ``[]``
#: are what the JSON body says, and a human renderer printing JSON literals is
#: a serialiser wearing a renderer's name. ONE word for both, because they say
#: the same thing to an operator -- there is nothing here.
_NOT_STATED = "none"

#: The eight published money lines, in memo section 6's render order, as
#: ``(body field, label)``. The ORDER is load-bearing twice over: the deposits
#: sit BELOW the subtotal because 24 000 PLN of funding is the easiest number
#: in this repo to misread as profit, and the cost basis of what is still open
#: is its own line because it is not realized money.
#: The lines that ADD UP, in the order they add up in. Four terms, the rule,
#: the subtotal, the deposits, the rule, the sum -- so a reader who totals the
#: block above each rule gets the number the rule carries.
_TOTALS_MONEY_LINES: tuple[tuple[str, str], ...] = (
    ("net_closed_acct", "net on closed episodes"),
    ("income_acct", "dividends and withholding tax"),
    ("unclassified_acct", "unclassified cash events"),
    ("residual_acct", "residual the named lines leave"),
    ("subtotal_acct", "trading and income subtotal"),
    ("deposits_acct", "deposits and withdrawals"),
    ("cash_book_sum_acct", "cash book sum"),
)

#: Money the report states but does not ADD, printed below the sum and outside
#: both rules. The open cost basis answers as of the store's watermark while
#: every line above answers as of the window, so it is not a term of either
#: derived line -- and while it sat among the terms, a reader who totalled the
#: block was wrong by exactly its value (measured: the four terms give
#: -7827.16 at the 2026-09-12 cut, the five gave -11422.02, and the rule says
#: -7827.16).
_TOTALS_STATED_LINES: tuple[tuple[str, str], ...] = (
    ("cost_basis_open_acct", "cost basis of open episodes"),
)

#: What each money line's row count COUNTS, and what the number is not. The
#: counts are in two spaces -- episodes for the first two lines, booking rows
#: for the rest -- and a count in one space printed beside a number in another
#: is this repo's own named defect.
_TOTALS_MONEY_NOTES: dict[str, str] = {
    "net_closed_acct": "episodes, after commission and exchange fee",
    "cost_basis_open_acct": "episodes, not realized",
    "income_acct": "rows",
    "unclassified_acct": "rows",
    # The terms themselves are appended from the RECORD (see `_money_note`):
    # one of them -- the deposits -- is rendered BELOW this line, so a note
    # that said "the lines above" described a number 24 000.00 PLN away from
    # the one it was printed beside.
    "residual_acct": "rows, the window's cash book sum less",
    "subtotal_acct": "rows",
    "deposits_acct": "rows, not P&L",
    "cash_book_sum_acct": "rows",
}

#: The money line whose row count is not the whole story: it carries its own
#: as-of (the cost of a position still open is as-of the last day the store can
#: speak for, not the window's end) and the count of spans still holding shares
#: that the amount leaves out.
_COST_BASIS_FIELD = "cost_basis_open_acct"

#: The money line whose note names the OTHER lines it was computed from. Its
#: terms come off the record's own ``residual_terms``, never from a list here:
#: the deposits are one of them and are rendered below this line, so a note
#: spelled in the renderer is a sentence that was true when it was written and
#: wrong by 24 000.00 PLN after the deposits moved under the subtotal.
_RESIDUAL_FIELD = "residual_acct"


def _stated(value: Any) -> str:
    """One published scalar, with a word for the absent case."""
    return _NOT_STATED if value is None else str(value)


def _joined(values: list[str]) -> str:
    """A published list of words, with a word for the empty case.

    Two spaces between them rather than a comma, so each word stays bounded by
    whitespace: a reader looking for one of them finds it whole.
    """
    return "  ".join(values) if values else _NOT_STATED


def _fact(label: str, rest: str) -> None:
    typer.echo(f"{label:<{_TOTALS_LABEL_WIDTH}} {rest}")


def _named_terms(terms: list[str]) -> str:
    """A published list of line names, as the last clause of a note reads.

    Commas and one ``and``, because this is the tail of an English sentence
    rather than a column of counts. Each name stays bounded by whitespace or a
    comma, so a reader looking for one of them finds it whole.
    """
    if len(terms) < 2:
        return _joined(terms)
    return f"{', '.join(terms[:-1])} and {terms[-1]}"


def _money_note(field: str, body: dict[str, Any]) -> str:
    """The row count one money line rests on, and what it is not."""
    note = f"{body[field]['rows']} {_TOTALS_MONEY_NOTES[field]}"
    if field == _COST_BASIS_FIELD:
        return (
            f"{note}, as of {_stated(body['cost_basis_open_asof'])}, "
            f"{body['cost_basis_open_excluded']} more held and not on this line"
        )
    if field == _RESIDUAL_FIELD:
        # From the RECORD: it states which lines its own subtraction took off,
        # so adding or removing a term changes this note instead of leaving it
        # describing the subtraction the report used to make.
        return f"{note} {_named_terms(body['residual_terms'])}"
    return note


def _render_totals_money(body: dict[str, Any]) -> None:
    """Seven lines that add up, then the one the report states but does not add.

    Both widths are DERIVED -- from the widest published label and the widest
    rendered amount, over BOTH blocks so the two share one column -- so a
    renamed line or a null value (which renders as its reason, and a reason is
    longer than a number) moves the column instead of overflowing a literal.

    The split is the point: everything above a rule totals to the number that
    rule carries. The open cost basis does not belong to either total, so it
    goes below the sum rather than among the terms.
    """
    every = _TOTALS_MONEY_LINES + _TOTALS_STATED_LINES
    label_width = max(len(label) for _field, label in every)
    money_width = max(len(body[field]["value_rendered"]) for field, _label in every)
    rule = label_width + 1 + money_width

    def emit(field: str, label: str) -> None:
        amount = body[field]["value_rendered"]
        typer.echo(f"{label:<{label_width}} {amount:>{money_width}}  {_money_note(field, body)}")

    for field, label in _TOTALS_MONEY_LINES:
        if field == "subtotal_acct":
            typer.echo("-" * rule)
        if field == "cash_book_sum_acct":
            typer.echo("=" * rule)
        emit(field, label)
    for field, label in _TOTALS_STATED_LINES:
        typer.echo("")
        emit(field, label)


def _render_totals_basis(basis: dict[str, Any]) -> None:
    """What every number above rests on, printed ABOVE them (memo 5(d))."""
    _fact(
        "basis",
        f"currency {_stated(basis['currency'])}  episodes {basis['episodes']}  "
        f"episodes_summed {basis['episodes_summed']}  "
        f"episodes_excluded {basis['episodes_excluded']}",
    )
    _fact(
        "basis",
        f"rows_in_store {basis['rows_in_store']}  rows_read {basis['rows_read']}  "
        f"rows_classified {basis['rows_classified']}",
    )
    _fact(
        "basis",
        f"rows_outside_window {basis['rows_outside_window']}  "
        f"rows_undated {basis['rows_undated']}  "
        f"rows_date_malformed {basis['rows_date_malformed']}",
    )
    _fact("basis", f"fills_read {basis['fills_read']}  fills_assigned {basis['fills_assigned']}")
    # Each mapping under its OWN label, the body's own key: these are four
    # mappings of small integers that read alike, so one shared label would let
    # a reader take one mapping's counts for another's.
    for label in ("exclusions", "fills_unassigned", "restated", "malformed"):
        _fact(label, "  ".join(f"{name} {count}" for name, count in basis[label].items()))


def _render_totals_buckets(published: list[dict[str, Any]]) -> None:
    """Every bucket, zero ones included: a type nobody has seen then reads as
    a CHANGED number rather than as a new row in a list the reader has stopped
    scanning (memo section 5(a))."""
    name_width = max(len(bucket["name"]) for bucket in published)
    money_width = max(len(bucket["total"]["value_rendered"]) for bucket in published)
    for bucket in published:
        _fact(
            "bucket",
            f"{bucket['name']:<{name_width}} "
            f"{bucket['total']['value_rendered']:>{money_width}}  "
            f"{bucket['rows']} rows  {_joined(bucket['bk_amount_types'])}",
        )


def _render_totals_coverage(coverage: dict[str, Any]) -> None:
    """What the store can speak for, where it cannot, and what it cannot see."""
    _fact(
        "coverage",
        f"history_begins {_stated(coverage['history_begins'])}  "
        f"swept_through {_stated(coverage['swept_through'])}  "
        f"sweeps {coverage['sweeps']}  malformed_windows {coverage['malformed_windows']}",
    )
    # One line per hole rather than one joined line, and `gaps none` when there
    # is none: a shorter list reads as fuller coverage.
    if coverage["gaps"]:
        for first, last in coverage["gaps"]:
            _fact("coverage", f"gap {first} .. {last}")
    else:
        _fact("coverage", f"gaps {_NOT_STATED}")
    _fact("coverage", f"cannot_detect {_joined(coverage['cannot_detect'])}")


def _render_totals_balance(checks: list[dict[str, Any]]) -> None:
    """Both cross-checks, each stating its own basis and its own blind spot.

    ``detects_restatement`` sits on the same line as the verdict on purpose:
    the cumulative check is store-wide against the latest balance and cannot
    see a correction to an old row, so "reconciles" alone would read as proof
    the history is intact (memo section 5(c)).
    """
    for check in checks:
        kind = check["kind"]
        _fact(
            "balance",
            f"{kind}  {check['verdict']}  "
            f"detects_restatement {_YES_NO[check['detects_restatement']]}  "
            f"null_reason {_stated(check['null_reason'])}",
        )
        for name in ("expected", "observed", "difference"):
            amount = check[name]
            _fact(
                "balance",
                f"{kind}  {name} {amount['value_rendered']} over {amount['rows']} rows",
            )
        _fact(
            "balance",
            f"{kind}  rows {check['rows']}  {_stated(check['since'])} .. "
            f"{_stated(check['until'])}  asof {_stated(check['asof'])}",
        )


def _render_totals_human(body: dict[str, Any]) -> None:
    """The operator's version of the cash book's answer (memo section 6).

    EVERY published fact, which is the repo CLI doctrine's own rule -- one
    internal result rendered two ways, both carrying the same facts. ``schema``
    is the one key with no prose form: it is the machine contract's identity
    rather than a fact about this window.

    The currency is ON every number and never once in a header, per memo
    section 5(d) and the house renderer: a number beside a threshold in a
    different unit reads as a comfortable pass. It arrives already rendered,
    as the body's own ``value_rendered``, so the prose and the JSON cannot
    come apart over how one amount reads.
    """
    basis = body["basis"]
    typer.echo(f"cashbook totals {body['env']} {basis['window_since']} .. {basis['window_until']}")
    _render_totals_basis(basis)
    typer.echo("")
    _render_totals_money(body)
    typer.echo("")
    _render_totals_buckets(body["buckets"])
    _render_totals_coverage(body["coverage"])
    _render_totals_balance(body["balance_checks"])


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


def _totals_body(totals: Any) -> dict[str, Any]:
    """The record's own wire form, with the envelope's field removed.

    Nothing is computed here and nothing is renamed: the body IS
    ``CashBookTotals.to_jsonable()``, because memo section 5(d) makes every
    total structurally inseparable from its basis and a CLI that rebuilt the
    body field by field is a second place the two could come apart. ``env`` is
    dropped because :func:`_envelope` publishes it, and publishing it twice
    would let the two disagree.
    """
    body = totals.to_jsonable()
    body.pop("env")
    return body


@cashbook_app.command("totals")
def totals_command(
    since: str = _TOTALS_SINCE_OPTION,
    until: str = _TOTALS_UNTIL_OPTION,
    env: str | None = _CASHBOOK_ENV_OPTION,
    output_format: str | None = _FORMAT_OPTION,
) -> None:
    """What the account's own money did over one window (#1689).

    Reads the stored cash book -- the bookings, the trades and the balance
    snapshots a previous ``cashbook sync`` appended -- and publishes one set of
    lines with the basis they rest on. It touches NO vendor: a window is
    answerable offline or it is refused.

    STEPS, in this order:

    \b
      format      settled first, so every refusal below renders as asked
      window      both bounds as dates, then --since <= --until
      instance    the same `state_paths` seam the broker group resolves, so
                  the store root this reads cannot disagree with the one a
                  sweep wrote
      answer      `report.build_totals`, which refuses a window it cannot
                  answer rather than returning a smaller number
      render      the record, as JSON or as prose; no number is computed here

    WHAT IT REFUSES, all as `cashbook_totals_refused` with `details.reason`:
    `sim_reports_unusable` (on SIM the vendor's report endpoints answer with
    other accounts' canned rows, so the store is empty by construction and a
    sum over it would publish 0.00 for an account nobody read),
    `nothing_swept` (no sweep has ever covered any window, so there is no
    watermark to answer against), `before_first_sweep` (`--since` is
    earlier than the earliest window any sweep asked for; the store holds rows
    whose own entries may predate it, so a smaller number would read as a
    complete answer) and `window_never_swept` (no sweep asked about a single
    day of this window -- it lies past every asked window, or inside a hole
    between two of them).

    An `--until` beyond the swept range is NOT refused: the store can be asked
    about a day nobody swept and answers that it holds nothing for it, which
    is honest because the coverage block is published beside every number. A
    window with no day in common with any sweep is the case that does refuse:
    nothing in it was read, so there is nothing for the coverage block to state
    a shortfall against.
    """
    from alphalens_pipeline.account_cash import report
    from alphalens_pipeline.brokers.automanager import state_paths

    from alphalens_cli.commands.broker import _apply_env_option

    resolved_format = _resolve_format(output_format)
    since_date = _booking_date(since, option="--since")
    until_date = _booking_date(until, option="--until")
    if since_date > until_date:
        raise _fail_with(
            "usage",
            f"--since {since} is after --until {until}; the window is inclusive on both ends",
            details={"option": "--since", "value": since},
        )

    # BEST-EFFORT composition, unlike `sync`'s. This command holds no broker
    # and places nothing, so the LIVE unit's rails are no part of its answer;
    # what it needs from `--env live` is the instance pin, which the
    # best-effort branch still applies (and it forces ALLOW_ORDERS=0 either
    # way). Requiring the unit would refuse a read of a store copied onto a
    # machine where no LIVE unit is installed.
    _apply_env_option(env, required=False)
    try:
        resolved_env = state_paths.broker_environment()
    except ValueError as exc:
        raise _fail_with("usage", str(exc)) from exc

    try:
        totals = report.build_totals(env=resolved_env, since=since_date, until=until_date)
    except report.CashBookTotalsRefusedError as exc:
        raise _fail_with(
            "cashbook_totals_refused",
            str(exc),
            # The record's own `details` ride along: a refusal leaves stdout
            # EMPTY, so the watermark a recovery needs has to be on the failure
            # object. `reason` and `env` lead, and nothing in `exc.details`
            # may rename them -- they are the two keys the published table
            # names for this code.
            details={"reason": exc.reason, "env": exc.env, **exc.details},
        ) from exc
    except report.CashBookIdentityError as exc:
        # NOT `unclassified`. The repo rule is that `unclassified` marks a
        # refusal nobody has given a code yet and is never a branch condition;
        # this one is named, is raised deliberately rather than by accident,
        # and its recovery differs from the one below -- so a client that
        # branches on the code gets the right answer rather than "a human must
        # look" for two different humans. Its own code rather than a `reason`
        # under the one above, because that code means "this WINDOW cannot be
        # answered"; no window can be answered while the store contradicts
        # itself.
        raise _fail_with(
            "cashbook_identity_broken", str(exc), details={"identity": exc.identity}
        ) from exc
    except OSError as exc:
        # A journal or the store root this process cannot read. Separate from
        # the identity above because `retryable` is a property of the CODE and
        # the two differ in it: this one answers once the cause clears, and
        # this command writes nothing either way, which is what `retryable`
        # means here ("safe to re-run WITHOUT reconciling first").
        #
        # Caught at the command and not inside the record: the store's readers
        # are pure over paths, and a reader that swallowed an unreadable
        # journal would answer a SMALLER number -- the one thing the whole
        # refusal surface exists to prevent.
        raise _fail_with(
            "cashbook_store_unreadable",
            f"could not read the account cash book: {exc}",
            details={"journal": _failed_file(exc)},
        ) from exc

    body = _envelope(_TOTALS_SCHEMA, resolved_env, **_totals_body(totals))
    if resolved_format == _FORMAT_JSON:
        _emit_json(body)
        return
    _render_totals_human(body)
