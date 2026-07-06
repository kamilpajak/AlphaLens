"""ATR-soft-tilt selection score: deprioritize high-ATR (extended/volatile) names.

``selection_score = layer4_weighted_score − atr_penalty(technical_atr_pct)``.

``atr_penalty`` is a function of ``technical_atr_pct`` only, which the score stage
derives from ``(ticker, asof)`` OHLCV — it carries NO theme input. So for a ticker
that hits several themes, every theme-row gets the SAME penalty, and subtracting a
per-ticker constant cannot reorder that ticker's theme-rows. That is the invariant
the EDGE test-set safety rests on: ``_sort_and_dedup_for_brief``'s
``drop_duplicates(keep="first")`` still keeps the same row, so monitored outcomes
are byte-identical pre/post. A future theme-dependent term folded in here WOULD
break it — keep selection_score per-ticker-constant in its non-layer4 part.

Breakpoints are FROZEN from the historical signal panel (N=328, 2026-06-25):
p67=5.77 (top-tercile onset), p90=8.37 (deep loser tail, car_5 −7.3%). PROVISIONAL
— a single 4-week in-sample window. Recalibrating bumps ``SCORER_CONFIG_VERSION``,
which partitions the EDGE cohort (old briefs stay a frozen pool).
Re-validated 2026-07-06 on the refreshed panel (N=415 plannable): fresh p67/p90 =
5.71/8.25, max |score delta| 0.044, ZERO top-3 churn over 38 episode-days —
constants and version string deliberately unchanged (a bump would fragment the
EDGE cohort for no ordering change). A scorer-v2 extension axis was calibrated
and REJECTED the same day; the single pre-registered revisit candidate lives in
docs/research/selection_score_v2_ext_tilt_decision_2026_07_06.md.

Design: docs/superpowers/specs/2026-06-25-selection-score-atr-tilt-design.md
"""

from __future__ import annotations

import math

ATR_RAMP_LO = 5.77  # panel p67 — penalty onset; low+mid terciles untouched
ATR_RAMP_HI = 8.37  # panel p90 — full penalty in the loser tail
LAMBDA = 1.0  # full penalty = one layer4 point at/above p90

# Poolability key. Encodes the scorer identity + every parameter that changes the
# ordering, so two outcomes are only pooled when produced by the same policy.
SCORER_CONFIG_VERSION = "scorer-v1-atrtilt-lam1.0-lo5.77-hi8.37"


def atr_penalty(atr_pct: float | None) -> float:
    """Ramp penalty in ``[0, LAMBDA]``: 0 at/below p67, full at/above p90, linear
    between. Missing / NaN / non-positive ATR → 0 (never punish unknown ATR)."""
    if atr_pct is None:
        return 0.0
    try:
        x = float(atr_pct)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(x) or x <= ATR_RAMP_LO:
        return 0.0
    if x >= ATR_RAMP_HI:
        return LAMBDA
    return LAMBDA * (x - ATR_RAMP_LO) / (ATR_RAMP_HI - ATR_RAMP_LO)


def selection_score(layer4_weighted_score: float, atr_pct: float | None) -> float:
    """Primary brief sort key: layer4 minus the per-ticker ATR penalty."""
    return float(layer4_weighted_score) - atr_penalty(atr_pct)
