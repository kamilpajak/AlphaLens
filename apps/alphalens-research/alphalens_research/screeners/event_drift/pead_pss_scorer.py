"""PEAD daily-rebalance adapter — paradigm-14 PEAD v2 B2.

Translates ``AVEarningsAnnouncement`` events (top-quintile selection from
B1's ``pss_rank``) into daily ticker-weight DataFrames consumable by the
backtest engine. Implements the pre-registered execution semantics:

  - Pre-market report on trading day ``t`` → entry at ``close(t)``. The
    position has NO exposure during day ``t`` (it was opened at the close).
    Weights become 1/n_fixed starting day ``t+1`` and run for ``hold_days``
    trading days. Last weighted day is ``t + hold_days`` (whose close is
    the exit price).
  - Post-market report on trading day ``t`` → entry at ``close(t+1)``. The
    position has zero weight on days ``t`` and ``t+1``. Weights become
    1/n_fixed starting day ``t+2``, running ``hold_days`` days. Last
    weighted day is ``t + 1 + hold_days``.
  - α2 sub-leveraged weighting per audit memo
    ``docs/research/paradigm14_pead_cost_model_audit_2026_05_14.md`` §5:
    each active position carries weight ``1 / n_fixed`` (default 150).
    Total gross varies in ``[0, 1]`` — never forced rebalancing.

Convention: ``weights[t]`` represents the position held DURING day ``t``'s
session, capturing ``returns[t] = close(t) / close(t-1) - 1``. A position
opened at close(t) has no daytime exposure during t, so weight[t]=0.

Reports landing on a non-trading day (rare) roll forward to the next
trading day. Trading ``calendar`` is the source of truth — callers must
supply trading dates spanning the backtest window.
"""

from __future__ import annotations

import bisect
import logging
from collections.abc import Iterable
from datetime import date

import pandas as pd

from alphalens_research.screeners.event_drift.av_earnings_ingestion import AVEarningsAnnouncement

logger = logging.getLogger(__name__)


def compute_entry_day(event: AVEarningsAnnouncement, calendar: list[date]) -> date:
    """Trading day on whose close the position is OPENED.

    Pre-market: first trading day on or after ``reported_date``.
    Post-market: first trading day strictly after ``reported_date``.

    The position acquires non-zero weight only on the trading day AFTER
    ``compute_entry_day`` (see ``build_daily_weights``), since entry happens
    at the close.

    Raises ``ValueError`` if no eligible trading day exists in calendar.
    """
    rd = event.reported_date
    # bisect on a pre-sorted calendar — O(log n) per event. The pre-market
    # case is "first d >= rd" → bisect_left. Post-market is "first d > rd"
    # → bisect_right.
    if event.report_time == "pre-market":
        idx = bisect.bisect_left(calendar, rd)
    else:  # post-market
        idx = bisect.bisect_right(calendar, rd)
    if idx >= len(calendar):
        raise ValueError(f"No trading day in calendar after {rd} for {event.report_time} entry")
    return calendar[idx]


def compute_exit_day(*, entry_day: date, calendar: list[date], hold_days: int) -> date:
    """Trading day on whose close the position is EXITED — also the last
    day the position carries non-zero weight.

    The position is held for ``hold_days`` trading days of P&L exposure
    starting the day AFTER ``entry_day``. Exit close = ``entry_day +
    hold_days`` trading days.

    Raises ``ValueError`` if the entry+hold window extends past calendar.
    """
    try:
        idx = calendar.index(entry_day)
    except ValueError as exc:
        raise ValueError(f"entry_day {entry_day} not in calendar") from exc
    exit_idx = idx + hold_days
    if exit_idx >= len(calendar):
        raise ValueError(
            f"entry_day {entry_day} + hold_days {hold_days} extends past calendar "
            f"(len={len(calendar)}, last={calendar[-1]})"
        )
    return calendar[exit_idx]


def build_daily_weights(
    *,
    events: Iterable[AVEarningsAnnouncement],
    calendar: list[date],
    n_fixed: int = 150,
    hold_days: int = 20,
) -> pd.DataFrame:
    """Daily ticker-weight DataFrame for the engine's portfolio_returns path.

    Returned frame is indexed by ``calendar`` (one row per trading day) with
    one column per ticker that has at least one active day. Each active
    cell carries ``1 / n_fixed``; inactive cells are 0.0.

    Active window for an event: ``(entry_day, exit_day]`` — strict left,
    closed right. That gives ``hold_days`` trading days of weight, capturing
    ``hold_days`` close-to-close returns starting ``returns[entry_day + 1
    trading day]``.

    Empty ``events`` yields an empty-column DataFrame indexed by calendar
    (zero gross every day). Sub-leverage by design.
    """
    # Pre-allocate full zero matrix indexed by calendar; fill active cells.
    materialised_events = list(events)
    tickers = sorted({e.ticker for e in materialised_events})
    df = pd.DataFrame(
        0.0,
        index=pd.Index(calendar, name="date"),
        columns=tickers,
    )

    weight = 1.0 / float(n_fixed)
    for event in materialised_events:
        entry = compute_entry_day(event, calendar)
        exit_d = compute_exit_day(entry_day=entry, calendar=calendar, hold_days=hold_days)
        entry_idx = calendar.index(entry)
        exit_idx = calendar.index(exit_d)
        # Active days: strict-left open, closed right → (entry_idx, exit_idx].
        # Vectorise the slice assignment to avoid per-cell pandas overhead.
        active_dates = calendar[entry_idx + 1 : exit_idx + 1]
        df.loc[active_dates, event.ticker] += weight

    # Enforce α2 sub-leverage contract: weight per ticker per day must NEVER
    # exceed 1/n_fixed. Concurrent same-ticker events (rare: 10-Q/A restatement
    # within 20-day hold, or merger event) accumulate via += above; clipping
    # caps the doubling while extending the hold window to the union. This
    # is the conservative interpretation — two positive signals on the same
    # ticker do not justify 2× leverage under sub-leveraged α2.
    return df.clip(upper=weight)


def portfolio_returns_from_weights(
    weights: pd.DataFrame,
    returns: pd.DataFrame,
) -> pd.Series:
    """Element-wise weight × return, summed across tickers per row.

    ``portfolio_return[t] = sum_i weights[t, i] * returns[t, i]``. Mismatched
    columns are right-aligned (missing tickers default to zero contribution
    rather than NaN). Both inputs must share the same trading-day index.

    Logs a WARNING if any weighted ticker is missing from ``returns`` — a
    silent zero-fill would otherwise mask data-coverage bugs (e.g. an
    active position whose price feed is offline appears to contribute
    nothing to P&L, NOT the intended "drop the position" behaviour).
    """
    missing = [t for t in weights.columns if t not in returns.columns]
    if missing:
        logger.warning(
            "portfolio_returns_from_weights: %d weighted ticker(s) missing "
            "from returns — silently filled with 0. Tickers: %s",
            len(missing),
            sorted(missing),
        )
    aligned_returns = returns.reindex(columns=weights.columns).fillna(0.0)
    return (weights * aligned_returns).sum(axis=1)
