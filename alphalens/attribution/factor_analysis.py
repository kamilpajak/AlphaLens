"""Factor-model regressions for backtest alpha attribution.

Primary interface:

    run_regression(portfolio_returns, factors, factor_columns, periods_per_year=252)
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
    spec_name: str  # e.g. "CAPM", "FF3", "Carhart-4F", "Carhart-4F + BusEq"
    alpha_daily: float  # Backwards-compat alias for alpha_per_period (always per-input-period).
    alpha_annualized: float  # alpha_per_period * periods_per_year_assumption.
    alpha_tstat: float  # t-stat from cov_type (HAC by default)
    betas: dict[str, float]  # one entry per factor in regression
    r_squared: float
    n_observations: int
    cov_type: str  # "HAC" | "nonrobust"
    # Tier 2.A 2026-05-05 — make annualization assumption explicit. Two new fields
    # for callers/serialized JSONs to verify the math without inspecting code.
    alpha_per_period: float = 0.0  # Same as alpha_daily but unambiguously named.
    periods_per_year_assumption: int = 252  # The multiplier used for annualization.


def _newey_west_maxlags(n: int) -> int:
    """Standard lag rule: int(4·(n/100)^(2/9)). For n=1260 → 10."""
    return max(1, int(np.floor(4 * (n / 100) ** (2 / 9))))


def run_regression(
    portfolio_returns: pd.Series,
    factors: pd.DataFrame,
    factor_columns: list[str],
    *,
    periods_per_year: int,
    cov_type: str = "HAC",
    spec_name: str | None = None,
    subtract_rf: bool = True,
    hac_maxlags: int | None = None,
) -> AlphaResult:
    """Run OLS of portfolio excess return on [intercept, *factor_columns].

    factors: DataFrame containing at least the requested columns + "RF" (decimals).
    periods_per_year: REQUIRED keyword. Annualization multiplier matching the
        input cadence — 252 for daily, 52 for weekly, 12 for monthly, 4 for
        quarterly. Previously defaulted to 252; the default was removed
        (issue #67) because 27 of 40 historical call sites passed strided
        returns and silently got 5× wrong annualization. The new contract
        forces explicit intent at every call site.
    cov_type: "HAC" (Newey-West, default) or "nonrobust" (plain OLS).
    subtract_rf: when True (default), y = port - RF. Set False when the input is
        already an excess return (e.g. a long-short factor) — then y = port
        directly and factors["RF"] is ignored for the LHS.
    hac_maxlags: explicit override for the Newey-West lag count. ``None`` (default)
        applies the formula ``int(4·(n/100)^(2/9))`` tuned for daily returns.
        For overlapping returns (e.g. stride=5 with holding=20 → MA(3-4) by
        construction), pass an explicit value at least equal to ``holding/stride``
        to ensure HAC SEs aren't artificially compressed. Ignored when
        ``cov_type != "HAC"``.
    """
    missing = [c for c in factor_columns if c not in factors.columns]
    if missing:
        raise ValueError(f"factor_columns not present in factors: {missing}")
    if subtract_rf and "RF" not in factors.columns:
        raise ValueError("factors DataFrame must contain an 'RF' column when subtract_rf=True")

    aligned = pd.concat([portfolio_returns.rename("port"), factors], axis=1, join="inner").dropna()
    if len(aligned) < 20:
        raise ValueError(f"Need at least 20 overlapping observations, got {len(aligned)}")

    y = aligned["port"] - aligned["RF"] if subtract_rf else aligned["port"]
    X = sm.add_constant(aligned[factor_columns])

    if cov_type == "HAC":
        maxlags = int(hac_maxlags) if hac_maxlags is not None else _newey_west_maxlags(len(aligned))
        model = sm.OLS(y, X).fit(
            cov_type="HAC",
            cov_kwds={"maxlags": maxlags},
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
        n_observations=len(aligned),
        cov_type=cov_type,
        alpha_per_period=alpha_daily,
        periods_per_year_assumption=int(periods_per_year),
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
        run_regression(
            portfolio_returns,
            carhart_factors,
            cols,
            periods_per_year=252,
            spec_name=name,
        )
        for name, cols in specs
    ]


def bootstrap_carhart_alpha_ci(
    portfolio_returns: pd.Series,
    carhart_factors: pd.DataFrame,
    *,
    iterations: int = 10_000,
    seed: int = 42,
    confidence: float = 0.95,
) -> tuple[float, float]:
    """Moving-block bootstrap 95% CI on annualized Carhart-4F alpha.

    Per Hall-Horowitz 1995 the block length is ``n^(1/3)``. HAC on the
    full-sample test handles serial correlation asymptotically; this
    bootstrap provides finite-sample robustness so the decision matrix
    can gate on "CI excludes 0" independently of the HAC t-stat
    threshold. Returns ``(ci_low_annualized, ci_high_annualized)``.

    ``carhart_factors`` must contain columns Mkt-RF, SMB, HML, Mom, RF.

    The kill_verdict_checklist Gate 6 spec uses this function as its
    canonical implementation — bootstrap the *Carhart alpha* (residual
    after factor controls), not raw mean return, so the CI answers the
    same question as the headline α t-stat.
    """
    factor_cols = ["Mkt-RF", "SMB", "HML", "Mom"]
    aligned = pd.concat(
        [portfolio_returns.rename("port"), carhart_factors],
        axis=1,
        join="inner",
    ).dropna()
    n = len(aligned)
    if n < 50:
        raise ValueError(f"Need >=50 obs for bootstrap, got {n}")

    block_len = max(1, int(np.floor(n ** (1 / 3))))
    n_blocks = int(np.ceil(n / block_len))

    y_arr = (aligned["port"] - aligned["RF"]).to_numpy()
    x_arr = sm.add_constant(aligned[factor_cols]).to_numpy()

    rng = np.random.default_rng(seed)
    alphas_daily = np.empty(iterations)
    for i in range(iterations):
        starts = rng.integers(0, n - block_len + 1, size=n_blocks)
        idx = np.concatenate([np.arange(s, s + block_len) for s in starts])[:n]
        beta, *_ = np.linalg.lstsq(x_arr[idx], y_arr[idx], rcond=None)
        alphas_daily[i] = beta[0]

    tail = (1 - confidence) / 2
    ci_low_daily, ci_high_daily = np.percentile(alphas_daily, [tail * 100, (1 - tail) * 100])
    return float(ci_low_daily * 252), float(ci_high_daily * 252)


def run_ff5_umd_attribution(
    portfolio_returns: pd.Series,
    ff5_umd_factors: pd.DataFrame,
) -> AlphaResult:
    """FF5+UMD 6-factor robustness regression.

    Phase 3b robustness check: if Carhart-4F α passes Bonferroni but this
    α attenuates >30%, the alpha was loading on RMW (profitability) or
    CMA (investment) rather than an independent edge (design doc §7 R5).

    ff5_umd_factors must contain: Mkt-RF, SMB, HML, RMW, CMA, Mom, RF.
    """
    return run_regression(
        portfolio_returns,
        ff5_umd_factors,
        ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "Mom"],
        periods_per_year=252,
        spec_name="FF5+UMD",
    )


def run_q4_attribution(
    portfolio_returns: pd.Series,
    q4_factors: pd.DataFrame,
) -> AlphaResult:
    """Hou-Xue-Zhang q-factor (Q4) robustness regression.

    Phase 3b robustness check alongside FF5+UMD. Q4 offers different
    factor construction (investment + ROE instead of HML/RMW), so an
    alpha that survives both Carhart-4F and Q4 has stronger claim to
    independence than one that survives only one.

    q4_factors must contain: Mkt-RF, ME, IA, ROE, RF.
    """
    return run_regression(
        portfolio_returns,
        q4_factors,
        ["Mkt-RF", "ME", "IA", "ROE"],
        periods_per_year=252,
        spec_name="Q4",
    )


def run_rolling_regression(
    portfolio_returns: pd.Series,
    factors: pd.DataFrame,
    factor_columns: list[str],
    window: int = 60,
    subtract_rf: bool = True,
) -> pd.DataFrame:
    """Rolling-window OLS via statsmodels.regression.rolling.RollingOLS.

    Returns DataFrame indexed by date with columns ``alpha`` and ``beta_<factor>``.
    Rows before the first full window are NaN. Plain OLS within each window
    (HAC is for full-sample inference, not window-by-window beta tracking).
    """
    from statsmodels.regression.rolling import RollingOLS

    missing = [c for c in factor_columns if c not in factors.columns]
    if missing:
        raise ValueError(f"factor_columns not present in factors: {missing}")
    if subtract_rf and "RF" not in factors.columns:
        raise ValueError("factors DataFrame must contain an 'RF' column when subtract_rf=True")

    aligned = pd.concat([portfolio_returns.rename("port"), factors], axis=1, join="inner").dropna()

    y_full = aligned["port"] - aligned["RF"] if subtract_rf else aligned["port"]
    # Capital X is the standard statsmodels/sklearn convention for a design matrix.
    X_full = sm.add_constant(aligned[factor_columns])  # NOSONAR

    res = RollingOLS(y_full, X_full, window=window).fit()
    params = res.params

    out = pd.DataFrame(index=aligned.index)
    out["alpha"] = params["const"]
    for col in factor_columns:
        out[f"beta_{col}"] = params[col]
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
