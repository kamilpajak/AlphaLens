"""Multi-exchange trading-day helpers for the paper-trade harness.

The pipeline-build timer runs daily at 06:30 UTC regardless of whether
any exchange is open; brief generation is intentionally calendar-day
based (news doesn't stop on weekends). What MUST not be calendar-day
based is the order-submission and TTL/time-stop side of the harness.

Two failure modes the helpers in this module exist to prevent:

* **Stale-ladder gap risk.** Submitting GTC limits over a weekend
  queues them at Friday-close-anchored prices into Monday's opening
  auction. A gap-down through E2 fills the entire ladder at prices the
  ladder's pull-back logic never intended to commit at. The submitter
  guards on ``is_trading_day(today)`` and defers to the next session.

* **TTL / time-stop drift on holidays.** The trade-setup memo defines
  the entry-fill window as N trading days; the legacy reconciler uses
  ``observed_at.date() - planned_at.date()`` which counts weekends and
  holidays. Switching the sweep to ``trading_days_elapsed`` (planned
  in PR-B) keeps the harness aligned with the memo.

## Multi-exchange design

All helpers accept an ``exchange`` parameter (ISO 10383 MIC) that
defaults to ``"XNYS"`` (NYSE) — the only venue the paper harness
currently routes to. Adding a Polish (XWAR), Tokyo (XTKS), Hong Kong
(XHKG), or Shanghai (XSHG) market in future is a per-call argument
change, not a refactor:

    is_trading_day(today, exchange="XWAR")
    next_trading_open(now, exchange="XTKS")

Plug-in points the caller will eventually need to think about beyond
this module:

* per-ticker exchange routing (a ``ticker → exchange`` map in
  ``planner.py``; today every position implicitly trades on XNYS),
* per-exchange broker clients (Alpaca is US-equities-only; XWAR
  routes through e.g. IBKR or a Polish brokerage),
* per-exchange currency / FX (XNYS in USD; XWAR in PLN — sizing math
  in ``planner.py`` would need an FX leg).

The calendar wrapper here is intentionally the smallest of those
three layers; tests pin XNYS as the default but exercise at least
one alternative venue (XWAR) so the parametric API stays honest.

The single backing dependency is ``exchange_calendars``. The
module-level ``_CALENDARS`` cache lazy-initialises each venue on
first use so import is cheap; subsequent calls reuse the loaded
calendar.

Design memo: ``docs/research/paper_trading_non_trading_day_2026_05_29.md``.
"""

from __future__ import annotations

import datetime as dt
import threading

import exchange_calendars as ec
import pandas as pd

DEFAULT_EXCHANGE = "XNYS"

DateLike = dt.date | dt.datetime | pd.Timestamp


# Per-exchange ``ExchangeCalendar`` cache. ``exchange_calendars.get_calendar``
# also caches internally, but binding handles here avoids the dict lookup +
# repeated MIC normalisation on every helper call (the reconciler hits these
# helpers inside a tight per-order loop). Guarded by ``_LOCK`` so a
# concurrent first-call from two threads doesn't trigger redundant calendar
# construction.
_CALENDARS: dict[str, ec.ExchangeCalendar] = {}
_LOCK = threading.Lock()


def _calendar(exchange: str = DEFAULT_EXCHANGE) -> ec.ExchangeCalendar:
    cal = _CALENDARS.get(exchange)
    if cal is None:
        with _LOCK:
            cal = _CALENDARS.get(exchange)
            if cal is None:
                cal = ec.get_calendar(exchange)
                _CALENDARS[exchange] = cal
    return cal


def _to_session_timestamp(d: DateLike) -> pd.Timestamp:
    """Coerce a date-like into a naive midnight Timestamp suitable for
    ``ExchangeCalendar.is_session`` lookups.

    ``exchange_calendars`` keys sessions on date-only naive Timestamps;
    passing a tz-aware or sub-day timestamp would raise ``ValueError``.
    """
    if isinstance(d, dt.datetime):
        # Strip time + tz; the session lookup is date-granular.
        return pd.Timestamp(d.date())
    if isinstance(d, pd.Timestamp):
        return pd.Timestamp(d.date())
    return pd.Timestamp(d)


def is_trading_day(d: DateLike, exchange: str = DEFAULT_EXCHANGE) -> bool:
    """True when ``exchange`` holds a session (full or half-day) on ``d``.

    Half-days count as trading days — the submitter and reconciler
    must still run; only the close time differs. Callers needing to
    distinguish half-days (e.g. for cron scheduling) should compose
    with ``is_half_day``.
    """
    return bool(_calendar(exchange).is_session(_to_session_timestamp(d)))


def is_half_day(d: DateLike, exchange: str = DEFAULT_EXCHANGE) -> bool:
    """True when ``d`` is a session on ``exchange`` with an early close
    (below the venue's nominal regular close).

    For XNYS the early-close convention is 13:00 ET vs the usual 16:00.
    Occurrences include the Friday after Thanksgiving, the trading day
    before Independence Day on weekday calendars, and Christmas Eve.
    The full list is governed by NYSE rule and maintained inside
    ``exchange_calendars`` — checking close-hour against the venue's
    regular close is the canonical detection idiom rather than
    hard-coding holiday tables.

    For non-XNYS venues this returns True whenever the actual session
    close is earlier than that venue's regular close. The reference
    "regular" close is read off the calendar's own metadata so the
    idiom transfers without per-venue tuning.
    """
    ts = _to_session_timestamp(d)
    cal = _calendar(exchange)
    if not cal.is_session(ts):
        return False
    actual_close = cal.session_close(ts)
    # ``ExchangeCalendar.close_times`` is a list[(effective_date, time)]
    # tuples giving the venue's regular close. Walking it picks up
    # permanent close-time changes (e.g. NYSE's historical 16:00 vs
    # earlier-era 15:30) — the LAST entry with ``effective_date <= ts``
    # is the schedule in force on the query date.
    regular_close_time = None
    for effective_date, close_time in cal.close_times:
        if effective_date is None or pd.Timestamp(effective_date) <= ts:
            regular_close_time = close_time
    # Defensive isinstance: a future ``exchange_calendars`` release that
    # changes the ``close_times`` shape would otherwise silently return
    # False for every half-day instead of raising. Half-day detection
    # is opt-in (callers must explicitly invoke ``is_half_day``), so a
    # silent False is OK at runtime but unwelcome under test.
    if not isinstance(regular_close_time, dt.time):
        return False
    # Both ``actual_close`` and ``regular_close_time`` reference the same
    # venue-local tz; compare the time-of-day portion.
    actual_local = actual_close.tz_convert(cal.tz).time()
    return actual_local < regular_close_time


def next_trading_open(
    after: DateLike,
    exchange: str = DEFAULT_EXCHANGE,
) -> dt.datetime:
    """The UTC datetime of the next session open on ``exchange`` strictly
    after ``after``.

    "Strictly after" means: if ``after`` falls during a live session
    (e.g. 14:00 ET on a Wednesday for XNYS), the result is the
    FOLLOWING session's open, not the current session's open. Callers
    wanting "is the market open right now?" should check
    ``is_trading_day`` on today plus a clock comparison — this helper
    exists for the "submission was deferred; when will it next be
    attempted?" path.

    Naive ``after`` is treated as UTC.
    """
    if isinstance(after, dt.datetime):
        if after.tzinfo is None:
            after_ts = pd.Timestamp(after, tz="UTC")
        else:
            after_ts = pd.Timestamp(after).tz_convert("UTC")
    elif isinstance(after, pd.Timestamp):
        after_ts = after.tz_convert("UTC") if after.tzinfo else after.tz_localize("UTC")
    else:
        # Plain date — anchor at 23:59 UTC so the lookup skips today's
        # session even if today is itself a trading day.
        after_ts = pd.Timestamp(after, tz="UTC") + pd.Timedelta(hours=23, minutes=59)

    nxt = _calendar(exchange).next_open(after_ts)
    return nxt.to_pydatetime().astimezone(dt.UTC)


def previous_trading_day(
    d: DateLike,
    exchange: str = DEFAULT_EXCHANGE,
) -> dt.date:
    """The session date on ``exchange`` strictly before ``d``.

    "Strictly before" — even if ``d`` itself is a session, the prior
    session is returned. Callers usually want this when answering
    "what was the last close anchor?".
    """
    ts = _to_session_timestamp(d)
    cal = _calendar(exchange)
    if cal.is_session(ts):
        prev = cal.previous_session(ts)
    else:
        prev = cal.date_to_session(ts, direction="previous")
    return prev.date()


def trading_days_elapsed(
    start: DateLike,
    end: DateLike,
    exchange: str = DEFAULT_EXCHANGE,
) -> int:
    """Number of ``exchange`` sessions elapsed between ``start`` and ``end``.

    Semantics — sessions falling in the half-open interval ``(start, end]``:

        * ``start == end`` → 0
        * Fri close → Mon close → 1 (the Monday session counts)
        * Fri close (week 1) → Fri close (week 2, clean week) → 5
        * Same span containing a Monday holiday → 4

    This matches the way an operator reasons about "the brief is N days
    old" — at end-of-day Monday after a Friday plan, one trading day
    has elapsed, regardless of how many weekend days passed in between.
    The legacy reconciler used ``(observed_at.date() - planned_at.date()).days``
    which double-counts weekends; swapping it for this helper restores
    alignment with the trade-setup memo's "N trading days" intent.

    Returns 0 (not a negative value or exception) when ``end < start``
    so a clock-skew or caller bug cannot accidentally fire a TTL sweep
    by reporting a fake "TTL exceeded by 9999 trading days". Silent
    clamping was chosen over ``ValueError`` because a swept-but-shouldn't
    cancellation is materially worse than a logged-and-silenced no-op.
    """
    s = _to_session_timestamp(start)
    e = _to_session_timestamp(end)
    if e <= s:
        return 0
    cal = _calendar(exchange)
    # ``sessions_in_range`` is end-inclusive when the endpoint is itself a
    # session. We want sessions strictly after ``start`` up through and
    # including ``end`` — drop ``start`` if it's a session, keep ``end``.
    sessions = cal.sessions_in_range(s, e)
    count = len(sessions)
    if cal.is_session(s):
        count -= 1
    return max(0, count)
