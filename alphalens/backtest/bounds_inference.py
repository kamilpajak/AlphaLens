"""Andrews-Manski (2010) partial-identification bounds for unbiased α-tstat.

When a Carhart-α regression is run on a universe known to be biased
upward by a documented but bounded amount (e.g. survivorship-bias survival
cohort), the observed t-statistic and annualised excess return both
overstate the unbiased counterparts. Andrews-Manski bounds inference
formalises the resulting partial identification: instead of reporting a
point estimate, the unbiased parameter lies inside an interval whose
endpoints come from the upper / lower edges of the assumed bias range.

For a frozen scorer, the bias subtracts directly from the annualised α
(in % terms): ``α_unbiased_pct = α_observed_pct − B`` for ``B ∈ [B_L,
B_U]``. To translate to t-stat space, we use the regression's reported
standard error of the α estimate ``α_pct_se`` (annualised %, same
unit as α_pct):
    ``α_unbiased_t = α_observed_t − B / α_pct_se``.
The lower-bound is reached at ``B = B_U``, the upper-bound at
``B = B_L``.

The pre-reg
``params_v9d_retrospective_pre_2018_2026_05_05.json`` locks
``[B_L, B_U] = [1.0%, 2.0%]`` /y for the 8-year-survivor cohort. This
helper is used by the verdict driver to report bounds CI alongside the
point estimate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class BoundsResult:
    """Andrews-Manski-style bounds in both t-stat and annualised % space."""

    alpha_t_lower: float
    alpha_t_upper: float
    alpha_pct_lower: float
    alpha_pct_upper: float
    bias_lower_pct: float
    bias_upper_pct: float

    @property
    def lower_bound_excludes_zero(self) -> bool:
        """True iff the lower-bound t-stat is strictly positive — the
        decision-grade signal that bias-adjusted α is ≥ 0 across the
        entire pre-registered bias range."""
        return self.alpha_t_lower > 0.0


def andrews_manski_bounds(
    *,
    alpha_t: float,
    alpha_pct: float,
    alpha_pct_se: float,
    bias_lower_pct: float,
    bias_upper_pct: float,
    consistency_tol: float | None = None,
) -> BoundsResult:
    """Compute partial-identification bounds for unbiased α.

    ``consistency_tol`` (when not None) verifies that the observed
    ``alpha_t`` is consistent with ``alpha_pct / alpha_pct_se`` to within
    the given relative tolerance. This guards against the caller passing
    inconsistent regression outputs (e.g. HAC SE for one and OLS SE for
    the other). Disabled by default."""
    for name, value in (
        ("alpha_t", alpha_t),
        ("alpha_pct", alpha_pct),
        ("alpha_pct_se", alpha_pct_se),
        ("bias_lower_pct", bias_lower_pct),
        ("bias_upper_pct", bias_upper_pct),
    ):
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite, got {value!r}")
    if alpha_pct_se <= 0.0:
        raise ValueError(
            f"alpha_pct_se must be > 0 (got {alpha_pct_se}); cannot map bias "
            "to t-stat space without a positive SE."
        )
    if bias_upper_pct < bias_lower_pct:
        raise ValueError(
            f"bias_upper_pct ({bias_upper_pct}) must be ≥ bias_lower_pct ({bias_lower_pct})."
        )
    if consistency_tol is not None:
        implied_t = alpha_pct / alpha_pct_se
        denom = max(abs(alpha_t), 1e-12)
        rel_diff = abs(implied_t - alpha_t) / denom
        if rel_diff > consistency_tol:
            raise ValueError(
                f"alpha_t={alpha_t} inconsistent with alpha_pct/alpha_pct_se="
                f"{implied_t:.4f} (relative diff {rel_diff:.4f} > tol "
                f"{consistency_tol})."
            )

    alpha_pct_lower = alpha_pct - bias_upper_pct
    alpha_pct_upper = alpha_pct - bias_lower_pct
    alpha_t_lower = alpha_t - bias_upper_pct / alpha_pct_se
    alpha_t_upper = alpha_t - bias_lower_pct / alpha_pct_se

    return BoundsResult(
        alpha_t_lower=alpha_t_lower,
        alpha_t_upper=alpha_t_upper,
        alpha_pct_lower=alpha_pct_lower,
        alpha_pct_upper=alpha_pct_upper,
        bias_lower_pct=bias_lower_pct,
        bias_upper_pct=bias_upper_pct,
    )


__all__ = ["BoundsResult", "andrews_manski_bounds"]
