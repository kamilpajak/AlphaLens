"""Factor-model regressions for backtest alpha attribution.

Primary interface:

    run_regression(portfolio_returns, factors, factor_columns, cov_type="HAC")
        Single OLS, (portfolio − RF) on [intercept, *factors]. Default t-stats are
        Newey-West HAC (lag = int(4·(n/100)^(2/9))), which dampens false-positive
        significance caused by autocorrelated daily errors.

    run_carhart_attribution(portfolio_returns, carhart_factors)
        Three incremental regressions — CAPM → FF3 → Carhart-4F — so alpha can be
        tracked as momentum (UMD/Mom) is added. If Carhart alpha collapses, the
        strategy was re-packaged momentum factor beta, not an independent edge.

    run_rolling_regression(portfolio_returns, factors, factor_columns, window=60)
        Rolling-window OLS betas + alpha, one row per date. Diagnostic for
        time-varying factor exposures (top-5 daily-rebal portfolios violate
        static-beta OLS assumptions — rolling exposes that directly).

All factor values are assumed pre-converted to decimals by `factors.load_*`.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import statsmodels.api as sm


@dataclass(frozen=True)
class AlphaResult:
    spec_name: str                    # e.g. "CAPM", "FF3", "Carhart-4F", "Carhart-4F + BusEq"
    alpha_daily: float
    alpha_annualized: float
    alpha_tstat: float                # t-stat from cov_type (HAC by default)
    betas: dict[str, float]           # one entry per factor in regression
    r_squared: float
    n_observations: int
    cov_type: str                     # "HAC" | "nonrobust"


def _newey_west_maxlags(n: int) -> int:
    """Standard lag rule: int(4·(n/100)^(2/9)). For n=1260 → 10."""
    return max(1, int(np.floor(4 * (n / 100) ** (2 / 9))))


def run_regression(
    portfolio_returns: pd.Series,
    factors: pd.DataFrame,
    factor_columns: list[str],
    cov_type: str = "HAC",
    spec_name: str | None = None,
    periods_per_year: int = 252,
) -> AlphaResult:
    """Run OLS of (portfolio − RF) on [intercept, *factor_columns].

    factors: DataFrame containing at least the requested columns + "RF" (decimals).
    cov_type: "HAC" (Newey-West, default) or "nonrobust" (plain OLS).
    """
    if "RF" not in factors.columns:
        raise ValueError("factors DataFrame must contain an 'RF' column")
    missing = [c for c in factor_columns if c not in factors.columns]
    if missing:
        raise ValueError(f"factor_columns not present in factors: {missing}")

    aligned = pd.concat(
        [portfolio_returns.rename("port"), factors], axis=1, join="inner"
    ).dropna()
    if len(aligned) < 20:
        raise ValueError(
            f"Need at least 20 overlapping observations, got {len(aligned)}"
        )

    y = aligned["port"] - aligned["RF"]
    X = sm.add_constant(aligned[factor_columns])

    if cov_type == "HAC":
        model = sm.OLS(y, X).fit(
            cov_type="HAC",
            cov_kwds={"maxlags": _newey_west_maxlags(len(aligned))},
        )
    elif cov_type == "nonrobust":
        model = sm.OLS(y, X).fit()
    else:
        raise ValueError(f"Unsupported cov_type: {cov_type!r}")

    alpha_daily = float(model.params["const"])
    return AlphaResult(
        spec_name=spec_name or "+".join(factor_columns),
        alpha_daily=alpha_daily,
        alpha_annualized=alpha_daily * periods_per_year,
        alpha_tstat=float(model.tvalues["const"]),
        betas={col: float(model.params[col]) for col in factor_columns},
        r_squared=float(model.rsquared),
        n_observations=int(len(aligned)),
        cov_type=cov_type,
    )


def run_carhart_attribution(
    portfolio_returns: pd.Series,
    carhart_factors: pd.DataFrame,
) -> list[AlphaResult]:
    """Incremental attribution: CAPM → FF3 → Carhart-4F.

    carhart_factors must contain columns: Mkt-RF, SMB, HML, Mom, RF.
    """
    specs = [
        ("CAPM", ["Mkt-RF"]),
        ("FF3", ["Mkt-RF", "SMB", "HML"]),
        ("Carhart-4F", ["Mkt-RF", "SMB", "HML", "Mom"]),
    ]
    return [
        run_regression(portfolio_returns, carhart_factors, cols, spec_name=name)
        for name, cols in specs
    ]


def run_rolling_regression(
    portfolio_returns: pd.Series,
    factors: pd.DataFrame,
    factor_columns: list[str],
    window: int = 60,
) -> pd.DataFrame:
    """Rolling-window OLS. Returns DataFrame indexed by date with columns
    ``alpha`` and ``beta_<factor>`` for each factor.

    Rows before the first full window are NaN. Uses plain OLS inside each window
    (HAC is for full-sample inference, not window-by-window beta tracking).
    """
    if "RF" not in factors.columns:
        raise ValueError("factors DataFrame must contain an 'RF' column")
    missing = [c for c in factor_columns if c not in factors.columns]
    if missing:
        raise ValueError(f"factor_columns not present in factors: {missing}")

    aligned = pd.concat(
        [portfolio_returns.rename("port"), factors], axis=1, join="inner"
    ).dropna()

    idx = aligned.index
    out = pd.DataFrame(
        index=idx,
        columns=["alpha"] + [f"beta_{c}" for c in factor_columns],
        dtype=float,
    )

    y_full = aligned["port"] - aligned["RF"]
    X_full = sm.add_constant(aligned[factor_columns])

    for end in range(window, len(aligned) + 1):
        sl = slice(end - window, end)
        res = sm.OLS(y_full.iloc[sl], X_full.iloc[sl]).fit()
        row_idx = idx[end - 1]
        out.at[row_idx, "alpha"] = float(res.params["const"])
        for col in factor_columns:
            out.at[row_idx, f"beta_{col}"] = float(res.params[col])

    return out


def format_alpha_summary(result: AlphaResult) -> str:
    """Multi-line human-readable summary of a single regression."""
    lines = [
        f"[{result.spec_name}] n={result.n_observations}  cov={result.cov_type}",
        f"  alpha (daily)      = {result.alpha_daily * 10_000:+.2f} bps/day",
        f"  alpha (annualized) = {result.alpha_annualized * 100:+.2f}%",
        f"  alpha t-stat       = {result.alpha_tstat:+.2f}",
    ]
    for name, beta in result.betas.items():
        lines.append(f"  beta[{name:<8}]    = {beta:+.3f}")
    lines.append(f"  R²                 = {result.r_squared:.3f}")
    return "\n".join(lines)


def format_attribution_table(results: list[AlphaResult]) -> str:
    """Compact one-row-per-spec table comparing CAPM / FF3 / Carhart-4F side-by-side."""
    header = f"{'spec':<22} {'α bps/d':>10} {'α ann':>8} {'t-stat':>8} {'R²':>6} {'n':>6}"
    rows = [header, "-" * len(header)]
    for r in results:
        rows.append(
            f"{r.spec_name:<22} "
            f"{r.alpha_daily * 10_000:>+10.2f} "
            f"{r.alpha_annualized * 100:>+7.2f}% "
            f"{r.alpha_tstat:>+8.2f} "
            f"{r.r_squared:>6.3f} "
            f"{r.n_observations:>6d}"
        )
    return "\n".join(rows)
