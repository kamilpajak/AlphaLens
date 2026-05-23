"""Shared helpers for the Layer 4 signal modules.

These were previously private to ``insider_signal`` / ``fcff_signal`` /
``valuation_signal`` with cross-module imports of underscore-prefixed
names. Promoting them to a dedicated module keeps the "underscore =
internal" convention honest and gives downstream callers (e.g. a future
sector-relative ranking layer) a single import surface.
"""

from __future__ import annotations

from alphalens_pipeline.scorers.fcff_yield import (
    _TAX_RATE_CEILING,
    _TAX_RATE_FLOOR,
)


def percentile_rank(value: float, peers: list[float]) -> float:
    """Return the ``≤``-percentile of ``value`` within ``peers`` (0..100).

    Includes ``value`` itself in the cohort so a single-element cohort is
    always at the top. Empty peer list → 50.0 ("no information" midpoint).
    """
    if not peers:
        return 50.0
    cohort = peers if value in peers else peers + [value]
    le_count = sum(1 for v in cohort if v <= value)
    return 100.0 * le_count / len(cohort)


def clamp_tax(value: float | None) -> float | None:
    """Clamp a tax rate to the paradigm #13 ``[0, 0.35]`` window.

    Returns ``None`` unchanged so callers can short-circuit on missing
    data. Bounds reused directly from
    :mod:`alphalens_pipeline.scorers.fcff_yield` so they stay in sync
    with the paradigm spec.
    """
    if value is None:
        return None
    if value < _TAX_RATE_FLOOR:
        return _TAX_RATE_FLOOR
    if value > _TAX_RATE_CEILING:
        return _TAX_RATE_CEILING
    return value


__all__ = ["clamp_tax", "percentile_rank"]
