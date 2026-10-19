"""The cash book's store: append-only JSONL, folded by key (#1689, memo section 3).

Four journals under ``~/.alphalens/account_cash/<env>/``: ``cash_events.jsonl``
keyed by ``BkAmountId``, ``trade_rows.jsonl`` keyed by ``TradeId``, and
``balances.jsonl`` / ``sweeps.jsonl`` both keyed by ``sweep_id``.

**Append-only, latest line wins, and NEVER compacted.** A restatement's shape
on the wire is unknown (memo section 9) and this store is the only thing that
could ever detect one. A parquet overwrite replaces yesterday's number
silently; the journal keeps both lines and reports ``restated: N``. A number
that changed must be visible as having changed. Compaction is what destroyed
``trailed``, ``reanchored``, ``stop_filled`` and ``tranche_fired`` lines
elsewhere in this repo, and a restatement history that gets compacted is not a
restatement history --
``tests/account_cash/test_no_compactor_reaches_the_cash_book.py`` holds that
structurally.

**Every append goes through** ``brokers.journal.append_json_line``: one write,
flush, fsync, and a torn predecessor repaired before appending, so the
registered ``queue_write_failed`` code applies unchanged. No ``default=`` is
passed, the ``picks.py`` choice: ``default=str`` would turn an unrepresentable
money value into a quoted string on disk instead of refusing it.

**Append order is load-bearing**, and it is what makes a retry of a half-written
sweep safe: the two KEYED journals first, then ``balances.jsonl``, then
``sweeps.jsonl`` LAST. The last two both carry ``sweep_id`` and both readers
dedupe on it, so a crash anywhere in the sequence leaves a window a re-sweep
closes with one record per sweep rather than two. :mod:`sweep` owns that order;
this module owns the four appends it is made of.

**On SIM every appender refuses and nothing is created.** :func:`_writable_root`
is the one place that decides it, and no appender takes a path, so there is no
argument a caller can pass that routes around it. :func:`assert_writable` is
the public way to ask that same question without writing, for a caller that
must authorise before it plans. The SIM report endpoints
answer with canned rows belonging to OTHER accounts, and memo section 3 calls
storing that fiction worse than storing nothing. A READ is allowed on either
environment: an absent journal folds empty, which is the honest answer.

The fold rules mirror ``automanager/picks.py::read_pick_fold``, whose code this
module may not import (dependency rule 2) -- the shape is mirrored, not reused.
Two deliberate differences: the published order is sorted EXPLICITLY, because
first-seen key order is the wrong order for a cash book, and ``restated``
counts keys whose payload CHANGED, which is the signal the journal exists for.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alphalens_pipeline.account_cash import codec, paths
from alphalens_pipeline.brokers.account_cash import BalanceSnapshot, CashEvent, TradeRow
from alphalens_pipeline.brokers.journal import append_json_line

logger = logging.getLogger(__name__)

#: The write-side refusal's published reason. Distinct from the READ-side
#: ``brokers.account_cash.SIM_REPORTS_UNUSABLE``: that one says the vendor's SIM
#: reports answer with other accounts' rows, this one says a write was aimed at
#: a non-live store. They travel together on SIM and would be the same string
#: only by accident -- a write refused on LIVE-only grounds is not evidence
#: about any report.
SIM_STORE_REFUSED = "sim_store_refused"

#: The key each balances/sweeps line is deduped on. Lives beside the appenders
#: because the STORE owns the key while the codec owns the payload.
_SWEEP_ID_KEY = "sweep_id"

#: What DECODING one journal line can raise, beside the codec's own refusal.
#: Each member is NAMED rather than covered by ``except Exception``: the whole
#: value of this store is that a problem announces itself, and a blanket
#: handler would count a defect in the codec as corruption on disk. Two
#: reachable routes to ``OverflowError``, both measured on a complete rendered
#: row with one field doctored:
#:
#: * ``"amount": 10**400`` -- valid JSON, a valid Python ``int``, so it passes
#:   every type check in ``_finite`` and raises at ``float(value)``.
#: * ``"swept_at": "0001-01-01T00:00:00+14:00"`` -- parses, so
#:   ``fromisoformat``'s own ``ValueError`` guard never fires, and the
#:   ``astimezone(UTC)`` that follows falls before ``datetime.min``.
#:
#: The same shape ``validate_intent`` has already paid for once: a number the
#: schema admits and the arithmetic cannot hold.
_UNREADABLE_ROW: tuple[type[BaseException], ...] = (codec.CashBookDecodeError, OverflowError)

__all__ = [
    "SIM_STORE_REFUSED",
    "CashEventFold",
    "SimCashBookRefusedError",
    "TradeRowFold",
    "append_balance",
    "append_cash_events",
    "append_sweep",
    "append_trade_rows",
    "assert_writable",
    "read_balances",
    "read_cash_events",
    "read_sweeps",
    "read_trade_rows",
]


class SimCashBookRefusedError(Exception):
    """A write was aimed at a non-live environment (memo section 3)."""

    reason: str = SIM_STORE_REFUSED

    def __init__(self, env: str) -> None:
        super().__init__(
            f"the account cash book will not write to the {env!r} environment: the SIM "
            f"report endpoints answer with canned rows belonging to other accounts, and "
            f"storing that in a money store is worse than storing nothing"
        )
        self.env = env


def _writable_root(env: str) -> Path:
    """The env's store root, FOR WRITING. Refuses anything but live.

    An unknown env still raises ``ValueError`` from
    :func:`paths.validate_environment` rather than the SIM refusal: that is a
    caller defect, and answering the SIM reason for it would publish a reason
    that is not true.
    """
    if paths.validate_environment(env) != paths.ENV_LIVE:
        raise SimCashBookRefusedError(env)
    return paths.account_cash_root(env)


def assert_writable(env: str) -> None:
    """Raise if this environment may not be written to. Creates nothing.

    The one public way to ASK the write guard a question, for a caller that
    must authorise before it writes rather than on its way to a path. The
    appenders reach the guard through :func:`_writable_journal`, which is the
    right shape for a write and reaches nobody who is not about to perform
    one: ``alphalens cashbook sync --dry-run`` writes by construction nothing,
    so it never touched an appender and so never authorised, and it reported a
    success for a sweep the same invocation without ``--dry-run`` refuses
    (#1689).

    It delegates rather than restating ``!= ENV_LIVE``: a second copy of a
    money-path rule behaves identically on the day it is written and drifts on
    the day the rule changes.
    """
    _writable_root(env)


def _writable_journal(env: str, builder: Callable[[str], Path]) -> Path:
    """One journal of the env's root, resolved THROUGH :func:`_writable_root`.

    The parent comes from the write guard rather than from the builder, so the
    refusal is structural: a path can only be produced here by passing the
    guard first.
    """
    return _writable_root(env) / builder(env).name


# --- Folds -------------------------------------------------------------------


@dataclass(frozen=True)
class CashEventFold:
    """Latest-per-``BkAmountId`` events, plus what the read could not account for.

    ``lines`` counts every non-blank line read, malformed ones included, so
    ``lines`` and ``len(events)`` differing is ordinary rather than alarming --
    an append-only journal with one restatement holds two lines and one record.
    ``restated`` counts keys that arrived more than once with a DIFFERENT
    payload; an identical re-append is not a correction.
    """

    events: list[CashEvent]
    malformed: int
    restated: int
    lines: int


@dataclass(frozen=True)
class TradeRowFold:
    """Latest-per-``TradeId`` rows, with the same three counters."""

    rows: list[TradeRow]
    malformed: int
    restated: int
    lines: int


def _iter_payloads(path: Path) -> Iterator[tuple[int, Any]]:
    """Every non-blank line's parsed payload, or ``None`` when it will not parse.

    A blank line is skipped WITHOUT being counted: a trailing newline is
    ordinary in an append-only file, and counting it as corruption would make
    every torn-line repair read as damage.

    Opened in BINARY mode and decoded by ``json.loads`` itself, so BOTH ways a
    line can fail to parse are raised inside the guard. In text mode the UTF-8
    decode happens while the ``for`` statement advances the file, which is
    outside any ``try`` that a caller could write: one line ending mid-way
    through a multibyte sequence then raised ``UnicodeDecodeError`` out of the
    whole fold, and ONE such line made the entire cash book unreadable.
    ``UnicodeDecodeError`` is a ``ValueError``, like ``json.JSONDecodeError``,
    so moving the decode inside is the whole fix -- the handler below covers
    both, and each has a test of its own.
    """
    if not path.exists():
        return
    with path.open("rb") as fh:
        for index, raw_line in enumerate(fh):
            if not raw_line.strip():
                continue
            try:
                yield index, json.loads(raw_line)
            except ValueError:
                yield index, None


def _fold_by_key(
    path: Path,
    *,
    decode: Callable[[Any], Any],
    key_of: Callable[[Any], str],
    row: str,
) -> tuple[dict[str, Any], int, int, int]:
    """``(latest per key, malformed, restated, lines)`` over one journal.

    A malformed line is COUNTED, never dropped silently: a reader of a money
    store must be able to say how much of the journal it could not account for.
    A line the codec refuses counts as malformed too -- a hand-edited ``NaN``
    is the reachable case, and a fold must report it rather than raise. So does
    a line the decode cannot read for a reason the codec never names: see
    :data:`_UNREADABLE_ROW` for each member and why the set is named rather
    than blanket.
    """
    latest: dict[str, Any] = {}
    rendered: dict[str, Any] = {}
    restated_keys: set[str] = set()
    malformed = 0
    lines = 0
    for index, payload in _iter_payloads(path):
        lines += 1
        if payload is None:
            malformed += 1
            logger.warning("%s line %d is not JSON", path, index + 1)
            continue
        try:
            record = decode(payload)
        except _UNREADABLE_ROW as exc:
            malformed += 1
            logger.warning("%s line %d is not a readable %s row: %s", path, index + 1, row, exc)
            continue
        key = key_of(record)
        if key in latest and rendered[key] != payload:
            restated_keys.add(key)
        latest[key] = record
        rendered[key] = payload
    return latest, malformed, len(restated_keys), lines


# --- Appenders ---------------------------------------------------------------
#
# Each takes `env` and NEVER a path: there must be no argument a caller can
# pass that routes around the SIM refusal (memo section 3). Every row of a
# batch is RENDERED before the first append, so a row the codec refuses leaves
# the earlier rows of that batch unwritten rather than half-stored.


def append_cash_events(events: Sequence[CashEvent], *, env: str) -> None:
    """Append one ``cash_events.jsonl`` line per event, keyed ``BkAmountId``."""
    target = _writable_journal(env, paths.cash_events_path)
    for payload in [codec.cash_event_to_jsonable(event) for event in events]:
        append_json_line(target, payload)


def append_trade_rows(rows: Sequence[TradeRow], *, env: str) -> None:
    """Append one ``trade_rows.jsonl`` line per row, keyed ``TradeId``."""
    target = _writable_journal(env, paths.trade_rows_path)
    for payload in [codec.trade_row_to_jsonable(row) for row in rows]:
        append_json_line(target, payload)


def append_balance(snapshot: BalanceSnapshot, *, env: str, sweep_id: str) -> None:
    """Append one ``balances.jsonl`` line: the snapshot plus its sweep key.

    The key is checked HERE and not only on the read side: see
    :func:`_usable_sweep_id`.
    """
    target = _writable_journal(env, paths.balances_path)
    key = _usable_sweep_id(sweep_id, row="balances")
    payload = {**codec.balance_to_jsonable(snapshot), _SWEEP_ID_KEY: key}
    append_json_line(target, payload)


def append_sweep(record: codec.SweepRecord, *, env: str) -> None:
    """Append one ``sweeps.jsonl`` line: what was asked, and what came back.

    LAST of the four, deliberately (see the module docstring): a sweep record
    on disk whose rows are not is a record that claims more than happened.

    The key is checked the SAME way ``balances.jsonl``'s is, by the same
    function, because the two journals are one pair keyed on one value: an id
    one of them took and the other refused would survive in one half of the
    pair and be lost in the other.
    """
    target = _writable_journal(env, paths.sweeps_path)
    _usable_sweep_id(record.sweep_id, row="sweeps")
    append_json_line(target, codec.sweep_to_jsonable(record))


# --- Readers -----------------------------------------------------------------
#
# Allowed on EITHER environment: a read of an absent journal folds empty, which
# is the honest answer, and it creates nothing.
#
# None of them calls `paths.validate_environment` itself, on purpose. Each one
# built its path one line later through a builder that validates
# unconditionally, so an explicit call could never be the thing that refused
# anything: deleting all four left the same ValueError with the same message,
# one frame deeper. That is also the `state_paths` shape -- it validates inside
# the builder and no consumer re-validates -- and `read_pick_fold`, the fold
# these mirror, does not validate at all. Do not re-add the line: a guard no
# test can refute reads as protection that is not there.


def read_cash_events(*, env: str) -> CashEventFold:
    """Fold ``cash_events.jsonl`` to one event per ``BkAmountId``.

    Sorted EXPLICITLY by ``(date, bk_amount_id)``. ``date`` is optional on the
    vendor row, so the sort key substitutes ``""`` rather than comparing
    ``None`` with a string, which raises.
    """
    latest, malformed, restated, lines = _fold_by_key(
        paths.cash_events_path(env),
        decode=codec.cash_event_from_jsonable,
        key_of=lambda event: event.bk_amount_id,
        row="cash_events",
    )
    events = sorted(latest.values(), key=lambda event: (event.date or "", event.bk_amount_id))
    return CashEventFold(events=events, malformed=malformed, restated=restated, lines=lines)


def read_trade_rows(*, env: str) -> TradeRowFold:
    """Fold ``trade_rows.jsonl`` to one row per ``TradeId``, sorted by
    ``(trade_date, trade_id)``."""
    latest, malformed, restated, lines = _fold_by_key(
        paths.trade_rows_path(env),
        decode=codec.trade_row_from_jsonable,
        key_of=lambda row: row.trade_id,
        row="trade_rows",
    )
    rows = sorted(latest.values(), key=lambda row: (row.trade_date or "", row.trade_id))
    return TradeRowFold(rows=rows, malformed=malformed, restated=restated, lines=lines)


def read_balances(*, env: str) -> list[BalanceSnapshot]:
    """Every sweep's balance snapshot, one per ``sweep_id``, oldest first.

    A malformed line is logged rather than counted into a returned field: this
    journal has no identity to fold a broken line onto. Phase 4 reads these
    for the balance cross-check, and it will want a count -- the shape to
    widen then is this return type, not the log.

    **Until it does, a log line is the ONLY channel**, and the sweep record
    cannot carry it either: ``SweepPlan.malformed`` and ``SweepRecord.malformed``
    both hold ``cash_events`` and ``trade_rows`` only, because the plan folds
    just the two keyed journals. So a corrupt balance line is today invisible
    to every later reader of the store. Deliberately not fixed here rather than
    merely unnoticed: a third ``malformed`` key is a change to the
    ``sweeps.jsonl`` wire form, and the codec's count maps are key-agnostic in
    both directions so it stays additive -- it belongs with the Phase 4
    consumer that gives it a meaning, not with a counter nothing reads.
    """
    latest, _, _, _ = _fold_by_key(
        paths.balances_path(env),
        decode=_balance_line_from_jsonable,
        key_of=lambda pair: pair[0],
        row="balances",
    )
    return [snapshot for _, snapshot in sorted(latest.values(), key=_balance_sort_key)]


def read_sweeps(*, env: str) -> list[codec.SweepRecord]:
    """Every sweep record, one per ``sweep_id``, oldest first."""
    latest, _, _, _ = _fold_by_key(
        paths.sweeps_path(env),
        decode=codec.sweep_from_jsonable,
        key_of=lambda record: record.sweep_id,
        row="sweeps",
    )
    return sorted(latest.values(), key=lambda record: (record.swept_at, record.sweep_id))


def _usable_sweep_id(sweep_id: Any, *, row: str) -> str:
    """The sweep key, or a refusal. ONE definition of usable, for BOTH sides.

    Called by the two appenders that carry the key and by the balance decode,
    so a key a reader cannot fold on cannot be written in the first place. The
    write side is the right place for it: these journals are append-only and
    never compacted, so a line stored under an unusable key stays unreadable
    forever. ``append_balance`` used to write an empty one happily and
    ``read_balances`` then answered zero rows with only a log line -- no
    counter carries a broken balance line, so nothing later reported it.

    One function rather than one check per side, because the defect was the two
    sides disagreeing: the balance decode named both the empty and the
    non-string case while ``append_sweep`` took an empty id that
    ``read_sweeps`` read back, leaving one id alive in one half of a pair that
    is keyed on it and lost in the other.
    """
    if not isinstance(sweep_id, str) or not sweep_id:
        raise codec.CashBookDecodeError(
            f"a {row} line carries no usable {_SWEEP_ID_KEY} ({sweep_id!r}), and it is "
            f"the key one record per sweep is folded on"
        )
    return sweep_id


def _balance_line_from_jsonable(payload: Any) -> tuple[str, BalanceSnapshot]:
    """One ``balances.jsonl`` line: its sweep key and the snapshot.

    The key is popped before the codec sees the payload, so the codec stays
    symmetric over the snapshot alone and the store owns the key it dedupes on.
    """
    if not isinstance(payload, dict):
        raise codec.CashBookDecodeError("a balances line is not a JSON object")
    body = dict(payload)
    sweep_id = _usable_sweep_id(body.pop(_SWEEP_ID_KEY, None), row="balances")
    return sweep_id, codec.balance_from_jsonable(body)


def _balance_sort_key(pair: tuple[str, BalanceSnapshot]) -> tuple[dt.datetime, str]:
    sweep_id, snapshot = pair
    return snapshot.asof, sweep_id
