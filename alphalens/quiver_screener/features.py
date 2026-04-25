"""Congress + insider signal feature computations on normalized trade logs.

All functions are pure — no I/O, no Quiver SDK coupling. Normalization from raw
Quiver response schema happens in `client.py` / fetch scripts so that features
can be unit-tested with tiny synthetic fixtures.

Normalized schemas (contract):

congress_trades DataFrame:
    ticker, date, representative, transaction ('PURCHASE'|'SALE'|'EXCHANGE'), amount_mid (float $)

insider_trades DataFrame:
    ticker, date, name, transaction ('A'|'D'), shares (int), price (float), value (float $)
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _window_filter(
    df: pd.DataFrame, ticker: str, as_of: pd.Timestamp, lookback_days: int
) -> pd.DataFrame:
    """Slice to (ticker, (as_of - lookback_days, as_of]).  Returns a view-like copy."""
    if df.empty:
        return df.iloc[0:0]
    lower = as_of - pd.Timedelta(days=lookback_days)
    return df[(df["ticker"] == ticker) & (df["date"] > lower) & (df["date"] <= as_of)]


def congress_net_flow(
    trades: pd.DataFrame,
    ticker: str,
    as_of: pd.Timestamp,
    lookback_days: int = 30,
) -> float:
    """Sum of (buys − sells) $-value by Congress members for ticker in window.

    EXCHANGE transactions contribute 0 (informationally neutral).
    """
    window = _window_filter(trades, ticker, as_of, lookback_days)
    if window.empty:
        return 0.0
    sign = window["transaction"].map({"PURCHASE": 1.0, "SALE": -1.0}).fillna(0.0)
    return float((sign * window["amount_mid"]).sum())


def congress_unique_members(
    trades: pd.DataFrame,
    ticker: str,
    as_of: pd.Timestamp,
    lookback_days: int = 30,
) -> int:
    """Count of distinct representatives who traded the ticker in window."""
    window = _window_filter(trades, ticker, as_of, lookback_days)
    return int(window["representative"].nunique()) if not window.empty else 0


def insider_cluster_flag(
    trades: pd.DataFrame,
    ticker: str,
    as_of: pd.Timestamp,
    lookback_days: int = 30,
    min_insiders: int = 3,
) -> bool:
    """True iff ≥ min_insiders distinct insiders posted ACQUIRED (A) Form 4s for ticker.

    Sells-only clusters do NOT count — classic insider-cluster signal is buy-side only.
    """
    window = _window_filter(trades, ticker, as_of, lookback_days)
    if window.empty:
        return False
    buys = window[window["transaction"] == "A"]
    return bool(buys["name"].nunique() >= min_insiders)


def insider_buy_ratio(
    trades: pd.DataFrame,
    ticker: str,
    as_of: pd.Timestamp,
    lookback_days: int = 60,
) -> float:
    """$-value fraction of BUY trades over total trade $-value in window.

    Returns NaN when window has no trades (no signal to report; don't collapse to 0
    because that would look like "all sells").
    """
    window = _window_filter(trades, ticker, as_of, lookback_days)
    if window.empty:
        return float("nan")
    buys = window.loc[window["transaction"] == "A", "value"].sum()
    total = window["value"].sum()
    if total == 0:
        return float("nan")
    return float(buys / total)


def insider_net_flow(
    trades: pd.DataFrame,
    ticker: str,
    as_of: pd.Timestamp,
    lookback_days: int = 60,
) -> float:
    """Sum of (buy $-value − sell $-value) by insiders for ticker in window.

    Returns 0.0 when window is empty — so this feature is safe to use in a
    cross-sectional ranking panel (unlike `insider_buy_ratio`, which returns
    NaN to distinguish "no data" from "all sells").
    """
    window = _window_filter(trades, ticker, as_of, lookback_days)
    if window.empty:
        return 0.0
    buys = window.loc[window["transaction"] == "A", "value"].sum()
    sells = window.loc[window["transaction"] == "D", "value"].sum()
    return float(buys - sells)


def _rolling_net_flow_panel(
    trades: pd.DataFrame,
    signed_amount: pd.Series,
    tickers: list[str],
    dates: pd.DatetimeIndex,
    lookback_days: int,
) -> pd.DataFrame:
    """Vectorized time-rolling sum of per-(date, ticker) net $ flows.

    Pandas' time-based ``rolling("{N}D")`` matches our scalar window semantics
    (``date > as_of - lookback AND date <= as_of``) by default (closed='right').
    Roughly 100x faster than per-cell Python iteration on a 113-ticker × 5y panel.
    """
    if trades.empty:
        return pd.DataFrame(0.0, index=pd.DatetimeIndex(dates), columns=tickers)

    daily = (
        pd.DataFrame(
            {
                "date_norm": trades["date"].dt.normalize(),
                "ticker": trades["ticker"],
                "net": signed_amount.values,
            }
        )
        .groupby(["date_norm", "ticker"])["net"]
        .sum()
        .unstack("ticker")
        .fillna(0.0)
    )
    # Trade dates must be present in the rolling index to contribute to the sum
    # at later requested dates, even if trade-date isn't itself requested.
    dates_norm = pd.DatetimeIndex(dates).normalize()
    union_idx = daily.index.union(dates_norm).sort_values()
    daily = daily.reindex(union_idx).fillna(0.0)

    rolling = daily.rolling(f"{lookback_days}D", min_periods=1, closed="right").sum()
    result = rolling.reindex(dates_norm).reindex(columns=tickers).fillna(0.0)
    result.index = pd.DatetimeIndex(dates)
    return result


def _slow_panel(
    trades: pd.DataFrame,
    fn,
    tickers: list[str],
    dates: pd.DatetimeIndex,
    lookback_days: int,
) -> pd.DataFrame:
    """Per-cell Python fallback. Used for features that aren't simple rolling sums
    (counts-distinct, ratios) where the fast vectorized path doesn't apply."""
    values = np.zeros((len(dates), len(tickers)), dtype=float)
    for i, d in enumerate(dates):
        for j, t in enumerate(tickers):
            values[i, j] = fn(trades, t, d, lookback_days=lookback_days)
    return pd.DataFrame(values, index=dates, columns=tickers)


def build_insider_feature_panel(
    trades: pd.DataFrame,
    tickers: list[str],
    dates: pd.DatetimeIndex,
    lookback_days: int = 60,
    feature: str = "net_flow",
) -> pd.DataFrame:
    """Cross-sectional insider feature panel. `feature`: 'net_flow' | 'buy_ratio'."""
    if feature == "net_flow":
        if trades.empty:
            return pd.DataFrame(0.0, index=pd.DatetimeIndex(dates), columns=tickers)
        signed_value = trades["value"] * trades["transaction"].map({"A": 1.0, "D": -1.0}).fillna(
            0.0
        )
        return _rolling_net_flow_panel(trades, signed_value, tickers, dates, lookback_days)
    if feature == "buy_ratio":
        return _slow_panel(trades, insider_buy_ratio, tickers, dates, lookback_days)
    raise ValueError(f"Unknown insider feature: {feature!r}")


def build_congress_feature_panel(
    trades: pd.DataFrame,
    tickers: list[str],
    dates: pd.DatetimeIndex,
    lookback_days: int = 30,
    feature: str = "net_flow",
) -> pd.DataFrame:
    """Cross-sectional panel of congress_net_flow (default) or unique_members.

    Index: dates. Columns: tickers (in given order). Used as factor series input
    to run_regression — convert panel → cross-sectional portfolio feature by
    aggregating across tickers per date (e.g. top-5 sum / median / etc).
    """
    if feature == "net_flow":
        if trades.empty:
            return pd.DataFrame(0.0, index=pd.DatetimeIndex(dates), columns=tickers)
        signed_amount = (
            trades["transaction"].map({"PURCHASE": 1.0, "SALE": -1.0}).fillna(0.0)
            * trades["amount_mid"]
        )
        return _rolling_net_flow_panel(trades, signed_amount, tickers, dates, lookback_days)
    if feature == "unique_members":
        return _slow_panel(trades, congress_unique_members, tickers, dates, lookback_days)
    raise ValueError(f"Unknown feature: {feature!r}")
