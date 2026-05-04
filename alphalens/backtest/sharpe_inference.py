"""Block-bootstrap Sharpe-difference inference (Ledoit-Wolf style).

Used by the overlay-class success metric per ADR 0007: when comparing a
risk-overlay-modified strategy to its ungated base, the Carhart α t-stat
is unreliable because vol-scaling makes betas time-varying. Sharpe-
difference is the canonical replacement.

Jobson-Korkie 1981 closed-form inference assumes IID-normal returns —
unreliable for option-roll-cycled, kurtotic portfolios. Ledoit & Wolf
2008 propose a circular block-bootstrap on paired returns instead, which
respects (a) heavy tails, (b) serial autocorrelation up to the block
length, and (c) cross-correlation between the two strategies (because
the same block index is sampled for both series).

This module implements the paired circular block-bootstrap. Sharpe is
computed as ``mean / std * sqrt(periods_per_year)`` on each resample.
The 1-sided p-value tests the null *Sharpe(a) ≤ Sharpe(b)*, which is
the relevant null for the overlay-vs-base hypothesis.

References:
- Ledoit, O., & Wolf, M. (2008). Robust performance hypothesis testing
  with the Sharpe ratio. *Journal of Empirical Finance*, 15(5), 850-859.
- Politis, D. N., & Romano, J. P. (1994). The stationary bootstrap.
  *JASA*, 89(428), 1303-1313.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class SharpeDiffResult:
    """One-sided block-bootstrap test of Sharpe(a) > Sharpe(b)."""

    sharpe_a: float
    sharpe_b: float
    sharpe_diff: float
    """Annualised Sharpe(a) − Sharpe(b)."""

    bootstrap_se: float
    """Bootstrap standard error of the Sharpe difference."""

    t_stat: float
    """sharpe_diff / bootstrap_se. NaN if SE is exactly zero (degenerate)."""

    p_value_one_sided: float
    """Fraction of bootstrap resamples where Sharpe(a) ≤ Sharpe(b).

    Tests H0: Sharpe(a) ≤ Sharpe(b), HA: Sharpe(a) > Sharpe(b)."""

    ci_lower: float
    ci_upper: float
    """Two-sided percentile CI for the Sharpe difference (default 95%)."""

    n_bootstrap: int
    block_size: int
    n_obs: int


def _sharpe(returns: np.ndarray, periods_per_year: int) -> float:
    """Annualised Sharpe = mean / std * sqrt(periods_per_year). Std uses ddof=1."""
    mu = float(np.mean(returns))
    sigma = float(np.std(returns, ddof=1))
    if not math.isfinite(sigma) or sigma <= 0.0:
        return float("nan")
    return mu / sigma * math.sqrt(periods_per_year)


def _circular_block_bootstrap_indices(
    n_obs: int, block_size: int, n_bootstrap: int, rng: np.random.Generator
) -> np.ndarray:
    """Generate (n_bootstrap × n_obs) index matrix for circular block-bootstrap.

    Each row contains ``ceil(n_obs / block_size)`` blocks of length
    ``block_size`` started at random positions in [0, n_obs); circular
    means indices wrap modulo n_obs. Truncated to length n_obs.
    """
    n_blocks = math.ceil(n_obs / block_size)
    starts = rng.integers(low=0, high=n_obs, size=(n_bootstrap, n_blocks))
    # Build n_blocks × block_size offset grid, then index-add and modulo.
    offsets = np.arange(block_size)
    # shape: (n_bootstrap, n_blocks, block_size)
    idx = (starts[:, :, None] + offsets[None, None, :]) % n_obs
    # flatten last two dims; truncate to n_obs.
    return idx.reshape(n_bootstrap, n_blocks * block_size)[:, :n_obs]


def block_bootstrap_sharpe_diff(
    returns_a: pd.Series | np.ndarray,
    returns_b: pd.Series | np.ndarray,
    *,
    periods_per_year: int,
    block_size: int = 21,
    n_bootstrap: int = 10_000,
    confidence: float = 0.95,
    seed: int | None = 0,
) -> SharpeDiffResult:
    """Paired circular block-bootstrap test of Sharpe(a) > Sharpe(b).

    ``returns_a`` and ``returns_b`` must be aligned (same length, same
    rebalance grid). Pairing is preserved by sampling the same row indices
    for both series — this is the Ledoit-Wolf 2008 prescription and makes
    the inference robust to cross-correlation between the two strategies.

    Parameters
    ----------
    returns_a, returns_b
        Per-rebalance return series. Length must match. NaN values are
        rejected (caller responsible for cleaning).
    periods_per_year
        Annualisation factor (e.g. 52 for stride=5 weekly).
    block_size
        Block length in *rebalance periods*. Default 21 ≈ one option-roll
        cycle at daily cadence; should be multi-period at the rebalance
        cadence used. Must be ≥ 1 and ≤ n_obs.
    n_bootstrap
        Number of bootstrap resamples. Default 10000.
    confidence
        Two-sided percentile-CI level. Default 0.95.
    seed
        RNG seed for reproducibility. Pass ``None`` for non-deterministic.
    """
    a = np.asarray(returns_a, dtype=float)
    b = np.asarray(returns_b, dtype=float)
    if a.shape != b.shape:
        raise ValueError(f"returns_a/b shape mismatch: {a.shape} vs {b.shape}")
    if a.ndim != 1:
        raise ValueError(f"returns must be 1-D, got shape {a.shape}")
    n = len(a)
    if n < 2:
        raise ValueError(f"need ≥2 observations, got {n}")
    if np.isnan(a).any() or np.isnan(b).any():
        raise ValueError("NaN values in returns — caller must clean")
    if not 1 <= block_size <= n:
        raise ValueError(f"block_size must be in [1, {n}], got {block_size}")
    if n_bootstrap < 100:
        raise ValueError(f"n_bootstrap must be ≥ 100, got {n_bootstrap}")
    if not 0.5 < confidence < 1.0:
        raise ValueError(f"confidence must be in (0.5, 1.0), got {confidence}")

    sharpe_a_obs = _sharpe(a, periods_per_year)
    sharpe_b_obs = _sharpe(b, periods_per_year)
    diff_obs = sharpe_a_obs - sharpe_b_obs

    rng = np.random.default_rng(seed)
    idx = _circular_block_bootstrap_indices(n, block_size, n_bootstrap, rng)

    a_resamples = a[idx]  # (n_bootstrap, n_obs)
    b_resamples = b[idx]

    # Vectorised Sharpe over each row.
    mean_a = a_resamples.mean(axis=1)
    mean_b = b_resamples.mean(axis=1)
    std_a = a_resamples.std(axis=1, ddof=1)
    std_b = b_resamples.std(axis=1, ddof=1)
    ann = math.sqrt(periods_per_year)

    # Degenerate-std rows yield NaN Sharpe; we drop them from inference rather
    # than imputing — for option-implied portfolios the chance is vanishingly
    # small but the guard is cheap.
    valid = (std_a > 0) & (std_b > 0)
    if not valid.any():
        raise RuntimeError("all bootstrap resamples produced zero std — degenerate input")

    sharpe_a_boot = (mean_a[valid] / std_a[valid]) * ann
    sharpe_b_boot = (mean_b[valid] / std_b[valid]) * ann
    diffs = sharpe_a_boot - sharpe_b_boot

    se = float(np.std(diffs, ddof=1))
    t_stat = float(diff_obs / se) if se > 0 else float("nan")

    # 1-sided p-value for H0: Sharpe(a) ≤ Sharpe(b) vs HA: Sharpe(a) > Sharpe(b).
    # The bootstrap distribution `diffs` approximates the sampling distribution
    # of the observed diff. Under H0 the diff is at most 0, so the canonical
    # non-studentized one-sided p-value is the fraction of bootstrap resamples
    # that DO NOT favor HA — i.e. yield diff ≤ 0.
    p_one = float((diffs <= 0.0).mean())

    alpha = 1.0 - confidence
    ci_lower = float(np.quantile(diffs, alpha / 2.0))
    ci_upper = float(np.quantile(diffs, 1.0 - alpha / 2.0))

    return SharpeDiffResult(
        sharpe_a=float(sharpe_a_obs),
        sharpe_b=float(sharpe_b_obs),
        sharpe_diff=float(diff_obs),
        bootstrap_se=se,
        t_stat=t_stat,
        p_value_one_sided=p_one,
        ci_lower=ci_lower,
        ci_upper=ci_upper,
        n_bootstrap=int(valid.sum()),
        block_size=block_size,
        n_obs=n,
    )
