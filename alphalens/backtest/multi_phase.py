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

import logging
import math
from collections.abc import Sequence
from typing import Any

logger = logging.getLogger(__name__)

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
    total = len(phase_results)
    for key in _AGGREGATED_KEYS:
        values = [
            float(r[key])
            for r in phase_results
            if key in r and r[key] is not None and not _is_nan(r[key])
        ]
        if not values:
            continue
        if len(values) != total:
            # Per-key independent NaN filtering means cross-metric comparisons
            # may draw from non-overlapping samples. WARNING (not INFO) so it
            # surfaces under Python's default root config — direct script
            # invocations of the audit driver do not configure logging.
            logger.warning(
                "summarise_phase_results: partial coverage on %r — n=%d of %d phases",
                key,
                len(values),
                total,
            )
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

    PASS  — every phase has alpha_t >= 1.5 AND excess_net_ann >= 0
            (this implies mean alpha_t >= 1.5 and mean excess >= 0).
    FAIL  — mean(alpha_t) < 1.0, OR mean excess non-positive, OR majority
            of phases negative on either metric.
    MID   — anything between (mean ≥ 1.0 with positive excess but the gate
            is not uniformly cleared, signalling regime fragility).

    The thresholds match the original gate matrix from
    `project_next_session_edgar_backfill.md`, adapted to require robustness
    across the full set of sampling phases rather than a single point estimate.

    Filtering correctness: drop a phase row when EITHER required metric is
    missing/NaN. Independent per-metric filtering would let the verdict
    pair phase-i of one metric with phase-j of the other in the
    majority-negative count — silently mis-aligning the data.
    """
    valid_phases = [
        r
        for r in phase_results
        if r.get("alpha_t") is not None
        and not _is_nan(r.get("alpha_t"))
        and r.get("excess_net_ann") is not None
        and not _is_nan(r.get("excess_net_ann"))
    ]
    if not valid_phases:
        return "FAIL"
    t_values = [float(r["alpha_t"]) for r in valid_phases]
    excess_values = [float(r["excess_net_ann"]) for r in valid_phases]
    mean_t = sum(t_values) / len(t_values)
    mean_excess = sum(excess_values) / len(excess_values)

    if mean_t < 1.0 or mean_excess <= 0:
        return "FAIL"
    # Count materially-negative phases (alpha_t < 0 OR excess_net_ann < 0).
    # Pairing is now correct because both lists were drawn from `valid_phases`.
    n_neg = sum(1 for t, e in zip(t_values, excess_values, strict=True) if t < 0 or e < 0)
    # If a majority of phases are negative the mean is being pulled by an
    # outlier — distrust the headline.
    if n_neg > len(valid_phases) / 2:
        return "FAIL"
    # `all(t >= 1.5)` mathematically implies `mean_t >= 1.5`, so no separate
    # mean check is needed for the PASS predicate.
    if all(t >= 1.5 for t in t_values) and all(e >= 0 for e in excess_values):
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
