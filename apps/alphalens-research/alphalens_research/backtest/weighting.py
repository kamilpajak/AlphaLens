"""Position-weighting schemes for top-N portfolios.

Equal-weight is the default for textbook backtests, but the thematic-momentum
literature (Perplexity research, ARK operationalisation) shows that
**conviction-scaling** — overweighting the highest-scored names — delivers
higher Sharpe without a dramatically larger drawdown.

Available schemes:
- `equal` — each position = 1/N (baseline)
- `linear` — weight decreases linearly from top (2.0/N) to bottom (0.2/N),
  then normalised to sum to 1.0
- `conviction` — three tiers: top 1/3 × 2.0, mid 1/3 × 1.0, bottom 1/3 × 0.5,
  normalised

All schemes return weights summing to 1.0 (no leverage).
"""

from __future__ import annotations

from typing import Literal

import numpy as np

WeightingScheme = Literal["equal", "linear", "conviction"]


def compute_position_weights(n: int, scheme: WeightingScheme = "equal") -> np.ndarray:
    """Return a weights array [w_1, w_2, ..., w_n] with w_i > w_{i+1} (top-down).

    Weights sum to 1.0. The rank-1 position receives the largest weight.
    """
    if n <= 0:
        return np.array([], dtype=float)

    if scheme == "equal":
        return np.full(n, 1.0 / n)

    if scheme == "linear":
        # Linear descent: top = 2.0 units, bottom = 0.2 units, interpolated.
        raw = np.linspace(2.0, 0.2, n)
        return raw / raw.sum()

    if scheme == "conviction":
        # Three tiers (rounded): top ceil(n/3) × 2.0, middle floor(n/3) × 1.0,
        # bottom rest × 0.5. For small n these collapse gracefully.
        top = max(1, (n + 2) // 3)  # rounded up third
        bottom = max(1, n // 3)  # rounded down third
        middle = n - top - bottom
        raw = np.concatenate(
            [
                np.full(top, 2.0),
                np.full(max(0, middle), 1.0),
                np.full(bottom, 0.5),
            ]
        )
        # Safety for tiny n that collapsed to single tier.
        raw = raw[:n]
        if raw.sum() == 0:
            return np.full(n, 1.0 / n)
        return raw / raw.sum()

    raise ValueError(f"unknown weighting scheme: {scheme!r}")


def weighted_return(returns: np.ndarray, weights: np.ndarray) -> float:
    """Portfolio return = sum(weights × returns), assuming both arrays are
    aligned per position rank (rank 1 = highest score).

    NaN returns are treated as 0 (stocks that delisted mid-hold). Weights are
    re-normalised after masking NaNs.
    """
    if len(returns) == 0 or len(weights) == 0:
        return 0.0
    if len(returns) != len(weights):
        raise ValueError(f"length mismatch: returns={len(returns)}, weights={len(weights)}")
    mask = ~np.isnan(returns)
    if not mask.any():
        return 0.0
    valid_w = weights[mask]
    if valid_w.sum() == 0:
        return 0.0
    # np.asarray(..., dtype=float) pins the element type on both operands so
    # np.sum stays statically typed under numpy 2.5's stubs (untyped ndarray
    # arithmetic otherwise resolves to an unknown type in strict mode).
    ret = np.asarray(returns[mask], dtype=np.float64)
    w = np.asarray(valid_w, dtype=np.float64)
    return float((ret * (w / w.sum())).sum())
