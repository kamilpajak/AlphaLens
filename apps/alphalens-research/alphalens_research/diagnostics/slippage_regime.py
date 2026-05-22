"""Regime-amplified slippage stress diagnostic — pure-function helpers.

Pre-reg memo: ``docs/research/insider_form4_opportunistic_slippage_stress_design_2026_05_12.md``.

The diagnostic re-evaluates an existing pre-registered Carhart-4F
regression on net-of-drag daily returns under varying bid-ask spread
assumptions. Regime amplification follows the literature anchor
(Chordia-Roll-Subrahmanyam 2001, Naes-Skjeltorp-Odegaard 2011):

    effective_half_spread(t) = base_bps * (1 + beta * max(0,
                                  (sigma_60d(t) - sigma_median) / sigma_median))

Daily drag (consistent with ``RealisticCostModel.primary_period_drag_bps``
semantics):

    drag_decimal(t) = (effective_half_spread(t) + adverse_selection_bps)
                       * turnover(t) * 2 / 10000

Turnover is broadcast from per-rebalance values to a daily series via
either concentrate (full turnover on rebalance day, zero elsewhere — the
pre-reg default to preserve Q5-panic clustering) or amortize (spread
evenly across the holding window — research alternative).
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
import pandas as pd

from alphalens_research.attribution.factor_analysis import run_regression
from alphalens_research.attribution.signal_vol_regime import (
    aggregate_returns_by_regime,
    assign_vol_regime_quintiles,
    classify_cyclicality_excess,
)
from alphalens_research.backtest.metrics import sharpe

_CARHART_FACTORS = ["Mkt-RF", "SMB", "HML", "Mom"]
_DEFAULT_HAC_MAXLAGS = 126
_DEFAULT_PERIODS_PER_YEAR = 252


def compute_effective_half_spread(
    vol_series: pd.Series,
    *,
    base_bps: float,
    beta: float,
    sigma_median: float | None = None,
) -> pd.Series:
    """Regime-amplified half-spread per-date.

    Above-median vol dates get amplified by ``beta * excess_fraction``;
    below-median vol dates get the base spread.

    Pre-reg memo §5: σ_median should be computed ONCE on the full joint
    test window and passed in via ``sigma_median``; falls back to the
    median of the supplied series when omitted (useful for tests).
    """
    if base_bps < 0:
        raise ValueError(f"base_bps must be >= 0, got {base_bps}")
    if beta < 0:
        raise ValueError(f"beta must be >= 0, got {beta}")

    if sigma_median is None:
        sigma_median = float(vol_series.median())
    if not math.isfinite(sigma_median) or sigma_median <= 0:
        raise ValueError(f"sigma_median must be a positive finite value, got {sigma_median}")

    excess_fraction = ((vol_series - sigma_median) / sigma_median).clip(lower=0.0)
    multiplier = 1.0 + beta * excess_fraction
    return base_bps * multiplier


def broadcast_turnover_to_daily(
    turnover_df: pd.DataFrame,
    daily_index: pd.DatetimeIndex,
    *,
    holding_days: int,
    mode: str = "concentrate",
) -> pd.Series:
    """Broadcast per-rebalance turnover to a daily series over ``daily_index``.

    Parameters
    ----------
    turnover_df : pd.DataFrame
        Index = rebalance dates (DatetimeIndex). Column ``turnover`` is the
        per-rebalance fraction (first row may be NaN, treated as zero drag).
    daily_index : pd.DatetimeIndex
        Target daily calendar.
    holding_days : int
        Rebalance stride (e.g. 21 for monthly).
    mode : str
        ``"concentrate"`` (default) — full turnover on the rebalance day,
        zero on all other days. Preserves Q5-panic clustering.
        ``"amortize"`` — spread turnover evenly across the holding window
        starting at the rebalance day.
    """
    if mode not in {"concentrate", "amortize"}:
        raise ValueError(f"mode must be 'concentrate' or 'amortize', got {mode!r}")
    if holding_days <= 0:
        raise ValueError(f"holding_days must be > 0, got {holding_days}")

    result = pd.Series(0.0, index=daily_index, dtype=float)
    if turnover_df.empty:
        return result

    turnover = turnover_df["turnover"].fillna(0.0)
    rebal_dates = turnover_df.index

    if mode == "concentrate":
        # Drop rebalance dates that aren't in the daily calendar.
        aligned = turnover.reindex(daily_index, fill_value=0.0)
        return aligned.fillna(0.0).astype(float)

    # amortize: spread each turnover over the next ``holding_days`` calendar
    # positions in daily_index.
    daily_positions = {ts: i for i, ts in enumerate(daily_index)}
    n_daily = len(daily_index)
    arr = np.zeros(n_daily, dtype=float)
    for rebal_ts, t in zip(rebal_dates, turnover.values, strict=False):
        if t <= 0.0:
            continue
        start_pos = daily_positions.get(rebal_ts)
        if start_pos is None:
            continue
        end_pos = min(start_pos + holding_days, n_daily)
        span = end_pos - start_pos
        if span <= 0:
            continue
        arr[start_pos:end_pos] += t / span
    return pd.Series(arr, index=daily_index, dtype=float)


def apply_regime_drag(
    gross_daily: pd.Series,
    effective_half_spread_bps: pd.Series,
    turnover_daily: pd.Series,
    *,
    adverse_selection_bps: float = 5.0,
) -> dict:
    """Compute per-day drag and net returns.

    Drag formula (round-trip per execution):
        drag_decimal(t) = (effective_hs(t) + adverse_selection_bps)
                          * turnover_daily(t) * 2 / 10_000

    Inputs aligned on common index via inner join; non-aligned rows dropped.
    """
    aligned = pd.concat(
        [
            gross_daily.rename("gross"),
            effective_half_spread_bps.rename("hs"),
            turnover_daily.rename("turn"),
        ],
        axis=1,
        join="inner",
    ).dropna()
    drag_bps = (aligned["hs"] + adverse_selection_bps) * aligned["turn"] * 2.0
    drag_daily = drag_bps / 10_000.0
    rets_net = aligned["gross"] - drag_daily
    return {
        "rets_net": rets_net,
        "drag_daily": drag_daily,
        "gross_aligned": aligned["gross"],
    }


def per_quintile_mean_returns(rets: pd.Series, vol_series: pd.Series) -> pd.Series:
    """Mean of ``rets`` per vol-quintile (Q1-Q5)."""
    aligned_vol = vol_series.reindex(rets.index)
    quintiles = assign_vol_regime_quintiles(aligned_vol)
    summary = aggregate_returns_by_regime(rets, quintiles)
    return summary.quintile_means


def run_one_slippage_combo(
    *,
    gross_daily: pd.Series,
    turnover_daily: pd.Series,
    vol_series: pd.Series,
    factors: pd.DataFrame,
    half_spread_bps: float,
    beta: float,
    sigma_median: float | None = None,
    adverse_selection_bps: float = 5.0,
    hac_maxlags: int = _DEFAULT_HAC_MAXLAGS,
    periods_per_year: int = _DEFAULT_PERIODS_PER_YEAR,
) -> dict:
    """Run a single ``(half_spread, beta)`` stress combo on one phase.

    Returns a dict with: half_spread_bps, beta, alpha_t_net,
    alpha_annualized_net, sharpe_net, total_drag_decimal, drag_mean_q1,
    drag_mean_q5, n_observations. (Post-drag cyclicality is computed
    separately by :func:`run_post_drag_cyclicality`.)

    ``sigma_median`` should be pre-computed ONCE on the full joint test
    window per pre-reg memo §5 and threaded through from the orchestrator;
    falls back to the per-phase vol median when omitted (useful for
    isolated calls in tests, but VIOLATES pre-reg §5 if used in production
    diagnostic runs).
    """
    vol_aligned = vol_series.reindex(gross_daily.index)
    effective_hs = compute_effective_half_spread(
        vol_aligned,
        base_bps=half_spread_bps,
        beta=beta,
        sigma_median=sigma_median,
    )
    drag_result = apply_regime_drag(
        gross_daily,
        effective_hs,
        turnover_daily,
        adverse_selection_bps=adverse_selection_bps,
    )
    rets_net = drag_result["rets_net"]
    drag_daily = drag_result["drag_daily"]

    res = run_regression(
        rets_net,
        factors[[*_CARHART_FACTORS, "RF"]],
        _CARHART_FACTORS,
        hac_maxlags=hac_maxlags,
        periods_per_year=periods_per_year,
    )

    # Per-quintile drag means (Q1=calm, Q5=panic).
    drag_quintiles_summary = aggregate_returns_by_regime(
        drag_daily, assign_vol_regime_quintiles(vol_aligned.reindex(drag_daily.index))
    )
    drag_mean_q1 = float(drag_quintiles_summary.quintile_means.loc["Q1"])
    drag_mean_q5 = float(drag_quintiles_summary.quintile_means.loc["Q5"])

    return {
        "half_spread_bps": float(half_spread_bps),
        "beta": float(beta),
        "alpha_t_net": float(res.alpha_tstat),
        "alpha_annualized_net": float(res.alpha_annualized),
        "sharpe_net": sharpe(rets_net.tolist(), periods_per_year=periods_per_year),
        "total_drag_decimal": float(drag_daily.sum()),
        "drag_mean_q1": drag_mean_q1,
        "drag_mean_q5": drag_mean_q5,
        "n_observations": int(res.n_observations),
    }


def run_post_drag_cyclicality(
    *,
    rets_net: pd.Series,
    benchmark_daily: pd.Series,
    vol_series: pd.Series,
) -> dict:
    """Re-run ``classify_cyclicality_excess`` on net-of-drag returns.

    Pre-reg memo §9 mandates this as a mandatory observation gate: a
    flip from EXTREME counter-cyclical (excess_R ≤ -1.0) toward
    orthogonal or pro-cyclical reopens the Layer 4 overlay class.
    """
    vol_aligned = vol_series.reindex(rets_net.index)
    quintiles = assign_vol_regime_quintiles(vol_aligned)
    strategy_summary = aggregate_returns_by_regime(rets_net, quintiles)

    bench_aligned = benchmark_daily.reindex(rets_net.index)
    benchmark_summary = aggregate_returns_by_regime(bench_aligned, quintiles)

    verdict = classify_cyclicality_excess(strategy_summary, benchmark_summary)
    return {
        "strategy_r_mean": verdict.strategy_r_mean,
        "benchmark_r_mean": verdict.benchmark_r_mean,
        "excess_r_mean": verdict.excess_r_mean,
        "classification": verdict.classification,
        "proceed": verdict.proceed,
    }


def run_full_grid(
    *,
    gross_per_phase: Sequence[pd.Series],
    turnover_per_phase: Sequence[pd.DataFrame],
    vol_series: pd.Series,
    factors: pd.DataFrame,
    half_spread_grid: Sequence[float],
    beta_grid: Sequence[float],
    sigma_median: float | None = None,
    holding_days: int = 21,
    adverse_selection_bps: float = 5.0,
    hac_maxlags: int = _DEFAULT_HAC_MAXLAGS,
    broadcast_mode: str = "concentrate",
) -> pd.DataFrame:
    """Run the full ``(half_spread, beta) × phase`` grid.

    Returns a long-format DataFrame with one row per
    ``(half_spread_bps, beta, phase_offset)`` combination plus a pooled
    summary across phases (computed by the caller).

    ``sigma_median`` is threaded through to each combo call per pre-reg
    memo §5 (full-sample joint window median, computed once by caller).
    """
    if len(gross_per_phase) != len(turnover_per_phase):
        raise ValueError(
            f"gross_per_phase ({len(gross_per_phase)}) and turnover_per_phase "
            f"({len(turnover_per_phase)}) must align"
        )

    rows: list[dict] = []
    for phase_idx, (gross, turnover_df) in enumerate(
        zip(gross_per_phase, turnover_per_phase, strict=True)
    ):
        turnover_daily = broadcast_turnover_to_daily(
            turnover_df,
            gross.index,
            holding_days=holding_days,
            mode=broadcast_mode,
        )
        for hs in half_spread_grid:
            for beta in beta_grid:
                row = run_one_slippage_combo(
                    gross_daily=gross,
                    turnover_daily=turnover_daily,
                    vol_series=vol_series,
                    factors=factors,
                    half_spread_bps=float(hs),
                    beta=float(beta),
                    sigma_median=sigma_median,
                    adverse_selection_bps=adverse_selection_bps,
                    hac_maxlags=hac_maxlags,
                )
                row["phase_offset"] = phase_idx
                rows.append(row)
    return pd.DataFrame(rows)
