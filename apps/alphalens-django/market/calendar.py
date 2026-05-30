"""Thin exchange-calendar wrapper for the ``/v1/market/status`` endpoint.

This is a deliberately small subset of the pipeline's
``alphalens_pipeline.paper.calendar`` helpers — only the three projections
the SPA banner needs (``is_trading_day``, ``is_half_day``,
``next_trading_open_utc``). Both wrappers delegate to the same backing
library (``exchange_calendars``); the algorithms are direct reads off the
library's session metadata so divergence risk is bounded by the library
itself, not by hand-rolled logic on either side.

Why not import the pipeline module directly:

* The Django prod image installs only the ``alphalens-django`` workspace
  package (``uv sync ... --package alphalens-django`` in
  ``deploy/docker/django-prod/Dockerfile``). Pulling the full pipeline
  would drag in google-genai, langchain-google-genai, yfinance, alpaca-py,
  pandera, ivolatility, etc. — none of which the Django container needs.
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


def next_trading_open_utc(
    anchor: dt.date,
    exchange: str = DEFAULT_EXCHANGE,
) -> dt.datetime:
    """UTC datetime of the next session open on ``exchange`` strictly
    after the end of the day named by ``anchor``.

    "Strictly after the end of the day" — even if ``anchor`` is itself a
    trading day, the result is the NEXT session, not the current one.
    The SPA uses this to render a "next open in HH:MM" countdown that
    points to tomorrow's open during after-hours, not back to today's
    long-past open.

    Implementation note: ``next_open`` operates on a UTC timestamp; we
    anchor at 23:59 UTC of the requested date so a same-day session is
    skipped consistently regardless of how the venue's local tz lines up
    with UTC.
    """
    after_ts = pd.Timestamp(anchor, tz="UTC") + pd.Timedelta(hours=23, minutes=59)
    nxt = _calendar(exchange).next_open(after_ts)
    return nxt.to_pydatetime().astimezone(dt.UTC)
