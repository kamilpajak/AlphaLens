"""Multiple-hypothesis-testing corrections for Phase 3b validation.

Per Perplexity R8 Q2 (confirmed 2026-04-22): Layer 2d validation counts
n=2 decision-critical hypotheses (H1 primary + H2 sector-neutral from
design doc §1). Robustness checks (FF5+UMD, Q4 Hou-Xue-Zhang) and regime
subsets (bull/bear/flat) do NOT inflate the Bonferroni denominator —
they are consistency validations on the same H1, not independent tests
(Harvey-Liu-Zhu 2016; Bailey-Lopez de Prado "Sharper Angle").

Threshold at n=2, α=0.05:
    α_adj = 0.05 / 2 = 0.025
    critical |t| ≈ 2.24 (two-tailed standard normal)

For n>≈30 the normal approximation is adequate for any Layer 2d backtest
(4000+ daily observations → df≫30). If finite-sample correction matters
in a future use case, switch :func:`bonferroni_critical_tstat` to
``scipy.stats.t.ppf`` with explicit ``df``.
"""

from __future__ import annotations

from collections.abc import Mapping

from scipy import stats


def bonferroni_critical_tstat(n_tests: int, alpha: float = 0.05) -> float:
    """Two-tailed critical |t| for Bonferroni-adjusted significance.

    Uses standard-normal quantile (``scipy.stats.norm.ppf``) which matches
    the large-n asymptote of the t-distribution — suitable for Layer 2d's
    daily 2009-2026 sample (≈4400 observations, df ≫ any threshold effect).
    """
    if n_tests < 1:
        raise ValueError(f"n_tests must be ≥ 1, got {n_tests}")
    adjusted = alpha / n_tests
    return float(stats.norm.ppf(1 - adjusted / 2))


def apply_bonferroni(
    alpha_tstats: Mapping[str, float],
    n_tests: int,
    alpha: float = 0.05,
) -> dict[str, bool]:
    """Return ``{spec_name: passes_bonferroni}`` for each t-stat.

    Uses absolute value — two-tailed. Callers typically pass
    ``{"H1": carhart_t, "H2": sector_neutral_t}`` and gate on all True.
    """
    threshold = bonferroni_critical_tstat(n_tests, alpha)
    return {name: abs(t) > threshold for name, t in alpha_tstats.items()}


def tstat_to_pvalue(tstat: float) -> float:
    """Two-tailed p-value from a t-statistic (large-n normal approximation)."""
    return float(2.0 * (1.0 - stats.norm.cdf(abs(tstat))))


def fdr_adjusted_pvalues(p_values: list[float]) -> list[float]:
    """Benjamini-Hochberg FDR-adjusted p-values, order preserved.

    For n hypotheses, step-up procedure: sort ascending, adjust by
    ``p_k × n / k``, enforce monotonicity. Returns list aligned to input
    order. Less conservative than Bonferroni; used in Phase 3b only for
    diagnostic reporting (regime sub-analyses), not the decision gate.
    """
    n = len(p_values)
    if n == 0:
        return []
    indexed = sorted(enumerate(p_values), key=lambda pair: pair[1])
    adjusted: list[float] = [0.0] * n
    # Step-up: adjusted_k = min over j>=k of (p_j * n / j), then clamp to 1.
    running_min = 1.0
    for rank in range(n - 1, -1, -1):
        orig_idx, p = indexed[rank]
        candidate = min(p * n / (rank + 1), 1.0)
        running_min = min(running_min, candidate)
        adjusted[orig_idx] = running_min
    return adjusted
