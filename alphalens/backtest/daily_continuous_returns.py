"""Daily-cadence continuous-holding portfolio return reconstruction.

The :class:`~alphalens.backtest.engine.BacktestEngine` emits one
``portfolio_return`` per rebalance — a single 1-day forward return — which is
correct for Sharpe annualization (``periods_per_year = 252 / stride``) but
**not** for a Carhart 4-factor regression with HAC standard errors keyed to a
multi-month autocorrelation horizon. With ``rebalance_stride=21`` (monthly)
and ``hac_maxlags=126`` (six-month signal memory per pre-reg), the regression
input must be at daily cadence with ~1500 trading-day observations over a
6-year OOS window — otherwise statsmodels' Bartlett kernel (``w(j) = 1 -
j/(L+1)``) silently degenerates to ~uniform weights when the requested lag
length exceeds the sample length, producing biased standard errors.

This module reconstructs the daily series under a continuous-holding,
equal-weight-rebalanced-each-day convention:

- A rebalance on day ``d_k`` selects a basket; the basket starts being held
  at the next trading day (matching the engine's one-day execution lag).
- Between rebalance ``d_k`` and ``d_{k+1}`` the daily portfolio return on
  trading day ``t`` (``d_k < t <= d_{k+1}``) is the equal-weight average of
  ``close(t) / close(t-1) - 1`` over the d_k basket.
- After the final rebalance, the basket is held until ``end_date`` (or the
  last trading day in the calendar ticker's history).

Tickers absent from the history store on a given day are skipped in the
average for that day (the basket loses a name but continues equal-weight on
remaining names) — matches the engine's tolerance for thin-coverage names.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

import pandas as pd

from alphalens.backtest.engine import RebalanceSnapshot
from alphalens.data.store.history import HistoryStore


def daily_continuous_returns(
    rebalance_results: Iterable[RebalanceSnapshot],
    history_store: HistoryStore,
    *,
    calendar_ticker: str | None = None,
    end_date: date | None = None,
) -> pd.Series:
    """Reconstruct daily continuous-holding portfolio returns.

    Parameters
    ----------
    rebalance_results
        Iterable of :class:`RebalanceSnapshot` from a completed backtest.
    history_store
        :class:`HistoryStore` with at least one bar per ticker referenced by
        any basket.
    calendar_ticker
        Trading-day calendar source. Defaults to the first ticker in the
        first non-empty basket. Pass an authoritative benchmark (e.g. SPY)
        for a cleaner calendar across long horizons.
    end_date
        Truncate the daily series at this calendar date (inclusive). When
        ``None`` the series extends to the last trading day in the calendar
        ticker's history.

    Returns
    -------
    pd.Series
        Daily returns indexed by ``DatetimeIndex`` of trading days strictly
        after the first rebalance. Empty when ``rebalance_results`` is empty
        or no calendar can be derived.
    """
    rebalances = sorted(
        (r for r in rebalance_results if r.top_n_tickers),
        key=lambda r: r.date,
    )
    if not rebalances:
        return pd.Series(dtype=float, name="portfolio_daily")

    calendar_src = calendar_ticker or rebalances[0].top_n_tickers[0]
    try:
        cal_df = history_store.full(calendar_src)
    except KeyError:
        return pd.Series(dtype=float, name="portfolio_daily")

    cal_idx = cal_df.index
    first_reb_ts = rebalances[0].date
    end_ts = pd.Timestamp(end_date) if end_date is not None else cal_idx[-1]
    held_days = cal_idx[(cal_idx > first_reb_ts) & (cal_idx <= end_ts)]
    if held_days.empty:
        return pd.Series(dtype=float, name="portfolio_daily")

    # For each held trading day, find the active basket = most recent rebalance
    # strictly before that day. The two pointers walk in lockstep — O(N + R).
    rebalance_dates = [r.date for r in rebalances]
    basket_for_day: list[list[str]] = []
    r_idx = -1
    for d in held_days:
        while r_idx + 1 < len(rebalances) and rebalance_dates[r_idx + 1] < d:
            r_idx += 1
        basket_for_day.append(rebalances[r_idx].top_n_tickers)

    # Pre-fetch close-price reindex per ticker for speed: each ticker hit on
    # average ~21 days (one full holding cycle), so cache miss is dominant
    # cost. Reindex to held_days once per ticker.
    needed_tickers = {t for basket in basket_for_day for t in basket}
    closes_by_ticker: dict[str, pd.Series] = {}
    prev_closes_by_ticker: dict[str, pd.Series] = {}
    for tkr in needed_tickers:
        try:
            full = history_store.full(tkr)
        except KeyError:
            continue
        # Reindex to held_days; use prev-day close from the ticker's own index
        # (NOT held_days) to compute return — this preserves correctness when
        # the ticker is missing one of the held_days but had a prior bar.
        closes = full["close"].reindex(held_days)
        prev_closes = full["close"].shift(1).reindex(held_days)
        closes_by_ticker[tkr] = closes
        prev_closes_by_ticker[tkr] = prev_closes

    daily_returns: list[float] = []
    for i in range(len(held_days)):
        basket = basket_for_day[i]
        rets: list[float] = []
        for tkr in basket:
            c = closes_by_ticker.get(tkr)
            p = prev_closes_by_ticker.get(tkr)
            if c is None or p is None:
                continue
            cv = c.iloc[i]
            pv = p.iloc[i]
            if pd.isna(cv) or pd.isna(pv) or pv <= 0:
                continue
            rets.append(float(cv) / float(pv) - 1.0)
        if rets:
            daily_returns.append(sum(rets) / len(rets))
        else:
            daily_returns.append(float("nan"))

    return pd.Series(
        daily_returns,
        index=held_days,
        name="portfolio_daily",
    ).dropna()
