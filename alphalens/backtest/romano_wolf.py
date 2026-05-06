"""Romano-Wolf (2005) step-down family-wise-error-rate control.

Combines a Politis-Romano (1994) stationary block-bootstrap of the
joint test-statistic distribution under ``H_0`` with a step-down
multiple-testing procedure. Less conservative than naive Bonferroni
when test statistics are positively correlated; reduces to
Bonferroni-like behaviour at zero correlation and to unadjusted
behaviour at perfect correlation.

The retrospective replication pre-reg
``params_v9d_retrospective_pre_2018_2026_05_05.json`` calls for
n=25-family Romano-Wolf with ``mean_block_length=4`` weeks. This
module is the building block; the verdict driver supplies the
returns matrix (24 historical + 1 retrospective strategies, each
column a per-rebalance-period return series).

References:
- Romano, J. P., & Wolf, M. (2005). Stepwise multiple testing as
  formalized data snooping. *Econometrica*, 73(4), 1237-1282.
- Politis, D. N., & Romano, J. P. (1994). The stationary bootstrap.
  *JASA*, 89(428), 1303-1313.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class RomanoWolfResult:
    """Outcome of a Romano-Wolf step-down family-wise test."""

    observed_tstats: np.ndarray
    """Observed (one-sample) t-statistic per strategy, shape ``(S,)``."""

    adjusted_critical: np.ndarray
    """Romano-Wolf adjusted critical |t| per strategy, shape ``(S,)``.

    A strategy ``s`` is rejected iff ``|observed_tstats[s]| >
    adjusted_critical[s]``. Strategies that cannot be tested (step-down
    halted before reaching them) get ``inf``."""

    rejected: np.ndarray
    """Boolean rejection vector per strategy, shape ``(S,)``."""

    n_obs: int
    n_strategies: int
    n_bootstrap: int
    mean_block_length: float
    alpha: float


def stationary_bootstrap_indices(
    n_obs: int,
    mean_block_length: float,
    n_bootstrap: int,
    rng: np.random.Generator,
) -> np.ndarray:
    """Politis-Romano stationary bootstrap index matrix.

    Each resampled observation: with probability ``1 / mean_block_length``
    start a new block at a fresh uniform position; otherwise continue the
    current block by incrementing index modulo ``n_obs``. Block lengths
    follow a geometric distribution with mean ``mean_block_length``.

    Returns an ``(n_bootstrap, n_obs)`` integer array of indices into the
    original sample. ``mean_block_length=1`` collapses to IID resampling
    (every step starts a new block)."""
    if mean_block_length <= 0:
        raise ValueError(f"mean_block_length must be > 0, got {mean_block_length}")
    if n_obs <= 0:
        raise ValueError(f"n_obs must be > 0, got {n_obs}")
    if n_bootstrap <= 0:
        raise ValueError(f"n_bootstrap must be > 0, got {n_bootstrap}")

    p_new_block = 1.0 / mean_block_length
    indices = np.empty((n_bootstrap, n_obs), dtype=np.int64)
    # Initial position for each bootstrap replicate
    indices[:, 0] = rng.integers(0, n_obs, size=n_bootstrap)
    # Per-step new-block decisions
    new_block = rng.random(size=(n_bootstrap, n_obs)) < p_new_block
    fresh_starts = rng.integers(0, n_obs, size=(n_bootstrap, n_obs))
    for t in range(1, n_obs):
        prev = indices[:, t - 1]
        cont = (prev + 1) % n_obs
        indices[:, t] = np.where(new_block[:, t], fresh_starts[:, t], cont)
    return indices


def _compute_tstats(returns: np.ndarray) -> np.ndarray:
    """One-sample t-statistic per column: ``mean / (std/sqrt(n))``."""
    n_obs = returns.shape[0]
    means = returns.mean(axis=0)
    stds = returns.std(axis=0, ddof=1)
    # Guard against zero-variance columns (yields ``inf`` t-stat / NaN; treat
    # as 0 to avoid bootstrap pollution).
    stds = np.where(stds <= 0, np.nan, stds)
    return means / (stds / math.sqrt(n_obs))


def romano_wolf_step_down(
    returns: np.ndarray,
    *,
    alpha: float = 0.05,
    mean_block_length: float = 4.0,
    n_bootstrap: int = 10000,
    rng: np.random.Generator | None = None,
) -> RomanoWolfResult:
    """Step-down FWER control over the family of strategies in ``returns``.

    Each column is one strategy's per-rebalance-period return series. The
    test for strategy ``s`` is the two-sided ``H_0,s: μ_s = 0``. Bootstrap
    is centered (subtract observed mean before computing bootstrap t-stats)
    so that the joint null distribution can be sampled even when the data
    contain real signals.

    Parameters mirror the pre-reg lock: ``alpha=0.05``,
    ``mean_block_length=4`` (weeks; calibrate by user), ``n_bootstrap=10000``.
    """
    if returns.ndim != 2:
        raise ValueError(f"returns must be 2-D, got shape {returns.shape}")
    n_obs, n_strats = returns.shape
    if n_obs == 0 or n_strats == 0:
        raise ValueError(f"returns is empty: shape={returns.shape}")
    if not (0.0 < alpha < 1.0):
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    if n_bootstrap <= 0:
        raise ValueError(f"n_bootstrap must be > 0, got {n_bootstrap}")
    if mean_block_length <= 0:
        raise ValueError(f"mean_block_length must be > 0, got {mean_block_length}")

    # Caller passes seeded rng for reproducibility; unseeded default is intentional.
    if rng is None:
        rng = np.random.default_rng()  # NOSONAR

    observed_tstats = _compute_tstats(returns)
    observed_means = returns.mean(axis=0)

    # Bootstrap centered t-stats under H_0
    boot_idx = stationary_bootstrap_indices(
        n_obs=n_obs,
        mean_block_length=mean_block_length,
        n_bootstrap=n_bootstrap,
        rng=rng,
    )
    boot_tstats = np.empty((n_bootstrap, n_strats), dtype=np.float64)
    for b in range(n_bootstrap):
        sample = returns[boot_idx[b], :]
        boot_means = sample.mean(axis=0)
        boot_stds = sample.std(axis=0, ddof=1)
        boot_stds = np.where(boot_stds <= 0, np.nan, boot_stds)
        # Centered: subtract observed mean to nullify the signal
        boot_tstats[b, :] = (boot_means - observed_means) / (boot_stds / math.sqrt(n_obs))

    # Replace any NaN bootstrap stats with 0 (degenerate columns)
    boot_tstats = np.nan_to_num(boot_tstats, nan=0.0, posinf=0.0, neginf=0.0)
    abs_boot = np.abs(boot_tstats)
    rejected, adjusted_critical = _run_step_down(observed_tstats, abs_boot, alpha=alpha)
    return _build_result(
        observed_tstats=observed_tstats,
        adjusted_critical=adjusted_critical,
        rejected=rejected,
        n_obs=n_obs,
        n_strats=n_strats,
        n_bootstrap=n_bootstrap,
        mean_block_length=mean_block_length,
        alpha=alpha,
    )


def _run_step_down(
    observed_tstats: np.ndarray,
    abs_boot: np.ndarray,
    *,
    alpha: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Romano-Wolf step-down core: returns ``(rejected, adjusted_critical)``.

    Shared by ``romano_wolf_step_down`` and ``romano_wolf_step_down_stratified``.
    Processes strategies in descending order of |observed_t|; once a
    hypothesis fails to reject, the remaining strategies are reported with
    the same family-conditional critical value (they cannot be rejected
    since they have smaller |observed_t|).
    """
    abs_observed = np.nan_to_num(np.abs(observed_tstats), nan=0.0)
    n_strats = abs_observed.shape[0]
    sort_order = np.argsort(-abs_observed)
    adjusted_critical = np.full(n_strats, np.inf, dtype=np.float64)
    rejected = np.zeros(n_strats, dtype=bool)
    active_mask = np.ones(n_strats, dtype=bool)

    halted = False
    for s in sort_order:
        if not bool(active_mask.any()):
            break
        max_dist = abs_boot[:, active_mask].max(axis=1)
        c = float(np.quantile(max_dist, 1.0 - alpha))
        adjusted_critical[s] = c
        if halted:
            continue
        if abs_observed[s] > c:
            rejected[s] = True
            active_mask[s] = False
        else:
            halted = True
    return rejected, adjusted_critical


def _build_result(
    *,
    observed_tstats: np.ndarray,
    adjusted_critical: np.ndarray,
    rejected: np.ndarray,
    n_obs: int,
    n_strats: int,
    n_bootstrap: int,
    mean_block_length: float,
    alpha: float,
) -> RomanoWolfResult:
    return RomanoWolfResult(
        observed_tstats=observed_tstats,
        adjusted_critical=adjusted_critical,
        rejected=rejected,
        n_obs=n_obs,
        n_strategies=n_strats,
        n_bootstrap=n_bootstrap,
        mean_block_length=mean_block_length,
        alpha=alpha,
    )


def romano_wolf_step_down_stratified(
    returns_per_stratum: list[np.ndarray],
    *,
    alpha: float = 0.05,
    mean_block_length: float = 4.0,
    n_bootstrap: int = 10000,
    rng: np.random.Generator | None = None,
) -> RomanoWolfResult:
    """Stratified step-down FWER control over a family of strategies whose
    returns span calendar-disjoint sub-periods.

    Each element of ``returns_per_stratum`` is a ``(n_obs_k, n_strats)`` array
    representing one continuous calendar window. Block bootstrap is performed
    *independently within each stratum* (preserving within-window serial
    correlation) and replicate-by-replicate samples are concatenated to form
    the pooled bootstrap distribution. Compared to naive concat-then-bootstrap,
    this avoids spurious correlation across stratum seams (e.g. a block that
    crosses from December 2011 into January 2012 in v9D retrospective which
    has a calendar gap there in the pooled timeline).

    Equivalent to ``romano_wolf_step_down(np.vstack(returns_per_stratum))``
    when ``len(returns_per_stratum) == 1``, but produces a different (correctly
    structured) bootstrap distribution otherwise.

    The number of strategies must be equal across strata.
    """
    if not returns_per_stratum:
        raise ValueError("returns_per_stratum must contain at least one stratum")
    n_strats_set = {arr.shape[1] for arr in returns_per_stratum}
    if len(n_strats_set) != 1:
        raise ValueError(
            f"all strata must have same n_strategies, got shapes "
            f"{[a.shape for a in returns_per_stratum]}"
        )
    n_strats = next(iter(n_strats_set))
    if n_strats == 0:
        raise ValueError("n_strategies must be > 0")
    if not (0.0 < alpha < 1.0):
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    if n_bootstrap <= 0:
        raise ValueError(f"n_bootstrap must be > 0, got {n_bootstrap}")
    if mean_block_length <= 0:
        raise ValueError(f"mean_block_length must be > 0, got {mean_block_length}")

    # Caller passes seeded rng for reproducibility; unseeded default is intentional.
    if rng is None:
        rng = np.random.default_rng()  # NOSONAR

    pooled = np.vstack(returns_per_stratum)
    n_obs_total = pooled.shape[0]
    if n_obs_total == 0:
        raise ValueError("pooled returns is empty")

    observed_tstats = _compute_tstats(pooled)
    observed_means = pooled.mean(axis=0)

    # Per-stratum independent bootstrap. We materialize the pooled bootstrap
    # samples replicate-by-replicate by concatenating each stratum's resample.
    boot_tstats = np.empty((n_bootstrap, n_strats), dtype=np.float64)
    for b in range(n_bootstrap):
        # Concat stratum-resamples for replicate b (independent within-stratum).
        pooled_resample_chunks = []
        for stratum in returns_per_stratum:
            n_k = stratum.shape[0]
            idx = stationary_bootstrap_indices(
                n_obs=n_k,
                mean_block_length=mean_block_length,
                n_bootstrap=1,
                rng=rng,
            )[0]
            pooled_resample_chunks.append(stratum[idx, :])
        sample = np.vstack(pooled_resample_chunks)

        boot_means = sample.mean(axis=0)
        boot_stds = sample.std(axis=0, ddof=1)
        boot_stds = np.where(boot_stds <= 0, np.nan, boot_stds)
        boot_tstats[b, :] = (boot_means - observed_means) / (boot_stds / math.sqrt(n_obs_total))

    boot_tstats = np.nan_to_num(boot_tstats, nan=0.0, posinf=0.0, neginf=0.0)
    abs_boot = np.abs(boot_tstats)

    rejected, adjusted_critical = _run_step_down(observed_tstats, abs_boot, alpha=alpha)
    return _build_result(
        observed_tstats=observed_tstats,
        adjusted_critical=adjusted_critical,
        rejected=rejected,
        n_obs=n_obs_total,
        n_strats=n_strats,
        n_bootstrap=n_bootstrap,
        mean_block_length=mean_block_length,
        alpha=alpha,
    )


__all__ = [
    "RomanoWolfResult",
    "romano_wolf_step_down",
    "romano_wolf_step_down_stratified",
    "stationary_bootstrap_indices",
]
