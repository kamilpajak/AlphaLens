"""Multi-phase aggregator — collapse phase-aliasing in strided backtests.

`BacktestEngine.rebalance_stride > 1` samples 1-in-stride trading days as
rebalances. Different phases (start-of-stride offsets) sample disjoint
trading days, producing wildly different point-estimate Sharpes for the
same strategy on the same period (30-77pp/y swings observed; see
`docs/research/methodology_audit_2026_04_29.md`).

Aggregating across all `stride` phases gives a stable distributional
estimate. This module is the small library piece; experiment scripts call
`summarise_phase_results(...)` after looping engine runs over
`phase_offset = 0..stride-1`.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

# Headline metrics aggregated across phases. Add new keys here; the helper
# walks them defensively so missing keys never raise.
_AGGREGATED_KEYS: tuple[str, ...] = (
    "sharpe_gross",
    "sharpe_net",
    "excess_gross_ann",
    "excess_net_ann",
    "alpha_t",
)


def summarise_phase_results(
    phase_results: Sequence[dict[str, Any]],
) -> dict[str, dict[str, float]]:
    """For each headline metric, report mean / std / min / max / n across phases.

    Returns ``{metric_name: {"mean": ..., "std": ..., "min": ..., "max": ..., "n": ...}}``
    so each metric's distribution can be inspected independently.
    """
    summary: dict[str, dict[str, float]] = {}
    for key in _AGGREGATED_KEYS:
        values = [
            float(r[key])
            for r in phase_results
            if key in r and r[key] is not None and not _is_nan(r[key])
        ]
        if not values:
            continue
        summary[key] = {
            "mean": sum(values) / len(values),
            "std": _stdev(values),
            "min": min(values),
            "max": max(values),
            "n": len(values),
        }
    return summary


def robust_verdict(phase_results: Sequence[dict[str, Any]]) -> str:
    """Decision-gate verdict that accounts for phase-dispersion.

    PASS  — every phase has alpha_t >= 1.5 AND excess_net_ann >= 0.
    MID   — mean(alpha_t) >= 1.5 AND mean(excess_net_ann) > 0, but at least
            one phase is materially negative (signals regime fragility).
    FAIL  — anything else, including mean(alpha_t) < 1.0, or mean excess
            non-positive, or majority of phases negative.

    The thresholds match the original gate matrix from
    `project_next_session_edgar_backfill.md` adapted to require robustness
    across the full set of sampling phases rather than a single point estimate.
    """
    t_values = [
        float(r["alpha_t"])
        for r in phase_results
        if "alpha_t" in r and r["alpha_t"] is not None and not _is_nan(r["alpha_t"])
    ]
    excess_values = [
        float(r["excess_net_ann"])
        for r in phase_results
        if "excess_net_ann" in r
        and r["excess_net_ann"] is not None
        and not _is_nan(r["excess_net_ann"])
    ]
    if not t_values or not excess_values:
        return "FAIL"
    mean_t = sum(t_values) / len(t_values)
    mean_excess = sum(excess_values) / len(excess_values)

    if mean_t < 1.0 or mean_excess <= 0:
        return "FAIL"
    # Count materially-negative phases (alpha_t < 0 OR excess_net_ann < 0).
    n_neg = sum(1 for t, e in zip(t_values, excess_values, strict=False) if t < 0 or e < 0)
    # If a majority of phases are negative the mean is being pulled by an
    # outlier — distrust the headline.
    if n_neg > len(t_values) / 2:
        return "FAIL"
    all_phases_pass = all(t >= 1.5 for t in t_values) and all(e >= 0 for e in excess_values)
    if all_phases_pass and mean_t >= 1.5:
        return "PASS"
    return "MID"


def _stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = sum(values) / len(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def _is_nan(x: Any) -> bool:
    try:
        return math.isnan(float(x))
    except (TypeError, ValueError):
        return False
