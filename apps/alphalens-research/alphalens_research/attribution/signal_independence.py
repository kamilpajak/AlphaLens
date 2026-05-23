# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownParameterType=false, reportUnknownLambdaType=false
"""Signal-independence pre-screen for compound-experiment design.

Used before registering a multi-component compound test (per ADR 0007 +
session 2026-05-10 plan): verify that the components are sufficiently
orthogonal that combining them carries independent information rather
than just doubling the same signal under a different name.

Rule of thumb (zen review 2026-05-10): if two scorers' cross-sectional
ranks correlate above ρ ≈ 0.5, they share a latent common factor and the
equal-weight compound is just a leveraged bet on that factor — Bonferroni
budget unjustified.

API:
- ``pairwise_rank_ic_correlation(scorer_a_panel, scorer_b_panel)`` —
  per-asof Spearman ρ on the strict-intersection ticker set, pooled
  across asofs.
- ``classify_independence(result)`` — sign-pattern + magnitude
  classification with PROCEED / REJECT / ABORT verdict.

Critical: classifier handles all sign cases of mean ρ (positive
correlated, anti-correlated, near-zero) without conflating them. A
naive |ρ| > 0.5 → REJECT rule would mis-route anti-correlated signals
(which actually indicate sign-flip bug, not redundancy).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, cast

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

_REQUIRED_COLUMNS = frozenset({"asof", "ticker", "score"})

# Decision thresholds (locked per session 2026-05-10 plan; zen-validated).
_ORTHOGONAL_BAND_LOW = -0.5
_ORTHOGONAL_BAND_HIGH = 0.5
_DEFAULT_MIN_INTERSECTION = 5
_DEFAULT_MIN_ASOFS = 5


@dataclass(frozen=True)
class PairwiseRhoResult:
    """Per-asof Spearman ρ between two scorers + pooled summary."""

    per_asof_rhos: pd.Series
    """Spearman ρ per asof, NaN when intersection too small."""

    mean_rho: float
    """Mean of valid (non-NaN) per-asof ρs."""

    t_stat: float
    """mean_rho / (std_rho / sqrt(n_valid)) — IC-style significance."""

    n_asofs_total: int
    n_asofs_with_valid_rho: int


@dataclass(frozen=True)
class IndependenceVerdict:
    """Classification + GO/NO-GO for compound registration."""

    mean_rho: float
    classification: str  # "orthogonal" / "REDUNDANT (latent common factor)" / "DEGENERATE (sign-flip suspected)"
    proceed: bool | None  # True / False (REJECT) / None (ABORT, investigate)
    rationale: str


def _validate_panel(df: pd.DataFrame, name: str) -> None:
    missing = _REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(
            f"scorer panel '{name}' missing required columns {sorted(missing)} "
            f"(expected: {sorted(_REQUIRED_COLUMNS)})"
        )


def _per_asof_rho(
    a_scores: pd.Series,
    b_scores: pd.Series,
    min_intersection: int,
) -> float:
    """Spearman ρ on intersection of two score Series indexed by ticker.

    Returns NaN if the intersection (after dropping NaN in either) has
    fewer than min_intersection observations OR either series is constant.
    """
    df = pd.DataFrame({"a": a_scores, "b": b_scores}).dropna()
    if len(df) < min_intersection:
        return float("nan")
    if df["a"].nunique() < 2 or df["b"].nunique() < 2:
        return float("nan")
    result = spearmanr(df["a"], df["b"])
    rho: Any = result[0]
    if rho is None or np.isnan(rho):
        return float("nan")
    return float(rho)


def pairwise_rank_ic_correlation(
    scorer_a_panel: pd.DataFrame,
    scorer_b_panel: pd.DataFrame,
    *,
    min_intersection: int = _DEFAULT_MIN_INTERSECTION,
    min_asofs: int = _DEFAULT_MIN_ASOFS,
) -> PairwiseRhoResult:
    """Per-asof Spearman ρ between two scorer outputs.

    Each panel is long-format ``DataFrame[asof, ticker, score]``. For each
    common asof, compute Spearman ρ on the strict-intersection ticker set
    (both scorers non-NaN). Pool ρ series; report mean and IC-style t-stat.

    Parameters
    ----------
    scorer_a_panel, scorer_b_panel
        Long-format scorer outputs. Required columns: asof, ticker, score.
    min_intersection
        Minimum number of common tickers per asof required to compute ρ
        (asofs below threshold contribute NaN, dropped from mean).
    min_asofs
        Minimum number of asofs with valid ρ required for the result to
        be trustworthy. Below → ValueError.
    """
    _validate_panel(scorer_a_panel, "scorer_a_panel")
    _validate_panel(scorer_b_panel, "scorer_b_panel")

    # Build per-asof ticker→score lookup
    a_by_asof = {
        asof: group.set_index("ticker")["score"]
        for asof, group in scorer_a_panel.groupby("asof", observed=True)
    }
    b_by_asof = {
        asof: group.set_index("ticker")["score"]
        for asof, group in scorer_b_panel.groupby("asof", observed=True)
    }

    common_asofs = sorted(cast(set[Any], set(a_by_asof) & set(b_by_asof)))
    if not common_asofs:
        raise ValueError("no common asofs between scorer_a_panel and scorer_b_panel")

    rhos = pd.Series(
        [
            _per_asof_rho(a_by_asof[asof], b_by_asof[asof], min_intersection)
            for asof in common_asofs
        ],
        index=pd.Index(common_asofs, name="asof"),
        name="rho",
    )
    valid = rhos.dropna()
    n_valid = len(valid)

    if n_valid < min_asofs:
        raise ValueError(
            f"need at least {min_asofs} asofs with valid ρ, got {n_valid} "
            f"(of {len(common_asofs)} common asofs; min_intersection={min_intersection})"
        )

    mean_rho = float(valid.mean())
    std_rho = float(valid.std(ddof=1))
    t_stat = float("inf") if std_rho == 0 else mean_rho / (std_rho / math.sqrt(n_valid))

    return PairwiseRhoResult(
        per_asof_rhos=rhos,
        mean_rho=mean_rho,
        t_stat=t_stat,
        n_asofs_total=len(common_asofs),
        n_asofs_with_valid_rho=n_valid,
    )


def classify_independence(result: PairwiseRhoResult) -> IndependenceVerdict:
    """Classify pairwise ρ result + decide PROCEED / REJECT / ABORT.

    Decision tree:
    - mean_rho ∈ [-0.5, 0.5] → orthogonal → PROCEED.
    - mean_rho > 0.5 → REDUNDANT (latent common factor) → REJECT (proceed=False).
    - mean_rho < -0.5 → DEGENERATE (sign-flip suspected) → ABORT (proceed=None).
    """
    rho = result.mean_rho
    if _ORTHOGONAL_BAND_LOW <= rho <= _ORTHOGONAL_BAND_HIGH:
        return IndependenceVerdict(
            mean_rho=rho,
            classification="orthogonal",
            proceed=True,
            rationale=(
                f"mean ρ = {rho:+.3f} ∈ [{_ORTHOGONAL_BAND_LOW:+.1f}, {_ORTHOGONAL_BAND_HIGH:+.1f}]. "
                f"Components carry independent information; equal-weight compound is justified."
            ),
        )

    if rho > _ORTHOGONAL_BAND_HIGH:
        return IndependenceVerdict(
            mean_rho=rho,
            classification="REDUNDANT (latent common factor)",
            proceed=False,
            rationale=(
                f"mean ρ = {rho:+.3f} > {_ORTHOGONAL_BAND_HIGH:+.1f}. Components share a latent common "
                f"factor; equal-weight compound becomes a leveraged bet on that factor, not "
                f"diversification. Bonferroni cost unjustified."
            ),
        )

    # rho < _ORTHOGONAL_BAND_LOW
    return IndependenceVerdict(
        mean_rho=rho,
        classification="DEGENERATE (sign-flip suspected)",
        proceed=None,
        rationale=(
            f"mean ρ = {rho:+.3f} < {_ORTHOGONAL_BAND_LOW:+.1f}. Strong anti-correlation suggests "
            f"sign-flip in one of the scorers (accidental negation). Investigate before proceeding."
        ),
    )
