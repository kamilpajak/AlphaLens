"""T0 timing rules — translate raw filing timestamp to market-reaction day.

Pre-reg ``event_drift_v3_pead_quality_clean`` locks the after-hours rule:

  - hour < 16:00 ET (pre-market or regular session): the market sees the
    filing during today's session and reacts same day
  - hour >= 16:00 ET (after-hours): market reacts on the next trading day
  - hour is None (unknown — current state with companyfacts-only ingestion):
    conservative default = treat as after-hours, advance one trading day

The drift window [d+2, d+60] is then anchored to the market-reaction day,
not the filing day. This eliminates the intraday lookahead that zen flagged
in the v1 review (an after-close filing cannot be traded same day).
"""

from __future__ import annotations

from datetime import date
from typing import Protocol


class TradingCalendar(Protocol):
    """Minimal trading-calendar API consumed by this module."""

    def snap_to_trading_day(self, d: date) -> date: ...
    def next_trading_day(self, d: date) -> date: ...
    def add_trading_days(self, d: date, n: int) -> date: ...


_AFTER_HOURS_BOUNDARY_ET = 16  # NYSE close


def market_announcement_day(
    filed_date: date,
    accepted_hour_et: int | None,
    *,
    calendar: TradingCalendar,
) -> date:
    """Map a filing (date, hour) tuple to the trading day the market first reacts.

    - ``accepted_hour_et < 16``: market sees filing today (or, if filed
      on a non-trading day, on the next trading day)
    - ``accepted_hour_et >= 16`` or ``None``: filing is after-hours or
      timing unknown -> next trading day
    """
    snapped = calendar.snap_to_trading_day(filed_date)
    if accepted_hour_et is not None and accepted_hour_et < _AFTER_HOURS_BOUNDARY_ET:
        return snapped
    return calendar.next_trading_day(snapped)


def drift_entry_day(
    filed_date: date,
    accepted_hour_et: int | None,
    *,
    calendar: TradingCalendar,
    skip_days: int = 2,
) -> date:
    """First trading day to hold the position (Engelberg's d+2 from market day)."""
    market_day = market_announcement_day(filed_date, accepted_hour_et, calendar=calendar)
    return calendar.add_trading_days(market_day, skip_days)


def drift_exit_day(
    filed_date: date,
    accepted_hour_et: int | None,
    *,
    calendar: TradingCalendar,
    exit_days: int = 60,
) -> date:
    """Last trading day to hold the position (Engelberg's d+60 from market day)."""
    market_day = market_announcement_day(filed_date, accepted_hour_et, calendar=calendar)
    return calendar.add_trading_days(market_day, exit_days)
