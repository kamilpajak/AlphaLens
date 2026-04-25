"""Schematy wag pozycji dla portfolio top-N.

Equal-weight to domyślny wybór dla większości szkolnych backtestów, ale
literatura o thematic momentum (Perplexity research, ARK operationalization)
pokazuje że **conviction-scaling** — overweight najwyżej oceniane nazwy — daje
wyższy Sharpe bez dramatycznie większego drawdown.

Dostępne schematy:
- `equal` — każda pozycja = 1/N (baseline)
- `linear` — waga maleje liniowo od top (2.0/N) do bottom (0.2/N), potem normalizowane do 1.0
- `conviction` — trzy segmenty: top 1/3 × 2.0, mid 1/3 × 1.0, bottom 1/3 × 0.5, znormalizowane

Wszystkie schematy zwracają wagi sumujące do 1.0 (bez leverage).
"""

from __future__ import annotations

from typing import Literal

import numpy as np

WeightingScheme = Literal["equal", "linear", "conviction"]


def compute_position_weights(n: int, scheme: WeightingScheme = "equal") -> np.ndarray:
    """Zwróć tablicę wag [w_1, w_2, ..., w_n] gdzie w_i > w_{i+1} (top-down).

    Wagi sumują się do 1.0. Pozycja rank 1 dostaje największą wagę.
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
    """Zwrot portfela = suma(wagi × zwroty) przy założeniu że obie tablice są
    aligned per position rank (rank 1 = najwyższy score).

    NaN w returns traktowane jako 0 (stocki które się zdelisowały mid-hold).
    Wagi są re-normalizowane po maskowaniu NaN.
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
    return float(np.sum(returns[mask] * (valid_w / valid_w.sum())))
