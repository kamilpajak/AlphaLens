"""The cash book's answer: one window, one set of published lines (#1689).

Memo: ``docs/research/account_cashbook_design_2026_10_05.md`` sections 4, 5
and 6. This is the ONLY module in the package that reads the store, so it is
the only one that knows ``env``; :mod:`money`, :mod:`buckets` and
:mod:`episodes` are pure over rows and this composes them.

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

**Three identities are checked, not described.** The bucket identity (the
buckets add back to the rows they were given), the coverage partition
(assigned + unassigned == the fills read) and the row partition (window +
outside + undated == the store's rows). A failure raises
:class:`CashBookIdentityError` rather than publishing a number beside a broken
identity, which is the one thing the identities exist to prevent.

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
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from alphalens_pipeline.account_cash.codec import SweepRecord
from alphalens_pipeline.account_cash.money import AcctAmount

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

__all__ = [
    "EXCLUSION_CLOSED_OUTSIDE_WINDOW",
    "UNDETECTED_PARTIAL_ENTRY",
    "Coverage",
    "coverage_of",
    "history_begins",
]


def _as_pairs(amounts: Sequence[AcctAmount]) -> list[tuple[float | None, str | None]]:
    """Published totals, as pairs :func:`~.money.acct_sum` can add.

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
        unit = amount.currency
        pairs.append((amount.value, unit))
    return pairs


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


def _window_of(record: SweepRecord) -> tuple[dt.date, dt.date] | None:
    """The record's asked window as dates, or ``None`` when it will not parse.

    One parse at the boundary, because the three date vocabularies in this
    layer are different types: ``SweepRecord.since`` / ``.until`` are ISO
    strings, ``SweepPlan.since`` / ``.until`` are ``dt.date``, and
    ``CashEvent.date`` is a raw vendor string. Comparing them unparsed either
    raises or sorts lexically.
    """
    try:
        since = dt.date.fromisoformat(record.since)
        until = dt.date.fromisoformat(record.until)
    except ValueError:
        return None
    return (since, until) if since <= until else None


def history_begins(sweeps: Sequence[SweepRecord]) -> str | None:
    """The earliest window any sweep ASKED for, or ``None`` if none did."""
    windows = [window for window in map(_window_of, sweeps) if window is not None]
    return min(since for since, _ in windows).isoformat() if windows else None


def coverage_of(sweeps: Sequence[SweepRecord]) -> Coverage:
    """Merge the asked windows into contiguous runs and publish the watermarks.

    Two windows are one run when the second starts no later than the day AFTER
    the first ends: back-to-back sweeps of 08-01..08-10 and 08-11..08-20 cover
    08-01..08-20 with no hole, and a contiguity test written ``next.since <=
    prev.until`` reports a one-day gap between every such pair.
    """
    windows = sorted(window for window in map(_window_of, sweeps) if window is not None)
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
