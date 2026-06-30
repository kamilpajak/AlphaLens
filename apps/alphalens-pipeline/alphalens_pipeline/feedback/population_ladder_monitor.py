"""Broker-free POPULATION ladder monitor (Track A v2 PR-2).

A SECOND measurement metric beside the fixed-5-day ``shadow_return`` (which is
left untouched). Where ``shadow_return`` answers "what was the close-to-close move
over a fixed 5-session window?", this monitor replays EVERY brief candidate's
trade ladder against its real intraday price path UNTIL the position is TERMINAL
(``TP_FULL`` / ``SL_HIT`` / ``PARTIAL_TP_THEN_SL`` / ``TIME_STOP`` / ``NO_FILL``)
over the ladder's real ~42-trading-session hold — NOT 5 days. It consumes the
pure :func:`alphalens_pipeline.feedback.ladder_replay.replay_ladder` engine (which
already supports ``entry_expiry_ms`` + ``position_expiry_ms`` + ``TIME_STOP``);
this module does the enumeration, the incremental Polygon cache, and the parquet
store.

Population = the paper-trading population, mirrored broker-free: every verified
candidate with a plannable ``brief_trade_setup`` (the same predicate the paper
planner applies via :func:`validate_trade_setup`). Enumeration is from the brief
parquet (:func:`load_brief`), NEVER from any ledger / broker.

Telemetry ONLY — never a re-weighting loop. Click-orthogonal: this module reads
briefs + Polygon ONLY, never the decisions / click ledger. It imports nothing
from ``alphalens_feedback`` (FeedbackStore) or ``alphalens_pipeline.paper.{ledger,
planner,broker}``.

Maturity gate (per ROW, not per date)
-------------------------------------
A row is TERMINAL when its as-specified replay returns a terminal classification
with the position fully resolved (``classification in _TERMINAL_SET``). It is
still ONGOING — re-replayed next night — when the engine reports ``OPEN`` /
``PARTIAL_TP_OPEN`` AND its ``position_expiry_session`` is still on-or-after the
last closed session. Once ``position_expiry_session`` falls before the last closed
session the engine's ``position_expiry_ms`` forces a ``TIME_STOP`` and the row
becomes terminal that night.

Resilience
----------
* A missing brief parquet skips the date (no crash).
* A per-ticker fetch failure or an implausible (split-class) move CARRIES the
  prior row forward verbatim — the population denominator never silently shrinks.
  A brand-new ticker that fails on its first night gets a retryable placeholder
  row (``ladder_classification=None``, ``terminal=False``), never dropped.
* A terminal NO_DATA is never written on a transient gap (cache-poisoning class).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pandas as pd

from alphalens_pipeline.feedback.bar_window import (
    ARRIVAL_VWAP_WINDOW_MIN,
    IMPLAUSIBLE_RETURN_THRESHOLD,
    _window_vwap,
)
from alphalens_pipeline.feedback.ladder_config import ladder_config_version
from alphalens_pipeline.feedback.ladder_replay import (
    LadderOutcome,
    realized_r_full_fill,
    replay_ladder,
    replay_ladder_grid,
)
from alphalens_pipeline.paper.brief_loader import CandidateBrief, load_brief
from alphalens_pipeline.paper.calendar import (
    DEFAULT_EXCHANGE,
    advance_trading_sessions,
    session_on_or_after,
    session_open_utc,
)
from alphalens_pipeline.paper.constants import DEFAULT_ORDER_TTL_DAYS, TIME_STOP_DAYS
from alphalens_pipeline.paper.sizing import TradeSetupNotPlannableError, validate_trade_setup
from alphalens_pipeline.thematic.theme_text import slugify_theme

logger = logging.getLogger(__name__)

# A (ticker, window start, window end) → list of Polygon agg bars. Same shape as
# ``bar_window.BarFetch`` so the production default + test stubs are shared.
BarFetch = Callable[[str, dt.datetime, dt.datetime], Sequence[dict[str, Any]]]

# Monitor-LOCAL lookback in CALENDAR days. The hold is 42 trading sessions
# (≈ 60 calendar days); 75 gives margin so a row can still be re-replayed for a
# night or two after it matures (and after VPS downtime). DELIBERATELY NOT
# ``bar_window.DEFAULT_LOOKBACK_DAYS`` (14) — that is the fixed 5-session metric's
# window and is far too short for the full-hold replay.
MONITOR_LOOKBACK_DAYS = 75

# Per-night hard cap on Polygon MINUTE fetches (the precise Tier-2 resolve). When
# exceeded the remaining tickers are logged + DEFERRED (their prior rows carry
# forward) and retried next night — never a silent truncation that would drop
# rows from the population. The cheap Tier-1 daily screen runs for EVERYONE and
# is NOT budget-bounded (one grouped-daily call prices the whole market per day).
_MAX_FETCHES_PER_RUN = 150

# Reserved sub-budget for the periodic forced resolve (R7) + brand-new
# establishment. Drawn SEPARATELY from the main touch budget so a brief-inflow /
# crash night cannot starve a long-quiet candidate's forced re-pricing. Total
# nightly minute fetches are bounded by ``_MAX_FETCHES_PER_RUN + _FORCED_RESOLVE_BUDGET``.
_FORCED_RESOLVE_BUDGET = 50

_FETCH_DEADLINE_S_DEFAULT = 75 * 60  # wall-clock budget, under TimeoutStartSec=90min
_BREAKER_CONSECUTIVE_FAILS = 6  # consecutive real Polygon errors before fast-bail


class _RunDeadline:
    """Per-run wall-clock budget + consecutive-Polygon-error breaker.

    ``should_stop()`` latches: once the wall-clock budget is spent OR
    ``breaker_fails`` real Polygon errors arrive back-to-back, every later call
    returns True so the run stops issuing NEW fetches and defers the rest via
    the existing carry-forward path. ``record_fetch_result(ok=False)`` is fed
    ONLY on retry-exhausting PolygonError/timeout — never on a clean empty /
    NO_FILL / implausible carry.
    """

    def __init__(
        self,
        budget_s: float,
        breaker_fails: int = _BREAKER_CONSECUTIVE_FAILS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._mono = monotonic
        self._deadline = self._mono() + budget_s
        self._consecutive_fails = 0
        self._breaker_fails = breaker_fails
        self.stopped_reason: str | None = None

    def should_stop(self) -> bool:
        if self.stopped_reason is not None:
            return True
        if self._mono() >= self._deadline:
            self.stopped_reason = "deadline"
        elif self._consecutive_fails >= self._breaker_fails:
            self.stopped_reason = "breaker"
        return self.stopped_reason is not None

    def record_fetch_result(self, *, ok: bool) -> None:
        self._consecutive_fails = 0 if ok else self._consecutive_fails + 1


# Regular-trading-hours session length in minutes (09:30→16:00 ET = 390). Half-days
# derive their own (shorter) span from the calendar; see ``_session_rth_span_min``.
_RTH_FULL_SESSION_SPAN_MIN = 390

# R7 — periodic forced minute-resolve cadence. A candidate whose latest priced
# session is more than this many sessions behind the last closed session is
# force-resolved (defence against endpoint drift / future domain skew), even with
# no daily touch. K=5 = one trading week of max mis-classification latency.
_PERIODIC_RESOLVE_SESSIONS = 5

# Relative guard band on the daily touch gate (25 bps). A level counts as touched
# when ``daily_low <= level*(1+eps)`` (lower) or ``daily_high >= level*(1-eps)``
# (upper). Absorbs residual consolidated-vs-primary / odd-lot endpoint
# disagreement so a daily low one tick above the stop (true minute low one tick
# below) still resolves. Conservative — a wrong value only costs extra resolves.
_TOUCH_EPS = 0.0025

# Split-class day threshold on ``|c/prev_c − 1|`` (and the cheap-path
# implausible guard on ``|c*/last_close − 1|``). 0.18 catches 2:1 (0.5), 3:2
# (0.5), 5:4 (0.25), 1.25:1 (0.20) and their reverses, well above normal
# single-day vol. Lower than the legacy IMPLAUSIBLE_RETURN_THRESHOLD (0.60) so a
# split forces a (carry-guarded) resolve rather than corrupting open_r.
_SPLIT_SCREEN_THRESHOLD = 0.18

# Terminal classifications: the position is fully resolved and will not change on
# a future re-replay. ``BAD_GEOMETRY`` is terminal-degenerate (stop at/above the
# blended entry → realized_r undefined; it never resolves to a real number on a
# later night, so freezing it avoids pointless re-fetches). ``OPEN`` /
# ``PARTIAL_TP_OPEN`` are the ONLY ongoing states.
_TERMINAL_SET = frozenset(
    {"TP_FULL", "SL_HIT", "PARTIAL_TP_THEN_SL", "TIME_STOP", "NO_FILL", "BAD_GEOMETRY"}
)

_BAR_COLUMNS = ("t", "o", "h", "l", "c", "v")

# Portfolio-size (two-layer design) columns. These are STRICTLY ADDITIVE on top of
# the size-free R-space edge fields (realized_r / open_r / mfe / mae): they describe
# the position's portfolio contribution, never the edge of the setup. The bridge
# from "% of book" (gross exposure) to "R" (risk-normalised) is ALWAYS the stop
# distance — never suggested_size × R directly (dimensionally wrong). Listed here so
# a carried-forward OLD-format row (lacking them) can be back-filled to NULL rather
# than dropping the column from the rewritten parquet.
_SIZE_COLUMNS = (
    # Signal-time (intended geometry, independent of fills).
    "suggested_gross_weight_pct",  # suggested size as a FRACTION of book (e.g. 0.0407)
    "full_ladder_blended_entry",  # alloc-weighted blended entry over ALL 3 intended tiers
    "stop_distance_pct_full",  # (full_blended - disaster_stop) / full_blended
    "implied_risk_pct_full",  # suggested_gross_weight_pct * stop_distance_pct_full
    # Outcome-time (what actually deployed).
    "tiers_filled_count",  # 0-3 entry tiers filled
    "realized_gross_weight_pct",  # suggested_gross_weight_pct * filled_fraction
    "stop_distance_pct",  # (realized blended_entry - disaster_stop) / realized blended_entry
    "realized_risk_pct",  # realized_gross_weight_pct * stop_distance_pct
    "realized_return_pct_of_book",  # realized_r * realized_risk_pct (terminal only)
    "open_return_pct_of_book",  # open_r * realized_risk_pct (ongoing only)
)

# Grouped-daily two-tier screen columns. STRICTLY ADDITIVE (same discipline as
# ``_SIZE_COLUMNS``): a carried OLD-format row lacking them is back-filled to None.
# ``last_priced_session`` — the latest session the row's mark was advanced to
# (drives the least-recently-priced fair ordering). ``last_resolved_session`` —
# the latest session a row was produced by an actual MINUTE resolve (NOT a cheap
# update / carry); drives the R7 periodic forced resolve. They DIVERGE because the
# cheap path advances ``last_priced_session`` every night while leaving
# ``last_resolved_session`` untouched, so a nightly-cheap-advanced position still
# accumulates sessions-since-resolve and R7 can fire. ``reference_close`` — the
# FROZEN arrival-window VWAP stamped at the brand-new minute resolve; the cheap
# path recomputes ``forward_return`` from it with ZERO minute access. NEVER sourced
# from grouped-daily ``vw`` (session VWAP, wrong window) — minute-only at first
# resolve, then immutable.
_SCREEN_COLUMNS = (
    "last_priced_session",
    "last_resolved_session",
    "reference_close",
)

# The pre-computed /edge ladder-chart projection column, owned + written by the
# separate ``ladder_chart.enrich_store_with_chart_payloads`` pass. Duplicated as a
# LOCAL literal (NOT imported) because ``population_ladder_monitor`` <-> ``ladder_chart``
# is a genuine bidirectional lazy-import cycle; parity to the canonical
# ``ladder_chart.CHART_PAYLOAD_COLUMN`` is pinned by a test (same duplicate-with-parity
# precedent as ``_DEFAULT_SMOKE_TIMEOUT_S``). The monitor never builds a payload; it
# only CARRIES a prior row's last-good chart across a resolve rewrite so a
# deadline-starved enrich pass cannot blank an ongoing position's chart.
_CHART_PAYLOAD_COLUMN = "chart_payload_json"

# The load-bearing replay-config stamp (PR-1). Like the size/screen columns, a
# carried OLD-format row predating it is back-filled to None so the schema stays
# stable; the next successful replay repopulates it.
_CONFIG_COLUMNS = ("ladder_config_version",)

# Brief-provenance columns: stamped from the CandidateBrief, not computed by the
# replay engine. Like ``theme``, they travel WITH the outcome record so they stay
# queryable even after the brief is rebuilt or the candidate churns out.
_PROVENANCE_COLUMNS = ("scorer_config_version",)

# The alternate-exit-ladder grid (PR-2): a JSON map {config -> realized_r} from
# re-replaying the SAME bars under each EXIT policy. Carried/back-filled like the
# other additive measurement columns.
_GRID_COLUMNS = ("grid_realized_r_json",)

# The entry-side counterfactual (PR-3): realized R if all tiers had filled at the
# full-ladder blended entry. Paired with full_ladder_blended_entry; the gap vs
# the headline realized_r is the entry-tier-spacing drag. Carried like the rest.
_ENTRY_CF_COLUMNS = ("realized_r_full_fill",)


@dataclass(frozen=True)
class PopulationMonitorReport:
    """Summary of one monitor sweep over a single brief date."""

    brief_date: dt.date
    n_brief: int  # candidates in the brief
    n_plannable: int  # verified + plannable trade_setup
    terminal: int  # rows resolved to a terminal classification this run
    ongoing: int  # rows still OPEN / PARTIAL_TP_OPEN
    carried_forward: int  # rows whose prior row was carried (fetch fail / deferred / skip)
    fetches: int  # Polygon fetches issued this run
    cheap_updates: int = 0  # rows advanced by the cheap daily-close screen (0 minute fetches)
    resolve_queue_depth: int = 0  # candidates that needed a minute resolve this date
    deferred_touches: int = 0  # touched rows deferred past the budget (retry next night)
    oldest_deferred_touch_age: int = (
        0  # max sessions a deferred-touch row is behind (dead-man-switch)
    )
    stopped_for_deadline: int = 0  # items deferred because the run deadline tripped


def _default_bar_fetch(
    ticker: str, start: dt.datetime, end: dt.datetime
) -> Sequence[dict[str, Any]]:
    """Production bar source: the canonical Polygon client minute aggregates."""
    from alphalens_pipeline.data.alt_data.polygon_client import get_default_polygon_client

    return get_default_polygon_client().get_agg_range(ticker=ticker, start=start, end=end)


def _is_plannable(c: CandidateBrief) -> tuple[bool, str | None]:
    """Mirror the paper planner's selection predicate, broker-free.

    Returns ``(plannable, reason)``: ``reason`` is ``None`` when plannable, else a
    short human string for the store's ``nonplannable_reason`` column.
    """
    if not c.verified:
        return False, "not verified"
    if c.trade_setup is None:
        return False, "no trade_setup"
    try:
        validate_trade_setup(c.trade_setup)
    except TradeSetupNotPlannableError as exc:
        return False, str(exc)
    return True, None


def _engine_cutoffs(
    brief_date: dt.date, setup: dict, exchange: str
) -> tuple[dt.date, dt.date, dt.date, int, int, int, int]:
    """Convert the TRADING-day cutoffs to the engine's absolute epoch-ms scalars.

    Both ``TIME_STOP_DAYS`` (=42) and ``order_ttl_days`` are TRADING days, so they
    are advanced via the exchange calendar, NOT naive ms. Returns
    ``(arrival_session, entry_expiry_session, position_expiry_session,
    entry_ttl_days, position_ttl_days, entry_expiry_ms, position_expiry_ms)``.
    """
    arrival_session = session_on_or_after(brief_date, exchange)
    entry_ttl_days = int(setup.get("order_ttl_days") or DEFAULT_ORDER_TTL_DAYS)
    position_ttl_days = TIME_STOP_DAYS
    entry_expiry_session = advance_trading_sessions(arrival_session, entry_ttl_days, exchange)
    position_expiry_session = advance_trading_sessions(arrival_session, position_ttl_days, exchange)
    entry_expiry_ms = int(session_open_utc(entry_expiry_session, exchange).timestamp() * 1000)
    position_expiry_ms = int(session_open_utc(position_expiry_session, exchange).timestamp() * 1000)
    return (
        arrival_session,
        entry_expiry_session,
        position_expiry_session,
        entry_ttl_days,
        position_ttl_days,
        entry_expiry_ms,
        position_expiry_ms,
    )


def _bars_cache_path(store_dir: Path, ticker: str, arrival_session: dt.date) -> Path:
    # Keyed by (ticker, arrival_session): the cache holds exactly ONE brief's
    # window. A ticker that re-surfaces on a later brief date has a DIFFERENT
    # arrival and therefore its own cache file — so a later-arrival brief can
    # never set the cache floor for an earlier-arrival one, and the bars handed
    # to the replay are exactly that brief's [arrival, horizon] window (no
    # cross-brief slicing needed). Forward-only tail-fetch is correct because a
    # single brief's window only grows at the horizon end.
    return store_dir / "bars" / f"{ticker.upper()}_{arrival_session.isoformat()}.parquet"


def _read_cached_bars(
    store_dir: Path, ticker: str, arrival_session: dt.date
) -> list[dict[str, Any]]:
    """Read the append-only per-(ticker, arrival) bar cache (empty when absent)."""
    path = _bars_cache_path(store_dir, ticker, arrival_session)
    if not path.exists():
        return []
    try:
        df = pd.read_parquet(path)
    except (OSError, ValueError) as exc:  # corrupt / partial cache: re-fetch from scratch
        logger.warning("population-monitor: bad bar cache for %s — %s; ignoring.", ticker, exc)
        return []
    # pandas types to_dict("records") as list[dict[Hashable, Any]]; the frame's
    # columns are the fixed str _BAR_COLUMNS, so narrow to the declared type.
    return cast("list[dict[str, Any]]", df.to_dict("records"))


def _write_cached_bars(
    store_dir: Path, ticker: str, arrival_session: dt.date, bars: list[dict[str, Any]]
) -> None:
    """Atomically write the merged per-(ticker, arrival) bar cache (tmp + replace)."""
    path = _bars_cache_path(store_dir, ticker, arrival_session)
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(bars, columns=list(_BAR_COLUMNS))
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp)
    os.replace(tmp, path)


def _extend_bar_cache(
    store_dir: Path,
    ticker: str,
    arrival_session: dt.date,
    fetch: BarFetch,
    arrival_start: dt.datetime,
    horizon_end: dt.datetime,
) -> list[dict[str, Any]]:
    """Incrementally extend the per-(ticker, arrival) bar cache and return it.

    Fetches ONLY the tail ``[last_cached_ts + 1ms, horizon_end)`` and appends it
    to the cache — never re-fetches the whole growing window. The cache holds
    exactly this brief's ``[arrival_start, horizon_end]`` window (keyed by
    ``arrival_session``), so the returned bars are exactly what the replay needs
    and forward-only tail-fetch can never miss earlier bars. Returns the merged,
    de-duplicated, time-sorted bar list. Raises on a fetch error so the caller
    can carry the prior row forward.
    """
    cached = _read_cached_bars(store_dir, ticker, arrival_session)
    last_ts = max((int(b["t"]) for b in cached), default=None)
    if last_ts is None:
        fetch_start = arrival_start
    else:
        fetch_start = dt.datetime.fromtimestamp((last_ts + 1) / 1000, tz=dt.UTC)
    if fetch_start < horizon_end:
        new_bars = list(fetch(ticker, fetch_start, horizon_end))
    else:
        new_bars = []  # cache already covers the window; nothing to fetch
    merged: dict[int, dict[str, Any]] = {int(b["t"]): b for b in cached}
    for b in new_bars:
        merged[int(b["t"])] = b
    ordered = [merged[t] for t in sorted(merged)]
    if new_bars:
        _write_cached_bars(store_dir, ticker, arrival_session, ordered)
    return ordered


# A grouped-daily fetch: date -> {TICKER: {t,o,h,l,c,v,vw}} (whole-market OHLCV).
GroupedFetch = Callable[[dt.date], dict[str, dict[str, Any]]]

_GROUPED_DAILY_FIELDS = ("t", "o", "h", "l", "c", "v", "vw")


def _default_grouped_fetch(date: dt.date) -> dict[str, dict[str, Any]]:
    """Production grouped-daily source: the canonical Polygon client, ONE call/date."""
    from alphalens_pipeline.data.alt_data.polygon_client import get_default_polygon_client

    return get_default_polygon_client().get_grouped_daily(date)


def _grouped_cache_path(store_dir: Path, date: dt.date) -> Path:
    return store_dir / "grouped" / f"{date.isoformat()}.parquet"


def _read_grouped_cache(store_dir: Path, date: dt.date) -> dict[str, dict[str, Any]] | None:
    """Read the whole-market grouped-daily cache for ``date``.

    ``None`` means "not fetched yet" (distinct from ``{}`` = a genuinely empty
    session). A present cache means we have the ENTIRE market for that session, so
    a ticker absent from it did not trade.
    """
    path = _grouped_cache_path(store_dir, date)
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path)
    except (OSError, ValueError) as exc:
        logger.warning("population-monitor: bad grouped cache for %s — %s; refetching.", date, exc)
        return None
    out: dict[str, dict[str, Any]] = {}
    for _, r in df.iterrows():
        symbol = str(r["T"]).upper()
        out[symbol] = {k: r[k] for k in _GROUPED_DAILY_FIELDS if k in r.index}
    return out


def _write_grouped_cache_atomic(
    store_dir: Path, date: dt.date, payload: dict[str, dict[str, Any]]
) -> None:
    """Atomically write the FULL market grouped-daily payload for ``date`` (tmp+replace).

    Stores every ticker's row so a cached date means "the whole market for this
    session is on disk" — two candidates with disjoint tickers on the same date
    therefore share ONE fetch.
    """
    path = _grouped_cache_path(store_dir, date)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"T": sym, **{k: bar.get(k) for k in _GROUPED_DAILY_FIELDS}} for sym, bar in payload.items()
    ]
    df = pd.DataFrame(rows, columns=["T", *_GROUPED_DAILY_FIELDS])
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp)
    os.replace(tmp, path)


def _prefetch_grouped_daily(
    store_dir: Path,
    sessions: list[dt.date],
    grouped_fetch: GroupedFetch,
    exchange: str,
) -> dict[dt.date, dict[str, dict[str, Any]] | None]:
    """Fetch (once) + cache the grouped-daily map for each session in ``sessions``.

    Reuses the date-keyed cache; fetches ONLY the genuinely-new sessions (the union
    across all candidates, so a date is never fetched twice per run). A 0-result on
    a CALENDAR-CONFIRMED session is treated as an upstream gap (value ``None`` = do
    not cache, do not screen — carry / resolve), distinct from a complete cached
    session in which a ticker is simply absent.
    """
    from alphalens_pipeline.paper.calendar import is_trading_day

    result: dict[dt.date, dict[str, dict[str, Any]] | None] = {}
    for session in sessions:
        if session in result:
            continue
        cached = _read_grouped_cache(store_dir, session)
        if cached is not None:
            result[session] = cached
            continue
        if not is_trading_day(session, exchange):
            result[session] = None  # not a session — nothing to fetch / screen
            continue
        try:
            payload = grouped_fetch(session)
        except Exception as exc:  # any fetch error: treat as a gap (carry / resolve)
            logger.warning(
                "population-monitor: grouped-daily fetch failed for %s — %s.", session, exc
            )
            result[session] = None
            continue
        if not payload:
            # 0-result on a confirmed session = upstream gap; do NOT cache an empty
            # file (that would freeze a phantom "complete empty market").
            logger.warning(
                "population-monitor: grouped-daily empty on confirmed session %s.", session
            )
            result[session] = None
            continue
        _write_grouped_cache_atomic(store_dir, session, payload)
        result[session] = payload
    return result


def _session_close_utc(session: dt.date, exchange: str) -> dt.datetime:
    """UTC datetime of ``exchange``'s closing auction on session date ``session``.

    Sibling of :func:`session_open_utc` (which exposes only the open). Reads the
    venue's actual session close off the calendar so half-days resolve to their
    early (e.g. 13:00 ET) close, not the nominal 16:00. Requires ``session`` to be
    an exact session.
    """
    from alphalens_pipeline.paper.calendar import _calendar, _to_session_timestamp

    ts = _to_session_timestamp(session)
    cal = _calendar(exchange)
    return cal.session_close(ts).to_pydatetime().astimezone(dt.UTC)


def _session_rth_span_min(session: dt.date, exchange: str) -> int:
    """Regular-trading-hours span (minutes) of ``session`` on ``exchange``.

    Full sessions are :data:`_RTH_FULL_SESSION_SPAN_MIN` (390 = 09:30→16:00 ET);
    half-days (Friday after Thanksgiving, Christmas Eve, ...) are the SHORTER
    actual open→close span read off the calendar. Computed from the real
    open/close so the idiom transfers to any venue without a hard-coded table.
    """
    from alphalens_pipeline.paper.calendar import is_half_day

    if not is_half_day(session, exchange):
        return _RTH_FULL_SESSION_SPAN_MIN
    open_utc = session_open_utc(session, exchange)
    close_utc = _session_close_utc(session, exchange)
    span = int((close_utc - open_utc).total_seconds() // 60)
    # Defensive: a degenerate / mis-reported close never yields a negative or
    # zero span (which would drop the whole session); fall back to the full span.
    return span if span > 0 else _RTH_FULL_SESSION_SPAN_MIN


def _rth_window_utc(session: dt.date, exchange: str) -> tuple[int, int]:
    """``(open_ms, close_ms)`` epoch-ms RTH bounds for ``session`` (close inclusive).

    ``close_ms = open_ms + span_min*60_000`` rather than the raw calendar close so
    the minute-bar filter and the horizon-end span use ONE definition of the RTH
    window (no off-by-one between the two derivations).
    """
    open_utc = session_open_utc(session, exchange)
    open_ms = int(open_utc.timestamp() * 1000)
    close_ms = open_ms + _session_rth_span_min(session, exchange) * 60_000
    return open_ms, close_ms


def _filter_bars_to_rth(
    bars: list[dict[str, Any]],
    arrival_session: dt.date,
    horizon_session: dt.date,
    exchange: str,
) -> list[dict[str, Any]]:
    """Drop every bar outside regular trading hours of each session in the window.

    The grouped-daily [low, high] is REGULAR-SESSION only, but ``get_agg_range``
    returns pre/post-market prints too. Restricting the minute replay to RTH makes
    grouped-daily [low, high] a TRUE superset of the minute path, so the cheap
    no-touch screen can never silently drop an after-hours stop or a pre-market
    entry. Pre/post-market fills are DELIBERATELY not modelled — resting-limit
    geometry is an RTH construct. The cache itself keeps every fetched bar (a
    faithful raw record); this filter applies at replay time only.

    A bar is kept when its start ``t`` falls in ``[session_open, session_close]``
    of some session in ``[arrival_session, horizon_session]``. Sessions are walked
    via the exchange calendar so weekends / holidays inside the span contribute no
    window (and thus drop any stray bar that lands on them).
    """
    if not bars:
        return bars
    from alphalens_pipeline.paper.calendar import advance_trading_sessions, trading_days_elapsed

    n_sessions = trading_days_elapsed(arrival_session, horizon_session, exchange)
    windows: list[tuple[int, int]] = []
    for i in range(n_sessions + 1):
        session = advance_trading_sessions(arrival_session, i, exchange)
        if session > horizon_session:
            break
        windows.append(_rth_window_utc(session, exchange))
    kept: list[dict[str, Any]] = []
    for bar in bars:
        ts = int(bar["t"])
        if any(lo <= ts <= hi for lo, hi in windows):
            kept.append(bar)
    return kept


def _outcome_is_implausible(outcome: LadderOutcome) -> bool:
    """True when the replay's forward return is a likely unadjusted-split artifact.

    Mirrors the bar_window implausible-move guard (Polygon bars are
    ``adjusted=false``). When True the caller skips + carries the prior row
    forward rather than recording a corrupted outcome.
    """
    fr = outcome.forward_return
    return fr is not None and abs(fr) > IMPLAUSIBLE_RETURN_THRESHOLD


def _safe_div(num: float | None, den: float | None) -> float | None:
    """``num / den`` or ``None`` when either is missing / non-finite / den == 0."""
    if num is None or den is None:
        return None
    try:
        n = float(num)
        d = float(den)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(n) and math.isfinite(d)) or d == 0:
        return None
    return n / d


def _full_ladder_blended_entry(setup: dict) -> float | None:
    """Alloc-weighted blended entry over ALL THREE intended entry tiers.

    Uses the SAME ``alloc_pct`` weights the replay engine uses, with the same
    equal-weight fallback when allocs are absent / zero. This is the intended
    full-fill blended entry (independent of which tiers actually filled).
    Returns ``None`` when the setup has no usable entry tiers / prices.
    """
    raw_entries = setup.get("entry_tiers") or []
    prices: list[float] = []
    weights: list[float] = []
    for t in raw_entries:
        try:
            price = float(t["limit"])
            weight = float(t.get("alloc_pct", 0.0))
        except (TypeError, ValueError, KeyError):
            continue
        if not math.isfinite(price):
            continue
        prices.append(price)
        weights.append(weight if math.isfinite(weight) else 0.0)
    if not prices:
        return None
    wsum = sum(weights)
    if wsum > 0:
        return sum(p * w for p, w in zip(prices, weights, strict=True)) / wsum
    return sum(prices) / len(prices)  # equal-weight fallback (allocs absent / zero)


def _safe_finite_float(raw: Any) -> float | None:
    """Coerce ``raw`` to a finite float, or ``None`` (missing / non-numeric / inf/nan)."""
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val if math.isfinite(val) else None


def _signal_time_size(setup: dict) -> dict[str, Any]:
    """Signal-time (intended) size geometry, independent of fills."""
    pct = _safe_finite_float(setup.get("suggested_size_pct"))
    # Stored as a PERCENT (e.g. 4.07 -> 4.07%); normalise to a fraction.
    suggested_gross_weight_pct = pct / 100.0 if pct is not None else None

    full_blended = _full_ladder_blended_entry(setup)
    disaster_stop = _safe_finite_float(setup.get("disaster_stop"))

    stop_distance_pct_full = _safe_div(
        (full_blended - disaster_stop)
        if (full_blended is not None and disaster_stop is not None)
        else None,
        full_blended,
    )
    implied_risk_pct_full = (
        suggested_gross_weight_pct * stop_distance_pct_full
        if (suggested_gross_weight_pct is not None and stop_distance_pct_full is not None)
        else None
    )
    return {
        "suggested_gross_weight_pct": suggested_gross_weight_pct,
        "full_ladder_blended_entry": full_blended,
        "disaster_stop": disaster_stop,
        "stop_distance_pct_full": stop_distance_pct_full,
        "implied_risk_pct_full": implied_risk_pct_full,
    }


def _outcome_time_size(
    outcome: LadderOutcome,
    *,
    realized_r: float | None,
    open_r: float | None,
    suggested_gross_weight_pct: float | None,
    disaster_stop: float | None,
) -> dict[str, Any]:
    """Outcome-time (what actually deployed) size geometry."""
    tiers_filled_count = len(outcome.entries_filled)
    filled_fraction = outcome.filled_fraction
    # realized gross weight = suggested × filled_fraction. NULL when the size is
    # unknown; 0 when the size IS known but nothing filled (zero capital deployed —
    # a real, non-NULL fact); otherwise the product.
    if suggested_gross_weight_pct is None:
        realized_gross_weight_pct: float | None = None
    elif filled_fraction is not None:
        realized_gross_weight_pct = suggested_gross_weight_pct * filled_fraction
    elif tiers_filled_count == 0:
        realized_gross_weight_pct = 0.0
    else:
        realized_gross_weight_pct = None
    # Realized stop distance over the FILLED blended entry (None when nothing filled).
    stop_distance_pct = _safe_div(
        (outcome.blended_entry - disaster_stop)
        if (outcome.blended_entry is not None and disaster_stop is not None)
        else None,
        outcome.blended_entry,
    )
    if tiers_filled_count == 0:
        realized_risk_pct: float | None = 0.0
    elif realized_gross_weight_pct is not None and stop_distance_pct is not None:
        realized_risk_pct = realized_gross_weight_pct * stop_distance_pct
    else:
        realized_risk_pct = None

    realized_return_pct_of_book = (
        realized_r * realized_risk_pct
        if (realized_r is not None and realized_risk_pct is not None)
        else None
    )
    open_return_pct_of_book = (
        open_r * realized_risk_pct
        if (open_r is not None and realized_risk_pct is not None)
        else None
    )
    return {
        "tiers_filled_count": tiers_filled_count,
        "realized_gross_weight_pct": realized_gross_weight_pct,
        "stop_distance_pct": stop_distance_pct,
        "realized_risk_pct": realized_risk_pct,
        "realized_return_pct_of_book": realized_return_pct_of_book,
        "open_return_pct_of_book": open_return_pct_of_book,
    }


def _size_fields(
    setup: dict,
    outcome: LadderOutcome,
    *,
    realized_r: float | None,
    open_r: float | None,
) -> dict[str, Any]:
    """Portfolio-size (two-layer) fields for one candidate row.

    ALL additive: never reads or mutates the R-space edge fields. Every division is
    guarded (a missing size / degenerate geometry yields ``None`` for that field, never
    a crash). ``realized_r`` / ``open_r`` are the monitor's already-split terminal /
    ongoing R values (mutually exclusive). The contribution composition is exact:
    ``P&L%_of_book = R × gross_weight × stop_distance_pct`` (the stop distance is the
    bridge between gross exposure and risk-normalised R).
    """
    signal = _signal_time_size(setup)
    outcome_fields = _outcome_time_size(
        outcome,
        realized_r=realized_r,
        open_r=open_r,
        suggested_gross_weight_pct=signal["suggested_gross_weight_pct"],
        disaster_stop=signal["disaster_stop"],
    )
    # ``disaster_stop`` is an internal bridge value, not an emitted field.
    signal.pop("disaster_stop")
    return {**signal, **outcome_fields}


def _terminal_row(
    brief_date: dt.date,
    ticker: str,
    setup: dict,
    outcome: LadderOutcome,
    cutoffs: tuple[dt.date, dt.date, dt.date, int, int, int, int],
    last_closed_session: dt.date,
    *,
    last_priced_session: dt.date | None = None,
    last_resolved_session: dt.date | None = None,
    reference_close: float | None = None,
    grid_realized_r: dict[str, float | None] | None = None,
    realized_r_full: float | None = None,
    prior_chart_payload: str | None = None,
) -> dict[str, Any]:
    """Build a monitor store row from a replay outcome (terminal or ongoing).

    ``last_priced_session`` / ``last_resolved_session`` / ``reference_close`` are
    the grouped-daily screen columns: the latest session this resolve priced, the
    latest session produced by an actual MINUTE resolve (this IS a resolve, so it
    equals the session priced), and the FROZEN arrival-window VWAP. They are
    stamped on every minute resolve so the cheap path can advance the mark (and the
    fair ordering can rank by recency) without a minute fetch — while
    ``last_resolved_session`` stays pinned to real resolves so R7's
    sessions-since-resolve cadence cannot be defeated by nightly cheap advances.
    """
    (_arrival, entry_expiry_session, _pos_exp, entry_ttl, position_ttl, _e, _p) = cutoffs
    classification = outcome.classification if outcome.status == "OK" else outcome.status
    terminal = (
        outcome.status == "OK" and classification in _TERMINAL_SET
        # An OPEN/PARTIAL_TP_OPEN whose position-expiry already passed will be a
        # TIME_STOP next replay; until then it is ongoing. (Belt-and-suspenders:
        # the engine already forces TIME_STOP once position_expiry_ms is reached.)
    )
    # A NO_FILL is only TERMINAL once the entry-TTL window has fully elapsed
    # (entry_expiry strictly in the past). Until then the dip-buy limits are still
    # live and price could fill them on a later session, so the candidate is "not
    # filled YET" (ongoing) -- freezing it here would permanently miss a later
    # fill and under-count the population's fill rate.
    # ``>`` not ``>=``: the entry cutoff is the entry_expiry session's OPEN
    # (entry_expiry_ms), so once that session has closed no fill is possible and
    # the NO_FILL is final. Only an entry_expiry session STILL in the future
    # (strictly after the last closed session) leaves the limits live.
    if classification == "NO_FILL" and entry_expiry_session > last_closed_session:
        terminal = False
    realized_r = outcome.realized_r if terminal else None
    open_r = None if terminal else outcome.realized_r
    holding_days = _holding_days(outcome, last_closed_session, terminal=terminal)
    row = {
        "brief_date": brief_date,
        "ticker": ticker,
        "plannable": True,
        "nonplannable_reason": None,
        "terminal": bool(terminal),
        "matured_at": last_closed_session if terminal else None,
        "ladder_classification": classification,
        "blended_entry": outcome.blended_entry,
        "realized_r": realized_r,
        "open_r": open_r,
        "mfe": outcome.mfe,
        "mae": outcome.mae,
        "mfe_pct": outcome.mfe_pct,
        "mae_pct": outcome.mae_pct,
        "forward_return": outcome.forward_return,
        "sequence_str": outcome.sequence_str(),
        "ambiguous_bars": outcome.ambiguous_bars,
        "ratchet_realized_r": outcome.ratchet_realized_r,
        "holding_days_elapsed": holding_days,
        "entry_ttl_days": entry_ttl,
        "position_ttl_days": position_ttl,
        # The replay-config stamp uses the TTL ACTUALLY applied to this row.
        "ladder_config_version": ladder_config_version(order_ttl_days=entry_ttl),
        # Alternate-exit-ladder grid (None until a minute resolve computes it).
        "grid_realized_r_json": json.dumps(grid_realized_r) if grid_realized_r else None,
        # Entry-side counterfactual: realized R at the full-fill blended entry.
        "realized_r_full_fill": realized_r_full,
        # Grouped-daily screen columns: the session this resolve priced, the same
        # session as the last ACTUAL minute resolve (this is a resolve), + the
        # FROZEN arrival VWAP (the cheap path's forward_return anchor).
        "last_priced_session": last_priced_session,
        "last_resolved_session": last_resolved_session,
        "reference_close": reference_close,
    }
    # Carry the prior row's last-good /edge chart across this rewrite (None for a
    # brand-new row with nothing to carry). The resolve path rebuilds the row from
    # scratch and would otherwise drop the column written by the separate enrich
    # pass; a deadline-starved enrich pass could then never repopulate it, leaving
    # an ongoing position's chart blank. The enrich pass still UPGRADES the carried
    # payload whenever it runs.
    row[_CHART_PAYLOAD_COLUMN] = prior_chart_payload
    row.update(_size_fields(setup, outcome, realized_r=realized_r, open_r=open_r))
    return row


def _holding_days(
    outcome: LadderOutcome, last_closed_session: dt.date, *, terminal: bool
) -> int | None:
    """Trading sessions the POSITION was actually held: first entry fill → exit.

    The hold is the capital-deployed window, NOT arrival → expiry. The pure engine
    DOES expose the per-bar timestamps via ``outcome.sequence`` (each
    ``LevelCrossing`` carries ``bar_ts_ms``), so the exact first-fill and exit
    sessions are read from there:

    * first fill = the first entry crossing (``level_id`` starts with ``E``);
    * exit = the LAST crossing when terminal (SL / final TP / TIME_STOP); the
      last-closed session while still ongoing.

    Returns ``None`` when no entry ever filled (NO_FILL = no position held), so
    the read-side holding-period distribution excludes never-held rows rather
    than reporting a spurious full-hold span for them.
    """
    from alphalens_pipeline.paper.calendar import trading_days_elapsed

    entry_crossings = [c for c in outcome.sequence if c.level_id.startswith("E")]
    if not entry_crossings:
        return None  # no position ever opened
    first_fill_session = dt.datetime.fromtimestamp(
        entry_crossings[0].bar_ts_ms / 1000, dt.UTC
    ).date()
    if terminal and outcome.sequence:
        exit_session = dt.datetime.fromtimestamp(
            outcome.sequence[-1].bar_ts_ms / 1000, dt.UTC
        ).date()
    else:
        exit_session = last_closed_session
    return max(0, trading_days_elapsed(first_fill_session, exit_session))


def _null_size_fields() -> dict[str, Any]:
    """All size columns set to ``None`` (non-replayed / placeholder rows)."""
    return dict.fromkeys(_SIZE_COLUMNS)


def _nonplannable_row(brief_date: dt.date, ticker: str, reason: str) -> dict[str, Any]:
    """A row for a non-plannable candidate (recorded with a reason, not replayed)."""
    return {
        "brief_date": brief_date,
        "ticker": ticker,
        "plannable": False,
        "nonplannable_reason": reason,
        "terminal": False,
        "matured_at": None,
        "ladder_classification": None,
        "blended_entry": None,
        "realized_r": None,
        "open_r": None,
        "mfe": None,
        "mae": None,
        "mfe_pct": None,
        "mae_pct": None,
        "forward_return": None,
        "sequence_str": None,
        "ambiguous_bars": None,
        "ratchet_realized_r": None,
        "holding_days_elapsed": None,
        "entry_ttl_days": None,
        "position_ttl_days": None,
        # Non-plannable candidates are never replayed, so no geometry produced
        # this row -- leave the stamp empty rather than invent a config.
        "ladder_config_version": None,
        "grid_realized_r_json": None,
        "realized_r_full_fill": None,
        "last_priced_session": None,
        "last_resolved_session": None,
        "reference_close": None,
        **_null_size_fields(),
    }


def _placeholder_row(
    brief_date: dt.date, ticker: str, cutoffs: tuple[dt.date, dt.date, dt.date, int, int, int, int]
) -> dict[str, Any]:
    """A retryable placeholder for a plannable ticker we could not price yet.

    Used for a brand-new ticker whose first night's fetch failed / was deferred —
    it must NOT be dropped (the denominator must stay stable), and it must NOT be
    terminal (so the next sweep retries it).
    """
    entry_ttl, position_ttl = cutoffs[3], cutoffs[4]
    return {
        "brief_date": brief_date,
        "ticker": ticker,
        "plannable": True,
        "nonplannable_reason": None,
        "terminal": False,
        "matured_at": None,
        "ladder_classification": None,
        "blended_entry": None,
        "realized_r": None,
        "open_r": None,
        "mfe": None,
        "mae": None,
        "mfe_pct": None,
        "mae_pct": None,
        "forward_return": None,
        "sequence_str": None,
        "ambiguous_bars": None,
        "ratchet_realized_r": None,
        "holding_days_elapsed": None,
        "entry_ttl_days": entry_ttl,
        "position_ttl_days": position_ttl,
        # The geometry is already known for a plannable candidate; stamp the
        # config that the first successful replay will use.
        "ladder_config_version": ladder_config_version(order_ttl_days=entry_ttl),
        # Grid + entry counterfactual need minute bars; empty until first resolve.
        "grid_realized_r_json": None,
        "realized_r_full_fill": None,
        "last_priced_session": None,
        "last_resolved_session": None,
        "reference_close": None,
        **_null_size_fields(),
    }


def _carry_prior(prior: dict[str, Any]) -> dict[str, Any]:
    """Carry a prior store row forward, back-filling any NEW (size) columns to None.

    An OLD-format parquet predates the size columns; carrying its row verbatim would
    drop those columns from the rewritten frame for a single-row date. Back-filling to
    ``None`` keeps the schema stable (the next successful replay repopulates them).
    """
    carried = dict(prior)
    for col in (
        *_SIZE_COLUMNS,
        *_SCREEN_COLUMNS,
        *_CONFIG_COLUMNS,
        *_GRID_COLUMNS,
        *_ENTRY_CF_COLUMNS,
        *_PROVENANCE_COLUMNS,
    ):
        carried.setdefault(col, None)
    return carried


def _carried_chart(prior: dict[str, Any] | None) -> str | None:
    """The prior row's last-good chart payload to carry across a resolve rewrite.

    Returns the prior ``chart_payload_json`` ONLY when it is a real, non-empty JSON
    string; the on-disk blank surfaces as a pandas float-NaN (NOT ``None``) and an
    enrich-skipped row can hold an empty string — both coerce to ``None`` so a
    rebuilt ongoing row is born either carrying a usable chart or honestly blank,
    never carrying a NaN sentinel.
    """
    if not prior:
        return None
    value = prior.get(_CHART_PAYLOAD_COLUMN)
    if isinstance(value, str) and value.strip():
        return value
    return None


def _stamp_theme(row: dict[str, Any], theme: str | None) -> dict[str, Any]:
    """Stamp the canonical theme SLUG onto a store row (provenance from the brief).

    The theme travels WITH the outcome record rather than being re-joined downstream
    from the (mutable, 6x/day-rebuilt) briefs cache — that join returns NULL whenever
    a candidate has churned out of the latest brief for its date.

    The stored value is always a slug (``slugify_theme``): an EXISTING theme is kept
    (provenance — a frozen row keeps *which* concept it had) but re-slugged to the
    canonical format, so a row stamped before slug-canonicalisation (a spaced theme)
    is normalised in place on its next stamp; an empty row takes the brief's theme.
    ``None`` for an empty result so the read side renders an em dash, not "".
    """
    existing = row.get("theme")
    canonical = slugify_theme(existing) if existing else slugify_theme(theme or "")
    row["theme"] = canonical or None
    return row


def _stamp_scorer_version(row: dict[str, Any], scorer_config_version: str | None) -> dict[str, Any]:
    """Stamp the scorer-config version onto a store row (provenance from the brief).

    Like ``theme``, this value travels WITH the outcome record rather than being
    re-joined downstream from the briefs cache.  An empty/absent brief value is
    stored as ``None`` so the read side renders an em dash (mirrors ``_stamp_theme``).
    """
    row["scorer_config_version"] = scorer_config_version or None
    return row


def _read_existing_store(store_dir: Path, brief_date: dt.date) -> dict[str, dict[str, Any]]:
    """Read the existing per-date store into ``{ticker: row}`` (empty when absent)."""
    path = store_dir / f"{brief_date.isoformat()}.parquet"
    if not path.exists():
        return {}
    try:
        df = pd.read_parquet(path)
    except (OSError, ValueError) as exc:
        logger.warning(
            "population-monitor: bad store parquet for %s — %s; rebuilding.",
            brief_date.isoformat(),
            exc,
        )
        return {}
    return {str(r["ticker"]).upper(): dict(r) for _, r in df.iterrows()}


def _write_store_atomic(store_dir: Path, brief_date: dt.date, rows: list[dict[str, Any]]) -> None:
    """Atomically write the per-date store frame (write *.parquet.tmp then replace)."""
    store_dir.mkdir(parents=True, exist_ok=True)
    path = store_dir / f"{brief_date.isoformat()}.parquet"
    df = pd.DataFrame(rows)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp)
    os.replace(tmp, path)


def replay_population_ladders(
    briefs_dir: Path,
    *,
    end_date: dt.date | None = None,
    lookback_days: int = MONITOR_LOOKBACK_DAYS,
    store_dir: Path | None = None,
    bar_fetch: BarFetch | None = None,
    grouped_fetch: GroupedFetch | None = None,
    now: dt.datetime | None = None,
    exchange: str = DEFAULT_EXCHANGE,
    deadline: _RunDeadline | None = None,
) -> list[PopulationMonitorReport]:
    """Replay every brief candidate's ladder to terminal over the monitor window.

    Sweeps brief dates in ``[end_date - lookback_days, end_date]`` (newest first).
    Each night a CHEAP grouped-daily screen prices every ongoing candidate from the
    daily close (one whole-market fetch per session); only candidates whose daily
    [low, high] touched a ladder level (or a time / split / brand-new / partial
    event fired) get the precise minute resolve. Terminal rows are FROZEN; a
    fetch failure / deferral / implausible move CARRIES the prior row forward (or
    writes a retryable placeholder). The per-date store is written atomically.
    Returns one report per brief date touched (newest first).
    """
    now = now or dt.datetime.now(dt.UTC)
    end = end_date or now.date()
    fetch = bar_fetch or _default_bar_fetch
    grouped = grouped_fetch or _default_grouped_fetch
    store = store_dir or (Path.home() / ".alphalens" / "population_ladders")
    last_closed_session = _last_closed_session(now, exchange)

    # One shared MINUTE budget across the whole run (the cheap daily screen is NOT
    # budget-bounded). The reserved forced sub-budget protects R7 periodic +
    # brand-new establishment from a brief-inflow / crash-night flood.
    budget = _FetchBudget(_MAX_FETCHES_PER_RUN)
    forced_budget = _FetchBudget(_FORCED_RESOLVE_BUDGET)
    reports: list[PopulationMonitorReport] = []
    for offset in range(lookback_days + 1):  # inclusive both ends; newest -> oldest
        if deadline is not None and deadline.should_stop():
            break
        brief_date = end - dt.timedelta(days=offset)
        report = _replay_one_date(
            store,
            briefs_dir,
            brief_date,
            fetch=fetch,
            grouped_fetch=grouped,
            last_closed_session=last_closed_session,
            exchange=exchange,
            budget=budget,
            forced_budget=forced_budget,
            deadline=deadline,
        )
        if report is not None:
            reports.append(report)
    return reports


def _last_closed_session(now: dt.datetime, exchange: str) -> dt.date:
    """The most recent EXCHANGE session strictly before today (Polygon-safe).

    Polygon Basic serves only closed sessions, so the replay horizon must stop at
    the last fully-closed session. ``previous_trading_day`` returns the session
    strictly before its argument, so feeding today's date yields the last closed
    session whether or not today is itself a session.
    """
    from alphalens_pipeline.paper.calendar import previous_trading_day

    return previous_trading_day(now.date(), exchange)


class _FetchBudget:
    """Per-run hard cap on Polygon fetches (tickers beyond it defer to next night)."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self.used = 0

    def take(self) -> bool:
        if self.used >= self._limit:
            return False
        self.used += 1
        return True


def _replay_one_date(
    store_dir: Path,
    briefs_dir: Path,
    brief_date: dt.date,
    *,
    fetch: BarFetch,
    grouped_fetch: GroupedFetch,
    last_closed_session: dt.date,
    exchange: str,
    budget: _FetchBudget,
    forced_budget: _FetchBudget,
    deadline: _RunDeadline | None = None,
) -> PopulationMonitorReport | None:
    """Two-tier screen + resolve every candidate on one brief date. ``None`` when no brief.

    PASS 1 (cheap, NOT budget-bounded): screen every candidate against the
    grouped-daily map; emit a non-plannable / terminal-freeze / cheap-update row
    directly, or queue a minute-resolve.
    PASS 2 (budget-bounded): order the resolve queue (deferred-touch oldest first,
    then brand-new, then least-recently-priced), then resolve under the main +
    reserved forced budgets. Deferred rows carry their prior forward.
    """
    try:
        candidates = load_brief(brief_date, briefs_dir)
    except (FileNotFoundError, ValueError):
        # Missing / unreadable brief: skip the date entirely (no crash, no store
        # write). This is NOT cache-poisoning — we simply do not touch the date.
        logger.info("population-monitor: no brief for %s — skipping.", brief_date.isoformat())
        return None

    existing = _read_existing_store(store_dir, brief_date)
    fetches_before = budget.used + forced_budget.used

    # ---- PASS 0: prefetch grouped-daily once for the union of needed sessions ----
    needed_sessions = _union_new_sessions(
        candidates, brief_date, existing, last_closed_session, exchange
    )
    grouped_by_session = _prefetch_grouped_daily(
        store_dir, needed_sessions, grouped_fetch, exchange
    )

    rows_by_ticker: dict[str, dict[str, Any]] = {}
    order: list[str] = []  # preserve brief order for the output frame
    resolve_queue: list[_ResolveItem] = []
    counts = {"brief": 0, "plannable": 0, "terminal": 0, "ongoing": 0, "carried": 0, "cheap": 0}

    # ---- PASS 1: screen ----
    for c in candidates:
        counts["brief"] += 1
        ticker = c.ticker.upper()
        order.append(ticker)
        outcome = _screen_one(
            c,
            brief_date,
            existing,
            grouped_by_session=grouped_by_session,
            last_closed_session=last_closed_session,
            exchange=exchange,
        )
        if outcome.category != "nonplannable":
            counts["plannable"] += 1
        if outcome.row is not None:
            rows_by_ticker[ticker] = outcome.row
            if outcome.category in counts:
                counts[outcome.category] += 1
        else:
            assert outcome.resolve_item is not None  # row is None ⇒ a resolve is queued
            resolve_queue.append(outcome.resolve_item)

    # ---- PASS 2: ordered, budgeted resolve ----
    deferred_ages = _resolve_queue(
        resolve_queue,
        rows_by_ticker,
        counts,
        store_dir=store_dir,
        fetch=fetch,
        last_closed_session=last_closed_session,
        exchange=exchange,
        budget=budget,
        forced_budget=forced_budget,
        deadline=deadline,
    )

    rows = [rows_by_ticker[t] for t in order]
    _write_store_atomic(store_dir, brief_date, rows)
    return PopulationMonitorReport(
        brief_date=brief_date,
        n_brief=counts["brief"],
        n_plannable=counts["plannable"],
        terminal=counts["terminal"],
        ongoing=counts["ongoing"],
        carried_forward=counts["carried"],
        fetches=(budget.used + forced_budget.used) - fetches_before,
        cheap_updates=counts["cheap"],
        resolve_queue_depth=len(resolve_queue),
        deferred_touches=len(deferred_ages),
        oldest_deferred_touch_age=max(deferred_ages, default=0),
        stopped_for_deadline=counts.get("stopped_for_deadline", 0),
    )


@dataclass(frozen=True)
class _ResolveItem:
    """A candidate queued for a minute resolve (Pass 2)."""

    candidate: CandidateBrief
    brief_date: dt.date
    cutoffs: tuple[dt.date, dt.date, dt.date, int, int, int, int]
    prior: dict[str, Any] | None
    reference_close: float | None  # carried from prior (None ⇒ brand-new ⇒ recompute)
    last_priced_session: dt.date | None
    last_resolved_session: dt.date | None  # last ACTUAL minute resolve (drives R7)
    touched: bool  # a daily level touch drove the resolve (deferred-touch priority)
    forced: bool  # R7 periodic / brand-new establishment (reserved budget)


@dataclass(frozen=True)
class _ScreenOutcome:
    category: (
        str  # nonplannable | terminal | ongoing | cheap | carried | (resolve via resolve_item)
    )
    row: dict[str, Any] | None
    resolve_item: _ResolveItem | None = None


def _union_new_sessions(
    candidates: Sequence[CandidateBrief],
    brief_date: dt.date,
    existing: dict[str, dict[str, Any]],
    last_closed_session: dt.date,
    exchange: str,
) -> list[dt.date]:
    """The UNION of genuinely-new sessions across all candidates on this date.

    A grouped-daily date is fetched at most once per run (the union avoids
    per-candidate re-fetch — the load-bearing throughput win). For each
    cheap-eligible candidate the new window is ``(last_priced_session,
    min(position_expiry, last_closed_session)]`` plus the session immediately
    before the earliest new session (the split screen / prev_close fail-closed
    needs it).
    """
    sessions: set[dt.date] = set()
    for c in candidates:
        sessions.update(
            _candidate_new_sessions(c, brief_date, existing, last_closed_session, exchange)
        )
    return sorted(sessions)


def _candidate_new_sessions(
    c: CandidateBrief,
    brief_date: dt.date,
    existing: dict[str, dict[str, Any]],
    last_closed_session: dt.date,
    exchange: str,
) -> list[dt.date]:
    """New (+ the session before each) grouped-daily dates for ONE candidate; ``[]``
    when the candidate is not cheap-eligible.

    Only CHEAP-ELIGIBLE priors consult the grouped-daily screen. Brand-new
    (no prior / no classification) and PARTIAL_TP_OPEN priors ALWAYS resolve via
    the minute path (predicate items 1+2), so they need no grouped fetch. The
    split screen + prev_close fail-closed also need the session immediately BEFORE
    each new session, so it is included.
    """
    from alphalens_pipeline.paper.calendar import previous_trading_day

    plannable, _ = _is_plannable(c)
    if not plannable or c.trade_setup is None:
        return []
    prior = existing.get(c.ticker.upper())
    if prior is not None and bool(prior.get("terminal")):
        return []  # frozen — no new pricing
    classification = _prior_classification(prior)
    if classification not in ("OPEN", "NO_FILL"):
        return []
    cutoffs = _engine_cutoffs(brief_date, c.trade_setup, exchange)
    arrival_session = cutoffs[0]
    horizon = min(cutoffs[2], last_closed_session)
    if horizon < arrival_session:
        return []
    last_priced = _coerce_session(prior.get("last_priced_session")) if prior else None
    start = last_priced if last_priced is not None else arrival_session
    out: list[dt.date] = []
    for s in _sessions_between(start, horizon, exchange, inclusive_start=last_priced is None):
        out.append(s)
        out.append(previous_trading_day(s, exchange))
    return out


def _coerce_session(value: Any) -> dt.date | None:
    """Coerce a stored session cell (date / datetime / iso str / Timestamp) to a date."""
    return _as_store_date(value)


def _sessions_between(
    start: dt.date, end: dt.date, exchange: str, *, inclusive_start: bool
) -> list[dt.date]:
    """Exchange sessions in ``(start, end]`` (or ``[start, end]`` when inclusive_start)."""
    from alphalens_pipeline.paper.calendar import advance_trading_sessions, trading_days_elapsed

    if end < start:
        return []
    n = trading_days_elapsed(start, end, exchange)
    out: list[dt.date] = []
    if inclusive_start:
        first = advance_trading_sessions(start, 0, exchange)
        if first <= end:
            out.append(first)
    for i in range(1, n + 1):
        s = advance_trading_sessions(start, i, exchange)
        if s > end:
            break
        out.append(s)
    return out


def _grouped_close(grouped: dict[str, dict[str, Any]] | None, ticker: str) -> float | None:
    """Daily close ``c`` for ``ticker`` from a grouped-daily map (None when absent)."""
    if grouped is None:
        return None
    row = grouped.get(ticker.upper())
    if row is None:
        return None
    return _safe_finite_float(row.get("c"))


def _grouped_low_high(
    grouped: dict[str, dict[str, Any]] | None, ticker: str
) -> tuple[float | None, float | None]:
    """Daily ``(low, high)`` for ``ticker`` from a grouped-daily map (None when absent)."""
    if grouped is None:
        return None, None
    row = grouped.get(ticker.upper())
    if row is None:
        return None, None
    return _safe_finite_float(row.get("l")), _safe_finite_float(row.get("h"))


def _unfilled_entry_limits(setup: dict, prior: dict[str, Any] | None) -> list[float]:
    """Entry-tier limit prices that have NOT yet filled, per the prior sequence.

    Reuses :func:`_parse_filled_entry_ids` + :func:`parse_ladder` so the fill set
    + alloc-weighting are byte-identical to the live replay. A brand-new prior
    (None) leaves ALL tiers un-filled.
    """
    from alphalens_pipeline.feedback.ladder_replay import parse_ladder

    ladder = parse_ladder(setup)
    if not ladder.ok:
        return []
    filled_ids = set(_parse_filled_entry_ids(prior.get("sequence_str")) if prior else [])
    return [lvl.price for lvl in ladder.entries if lvl.level_id not in filled_ids]


def _lowest_unhit_tp(setup: dict, prior: dict[str, Any] | None) -> float | None:
    """Lowest TP target NOT yet hit, per the prior sequence (None when all hit / none)."""
    from alphalens_pipeline.feedback.ladder_replay import parse_ladder

    ladder = parse_ladder(setup)
    if not ladder.ok or not ladder.tps:
        return None
    hit = _parse_hit_tp_ids(prior.get("sequence_str")) if prior else set()
    remaining = [t.price for t in ladder.tps if t.level_id not in hit]
    return min(remaining) if remaining else None


def _parse_hit_tp_ids(sequence_str: Any) -> set[str]:
    """TP ids (``TP1`` ...) from a stored ``sequence_str`` (tolerates None / non-str)."""
    if sequence_str is None or not isinstance(sequence_str, str):
        return set()
    out: set[str] = set()
    for token in sequence_str.split("->"):
        tok = token.strip()
        if len(tok) >= 3 and tok.startswith("TP") and tok[2:].isdigit():
            out.add(tok)
    return out


def _lower_touched(level: float, daily_low: float | None) -> bool:
    """A lower level (entry / stop) is touched when ``daily_low <= level*(1+eps)``."""
    return daily_low is not None and daily_low <= level * (1 + _TOUCH_EPS)


def _upper_touched(level: float, daily_high: float | None) -> bool:
    """An upper level (TP) is touched when ``daily_high >= level*(1-eps)``."""
    return daily_high is not None and daily_high >= level * (1 - _TOUCH_EPS)


def _prior_classification(prior: dict[str, Any] | None) -> str | None:
    """Prior ladder classification (None for a brand-new row)."""
    if prior is None:
        return None
    val = prior.get("ladder_classification")
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return None
    return str(val)


@dataclass(frozen=True)
class _ScreenDecision:
    needs_resolve: bool
    touched: bool = False  # a daily level touch drove it (deferred-touch priority)
    forced: bool = False  # R7 periodic / brand-new (reserved budget + cheap-flip precedence)


def _touch_triggered(
    classification: str,
    *,
    daily_low: float | None,
    daily_high: float | None,
    unfilled_entries: list[float],
    disaster_stop: float | None,
    lowest_unhit_tp: float | None,
    entry_window_open: bool,
) -> bool:
    """Screen step 7 — did a daily H/L touch a level that needs a minute resolve?

    NO_FILL watches unfilled entry limits (only while the entry window is open);
    OPEN watches the disaster stop (always), unfilled entries (entry-window-gated)
    and the lowest unhit TP. Any other classification never touch-triggers.
    """
    if classification == "NO_FILL":
        return entry_window_open and any(_lower_touched(lim, daily_low) for lim in unfilled_entries)
    if classification == "OPEN":
        if disaster_stop is not None and _lower_touched(disaster_stop, daily_low):
            return True
        if entry_window_open and any(_lower_touched(lim, daily_low) for lim in unfilled_entries):
            return True
        if lowest_unhit_tp is not None and _upper_touched(lowest_unhit_tp, daily_high):
            return True
    return False


def _screen_session(
    session: dt.date,
    *,
    ticker: str,
    classification: str,
    prior: dict[str, Any],
    grouped_by_session: dict[dt.date, dict[str, dict[str, Any]] | None],
    unfilled_entries: list[float],
    disaster_stop: float | None,
    lowest_unhit_tp: float | None,
    entry_window_open: bool,
    position_expiry_session: dt.date,
    exchange: str,
) -> _ScreenDecision | None:
    """Screen steps 4-8 for one new session; ``None`` means "no trigger, keep going".

    Fail-closed (``touched=True``) on a missing bar / missing-or-zero prev_close /
    split-class day, then the per-state level-touch screen, then position-expiry.
    """
    from alphalens_pipeline.paper.calendar import previous_trading_day

    grouped = grouped_by_session.get(session)
    # 4. missing-bar fail-closed: a NEW session absent from its complete map
    # (halt / illiquid / upstream gap). Never cheap-advance on a missing close.
    if grouped is None or ticker.upper() not in grouped:
        return _ScreenDecision(needs_resolve=True, touched=True)
    c_star = _grouped_close(grouped, ticker)
    if c_star is None:
        return _ScreenDecision(needs_resolve=True, touched=True)

    # 5. missing prev_close fail-closed — the split screen needs it.
    prev_session = previous_trading_day(session, exchange)
    prev_grouped = grouped_by_session.get(prev_session)
    prev_c = _grouped_close(prev_grouped, ticker) if prev_grouped is not None else None
    # Reuse the prior row's last_close as the prev_c when the prev session is
    # not in this run's fetch set (it was priced on an earlier night).
    if prev_c is None:
        prev_c = _safe_finite_float(prior.get("last_close"))
    if prev_c is None or prev_c == 0:
        return _ScreenDecision(needs_resolve=True, touched=True)

    # 6. split-class day.
    if abs(c_star / prev_c - 1) > _SPLIT_SCREEN_THRESHOLD:
        return _ScreenDecision(needs_resolve=True, touched=True)

    # 7. level touch (eps band) per prior state.
    daily_low, daily_high = _grouped_low_high(grouped, ticker)
    if _touch_triggered(
        classification,
        daily_low=daily_low,
        daily_high=daily_high,
        unfilled_entries=unfilled_entries,
        disaster_stop=disaster_stop,
        lowest_unhit_tp=lowest_unhit_tp,
        entry_window_open=entry_window_open,
    ):
        return _ScreenDecision(needs_resolve=True, touched=True)

    # 8. position-expiry (time-stop) session in the new range — one-time resolve.
    if session >= position_expiry_session:
        return _ScreenDecision(needs_resolve=True, touched=True)

    return None


def _screen_decision(
    prior: dict[str, Any] | None,
    ticker: str,
    setup: dict,
    new_sessions: list[dt.date],
    grouped_by_session: dict[dt.date, dict[str, dict[str, Any]] | None],
    cutoffs: tuple[dt.date, dt.date, dt.date, int, int, int, int],
    last_priced_session: dt.date | None,
    last_resolved_session: dt.date | None,
    last_closed_session: dt.date,
    exchange: str,
) -> _ScreenDecision:
    """The ``needs_minute_resolve`` predicate (spec §screen_logic).

    OR of: brand-new / PARTIAL_TP_OPEN / R7 periodic / missing-bar fail-closed /
    missing-prev_close fail-closed / split-class day / level touch (eps band) /
    position-expiry. A periodic forced resolve takes precedence over any cheap
    terminal flip (signalled via ``forced=True``).

    R7 gates on ``last_resolved_session`` (the last ACTUAL minute resolve), NOT
    ``last_priced_session``: the cheap path advances ``last_priced_session`` every
    night, so gating R7 on it would NEVER accumulate K sessions for a
    nightly-cheap-advanced position and the after-hours safety net would be
    defeated. ``last_priced_session`` stays the least-recently-priced fair-ordering
    key only.
    """
    from alphalens_pipeline.paper.calendar import trading_days_elapsed

    classification = _prior_classification(prior)

    # 1. brand-new row — must establish reference_close + initial fills.
    if prior is None or classification is None:
        return _ScreenDecision(needs_resolve=True, forced=True)

    # 2. PARTIAL_TP_OPEN — mark mixes realized tranches + open remainder; resolve.
    if classification == "PARTIAL_TP_OPEN":
        return _ScreenDecision(needs_resolve=True)

    # 3. R7 periodic forced resolve — last ACTUAL minute resolve too far behind
    # last_closed. A None ``last_resolved_session`` is a carried-forward OLD-format
    # row (never resolved under the new schema) → force a resolve. Gating on the
    # RESOLVE session (not the priced session) is what makes R7 fire for a position
    # the cheap path advances every night.
    if last_resolved_session is None or (
        trading_days_elapsed(last_resolved_session, last_closed_session, exchange)
        > _PERIODIC_RESOLVE_SESSIONS
    ):
        return _ScreenDecision(needs_resolve=True, forced=True)

    entry_expiry_session = cutoffs[1]
    position_expiry_session = cutoffs[2]
    unfilled_entries = _unfilled_entry_limits(setup, prior)
    disaster_stop = _safe_finite_float(setup.get("disaster_stop"))
    lowest_unhit_tp = _lowest_unhit_tp(setup, prior)
    entry_window_open = entry_expiry_session > (last_priced_session or last_closed_session)

    for session in new_sessions:
        decision = _screen_session(
            session,
            ticker=ticker,
            classification=classification,
            prior=prior,
            grouped_by_session=grouped_by_session,
            unfilled_entries=unfilled_entries,
            disaster_stop=disaster_stop,
            lowest_unhit_tp=lowest_unhit_tp,
            entry_window_open=entry_window_open,
            position_expiry_session=position_expiry_session,
            exchange=exchange,
        )
        if decision is not None:
            return decision

    return _ScreenDecision(needs_resolve=False)


def _screen_one(
    c: CandidateBrief,
    brief_date: dt.date,
    existing: dict[str, dict[str, Any]],
    *,
    grouped_by_session: dict[dt.date, dict[str, dict[str, Any]] | None],
    last_closed_session: dt.date,
    exchange: str,
) -> _ScreenOutcome:
    """Screen one candidate → a finished row, or a queued minute resolve."""
    ticker = c.ticker.upper()
    theme = c.theme or None
    scorer_version = c.scorer_config_version or None
    plannable, reason = _is_plannable(c)
    if not plannable:
        row = _nonplannable_row(brief_date, ticker, reason or "not plannable")
        return _ScreenOutcome(
            "nonplannable", _stamp_scorer_version(_stamp_theme(row, theme), scorer_version)
        )
    assert c.trade_setup is not None
    prior = existing.get(ticker)

    # FREEZE: a terminal prior row is copied forward verbatim, no screen / resolve.
    if prior is not None and bool(prior.get("terminal")):
        return _ScreenOutcome(
            "terminal",
            _stamp_scorer_version(_stamp_theme(_carry_prior(prior), theme), scorer_version),
        )

    cutoffs = _engine_cutoffs(brief_date, c.trade_setup, exchange)
    arrival_session = cutoffs[0]
    horizon = min(cutoffs[2], last_closed_session)
    last_priced_session = _coerce_session(prior.get("last_priced_session")) if prior else None
    last_resolved_session = _coerce_session(prior.get("last_resolved_session")) if prior else None
    reference_close = _safe_finite_float(prior.get("reference_close")) if prior else None

    if horizon < arrival_session:
        # Arrival not yet closed — carry prior (or retryable placeholder) without
        # consuming budget; nothing new to price.
        return _carry_or_placeholder(c, brief_date, prior, cutoffs, theme, scorer_version)

    start = last_priced_session if last_priced_session is not None else arrival_session
    new_sessions = _sessions_between(
        start, horizon, exchange, inclusive_start=last_priced_session is None
    )

    decision = _screen_decision(
        prior,
        ticker,
        c.trade_setup,
        new_sessions,
        grouped_by_session,
        cutoffs,
        last_priced_session,
        last_resolved_session,
        last_closed_session,
        exchange,
    )
    if decision.needs_resolve:
        item = _ResolveItem(
            candidate=c,
            brief_date=brief_date,
            cutoffs=cutoffs,
            prior=prior,
            reference_close=reference_close,
            last_priced_session=last_priced_session,
            last_resolved_session=last_resolved_session,
            touched=decision.touched,
            forced=decision.forced,
        )
        return _ScreenOutcome("resolve", None, resolve_item=item)

    # CHEAP PATH — advance from the latest new daily close, zero minute fetch.
    # A brand-new (prior is None) row always forces a resolve above, so the cheap
    # path is only reached with a real prior — assert it for the type-checker.
    assert prior is not None
    cheap = _cheap_update_row(
        c.trade_setup,
        prior,
        ticker,
        new_sessions,
        grouped_by_session,
        cutoffs,
        last_closed_session,
        reference_close,
    )
    if cheap is None:
        # Implausible cheap move (split guard) — carry prior verbatim.
        return _ScreenOutcome(
            "carried",
            _stamp_scorer_version(_stamp_theme(_carry_prior(prior), theme), scorer_version),
        )
    row, category = cheap
    return _ScreenOutcome(category, _stamp_scorer_version(_stamp_theme(row, theme), scorer_version))


def _carry_or_placeholder(
    c: CandidateBrief,
    brief_date: dt.date,
    prior: dict[str, Any] | None,
    cutoffs: tuple[dt.date, dt.date, dt.date, int, int, int, int],
    theme: str | None,
    scorer_version: str | None = None,
) -> _ScreenOutcome:
    """Carry the prior row (or a retryable placeholder for a brand-new ticker)."""
    if prior is not None:
        return _ScreenOutcome(
            "carried",
            _stamp_scorer_version(_stamp_theme(_carry_prior(prior), theme), scorer_version),
        )
    row = _placeholder_row(brief_date, c.ticker.upper(), cutoffs)
    return _ScreenOutcome(
        "carried", _stamp_scorer_version(_stamp_theme(row, theme), scorer_version)
    )


def _apply_cheap_open_update(
    row: dict[str, Any],
    *,
    prior: dict[str, Any],
    setup: dict,
    c_star: float,
    last_priced_prev: dt.date | None,
    latest_session: dt.date,
    trading_days_elapsed,
) -> None:
    """Refresh the OPEN-specific marks on a cheap daily advance, in place.

    Extracted from :func:`_cheap_update_row` so that function stays within the
    cognitive-complexity budget. open_r is recomputed from the daily close;
    mfe/mae are widened to contain it (a close is a point ON the path, so the
    stale minute-replay band must at least reach open_r — edge-data audit
    2026-06-18: 30% of OPEN rows trended outside the carried band);
    open_return_pct_of_book moves with open_r via the carried realized_risk_pct.
    """
    blended_entry = _safe_finite_float(prior.get("blended_entry"))
    disaster_stop = _safe_finite_float(setup.get("disaster_stop"))
    open_r = _cheap_open_r(c_star, blended_entry, disaster_stop)
    row["open_r"] = open_r
    if open_r is not None:
        prior_mfe = _safe_finite_float(row.get("mfe"))
        prior_mae = _safe_finite_float(row.get("mae"))
        row["mfe"] = open_r if prior_mfe is None else max(prior_mfe, open_r)
        row["mae"] = open_r if prior_mae is None else min(prior_mae, open_r)
    row["holding_days_elapsed"] = _cheap_holding_days(
        prior, last_priced_prev, latest_session, trading_days_elapsed
    )
    realized_risk_pct = _safe_finite_float(prior.get("realized_risk_pct"))
    row["open_return_pct_of_book"] = (
        open_r * realized_risk_pct
        if (open_r is not None and realized_risk_pct is not None)
        else None
    )


def _cheap_update_row(
    setup: dict,
    prior: dict[str, Any],
    ticker: str,
    new_sessions: list[dt.date],
    grouped_by_session: dict[dt.date, dict[str, dict[str, Any]] | None],
    cutoffs: tuple[dt.date, dt.date, dt.date, int, int, int, int],
    last_closed_session: dt.date,
    reference_close: float | None,
) -> tuple[dict[str, Any], str] | None:
    """Advance a no-touch ongoing row from the latest daily close. ``None`` = carry.

    The screen has already proven (a) no relevant level was touched on any new
    session and (b) no day-over-day jump was split-class — ``_screen_decision``
    checks every consecutive close ratio against ``_SPLIT_SCREEN_THRESHOLD`` and
    routes a split to a minute resolve, so this path is never reached on a split
    day. Hence ``classification`` / fills / blended_entry are INVARIANT here and
    only the mark-to-market moves. (No second split guard lives here: a multi-day
    ``c*/prior.last_close`` ratio would false-trigger on a legitimate compounding
    trend and permanently freeze the mark — the per-day screen is the sole, exact
    split gate.)
    """
    from alphalens_pipeline.paper.calendar import trading_days_elapsed

    latest_session = new_sessions[-1] if new_sessions else None
    c_star = None
    if latest_session is not None:
        c_star = _grouped_close(grouped_by_session.get(latest_session), ticker)
    if c_star is None or latest_session is None:
        return _carry_only(prior)  # no usable new close — carry verbatim

    row = dict(prior)
    # setdefault only BACK-FILLS columns an old-format prior lacked; an existing
    # value (e.g. grid_realized_r_json from the last minute resolve) is kept frozen
    # -- the cheap daily path never recomputes the grid.
    for col in (
        *_SIZE_COLUMNS,
        *_SCREEN_COLUMNS,
        *_CONFIG_COLUMNS,
        *_GRID_COLUMNS,
        *_ENTRY_CF_COLUMNS,
        *_PROVENANCE_COLUMNS,
    ):
        row.setdefault(col, None)
    row["last_close"] = c_star
    row["last_priced_session"] = latest_session
    # Mark-to-market vs the arrival anchor — refreshed for EVERY ongoing state
    # (a NO_FILL tracks where price went after the non-fill; an OPEN marks the
    # position). Uses the STORED frozen reference_close, never grouped ``vw``.
    row["forward_return"] = _cheap_forward_return(c_star, reference_close)

    classification = _prior_classification(prior)
    entry_expiry_session = cutoffs[1]
    last_priced_prev = _coerce_session(prior.get("last_priced_session"))

    # NO_FILL + entry window now closed and no fill touched → terminal NO_FILL.
    if classification == "NO_FILL" and entry_expiry_session <= last_closed_session:
        row["terminal"] = True
        row["matured_at"] = last_closed_session
        return row, "terminal"

    if classification == "OPEN":
        _apply_cheap_open_update(
            row,
            prior=prior,
            setup=setup,
            c_star=c_star,
            last_priced_prev=last_priced_prev,
            latest_session=latest_session,
            trading_days_elapsed=trading_days_elapsed,
        )

    return row, "cheap"


def _carry_only(prior: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Carry the prior row (no new close to advance from)."""
    return _carry_prior(prior), "carried"


def _cheap_open_r(
    c_star: float, blended_entry: float | None, disaster_stop: float | None
) -> float | None:
    """``(c* − blended_entry) / (blended_entry − disaster_stop)`` — EXACT for a
    no-TP OPEN (matches ``_realized_r_with_frac``'s mark-to-last-close branch)."""
    if blended_entry is None or disaster_stop is None:
        return None
    risk = blended_entry - disaster_stop
    if risk == 0:
        return None
    return (c_star - blended_entry) / risk


def _cheap_forward_return(c_star: float, reference_close: float | None) -> float | None:
    """``(c* − reference_close) / reference_close`` from the STORED reference_close."""
    if reference_close is None or reference_close == 0:
        return None
    return (c_star - reference_close) / reference_close


def _cheap_holding_days(
    prior: dict[str, Any],
    last_priced_prev: dt.date | None,
    latest_session: dt.date,
    trading_days_elapsed: Callable[..., int],
) -> int | None:
    """Holding sessions, advanced incrementally from the prior count.

    The hold is first-fill → exit in trading sessions; on a cheap night the
    position is still open, so it grows by the sessions elapsed since the prior
    priced session. ``None`` is preserved (a never-held NO_FILL has no hold).
    """
    prior_hold = prior.get("holding_days_elapsed")
    if prior_hold is None or (isinstance(prior_hold, float) and math.isnan(prior_hold)):
        return None
    if last_priced_prev is None:
        return int(prior_hold)
    return int(prior_hold) + max(0, trading_days_elapsed(last_priced_prev, latest_session))


def _resolve_order_key(item: _ResolveItem) -> tuple:
    """Fair ordering: deferred-touch (oldest last_priced) first, then brand-new,
    then least-recently-priced. A touched-but-deferred OPEN row therefore cannot
    be overtaken indefinitely by fresh brand-new rows.

    Sort tuple (ascending): (priority_class, age_descending_proxy). Class 0 =
    touched (must not starve), 1 = brand-new establishment, 2 = forced/periodic.
    Within a class, the OLDEST last_priced_session sorts first (most stale wins).
    """
    if item.touched and not item.forced:
        cls = 0
    elif item.prior is None:
        cls = 1
    else:
        cls = 2
    # Older last_priced_session => smaller ordinal => sorts first. A None (brand-new)
    # is maximally stale → use date.min so it leads its class.
    lps = item.last_priced_session or dt.date.min
    return (cls, lps)


def _carried_row(item: _ResolveItem) -> dict[str, Any]:
    """Row to persist when an item can't be resolved (budget / fetch / implausible).

    Carries the prior forward so the denominator never shrinks; a brand-new ticker
    with no prior gets a retryable placeholder instead.
    """
    if item.prior is not None:
        return _carry_prior(item.prior)
    return _placeholder_row(item.brief_date, item.candidate.ticker.upper(), item.cutoffs)


def _deferred_age(item: _ResolveItem, last_closed_session: dt.date) -> int | None:
    """Sessions-behind age for a deferred TOUCH item (dead-man input), else None."""
    from alphalens_pipeline.paper.calendar import trading_days_elapsed

    if item.touched and item.last_priced_session is not None:
        return max(0, trading_days_elapsed(item.last_priced_session, last_closed_session))
    return None


def _resolve_queue(
    resolve_queue: list[_ResolveItem],
    rows_by_ticker: dict[str, dict[str, Any]],
    counts: dict[str, int],
    *,
    store_dir: Path,
    fetch: BarFetch,
    last_closed_session: dt.date,
    exchange: str,
    budget: _FetchBudget,
    forced_budget: _FetchBudget,
    deadline: _RunDeadline | None = None,
) -> list[int]:
    """Pass 2 — resolve the queued candidates under the main + reserved budgets.

    Forced (R7 periodic / brand-new) items draw from ``forced_budget``; everyone
    else from the main ``budget``. Items are processed least-recently-priced first
    so a one-night flood drains fairly over a couple of nights rather than starving
    a fixed tail. A budget-exhausted item carries its prior forward (denominator
    never shrinks) and reports its age for the dead-man-switch. Returns the list of
    deferred-touch ages (sessions behind).
    """
    ordered = sorted(resolve_queue, key=_resolve_order_key)
    deferred_ages: list[int] = []

    for item in ordered:
        ticker = item.candidate.ticker.upper()
        theme = item.candidate.theme or None
        scorer_version = item.candidate.scorer_config_version or None
        if deadline is not None and deadline.should_stop():
            rows_by_ticker[ticker] = _stamp_scorer_version(
                _stamp_theme(_carried_row(item), theme), scorer_version
            )
            counts["carried"] += 1
            counts["stopped_for_deadline"] = counts.get("stopped_for_deadline", 0) + 1
            age = _deferred_age(item, last_closed_session)
            if age is not None:
                deferred_ages.append(age)
            continue
        use_budget = forced_budget if item.forced else budget
        assert item.candidate.trade_setup is not None
        result = _replay_candidate(
            store_dir,
            ticker,
            item.candidate.trade_setup,
            item.cutoffs,
            last_closed_session,
            fetch,
            use_budget,
            exchange,
            reference_close_override=item.reference_close,
            deadline=deadline,
        )
        if result is None:
            # Budget exhausted / fetch fail / implausible — carry prior (or a
            # retryable placeholder for a brand-new ticker) and record the age.
            rows_by_ticker[ticker] = _stamp_scorer_version(
                _stamp_theme(_carried_row(item), theme), scorer_version
            )
            counts["carried"] += 1
            age = _deferred_age(item, last_closed_session)
            if age is not None:
                deferred_ages.append(age)
            continue

        row = _terminal_row(
            item.brief_date,
            ticker,
            item.candidate.trade_setup,
            result.outcome,
            item.cutoffs,
            last_closed_session,
            last_priced_session=result.horizon_session,
            last_resolved_session=result.horizon_session,
            reference_close=result.reference_close,
            grid_realized_r=result.grid_realized_r,
            realized_r_full=result.realized_r_full,
            prior_chart_payload=_carried_chart(item.prior),
        )
        rows_by_ticker[ticker] = _stamp_scorer_version(_stamp_theme(row, theme), scorer_version)
        counts["terminal" if row["terminal"] else "ongoing"] += 1
    return deferred_ages


@dataclass(frozen=True)
class _ResolveResult:
    """One minute-resolve's outcome + the screen columns to stamp on the row."""

    outcome: LadderOutcome
    reference_close: float | None
    horizon_session: dt.date
    grid_realized_r: dict[str, float | None] | None = None
    realized_r_full: float | None = None


def _replay_candidate(
    store_dir: Path,
    ticker: str,
    setup: dict,
    cutoffs: tuple[dt.date, dt.date, dt.date, int, int, int, int],
    last_closed_session: dt.date,
    fetch: BarFetch,
    budget: _FetchBudget,
    exchange: str,
    *,
    reference_close_override: float | None = None,
    deadline: _RunDeadline | None = None,
) -> _ResolveResult | None:
    """RTH-only minute fetch + replay one ticker. ``None`` on fetch fail / defer / skip.

    Window: arrival open → ``min(position_expiry_session, last_closed_session)``
    session RTH close (open + :func:`_session_rth_span_min`). The fetched bars are
    filtered to regular trading hours via :func:`_filter_bars_to_rth` BEFORE the
    replay so the minute path agrees with the grouped-daily [low, high] superset
    (no after-hours stop / pre-market entry the daily screen cannot see).

    ``reference_close`` is the arrival opening-window VWAP — computed once at the
    BRAND-NEW resolve and then FROZEN; ``reference_close_override`` (from a prior
    row) is re-used verbatim on every later resolve so the close-to-close
    ``forward_return`` basis never drifts.
    """
    from alphalens_pipeline.data.alt_data.polygon_client import PolygonError

    arrival_session = cutoffs[0]
    position_expiry_session = cutoffs[2]
    entry_expiry_ms = cutoffs[5]
    position_expiry_ms = cutoffs[6]
    arrival_start = session_open_utc(arrival_session, exchange)
    # horizon stops at the earlier of the full hold and the last closed session.
    horizon_session = min(position_expiry_session, last_closed_session)
    if horizon_session < arrival_session:
        return None  # arrival session not yet closed — retry next night
    horizon_end = session_open_utc(horizon_session, exchange) + dt.timedelta(
        minutes=_session_rth_span_min(horizon_session, exchange)
    )

    if not budget.take():
        logger.info("population-monitor: fetch budget exhausted — deferring %s.", ticker)
        return None
    try:
        bars = _extend_bar_cache(
            store_dir, ticker, arrival_session, fetch, arrival_start, horizon_end
        )
    except (PolygonError, ValueError, KeyError, TypeError, OSError) as exc:
        logger.warning(
            "population-monitor: fetch failed for %s — carrying prior (%s).", ticker, exc
        )
        if deadline is not None and isinstance(exc, PolygonError):
            deadline.record_fetch_result(ok=False)
        return None
    if deadline is not None:
        deadline.record_fetch_result(ok=True)
    # RTH-only minute path: drop pre/post-market prints so grouped-daily [low,high]
    # is a true superset (applied to the raw cache record at replay time only).
    bars = _filter_bars_to_rth(bars, arrival_session, horizon_session, exchange)
    if not bars:
        # No bars is treated as RETRYABLE (carry prior), NOT a terminal NO_DATA:
        # a transient Polygon gap must never freeze a poisoned terminal (the
        # cache-poisoning class). The cost is that a genuinely data-less ticker
        # (delisted / no Polygon coverage) stays ONGOING until it ages out of the
        # MONITOR_LOOKBACK_DAYS window — bounded, never unbounded, and surfaced by
        # the report's `carried_forward` count rather than silently dropped.
        logger.warning("population-monitor: no bars for %s — carrying prior.", ticker)
        return None

    if reference_close_override is not None:
        reference_close = reference_close_override
    else:
        arrival_end = arrival_start + dt.timedelta(minutes=ARRIVAL_VWAP_WINDOW_MIN)
        reference_close = _window_vwap(bars, arrival_start, arrival_end)
    outcome = replay_ladder(
        setup,
        bars,
        reference_close=reference_close,
        entry_expiry_ms=entry_expiry_ms,
        position_expiry_ms=position_expiry_ms,
    )
    if _outcome_is_implausible(outcome):
        logger.warning(
            "population-monitor: implausible forward_return %.3f for %s (likely a split; "
            "bars adjusted=false) — carrying prior rather than recording.",
            outcome.forward_return,
            ticker,
        )
        return None
    # Re-replay the SAME bars under the alternate-exit grid (PR-2): zero extra
    # Polygon cost, separates ladder-capture from selection downstream.
    grid_realized_r = replay_ladder_grid(
        setup,
        bars,
        entry_expiry_ms=entry_expiry_ms,
        position_expiry_ms=position_expiry_ms,
    )
    # Entry-side counterfactual (PR-3): realized R at the full-fill blended entry,
    # same exit ladder + bars. Also zero extra Polygon cost.
    realized_r_full = realized_r_full_fill(
        setup,
        bars,
        entry_expiry_ms=entry_expiry_ms,
        position_expiry_ms=position_expiry_ms,
    )
    return _ResolveResult(
        outcome=outcome,
        reference_close=reference_close,
        horizon_session=horizon_session,
        grid_realized_r=grid_realized_r,
        realized_r_full=realized_r_full,
    )


def _parse_filled_entry_ids(sequence_str: Any) -> list[str]:
    """Entry-tier ids (``E1``/``E2``/``E3`` ...) from a stored ``sequence_str``.

    ``sequence_str`` is e.g. ``"E1->E2->TP1->SL"``; the entry crossings are the
    tokens that start with ``E`` followed by a digit. Returns them de-duplicated,
    in first-touch order (an entry fills at most once). Tolerates ``None`` / NaN /
    a non-string (``[]``).
    """
    if sequence_str is None or not isinstance(sequence_str, str):
        return []
    seen: list[str] = []
    for token in sequence_str.split("->"):
        tok = token.strip()
        if len(tok) >= 2 and tok[0] == "E" and tok[1:].isdigit() and tok not in seen:
            seen.append(tok)
    return seen


def _rederive_filled_fraction(setup: dict, filled_entry_ids: list[str]) -> float | None:
    """Re-derive ``filled_fraction`` from the brief tiers + the stored fill ids.

    Reuses the engine's own :func:`parse_ladder` + :func:`_filled_frac` so the
    alloc-weighting (and equal-weight fallback when allocs are absent / zero) is
    byte-identical to what the live replay produced — NO duplicated arithmetic.
    Returns ``None`` when nothing filled (so the size stub mirrors a real
    ``LadderOutcome.filled_fraction`` of ``None`` for an empty fill set) or when
    the ladder cannot be parsed.
    """
    if not filled_entry_ids:
        return None
    from alphalens_pipeline.feedback.ladder_replay import _filled_frac, parse_ladder

    ladder = parse_ladder(setup)
    if not ladder.ok:
        return None
    by_id = {lvl.level_id: lvl for lvl in ladder.entries}
    filled_levels = [by_id[eid] for eid in filled_entry_ids if eid in by_id]
    if not filled_levels:
        return None
    return _filled_frac(ladder, filled_levels)


def _size_fields_from_row(row: dict[str, Any], setup: dict) -> dict[str, Any] | None:
    """Recompute the size overlay for ONE stored terminal/ongoing row.

    Rebuilds the minimal :class:`LadderOutcome` the size math needs
    (``blended_entry`` + ``entries_filled`` + re-derived ``filled_fraction``) from
    the row's own frozen replay columns and the brief's ``entry_tiers``, then runs
    the SAME :func:`_size_fields` code path. The frozen verdict columns
    (``realized_r`` / ``open_r`` / ``classification`` / ``sequence_str`` / ...) are
    READ, never rewritten. Returns the size-column dict, or ``None`` when the size
    is genuinely unknowable (no usable brief geometry) so the caller leaves the row
    NULL rather than fudging.
    """
    blended_entry = _safe_finite_float(row.get("blended_entry"))
    filled_entry_ids = _parse_filled_entry_ids(row.get("sequence_str"))
    filled_fraction = _rederive_filled_fraction(setup, filled_entry_ids)
    outcome_stub = LadderOutcome(
        status="OK",
        entries_filled=tuple(filled_entry_ids),
        blended_entry=blended_entry,
        filled_fraction=filled_fraction,
    )
    realized_r = _finite(row.get("realized_r"))
    open_r = _finite(row.get("open_r"))
    fields = _size_fields(setup, outcome_stub, realized_r=realized_r, open_r=open_r)
    # If the setup carries no usable size at all (no suggested_size AND no parseable
    # geometry), every emitted column is None -> leave the row NULL (never fudge).
    if all(v is None for v in fields.values()):
        return None
    return fields


def _needs_size_enrichment(row: dict[str, Any]) -> bool:
    """A terminal/ongoing row whose size overlay is missing (NULL / NaN).

    The marker is ``realized_gross_weight_pct`` (the canonical size column): a row
    that already carries a non-null value has been size-stamped and is skipped
    (idempotent). Rows that never opened a position (NO_FILL) legitimately carry a
    size of ``0.0`` (a real fact, NOT null) and are therefore also skipped.
    """
    if not bool(row.get("plannable")):
        return False  # non-plannable rows carry NULL size by design
    val = row.get("realized_gross_weight_pct")
    if val is None:
        return True
    try:
        return math.isnan(float(val))
    except (TypeError, ValueError):
        return True


def enrich_store_with_size_fields(
    store_dir: Path | str,
    briefs_dir: Path | str,
    *,
    deadline: _RunDeadline | None = None,
) -> int:
    """Backfill the size overlay onto store rows frozen BEFORE the size feature.

    The monitor freezes terminal rows and carries them forward verbatim, so a row
    that resolved before the size-overlay feature (PR #431) keeps all 10 size
    columns NULL forever — the edge dashboard's "% book" column is empty for those
    matured trades. This post-hoc pass recomputes the size overlay
    DETERMINISTICALLY from the brief (``suggested_size_pct`` + ``entry_tiers`` +
    ``disaster_stop``) and the row's OWN frozen replay outcome
    (``blended_entry`` + ``sequence_str`` -> ``filled_fraction``), reusing the
    exact :func:`_size_fields` code path.

    Constraints (HARD):

    * The frozen verdict columns (``classification`` / ``realized_r`` / ``open_r``
      / ``mfe`` / ``mae`` / ``sequence_str`` / ``forward_return`` / ...) are NEVER
      written — only the 10 ``_SIZE_COLUMNS`` are filled.
    * Idempotent + self-healing: a row already carrying a non-null size is left
      byte-identical; re-running enriches 0 rows.
    * A row stays NULL when its brief is genuinely unavailable or the setup has no
      usable size geometry — never fudged.
    * Resilient: one bad row / missing brief is logged and skipped; the sweep
      never aborts.

    When ``deadline`` is provided and ``deadline.should_stop()`` is True at the
    start of a per-parquet-file iteration, the file loop breaks early. Files left
    unprocessed keep their existing store values and are retried on the next run.

    Returns the count of rows newly populated with a size overlay.
    """
    store = Path(store_dir)
    briefs = Path(briefs_dir)
    if not store.exists():
        return 0

    n_enriched = 0
    for path in sorted(store.glob("*.parquet")):
        if deadline is not None and deadline.should_stop():
            break
        try:
            df = pd.read_parquet(path)
        except (OSError, ValueError) as exc:
            logger.warning("size-enrichment: bad store parquet %s — %s; skipping.", path, exc)
            continue
        enriched, changed = _enrich_one_store_frame(df, briefs)
        n_enriched += enriched
        if changed:
            _write_store_atomic_path(path, df)
    return n_enriched


def _enrich_one_store_frame(df: pd.DataFrame, briefs_dir: Path) -> tuple[int, bool]:
    """Enrich one already-read store frame IN PLACE. ``(rows_enriched, changed)``.

    Briefs are loaded at most once per (brief_date) and re-used across that date's
    rows. A missing / unreadable brief leaves every row on that date untouched.
    """
    enriched = 0
    changed = False
    setups_by_date: dict[dt.date, dict[str, dict] | None] = {}

    for idx in df.index:
        row = {col: df.at[idx, col] for col in df.columns}
        if not _needs_size_enrichment(row):
            continue
        fields = _resolve_size_fields(row, setups_by_date, briefs_dir)
        if fields is None:
            continue
        for col, val in fields.items():
            df.at[idx, col] = val
        enriched += 1
        changed = True
    return enriched, changed


def _resolve_size_fields(
    row: dict[str, Any],
    setups_by_date: dict[dt.date, dict[str, dict] | None],
    briefs_dir: Path,
) -> dict[str, Any] | None:
    """Resolve + recompute the size overlay for one store row, or ``None``.

    ``None`` when the brief date / brief / ticker setup is unavailable, or the
    recompute fails — a single bad row must never abort the sweep. ``setups_by_date``
    is a per-frame cache so each brief date is loaded at most once.
    """
    brief_date = _as_store_date(row.get("brief_date"))
    if brief_date is None:
        return None
    if brief_date not in setups_by_date:
        setups_by_date[brief_date] = _load_setups_for_date(brief_date, briefs_dir)
    setups = setups_by_date[brief_date]
    if not setups:
        return None
    setup = setups.get(str(row.get("ticker")).upper())
    if setup is None:
        return None
    try:
        return _size_fields_from_row(row, setup)
    except Exception:  # one bad row must never abort the sweep
        logger.exception(
            "size-enrichment: failed for %s/%s; leaving NULL",
            brief_date.isoformat(),
            row.get("ticker"),
        )
        return None


def _load_setups_for_date(brief_date: dt.date, briefs_dir: Path) -> dict[str, dict] | None:
    """``{TICKER: trade_setup}`` for one brief date, or ``None`` when unavailable.

    A candidate without a parseable ``trade_setup`` is omitted (its row stays
    NULL). A missing / unreadable brief returns ``None`` so the whole date is left
    untouched.
    """
    try:
        candidates = load_brief(brief_date, briefs_dir)
    except (FileNotFoundError, ValueError) as exc:
        logger.info(
            "size-enrichment: no brief for %s — %s; leaving the date NULL.",
            brief_date.isoformat(),
            exc,
        )
        return None
    except Exception as exc:  # any other brief-load error must NOT abort the whole sweep
        logger.warning(
            "size-enrichment: brief load failed for %s — %s; leaving the date NULL.",
            brief_date.isoformat(),
            exc,
        )
        return None
    return {c.ticker.upper(): c.trade_setup for c in candidates if c.trade_setup is not None}


def _as_store_date(value: Any) -> dt.date | None:
    """Coerce a stored ``brief_date`` cell to a ``datetime.date`` (or ``None``)."""
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


def _write_store_atomic_path(path: Path, df: pd.DataFrame) -> None:
    """Atomically rewrite a store parquet at ``path`` (tmp + replace)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp)
    os.replace(tmp, path)


def _percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile of a non-empty list (``pct`` in [0, 100])."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = pct / 100.0 * (len(ordered) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return ordered[lo]
    frac = rank - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def _finite(value: Any) -> float | None:
    """Coerce to a finite float or ``None`` (drops NaN / inf / non-numeric)."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


class _PopulationAccumulator:
    """Running roll-up state over the monitor parquet store.

    Terminal (realized) and ongoing (open mark-to-market) populations are kept
    SEPARATE — the open mark is NEVER pooled into the realized mean. The size /
    portfolio layer (terminal rows only) is kept ENTIRELY separate from the
    equal-weight edge metric. Reads NO click column — only the monitor's own
    (terminal, realized_r, open_r, holding_days) fields.
    """

    def __init__(self) -> None:
        self.n_brief = 0
        self.n_plannable = 0
        self.realized: list[float] = []
        self.open_marks: list[float] = []
        self.holding_days: list[float] = []
        # Size layer: ``contributions`` sums portfolio P&L as % of book; the
        # risk-weighted mean R weights each trade by the capital-at-risk deployed.
        self.contributions: list[float] = []
        self.realized_risk_pcts: list[float] = []
        self.risk_weighted_r_num = 0.0  # Σ realized_r × realized_risk_pct
        self.risk_weighted_r_den = 0.0  # Σ realized_risk_pct
        self.tiers_filled: list[float] = []

    def add_row(self, row: Any) -> None:
        if not bool(row.get("plannable")):
            return
        self.n_plannable += 1
        if bool(row.get("terminal")):
            self._add_terminal(row)
        else:
            ov = _finite(row.get("open_r"))
            if ov is not None:
                self.open_marks.append(ov)

    def _add_terminal(self, row: Any) -> None:
        # Guard each field independently: .get() tolerates an OLD-format row that
        # lacks the size columns; _finite drops NaN / inf.
        rv = _finite(row.get("realized_r"))
        if rv is not None:
            self.realized.append(rv)
        hd = _finite(row.get("holding_days_elapsed"))
        if hd is not None:
            self.holding_days.append(hd)
        contrib = _finite(row.get("realized_return_pct_of_book"))
        if contrib is not None:
            self.contributions.append(contrib)
        risk = _finite(row.get("realized_risk_pct"))
        if risk is not None:
            self.realized_risk_pcts.append(risk)
            if rv is not None:
                self.risk_weighted_r_num += rv * risk
                self.risk_weighted_r_den += risk
        tfc = _finite(row.get("tiers_filled_count"))
        if tfc is not None:
            self.tiers_filled.append(tfc)

    def to_summary(self) -> dict[str, Any]:
        return {
            # ---- Edge layer (size-free, equal-weight) — the PRIMARY metric.
            "n_brief": self.n_brief,
            "n_plannable": self.n_plannable,
            "realized_n": len(self.realized),
            "realized_mean": _mean(self.realized),
            "open_n": len(self.open_marks),
            "open_mean": _mean(self.open_marks),
            "holding_days_n": len(self.holding_days),
            "holding_days_p50": _percentile(self.holding_days, 50.0),
            "holding_days_p95": _percentile(self.holding_days, 95.0),
            "regime_stratified": False,
            # ---- Size / portfolio layer (additive, NOT the edge) — terminal only.
            "total_realized_contribution_pct_of_book": (
                sum(self.contributions) if self.contributions else None
            ),
            "size_weighted_realized_r": (
                self.risk_weighted_r_num / self.risk_weighted_r_den
                if self.risk_weighted_r_den > 0
                else None
            ),
            "mean_realized_risk_pct": _mean(self.realized_risk_pcts),
            "mean_tiers_filled_count": _mean(self.tiers_filled),
        }


def _mean(values: list[float]) -> float | None:
    """Arithmetic mean of a list, or ``None`` when empty."""
    return (sum(values) / len(values)) if values else None


def summarize_population_ladders(store_dir: Path) -> dict[str, Any]:
    """Read-only roll-up over the monitor parquet store.

    Reports terminal (realized) and ongoing (open mark-to-market) populations
    SEPARATELY — the open mark is NEVER pooled into the realized mean. Two
    denominators are reported: over the plannable population and over the full
    brief population. A holding-period distribution (p50 / p95 + count) sits
    alongside the realized mean.

    Regime stratification (single stratum, by design)
    -------------------------------------------------
    There is NO point-in-time historical date → VIX source available (the only
    VIX is a current cache that is ~96h stale; stamping it onto a historical brief
    date would be look-ahead). So this PR does NOT stratify by regime: it stamps
    no regime, returns a single stratum, and sets ``regime_stratified=False`` so a
    consumer cannot mistake the single number for a regime-conditioned one.
    """
    store = Path(store_dir)
    acc = _PopulationAccumulator()
    if store.exists():
        for path in sorted(store.glob("*.parquet")):
            try:
                df = pd.read_parquet(path)
            except (OSError, ValueError) as exc:
                logger.warning(
                    "population-monitor: bad store parquet %s — %s; skipping.", path, exc
                )
                continue
            acc.n_brief += len(df)
            for _, row in df.iterrows():
                acc.add_row(row)
    return acc.to_summary()


__all__ = [
    "MONITOR_LOOKBACK_DAYS",
    "BarFetch",
    "GroupedFetch",
    "PopulationMonitorReport",
    "enrich_store_with_size_fields",
    "replay_population_ladders",
    "summarize_population_ladders",
]
