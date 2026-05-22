"""PEAD x earnings-quality main scorer.

Composes the building blocks (announcements, event windows, single-active-
window invariant, trailing-90d cohort quantiles, sector filter, Day-1
sign confirmation) into a daily long-only portfolio:

  1. Find every event window active on ``asof`` (entry_day <= asof <= exit_day)
  2. Apply single-active-window invariant per ticker
  3. Build the trailing-90d cohort: every announcement whose market_day
     fell in [asof - quantile_cohort_window_days, asof]
  4. Compute SUE top-quintile and accruals below-median thresholds from
     the cohort
  5. Drop sector-excluded tickers (Financials/Utilities)
  6. Drop tickers below SUE threshold or above accruals threshold
  7. Drop tickers whose Day-1 reaction sign disagrees with SUE sign
  8. Return DataFrame[ticker, score] where score = SUE value (drives
     ranking; downstream weighting is equal-weight per pre-reg)
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import date, timedelta

import numpy as np
import pandas as pd

from alphalens_research.screeners.event_drift.announcement_dates import EarningsAnnouncement
from alphalens_research.screeners.event_drift.day1_filter import day1_sign_confirmed
from alphalens_research.screeners.event_drift.event_window import (
    EventWindow,
    apply_single_active_window,
    build_event_windows,
    windows_active_on,
)
from alphalens_research.screeners.event_drift.sector_filter import SectorFilter
from alphalens_research.screeners.event_drift.t0_timing import TradingCalendar


def _collect_event_windows(
    *,
    universe: Sequence[str],
    sector_filter: SectorFilter,
    announcement_lookup: Callable[[str], Iterable[EarningsAnnouncement]],
    sue_lookup: Callable[[str, date], float | None],
    accruals_lookup: Callable[[str, date], float | None],
    calendar: TradingCalendar,
    skip_days: int,
    exit_days: int,
) -> list[EventWindow]:
    all_windows: list[EventWindow] = []
    for ticker in universe:
        if sector_filter.is_excluded(ticker):
            continue
        anns = list(announcement_lookup(ticker))
        if not anns:
            continue
        all_windows.extend(
            build_event_windows(
                anns,
                sue_lookup=sue_lookup,
                accruals_lookup=accruals_lookup,
                calendar=calendar,
                skip_days=skip_days,
                exit_days=exit_days,
            )
        )
    return all_windows


def _compute_cohort_thresholds(
    *,
    deduped: list[EventWindow],
    asof: date,
    quantile_cohort_window_days: int,
    sue_quantile_top_pct: float,
    accrual_quantile_bottom_pct: float,
) -> tuple[float, float]:
    """Trailing-90d cohort quantile thresholds. Sparse cohort → pass-through."""
    cohort_lookback = asof - timedelta(days=quantile_cohort_window_days)
    cohort = [w for w in deduped if cohort_lookback <= w.market_day <= asof]
    if len(cohort) < 2:
        return -float("inf"), float("inf")
    sue_values = np.asarray([w.sue for w in cohort], dtype=float)
    accruals_values = np.asarray([w.accruals_ratio for w in cohort], dtype=float)
    return (
        float(np.percentile(sue_values, 100.0 - sue_quantile_top_pct)),
        float(np.percentile(accruals_values, accrual_quantile_bottom_pct)),
    )


def score_pead_quality(
    *,
    asof: date,
    universe: Sequence[str],
    sue_lookup: Callable[[str, date], float | None],
    accruals_lookup: Callable[[str, date], float | None],
    announcement_lookup: Callable[[str], Iterable[EarningsAnnouncement]],
    day1_return_lookup: Callable[[str, date], float | None],
    sector_filter: SectorFilter,
    calendar: TradingCalendar,
    sue_quantile_top_pct: float = 20.0,
    accrual_quantile_bottom_pct: float = 50.0,
    quantile_cohort_window_days: int = 90,
    skip_days: int = 2,
    exit_days: int = 60,
) -> pd.DataFrame:
    """Score the universe at ``asof`` and return DataFrame[ticker, score]."""
    if not universe:
        return pd.DataFrame(columns=["ticker", "score"])

    all_windows = _collect_event_windows(
        universe=universe,
        sector_filter=sector_filter,
        announcement_lookup=announcement_lookup,
        sue_lookup=sue_lookup,
        accruals_lookup=accruals_lookup,
        calendar=calendar,
        skip_days=skip_days,
        exit_days=exit_days,
    )
    if not all_windows:
        return pd.DataFrame(columns=["ticker", "score"])

    deduped = apply_single_active_window(all_windows)
    active = windows_active_on(deduped, asof)
    if not active:
        return pd.DataFrame(columns=["ticker", "score"])

    sue_threshold, accruals_threshold = _compute_cohort_thresholds(
        deduped=deduped,
        asof=asof,
        quantile_cohort_window_days=quantile_cohort_window_days,
        sue_quantile_top_pct=sue_quantile_top_pct,
        accrual_quantile_bottom_pct=accrual_quantile_bottom_pct,
    )

    rows: list[dict] = []
    for w in active:
        if w.sue < sue_threshold or w.accruals_ratio > accruals_threshold:
            continue
        d1_ret = day1_return_lookup(w.ticker, w.market_day)
        if not day1_sign_confirmed(sue=w.sue, day1_return=d1_ret):
            continue
        rows.append({"ticker": w.ticker, "score": w.sue})

    if not rows:
        return pd.DataFrame(columns=["ticker", "score"])
    return pd.DataFrame(rows).sort_values("score", ascending=False, ignore_index=True)
