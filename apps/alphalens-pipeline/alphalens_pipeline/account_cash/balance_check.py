"""The balance cross-check: does the cash book account for the cash? (#1689)

Memo section 5(c), the third of the three mechanisms by which an incomplete
number announces itself. Each sweep stores a balance snapshot; this asserts
that the account's own cash agrees with what the cash book says moved. **A
disagreement is CONTENT, not a crash:** the check publishes ``differs`` with
the difference as a number, because the one thing a money report must not do
is refuse to print.

Two versions, and the difference matters more than it looks.

**Cumulative** -- the whole stored cash book against the latest
``CashBalance``. Known to hold, and it carries ``detects_restatement: False``
on its face: a check that only ever says yes cannot see a correction to an old
row, and without that field "reconciles" reads as proof the history is intact,
which it is not. It is also STORE-WIDE by definition, so it publishes its own
row count and its own since/until on its own line and can never be read as the
presentation window's figure (amendment W1).

**Its stated basis is the span of the rows it SUMMED**, read off those rows and
never handed in. The report's coverage watermarks answer a different question
-- what the store can speak for -- and ``swept_through`` stops at the end of
the first contiguous run so a hole cannot be claimed as covered. Passing them
here published a store-wide row count under an ``until`` that stopped at the
hole: with sweeps of 08-01..08-10 and 09-01..09-10, ``until`` read 08-10 with
September money inside the number. Clipping the SUM to the first run instead
would make the verdict wrong, which is worse.

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

**That interval is derived, so its DIRECTION is a check and not an assumption.**
A re-run of a sweep over the same window, and a backfill of an older one, both
put the interval's start past its end. It then holds no row, and the empty sum
would be compared against a balance change that is real: measured ``differs``
with -5 000.00 PLN over 0 rows, ``since=2026-10-09``, ``until=2026-10-08`` --
a money line whose own stated interval runs backwards.
:data:`NULL_INTERVAL_RUNS_BACKWARDS` is the fourth named absence, and it
publishes no bounds at all.

**The DIFFERENCE carries no count of its own.** Its two sides are counted in
different spaces -- booking rows on one, balance snapshots on the other -- and
:func:`~.money.acct_difference` adds both counts, so the record published
``rows: 105`` for a 104-row cash book against one snapshot and the human
render printed "difference 0.00 PLN over 105 rows". It rests on the booking
rows the check summed instead, which is :attr:`BalanceCheck.rows`, so one
record states one count for one basis (:func:`_difference_of`).

**A verdict of ``not_checkable`` always names its reason on the RECORD.** It is
reachable two ways: a check that could not be SET UP (the four absences above),
and one that was set up and whose arithmetic produced no number -- no
``CashBalance`` reported, or two stated currencies. The second said
``not_checkable`` with ``null_reason: None`` and left the reason one level down
on the nested amount, in money.py's vocabulary rather than this one's, so a
consumer branching on the record met a verdict it could not explain.
:data:`NULL_DIFFERENCE_NOT_A_NUMBER` is that case, read off the same value the
verdict is, so the two cannot disagree.

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
    acct_resting_on,
    acct_sum,
)
from alphalens_pipeline.account_cash.store import BalanceAt, sweep_window
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
#: The two sweep windows are on disk and the interval they DERIVE runs
#: backwards: the later sweep's window ends no later than the earlier one's, so
#: ``[earlier until + 1 day, later until]`` has its bounds the wrong way round
#: and no row can fall inside it. Two ordinary operator actions reach it -- a
#: re-run of a sweep over the same window, and a backfill of an older one --
#: and both leave two snapshots whose cash may genuinely differ. Without this
#: the empty sum was compared against that real change and published
#: ``differs`` with a money number beside an interval running backwards
#: (measured: -5 000.00 PLN over 0 rows, ``since=2026-10-09``,
#: ``until=2026-10-08``). Registered beside
#: :data:`NULL_SWEEP_WINDOW_NOT_FOUND` because it is the same KIND of fact:
#: the join the check rests on did not produce an interval it may sum.
NULL_INTERVAL_RUNS_BACKWARDS = "interval_runs_backwards"
#: Every input was present and the ARITHMETIC produced no number: the account
#: reported no ``CashBalance``, or two stated currencies reached one sum. The
#: other four reasons say a check could not be SET UP; this one says it was set
#: up and has no difference to compare.
#:
#: Published on the record rather than left on the nested amount alone. The
#: verdict is ``not_checkable`` either way, and a consumer branches on the
#: RECORD -- so without this the record carried a verdict it could not explain
#: and the reason sat one level down, in a different vocabulary. The WHY stays
#: there, in money.py's words (``account_amount_not_reported``,
#: ``currency_mixed``); this says the verdict rests on it.
#:
#: Unlike the other four it is NOT published through :func:`_not_checkable`:
#: the amounts on this path are real computations and must stay on the record.
NULL_DIFFERENCE_NOT_A_NUMBER = "difference_not_a_number"
CHECK_NULL_REASONS: tuple[str, ...] = (
    NULL_NO_BALANCE_SNAPSHOT,
    NULL_ONE_SNAPSHOT_ONLY,
    NULL_SWEEP_WINDOW_NOT_FOUND,
    NULL_INTERVAL_RUNS_BACKWARDS,
    NULL_DIFFERENCE_NOT_A_NUMBER,
)

__all__ = [
    "CHECK_CUMULATIVE",
    "CHECK_INCREMENTAL",
    "CHECK_KINDS",
    "CHECK_NULL_REASONS",
    "NULL_DIFFERENCE_NOT_A_NUMBER",
    "NULL_INTERVAL_RUNS_BACKWARDS",
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
    #: What the account's own cash did: ``CashBalance``, or its change. Its
    #: own ``rows`` counts BALANCE SNAPSHOTS -- one here, two on the
    #: incremental check -- and not booking rows.
    observed: AcctAmount
    #: ``expected - observed``. Its ``rows`` is :attr:`rows`, the booking rows
    #: this check summed: a difference whose sides are counted in two spaces
    #: has no count of its own, and the two added together counted nothing
    #: (``rows: 105`` over a 104-row cash book). See :func:`_difference_of`.
    difference: AcctAmount
    #: Published, never implied. ``False`` on the cumulative check.
    detects_restatement: bool
    # ``null_reason`` is set exactly when ``verdict`` is ``not_checkable``: a
    # consumer branches on this record, so a verdict with no reason beside it
    # is one it cannot explain, and a reason beside a number is a figure that
    # is also a refusal.
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


def _difference_of(expected: AcctAmount, observed: AcctAmount, *, rows: int) -> AcctAmount:
    """``expected - observed``, resting on the BOOKING rows the check summed.

    The two sides are counted in different spaces: ``expected`` is a sum over
    booking rows and ``observed`` is a balance snapshot, or the change between
    two of them. :func:`~.money.acct_difference` adds both counts, which is
    right for a difference of two independent sums over disjoint rows and
    counts nothing here -- measured on the real capture as ``rows: 105``
    against a 104-row cash book, which the human render printed as "difference
    0.00 PLN over 105 rows". There is no 105th anything.

    So the published count is ``rows``: the booking rows this check summed,
    which is also :attr:`BalanceCheck.rows`. One record then states ONE count
    for one basis -- a renderer prints the nested count beside the record's
    own, and two different numbers there are two answers to one question.

    The fix is here and not in :func:`~.money.acct_difference`, whose rule is
    relied on elsewhere and is right for what it documents. Nor is the count
    dropped to ``0``: that is what a check which summed nothing publishes (see
    :func:`_not_checkable`), so a real difference carrying it would read as a
    check that rests on no row.

    ``observed`` keeps its own count, deliberately. On the incremental check
    that count is 2, and two is how many balance snapshots it read -- a count
    of records that exist, in the balances journal rather than in the cash
    book. What the difference carried was two spaces ADDED, which counts
    nothing in either.
    """
    return acct_resting_on(acct_difference(expected, observed), rows=rows)


def _verdict(difference: AcctAmount) -> str:
    if difference.value is None:
        return VERDICT_NOT_CHECKABLE
    return (
        VERDICT_RECONCILES if abs(difference.value) <= BALANCE_TOLERANCE_ACCT else VERDICT_DIFFERS
    )


def _reason_for(difference: AcctAmount) -> str | None:
    """The CHECK's reason when a set-up check still produced no number.

    Read off the same value :func:`_verdict` reads, so the record's verdict and
    its reason cannot disagree: a ``not_checkable`` with no reason, and a
    reason beside a number, are the two states a consumer would misread.
    """
    return None if difference.value is not None else NULL_DIFFERENCE_NOT_A_NUMBER


def _not_checkable(kind: str, reason: str, *, asof: str | None = None) -> BalanceCheck:
    """A check nobody could make: four named absences and no number.

    ``since`` / ``until`` are left unset on purpose. Three of the four reasons
    have no interval to state, and the fourth --
    :data:`NULL_INTERVAL_RUNS_BACKWARDS` -- has one that is not an interval,
    which is the whole reason it refuses.
    """
    absent = acct_null(_money_null_for(reason), None, rows=0)
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


def _money_null_for(check_null_reason: str) -> str:
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


def _span_of(events: Sequence[CashEvent]) -> tuple[str | None, str | None]:
    """The first and last date the rows THEMSELVES carry, as ISO strings.

    The basis of a sum is read off what was summed, never handed in. The
    caller used to pass :func:`~.report.coverage_of`'s watermarks, and those
    answer a different question: that function stops at the end of the FIRST
    contiguous run so that a hole cannot be claimed as covered, while this
    check is handed EVERY stored row. With sweeps of 08-01..08-10 and
    09-01..09-10 it therefore published a row count covering everything under
    an ``until`` of 08-10 -- September money inside a number whose stated
    basis excluded it.

    Clipping the SUM to the first run instead would make the VERDICT wrong,
    which is worse: the balance snapshot reflects every row the account
    booked, not the ones before the hole. So the sum stays store-wide and the
    stated span follows it.

    Rows carrying no readable date contribute no bound: ``CashEvent.date`` is
    optional and the codec stores it as free text, so an absent or
    unparseable one has nothing to put in a span. Such a row is still SUMMED
    -- that is what makes this check store-wide -- so a store holding one has
    a ``rows`` count wider than its span, and that is the honest pair rather
    than a span invented for it or money dropped to make the two agree.

    No row at all is no span, not a zero-length one.
    """
    dated = sorted(
        stamped for stamped in (_stamped(event) for event in events) if stamped is not None
    )
    return (dated[0].isoformat(), dated[-1].isoformat()) if dated else (None, None)


def cumulative_check(events: Sequence[CashEvent], latest: BalanceAt | None) -> BalanceCheck:
    """The whole stored cash book against the latest ``CashBalance``.

    ``events`` is EVERY stored booking row, not the window's: this check is
    store-wide by definition. Its published ``since`` / ``until`` are the span
    of the rows it actually summed (:func:`_span_of`), which is a DIFFERENT
    fact from the coverage watermarks the report publishes beside it -- those
    say what the store can speak for, this says what the number rests on.
    """
    if latest is None:
        return _not_checkable(CHECK_CUMULATIVE, NULL_NO_BALANCE_SNAPSHOT)
    since, until = _span_of(events)
    expected = acct_sum(
        [(event.amount_account_currency, event.account_currency) for event in events],
        rows=len(events),
        currency=_account_currency(latest),
    )
    observed = _cash_as_acct(latest)
    difference = _difference_of(expected, observed, rows=len(events))
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
        null_reason=_reason_for(difference),
    )


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

    **The interval is DERIVED, so it is also checked.** Nothing about the two
    windows guarantees the second ends after the first: a re-run of the same
    window and a backfill of an older one both put ``first`` past ``last``.
    Such a pair holds no row, so the sum would be empty while the two
    snapshots' cash may differ by a real amount --
    :data:`NULL_INTERVAL_RUNS_BACKWARDS` refuses it instead, and publishes no
    bounds, because an interval nobody can form has none to state.
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
    windows = {record.sweep_id: sweep_window(record) for record in sweeps}
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
    if last < first:
        # Refused rather than summed: a backwards pair holds no row, so the
        # empty sum would be compared against a balance change that is real.
        # `last == first` is the ordinary daily cadence -- two back-to-back
        # sweeps one day apart -- so the guard is strict.
        return _not_checkable(
            CHECK_INCREMENTAL,
            NULL_INTERVAL_RUNS_BACKWARDS,
            asof=later.snapshot.asof.isoformat(),
        )

    settled = [event for event in events if _settles_in(event, first, last)]
    expected = acct_sum(
        [(event.amount_account_currency, event.account_currency) for event in settled],
        rows=len(settled),
        currency=_account_currency(later),
    )
    observed = acct_difference(_cash_as_acct(later), _cash_as_acct(earlier))
    difference = _difference_of(expected, observed, rows=len(settled))
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
        null_reason=_reason_for(difference),
    )


def _stamped(event: CashEvent) -> dt.date | None:
    """``event.date`` -- the TRADE date -- as a date, or ``None``.

    ``value_date`` is deliberately not read: the venue stamps it with the next
    day on every row, so reading it moves every row of a settlement batch out
    of the interval that batch explains.

    One parse for both readers of a booking row's date in this module: the
    interval membership test and the cumulative span. ``None`` covers an
    absent value and one the parse refuses, because neither reader has
    anything different to do with the two -- a row with no placeable date
    falls in no interval and bounds no span. The REPORT tells them apart,
    because there a row excluded from a window has to say which way it went.
    """
    if event.date is None:
        return None
    try:
        return dt.date.fromisoformat(event.date)
    except ValueError:
        return None


def _settles_in(event: CashEvent, first: dt.date, last: dt.date) -> bool:
    """``event.date`` inside the interval, both ends included."""
    stamped = _stamped(event)
    return stamped is not None and first <= stamped <= last
