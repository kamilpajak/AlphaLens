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
import logging
import math
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pandas as pd

from alphalens_pipeline.feedback.ladder_replay import LadderOutcome, replay_ladder
from alphalens_pipeline.feedback.shadow_return import (
    ARRIVAL_VWAP_WINDOW_MIN,
    IMPLAUSIBLE_RETURN_THRESHOLD,
    _window_vwap,
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

logger = logging.getLogger(__name__)

# A (ticker, window start, window end) → list of Polygon agg bars. Same shape as
# ``shadow_return.BarFetch`` so the production default + test stubs are shared.
BarFetch = Callable[[str, dt.datetime, dt.datetime], Sequence[dict[str, Any]]]

# Monitor-LOCAL lookback in CALENDAR days. The hold is 42 trading sessions
# (≈ 60 calendar days); 75 gives margin so a row can still be re-replayed for a
# night or two after it matures (and after VPS downtime). DELIBERATELY NOT
# ``shadow_return.DEFAULT_LOOKBACK_DAYS`` (14) — that is the 5-session metric's
# window and is far too short for the full-hold replay.
MONITOR_LOOKBACK_DAYS = 75

# Per-night hard cap on Polygon fetches. When exceeded the remaining tickers are
# logged + DEFERRED (their prior rows carry forward) and retried next night —
# never a silent truncation that would drop rows from the population.
_MAX_FETCHES_PER_RUN = 150

# Minutes of the horizon-end session to include so the replay path covers the
# full session (open → close, half-days too). Mirrors ladder_backfill.
_HORIZON_SESSION_SPAN_MIN = 480

# Terminal classifications: the position is fully resolved and will not change on
# a future re-replay. ``BAD_GEOMETRY`` is terminal-degenerate (stop at/above the
# blended entry → realized_r undefined; it never resolves to a real number on a
# later night, so freezing it avoids pointless re-fetches). ``OPEN`` /
# ``PARTIAL_TP_OPEN`` are the ONLY ongoing states.
_TERMINAL_SET = frozenset(
    {"TP_FULL", "SL_HIT", "PARTIAL_TP_THEN_SL", "TIME_STOP", "NO_FILL", "BAD_GEOMETRY"}
)

_BAR_COLUMNS = ("t", "o", "h", "l", "c", "v")


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


def _outcome_is_implausible(outcome: LadderOutcome) -> bool:
    """True when the replay's forward return is a likely unadjusted-split artifact.

    Mirrors shadow_return's guard (Polygon bars are ``adjusted=false``). When True
    the caller skips + carries the prior row forward rather than recording a
    corrupted outcome.
    """
    fr = outcome.forward_return
    return fr is not None and abs(fr) > IMPLAUSIBLE_RETURN_THRESHOLD


def _terminal_row(
    brief_date: dt.date,
    ticker: str,
    outcome: LadderOutcome,
    cutoffs: tuple[dt.date, dt.date, dt.date, int, int, int, int],
    last_closed_session: dt.date,
) -> dict[str, Any]:
    """Build a monitor store row from a replay outcome (terminal or ongoing)."""
    (_arrival, _entry_exp, _pos_exp, entry_ttl, position_ttl, _e, _p) = cutoffs
    classification = outcome.classification if outcome.status == "OK" else outcome.status
    terminal = (
        outcome.status == "OK" and classification in _TERMINAL_SET
        # An OPEN/PARTIAL_TP_OPEN whose position-expiry already passed will be a
        # TIME_STOP next replay; until then it is ongoing. (Belt-and-suspenders:
        # the engine already forces TIME_STOP once position_expiry_ms is reached.)
    )
    realized_r = outcome.realized_r if terminal else None
    open_r = None if terminal else outcome.realized_r
    holding_days = _holding_days(outcome, last_closed_session, terminal=terminal)
    return {
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
    }


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
    }


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
    now: dt.datetime | None = None,
    exchange: str = DEFAULT_EXCHANGE,
) -> list[PopulationMonitorReport]:
    """Replay every brief candidate's ladder to terminal over the monitor window.

    Sweeps brief dates in ``[end_date - lookback_days, end_date]`` (newest first),
    replaying each plannable candidate against its incrementally-cached price path.
    Terminal rows are FROZEN (no re-fetch); ongoing rows are re-replayed; a
    fetch failure / deferral / implausible move CARRIES the prior row forward (or
    writes a retryable placeholder for a brand-new ticker). The per-date store is
    written atomically. Returns one report per brief date touched (newest first).
    """
    now = now or dt.datetime.now(dt.UTC)
    end = end_date or now.date()
    fetch = bar_fetch or _default_bar_fetch
    store = store_dir or (Path.home() / ".alphalens" / "population_ladders")
    last_closed_session = _last_closed_session(now, exchange)

    budget = _FetchBudget(_MAX_FETCHES_PER_RUN)
    reports: list[PopulationMonitorReport] = []
    for offset in range(lookback_days + 1):  # inclusive both ends; newest -> oldest
        brief_date = end - dt.timedelta(days=offset)
        report = _replay_one_date(
            store,
            briefs_dir,
            brief_date,
            fetch=fetch,
            last_closed_session=last_closed_session,
            exchange=exchange,
            budget=budget,
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
    last_closed_session: dt.date,
    exchange: str,
    budget: _FetchBudget,
) -> PopulationMonitorReport | None:
    """Replay + stamp every candidate on one brief date. ``None`` when no brief."""
    try:
        candidates = load_brief(brief_date, briefs_dir)
    except (FileNotFoundError, ValueError):
        # Missing / unreadable brief: skip the date entirely (no crash, no store
        # write). This is NOT cache-poisoning — we simply do not touch the date.
        logger.info("population-monitor: no brief for %s — skipping.", brief_date.isoformat())
        return None

    existing = _read_existing_store(store_dir, brief_date)
    rows: list[dict[str, Any]] = []
    n_brief = 0
    n_plannable = 0
    terminal = 0
    ongoing = 0
    carried = 0
    fetches_before = budget.used

    for c in candidates:
        n_brief += 1
        ticker = c.ticker.upper()
        plannable, reason = _is_plannable(c)
        if not plannable:
            rows.append(_nonplannable_row(brief_date, ticker, reason or "not plannable"))
            continue
        n_plannable += 1
        assert c.trade_setup is not None  # plannable guarantees it
        prior = existing.get(ticker)

        # FREEZE: a terminal prior row is copied forward verbatim, no fetch/replay.
        if prior is not None and bool(prior.get("terminal")):
            rows.append(dict(prior))
            terminal += 1
            continue

        cutoffs = _engine_cutoffs(brief_date, c.trade_setup, exchange)
        outcome = _replay_candidate(
            store_dir, ticker, c.trade_setup, cutoffs, last_closed_session, fetch, budget, exchange
        )
        if outcome is None:
            # Fetch failure / deferral / implausible move: carry the prior row
            # forward verbatim (denominator never shrinks); a brand-new ticker
            # gets a retryable placeholder.
            rows.append(
                dict(prior) if prior is not None else _placeholder_row(brief_date, ticker, cutoffs)
            )
            carried += 1
            continue
        row = _terminal_row(brief_date, ticker, outcome, cutoffs, last_closed_session)
        rows.append(row)
        if row["terminal"]:
            terminal += 1
        else:
            ongoing += 1

    _write_store_atomic(store_dir, brief_date, rows)
    return PopulationMonitorReport(
        brief_date=brief_date,
        n_brief=n_brief,
        n_plannable=n_plannable,
        terminal=terminal,
        ongoing=ongoing,
        carried_forward=carried,
        fetches=budget.used - fetches_before,
    )


def _replay_candidate(
    store_dir: Path,
    ticker: str,
    setup: dict,
    cutoffs: tuple[dt.date, dt.date, dt.date, int, int, int, int],
    last_closed_session: dt.date,
    fetch: BarFetch,
    budget: _FetchBudget,
    exchange: str,
) -> LadderOutcome | None:
    """Fetch (incrementally) + replay one ticker. ``None`` on fetch fail / defer / skip.

    Window: arrival open → ``min(position_expiry_session, last_closed_session)``
    session close (open + ``_HORIZON_SESSION_SPAN_MIN``). ``reference_close`` is
    the arrival opening-window VWAP (same anchor as shadow_return).
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
        minutes=_HORIZON_SESSION_SPAN_MIN
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
        return None
    if not bars:
        # No bars is treated as RETRYABLE (carry prior), NOT a terminal NO_DATA:
        # a transient Polygon gap must never freeze a poisoned terminal (the
        # cache-poisoning class). The cost is that a genuinely data-less ticker
        # (delisted / no Polygon coverage) stays ONGOING until it ages out of the
        # MONITOR_LOOKBACK_DAYS window — bounded, never unbounded, and surfaced by
        # the report's `carried_forward` count rather than silently dropped.
        logger.warning("population-monitor: no bars for %s — carrying prior.", ticker)
        return None

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
    return outcome


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


def summarize_population_ladders(
    store_dir: Path,
    *,
    lookback_days: int = MONITOR_LOOKBACK_DAYS,
    exchange: str = DEFAULT_EXCHANGE,
) -> dict[str, Any]:
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
    consumer cannot mistake the single number for a regime-conditioned one. This
    reads NO click column — only the monitor's own (terminal, realized_r, open_r,
    holding_days) fields.
    """
    store = Path(store_dir)
    realized: list[float] = []
    open_marks: list[float] = []
    holding_days: list[float] = []
    n_brief = 0
    n_plannable = 0

    if store.exists():
        for path in sorted(store.glob("*.parquet")):
            try:
                df = pd.read_parquet(path)
            except (OSError, ValueError) as exc:
                logger.warning(
                    "population-monitor: bad store parquet %s — %s; skipping.", path, exc
                )
                continue
            n_brief += len(df)
            for _, row in df.iterrows():
                if not bool(row.get("plannable")):
                    continue
                n_plannable += 1
                if bool(row.get("terminal")):
                    rv = _finite(row.get("realized_r"))
                    if rv is not None:
                        realized.append(rv)
                    hd = _finite(row.get("holding_days_elapsed"))
                    if hd is not None:
                        holding_days.append(hd)
                else:
                    ov = _finite(row.get("open_r"))
                    if ov is not None:
                        open_marks.append(ov)

    return {
        "n_brief": n_brief,
        "n_plannable": n_plannable,
        "realized_n": len(realized),
        "realized_mean": (sum(realized) / len(realized)) if realized else None,
        "open_n": len(open_marks),
        "open_mean": (sum(open_marks) / len(open_marks)) if open_marks else None,
        "holding_days_n": len(holding_days),
        "holding_days_p50": _percentile(holding_days, 50.0),
        "holding_days_p95": _percentile(holding_days, 95.0),
        "regime_stratified": False,
    }


__all__ = [
    "MONITOR_LOOKBACK_DAYS",
    "BarFetch",
    "PopulationMonitorReport",
    "replay_population_ladders",
    "summarize_population_ladders",
]
