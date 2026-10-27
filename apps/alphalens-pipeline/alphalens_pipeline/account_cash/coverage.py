"""What the store can speak for, and where it cannot (#1689).

Memo: ``docs/research/account_cashbook_design_2026_10_05.md`` section 5. One
question is answered here and nothing else: given the sweeps that were run,
which days did somebody ASK the broker about, and which days did nobody ask
about.

**It answers "what can the store speak for", never "what does a number rest
on".** Those are two facts and they are meant to disagree wherever the
coverage has a hole. :attr:`Coverage.swept_through` stops at the end of the
FIRST contiguous run, so a hole can never be claimed as covered; the span a
published total actually summed is stated by that total, on its own line (see
:mod:`balance_check`, whose cumulative check publishes its own since/until).
Handing these watermarks to a figure as its basis publishes a store-wide
number under an ``until`` that stopped at the hole.

**The watermarks are the containment for what this source cannot detect at
all**, which is why :data:`UNDETECTED_PARTIAL_ENTRY` is published on every
total rather than left in a memo.

Pure over the records it is handed: it takes no ``env``, resolves no path and
calls no reader. :func:`~.store.sweep_window` is the one parse of a stored
window, shared with :func:`~.balance_check.incremental_check` so the two
answer the same thing about the same record.
"""

from __future__ import annotations

import datetime as dt
import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from alphalens_pipeline.account_cash import store
from alphalens_pipeline.account_cash.codec import SweepRecord

#: What this source cannot detect, published on every total rather than left in
#: a memo (amendment W8). An episode whose entry is PARTLY before the store --
#: bought 10 before the first sweep, bought 10 inside it, sold 10 inside --
#: never takes the quantity walk negative, so it reads as a complete round trip
#: with half the position and no ``window_clipped`` can fire. The containment
#: is :func:`coverage_of`'s watermarks plus the
#: :data:`~.refusals.REFUSED_BEFORE_FIRST_SWEEP` refusal, never a smaller number.
UNDETECTED_PARTIAL_ENTRY = "entry_partly_before_history"

__all__ = [
    "UNDETECTED_PARTIAL_ENTRY",
    "Coverage",
    "coverage_of",
    "history_begins",
]


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
