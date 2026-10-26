"""The cash book's answer: one window, one set of published lines (#1689).

Memo: ``docs/research/account_cashbook_design_2026_10_05.md`` sections 4, 5
and 6. This is the ONLY module in the package that reads the store, so it is
the only one that knows ``env``; :mod:`money`, :mod:`buckets`,
:mod:`episodes` and :mod:`balance_check` are pure over what they are handed,
and this composes them.

Five rules carry the published numbers, and a change must not break any of
them.

**The window-scoped lines say so, and the store-wide ones say that instead.**
The cash-event buckets and ``cash_book_sum_acct`` are over the booking rows
INSIDE the window: a report that states a window in its header and publishes a
store-wide total is a number that lies about its own basis. The CUMULATIVE
balance check is store-wide by definition -- the cash book since
``history_begins`` against the latest balance snapshot -- so it publishes its
OWN row count and its own since/until on its own line and can never be read as
the window's figure. ``cost_basis_open_acct`` is the third case: it keeps the
FULL cost of every open episode and publishes its own as-of, because the cost
of a still-open position is as-of now and not as-of the window's end. On the
real capture at ``until=2026-09-12`` a window-clipped basis would be -466.98
of the -3594.86 that is actually at risk.

**Episodes are summed when their CLOSED date falls in the window**, and one
that closed outside it is excluded with its own reason
(:data:`EXCLUSION_CLOSED_OUTSIDE_WINDOW`) so that ``episodes_summed +
episodes_excluded == len(episodes)`` stays arithmetically true and
``basis.exclusions`` names where they went. The derivation itself takes no
window (see :mod:`episodes`): an episode clipped by a presentation window is
indistinguishable from one clipped by the store, and only the second is a real
incompleteness.

**The residual is a published line, not a reconciliation step.** Episodes are
summable and their sum is NOT the period total (memo section 4): the
difference is real money -- a dividend arriving after an episode closed,
withholding tax, financing, a deposit, and the entry legs of an episode that
closed outside the window. ``residual_acct`` is that difference, computed by
the same summing function as everything else, and it is zero only by accident
on a window that happens to contain every episode whole.

**Four partitions are checked, not described.** The bucket identity (the
buckets add back to the rows they were given), the fill coverage partition
(assigned + unassigned == the fills read), the row partition (window + outside
+ undated == the store's rows) and the classification count (every row handed
to the classifier was counted by it). Each raises
:class:`CashBookIdentityError` on its own rather than publishing a number
beside a broken identity, which is the one thing they exist to prevent. The
episode partition -- summed + excluded == episodes -- is held by construction
instead, because :func:`_exclusions_of` stamps each episode exactly once.

The bucket identity is arithmetic, so it needs a number on both sides: when
neither side has one -- no account currency stated anywhere, or two of them --
it is unevaluable rather than broken, and the report publishes the named
absence on that line instead of a total (see :func:`_agree`). A lost row is
still caught there by the fourth partition, which compares COUNTS and reads no
amount at all.

The one relation NOT checked here is ``subtotal + deposits ==
cash_book_sum_acct``: the residual is DEFINED as the cash book sum minus the
five named lines, so that equation is algebraically guaranteed and asserting
it would be noise. ``test_report.py`` pins both sides against literals
instead, which is what catches the deposits being folded into the subtotal.

**The balance cross-check comes in two versions and the weaker one says so.**
The cumulative check carries ``detects_restatement: False`` on its face: a
check that only ever says yes cannot see a correction to an old row, and
without that field "reconciles" reads as proof the history is intact, which it
is not (memo section 5(c)).

What is deliberately NOT here: the ``TotalValue`` half of the account identity
(memo section 1.6 P2), because the market value of the open positions needs a
price and no capture in this tree carries one; and the pick join, which is a
later phase and changes no number.
"""

from __future__ import annotations

import datetime as dt
import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from alphalens_pipeline.account_cash import balance_check, buckets, episodes, paths, store
from alphalens_pipeline.account_cash.codec import SweepRecord
from alphalens_pipeline.account_cash.money import (
    BALANCE_TOLERANCE_ACCT,
    AcctAmount,
    acct_difference,
    acct_sum,
)
from alphalens_pipeline.brokers.account_cash import CashEvent

#: What this source cannot detect, published on every total rather than left in
#: a memo (amendment W8). An episode whose entry is PARTLY before the store --
#: bought 10 before the first sweep, bought 10 inside it, sold 10 inside --
#: never takes the quantity walk negative, so it reads as a complete round trip
#: with half the position and no ``window_clipped`` can fire. The containment
#: is :func:`coverage_of`'s watermarks plus the
#: :data:`REFUSED_BEFORE_FIRST_SWEEP` refusal, never a smaller number.
UNDETECTED_PARTIAL_ENTRY = "entry_partly_before_history"

#: A closed episode whose closing date falls OUTSIDE the window. The fourth
#: exclusion reason, owned here rather than by :mod:`episodes`, because it is a
#: fact about the presentation window and not about the rows. Keeping it in the
#: same counted vocabulary is what keeps ``episodes_summed +
#: episodes_excluded == len(episodes)`` addable.
EXCLUSION_CLOSED_OUTSIDE_WINDOW = "closed_outside_window"

#: Every reason an episode can be left out of the summed figure, in render
#: order. The first three are :mod:`episodes`' own, stamped by its first-hit
#: rule; the fourth is this module's. The counts therefore ADD UP to
#: ``episodes_excluded``.
REPORT_EXCLUSION_ORDER: tuple[str, ...] = (
    *episodes.EXCLUSION_ORDER,
    EXCLUSION_CLOSED_OUTSIDE_WINDOW,
)

#: The window could not be answered at all. One of these, never a ``0.0``.
REFUSED_SIM_REPORTS_UNUSABLE = "sim_reports_unusable"
REFUSED_BEFORE_FIRST_SWEEP = "before_first_sweep"
REFUSED_NOTHING_SWEPT = "nothing_swept"
TOTALS_REFUSALS: tuple[str, ...] = (
    REFUSED_SIM_REPORTS_UNUSABLE,
    REFUSED_BEFORE_FIRST_SWEEP,
    REFUSED_NOTHING_SWEPT,
)

#: The journals whose ``malformed`` and ``restated`` counters reach the basis.
#: ``sweeps.jsonl`` is NOT among them: ``store.read_sweeps`` does not publish
#: its counters, so a sweeps line that will not decode is invisible here -- the
#: record simply does not exist, and :func:`coverage_of` reports its window as
#: a gap. That is the safe direction, because a ``--since`` inside a gap earlier
#: than ``history_begins`` refuses rather than answering smaller, but the CAUSE
#: is not named. Widening that reader is a separate change with 22 call sites
#: and no consumer asking for it yet.
JOURNAL_CASH_EVENTS = "cash_events"
JOURNAL_TRADE_ROWS = "trade_rows"
JOURNAL_BALANCES = "balances"
JOURNALS: tuple[str, ...] = (JOURNAL_CASH_EVENTS, JOURNAL_TRADE_ROWS, JOURNAL_BALANCES)

#: How far two sides of an ARITHMETIC identity may differ. Six orders of
#: magnitude under :data:`BALANCE_TOLERANCE_ACCT`, and a different claim from
#: it: the grosz tolerance is the width of agreement between two independently
#: computed money figures (the cash book and the broker's own balance), while
#: this is the residual between a ``Decimal`` accumulation and its ``float``
#: publication, measured below 1e-9 on the real 104 rows. Using the grosz here
#: would let a dropped half-grosz row satisfy an identity.
_IDENTITY_EPSILON_ACCT = 1e-9

__all__ = [
    "BALANCE_TOLERANCE_ACCT",
    "EXCLUSION_CLOSED_OUTSIDE_WINDOW",
    "JOURNALS",
    "JOURNAL_BALANCES",
    "JOURNAL_CASH_EVENTS",
    "JOURNAL_TRADE_ROWS",
    "REFUSED_BEFORE_FIRST_SWEEP",
    "REFUSED_NOTHING_SWEPT",
    "REFUSED_SIM_REPORTS_UNUSABLE",
    "REPORT_EXCLUSION_ORDER",
    "TOTALS_REFUSALS",
    "UNDETECTED_PARTIAL_ENTRY",
    "Basis",
    "CashBookIdentityError",
    "CashBookTotals",
    "CashBookTotalsRefusedError",
    "Coverage",
    "build_totals",
    "coverage_of",
    "history_begins",
]


class CashBookTotalsRefusedError(Exception):
    """The window cannot be answered. ``reason`` is one of :data:`TOTALS_REFUSALS`."""

    def __init__(self, reason: str, env: str) -> None:
        super().__init__(f"the account cash book will not answer this window on {env}: {reason}")
        self.reason = reason
        self.env = env


class CashBookIdentityError(AssertionError):
    """A published identity does not hold, so no number is published at all.

    A raise rather than a flag on the body, and that is the whole design of
    memo section 5: a dropped row does not vanish, it makes an identity fail.
    Publishing a total beside a broken identity is the one outcome the three
    identities exist to prevent -- the reader would see a number and a
    footnote, and would read the number.
    """


def _as_pairs(
    amounts: Sequence[AcctAmount], *, currency: str | None
) -> list[tuple[float | None, str | None]]:
    """Published totals, as pairs :func:`~.money.acct_sum` can add.

    ``currency`` is the ACCOUNT's, and it is supplied for any line that states
    none of its own. That is money.py's own documented pattern -- its
    ``test_an_empty_total_is_a_unit_less_zero`` adds the unit-less zero back
    through ``acct_sum`` with the currency named beside it -- and without it an
    EMPTY window could not be answered at all: every bucket total arrives as a
    unit-less zero, ``acct_sum`` refuses a pair whose currency is ``None`` with
    ``account_amount_not_reported`` BEFORE its own ``currency=`` fallback
    applies, and the bucket identity then compared a null against ``0.00`` and
    raised. Measured on the real capture at ``until=2026-08-05``, a window the
    store covers and the account was quiet in.

    Substituting is safe because the only unit-less NUMBER this layer produces
    is ``acct_sum`` over an empty sequence -- a zero, which money.py names as a
    neutral element that cannot launder a unit. A line that is a named ABSENCE
    keeps its ``None`` value, so the all-or-nothing rule still nulls the sum.

    The currency is bound to a LOCAL before the pair is built, and that is not
    cosmetic. ``tests/account_cash/test_money.py``'s FX guard keys a money pair
    on a ``.currency``-suffixed attribute in the second slot and then reports
    every ``CashEvent`` field name inside it -- so ``(line.value,
    line.currency)`` would report ``currency``, the INSTRUMENT currency of a
    booking row, and turn the guard red on a line that is nothing of the kind.
    The guard is conservative by NAME because it cannot know a type, which is
    the safe direction over booking rows and a false positive over an
    already-published total.

    The alternative was to exempt this module from the ``currency`` half of the
    guard, and it is worse: ``CashEvent.currency`` is a real hazard HERE too,
    because this module is the one that reads cash events out of the store. The
    local keeps the guard STRICTLY applied -- no booking field may reach a sum
    in this module at all -- at the cost of one named helper.
    """
    pairs: list[tuple[float | None, str | None]] = []
    for amount in amounts:
        unit = amount.currency if amount.currency is not None else currency
        pairs.append((amount.value, unit))
    return pairs


def _in_account_currency(amount: AcctAmount, currency: str | None) -> AcctAmount:
    """A total with the account's currency stated on it (memo section 5(d)).

    Only a value that states NO currency is touched, and the only such value
    this layer produces is the zero :func:`~.money.acct_sum` publishes for an
    empty sequence. A total that states one keeps it -- a disagreement must
    stay ``currency_mixed`` rather than be overwritten -- and a named absence
    keeps its reason.

    The need is :func:`~.buckets.classify_buckets` reading its currency off
    the rows it was handed: with no rows there is nothing to read it from, and
    its own docstring says naming the account's currency on an empty window is
    the caller's job. This is the caller.
    """
    if amount.null_reason is not None or amount.currency is not None:
        return amount
    return AcctAmount(value=amount.value, currency=currency, rows=amount.rows, null_reason=None)


# --- Coverage: what the store can speak for ----------------------------------


@dataclass(frozen=True)
class Coverage:
    """The window the store can answer for, and where it cannot.

    ``swept_through`` is the end of the CONTIGUOUS run that starts at
    ``history_begins``, never ``max(until)``: two sweeps over 08-01..08-10 and
    09-01..09-10 leave a 21-day hole, and ``max(until)`` claims coverage
    through 09-10 across it.

    ``history_begins`` is the earliest window ASKED for, not the earliest row
    stored. The store can speak for a day it read and found empty; it cannot
    speak for a day nobody asked about. On the real capture the two differ by
    five days (asked 2026-08-01, earliest booking 2026-08-06).
    """

    history_begins: str | None
    swept_through: str | None
    #: ``(first uncovered, last uncovered)`` ISO date pairs, every hole between
    #: the runs, not only the first -- a shorter list reads as fuller coverage.
    gaps: tuple[tuple[str, str], ...]
    sweeps: int
    #: Records whose ``since`` / ``until`` would not parse as dates. Counted
    #: rather than skipped: the codec takes both as free text, so a hand-edited
    #: line decodes, and dropping it would shrink the coverage silently.
    malformed_windows: int
    #: Named incompleteness this source cannot detect at all.
    cannot_detect: tuple[str, ...]

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "history_begins": self.history_begins,
            "swept_through": self.swept_through,
            "gaps": [list(gap) for gap in self.gaps],
            "sweeps": self.sweeps,
            "malformed_windows": self.malformed_windows,
            "cannot_detect": list(self.cannot_detect),
        }


def history_begins(sweeps: Sequence[SweepRecord]) -> str | None:
    """The earliest window any sweep ASKED for, or ``None`` if none did."""
    windows = [window for window in map(store.sweep_window, sweeps) if window is not None]
    return min(since for since, _ in windows).isoformat() if windows else None


def coverage_of(sweeps: Sequence[SweepRecord]) -> Coverage:
    """Merge the asked windows into contiguous runs and publish the watermarks.

    Two windows are one run when the second starts no later than the day AFTER
    the first ends: back-to-back sweeps of 08-01..08-10 and 08-11..08-20 cover
    08-01..08-20 with no hole, and a contiguity test written ``next.since <=
    prev.until`` reports a one-day gap between every such pair.
    """
    windows = sorted(window for window in map(store.sweep_window, sweeps) if window is not None)
    malformed = len(sweeps) - len(windows)
    if not windows:
        return Coverage(
            history_begins=None,
            swept_through=None,
            gaps=(),
            sweeps=len(sweeps),
            malformed_windows=malformed,
            cannot_detect=(UNDETECTED_PARTIAL_ENTRY,),
        )

    runs: list[list[dt.date]] = [[*windows[0]]]
    for since, until in windows[1:]:
        if since <= runs[-1][1] + dt.timedelta(days=1):
            runs[-1][1] = max(runs[-1][1], until)
        else:
            runs.append([since, until])

    gaps = tuple(
        (
            (earlier[1] + dt.timedelta(days=1)).isoformat(),
            (later[0] - dt.timedelta(days=1)).isoformat(),
        )
        for earlier, later in itertools.pairwise(runs)
    )
    return Coverage(
        history_begins=runs[0][0].isoformat(),
        # The end of the FIRST run, so a hole cannot be claimed as covered.
        swept_through=runs[0][1].isoformat(),
        gaps=gaps,
        sweeps=len(sweeps),
        malformed_windows=malformed,
        cannot_detect=(UNDETECTED_PARTIAL_ENTRY,),
    )


# --- The published record ----------------------------------------------------


@dataclass(frozen=True)
class Basis:
    """What every total on this report rests on (memo section 5(d)).

    ``basis`` is structurally inseparable from the numbers: it is an object,
    not prose, and the human render prints it ABOVE the figures. Four
    partitions in here are checked rather than described -- the rows, the
    fills, the episodes, and the buckets against the rows.

    The row counts are in two spaces on purpose and the names say which:
    ``rows_*`` count BOOKING rows (the cash book), ``fills_*`` count TRADES
    rows (the executions). ``episodes_*`` count derived spans. A count in one
    space printed beside a threshold in another is this repo's own named
    defect.
    """

    currency: str | None
    window_since: str
    window_until: str
    episodes: int
    episodes_summed: int
    episodes_excluded: int
    exclusions: Mapping[str, int]
    rows_in_store: int
    rows_read: int
    rows_outside_window: int
    #: Booking rows carrying no ``Date`` at all. Excluded from the window and
    #: COUNTED: excluding them cannot inflate a total, and this count is what
    #: says it happened.
    rows_undated: int
    rows_classified: int
    fills_read: int
    fills_assigned: int
    fills_unassigned: Mapping[str, int]
    restated: Mapping[str, int]
    malformed: Mapping[str, int]

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "window_since": self.window_since,
            "window_until": self.window_until,
            "episodes": self.episodes,
            "episodes_summed": self.episodes_summed,
            "episodes_excluded": self.episodes_excluded,
            "exclusions": dict(self.exclusions),
            "rows_in_store": self.rows_in_store,
            "rows_read": self.rows_read,
            "rows_outside_window": self.rows_outside_window,
            "rows_undated": self.rows_undated,
            "rows_classified": self.rows_classified,
            "fills_read": self.fills_read,
            "fills_assigned": self.fills_assigned,
            "fills_unassigned": dict(self.fills_unassigned),
            "restated": dict(self.restated),
            "malformed": dict(self.malformed),
        }


@dataclass(frozen=True)
class CashBookTotals:
    """One window's answer: the lines, the basis they rest on, the coverage.

    The render order of the money lines is memo section 6's: the realized net,
    the cost basis of what is still open, income, unclassified, the residual,
    then the SUBTOTAL, then the deposits BELOW it labelled not P&L, then the
    cash book sum. Deposits sit below the subtotal because 24 000 PLN of
    funding is the easiest number in this repo to misread as profit.
    """

    env: str
    basis: Basis
    coverage: Coverage
    net_closed_acct: AcctAmount
    cost_basis_open_acct: AcctAmount
    #: The as-of of the line above, which is NOT the window's end. The cost of
    #: a position that is still open is as-of the last day the store can speak
    #: for; clipping it to the window's end publishes a fraction of the money
    #: at risk as if it were all of it (amendment W1).
    #:
    #: It is ``swept_through`` -- the end of the CONTIGUOUS run -- and not the
    #: latest sweep's end, even though the basis is summed over every stored
    #: fill including ones swept after a gap. That is the conservative
    #: direction and the only defensible one: past a gap the quantity walk may
    #: have missed an entry, so the position it reports is not known to be the
    #: position held. Stating the later date would claim the basis is current
    #: as of a day whose fills the store never read.
    cost_basis_open_asof: str | None
    income_acct: AcctAmount
    unclassified_acct: AcctAmount
    residual_acct: AcctAmount
    subtotal_acct: AcctAmount
    deposits_acct: AcctAmount
    cash_book_sum_acct: AcctAmount
    buckets: tuple[buckets.Bucket, ...]
    #: Both versions, cumulative first. The cumulative one carries
    #: ``detects_restatement: False`` on its face, so "reconciles" can never
    #: be read as proof the history is intact (memo section 5(c)).
    balance_checks: tuple[balance_check.BalanceCheck, ...]

    def lines(self) -> dict[str, AcctAmount]:
        """Every published money line by name, in render order."""
        return {
            "net_closed_acct": self.net_closed_acct,
            "cost_basis_open_acct": self.cost_basis_open_acct,
            "income_acct": self.income_acct,
            "unclassified_acct": self.unclassified_acct,
            "residual_acct": self.residual_acct,
            "subtotal_acct": self.subtotal_acct,
            "deposits_acct": self.deposits_acct,
            "cash_book_sum_acct": self.cash_book_sum_acct,
        }

    def to_jsonable(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "env": self.env,
            "basis": self.basis.to_jsonable(),
            "coverage": self.coverage.to_jsonable(),
            "cost_basis_open_asof": self.cost_basis_open_asof,
            "buckets": [bucket.to_jsonable() for bucket in self.buckets],
            "balance_checks": [check.to_jsonable() for check in self.balance_checks],
        }
        body.update({name: line.to_jsonable() for name, line in self.lines().items()})
        return body


# --- The window ---------------------------------------------------------------


@dataclass(frozen=True)
class _EventWindow:
    """The store's booking rows split by the window, with nothing dropped."""

    inside: tuple[CashEvent, ...]
    outside: int
    undated: int


def _split_by_window(
    events: Sequence[CashEvent], *, since: dt.date, until: dt.date
) -> _EventWindow:
    """Booking rows inside the window, and counts for the two ways out.

    The date basis is the vendor's ``Date``, which carries the TRADE date on
    every row (memo section 1.7 P9): the settlement crosses midnight UTC, so
    ``ValueDate`` is the next day and the two agree on only 5 of 104 rows.
    """
    inside: list[CashEvent] = []
    outside = 0
    undated = 0
    for event in events:
        stamped = _as_date(event.date)
        if stamped is None:
            undated += 1
        elif since <= stamped <= until:
            inside.append(event)
        else:
            outside += 1
    return _EventWindow(inside=tuple(inside), outside=outside, undated=undated)


def _as_date(value: str | None) -> dt.date | None:
    """One vendor date string as a date, or ``None`` when it will not parse."""
    if value is None:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _account_currency(events: Sequence[CashEvent], latest: store.BalanceAt | None) -> str | None:
    """The account's one currency, from the STORE's rows or the balance body.

    Read over every stored row rather than over the window's, so a window with
    no booking rows in it still names the account's currency -- which is what
    :func:`~.buckets.classify_buckets` says is the caller's job, since it can
    only read what it was handed. The balance snapshot is the fallback for an
    empty store.

    **Two disagreeing rows answer ``None``, and so does no row at all.** The
    consequence is that no unit is substituted anywhere, so each line refuses
    on its own terms rather than borrowing one: ``currency_mixed`` where the
    disagreeing rows actually meet, ``account_amount_not_reported`` or
    ``currency_not_stated`` where a unit-less operand reaches a sum, and a
    unit-less ``0.00`` where there is nothing to add. A line whose own rows
    are all in one currency still states it and still carries a number -- a
    second currency on one booking row says nothing about the trades rows --
    which is why this is not a report-wide refusal. Measured 2026-10-26 on the
    real capture with one row restated to ``EUR``.
    """
    stated = {event.account_currency for event in events if event.account_currency is not None}
    if len(stated) == 1:
        return next(iter(stated))
    if stated:
        return None
    return latest.snapshot.currency if latest is not None else None


def _resting_on(amount: AcctAmount, rows: int) -> AcctAmount:
    """``amount`` with the row count the published line actually rests on.

    :func:`~.money.acct_difference` adds both sides' counts, which is right
    for a difference of two independent sums and wrong for a line like the
    residual, computed by subtracting five lines from one total: the counts
    would add to six times the window. Re-stated once, HERE, rather than by
    letting each caller build its own ``AcctAmount`` -- the value and the null
    reason still come from the one summer.

    Written as a constructor call rather than ``dataclasses.replace``: the
    package's own anti-compaction gate
    (``tests/account_cash/test_no_compactor_reaches_the_cash_book.py``) refuses
    any ``.replace`` attribute call in this package by NAME, because
    ``os.replace`` is how a compactor arrives and a name-keyed gate cannot
    tell the two apart. Naming the four fields is also the more readable form
    here: it shows that only the count changes.
    """
    return AcctAmount(
        value=amount.value,
        currency=amount.currency,
        rows=rows,
        null_reason=amount.null_reason,
    )


# --- The identities, checked ---------------------------------------------------


def _agree(left: AcctAmount, right: AcctAmount) -> bool:
    """True when two money records say the same thing.

    Compared on the VALUE, not on the currency: an empty bucket publishes a
    unit-less zero by design (``acct_sum`` has nothing to read a currency
    from), and a currency disagreement already refuses on its own with
    ``currency_mixed`` on both sides.

    **A null on BOTH sides agrees, whatever the two reasons are.** There is no
    arithmetic to check when neither side carries a number, and the report
    then publishes the named absence on that line rather than a total -- so
    there is no figure for this identity to have protected. Requiring the same
    REASON was the stricter form and it refused two real inputs: a store with
    no stated account currency at all (the per-bucket sum nulls with
    ``account_amount_not_reported`` while the rows' own total is a plain
    ``0.00``), and a store holding two account currencies (``currency_mixed``
    on the rows against ``account_amount_not_reported`` on the buckets, which
    is the degradation path :func:`_account_currency` documents). Both ended
    in an ``AssertionError`` on the money path.
    A null on exactly ONE side still refuses, because that asymmetry IS a
    signal: drop the one row whose amount the vendor did not report and the
    bucket side becomes a number while the rows' total stays a null.

    What keeps a lost row caught while both sides are nulls is the fourth
    partition, ``bucket row count``: it compares COUNTS and reads no amount at
    all.
    """
    if (left.null_reason is not None) or (right.null_reason is not None):
        return (left.null_reason is not None) and (right.null_reason is not None)
    assert left.value is not None and right.value is not None  # the dataclass invariant
    return abs(left.value - right.value) <= _IDENTITY_EPSILON_ACCT


def _hold(name: str, held: bool, detail: str) -> None:
    if not held:
        raise CashBookIdentityError(
            f"the {name} identity does not hold ({detail}); no total is published, "
            f"because a number beside a broken identity is read as the number"
        )


def _check_identities(
    *,
    bucket_fold: buckets.BucketFold,
    episode_fold: episodes.EpisodeFold,
    window: _EventWindow,
    rows_in_store: int,
    currency: str | None,
) -> None:
    """The four partitions, in the order a dropped row would break them.

    Each one is a separate claim and each raises on its own, so none of them
    can be satisfied by another holding.
    """
    summed_buckets = acct_sum(
        _as_pairs([bucket.total for bucket in bucket_fold.buckets], currency=currency),
        rows=bucket_fold.rows,
        currency=currency,
    )
    # The rows' own total goes through the SAME summer and the SAME unit
    # substitution as the buckets it must agree with. Comparing the raw
    # ``all_rows`` against a re-summed bucket side made the identity's two
    # sides two different computations, which is this repo's own named defect
    # (#1227): with no account currency anywhere the bucket side nulled and
    # ``all_rows`` was a plain ``0.00``, and the identity refused a store it
    # should have answered for. The value is untouched by the substitution --
    # only a unit-less number is given a unit -- so a doctored ``all_rows``
    # still raises.
    all_rows = acct_sum(
        _as_pairs([bucket_fold.all_rows], currency=currency),
        rows=bucket_fold.rows,
        currency=currency,
    )
    _hold(
        "bucket",
        _agree(summed_buckets, all_rows),
        f"buckets {summed_buckets.rendered} against rows {all_rows.rendered}",
    )
    _hold(
        "fill coverage",
        episode_fold.fills_assigned + len(episode_fold.unassigned) == episode_fold.fills_read,
        f"{episode_fold.fills_assigned} assigned + {len(episode_fold.unassigned)} "
        f"unassigned against {episode_fold.fills_read} read",
    )
    _hold(
        "row",
        len(window.inside) + window.outside + window.undated == rows_in_store,
        f"{len(window.inside)} in the window + {window.outside} outside + "
        f"{window.undated} undated against {rows_in_store} in the store",
    )
    _hold(
        "bucket row count",
        bucket_fold.rows == len(window.inside),
        f"{bucket_fold.rows} classified against {len(window.inside)} given",
    )


def _exclusions_of(
    found: Sequence[episodes.CashEpisode], *, since: dt.date, until: dt.date
) -> tuple[tuple[episodes.CashEpisode, ...], dict[str, int]]:
    """The episodes summed, and the counts by reason for the rest.

    An episode is summed when its CLOSING date falls in the window. One that
    closed outside it is excluded under this module's own reason rather than
    by any of :mod:`episodes`' three, which is what keeps the counts addable;
    one with no readable closing date is outside EVERY window and is counted
    there too -- it cannot inflate a total, and the reason says where it went.
    """
    counts = dict.fromkeys(REPORT_EXCLUSION_ORDER, 0)
    summed: list[episodes.CashEpisode] = []
    for episode in found:
        if episode.exclusion_reason is not None:
            counts[episode.exclusion_reason] += 1
            continue
        closed = _as_date(episode.closed_date)
        if closed is not None and since <= closed <= until:
            summed.append(episode)
        else:
            counts[EXCLUSION_CLOSED_OUTSIDE_WINDOW] += 1
    return tuple(summed), counts


def _cost_basis_open(found: Sequence[episodes.CashEpisode], currency: str | None) -> AcctAmount:
    """The FULL cost of every open episode, at zero when none is open.

    Not window-clipped (amendment W1): the cost of a position that is still
    open is as-of the last day the store can speak for, and a clipped one
    answers -466.98 of the -3 594.86 actually at risk on the real capture at
    ``until=2026-09-12``.

    **With no open episode this is ``0.00`` over 0 episodes, and NOT
    ``money.NULL_NEVER_OPENED``**, although that constant names this exact
    line. The reason is measured: a named absence propagates through the
    all-or-nothing rule into the residual and the subtotal, so an account that
    is FLAT -- the ordinary state between positions -- published
    ``residual: never_opened`` and ``subtotal: account_amount_not_reported``
    and a report nobody could read. The row count carries the fact instead,
    which is memo section 5(a)'s own pattern for ``unclassified``: the line is
    printed at zero, and ``0.00 / 0 episodes`` says no position is open while
    leaving a non-zero one later readable as a changed number.

    The all-or-nothing rule is untouched by this, because it is about an
    amount the VENDOR did not report. Zero open episodes is not an unreported
    number; it is a measured empty set, and ``acct_sum`` over an empty
    sequence is the zero this returns.
    """
    still_open = [episode for episode in found if episode.state == episodes.STATE_OPEN]
    return acct_sum(
        _as_pairs([episode.cost_basis_acct for episode in still_open], currency=currency),
        rows=len(still_open),
        currency=currency,
    )


# --- The answer ----------------------------------------------------------------


def _refuse_a_non_live_environment(env: str) -> None:
    """``sim_reports_unusable``, BEFORE the first read.

    The store's READERS do not refuse on SIM -- only the appenders do -- and
    ``_iter_payloads`` returns silently on a missing path, so a sum over a SIM
    store answers ``0.0`` for an account whose report endpoints serve OTHER
    accounts' rows. Refused ahead of every read rather than merely ahead of
    every sum, so the order in the code says the same thing this docstring
    does: nothing about SIM is read at all.
    """
    if paths.validate_environment(env) != paths.ENV_LIVE:
        raise CashBookTotalsRefusedError(REFUSED_SIM_REPORTS_UNUSABLE, env)


def _refuse_an_unanswerable_window(env: str, sweeps: Sequence[SweepRecord], since: dt.date) -> None:
    """``nothing_swept``, then ``before_first_sweep``. Both before any sum.

    "Nothing swept" comes first because a store with no sweep record cannot
    say what window it covers, so there is no watermark to compare ``since``
    against.
    """
    begins = history_begins(sweeps)
    if begins is None:
        raise CashBookTotalsRefusedError(REFUSED_NOTHING_SWEPT, env)
    if since < dt.date.fromisoformat(begins):
        raise CashBookTotalsRefusedError(REFUSED_BEFORE_FIRST_SWEEP, env)


def build_totals(*, env: str, since: dt.date, until: dt.date) -> CashBookTotals:
    """The cash book's answer for one window of one environment.

    Reads the store once per journal, composes the two pure derivations, and
    checks the four partitions before publishing anything.

    The window is scoped as amendment W1 settles it: the booking rows and the
    cash book sum are the WINDOW's, the episodes are summed by their CLOSING
    date, the open cost basis is full and carries its own as-of, and the
    cumulative balance check is store-wide and says so on its own line.

    An ``until`` beyond ``swept_through`` is NOT refused. The store can be
    asked about a day nobody swept and answers that it holds nothing for it,
    which is honest as long as the coverage block is published beside the
    number -- and it is. A ``since`` before ``history_begins`` is different and
    does refuse: there the store holds rows whose own entries may predate it,
    and a smaller number would read as a complete answer (memo section 5's
    table, amendment W8).
    """
    if since > until:
        raise ValueError(f"a cash-book window must not end before it starts ({since} .. {until})")
    _refuse_a_non_live_environment(env)
    sweeps = store.read_sweeps(env=env)
    _refuse_an_unanswerable_window(env, sweeps, since)

    coverage = coverage_of(sweeps)
    events_fold = store.read_cash_events(env=env)
    trades_fold = store.read_trade_rows(env=env)
    balances = store.read_balances(env=env)
    latest = balances.snapshots[-1] if balances.snapshots else None
    currency = _account_currency(events_fold.events, latest)

    window = _split_by_window(events_fold.events, since=since, until=until)
    bucket_fold = buckets.classify_buckets(window.inside)
    # Over EVERY stored fill, never the window's: an episode clipped by a
    # presentation window is indistinguishable from one clipped by the store,
    # and only the second is a real incompleteness.
    episode_fold = episodes.derive_episodes(trades_fold.rows)
    # Over what ``classify_buckets`` published, BEFORE the unit is restated
    # below: the identity's subject is what the classification produced.
    _check_identities(
        bucket_fold=bucket_fold,
        episode_fold=episode_fold,
        window=window,
        rows_in_store=len(events_fold.events),
        currency=currency,
    )

    summed, exclusions = _exclusions_of(episode_fold.episodes, since=since, until=until)
    net_closed = acct_sum(
        _as_pairs([episode.net_acct for episode in summed], currency=currency),
        rows=len(summed),
        currency=currency,
    )
    cost_basis_open = _cost_basis_open(episode_fold.episodes, currency)
    published_buckets = tuple(
        buckets.Bucket(
            name=bucket.name,
            bk_amount_types=bucket.bk_amount_types,
            total=_in_account_currency(bucket.total, currency),
            rows=bucket.rows,
        )
        for bucket in bucket_fold.buckets
    )
    by_name = {bucket.name: bucket for bucket in published_buckets}
    income = by_name[buckets.BUCKET_INCOME].total
    unclassified = by_name[buckets.BUCKET_UNCLASSIFIED].total
    deposits = by_name[buckets.BUCKET_DEPOSITS].total
    cash_book_sum = _in_account_currency(bucket_fold.all_rows, currency)

    # The residual is the window's cash movement that the five named lines do
    # not explain -- a dividend after a close, withholding tax, financing, a
    # deposit, and the entry legs of an episode that closed outside the
    # window. Memo section 4: episodes are summable and their sum is NOT the
    # period total, and the difference is real money.
    residual = cash_book_sum
    for line in (net_closed, cost_basis_open, income, unclassified, deposits):
        residual = acct_difference(residual, line)
    # It rests on the whole window, not on a subset of it: the named lines
    # live in two row spaces (booking rows for the buckets, trades rows for
    # the episodes), so no set of booking rows is the residual's own.
    residual = _resting_on(residual, len(window.inside))

    subtotal = acct_sum(
        _as_pairs([net_closed, cost_basis_open, income, unclassified, residual], currency=currency),
        rows=len(window.inside) - deposits.rows,
        currency=currency,
    )
    return CashBookTotals(
        env=env,
        basis=Basis(
            currency=currency,
            window_since=since.isoformat(),
            window_until=until.isoformat(),
            episodes=len(episode_fold.episodes),
            episodes_summed=len(summed),
            episodes_excluded=sum(exclusions.values()),
            exclusions=exclusions,
            rows_in_store=len(events_fold.events),
            rows_read=len(window.inside),
            rows_outside_window=window.outside,
            rows_undated=window.undated,
            rows_classified=bucket_fold.rows,
            fills_read=episode_fold.fills_read,
            fills_assigned=episode_fold.fills_assigned,
            fills_unassigned=_unassigned_by_reason(episode_fold),
            restated={
                JOURNAL_CASH_EVENTS: events_fold.restated,
                JOURNAL_TRADE_ROWS: trades_fold.restated,
                JOURNAL_BALANCES: balances.restated,
            },
            malformed={
                JOURNAL_CASH_EVENTS: events_fold.malformed,
                JOURNAL_TRADE_ROWS: trades_fold.malformed,
                JOURNAL_BALANCES: balances.malformed,
            },
        ),
        coverage=coverage,
        net_closed_acct=net_closed,
        cost_basis_open_acct=cost_basis_open,
        cost_basis_open_asof=coverage.swept_through,
        income_acct=income,
        unclassified_acct=unclassified,
        residual_acct=residual,
        subtotal_acct=subtotal,
        deposits_acct=deposits,
        cash_book_sum_acct=cash_book_sum,
        buckets=published_buckets,
        balance_checks=(
            # Over EVERY stored row, with the store's own watermarks as its
            # basis: this check is store-wide by definition and the balance it
            # is compared against reflects every row, not the window's.
            balance_check.cumulative_check(
                events_fold.events,
                latest,
                since=coverage.history_begins,
                until=coverage.swept_through,
            ),
            balance_check.incremental_check(
                events_fold.events, snapshots=balances.snapshots, sweeps=sweeps
            ),
        ),
    )


def _unassigned_by_reason(fold: episodes.EpisodeFold) -> dict[str, int]:
    """Counts by reason over every registered one, zeros included.

    A reason at zero is printed for the same argument ``unclassified`` is: a
    non-zero one then reads as a CHANGED number rather than as a new row
    appearing in a list the reader has stopped scanning.
    """
    counts = dict.fromkeys(episodes.UNASSIGNED_REASONS, 0)
    for fill in fold.unassigned:
        counts[fill.reason] += 1
    return counts
