"""Thin exchange-calendar wrapper for the ``/v1/market/status`` endpoint.

This is a deliberately small subset of the pipeline's
``alphalens_pipeline.paper.calendar`` helpers — only the projections the SPA
banner needs: the day-level ``is_trading_day`` / ``is_half_day`` plus the
wall-clock ``is_session_open_at`` / ``next_session_open_utc`` /
``next_session_close_utc``. Both wrappers delegate to the same backing
library (``exchange_calendars``); the algorithms are direct reads off the
library's session metadata so divergence risk is bounded by the library
itself, not by hand-rolled logic on either side.

Why not import the pipeline module directly:

* The Django prod image installs only the ``alphalens-django`` workspace
  package (``uv sync ... --package alphalens-django`` in
  ``deploy/docker/django-prod/Dockerfile``). Pulling the full pipeline
  would drag in yfinance, alpaca-py, pandera, ivolatility, etc. — none of
  which the Django container needs.
* The Docker boundary means a top-level
  ``from alphalens_pipeline.paper.calendar import ...`` would crash at
  import time inside the runtime image.

Keeping the wrapper tiny (~50 LOC) and pinned to a shared library version
is the cheaper-to-maintain alternative.

Design memo: ``docs/research/paper_trading_non_trading_day_2026_05_29.md``
§5 (PR-C sequencing).
"""

from __future__ import annotations

import datetime as dt
import threading

import exchange_calendars as ec
import pandas as pd

DEFAULT_EXCHANGE = "XNYS"

# Per-exchange calendar cache. ``exchange_calendars.get_calendar`` caches
# internally too, but a local handle avoids the repeated MIC normalisation
# inside the library's getter on every API request.
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


def _session_ts(d: dt.date) -> pd.Timestamp:
    """Naive midnight Timestamp keyed for ``ExchangeCalendar.is_session``."""
    return pd.Timestamp(d)


def _utc_minute_ts(instant: dt.datetime) -> pd.Timestamp:
    """UTC, minute-floored Timestamp for minute-resolution session queries.

    A naive ``instant`` is assumed UTC. ``is_open_on_minute`` requires
    minute resolution; flooring sub-minute precision off ``now()`` keeps the
    query well-formed and the result stable across a poll window.
    """
    ts = pd.Timestamp(instant)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return ts.floor("min")


def is_trading_day(d: dt.date, exchange: str = DEFAULT_EXCHANGE) -> bool:
    """True when ``exchange`` holds a session (full or half-day) on ``d``."""
    return bool(_calendar(exchange).is_session(_session_ts(d)))


def is_half_day(d: dt.date, exchange: str = DEFAULT_EXCHANGE) -> bool:
    """True when ``d`` is a session on ``exchange`` with an early close.

    Detection idiom mirrors ``alphalens_pipeline.paper.calendar.is_half_day``:
    compare the actual session close against the venue's regular close.
    ``close_times`` is a list of ``(effective_date, time)`` tuples; the
    last entry with ``effective_date <= ts`` is the schedule in force on
    the query date. A future library release changing that shape would
    return False here (defensive isinstance below) rather than raise.
    """
    ts = _session_ts(d)
    cal = _calendar(exchange)
    if not cal.is_session(ts):
        return False
    actual_close = cal.session_close(ts)
    regular_close_time = None
    for effective_date, close_time in cal.close_times:
        if effective_date is None or pd.Timestamp(effective_date) <= ts:
            regular_close_time = close_time
    if not isinstance(regular_close_time, dt.time):
        return False
    actual_local = actual_close.tz_convert(cal.tz).time()
    return actual_local < regular_close_time


def next_session_open_utc(
    instant: dt.datetime,
    exchange: str = DEFAULT_EXCHANGE,
) -> dt.datetime:
    """UTC datetime of the next session open on ``exchange`` strictly after
    ``instant``.

    Wall-clock, minute-resolution — the mirror of ``next_session_close_utc``.
    Because it keys off the current instant (not a day anchor) it answers the
    "opens in HH:MM" countdown correctly in every phase:

    * pre-open on a trading day → *today's* open (the market opens later today,
      so the countdown must point at today, not the following session);
    * in-session or after-hours → the *next* session's open (today's open has
      already passed);
    * non-trading day → the next session's open.

    ``instant`` is normalised to UTC (naive datetimes assumed UTC). The SPA
    reads the result only while ``is_open_now`` is false.

    Superseded the day-anchored ``next_trading_open_utc(date)``, which always
    anchored at 23:59 UTC of the anchor day and so skipped a still-future
    same-day open — reporting tomorrow during the pre-open window. Keying off
    the live instant removes that class of error and drops the XNYS-specific
    23:59 UTC cutoff caveat (the library resolves the next open from the exact
    instant regardless of the venue's local tz).
    """
    nxt = _calendar(exchange).next_open(_utc_minute_ts(instant))
    return nxt.to_pydatetime().astimezone(dt.UTC)


def is_session_open_at(
    instant: dt.datetime,
    exchange: str = DEFAULT_EXCHANGE,
) -> bool:
    """True when ``exchange`` is in a regular trading session at ``instant``.

    Minute-resolution check via ``exchange_calendars.is_open_on_minute`` so it
    honours early closes (half-days) and any lunch breaks exactly as the
    library's own schedule does — unlike the day-level ``is_trading_day``,
    which can't tell a Monday pre-open from mid-session. ``instant`` is
    normalised to UTC (naive datetimes assumed UTC).
    """
    return bool(_calendar(exchange).is_open_on_minute(_utc_minute_ts(instant)))


def next_session_close_utc(
    instant: dt.datetime,
    exchange: str = DEFAULT_EXCHANGE,
) -> dt.datetime:
    """UTC datetime of the next session close on ``exchange`` strictly after
    ``instant``.

    When the venue is open at ``instant`` this is today's close (the early
    close on a half-day, not the regular one); when closed it is the next
    session's close. The SPA reads it only while ``is_open_now`` is true, to
    render a "closes in HH:MM" countdown.
    """
    nxt = _calendar(exchange).next_close(_utc_minute_ts(instant))
    return nxt.to_pydatetime().astimezone(dt.UTC)
