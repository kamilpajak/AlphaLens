"""Parity guard: the two MIC-keyed calendar wrappers must agree.

There are two independent thin wrappers over ``exchange_calendars`` for the SAME
exchange semantics, on opposite sides of the workspace split:

* ``alphalens_pipeline.paper.calendar`` — drives the LIVE broker-free feedback
  replay (``population_ladder_monitor`` / ``benchmark_excess``): session windows,
  trading-day membership.
* ``market.calendar`` (this Django app) — drives ``/v1/market/status`` (the SPA's
  "market open? next open in HH:MM" banner).

They are maintained separately (the Django app cannot import the pipeline package
under the slim production image). Their per-side unit tests pin each wrapper in
isolation, so nothing catches them DRIFTING apart — e.g. one gains a half-day or
holiday rule the other lacks. That drift is a money-entrusted hazard: the feedback
replay would treat a session differently from what the dashboard tells the user.

This test closes that seam: over a full year of XNYS (covering every holiday,
half-day, weekend and both DST transitions) the two wrappers must return identical
``is_trading_day`` / ``is_half_day``, and identical next-open instants. A rule that
lands in one wrapper but not the other turns this red.

The test lives in the Django suite because its CI job runs the full uv workspace
(``uv sync --group dev``) so it can import BOTH wrappers; it needs no Django ORM/DB
(both APIs are pure functions over ``exchange_calendars``).
"""

from __future__ import annotations

import datetime as dt

from alphalens_pipeline.paper import calendar as paper_cal
from market import calendar as market_cal

# A full calendar year exercises every XNYS holiday (New Year, MLK, Presidents,
# Good Friday, Memorial, Juneteenth, Independence Day, Labor, Thanksgiving,
# Christmas), both half-days (day after Thanksgiving, Christmas Eve), every
# weekend, and both DST transitions (spring-forward Mar, fall-back Nov).
_YEAR_START = dt.date(2025, 1, 1)
_DAYS_IN_2025 = 365


def _all_days_2025() -> list[dt.date]:
    return [_YEAR_START + dt.timedelta(days=i) for i in range(_DAYS_IN_2025)]


def test_is_trading_day_parity_full_year_xnys():
    """Every day in 2025: both wrappers agree on XNYS trading-day membership."""
    mismatches = [
        (d, paper_cal.is_trading_day(d), market_cal.is_trading_day(d))
        for d in _all_days_2025()
        if paper_cal.is_trading_day(d) != market_cal.is_trading_day(d)
    ]
    assert not mismatches, f"is_trading_day drift between the two calendar wrappers: {mismatches}"


def test_is_half_day_parity_full_year_xnys():
    """Every day in 2025: both wrappers agree on XNYS half-day (early close)."""
    mismatches = [
        (d, paper_cal.is_half_day(d), market_cal.is_half_day(d))
        for d in _all_days_2025()
        if paper_cal.is_half_day(d) != market_cal.is_half_day(d)
    ]
    assert not mismatches, f"is_half_day drift between the two calendar wrappers: {mismatches}"


def test_parity_test_is_not_vacuous():
    """Guard the guard: 2025 must actually contain trading days, non-trading days,
    AND at least one half-day — otherwise the parity assertions could pass by
    comparing two always-identical trivial answers."""
    days = _all_days_2025()
    trading = [d for d in days if paper_cal.is_trading_day(d)]
    non_trading = [d for d in days if not paper_cal.is_trading_day(d)]
    half_days = [d for d in days if paper_cal.is_half_day(d)]
    assert 230 <= len(trading) <= 260, f"unexpected XNYS trading-day count: {len(trading)}"
    assert len(non_trading) > 100, "expected weekends + holidays as non-trading days"
    assert len(half_days) >= 2, f"expected >=2 XNYS half-days in 2025, got {len(half_days)}"


def test_next_open_parity_across_holiday_halfday_and_dst():
    """The next-session-open instant agrees across both wrappers.

    Both wrappers expose an instant-based helper — the pipeline's
    ``next_trading_open(instant)`` and the Django ``next_session_open_utc(instant)``
    — each returning "the next session open strictly after this instant".
    Feeding the same UTC instants must yield identical results. The instants
    exercise the branches that could drift:

    * a pre-open instant on a trading day (must resolve to *today's* open — the
      case the old day-anchored helper got wrong);
    * an after-hours instant the evening before Thanksgiving (skips the holiday,
      reopens on the half-day);
    * a Christmas-Eve after-hours instant (reopens after Christmas);
    * a spring-DST-eve after-hours instant (crosses the clock change).
    """
    instants = [
        # Fri 2025-03-07 12:00 UTC == 07:00 EST — pre-open, must resolve to
        # TODAY's 14:30 UTC open, not the following session. Catches the bug.
        dt.datetime(2025, 3, 7, 12, 0, tzinfo=dt.UTC),
        # Wed 2025-11-26 22:00 UTC — after close, eve of Thanksgiving; reopen
        # Fri 11-28 (half-day).
        dt.datetime(2025, 11, 26, 22, 0, tzinfo=dt.UTC),
        # Christmas Eve 2025-12-24 22:00 UTC — after its early close; reopen
        # Fri 12-26.
        dt.datetime(2025, 12, 24, 22, 0, tzinfo=dt.UTC),
        # Fri 2025-03-07 22:00 UTC — after close, spring-forward weekend ahead;
        # reopen Mon 2025-03-10.
        dt.datetime(2025, 3, 7, 22, 0, tzinfo=dt.UTC),
        # Exact open minute (Fri 2025-03-07 14:30 UTC == 09:30 EST). ``next_open``
        # is strictly-after, so both wrappers must skip today and give Monday —
        # a boundary where an off-by-one-minute rule in one side would surface.
        dt.datetime(2025, 3, 7, 14, 30, tzinfo=dt.UTC),
    ]
    for instant in instants:
        paper_open = paper_cal.next_trading_open(instant)
        market_open = market_cal.next_session_open_utc(instant)
        assert paper_open == market_open, (
            f"next-open drift for instant {instant}: pipeline={paper_open} django={market_open}"
        )
