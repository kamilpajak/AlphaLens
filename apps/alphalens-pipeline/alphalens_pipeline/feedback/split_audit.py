"""Cross-source the label store's price scale against a second vendor (#1533).

WHY THIS EXISTS
---------------
``selection_label`` reads a Polygon grouped-daily store written one parquet per session
and never re-fetched, so each file carries the split adjustment as of its OWN fetch date.
A split after a file is written leaves exactly one unadjusted step, and the return across
that step is fabricated.

The old protection was a price-jump band: fail the row when a close ratio leaves
``(0.55, 1.8)``. Measured over the whole store, that band caught zero splits and produced
three false positives (one real -47% day, reported identically by a second vendor), while
being structurally blind to 3-for-2, 4-for-3 and 5-for-4 — roughly a fifth of US splits.
It cannot be fixed by widening: catching a 3-for-2 means failing every -33% earnings day.

WHAT REPLACES IT, AND WHY IT IS SHAPED THIS WAY
-----------------------------------------------
A second vendor fetched in ONE piece is uniformly adjusted, so the LEVEL RATIO
``q = store_close / reference_close`` is flat wherever the store's adjustment epoch
matches, and steps to a new flat level exactly where it does not.

The discriminator is therefore PERSISTENCE, not size. That choice is measured, not
argued:

- Over 13203 real session comparisons on 48 tickers with no artefact, ``q`` deviates from
  its own median by 0.000000 at the 99th percentile, 0.000542 at the 99.9th, and by at
  most 0.039087 on a single session. None of the 13203 exceeded 5%.
- On MQ, the one real artefact in the store (1-for-4 reverse), ``q`` is EXACTLY 0.2500 for
  the 20 sessions to 2026-06-29 and EXACTLY 1.0000 for the 22 sessions from 2026-06-30.

A per-session threshold cannot separate those two populations, because the smallest
artefact worth catching is not a 5-for-4 (a 20% step) but a 5% STOCK DIVIDEND (a 4.76%
step), which sits below the 3.9% single-session noise plus any working margin. Taking
MEDIANS on each side of a candidate step drops the noise floor to zero and opens the gap.

A window lying ENTIRELY on one side of a break is not damaged: a uniform rescaling of
every close in a window cancels out of the return. Only a window that CROSSES a break
carries a fabricated step, which is why :meth:`SpanAudit.corrupts` asks about crossing
rather than about membership of a stale stretch.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import pandas as pd

#: A candidate step in the level ratio, and the median shift confirming it, must both
#: exceed this. Sits in the measured gap: the null is 0.000000 at p99 on medians, and the
#: smallest artefact worth catching is a 5% stock dividend at 0.0476.
MEDIAN_SHIFT_TOLERANCE = 0.02

#: Sessions of level ratio needed on EACH side of a candidate step before persistence can
#: be judged. A step without them is reported unchecked, never clean.
CONFIRM_SESSIONS = 5

#: Fewer shared sessions than this and the span cannot answer at all.
MIN_COMPARABLE_SESSIONS = 20


@dataclass(frozen=True)
class SpanAudit:
    """What a second vendor could say about one ticker over one span of sessions.

    ``breaks`` holds each session whose step FROM THE PREVIOUS SHARED SESSION is an
    adjustment artefact; it is the first session on the new scale. ``unchecked`` holds
    sessions the reference could not adjudicate. ``answered`` is False when the reference
    said nothing usable about the span at all, and then the other two are empty — a
    caller reading only ``breaks`` would otherwise mistake silence for a clean bill.
    """

    answered: bool
    breaks: frozenset[dt.date]
    unchecked: frozenset[dt.date]

    def corrupts(self, sessions: Iterable[dt.date]) -> bool:
        """Does a window over ``sessions`` CROSS a break?

        The first session of the window is excluded: the window starts there, so the step
        into it is never inside the window's own return.
        """
        ordered = sorted(sessions)
        return any(s in self.breaks for s in ordered[1:])

    def has_unchecked(self, sessions: Iterable[dt.date]) -> bool:
        """Does a window over ``sessions`` touch anything the reference could not judge?"""
        if not self.answered:
            return True
        return any(s in self.unchecked for s in sessions)


#: The reference said nothing usable. Every window over it is unchecked, none is corrupt.
UNANSWERED = SpanAudit(answered=False, breaks=frozenset(), unchecked=frozenset())


def level_ratios(
    *, store_closes: Mapping[dt.date, float], reference: pd.Series | None
) -> dict[dt.date, float]:
    """``store_close / reference_close`` on every session BOTH sources price positively.

    Joining on the date, not on row order, is deliberate. The two vendors do not
    necessarily share a session calendar — a halt, a missing file or a vendor gap shifts
    one series against the other — and comparing adjacent ROWS would then measure a
    two-session move against a one-session move and call the difference an artefact.
    """
    if reference is None or len(reference) == 0:
        return {}
    try:
        index = pd.to_datetime(reference.index)
    except (TypeError, ValueError):
        return {}
    by_date: dict[dt.date, float] = {}
    for stamp, value in zip(index, reference.to_numpy(), strict=True):
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(price) and price > 0:
            by_date[stamp.date()] = price
    out: dict[dt.date, float] = {}
    for session, close in store_closes.items():
        ref = by_date.get(session)
        if ref is None:
            continue
        try:
            stored = float(close)
        except (TypeError, ValueError):
            continue
        if math.isfinite(stored) and stored > 0:
            out[session] = stored / ref
    return out


def audit_span(*, store_closes: Mapping[dt.date, float], reference: pd.Series | None) -> SpanAudit:
    """Find every adjustment break in one ticker's span, or report that nothing could be."""
    ratios = level_ratios(store_closes=store_closes, reference=reference)
    if len(ratios) < MIN_COMPARABLE_SESSIONS:
        return UNANSWERED
    sessions = sorted(ratios)
    series = [ratios[s] for s in sessions]
    breaks: set[dt.date] = set()
    unchecked = {s for s in store_closes if s not in ratios}
    for i in range(1, len(series)):
        if not _stepped(series[i - 1], series[i]):
            continue
        before, after = series[max(0, i - CONFIRM_SESSIONS) : i], series[i : i + CONFIRM_SESSIONS]
        if len(before) < CONFIRM_SESSIONS or len(after) < CONFIRM_SESSIONS:
            # Not enough level either side to tell a lasting shift from an excursion.
            unchecked.add(sessions[i])
            continue
        if _stepped(statistics.median(before), statistics.median(after)):
            breaks.add(sessions[i])
    return SpanAudit(answered=True, breaks=frozenset(breaks), unchecked=frozenset(unchecked))


def _stepped(before: float, after: float) -> bool:
    """Do two levels differ by more than the tolerance?

    Measured symmetrically on the log, because the quantity is a RATIO: a halving and a
    doubling are the same event seen from the two ends, and a plain relative difference
    would grade them differently.
    """
    if before <= 0 or after <= 0:
        return False
    return abs(math.log(after / before)) > math.log1p(MEDIAN_SHIFT_TOLERANCE)


__all__ = [
    "CONFIRM_SESSIONS",
    "MEDIAN_SHIFT_TOLERANCE",
    "MIN_COMPARABLE_SESSIONS",
    "UNANSWERED",
    "SpanAudit",
    "audit_span",
    "level_ratios",
]
