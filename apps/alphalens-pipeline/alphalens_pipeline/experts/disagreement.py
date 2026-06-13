"""Panel-level disagreement scalar — the log-now calibration corpus (PR-8a).

The expert panel exists to surface DISAGREEMENT between orthogonal lenses (Buffett
value/quality vs O'Neil momentum). This module records the raw disagreement as a
persisted scalar at decision time so the deferred Expert×EDGE study (N≥30, ~2026-09+)
can decide EMPIRICALLY whether it carries signal — the #522-#526 log-now pattern.

CRITICAL — manufactured-authority discipline (epic blocker #6):
``expert_spread`` is the absolute difference of two UN-CALIBRATED heuristic scores
built from DIFFERENT inputs, weights, and clip caps (``buffett_quality_score`` and
``oneil_score``). The two are NOT known to be commensurable. The gap is recorded
RAW so the deferred study can test it; it is NOT a calibrated disagreement
magnitude, and nothing in selection / ordering may read it (display-only — the
PR-6 sort allowlist already keeps both scores out of the brief sort, and the panel
scalars inherit that status). The display BANDS (CONSENSUS/MIXED/SPLIT) live only
in the SPA drawer (PR-8b), are unvalidated, and are folded into
``PANEL_CONFIG_VERSION`` — the analyst correlates the raw scalar, never the bucket.

N>2 generalization (documented, NOT wired): dispersion would become the population
standard deviation over present scores; for exactly 2 present scores ``pstdev`` =
``abs(a−b)/2`` ≠ the range used here, so a 3rd expert is a NEW ``PANEL_CONFIG_VERSION``
(``panel-v2-pstdev-3x``) and rows are only poolable within one config_version.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

import pandas as pd

# Pins, as one opaque token, every hand-chosen choice that determines how the raw
# spread was produced AND how the SPA buckets it for display: the formula family +
# arity (range over exactly 2 experts), the participating expert set, and the
# drawer band cutoffs. Bump on any change (3rd expert / retuned bands) so the
# calibration corpus knows the rows are not comparable across versions.
PANEL_CONFIG_VERSION = "panel-v1-absdiff-2x"

# The two flat columns this step stamps onto every scored row.
PANEL_COLUMNS: tuple[str, ...] = ("expert_spread", "panel_config_version")

# The per-expert composite columns the spread is computed over.
_SCORE_COLUMNS: tuple[str, ...] = ("buffett_quality_score", "oneil_score")


def compute_spread(scores: Mapping[str, float | None]) -> float | None:
    """The raw disagreement scalar over the present expert scores, or ``None``.

    Returns ``None`` when FEWER THAN TWO scores are present (finite) — a spread of
    ``0.0`` is a real value ("the lenses landed on the same grade"), so a
    missing-input case must be a distinct token (``None``), never ``0.0``, or it
    corrupts the calibration corpus. For the current two-expert panel the spread is
    the range ``max − min`` of the present scores, i.e. ``abs(buffett − oneil)``.
    """
    present = [float(v) for v in scores.values() if v is not None and math.isfinite(v)]
    if len(present) < 2:
        return None
    return max(present) - min(present)


def enrich(frame: pd.DataFrame) -> pd.DataFrame:
    """Return ``frame`` with ``expert_spread`` + ``panel_config_version`` appended.

    Pure frame arithmetic over ``buffett_quality_score`` + ``oneil_score`` (already
    stamped by the Buffett + O'Neil enrich steps) — no store, no network.
    ``panel_config_version`` is stamped UNCONDITIONALLY on every row (including
    null-spread rows) so the corpus records which config WOULD have produced the
    spread. With no rows, both columns are still added (zero length) for a stable
    parquet schema.
    """
    out = frame.copy()
    n = len(out)

    if n == 0:
        out["expert_spread"] = pd.Series([], dtype="float64")
        out["panel_config_version"] = pd.Series([], dtype="object")
        return out

    spreads: list[float | None] = []
    for _, row in out.iterrows():
        scores = {
            col: (float(row[col]) if col in out.columns and pd.notna(row[col]) else None)
            for col in _SCORE_COLUMNS
        }
        spreads.append(compute_spread(scores))

    # Explicit float64 (None -> NaN) so an all-None column keeps a stable dtype;
    # panel_config_version is the same opaque string on every row.
    out["expert_spread"] = pd.Series(spreads, index=out.index, dtype="float64")
    out["panel_config_version"] = PANEL_CONFIG_VERSION
    return out


__all__ = ["PANEL_COLUMNS", "PANEL_CONFIG_VERSION", "compute_spread", "enrich"]
