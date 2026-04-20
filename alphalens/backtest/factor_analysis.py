"""Fama-French 3-factor alpha regression for the backtest portfolio.

Regress daily excess returns on [Mkt-RF, SMB, HML] to separate genuine alpha
from loading on a known factor. If alpha is small / negative, the strategy is
replicating a standard style premium rather than generating new edge.

    y_t = alpha + beta_mkt * Mkt-RF_t + beta_smb * SMB_t + beta_hml * HML_t + eps

All factor values come pre-converted to decimals from `factors.load_ff3_daily`.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import statsmodels.api as sm


@dataclass(frozen=True)
class AlphaResult:
    alpha_daily: float            # regression intercept (already decimal, e.g. 0.0002)
    alpha_annualized: float       # alpha_daily * 252
    alpha_tstat: float
    beta_mkt: float
    beta_smb: float
    beta_hml: float
    r_squared: float
    n_observations: int


def fama_french_alpha(
    portfolio_returns: pd.Series,
    ff3_factors: pd.DataFrame,
    periods_per_year: int = 252,
) -> AlphaResult:
    """Run OLS of (portfolio − RF) on [Mkt-RF, SMB, HML] + constant.

    portfolio_returns: DatetimeIndex Series of daily fractional returns.
    ff3_factors: DataFrame with columns Mkt-RF, SMB, HML, RF (all decimals).

    Raises ValueError if the aligned window is empty or too short (< 20 obs).
    """
    required = {"Mkt-RF", "SMB", "HML", "RF"}
    missing = required - set(ff3_factors.columns)
    if missing:
        raise ValueError(f"ff3_factors missing columns: {missing}")

    aligned = pd.concat(
        [portfolio_returns.rename("port"), ff3_factors], axis=1, join="inner"
    ).dropna()
    if len(aligned) < 20:
        raise ValueError(
            f"Need at least 20 overlapping days for FF3 regression, got {len(aligned)}"
        )

    y = aligned["port"] - aligned["RF"]
    X = sm.add_constant(aligned[["Mkt-RF", "SMB", "HML"]])

    model = sm.OLS(y, X).fit()
    const_name = "const"
    return AlphaResult(
        alpha_daily=float(model.params[const_name]),
        alpha_annualized=float(model.params[const_name] * periods_per_year),
        alpha_tstat=float(model.tvalues[const_name]),
        beta_mkt=float(model.params["Mkt-RF"]),
        beta_smb=float(model.params["SMB"]),
        beta_hml=float(model.params["HML"]),
        r_squared=float(model.rsquared),
        n_observations=int(len(aligned)),
    )


def format_alpha_summary(result: AlphaResult) -> str:
    """Multi-line human-readable summary — used in the backtest CLI report."""
    lines = [
        f"Fama-French 3-factor regression (n={result.n_observations})",
        f"  alpha (daily)        = {result.alpha_daily * 10_000:+.2f} bps/day",
        f"  alpha (annualized)   = {result.alpha_annualized * 100:+.2f}%",
        f"  alpha t-stat         = {result.alpha_tstat:+.2f}",
        f"  beta[Mkt-RF]         = {result.beta_mkt:+.3f}",
        f"  beta[SMB]            = {result.beta_smb:+.3f}",
        f"  beta[HML]            = {result.beta_hml:+.3f}",
        f"  R²                   = {result.r_squared:.3f}",
    ]
    return "\n".join(lines)
