"""Pure scoring primitives for the idiosyncratic-momentum screener.

Each function is small and side-effect-free so it can be tested without
yfinance / FF3 fixtures. The composite ``score_idiosyncratic_momentum``
assembles them on a per-rebalance cross-section.

Math (Blitz-Huij-Martens 2011 canonical):

1. Resample daily close to month-end → monthly simple returns.
2. Form excess monthly return ``r_excess = r - rf`` aligned to FF3 months.
3. Rolling 36-month OLS regression ``r_excess = α + β_M·Mkt-RF +
   β_S·SMB + β_H·HML + ε``.
4. Residual cumulative momentum
   ``IM_i,t = (1/σ_36) · Σ_{k=t-12}^{t-2} ε_i,k``
   where σ_36 is the std-dev of the same 36 residuals (NOT just the
   formation window).
5. Cross-sectional winsorise [1%, 99%] + z-score; top-decile picks long.

Sign convention: higher IM → stronger residual-momentum, long-decile
candidate.
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

from alphalens_research.screeners._common import rank_zscore, winsorize

__all__ = [
    "compute_idio_momentum",
    "compute_residuals_window",
    "monthly_returns_from_daily",
    "rank_zscore",
    "score_idiosyncratic_momentum",
    "winsorize",
]

_DEFAULT_REGRESSION_WINDOW = 36
_DEFAULT_FORMATION_LOOKBACK = 12
_DEFAULT_SKIP = 2
_FF3_REGRESSORS = ("Mkt-RF", "SMB", "HML")


def monthly_returns_from_daily(close: pd.Series) -> pd.Series:
    """Resample daily close-price Series to month-end simple returns.

    Drops the first month (whose pct_change is NaN). Empty / single-month
    inputs return an empty Series. CRITICAL: the trailing month is excluded
    when the input series ends before that month's last business day, so
    rebalance-time partial-month observations never contaminate the
    regression input — Blitz canonical residualisation requires full-month
    returns regressed against full-month FF3 factors.
    """
    if close is None or close.empty:
        return pd.Series(dtype=float)
    monthly_close = close.resample("ME").last().dropna()
    # Drop the trailing month if the input ends before its business-month-end.
    # `pd.offsets.BMonthEnd()` is the inclusive last business day of the month;
    # if last_obs < BMonthEnd(last_obs) then the month is incomplete.
    if not monthly_close.empty:
        last_obs = close.index[-1]
        last_business_day_of_month = last_obs + pd.offsets.BMonthEnd(0)
        if last_obs < last_business_day_of_month:
            monthly_close = monthly_close.iloc[:-1]
    if len(monthly_close) < 2:
        return pd.Series(dtype=float)
    return monthly_close.pct_change().dropna()


def _fit_ols_residuals(y: np.ndarray, X: np.ndarray) -> np.ndarray | None:
    """OLS via ``np.linalg.lstsq``; prepends intercept column.

    Returns the residual vector ``y - X·β̂`` (length n), or ``None`` if
    the design matrix is rank-deficient.
    """
    n = len(y)
    if n == 0 or X.shape[0] != n:
        return None
    design = np.column_stack([np.ones(n), X])
    try:
        coef, _, rank, _ = np.linalg.lstsq(design, y, rcond=None)
    except np.linalg.LinAlgError:
        return None
    if rank < design.shape[1]:
        return None
    return y - design @ coef


def compute_residuals_window(
    monthly_excess_returns: pd.Series,
    ff3_monthly: pd.DataFrame,
    asof_month: pd.Timestamp,
    *,
    window: int = _DEFAULT_REGRESSION_WINDOW,
) -> pd.Series | None:
    """Fit FF3 regression on the ``window`` months ending at-or-before ``asof_month``.

    ``ff3_monthly`` must contain the three regressor columns ``Mkt-RF``,
    ``SMB``, ``HML`` indexed by month-end timestamps. ``monthly_excess_returns``
    is the ticker's monthly returns already net of the risk-free rate.

    Returns a Series of residuals indexed by the inner-joined month-end
    timestamps used in the regression. Returns ``None`` when there are
    fewer than ``window`` overlapping observations.
    """
    if monthly_excess_returns.empty or ff3_monthly.empty:
        return None
    missing_cols = [c for c in _FF3_REGRESSORS if c not in ff3_monthly.columns]
    if missing_cols:
        raise ValueError(f"ff3_monthly missing required columns: {missing_cols}")

    eligible_idx = monthly_excess_returns.index[monthly_excess_returns.index <= asof_month]
    if len(eligible_idx) < window:
        return None
    last_window_idx = eligible_idx[-window:]

    joined = pd.concat(
        [monthly_excess_returns.loc[last_window_idx], ff3_monthly.loc[:, list(_FF3_REGRESSORS)]],
        axis=1,
        join="inner",
    ).dropna()
    if len(joined) < window:
        return None
    joined = joined.iloc[-window:]

    y = joined.iloc[:, 0].to_numpy(dtype=float)
    X = joined.iloc[:, 1:].to_numpy(dtype=float)
    resid = _fit_ols_residuals(y, X)
    if resid is None:
        return None
    return pd.Series(resid, index=joined.index, name="residual")


def compute_idio_momentum(
    residuals: pd.Series,
    *,
    formation_lookback: int = _DEFAULT_FORMATION_LOOKBACK,
    skip: int = _DEFAULT_SKIP,
) -> float | None:
    """Idiosyncratic-momentum scalar: ``(1/σ_window) · Σ residuals[t-L..t-skip]``.

    The denominator σ is computed over the FULL residual window (Blitz
    canonical), not just the formation slice. Returns ``None`` if σ is
    non-finite or zero.

    With defaults (``formation_lookback=12, skip=2``) and a 36-month
    residual series, the formation window covers positions [-12, -2] —
    i.e. 11 months ending 1 month before the residual horizon, matching
    the academic skip-1 momentum convention.
    """
    if residuals is None or residuals.empty:
        return None
    n = len(residuals)
    if formation_lookback <= 0 or skip < 1 or formation_lookback < skip:
        raise ValueError(f"Invalid formation params: lookback={formation_lookback}, skip={skip}")
    if n < formation_lookback:
        return None
    formation = residuals.iloc[-formation_lookback : (-(skip - 1) if skip > 1 else None)]
    if formation.empty:
        return None
    sigma = float(residuals.std(ddof=0))
    # Treat sub-1e-12 σ as numerically zero — constant-residual series can
    # surface tiny float-noise std-devs that would otherwise blow up the
    # ratio to spurious 1e16-magnitude IM values.
    if not np.isfinite(sigma) or sigma < 1e-12:
        return None
    return float(formation.sum() / sigma)


def score_idiosyncratic_momentum(
    monthly_returns_by_ticker: Mapping[str, pd.Series],
    ff3_monthly: pd.DataFrame,
    rf_monthly: pd.Series,
    asof_month: pd.Timestamp,
    *,
    window: int = _DEFAULT_REGRESSION_WINDOW,
    formation_lookback: int = _DEFAULT_FORMATION_LOOKBACK,
    skip: int = _DEFAULT_SKIP,
    winsorize_pct: tuple[float, float] = (0.01, 0.99),
) -> pd.Series:
    """Build the cross-sectional IM score Series for a single rebalance.

    ``monthly_returns_by_ticker`` maps each ticker → its monthly simple
    return series. ``rf_monthly`` is the FF risk-free monthly rate
    (already in decimal form, e.g. 0.0017 for 17 bps/mo). ``ff3_monthly``
    holds the FF3 regressors.

    Tickers with insufficient history or degenerate residuals are
    dropped from the output entirely (not retained as NaN). Higher score
    = stronger residual-momentum = long-decile candidate.
    """
    ims: dict[str, float] = {}
    for ticker, monthly_rets in monthly_returns_by_ticker.items():
        if monthly_rets is None or monthly_rets.empty:
            continue
        aligned_rf = rf_monthly.reindex(monthly_rets.index)
        excess = (monthly_rets - aligned_rf).dropna()
        if excess.empty:
            continue
        resid = compute_residuals_window(excess, ff3_monthly, asof_month, window=window)
        if resid is None:
            continue
        im = compute_idio_momentum(
            resid,
            formation_lookback=formation_lookback,
            skip=skip,
        )
        if im is None or not np.isfinite(im):
            continue
        ims[ticker] = im

    if not ims:
        return pd.Series(dtype=float, name="idio_momentum_zscore")
    raw = pd.Series(ims, name="idio_momentum_zscore")
    winsorized = winsorize(raw, lower_pct=winsorize_pct[0], upper_pct=winsorize_pct[1])
    return rank_zscore(winsorized)
