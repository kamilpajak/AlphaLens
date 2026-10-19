"""One sweep of the account cash book: plan, then apply (#1689, memo section 3).

:func:`plan_sweep` folds what the vendor served against what is on disk and
writes NOTHING. :func:`apply_sweep` takes the lock, appends in order, releases
and returns the record. The CLI's ``--dry-run`` runs the first and stops, which
is the ``broker arm --dry-run`` shape: ONE code path, one branch around the
append, and the body rendered before anything is written -- so a dry run cannot
write, and a real run cannot report a plan other than the one it echoed.

**Append order is what makes a retry safe**, across four appends that are not
atomic together: the two KEYED journals first (``cash_events.jsonl`` by
``BkAmountId``, ``trade_rows.jsonl`` by ``TradeId``), then ``balances.jsonl``,
then ``sweeps.jsonl`` LAST. The last two both carry ``sweep_id`` and both
readers dedupe on it, so a crash anywhere in the sequence leaves a window a
re-sweep closes with one record per sweep rather than two. The pattern is
``journal.py``'s own ``"attempt"`` write-ahead on ``submissions.jsonl``.

**What the lock guarantees, and what it does not.** It is one ``flock`` on
``<root>/.sweep.lock``, held across all four appends, so two sweeps of the same
environment on the SAME HOST cannot interleave their appends -- a hand-run
sweep that overlaps a scheduled one refuses with :class:`SweepBusyError` and
appends nothing, rather than half-appending beside it. It does NOT make a sweep
atomic: a crash while it is held still leaves the partial state above, which is
why the append order and the ``sweep_id`` dedupe carry that case. It does not
order two sweeps either -- the loser refuses and is retried by whoever ran it.
And it is advisory and per-file, so it binds only processes that take it: a
writer that reached a journal without going through :mod:`store` would not be
stopped by it, which is what the append-mode gate in
``tests/brokers/test_journal_append.py`` exists for.

The bounded spin injects its clock and sleep, the
``data/alt_data/sec_rate_coordinator.py`` shape: without that a contention test
pays the full timeout on every run.

Pure of ``typer``. The sweep is what a command drives, never the reverse.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import logging
import os
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from alphalens_pipeline.account_cash import paths, store
from alphalens_pipeline.account_cash.codec import SweepRecord
from alphalens_pipeline.brokers.account_cash import AccountCashRead, CashEvent, TradeRow

logger = logging.getLogger(__name__)

_LOCK_ACQUIRE_TIMEOUT_S = 1.0
_LOCK_RETRY_SLEEP_S = 0.005

_CASH_EVENTS = "cash_events"
_TRADE_ROWS = "trade_rows"
_BALANCES = "balances"
_SWEEPS = "sweeps"

__all__ = [
    "SweepBusyError",
    "SweepPlan",
    "SweepRecord",
    "SweepRefusedError",
    "apply_sweep",
    "plan_sweep",
]


class SweepBusyError(Exception):
    """Another sweep holds this instance's cash-book lock. Nothing was appended."""

    def __init__(self, lock: Path) -> None:
        super().__init__(f"another sweep holds {lock}; nothing was appended, so re-running is safe")
        #: The lock file, as a string, for a caller to publish.
        self.lock = str(lock)


class SweepRefusedError(Exception):
    """The read answered nothing storable, so there is nothing to append.

    Raised rather than returning an empty record: a sweep record on disk with
    no rows reads afterwards as a window that was swept and held nothing, which
    is a different fact from a window that was never read.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"the cash-book sweep will not store this read: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class SweepPlan:
    """What one sweep WOULD append, folded against what is already on disk.

    ``read`` is carried whole so :func:`apply_sweep` needs no second read and
    the record can publish the window the VENDOR served rather than the one
    the caller asked for.
    """

    env: str
    since: dt.date
    until: dt.date
    read: AccountCashRead
    new_events: tuple[CashEvent, ...]
    restated_events: tuple[CashEvent, ...]
    already_present_events: int
    new_trade_rows: tuple[TradeRow, ...]
    restated_trade_rows: tuple[TradeRow, ...]
    already_present_trade_rows: int
    malformed: Mapping[str, int]
    refused_reason: str | None


def _split_against_known[Row](
    incoming: Sequence[Row], known: Mapping[str, Row], key_of: Callable[[Row], str]
) -> tuple[tuple[Row, ...], tuple[Row, ...], int]:
    """``(new, restated, already_present)`` for one journal's rows.

    ``restated`` is a row whose key is known and whose VALUE differs -- the
    signal the journal exists for. An identical row is ``already_present`` and
    is not re-appended: it would leave a second line that folds onto the same
    record and says nothing.

    Generic over the row type so the two journals share ONE definition of the
    three-way split: computing it twice is how the same quantity comes to be
    counted two different ways on two sides of one module.
    """
    new: list[Row] = []
    restated: list[Row] = []
    already_present = 0
    for row in incoming:
        key = key_of(row)
        if key not in known:
            new.append(row)
        elif known[key] != row:
            restated.append(row)
        else:
            already_present += 1
    return tuple(new), tuple(restated), already_present


def plan_sweep(read: AccountCashRead, *, env: str, since: dt.date, until: dt.date) -> SweepPlan:
    """Fold ``read`` against the store on disk. Writes nothing, creates nothing.

    A read that answered nothing (``skipped_reason`` set, which is what SIM
    answers) is planned as refused WITHOUT touching the store: on SIM the root
    must not even come into existence.
    """
    paths.validate_environment(env)
    if read.skipped_reason is not None:
        return SweepPlan(
            env=env,
            since=since,
            until=until,
            read=read,
            new_events=(),
            restated_events=(),
            already_present_events=0,
            new_trade_rows=(),
            restated_trade_rows=(),
            already_present_trade_rows=0,
            malformed={_CASH_EVENTS: 0, _TRADE_ROWS: 0},
            refused_reason=read.skipped_reason,
        )

    events_fold = store.read_cash_events(env=env)
    trades_fold = store.read_trade_rows(env=env)
    known_events = {event.bk_amount_id: event for event in events_fold.events}
    known_rows = {row.trade_id: row for row in trades_fold.rows}

    new_events, restated_events, present_events = _split_against_known(
        read.events or (), known_events, lambda event: event.bk_amount_id
    )
    new_rows, restated_rows, present_rows = _split_against_known(
        read.trades or (), known_rows, lambda row: row.trade_id
    )
    return SweepPlan(
        env=env,
        since=since,
        until=until,
        read=read,
        new_events=new_events,
        restated_events=restated_events,
        already_present_events=present_events,
        new_trade_rows=new_rows,
        restated_trade_rows=restated_rows,
        already_present_trade_rows=present_rows,
        malformed={
            _CASH_EVENTS: events_fold.malformed,
            _TRADE_ROWS: trades_fold.malformed,
        },
        refused_reason=None,
    )


def _acquire(
    fd: int, *, lock: Path, clock: Callable[[], float], sleep: Callable[[float], None]
) -> None:
    """Bounded non-blocking ``flock``, or :class:`SweepBusyError`.

    Bounded rather than blocking: a stuck holder must refuse a second sweep
    quickly and say so, not stall it for however long the first one runs.
    """
    deadline = clock() + _LOCK_ACQUIRE_TIMEOUT_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if clock() >= deadline:
                raise SweepBusyError(lock) from None
            sleep(_LOCK_RETRY_SLEEP_S)


@contextlib.contextmanager
def _sweep_lock(
    env: str, *, clock: Callable[[], float], sleep: Callable[[float], None]
) -> Iterator[Path]:
    """Hold the env's sweep lock for the whole append sequence.

    The path comes from ``store._writable_journal``, a private sibling in this
    same package, so the LOCK is behind the same write guard as the journals:
    taking it on SIM would create the root memo section 3 says must not exist.
    """
    lock = store._writable_journal(env, paths.sweep_lock_path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        _acquire(fd, lock=lock, clock=clock, sleep=sleep)
        yield lock
    finally:
        # Closing the descriptor releases the advisory lock, including on the
        # failure paths: a lock left held would poison every later sweep of
        # this process and read as contention with nobody.
        os.close(fd)


def apply_sweep(
    plan: SweepPlan,
    *,
    now: dt.datetime,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> SweepRecord:
    """Append what ``plan`` planned, under the lock, in order. Returns the record.

    ``now`` is supplied rather than read: it is the ``sweep_id``, so a retry of
    the same sweep must be able to carry the same one and fold onto one record
    instead of two.
    """
    if plan.refused_reason is not None:
        raise SweepRefusedError(plan.refused_reason)
    if now.tzinfo is None:
        raise ValueError(
            f"a sweep instant must carry an offset ({now!r} is naive); the sweep_id and the "
            f"balance stamp are compared across sweeps and a naive one is not comparable"
        )

    events = (*plan.new_events, *plan.restated_events)
    rows = (*plan.new_trade_rows, *plan.restated_trade_rows)
    balance = plan.read.balance
    sweep_id = now.isoformat()
    record = SweepRecord(
        sweep_id=sweep_id,
        swept_at=now,
        env=plan.env,
        since=plan.since.isoformat(),
        until=plan.until.isoformat(),
        # The window the VENDOR served, never one rebuilt from the arguments:
        # the two differ exactly when the read did not answer what was asked.
        events_window=plan.read.events_window,
        trades_window=plan.read.trades_window,
        appended={
            _CASH_EVENTS: len(events),
            _TRADE_ROWS: len(rows),
            _BALANCES: 0 if balance is None else 1,
            _SWEEPS: 1,
        },
        already_present={
            _CASH_EVENTS: plan.already_present_events,
            _TRADE_ROWS: plan.already_present_trade_rows,
        },
        restated={
            _CASH_EVENTS: len(plan.restated_events),
            _TRADE_ROWS: len(plan.restated_trade_rows),
        },
        malformed=dict(plan.malformed),
        refused_reason=None,
    )

    with _sweep_lock(plan.env, clock=clock, sleep=sleep):
        store.append_cash_events(events, env=plan.env)
        store.append_trade_rows(rows, env=plan.env)
        if balance is not None:
            store.append_balance(balance, env=plan.env, sweep_id=sweep_id)
        # LAST. A sweep record whose rows are not on disk claims more than
        # happened, and it is what a later reader would trust.
        store.append_sweep(record, env=plan.env)
    logger.info("cash-book sweep %s on %s appended %s", sweep_id, plan.env, dict(record.appended))
    return record
