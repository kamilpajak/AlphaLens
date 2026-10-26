"""The balance cross-check: does the cash book account for the cash? (#1689)

Memo section 5(c), the third of the three mechanisms by which an incomplete
number announces itself. Each sweep stores a balance snapshot; this asserts
that the account's own cash agrees with what the cash book says moved. **A
disagreement is CONTENT, not a crash:** the check publishes ``differs`` with
the difference as a number, because the one thing a money report must not do
is refuse to print.

Two versions, and the difference matters more than it looks.

**Cumulative** -- the cash book since ``history_begins`` against the latest
``CashBalance``. Known to hold, and it carries ``detects_restatement: False``
on its face: a check that only ever says yes cannot see a correction to an old
row, and without that field "reconciles" reads as proof the history is intact,
which it is not. It is also STORE-WIDE by definition, so it publishes its own
row count and its own since/until on its own line and can never be read as the
presentation window's figure (amendment W1).

**Incremental** -- the change between two snapshots against the events
settling between them. This is the one that catches a late restatement, which
is the entire reason the store is an append-only journal.

**The incremental interval is in TRADE DATES, never in instants.** A cash event
carries a ``Date`` and no timestamp; the settlement batch fires at about 23:48
UTC and the report completes about 26 minutes later, across midnight (memo
section 1.7 P5, P6). The venue stamps ``Date`` with the TRADE date and
``ValueDate`` with the next day on every row (P9). So the interval runs from
the day AFTER the earlier snapshot's sweep window ended to the day the later
one's ended, both taken from :class:`~.codec.SweepRecord`, and
:class:`~.store.BalanceAt` exists to carry that join. Measured: keying on the
``asof`` instants instead sums ZERO of the P8 pair's six rows -- each is dated
2026-10-08, and midnight on that date falls before the earlier snapshot's
23:46:52 -- and publishes ``differs`` with +4 319.07 against a pair that
reconciles exactly.

**What this cannot see**, and the memo says so rather than the code pretending
otherwise: a sweep run inside the window between the settlement batch and the
report completing sees the whole day's cash movement with none, or only some,
of the rows that explain it, and reports ``differs`` by up to a full day's
trading -- correctly by its own definition and meaninglessly. The containment
is the cadence (no earlier than about 00:30 UTC for the preceding trade date),
not a flag here, because ``TransactionsNotBooked == 0`` clears with the cash
about nineteen minutes before any report row exists and is therefore not a
completeness gate (P7).

Pure over rows, snapshots and sweep records: it takes no ``env`` and reads no
store. :mod:`report` hands it what it read.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from alphalens_pipeline.account_cash.codec import SweepRecord
from alphalens_pipeline.account_cash.money import (
    BALANCE_TOLERANCE_ACCT,
    AcctAmount,
    acct_difference,
    acct_null,
    acct_sum,
)
from alphalens_pipeline.account_cash.store import BalanceAt
from alphalens_pipeline.brokers.account_cash import CashEvent

#: The cash book and the account agree to within one grosz.
VERDICT_RECONCILES = "reconciles"
#: They disagree, and the difference is published as a number.
VERDICT_DIFFERS = "differs"
#: The check could not be made at all. Never ``0.0``, which reads as
#: agreement.
VERDICT_NOT_CHECKABLE = "not_checkable"
VERDICTS: tuple[str, ...] = (VERDICT_RECONCILES, VERDICT_DIFFERS, VERDICT_NOT_CHECKABLE)

CHECK_CUMULATIVE = "cumulative"
CHECK_INCREMENTAL = "incremental"
CHECK_KINDS: tuple[str, ...] = (CHECK_CUMULATIVE, CHECK_INCREMENTAL)

#: No sweep ever stored a balance body, so there is no account figure at all.
NULL_NO_BALANCE_SNAPSHOT = "no_balance_snapshot"
#: Exactly one snapshot: a change needs two.
NULL_ONE_SNAPSHOT_ONLY = "one_snapshot_only"
#: A snapshot whose sweep record is not on disk, or whose window will not
#: parse. The append order makes this reachable: ``balances.jsonl`` is written
#: BEFORE ``sweeps.jsonl``, so a crash between the two leaves exactly this.
#: Defaulting to the other snapshot's window would sum an interval nobody read.
NULL_SWEEP_WINDOW_NOT_FOUND = "sweep_window_not_found"
CHECK_NULL_REASONS: tuple[str, ...] = (
    NULL_NO_BALANCE_SNAPSHOT,
    NULL_ONE_SNAPSHOT_ONLY,
    NULL_SWEEP_WINDOW_NOT_FOUND,
)

__all__ = [
    "CHECK_CUMULATIVE",
    "CHECK_INCREMENTAL",
    "CHECK_KINDS",
    "CHECK_NULL_REASONS",
    "NULL_NO_BALANCE_SNAPSHOT",
    "NULL_ONE_SNAPSHOT_ONLY",
    "NULL_SWEEP_WINDOW_NOT_FOUND",
    "VERDICTS",
    "VERDICT_DIFFERS",
    "VERDICT_NOT_CHECKABLE",
    "VERDICT_RECONCILES",
    "BalanceCheck",
    "cumulative_check",
    "incremental_check",
]


@dataclass(frozen=True)
class BalanceCheck:
    """One cross-check, with the basis it rests on beside every number.

    ``rows``, ``since`` and ``until`` are the CHECK's own, not the report's:
    the cumulative check is store-wide and the incremental one covers a
    two-day interval, so a reader who takes either for the presentation
    window's figure would be reading a number against the wrong basis.
    """

    kind: str
    verdict: str
    #: What the cash book says moved, in the ACCOUNT currency.
    expected: AcctAmount
    #: What the account's own cash did: ``CashBalance``, or its change.
    observed: AcctAmount
    difference: AcctAmount
    #: Published, never implied. ``False`` on the cumulative check.
    detects_restatement: bool
    #: Booking rows this check summed.
    rows: int
    since: str | None
    until: str | None
    asof: str | None
    null_reason: str | None

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "verdict": self.verdict,
            "expected": self.expected.to_jsonable(),
            "observed": self.observed.to_jsonable(),
            "difference": self.difference.to_jsonable(),
            "detects_restatement": self.detects_restatement,
            "rows": self.rows,
            "since": self.since,
            "until": self.until,
            "asof": self.asof,
            "null_reason": self.null_reason,
        }


def _account_currency(at: BalanceAt) -> str | None:
    """The account's currency, as the balances body states it.

    Read through a named function rather than inline in an :func:`acct_sum`
    keyword, for the same reason :func:`_cash_as_acct` binds it to a local:
    the FX guard in ``tests/account_cash/test_money.py`` walks the arguments
    of every summing call and reports each ``CashEvent`` field name it finds,
    so ``currency=latest.snapshot.currency`` would be reported as a booking
    row's INSTRUMENT currency -- read off a record that has no such field.
    """
    return at.snapshot.currency


def _cash_as_acct(at: BalanceAt) -> AcctAmount:
    """The account's own cash balance as an account amount.

    ``BalanceSnapshot.currency`` IS the account currency -- the balances body
    carries one currency for the whole account (memo section 1.6 P1) -- so this
    is a legitimate account amount. It is bound to a LOCAL before the pair is
    built for the same reason ``report._as_pairs`` does it: the FX guard in
    ``tests/account_cash/test_money.py`` keys a money pair on a
    ``.currency``-suffixed attribute in the second slot and then reports every
    ``CashEvent`` field name inside it, so the literal pair would report
    ``currency`` -- a booking row's INSTRUMENT currency -- from a record that
    has no such field.

    An absent ``CashBalance`` nulls it with ``account_amount_not_reported``,
    which is what makes the whole check ``not_checkable`` rather than ``0.0``.
    """
    account_currency = _account_currency(at)
    return acct_sum([(at.snapshot.cash_balance, account_currency)], rows=1)


def _verdict(difference: AcctAmount) -> str:
    if difference.value is None:
        return VERDICT_NOT_CHECKABLE
    return (
        VERDICT_RECONCILES if abs(difference.value) <= BALANCE_TOLERANCE_ACCT else VERDICT_DIFFERS
    )


def _not_checkable(kind: str, reason: str, *, asof: str | None = None) -> BalanceCheck:
    """A check nobody could make: three named absences and no number."""
    absent = acct_null(reason_for(reason), None, rows=0)
    return BalanceCheck(
        kind=kind,
        verdict=VERDICT_NOT_CHECKABLE,
        expected=absent,
        observed=absent,
        difference=absent,
        detects_restatement=kind == CHECK_INCREMENTAL,
        rows=0,
        since=None,
        until=None,
        asof=asof,
        null_reason=reason,
    )


def reason_for(check_null_reason: str) -> str:
    """The money vocabulary's reason for a check that could not be made.

    :class:`~.money.AcctAmount` validates its ``null_reason`` against its own
    registry, and this module's three reasons are about the CHECK rather than
    about a money field, so they are published on
    :attr:`BalanceCheck.null_reason` and the amounts carry the money
    vocabulary's ``account_amount_not_reported`` -- the account figure was not
    reported to this check. One mapping, here, rather than three registrations
    in a vocabulary whose consumers are money lines.
    """
    if check_null_reason not in CHECK_NULL_REASONS:
        raise ValueError(f"{check_null_reason!r} is not a registered check null reason")
    return "account_amount_not_reported"


def cumulative_check(
    events: Sequence[CashEvent],
    latest: BalanceAt | None,
    *,
    since: str | None,
    until: str | None,
) -> BalanceCheck:
    """The cash book since ``history_begins`` against the latest ``CashBalance``.

    ``events`` is EVERY stored booking row, not the window's: this check is
    store-wide by definition, and ``since`` / ``until`` are the store's own
    watermarks so the published line says which basis it used.
    """
    if latest is None:
        return _not_checkable(CHECK_CUMULATIVE, NULL_NO_BALANCE_SNAPSHOT)
    expected = acct_sum(
        [(event.amount_account_currency, event.account_currency) for event in events],
        rows=len(events),
        currency=_account_currency(latest),
    )
    observed = _cash_as_acct(latest)
    difference = acct_difference(expected, observed)
    return BalanceCheck(
        kind=CHECK_CUMULATIVE,
        verdict=_verdict(difference),
        expected=expected,
        observed=observed,
        difference=difference,
        # A cumulative sum against a current balance cannot tell a corrected
        # old row from a right one: both sides move together.
        detects_restatement=False,
        rows=len(events),
        since=since,
        until=until,
        asof=latest.snapshot.asof.isoformat(),
        null_reason=None,
    )


def _window_of(record: SweepRecord) -> tuple[dt.date, dt.date] | None:
    try:
        since = dt.date.fromisoformat(record.since)
        until = dt.date.fromisoformat(record.until)
    except ValueError:
        return None
    return (since, until) if since <= until else None


def incremental_check(
    events: Sequence[CashEvent],
    *,
    snapshots: Sequence[BalanceAt],
    sweeps: Sequence[SweepRecord],
) -> BalanceCheck:
    """The change between the last two snapshots against the rows between them.

    The interval is ``[earlier sweep's until + 1 day, later sweep's until]``,
    in trade dates. The one-day offset is what keeps a row the earlier
    snapshot already reflected out of the later sum; the cadence rule (no
    sweep before about 00:30 UTC for the preceding trade date) is what makes
    that true, because a row dated inside the earlier window but settled after
    its snapshot would break it.
    """
    if not snapshots:
        return _not_checkable(CHECK_INCREMENTAL, NULL_NO_BALANCE_SNAPSHOT)
    if len(snapshots) < 2:
        return _not_checkable(
            CHECK_INCREMENTAL,
            NULL_ONE_SNAPSHOT_ONLY,
            asof=snapshots[-1].snapshot.asof.isoformat(),
        )

    earlier, later = snapshots[-2], snapshots[-1]
    windows = {record.sweep_id: _window_of(record) for record in sweeps}
    earlier_window = windows.get(earlier.sweep_id)
    later_window = windows.get(later.sweep_id)
    if earlier_window is None or later_window is None:
        return _not_checkable(
            CHECK_INCREMENTAL,
            NULL_SWEEP_WINDOW_NOT_FOUND,
            asof=later.snapshot.asof.isoformat(),
        )

    first = earlier_window[1] + dt.timedelta(days=1)
    last = later_window[1]
    settled = [event for event in events if _settles_in(event, first, last)]
    expected = acct_sum(
        [(event.amount_account_currency, event.account_currency) for event in settled],
        rows=len(settled),
        currency=_account_currency(later),
    )
    observed = acct_difference(_cash_as_acct(later), _cash_as_acct(earlier))
    difference = acct_difference(expected, observed)
    return BalanceCheck(
        kind=CHECK_INCREMENTAL,
        verdict=_verdict(difference),
        expected=expected,
        observed=observed,
        difference=difference,
        # This is the half that can: a corrected old row changes the cash book
        # for an interval whose balance change is already recorded.
        detects_restatement=True,
        rows=len(settled),
        since=first.isoformat(),
        until=last.isoformat(),
        asof=later.snapshot.asof.isoformat(),
        null_reason=None,
    )


def _settles_in(event: CashEvent, first: dt.date, last: dt.date) -> bool:
    """``event.date`` -- the TRADE date -- inside the interval.

    ``value_date`` is deliberately not read: the venue stamps it with the next
    day on every row, so reading it moves every row of a settlement batch out
    of the interval that batch explains.
    """
    if event.date is None:
        return False
    try:
        stamped = dt.date.fromisoformat(event.date)
    except ValueError:
        return False
    return first <= stamped <= last
