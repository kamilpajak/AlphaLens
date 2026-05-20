"""Top-decile EW long-only portfolio construction primitives.

Used by precheck/verification scripts that need to construct a portfolio
from raw scorer outputs without going through the full BacktestEngine
pipeline. Lighter-weight alternative to `daily_continuous_returns` —
takes a long-format scores panel directly instead of `RebalanceSnapshot`
list.

API:
- ``monthly_asof_calendar(start, end, day_of_month=21)`` — generate
  monthly rebalance asofs, skipping weekends forward.
- ``top_decile_portfolio_daily_returns(scores, histories, asofs, top_decile_pct=0.10)``
  — for each asof, pick top-decile of non-NaN non-zero scored tickers,
  hold equally-weighted until next asof; return concatenated daily
  return Series.

Critical edge cases (TDD-covered in tests/test_top_decile_portfolio.py):
- NaN scores excluded from selection
- Zero scores excluded (distress_credit zero-out pattern)
- Tickers missing from histories silently skipped
- Holding period bounded by next asof (not infinite)
- Asof timestamp normalization (handles both midnight and time-of-day)
- Empty inputs return empty Series, no crash
"""

from __future__ import annotations

from datetime import date

import pandas as pd

_DEFAULT_REBALANCE_TAIL_DAYS = 21


def monthly_asof_calendar(
    start: date,
    end: date,
    *,
    day_of_month: int = 21,
) -> list[pd.Timestamp]:
    """Generate monthly asof timestamps in [start, end], skipping weekends forward.

    For each month in the range, schedule asof at ``day_of_month``. If that day
    is a weekend, advance to the next weekday. Asofs outside [start, end] are
    dropped.

    Parameters
    ----------
    start, end
        Inclusive date range.
    day_of_month
        Target day-of-month for the asof (default 21 — matches insider_form4
        21d stride approximation).
    """
    asofs: list[pd.Timestamp] = []
    cur = pd.Timestamp(start.year, start.month, day_of_month)
    while cur.date() <= end:
        # Skip weekends forward
        adj = cur
        while adj.weekday() > 4:
            adj += pd.Timedelta(days=1)
        if start <= adj.date() <= end:
            asofs.append(adj)
        # Next month
        if cur.month == 12:
            cur = pd.Timestamp(cur.year + 1, 1, day_of_month)
        else:
            cur = pd.Timestamp(cur.year, cur.month + 1, day_of_month)
    return asofs


def _select_held_tickers(
    *,
    scores: pd.DataFrame,
    asof: pd.Timestamp,
    top_decile_pct: float,
    available: dict[str, pd.Series],
) -> list[str]:
    asof_norm = asof.normalize()
    scores_at_asof = scores[scores["asof"] == asof_norm]
    if scores_at_asof.empty:
        return []
    valid = scores_at_asof.dropna(subset=["score"])
    # Drop zero-score rows (distress_credit zeroes non-bottom-quintile)
    valid = valid[valid["score"] != 0]
    if valid.empty:
        return []
    n_decile = max(1, int(len(valid) * top_decile_pct))
    top_tickers = valid.nlargest(n_decile, "score")["ticker"].tolist()
    return [t for t in top_tickers if t in available]


def _period_returns_for(
    *,
    held_tickers: list[str],
    daily_returns_per_ticker: dict[str, pd.Series],
    asof: pd.Timestamp,
    end_idx: pd.Timestamp,
) -> pd.Series | None:
    held_returns = pd.DataFrame({t: daily_returns_per_ticker[t] for t in held_tickers})
    period_mask = (held_returns.index > asof) & (held_returns.index <= end_idx)
    period_returns = held_returns.loc[period_mask]
    if period_returns.empty:
        return None
    return period_returns.mean(axis=1, skipna=True)


def top_decile_portfolio_daily_returns(
    scores: pd.DataFrame,
    histories: dict[str, pd.DataFrame],
    asofs: list[pd.Timestamp],
    *,
    top_decile_pct: float = 0.10,
    tail_days: int = _DEFAULT_REBALANCE_TAIL_DAYS,
) -> pd.Series:
    """Daily continuous returns of top-decile EW long-only portfolio.

    For each asof t, pick the top ``ceil(N × top_decile_pct)`` tickers by
    score (excluding NaN and zero), hold equally-weighted until t+1's asof
    (or t + ``tail_days`` for the last asof). Daily portfolio return =
    arithmetic mean of held tickers' daily returns (skipna).

    Parameters
    ----------
    scores
        Long-format DataFrame with columns ``asof``, ``ticker``, ``score``.
    histories
        Mapping ticker → DataFrame with ``close`` column, indexed by date.
    asofs
        Sorted list of rebalance asof timestamps.
    top_decile_pct
        Fraction of non-NaN non-zero scored tickers to hold per asof
        (default 0.10 = top decile).
    tail_days
        Holding window for the final asof when no next-asof exists.
    """
    if not asofs or scores.empty:
        return pd.Series(dtype=float, name="portfolio_daily")

    # Pre-compute daily returns per ticker (cached)
    daily_returns_per_ticker: dict[str, pd.Series] = {}
    for ticker, hist in histories.items():
        if hist is None or hist.empty or "close" not in hist.columns:
            continue
        daily_returns_per_ticker[ticker] = hist["close"].pct_change()

    # Normalize asof column for robust matching (drop time-of-day component)
    scores = scores.copy()
    scores["asof"] = pd.to_datetime(scores["asof"]).dt.normalize()

    sorted_asofs = sorted(asofs)
    portfolio_returns_chunks: list[pd.Series] = []

    for i, asof in enumerate(sorted_asofs):
        held_tickers = _select_held_tickers(
            scores=scores,
            asof=asof,
            top_decile_pct=top_decile_pct,
            available=daily_returns_per_ticker,
        )
        if not held_tickers:
            continue

        # tail_days is in TRADING days (default 21 = ~monthly stride). Use BDay
        # offset, NOT calendar days, to avoid systematic under-sampling of the
        # final period (per zen 2026-05-10 code review).
        next_asof = sorted_asofs[i + 1] if i + 1 < len(sorted_asofs) else None
        end_idx = next_asof if next_asof is not None else asof + pd.offsets.BDay(tail_days)

        period_returns = _period_returns_for(
            held_tickers=held_tickers,
            daily_returns_per_ticker=daily_returns_per_ticker,
            asof=asof,
            end_idx=end_idx,
        )
        if period_returns is not None:
            portfolio_returns_chunks.append(period_returns)

    if not portfolio_returns_chunks:
        return pd.Series(dtype=float, name="portfolio_daily")

    series = pd.concat(portfolio_returns_chunks).sort_index()
    series = series[~series.index.duplicated(keep="first")]
    return series.rename("portfolio_daily")
