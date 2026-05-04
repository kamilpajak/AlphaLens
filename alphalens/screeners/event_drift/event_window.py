"""Event windows: rolling [d+2, d+60] holding ranges with the single-active-
window invariant.

Each ``EventWindow`` carries the per-cohort metadata (SUE, accruals ratio)
needed downstream by ``score_pead_quality``. Build the windows from a list
of ``EarningsAnnouncement`` objects, apply the invariant to drop nested
overlaps, then query ``windows_active_on(asof)`` for the daily portfolio.

The single-active-window invariant means: a new announcement that arrives
while an earlier window is still active is DROPPED. Pre-reg makes this
explicit because Engelberg-style PEAD anchors to one surprise per cohort
period; allowing a fresh announcement to extend or restart the window
would amplify both signal and noise asymmetrically.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date

from alphalens.screeners.event_drift.announcement_dates import EarningsAnnouncement
from alphalens.screeners.event_drift.t0_timing import (
    TradingCalendar,
    drift_entry_day,
    drift_exit_day,
    market_announcement_day,
)


@dataclass(frozen=True)
class EventWindow:
    ticker: str
    market_day: date
    entry_day: date
    exit_day: date
    sue: float
    accruals_ratio: float


def build_event_windows(
    announcements: Iterable[EarningsAnnouncement],
    *,
    sue_lookup: Callable[[str, date], float | None],
    accruals_lookup: Callable[[str, date], float | None],
    calendar: TradingCalendar,
    skip_days: int = 2,
    exit_days: int = 60,
) -> list[EventWindow]:
    """Materialise event windows from announcements.

    ``sue_lookup(ticker, period_end)`` and ``accruals_lookup(ticker, period_end)``
    return ``None`` when the value cannot be computed; such announcements
    are silently dropped (no window emitted).

    Returned list is sorted by (ticker, market_day).
    """
    windows: list[EventWindow] = []
    for ann in announcements:
        sue = sue_lookup(ann.ticker, ann.period_end)
        if sue is None:
            continue
        accruals = accruals_lookup(ann.ticker, ann.period_end)
        if accruals is None:
            continue
        market_day = market_announcement_day(
            ann.filed_date, ann.accepted_hour_et, calendar=calendar
        )
        entry = drift_entry_day(
            ann.filed_date, ann.accepted_hour_et, calendar=calendar, skip_days=skip_days
        )
        exit_d = drift_exit_day(
            ann.filed_date, ann.accepted_hour_et, calendar=calendar, exit_days=exit_days
        )
        windows.append(
            EventWindow(
                ticker=ann.ticker,
                market_day=market_day,
                entry_day=entry,
                exit_day=exit_d,
                sue=float(sue),
                accruals_ratio=float(accruals),
            )
        )
    windows.sort(key=lambda w: (w.ticker, w.market_day))
    return windows


def apply_single_active_window(windows: Sequence[EventWindow]) -> list[EventWindow]:
    """Drop windows whose ``market_day`` falls inside an earlier active window
    for the same ticker.

    Per pre-reg: the FIRST window of a chronological sequence wins; later
    overlapping announcements are silently dropped. After the first window
    naturally expires (asof > exit_day), the next announcement opens a
    fresh window.
    """
    by_ticker: dict[str, list[EventWindow]] = {}
    for w in windows:
        by_ticker.setdefault(w.ticker, []).append(w)

    kept: list[EventWindow] = []
    for ticker, sorted_windows in by_ticker.items():
        sorted_windows.sort(key=lambda w: w.market_day)
        last_exit: date | None = None
        for w in sorted_windows:
            if last_exit is not None and w.market_day <= last_exit:
                # Active window still in force; drop this one.
                continue
            kept.append(w)
            last_exit = w.exit_day
    kept.sort(key=lambda w: (w.ticker, w.market_day))
    return kept


def windows_active_on(windows: Sequence[EventWindow], asof: date) -> list[EventWindow]:
    """Subset of windows where ``entry_day <= asof <= exit_day``."""
    return [w for w in windows if w.entry_day <= asof <= w.exit_day]
