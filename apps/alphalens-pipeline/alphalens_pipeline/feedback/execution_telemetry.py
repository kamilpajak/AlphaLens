"""Execution-quality telemetry → Prometheus gauges (v3 feedback PR-3).

What this is
------------
A thin OBSERVATION layer that turns the EXISTING per-regime execution
aggregation (:func:`execution_modes.recommend_execution_modes`) into
Prometheus textfile gauges + a read-only CLI view. It is the near-term
honest deliverable of the v3 feedback roadmap: shadow-vs-realized realism,
per-regime non-fill, and pooled PnL — observed, not acted on.

Reuse, not duplication
----------------------
This module does NOT re-derive fill_rate / execution-gap / missed-opportunity
/ counts. It calls ``recommend_execution_modes`` and reads the fields off each
:class:`execution_modes.CellRecommendation` (one per present regime plus the
``"pooled"`` summary). The ONE statistic ``execution_modes`` does not expose —
the per-regime + pooled MEAN ``realized_return`` over FILLED rows ("pooled
PnL") — is computed here with the SAME admissibility as ``execution_modes``
(fill_status in {FILLED, UNFILLED} + finite shadow_return; realized counted
only when finite AND on a FILLED row), and the pooled realized mean is taken
over the switchable regimes only, matching the pooled scope in
``execution_modes``.

Gap sign convention
-------------------
``gap_mean`` carries ``rec.observed_execution_gap`` = mean(shadow − realized).
POSITIVE means the real fill did WORSE than the frictionless arrival-price
shadow = execution drag. Negative means the fill beat the shadow.

Orthogonality (hard constraint)
-------------------------------
This telemetry reads ONLY ``(regime, fill_status, shadow_return,
realized_return)`` via :meth:`FeedbackStore.iter_matured_decisions`. It NEVER
reads the ``action`` column or any click data — it is click-independent by
construction.

Observation-only
----------------
There is no control surface here: no re-weighting, no submitter wiring, no
ledger mutation. It emits gauges and prints a table; that is all.
"""

from __future__ import annotations

import math
from typing import TypeIs

from .execution_modes import (
    DEFAULT_POOLED_GATE_N,
    FILLED,
    SWITCHABLE_REGIMES,
    UNFILLED,
    recommend_execution_modes,
)

# The ``emit_domain_metrics`` job name → file ``alphalens_domain_<job>.prom``.
TELEMETRY_JOB = "feedback-execution-telemetry"

# Shared metric-name stem; every gauge below extends it. Kept private so the
# tests reference the SAME constant rather than re-typing the string.
_METRIC_PREFIX = "alphalens_feedback_execution"


def _finite(value: float | None) -> TypeIs[float]:
    """True iff ``value`` is a finite float (mirrors ``execution_modes._finite``).

    ``TypeIs`` (not plain ``bool``) so a guard like ``if not _finite(realized):
    continue`` narrows ``realized`` to ``float`` on the fall-through path.
    """
    return value is not None and math.isfinite(value)


def realized_means(
    rows: list[tuple[str, str, float | None, float | None]],
) -> dict[str, float]:
    """Per-regime + pooled mean ``realized_return`` over admissible FILLED rows.

    The ONE statistic ``execution_modes`` does not expose. Admissibility matches
    ``recommend_execution_modes`` exactly: a row counts only when its fill_status
    is in {FILLED, UNFILLED} and its shadow_return is finite; realized is then
    summed only on FILLED rows with a finite realized_return. The pooled mean is
    over the switchable regimes only (``unknown`` is excluded), the same scope as
    the ``execution_modes`` pooled cell. Returns a regime → mean mapping for every
    regime that has at least one such realized observation, plus ``"pooled"`` when
    the switchable pool has any.
    """
    per_regime: dict[str, list[float]] = {}
    pooled: list[float] = []
    for regime, fill_status, shadow, realized in rows:
        if fill_status not in (FILLED, UNFILLED) or not _finite(shadow):
            continue
        if fill_status != FILLED or not _finite(realized):
            continue
        per_regime.setdefault(regime, []).append(realized)
        if regime in SWITCHABLE_REGIMES:
            pooled.append(realized)
    means = {regime: sum(vals) / len(vals) for regime, vals in per_regime.items() if vals}
    if pooled:
        means["pooled"] = sum(pooled) / len(pooled)
    return means


def build_execution_gauges(
    rows: list[tuple[str, str, float | None, float | None]],
) -> dict[str, float | int]:
    """Build the Prometheus gauge mapping from matured feedback rows.

    ``rows`` is the output of :meth:`FeedbackStore.iter_matured_decisions` —
    ``(regime, fill_status, shadow_return, realized_return)`` for matured,
    de-duplicated decisions. Delegates fill/gap/MO/counts to
    ``recommend_execution_modes`` and adds the per-cell realized-return mean.

    The Prometheus label lives INSIDE the key (textfile-collector exposition
    form), e.g. ``alphalens_feedback_execution_matured_decisions{regime="low"}``.
    The ``regime`` label value comes from ``rec.regime`` so the pooled summary
    appears with ``regime="pooled"``. A stat that is None / non-finite is SKIPPED
    (the key is simply absent) — this function NEVER emits a NaN/inf/None value.
    """
    recs = recommend_execution_modes(rows)
    realized = realized_means(rows)

    gauges: dict[str, float | int] = {}

    def _put(stat: str, regime: str, value: float | int | None) -> None:
        """Add ``<prefix>_<stat>{regime="..."}`` unless ``value`` is non-finite/None."""
        if value is None:
            return
        if isinstance(value, float) and not math.isfinite(value):
            return
        gauges[f'{_METRIC_PREFIX}_{stat}{{regime="{regime}"}}'] = value

    for rec in recs.values():
        regime = rec.regime
        # Counts are always finite ints; emit unconditionally.
        _put("matured_decisions", regime, rec.n)
        _put("filled", regime, rec.n_filled)
        _put("unfilled", regime, rec.n_unfilled)
        # Mean stats are skipped when undefined (None) — see _put.
        _put("fill_rate", regime, rec.fill_rate)
        _put("gap_mean", regime, rec.observed_execution_gap)
        _put("missed_opportunity_mean", regime, rec.missed_opportunity)
        _put("realized_return_mean", regime, realized.get(regime))

    # One unlabelled constant gauge so the dashboard can draw the gate line.
    gauges[f"{_METRIC_PREFIX}_gate_n_threshold"] = float(DEFAULT_POOLED_GATE_N)
    return gauges


def execution_gauges_for_ledger(
    ledger_path,
    *,
    store_cls=None,
) -> dict[str, float | int]:
    """Open the ledger, read matured decisions, and build the gauge mapping.

    ``store_cls`` is injectable for tests; production lazy-imports
    :class:`alphalens_feedback.store.FeedbackStore` INSIDE the function so the
    ``alphalens`` CLI startup path does not pay the sqlite import cost on the
    Layer-1 ``edgar-detect`` cron tick.
    """
    if store_cls is None:
        from alphalens_feedback.store import FeedbackStore

        store_cls = FeedbackStore
    with store_cls.open(ledger_path) as fb:
        rows = fb.iter_matured_decisions()
    return build_execution_gauges(rows)


__all__ = [
    "TELEMETRY_JOB",
    "build_execution_gauges",
    "execution_gauges_for_ledger",
    "realized_means",
]
