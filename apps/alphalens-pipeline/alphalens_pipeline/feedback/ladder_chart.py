"""Ladder-chart payload projection for the ``/edge`` candlestick view (PR-1).

Why this lives in the pipeline (NOT in the Django ingest)
---------------------------------------------------------
The ``/edge`` dashboard wants to *draw* the broker-free ladder replay on a
candlestick chart: daily OHLC candles, the entry / take-profit / stop levels as
price lines, and the modeled fill / exit markers (memo §7). Drawing that needs
the cached Polygon minute bars + the exchange calendar (to fold minute bars into
RTH daily sessions) + the pure ladder replay — all of which live in
``alphalens_pipeline``.

The slim Django production image deliberately does NOT install
``alphalens_pipeline`` (the prod incident 2026-06-01: a top-level pipeline import
broke the image build). So — exactly like
:mod:`alphalens_pipeline.feedback.benchmark_excess` — the heavy compute is a
PRE-COMPUTED PROJECTION: this module builds the whole chart payload and persists
it as one ``chart_payload_json`` string column on the population-ladder parquet.
Django ONLY READS the column (and serves it), never recomputes, never imports the
pipeline.

Scope (PR-1): DAILY bars only. The daily candles are derived FROM the cached
minute bars (so a daily high/low is the union of its minute highs/lows — memo
§4.5/§6), but only the daily aggregation is persisted (a 42-session intraday
payload would be ~16k rows/record — Postgres/parquet bloat). Intraday lazy-fetch
is a later PR.

The marker-timestamp gotcha (memo §6, §9 appendix): Lightweight Charts requires a
marker's ``time`` to match an EXISTING bar — a timestamp in a non-trading gap
silently fails to render. So :func:`_markers_from_sequence` maps each crossing's
``bar_ts_ms`` to its DAILY session date string and DROPS any crossing whose
mapped date is not among the emitted daily bars.

Pure core (``build_chart_payload`` + the daily aggregation + the marker mapping)
imports nothing from the store / Polygon / Django; the impure
:func:`enrich_store_with_chart_payloads` mirrors ``benchmark_excess`` (never-raises
per row, idempotent over every store parquet, atomic write, injectable
``bar_fetch``).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from alphalens_pipeline.feedback.ladder_replay import (
    TIE_BREAK_SL_FIRST,
    LadderOutcome,
    parse_ladder,
    replay_ladder,
)
from alphalens_pipeline.paper.calendar import (
    DEFAULT_EXCHANGE,
    advance_trading_sessions,
    previous_trading_day,
    session_on_or_after,
    session_open_utc,
    trading_days_elapsed,
)

logger = logging.getLogger(__name__)

# The one column this module writes. Listed once so the ingest side references the
# same name.
CHART_PAYLOAD_COLUMN = "chart_payload_json"

# Marker kinds emitted in the payload (the chart's own vocabulary). These mirror
# the replay ``LevelCrossing.kind`` values; TIME_STOP is emitted as a MARKER only,
# never as a price line (a time-stop is an exit event, not a resting level).
_MARKER_ENTRY = "ENTRY"
_MARKER_TP = "TP"
_MARKER_SL = "SL"
_MARKER_TIME_STOP = "TIME_STOP"

# A (ticker, arrival_session) -> list of cached OHLC minute bars. The production
# default reads the per-(ticker, arrival) bar cache the population monitor writes;
# test stubs inject synthetic bars.
ChartBarFetch = Callable[[str, dt.date], Sequence[Mapping[str, Any]]]

# A (ticker, start, end) -> list of Polygon DAILY aggregate bars (same OHLCV dict
# shape as the minute bars). Used ONLY for the CONTEXT WINDOW around the trade
# (lead-in before arrival, trailing after the horizon) — the in-trade sessions
# keep the minute-fold path. Mirrors ``benchmark_excess.BarFetch``.
DailyBarFetch = Callable[[str, dt.datetime, dt.datetime], Sequence[Mapping[str, Any]]]

# Context-window geometry (design review). The lead-in (sessions BEFORE arrival)
# is ``min(LEAD_IN_CAP, max(LEAD_IN_FLOOR, 2 x hold_sessions))`` so a young
# position is not "a handful of lonely candles" yet a long hold does not drag in
# an unbounded history; the trailing window (sessions AFTER the exit / horizon) is
# a fixed ``TRAILING_SESSIONS`` for a closed trade and "whatever exists" for an
# open one (no synthetic future bars).
LEAD_IN_FLOOR = 20
LEAD_IN_CAP = 90
TRAILING_SESSIONS = 15


def _empty_price_lines() -> dict[str, Any]:
    return {"entry": None, "tp": [], "stop": None}


def _no_structure_payload() -> dict[str, Any]:
    return {
        "status": "NO_STRUCTURE",
        "bars": [],
        "price_lines": _empty_price_lines(),
        "markers": [],
        "ambiguous_bars": 0,
        "intrabar_rule": TIE_BREAK_SL_FIRST,
        "rth_only": True,
    }


def _no_data_payload() -> dict[str, Any]:
    return {
        "status": "NO_DATA",
        "bars": [],
        "price_lines": _empty_price_lines(),
        "markers": [],
        "ambiguous_bars": 0,
        "intrabar_rule": TIE_BREAK_SL_FIRST,
        "rth_only": True,
    }


def _session_date_for_ts(
    ts_ms: int, session_windows: Sequence[tuple[dt.date, int, int]]
) -> dt.date | None:
    """The session date whose RTH window contains ``ts_ms`` (or ``None``).

    ``session_windows`` is the pre-built ``(session, open_ms, close_ms)`` list for
    the emitted daily bars; matching against it (rather than truncating the UTC
    timestamp to a date) is what guarantees a marker maps to an EMITTED daily bar
    and never to a non-trading gap.
    """
    for session, open_ms, close_ms in session_windows:
        if open_ms <= ts_ms <= close_ms:
            return session
    return None


def _rth_session_windows(
    arrival_session: dt.date, horizon_session: dt.date, exchange: str
) -> list[tuple[dt.date, int, int]]:
    """``(session, open_ms, close_ms)`` per trading session in the window.

    Walks the exchange calendar so weekends / holidays inside the span contribute
    no window (and thus any minute bar landing on them is dropped from the daily
    aggregation). Close is the RTH close (honours half-days). ``close_ms`` is
    inclusive.
    """
    if horizon_session < arrival_session:
        return []
    n_sessions = trading_days_elapsed(arrival_session, horizon_session, exchange)
    windows: list[tuple[dt.date, int, int]] = []
    for i in range(n_sessions + 1):
        session = advance_trading_sessions(arrival_session, i, exchange)
        if session > horizon_session:
            break
        open_ms, close_ms = _rth_window_ms(session, exchange)
        windows.append((session, open_ms, close_ms))
    return windows


def _rth_window_ms(session: dt.date, exchange: str) -> tuple[int, int]:
    """``(open_ms, close_ms)`` epoch-ms RTH bounds for ``session`` (close inclusive).

    Half-days resolve to their early close read off the calendar; a degenerate /
    mis-reported close falls back to the full 390-minute session so a session is
    never silently dropped.
    """
    from alphalens_pipeline.paper.calendar import _calendar, _to_session_timestamp, is_half_day

    open_utc = session_open_utc(session, exchange)
    open_ms = int(open_utc.timestamp() * 1000)
    full_span_min = 390
    if not is_half_day(session, exchange):
        span_min = full_span_min
    else:
        ts = _to_session_timestamp(session)
        close_utc = _calendar(exchange).session_close(ts).to_pydatetime().astimezone(dt.UTC)
        span = int((close_utc - open_utc).total_seconds() // 60)
        span_min = span if span > 0 else full_span_min
    return open_ms, open_ms + span_min * 60_000


def _daily_bars_from_minute(
    bars: Sequence[Mapping[str, Any]],
    session_windows: Sequence[tuple[dt.date, int, int]],
) -> list[dict[str, Any]]:
    """Fold minute bars into one daily OHLC candle per RTH session.

    ``open`` = first (earliest ``t``) minute open, ``close`` = last minute close,
    ``high`` = max minute high, ``low`` = min minute low, ``volume`` = sum. A bar
    outside every session window (pre/post-market, weekend, holiday) is dropped so
    the daily [low, high] is exactly the RTH minute union (memo §4.5). Sessions
    with no minute bar emit no candle (a gap is honest, not a phantom flat bar).
    """
    # Group minute bars by their session date (ordered by ts for open/close).
    by_session: dict[dt.date, list[Mapping[str, Any]]] = {}
    for bar in sorted(bars, key=lambda b: int(b["t"])):
        session = _session_date_for_ts(int(bar["t"]), session_windows)
        if session is None:
            continue
        by_session.setdefault(session, []).append(bar)

    daily: list[dict[str, Any]] = []
    for session, _open_ms, _close_ms in session_windows:
        minute_bars = by_session.get(session)
        if not minute_bars:
            continue
        # Drop any minute bar with a missing or non-finite OHLC value: a single
        # NaN/Inf tick would otherwise poison the daily open/high/low/close and
        # the downstream JSON FloatField serialisation (a NaN is not valid JSON).
        # A session left with no finite bar emits no candle — the same "a gap is
        # honest, not a phantom flat bar" rule as a session with no bars at all.
        finite_bars = [b for b in minute_bars if _has_finite_ohlc(b)]
        if not finite_bars:
            continue
        daily.append(
            {
                "time": session.isoformat(),
                "open": float(finite_bars[0]["o"]),
                "high": max(float(b["h"]) for b in finite_bars),
                "low": min(float(b["l"]) for b in finite_bars),
                "close": float(finite_bars[-1]["c"]),
                "volume": sum(_finite_or_zero(b.get("v")) for b in finite_bars),
            }
        )
    return daily


def _has_finite_ohlc(bar: Mapping[str, Any]) -> bool:
    """True when the bar's open/high/low/close are all present and finite.

    A missing key (``KeyError``) or a non-numeric / NaN / Inf value makes the bar
    unusable for a daily candle, so it is dropped rather than crashing the fold.
    """
    try:
        return all(math.isfinite(float(bar[k])) for k in ("o", "h", "l", "c"))
    except (KeyError, TypeError, ValueError):
        return False


def _finite_or_zero(value: Any) -> float:
    """Coerce a volume value to a finite float; NaN / Inf / missing become 0.0."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) else 0.0


def _marker_kind_and_label(level_id: str, kind: str) -> tuple[str, str]:
    """Map a replay crossing ``(level_id, kind)`` to the chart marker kind + label.

    Labels are the compact level ids the UI draws (``E1``, ``TP1``, ``SL``); the
    time-stop carries the ``TIME_STOP`` label so the tooltip reads honestly.
    """
    if kind == "ENTRY":
        return _MARKER_ENTRY, level_id
    if kind == "TP":
        return _MARKER_TP, level_id
    if kind == "SL":
        return _MARKER_SL, level_id
    return _MARKER_TIME_STOP, _MARKER_TIME_STOP


def _markers_from_sequence(
    outcome: LadderOutcome, session_windows: Sequence[tuple[dt.date, int, int]]
) -> list[dict[str, Any]]:
    """Map each replay crossing to a chart marker on its DAILY session bar.

    Each ``LevelCrossing.bar_ts_ms`` is mapped to the session date whose RTH window
    contains it. A crossing whose timestamp falls outside every emitted daily
    session (a non-trading gap) is DROPPED — emitting it with a dangling ``time``
    would silently fail to render in Lightweight Charts (memo §6). ``ambiguous`` is
    carried straight from ``same_bar_ambiguous`` (the SL-first intrabar flag).
    """
    markers: list[dict[str, Any]] = []
    for crossing in outcome.sequence:
        session = _session_date_for_ts(crossing.bar_ts_ms, session_windows)
        if session is None:
            continue  # dangling time -> would not render; drop honestly
        marker_kind, label = _marker_kind_and_label(crossing.level_id, crossing.kind)
        markers.append(
            {
                "time": session.isoformat(),
                "kind": marker_kind,
                "level_id": crossing.level_id,
                "price": crossing.price,
                "label": label,
                "ambiguous": bool(crossing.same_bar_ambiguous),
            }
        )
    return markers


def _price_lines(setup: Mapping[str, Any] | None) -> dict[str, Any]:
    """Entry / TP / stop levels for the chart price lines.

    ``entry`` is the FIRST (E1) entry tier limit (the blended-entry / per-tier
    geometry is a richer view deferred to PR-2); ``tp`` is the ordered list of TP
    targets; ``stop`` is the disaster stop. TIME_STOP is intentionally NOT a price
    line — it is an exit event, drawn only as a marker.
    """
    parsed = parse_ladder(setup)
    entry = parsed.entries[0].price if parsed.entries else None
    tp = [lvl.price for lvl in parsed.tps]
    return {"entry": entry, "tp": tp, "stop": parsed.disaster_stop}


def _lead_in_sessions(hold_sessions: int) -> int:
    """Number of pre-arrival context sessions: ``min(CAP, max(FLOOR, 2 x hold))``."""
    return min(LEAD_IN_CAP, max(LEAD_IN_FLOOR, 2 * max(0, hold_sessions)))


def _retreat_sessions(session: dt.date, n: int, exchange: str) -> dt.date:
    """The session ``n`` sessions strictly before ``session`` (walks the calendar).

    There is no public "advance backward by N" helper, so this steps back one
    session at a time via :func:`previous_trading_day` (each call returns the
    session strictly before its argument). ``n == 0`` returns ``session``.
    """
    cursor = session
    for _ in range(max(0, n)):
        cursor = previous_trading_day(cursor, exchange)
    return cursor


def _daily_bars_from_context(
    raw_bars: Sequence[Mapping[str, Any]],
    *,
    keep_sessions: set[dt.date],
    exchange: str,
) -> list[dict[str, Any]]:
    """Fold fetched Polygon DAILY aggregates into one candle per kept session.

    Each daily aggregate's ``t`` (epoch-ms, session start) is mapped to the
    session on-or-after its UTC date; a bar whose session is not in
    ``keep_sessions`` (outside the lead-in / trailing span) or whose OHLC is
    non-finite is dropped. At most one candle per session (the last one wins on
    the rare duplicate). These bars carry NO markers — pure visual structure.
    """
    by_session: dict[dt.date, dict[str, Any]] = {}
    for bar in sorted(raw_bars, key=lambda b: int(b["t"])):
        if not _has_finite_ohlc(bar):
            continue
        bar_date = dt.datetime.fromtimestamp(int(bar["t"]) / 1000, dt.UTC).date()
        session = session_on_or_after(bar_date, exchange)
        if session not in keep_sessions:
            continue
        by_session[session] = {
            "time": session.isoformat(),
            "open": float(bar["o"]),
            "high": float(bar["h"]),
            "low": float(bar["l"]),
            "close": float(bar["c"]),
            "volume": _finite_or_zero(bar.get("v")),
        }
    return [by_session[s] for s in sorted(by_session)]


def _context_bars(
    ticker: str,
    daily_bar_fetch: DailyBarFetch,
    *,
    arrival_session: dt.date,
    horizon_session: dt.date,
    hold_sessions: int,
    exchange: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(lead_in_bars, trailing_bars)`` daily context candles around the trade.

    Session counts → a single generous CALENDAR date range for one Polygon daily
    fetch (sessions ≠ calendar days, so the range is widened: lead-in walks the
    calendar back ``lead_in`` sessions, trailing walks forward ``TRAILING_SESSIONS``
    sessions, then the fetched bars are filtered back to exactly the kept sessions
    — Polygon daily aggregates are already session-only, no weekend/holiday
    phantoms). A fetch that raises or returns empty degrades to no context
    (``([], [])``) so the caller falls back to the in-trade bars only.
    """
    lead_in = _lead_in_sessions(hold_sessions)
    oldest_lead_in = _retreat_sessions(arrival_session, lead_in, exchange)
    newest_trailing = advance_trading_sessions(horizon_session, TRAILING_SESSIONS, exchange)

    # The set of sessions we will KEEP as context: lead-in is strictly before
    # arrival, trailing strictly after the horizon. The in-trade sessions
    # (arrival..horizon) are intentionally NOT in either set — they keep their
    # minute-fold candle and a context bar must never overwrite them.
    lead_in_sessions = {
        _retreat_sessions(arrival_session, i, exchange) for i in range(1, lead_in + 1)
    }
    trailing_sessions = {
        advance_trading_sessions(horizon_session, i, exchange)
        for i in range(1, TRAILING_SESSIONS + 1)
    }
    keep = lead_in_sessions | trailing_sessions

    # One generous calendar range covers both ends in a single fetch. Build the
    # bounds from the session DATE at midnight UTC (not the exchange-open instant,
    # e.g. 13:30 UTC): Polygon daily bars may be keyed at 00:00 UTC, so a window
    # starting at the open instant could drop the boundary session's daily bar.
    # Pad the ends so the boundary session's bar is always inside the window; the
    # keep-set filtering below still restricts the output to exactly the kept
    # sessions, so this only widens/aligns the request range.
    start = dt.datetime.combine(oldest_lead_in, dt.time.min, tzinfo=dt.UTC) - dt.timedelta(days=1)
    end = dt.datetime.combine(newest_trailing, dt.time.min, tzinfo=dt.UTC) + dt.timedelta(days=2)

    try:
        raw = list(daily_bar_fetch(ticker, start, end))
    except Exception as exc:
        logger.warning(
            "chart-payload: daily context fetch failed for %s [%s, %s] — %s; in-trade only.",
            ticker,
            oldest_lead_in.isoformat(),
            newest_trailing.isoformat(),
            exc,
        )
        return [], []
    if not raw:
        return [], []

    folded = _daily_bars_from_context(raw, keep_sessions=keep, exchange=exchange)
    arrival_iso = arrival_session.isoformat()
    horizon_iso = horizon_session.isoformat()
    lead = [b for b in folded if b["time"] < arrival_iso]
    trail = [b for b in folded if b["time"] > horizon_iso]
    return lead, trail


def _merge_bars(
    lead_in: Sequence[Mapping[str, Any]],
    in_trade: Sequence[Mapping[str, Any]],
    trailing: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Merge context-pre + in-trade + context-post into one date-ordered list.

    Dedup by ``time``: the in-trade (minute-fold) bar WINS on any overlap so a
    context daily bar can never overwrite an in-trade session's minute union.
    """
    by_time: dict[str, dict[str, Any]] = {}
    for bar in lead_in:
        by_time[bar["time"]] = dict(bar)
    for bar in trailing:
        by_time[bar["time"]] = dict(bar)
    for bar in in_trade:  # in-trade wins on overlap
        by_time[bar["time"]] = dict(bar)
    return [by_time[t] for t in sorted(by_time)]


def build_chart_payload(
    setup: Mapping[str, Any] | None,
    bars: Sequence[Mapping[str, Any]],
    outcome: LadderOutcome,
    *,
    arrival_session: dt.date,
    horizon_session: dt.date,
    exchange: str = DEFAULT_EXCHANGE,
    ticker: str = "",
    daily_bar_fetch: DailyBarFetch | None = None,
) -> dict[str, Any]:
    """Build the chart payload for one (brief_date, ticker).

    ``setup`` is the parsed ``brief_trade_setup`` dict; ``bars`` are the cached
    minute bars over the hold window; ``outcome`` is the replay outcome (the caller
    passes the same one the store row was built from). Returns a JSON-serialisable
    dict (see the module docstring / the payload shape below).

    * ``NO_STRUCTURE`` when the setup has no parseable ladder (``parse_ladder``
      ``ok=False``).
    * ``NO_DATA`` when there are no minute bars in the RTH window (empty / all
      pre-post-market / outside the session span).
    * ``OK`` otherwise: daily candles + price lines + markers, all aligned so every
      marker time is an emitted daily bar time.

    Context window (option A): the in-trade sessions (arrival..horizon) keep the
    minute-fold path so a daily high/low stays the union of its minute high/low.
    When ``daily_bar_fetch`` is supplied, a lead-in (before arrival) + trailing
    (after horizon) band of DAILY aggregates is fetched and merged around the
    in-trade candles for readable market structure. Context bars carry NO markers
    and the in-trade bar wins on any date overlap. A failed / empty context fetch
    degrades silently to the in-trade bars only.
    """
    parsed = parse_ladder(setup)
    if not parsed.ok:
        return _no_structure_payload()

    session_windows = _rth_session_windows(arrival_session, horizon_session, exchange)
    daily = _daily_bars_from_minute(bars, session_windows)

    # The daily CONTEXT bars (lead-in before arrival + trailing after the horizon)
    # are computed regardless of whether the in-trade window produced any candle. A
    # freshly-started OPEN position has no in-trade minute bars yet, but its PLAN
    # (lead-in context candles + the entry/TP/stop price lines) must still render —
    # never an empty "no data" box. When there ARE in-trade bars the hold spans
    # arrival..horizon; with none yet the hold is 0 sessions so the LEAD_IN_FLOOR
    # (20) still governs the lead-in.
    lead_in: list[dict[str, Any]] = []
    trailing: list[dict[str, Any]] = []
    if daily_bar_fetch is not None:
        hold_sessions = (
            trading_days_elapsed(arrival_session, horizon_session, exchange) if daily else 0
        )
        lead_in, trailing = _context_bars(
            ticker,
            daily_bar_fetch,
            arrival_session=arrival_session,
            horizon_session=horizon_session,
            hold_sessions=hold_sessions,
            exchange=exchange,
        )

    if not daily:
        # In-trade window empty. Plan-preview the PLAN over the context bars when
        # any exist; only a total absence of bars (no in-trade AND no context) is
        # honest NO_DATA.
        context_bars = _merge_bars(lead_in, [], trailing)
        if not context_bars:
            return _no_data_payload()
        return {
            "status": "OK",
            "bars": context_bars,
            "price_lines": _price_lines(setup),
            "markers": [],  # no fills yet — a plan preview, not a replay
            "ambiguous_bars": int(outcome.ambiguous_bars),
            "intrabar_rule": TIE_BREAK_SL_FIRST,
            "rth_only": True,
        }

    # Restrict the marker mapping to the windows that actually produced a daily bar
    # so a crossing on a session with no emitted candle is dropped (not just one
    # outside the calendar span). Markers map to in-trade session dates, which the
    # context merge preserves, so the "every marker time lands on an existing bar"
    # invariant is unaffected by the context bars.
    emitted_dates = {b["time"] for b in daily}
    emitted_windows = [w for w in session_windows if w[0].isoformat() in emitted_dates]
    markers = _markers_from_sequence(outcome, emitted_windows)

    bars_out = _merge_bars(lead_in, daily, trailing)

    return {
        "status": "OK",
        "bars": bars_out,
        "price_lines": _price_lines(setup),
        "markers": markers,
        "ambiguous_bars": int(outcome.ambiguous_bars),
        "intrabar_rule": TIE_BREAK_SL_FIRST,
        "rth_only": True,
    }


def _default_bar_fetch(ticker: str, arrival_session: dt.date) -> Sequence[Mapping[str, Any]]:
    """Production bar source: the population monitor's per-(ticker, arrival) cache.

    Reads the SAME ``~/.alphalens/population_ladders/bars/TICKER_DATE.parquet`` the
    monitor already populated for the replay — no new Polygon fetch, no new cache.
    Returns ``[]`` when the cache is absent (the row gets a NO_DATA payload).
    """
    from alphalens_pipeline.feedback.population_ladder_monitor import _read_cached_bars

    store_dir = Path.home() / ".alphalens" / "population_ladders"
    return _read_cached_bars(store_dir, ticker, arrival_session)


def _default_daily_bar_fetch(
    ticker: str, start: dt.datetime, end: dt.datetime
) -> Sequence[Mapping[str, Any]]:
    """Production daily-context source: the canonical Polygon client.

    Pulls DAILY aggregates over the context calendar range; same bar dict shape as
    the minute bars (``t``/``o``/``h``/``l``/``c``/``v``). Mirrors
    ``benchmark_excess._default_bar_fetch`` but with ``timespan="day"``.
    """
    from alphalens_pipeline.data.alt_data.polygon_client import get_default_polygon_client

    return get_default_polygon_client().get_agg_range(
        ticker=ticker, start=start, end=end, timespan="day"
    )


def _memoized_daily_fetch(daily_fetch: DailyBarFetch) -> DailyBarFetch:
    """Wrap a daily-context fetch in a per-run memo keyed by ``(ticker, start, end)``.

    The same ticker can surface under several themes on one date (several store
    rows in one parquet), each asking ``build_chart_payload`` for the identical
    ``(ticker, start, end)`` Polygon daily fetch. Sharing ONE call across those
    rows saves quota and avoids 429s. Only SUCCESSFUL results are cached (an empty
    list from a successful empty return IS cached); a raise is never memoized into
    a poisoned empty, so the per-row graceful degradation (in-trade-only) is
    preserved and a later transient recovery still re-fetches.
    """
    cache: dict[tuple[str, dt.datetime, dt.datetime], Sequence[Mapping[str, Any]]] = {}

    def cached(ticker: str, start: dt.datetime, end: dt.datetime) -> Sequence[Mapping[str, Any]]:
        key = (ticker, start, end)
        if key not in cache:
            cache[key] = daily_fetch(ticker, start, end)  # a raised error propagates, uncached
        return cache[key]

    return cached


def enrich_store_with_chart_payloads(
    store_dir: Path | str,
    briefs_dir: Path | str,
    *,
    bar_fetch: ChartBarFetch | None = None,
    daily_bar_fetch: DailyBarFetch | None = None,
    exchange: str = DEFAULT_EXCHANGE,
    deadline: Any = None,
) -> int:
    """Add / refresh the ``chart_payload_json`` column on every store parquet.

    For each ``YYYY-MM-DD.parquet`` row: resolve the brief's trade setup, fetch the
    row's cached minute bars (keyed by ticker + arrival session), filter to RTH,
    replay the ladder, build the chart payload, and persist it as a JSON string.
    Rewrites the frame atomically. Returns the number of rows that got a non-empty
    (non-NO_DATA / non-NO_STRUCTURE) payload.

    Mirrors :func:`benchmark_excess.enrich_store_with_benchmark_excess`: never
    raises per row (a bad row / missing brief leaves a NO_DATA payload and is
    logged), idempotent + self-healing over every store parquet, atomic write.

    When ``deadline`` is provided and ``deadline.should_stop()`` is True at the
    top of the per-row loop, the loop breaks early. Rows left unprocessed keep
    their existing values in the store and are retried on the next run. ``deadline``
    is typed ``Any`` to avoid a circular import; callers pass a ``_RunDeadline``
    instance.
    """
    store = Path(store_dir)
    briefs = Path(briefs_dir)
    if not store.exists():
        return 0
    fetch = bar_fetch or _default_bar_fetch
    daily_fetch = _memoized_daily_fetch(daily_bar_fetch or _default_daily_bar_fetch)

    n_with_chart = 0
    setups_by_date: dict[dt.date, dict[str, dict] | None] = {}

    for path in sorted(store.glob("*.parquet")):
        try:
            df = pd.read_parquet(path)
        except (OSError, ValueError) as exc:
            logger.warning("chart-payload: bad store parquet %s — %s; skipping.", path, exc)
            continue
        if df.empty:
            continue

        payload_col: list[str] = []
        stopped_early = False
        for _, row in df.iterrows():
            if deadline is not None and deadline.should_stop():
                stopped_early = True
                break
            payload = _payload_for_row(
                dict(row),
                fetch=fetch,
                daily_fetch=daily_fetch,
                exchange=exchange,
                setups_by_date=setups_by_date,
                briefs_dir=briefs,
            )
            if payload.get("status") == "OK":
                n_with_chart += 1
            payload_col.append(json.dumps(payload))

        if stopped_early:
            # Deadline tripped mid-file: leave the parquet untouched so
            # unprocessed rows are retried on the next run. Break rather
            # than continue — every subsequent file would be opened only to
            # immediately skip (deadline latches), so skip the open entirely.
            break

        df[CHART_PAYLOAD_COLUMN] = payload_col
        _write_atomic(path, df)

    return n_with_chart


def _payload_for_row(
    row: Mapping[str, Any],
    *,
    fetch: ChartBarFetch,
    daily_fetch: DailyBarFetch,
    exchange: str,
    setups_by_date: dict[dt.date, dict[str, dict] | None],
    briefs_dir: Path,
) -> dict[str, Any]:
    """Build one row's chart payload; never raises (NO_DATA on any failure)."""
    try:
        # Lazy import (population_ladder_monitor <-> ladder_chart would be a top-level
        # import cycle): _filter_bars_to_rth and _engine_cutoffs both live there.
        from alphalens_pipeline.feedback.population_ladder_monitor import (
            _engine_cutoffs,
            _filter_bars_to_rth,
        )

        brief_date = _as_date(row.get("brief_date"))
        ticker = str(row.get("ticker") or "").upper()
        if brief_date is None or not ticker:
            return _no_data_payload()

        setup = _setup_for(brief_date, ticker, setups_by_date, briefs_dir)
        if setup is None:
            return _no_structure_payload()

        arrival_session = session_on_or_after(brief_date, exchange)
        raw_bars: list[dict[str, Any]] = [dict(b) for b in fetch(ticker, arrival_session)]
        if not raw_bars:
            return _no_data_payload()

        horizon_session = _horizon_session(arrival_session, raw_bars, exchange)
        rth_bars = _filter_bars_to_rth(raw_bars, arrival_session, horizon_session, exchange)
        # Replay with the SAME entry-TTL / position-TTL cutoffs the classification used
        # (population_ladder_monitor). Without them the chart re-replays TTL-less and
        # fills a limit touched only AFTER the order expired -> a stale E1 marker on a
        # NO_FILL row (and a missing TIME_STOP past the position TTL). The cutoffs make
        # the chart's modeled fills match the stored ladder_classification.
        # Named unpack (over cutoffs[5]/[6]) so the two ms scalars are self-documenting.
        (_arr, _ent_s, _pos_s, _ent_ttl, _pos_ttl, entry_expiry_ms, position_expiry_ms) = (
            _engine_cutoffs(brief_date, setup, exchange)
        )
        # reference_close is intentionally omitted: it only anchors forward_return,
        # which the chart markers / sequence do not use.
        outcome = replay_ladder(
            setup,
            rth_bars,
            entry_expiry_ms=entry_expiry_ms,
            position_expiry_ms=position_expiry_ms,
        )
        return build_chart_payload(
            setup,
            rth_bars,
            outcome,
            arrival_session=arrival_session,
            horizon_session=horizon_session,
            exchange=exchange,
            ticker=ticker,
            daily_bar_fetch=daily_fetch,
        )
    except Exception:
        logger.exception(
            "chart-payload: failed for %s/%s; persisting NO_DATA",
            row.get("brief_date"),
            row.get("ticker"),
        )
        return _no_data_payload()


def _horizon_session(
    arrival_session: dt.date, bars: Sequence[Mapping[str, Any]], exchange: str
) -> dt.date:
    """The last session the cached bars reach (drives the RTH window span).

    Derived from the newest cached bar's timestamp so the daily aggregation covers
    exactly the bars on disk — no separate horizon arithmetic, no over-walking the
    calendar past the data. Never earlier than the arrival session.
    The newest bar's calendar date is rolled to the session on-or-after it. The
    session-window walk + RTH filter then drop any session past the data, so an
    over-shoot is harmless; the only guard needed is "never before arrival".

    Defensive: ``bars`` empty -> the arrival session (a zero-length window the
    callers already handle as NO_DATA / plan-preview). Callers guard this today,
    but the empty fallback keeps the helper from raising on ``max()`` over [].
    """
    if not bars:
        return arrival_session
    last_ts = max(int(b["t"]) for b in bars)
    last_dt = dt.datetime.fromtimestamp(last_ts / 1000, dt.UTC).date()
    horizon = session_on_or_after(last_dt, exchange)
    return max(horizon, arrival_session)


def _setup_for(
    brief_date: dt.date,
    ticker: str,
    setups_by_date: dict[dt.date, dict[str, dict] | None],
    briefs_dir: Path,
) -> dict | None:
    """Resolve the trade setup for (brief_date, ticker), caching per brief date."""
    if brief_date not in setups_by_date:
        setups_by_date[brief_date] = _load_setups_for_date(brief_date, briefs_dir)
    setups = setups_by_date[brief_date]
    if not setups:
        return None
    return setups.get(ticker)


def _load_setups_for_date(brief_date: dt.date, briefs_dir: Path) -> dict[str, dict] | None:
    """``{TICKER: trade_setup}`` for one brief date, or ``None`` when unavailable."""
    from alphalens_pipeline.paper.brief_loader import load_brief

    try:
        candidates = load_brief(brief_date, briefs_dir)
    except (FileNotFoundError, ValueError) as exc:
        logger.info(
            "chart-payload: no brief for %s — %s; leaving the date NULL.",
            brief_date.isoformat(),
            exc,
        )
        return None
    except Exception as exc:  # any other brief-load error must NOT abort the sweep
        logger.warning(
            "chart-payload: brief load failed for %s — %s; leaving the date NULL.",
            brief_date.isoformat(),
            exc,
        )
        return None
    return {c.ticker.upper(): c.trade_setup for c in candidates if c.trade_setup is not None}


def _as_date(value: Any) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if hasattr(value, "to_pydatetime"):
        try:
            return value.to_pydatetime().date()
        except (ValueError, TypeError):
            return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _write_atomic(path: Path, df: pd.DataFrame) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp)
    os.replace(tmp, path)


__all__ = [
    "CHART_PAYLOAD_COLUMN",
    "LEAD_IN_CAP",
    "LEAD_IN_FLOOR",
    "TRAILING_SESSIONS",
    "build_chart_payload",
    "enrich_store_with_chart_payloads",
]
