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
# A TP price level the path TOUCHED but that sold NO tranche (its re-based share
# was 0 because a shallow entry fill let an earlier tranche take the whole held
# position). Drawn distinctly (hollow) so three green arrows never overstate what
# was actually captured. See LadderOutcome.realized_tp_ids.
_MARKER_TP_TOUCHED = "TP_TOUCHED"
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


def _marker_kind_and_label(level_id: str, kind: str, *, sold: bool = True) -> tuple[str, str]:
    """Map a replay crossing ``(level_id, kind)`` to the chart marker kind + label.

    Labels are the compact level ids the UI draws (``E1``, ``TP1``, ``SL``); the
    time-stop carries the ``TIME_STOP`` label so the tooltip reads honestly. A TP
    crossing that sold no tranche (``sold=False``) maps to ``TP_TOUCHED`` — the
    price touched the level but the held position was already flat.
    """
    if kind == "ENTRY":
        return _MARKER_ENTRY, level_id
    if kind == "TP":
        return (_MARKER_TP if sold else _MARKER_TP_TOUCHED), level_id
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
    realized_tps = set(outcome.realized_tp_ids)
    markers: list[dict[str, Any]] = []
    for crossing in outcome.sequence:
        session = _session_date_for_ts(crossing.bar_ts_ms, session_windows)
        if session is None:
            continue  # dangling time -> would not render; drop honestly
        # A TP crossing counts as SOLD only if it appears in realized_tp_ids
        # (positive re-based share). Non-TP crossings pass sold=True unchanged.
        sold = crossing.kind != "TP" or crossing.level_id in realized_tps
        marker_kind, label = _marker_kind_and_label(crossing.level_id, crossing.kind, sold=sold)
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
    deep_lead_in: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(lead_in_bars, trailing_bars)`` daily context candles around the trade.

    Session counts → a single generous CALENDAR date range for one Polygon daily
    fetch (sessions ≠ calendar days, so the range is widened: lead-in walks the
    calendar back ``lead_in`` sessions, trailing walks forward ``TRAILING_SESSIONS``
    sessions, then the fetched bars are filtered back to exactly the kept sessions
    — Polygon daily aggregates are already session-only, no weekend/holiday
    phantoms). A fetch that raises or returns empty degrades to no context
    (``([], [])``) so the caller falls back to the in-trade bars only.

    ``deep_lead_in`` (PR-2 write-once fetch): when True the lead-in is captured
    at the full ``LEAD_IN_CAP`` depth regardless of ``hold_sessions``, so a young
    position's later-deepening hold never needs a second fetch to widen an
    already-persisted band — the caller sets this whenever it is about to spend a
    genuine Polygon fetch (``context_allowed`` True in the enrich pass).
    """
    lead_in = LEAD_IN_CAP if deep_lead_in else _lead_in_sessions(hold_sessions)
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

    raw = list(daily_bar_fetch(ticker, start, end))
    if not raw:
        # A delisted / never-covered ticker has a genuinely empty context feed —
        # not a failure. The caller stamps CONTEXT_OK on the empty band, so the
        # terminal freezes on a marker-core-only chart rather than re-fetching an
        # empty feed forever.
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


# Context-state stamp values (memo §3a). Written on every OK payload so the freeze
# gate + anti-downgrade guard can tell a full-context chart from a marker-core-only
# one. A MISSING key defaults to CONTEXT_OK (the legacy already-final tail).
CONTEXT_OK = "OK"
CONTEXT_REUSED = "reused"
CONTEXT_IN_TRADE_ONLY = "in_trade_only"


def _split_reused_context(
    reused_bars: Sequence[Mapping[str, Any]],
    arrival_session: dt.date,
    horizon_session: dt.date,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split reused prior-payload bars into ``(lead_in, trailing)`` around the trade.

    Reused bars are daily candles from a prior ``chart_payload_json`` (``time`` /
    ``open`` / ``high`` / ``low`` / ``close`` / ``volume``). Bars strictly BEFORE
    arrival are lead-in, bars strictly AFTER the horizon are trailing; bars inside
    ``[arrival, horizon]`` are dropped — the fresh in-trade minute fold owns those
    sessions and wins on overlap in :func:`_merge_bars`, so a reused bar now inside
    an extended hold is correctly superseded.
    """
    arrival_iso = arrival_session.isoformat()
    horizon_iso = horizon_session.isoformat()
    lead: list[dict[str, Any]] = []
    trail: list[dict[str, Any]] = []
    for bar in reused_bars:
        time = bar.get("time")
        if not isinstance(time, str):
            continue
        if time < arrival_iso:
            lead.append(dict(bar))
        elif time > horizon_iso:
            trail.append(dict(bar))
    return lead, trail


def _resolve_context_bars(
    ticker: str,
    daily_bar_fetch: DailyBarFetch | None,
    reused_context_bars: Sequence[Mapping[str, Any]] | None,
    *,
    arrival_session: dt.date,
    horizon_session: dt.date,
    has_in_trade: bool,
    exchange: str,
    deep_lead_in: bool = False,
    reuse_is_complete: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """``(lead_in, trailing, context_state)`` for the payload's cosmetic band.

    * ``daily_bar_fetch`` supplied and it did not raise -> ``CONTEXT_OK`` (even when
      it returns ``[]`` — a delisted / empty ticker resolves to OK-context rather
      than retrying forever); a raised fetch degrades to ``CONTEXT_IN_TRADE_ONLY``
      so a later run re-attempts the band.
    * ``daily_bar_fetch`` None and ``reuse_is_complete`` True -> the band is
      ESTABLISHED (the caller took the reuse branch because nothing needed
      fetching, not because the budget was exhausted): stamp ``CONTEXT_OK`` — the
      reused bars are spliced in when present, or the band stays empty (a
      delisted / never-covered ticker) — either way the band is complete and must
      never be re-attempted.
    * ``daily_bar_fetch`` None and ``reuse_is_complete`` False -> the caller was
      STARVED (a genuine fetch was owed but the deadline stopped it): splice in
      ``reused_context_bars`` when present and stamp ``CONTEXT_REUSED``
      (provisional), else ``CONTEXT_IN_TRADE_ONLY``. Both provisional stamps keep
      the row eligible so a later budgeted night fetches the real band.

    ``deep_lead_in`` is forwarded to :func:`_context_bars` unchanged (PR-2
    write-once fetch depth); irrelevant when no fetch runs. ``reuse_is_complete``
    is the caller's ``not needs_fetch`` (memo: reuse-first steady-state fix) —
    it is what lets an established ongoing row's reuse stamp ``OK`` forever
    instead of oscillating OK -> reused -> re-fetch -> OK each cycle.
    """
    if daily_bar_fetch is not None:
        hold_sessions = (
            trading_days_elapsed(arrival_session, horizon_session, exchange) if has_in_trade else 0
        )
        try:
            lead_in, trailing = _context_bars(
                ticker,
                daily_bar_fetch,
                arrival_session=arrival_session,
                horizon_session=horizon_session,
                hold_sessions=hold_sessions,
                exchange=exchange,
                deep_lead_in=deep_lead_in,
            )
        except Exception as exc:
            logger.warning(
                "chart-payload: daily context fetch failed for %s — %s; in-trade only.",
                ticker,
                exc,
            )
            return [], [], CONTEXT_IN_TRADE_ONLY
        return lead_in, trailing, CONTEXT_OK
    lead_in, trailing = (
        _split_reused_context(reused_context_bars, arrival_session, horizon_session)
        if reused_context_bars
        else ([], [])
    )
    if reuse_is_complete:
        return lead_in, trailing, CONTEXT_OK
    if reused_context_bars:
        return lead_in, trailing, CONTEXT_REUSED
    return [], [], CONTEXT_IN_TRADE_ONLY


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
    reused_context_bars: Sequence[Mapping[str, Any]] | None = None,
    deep_lead_in: bool = False,
    reuse_is_complete: bool = False,
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
    degrades silently to the in-trade bars only. ``deep_lead_in`` (PR-2 write-once
    fetch): when a fetch actually runs, pass True to capture the lead-in at the
    full ``LEAD_IN_CAP`` depth so the persisted band never needs deepening later.

    ``reuse_is_complete`` (reuse-first steady-state fix): only meaningful when
    ``daily_bar_fetch`` is None. True means the caller took the reuse branch
    because the band is already ESTABLISHED (nothing needed fetching), so the
    reused band — spliced when present, empty for a delisted ticker — is
    stamped ``CONTEXT_OK`` and never revisited. False (the default) means the
    caller was STARVED (a fetch was owed but the budget/deadline stopped it),
    so the reused band stays provisional (``CONTEXT_REUSED`` /
    ``CONTEXT_IN_TRADE_ONLY``) and stays eligible for a real fetch later.
    """
    parsed = parse_ladder(setup)
    if not parsed.ok:
        return _no_structure_payload()

    session_windows = _rth_session_windows(arrival_session, horizon_session, exchange)
    daily = _daily_bars_from_minute(bars, session_windows)

    # The daily CONTEXT bars (lead-in before arrival + trailing after the horizon)
    # are resolved regardless of whether the in-trade window produced any candle. A
    # freshly-started OPEN position has no in-trade minute bars yet, but its PLAN
    # (lead-in context candles + the entry/TP/stop price lines) must still render —
    # never an empty "no data" box. When there ARE in-trade bars the hold spans
    # arrival..horizon; with none yet the hold is 0 sessions so the LEAD_IN_FLOOR
    # (20) still governs the lead-in. The marker-core / in-trade candles are built
    # in EVERY branch (Polygon-free); ``context_state`` records whether the cosmetic
    # band is fresh ("OK"), spliced from a prior payload ("reused"), or absent
    # ("in_trade_only") so the freeze gate + anti-downgrade guard can reason on it.
    lead_in, trailing, context_state = _resolve_context_bars(
        ticker,
        daily_bar_fetch,
        reused_context_bars,
        arrival_session=arrival_session,
        horizon_session=horizon_session,
        has_in_trade=bool(daily),
        exchange=exchange,
        deep_lead_in=deep_lead_in,
        reuse_is_complete=reuse_is_complete,
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
            "context": context_state,
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
        "context": context_state,
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


def _is_terminal_row(row: Mapping[str, Any]) -> bool:
    """Whether a store row is a resolved (terminal) position; NaN/None/missing -> False.

    ``pd.isna`` guards a NaN terminal (``bool(nan)`` is True) and a missing column
    (``None``); only a real truthy flag counts as terminal.
    """
    terminal = row.get("terminal")
    return not pd.isna(terminal) and bool(terminal)


def _is_frozen_terminal_ok(row: Any) -> bool:
    """Whether a store row is a resolved position whose chart is already final.

    A terminal row whose ``chart_payload_json`` is OK **and reaches the row's
    close** never changes — its price path is frozen and its ladder is fully
    resolved — so the nightly enrich pass can preserve it verbatim and skip the
    Polygon re-fetch. It is NOT frozen (must be re-priced) when:

    * ``terminal`` is falsy / NaN / absent — an ongoing position whose path can
      still extend the next session; or
    * the existing chart is missing / not OK (a NO_DATA payload from a prior
      transient Polygon gap) — so a later run can self-heal it to OK; or
    * the payload's last bar predates ``matured_at`` — a row that terminalized
      AFTER its last successful chart build carries a STALE chart (the
      2026-07-13 incident: charts frozen several sessions before their close
      after a multi-night enrich blackout); it stays eligible for rebuild until
      the chart reaches the close, then freezes. A row with no ``matured_at``
      (old-format tail) keeps the legacy freeze.
    """
    if not _is_terminal_row(row):
        return False
    existing = row.get(CHART_PAYLOAD_COLUMN)
    if not isinstance(existing, str) or not existing:
        return False
    try:
        payload = json.loads(existing)
    except (ValueError, TypeError):
        return False
    if payload.get("status") != "OK":
        return False
    matured = _as_date(row.get("matured_at"))
    if matured is None:
        return True
    bars = payload.get("bars") or []
    if not bars:
        return False
    last_bar = _as_date(bars[-1].get("time"))
    if last_bar is None or last_bar < matured:
        return False
    # Context-completeness gate (memo §3e): a marker-core-only chart is not final
    # until its cosmetic band is filled. A "reused" / "in_trade_only" terminal stays
    # eligible so a later budgeted run upgrades its band to "OK", then it freezes. A
    # MISSING key defaults to OK so the legacy already-final tail stays frozen (no
    # mass unfreeze / 429 storm on pre-fix DONE charts).
    return payload.get("context", CONTEXT_OK) == CONTEXT_OK


# One non-frozen row's build result: (payload_json_or_prior, is_ok, preserved).
# ``preserved`` is True only when the anti-downgrade guard kept the prior OK string
# byte-identical (so an all-preserved file skips the rewrite).
_BuiltPayload = tuple[str, bool, bool]


def _chart_frame_payloads(
    df: pd.DataFrame,
    *,
    path: Path,
    built: dict[tuple[str, int], _BuiltPayload],
) -> tuple[list[str], int, bool]:
    """Assemble the ``chart_payload_json`` column for one frame from pre-built rows.

    Returns ``(payload_col, n_with_chart, recomputed_any)``. A frozen terminal-OK
    row is preserved verbatim (never in ``built``). Every non-frozen row's payload
    was already built by :func:`_build_nonfrozen_payloads` (which rebuilds the
    Polygon-free marker core unconditionally and spends the scarce context band
    oldest-matured-first under a live wall-clock check); this pass only copies the
    result into position. A row whose result was anti-downgrade-preserved does NOT
    flip ``recomputed_any`` so an all-frozen / all-preserved file skips the rewrite
    and stays byte-identical on disk.
    """
    payload_col: list[str] = []
    n_with_chart = 0
    recomputed_any = False
    for iloc, (_, row) in enumerate(df.iterrows()):
        if _is_frozen_terminal_ok(row):
            # Resolved position with a complete OK chart — never changes. Preserve
            # the existing payload byte-for-byte and spend no fetch on it.
            payload_col.append(str(row[CHART_PAYLOAD_COLUMN]))
            n_with_chart += 1
            continue
        payload_str, is_ok, preserved = built[(str(path), iloc)]
        payload_col.append(payload_str)
        if is_ok:
            n_with_chart += 1
        if not preserved:
            recomputed_any = True
    return payload_col, n_with_chart, recomputed_any


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
    Rewrites the frame atomically. Returns the number of rows that have an OK chart
    payload after the pass — freshly built this run, OR preserved from a prior run
    for a frozen terminal row (see the fast-follow note below). So the count stays
    stable across nights even as more rows freeze, rather than cratering to only the
    rows recomputed this run.

    Mirrors :func:`benchmark_excess.enrich_store_with_benchmark_excess`: never
    raises per row (a bad row / missing brief leaves a NO_DATA payload and is
    logged), idempotent + self-healing over every store parquet, atomic write.

    ``deadline`` gates ONLY the cosmetic context band, never the exit marker. The
    Polygon-free marker core + in-trade candles are rebuilt for EVERY non-frozen
    row on EVERY run (no file-walk early break, no per-row break), so no maturation
    timing or queue position can defer an exit marker. When ``deadline`` is provided
    the scarce context band is spent OLDEST-MATURED-FIRST (:func:`_build_nonfrozen_payloads`)
    with a live per-fetch wall-clock check, so a long-held terminal's cosmetic band
    is not perpetually starved behind newer rows and the Polygon fetch phase stays
    bounded. ``deadline`` is typed ``Any`` to avoid a circular import; callers pass a
    ``_RunDeadline`` instance.

    Polygon-budget fast-follow: a frozen terminal-OK row (a resolved position whose
    OK chart already reaches its close AND whose context band is complete —
    :func:`_is_frozen_terminal_ok`) is preserved verbatim WITHOUT re-fetching, since
    its price path and ladder never change. An all-frozen file skips the rewrite
    entirely (``recomputed_any`` False) so the fully-resolved tail costs zero I/O.
    """
    store = Path(store_dir)
    briefs = Path(briefs_dir)
    if not store.exists():
        return 0
    fetch = bar_fetch or _default_bar_fetch
    daily_fetch = _memoized_daily_fetch(daily_bar_fetch or _default_daily_bar_fetch)

    # Load every frame once (the maturation-ordered build + the per-frame assembly
    # read the SAME df objects, so the (path, iloc) keys line up between passes).
    frames: list[tuple[Path, pd.DataFrame]] = []
    for path in sorted(store.glob("*.parquet")):
        try:
            df = pd.read_parquet(path)
        except (OSError, ValueError) as exc:
            logger.warning("chart-payload: bad store parquet %s — %s; skipping.", path, exc)
            continue
        if df.empty:
            continue
        frames.append((path, df))

    # Build every non-frozen row's payload in ONE pass, oldest-matured-first. The
    # Polygon-free marker core is rebuilt unconditionally; the scarce context band
    # is rationed by a LIVE ``deadline.should_stop()`` check evaluated right before
    # each row's fetch, so the real Polygon work accrues wall-clock and the budget
    # genuinely bounds the fetch phase (a zero-I/O grant census could not — it
    # would grant all-or-nothing whenever any budget remained).
    setups_by_date: dict[dt.date, dict[str, dict] | None] = {}
    built = _build_nonfrozen_payloads(
        frames,
        fetch=fetch,
        daily_fetch=daily_fetch,
        exchange=exchange,
        setups_by_date=setups_by_date,
        briefs=briefs,
        deadline=deadline,
    )

    n_with_chart = 0
    for path, df in frames:
        payload_col, n_delta, recomputed_any = _chart_frame_payloads(df, path=path, built=built)
        n_with_chart += n_delta

        if recomputed_any:
            # An all-frozen (or all-anti-downgrade-preserved) file's payload_col is
            # byte-identical to what is on disk — skip the rewrite so the resolved
            # tail costs zero I/O (not just zero fetches) every night.
            df[CHART_PAYLOAD_COLUMN] = payload_col
            _write_atomic(path, df)

    return n_with_chart


def _maturation_sort_key(row: Mapping[str, Any]) -> dt.date:
    """Sort key for the residual context budget: oldest matured session first.

    A terminal carries ``matured_at`` (the close session); an ongoing row (no
    ``matured_at``) sorts LAST — its chart is re-priced nightly anyway, so deferring
    its cosmetic band one night never strands a resolved position.
    """
    matured = _as_date(row.get("matured_at"))
    return matured if matured is not None else dt.date.max


def _nonfrozen_candidates(
    frames: Sequence[tuple[Path, pd.DataFrame]],
) -> list[tuple[dt.date, str, int, Mapping[str, Any]]]:
    """Collect every non-frozen row as ``(matured, path_str, iloc, row_map)``.

    Sorted by ``(matured, path, iloc)`` only — never comparing the row mappings.
    """
    candidates: list[tuple[dt.date, str, int, Mapping[str, Any]]] = []
    for path, df in frames:
        for iloc, (_, row) in enumerate(df.iterrows()):
            if _is_frozen_terminal_ok(row):
                continue
            row_map = dict(row)
            candidates.append((_maturation_sort_key(row_map), str(path), iloc, row_map))
    candidates.sort(key=lambda c: (c[0], c[1], c[2]))
    return candidates


def _build_nonfrozen_payloads(
    frames: Sequence[tuple[Path, pd.DataFrame]],
    *,
    fetch: ChartBarFetch,
    daily_fetch: DailyBarFetch,
    exchange: str,
    setups_by_date: dict[dt.date, dict[str, dict] | None],
    briefs: Path,
    deadline: Any,
) -> dict[tuple[str, int], _BuiltPayload]:
    """Build every non-frozen row's payload, oldest-matured-first (memo §3c/§3d).

    Returns ``{(path_str, iloc): (payload_json_or_prior, is_ok, preserved)}``.

    The Polygon-free marker core + in-trade candles are rebuilt for EVERY
    non-frozen row (this never consults the deadline — no maturation timing or
    queue position can defer an exit marker). The scarce cosmetic context band is
    rationed OLDEST-MATURED-FIRST: candidates are sorted by maturation recency and
    the deadline is checked LIVE right before each row's CONTEXT fetch (never the
    marker core, which is always built), so the actual Polygon fetch work accrues
    wall-clock between checks and the budget genuinely bounds the fetch phase. The
    check gates the next candidate based on whether the previous fetch spent the
    budget, so one final fetch may overrun the deadline before the next is refused. A zero-I/O grant census could NOT do this — with the
    real time-based ``_RunDeadline`` its ``should_stop()`` returns the same verdict
    for every candidate, granting all-or-nothing and leaving the Polygon phase
    unbounded. ``deadline is None`` means unbounded (every row builds full context).

    The anti-downgrade guard (memo §3c) keeps a prior OK payload byte-identical
    when a fresh build is non-OK (a transient empty minute cache must not blank a
    chart that already shows the trade); such rows are flagged ``preserved`` so an
    all-preserved file skips the rewrite.
    """
    built: dict[tuple[str, int], _BuiltPayload] = {}
    for _key, path_str, iloc, row in _nonfrozen_candidates(frames):
        prior = row.get(CHART_PAYLOAD_COLUMN)
        prior_str = prior if isinstance(prior, str) and prior else None
        is_terminal = _is_terminal_row(row)
        # Reuse-first (PR-2): an ongoing row with a usable prior OK-context band
        # reuses it (its lead-in is immutable history). Fetch only genuine new
        # work: a terminal not yet frozen (one-time trailing band) or an ongoing
        # row lacking a usable prior (new arrival / self-heal). The marker core
        # is rebuilt Polygon-free for every row regardless (needs_fetch gates
        # only the cosmetic context band).
        needs_fetch = is_terminal or not _prior_context_is_ok(prior_str)
        # reuse_is_complete (the fix for the reuse-first oscillation bug): True
        # only when the row does NOT need a fetch at all — an established
        # ongoing row whose prior is already OK. It threads down into the reuse
        # branch so THAT case stamps "OK" (permanent) instead of "reused"
        # (provisional), which is what used to make the row's own reuse
        # DOWNGRADE its own stamp and re-trigger needs_fetch=True the very next
        # night — an OK -> reused -> re-fetch -> OK oscillation instead of the
        # intended steady-state zero fetches. A row that DOES need a fetch
        # (terminal, or a starved new/self-healing ongoing row) always has
        # reuse_is_complete False, so a deadline-starved reuse for THAT row
        # stays provisional and genuinely retries next budgeted night.
        reuse_is_complete = not needs_fetch
        # Live wall-clock check AT fetch time (in maturation order): the real
        # Polygon fetch below advances the monotonic clock, so a finite budget
        # bounds how many context bands are fetched and the oldest maturers win.
        context_allowed = needs_fetch and (deadline is None or not deadline.should_stop())
        payload = _payload_for_row(
            row,
            fetch=fetch,
            daily_fetch=daily_fetch,
            exchange=exchange,
            setups_by_date=setups_by_date,
            briefs_dir=briefs,
            context_allowed=context_allowed,
            prior_payload_json=prior_str,
            reuse_is_complete=reuse_is_complete,
        )
        # Anti-downgrade guard: a fresh non-OK build never overwrites an existing
        # OK payload. Keep the prior string verbatim (not re-dumped) and flag it
        # preserved so an all-preserved file skips the rewrite.
        # NOTE: the guard is on status only, not on context tier. A fresh OK build
        # with a lower context tier ("in_trade_only"/"reused") DOES replace a
        # prior "OK"-context payload. That only happens for a row that genuinely
        # needs a fetch (reuse_is_complete False — a starved terminal or a
        # self-healing ongoing row): a one-cycle provisional downgrade the freeze
        # gate keeps eligible, so oldest-matured-first restores "OK" on the next
        # budgeted night. An established ongoing row (reuse_is_complete True)
        # never takes this path at all — its reuse stamps "OK" directly, so it
        # never downgrades its own prior. Ranking context tiers here would add
        # complexity for a transient, self-healing regression — the status-only
        # contract is intentional.
        if payload.get("status") != "OK" and _prior_payload_is_ok(prior_str):
            built[(path_str, iloc)] = (prior_str, True, True)  # type: ignore[arg-type]
        else:
            built[(path_str, iloc)] = (json.dumps(payload), payload.get("status") == "OK", False)
    return built


def _parse_prior_payload(prior_payload_json: str | None) -> dict[str, Any] | None:
    """Parse a stored ``chart_payload_json`` string to a dict, or ``None`` (never raises).

    Shared parse-guard for the three prior-payload predicates below: a
    non-string / empty / unparseable / non-dict value all resolve to ``None``
    rather than raising, so the caller never needs its own try/except.
    """
    if not isinstance(prior_payload_json, str) or not prior_payload_json:
        return None
    try:
        parsed = json.loads(prior_payload_json)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _reused_context_from_prior(
    prior_payload_json: str | None,
) -> list[Mapping[str, Any]] | None:
    """Parse the prior payload's ``bars`` as a reusable context band (never raises).

    Returns the prior daily candles so :func:`build_chart_payload` can splice their
    lead-in / trailing sessions back in when the context budget is exhausted this
    run. ``None`` when the prior is missing / unparseable / has no bars.
    """
    prior = _parse_prior_payload(prior_payload_json)
    if prior is None:
        return None
    bars = prior.get("bars")
    return bars if isinstance(bars, list) and bars else None


def _prior_payload_is_ok(prior_payload_json: str | None) -> bool:
    """Whether the prior stored payload parses to ``status == "OK"`` (never raises)."""
    prior = _parse_prior_payload(prior_payload_json)
    return prior is not None and prior.get("status") == "OK"


def _prior_context_is_ok(prior_payload_json: str | None) -> bool:
    """Whether the prior stored payload is reusable as a full context band.

    True iff it parses, ``status == "OK"``, AND its context tier is complete
    (``context == "OK"``; a MISSING key defaults to OK, matching the freeze gate).
    Keyed on ``context`` — NOT on non-empty ``bars`` — so a delisted ticker whose
    empty band is stamped ``CONTEXT_OK`` is reused, never re-fetched forever.
    """
    prior = _parse_prior_payload(prior_payload_json)
    return (
        prior is not None
        and prior.get("status") == "OK"
        and prior.get("context", CONTEXT_OK) == CONTEXT_OK
    )


def _payload_for_row(
    row: Mapping[str, Any],
    *,
    fetch: ChartBarFetch,
    daily_fetch: DailyBarFetch,
    exchange: str,
    setups_by_date: dict[dt.date, dict[str, dict] | None],
    briefs_dir: Path,
    context_allowed: bool = True,
    prior_payload_json: str | None = None,
    reuse_is_complete: bool = False,
) -> dict[str, Any]:
    """Build one row's chart payload; never raises (NO_DATA on any failure).

    The marker core + in-trade candles are built from the disk minute cache in
    either branch (Polygon-free). ``context_allowed`` gates only the cosmetic band:
    when True the daily context is fetched (``context == "OK"``); when False the
    prior payload's lead-in / trailing bars are reused. The reuse outcome depends
    on ``reuse_is_complete`` (the caller's ``not needs_fetch``): True stamps
    ``context == "OK"`` (the band is already established, never re-attempted);
    False stamps the provisional ``context == "reused"`` (or, absent a prior,
    ``"in_trade_only"``) so a starved row stays eligible for a real fetch later.
    """
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
        if context_allowed:
            # A genuine Polygon fetch is about to run: capture the lead-in at
            # LEAD_IN_CAP depth (deep-first-fetch, PR-2) so the persisted band
            # is write-once and never needs deepening as the hold grows.
            return build_chart_payload(
                setup,
                rth_bars,
                outcome,
                arrival_session=arrival_session,
                horizon_session=horizon_session,
                exchange=exchange,
                ticker=ticker,
                daily_bar_fetch=daily_fetch,
                deep_lead_in=True,
            )
        # Context budget exhausted this run: build the Polygon-free marker core and
        # reuse the prior payload's lead-in / trailing band (if any).
        return build_chart_payload(
            setup,
            rth_bars,
            outcome,
            arrival_session=arrival_session,
            horizon_session=horizon_session,
            exchange=exchange,
            ticker=ticker,
            daily_bar_fetch=None,
            reused_context_bars=_reused_context_from_prior(prior_payload_json),
            reuse_is_complete=reuse_is_complete,
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
    # Twin of population_ladder_monitor._as_store_date (kept local: the modules
    # only import each other lazily). Change BOTH if the coercion rules move.
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
