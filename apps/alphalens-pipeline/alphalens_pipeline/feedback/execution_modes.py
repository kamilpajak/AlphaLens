"""Execution-mode gating recommendation (Track A v2 PR-4).

What this answers
-----------------
For each market-regime bucket, should the paper harness switch its entries
from LIMIT to MARKET? A limit entry saves the spread but is adversely selected
and sometimes never fills (the candidate ran away); a market entry always fills
near the arrival price but pays the §6 slippage drag. This module derives the
Perold implementation-shortfall break-even per regime cell from the matured
feedback ledger and returns a recommendation per cell.

It is INTENTIONALLY INERT today
-------------------------------
This is a read-only RECOMMENDATION, NOT a control surface. It NEVER mutates the
ledger and NEVER touches the paper submitter. Wiring the live entry order to
MARKET is deferred (PR-5), gated on a production-validated sample, because a
code path that flips a live order to MARKET is *execution*, which the project's
``capital_deploy_clause`` structurally blocks. The break-even logic ships now so
the evidence shape is captured from day one (vision §8 "design-now build-later")
and so the human reading ``alphalens feedback execution-modes`` sees what the
loop *would* recommend once the data clears the gate.

Two hard gates keep it honest below threshold (vision §6/§8: "≥50 sample +
shrinkage", "pool first; regime-split only with shrinkage"):

* LEVEL 1 (program): pooled n < ``pooled_gate_n`` (50) → every cell returns
  ``limit``. The break-even is not even evaluated. Matured n today is far below
  50, so PR-4 ships with this firing for every real cell.
* LEVEL 2 (cell floor): even past the pooled gate, a regime cell with its own
  n < ``cell_floor_n`` (30) does NOT act on its own thin estimate — it adopts
  the pooled recommendation.

Every ambiguous / zero-denominator / non-finite / below-gate path resolves to
``limit`` (the §6-safe default): "limit→market is NOT a free fix", the burden of
proof is on the switch.

Gating dimension
----------------
REGIME ONLY ({low, mid, high}; ``unknown`` is never switchable; ``pooled`` is
the summary + the thin-cell fallback). High-vol's larger missed-opportunity is
captured automatically by the regime-conditional statistic, no special case.
Theme / template conditioning is deferred: ``template_id`` is not on the
``decisions`` row and ``theme`` is too high-cardinality for any sample floor.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TypeIs

# Regime buckets that can be switched to MARKET. ``unknown`` is excluded from
# both the pooled gate and any switch (its VIX bucket is non-actionable). Order
# is fixed for deterministic output.
SWITCHABLE_REGIMES = ("low", "mid", "high")
UNKNOWN_REGIME = "unknown"
POOLED_KEY = "pooled"

# Fill-status values the estimator acts on. PARTIAL is dropped (its blended
# economics are ambiguous between filled and unfilled).
FILLED = "FILLED"
UNFILLED = "UNFILLED"

# Defaults — the SINGLE source of truth. The CLI references these (it does not
# re-declare literals) so there is no drift hazard.
DEFAULT_POOLED_GATE_N = 50
DEFAULT_CELL_FLOOR_N = 30
DEFAULT_SHRINKAGE_K = 20.0
DEFAULT_DEAD_BAND = 0.0


@dataclass(frozen=True)
class CellRecommendation:
    """One regime cell's (or the pooled summary's) execution-mode verdict."""

    regime: str  # low | mid | high | unknown | pooled
    n: int  # actionable sample = n_filled + n_unfilled
    n_filled: int
    n_unfilled: int
    n_gap: int  # FILLED rows with a finite realized_return (backs the gap stat)
    fill_rate: float | None
    missed_opportunity: float | None  # raw cell mean shadow_return over UNFILLED
    missed_opportunity_shrunk: float | None  # MO* used in the inequality
    observed_execution_gap: float | None  # raw mean(shadow − realized) over n_gap FILLED
    expected_market_impact: float | None  # MI* = max(g*, 0)
    shrinkage_weight: float  # w = n/(n+K) for the cell (1.0 for pooled)
    switch_margin: float | None  # shrunk Δ; None when any input undefined
    recommended_mode: str  # "limit" | "market"
    gated_reason: str


def _finite(value: float | None) -> TypeIs[float]:
    """True iff ``value`` is a finite float. ``TypeIs`` narrows in both branches."""
    return value is not None and math.isfinite(value)


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _floor0(value: float | None) -> float | None:
    """Market-impact floor: ``max(g, 0)``; None when ``g`` is undefined.

    A negative execution gap (filled limit did worse than arrival = fill-side
    adverse selection) must NOT manufacture a negative impact that makes
    switching look free — floor it at 0.
    """
    return max(value, 0.0) if _finite(value) else None


def _shrink(cell_val: float | None, pool_val: float | None, n_eff: int, k: float) -> float | None:
    """Empirical-Bayes shrink of a cell statistic toward its pooled counterpart.

    ``θ* = w·θ_cell + (1−w)·θ_pool`` with ``w = n_eff/(n_eff+k)`` — a thin cell
    leans on the pool. Returns None when the cell statistic itself is undefined
    (nothing to estimate); falls back to the cell value when the pool target is
    undefined (nothing to shrink toward).
    """
    if not _finite(cell_val):
        return None
    if not _finite(pool_val):
        return cell_val
    w = n_eff / (n_eff + k)
    return w * cell_val + (1.0 - w) * pool_val


@dataclass(frozen=True)
class _CellStats:
    n_filled: int
    n_unfilled: int
    n_gap: int
    fill_rate: float | None
    missed_opportunity: float | None  # raw MO
    observed_execution_gap: float | None  # raw g

    @property
    def n(self) -> int:
        return self.n_filled + self.n_unfilled


def _cell_stats(rows: list[tuple[str, float, float | None]]) -> _CellStats:
    """Compute raw statistics from admissible ``(fill_status, shadow, realized)``.

    Caller guarantees fill_status in {FILLED, UNFILLED} and shadow finite.
    realized backs the gap only when finite.
    """
    unfilled_shadow = [shadow for fs, shadow, _ in rows if fs == UNFILLED]
    filled = [(shadow, realized) for fs, shadow, realized in rows if fs == FILLED]
    gap = [shadow - realized for shadow, realized in filled if _finite(realized)]
    n_filled = len(filled)
    n_unfilled = len(unfilled_shadow)
    n = n_filled + n_unfilled
    return _CellStats(
        n_filled=n_filled,
        n_unfilled=n_unfilled,
        n_gap=len(gap),
        fill_rate=(n_filled / n) if n else None,
        missed_opportunity=_mean(unfilled_shadow),
        observed_execution_gap=_mean(gap),
    )


def _break_even(
    fill_rate_star: float | None,
    mo_star: float | None,
    mi_star: float | None,
) -> float | None:
    """Shrunk Δ = (1 − fill_rate*)·MO* − MI* − fill_rate*·max(g*, 0).

    With MI* = max(g*, 0) the FILLED term is ≤ 0, so the only positive driver is
    the non-fill recovery ``(1 − fill_rate*)·MO*``. Returns None when any input
    is undefined.
    """
    if not _finite(fill_rate_star):
        return None
    if not _finite(mo_star):
        return None
    if not _finite(mi_star):
        return None
    return (1.0 - fill_rate_star) * mo_star - mi_star - fill_rate_star * mi_star


def _verdict_from_stats(
    *,
    fill_rate_star: float | None,
    mo_star: float | None,
    mi_star: float | None,
    dead_band: float,
) -> tuple[float | None, str, str]:
    """Map shrunk stats to (switch_margin, recommended_mode, gated_reason).

    Used for both the pooled row and a past-the-floor regime cell. The burden of
    proof is on the switch: every undefined path resolves to ``limit``.
    """
    if mo_star is None:
        return None, "limit", "no_unfilled_nothing_to_recover"
    if mi_star is None:
        return None, "limit", "no_filled_cannot_price_impact"
    if mo_star < 0:
        return None, "limit", "negative_missed_opportunity_limit_correct"
    margin = _break_even(fill_rate_star, mo_star, mi_star)
    if margin is None:
        return None, "limit", "non_finite_data_abstain"
    if margin > dead_band:
        return margin, "market", f"switch_breakeven_passed(margin={margin:.4f})"
    return margin, "limit", "no_positive_margin"


def recommend_execution_modes(
    rows: list[tuple[str, str, float | None, float | None]],
    *,
    pooled_gate_n: int = DEFAULT_POOLED_GATE_N,
    cell_floor_n: int = DEFAULT_CELL_FLOOR_N,
    shrinkage_k: float = DEFAULT_SHRINKAGE_K,
    dead_band: float = DEFAULT_DEAD_BAND,
) -> dict[str, CellRecommendation]:
    """Per-regime LIMIT→MARKET recommendation from matured feedback rows.

    ``rows`` = ``(regime, fill_status, shadow_return, realized_return)`` for
    MATURED, de-duplicated decisions (one row per economic ticker-day outcome —
    the caller's :meth:`FeedbackStore.iter_matured_decisions` does the dedup).
    Pure: no I/O, no clock, no DB; deterministic. Returns a mapping keyed by each
    regime present plus a ``"pooled"`` summary. Safe-default ``"limit"`` for
    every ambiguous / below-gate / non-finite path.
    """
    # Admissibility: actionable fill status + finite shadow_return. PARTIAL and
    # non-finite-shadow rows are dropped entirely (counted nowhere). The explicit
    # annotation pins shadow to float for the downstream stats.
    admissible: list[tuple[str, str, float, float | None]] = [
        (regime, fs, shadow, realized)
        for (regime, fs, shadow, realized) in rows
        if fs in (FILLED, UNFILLED) and _finite(shadow)
    ]

    labelled = [
        (fs, shadow, realized)
        for regime, fs, shadow, realized in admissible
        if regime in SWITCHABLE_REGIMES
    ]
    pooled = _cell_stats(labelled)
    n_pool = pooled.n
    below_gate = n_pool < pooled_gate_n
    pooled_mi = _floor0(pooled.observed_execution_gap)

    # Pooled summary row: raw stats (no self-shrink, w=1.0). Drives the thin-cell
    # fallback recommendation.
    if below_gate:
        pooled_margin: float | None = None
        pooled_mode = "limit"
        pooled_reason = f"below_pooled_gate(n_pool={n_pool}/{pooled_gate_n})"
    else:
        pooled_margin, pooled_mode, pooled_reason = _verdict_from_stats(
            fill_rate_star=pooled.fill_rate,
            mo_star=pooled.missed_opportunity,
            mi_star=pooled_mi,
            dead_band=dead_band,
        )

    out: dict[str, CellRecommendation] = {}

    # Per-regime cells, in a deterministic order, for whatever regimes appear.
    present = [
        r
        for r in (*SWITCHABLE_REGIMES, UNKNOWN_REGIME)
        if any(regime == r for regime, *_ in admissible)
    ]
    for regime in present:
        cell_rows = [
            (fs, shadow, realized) for reg, fs, shadow, realized in admissible if reg == regime
        ]
        stats = _cell_stats(cell_rows)
        w_fill = stats.n / (stats.n + shrinkage_k) if stats.n else 0.0

        # Shrunk statistics (reported even when the cell adopts the pooled mode).
        mo_shrunk = _shrink(
            stats.missed_opportunity, pooled.missed_opportunity, stats.n_unfilled, shrinkage_k
        )
        g_shrunk = _shrink(
            stats.observed_execution_gap, pooled.observed_execution_gap, stats.n_gap, shrinkage_k
        )
        fill_rate_shrunk = _shrink(stats.fill_rate, pooled.fill_rate, stats.n, shrinkage_k)
        mi_shrunk = _floor0(g_shrunk)

        if regime == UNKNOWN_REGIME:
            # Structural: an unknown VIX bucket is never switchable, regardless of
            # sample size — checked before the gate so an all-unknown ledger still
            # reads as non-actionable rather than just below-gate.
            margin: float | None = None
            mode = "limit"
            reason = "regime_unknown_not_actionable"
        elif below_gate:
            margin, mode, reason = (
                None,
                "limit",
                f"below_pooled_gate(n_pool={n_pool}/{pooled_gate_n})",
            )
        elif stats.n < cell_floor_n:
            # Adopt the pooled recommendation; report shrunk stats for context.
            margin = None
            mode = pooled_mode
            reason = f"cell_below_floor_uses_pooled(n={stats.n}/{cell_floor_n})"
        else:
            margin, mode, reason = _verdict_from_stats(
                fill_rate_star=fill_rate_shrunk,
                mo_star=mo_shrunk,
                mi_star=mi_shrunk,
                dead_band=dead_band,
            )

        out[regime] = CellRecommendation(
            regime=regime,
            n=stats.n,
            n_filled=stats.n_filled,
            n_unfilled=stats.n_unfilled,
            n_gap=stats.n_gap,
            fill_rate=stats.fill_rate,
            missed_opportunity=stats.missed_opportunity,
            missed_opportunity_shrunk=mo_shrunk,
            observed_execution_gap=stats.observed_execution_gap,
            expected_market_impact=mi_shrunk,
            shrinkage_weight=w_fill,
            switch_margin=margin,
            recommended_mode=mode,
            gated_reason=reason,
        )

    out[POOLED_KEY] = CellRecommendation(
        regime=POOLED_KEY,
        n=pooled.n,
        n_filled=pooled.n_filled,
        n_unfilled=pooled.n_unfilled,
        n_gap=pooled.n_gap,
        fill_rate=pooled.fill_rate,
        missed_opportunity=pooled.missed_opportunity,
        missed_opportunity_shrunk=pooled.missed_opportunity,  # pooled is the prior, w=1
        observed_execution_gap=pooled.observed_execution_gap,
        expected_market_impact=pooled_mi,
        shrinkage_weight=1.0,
        switch_margin=pooled_margin,
        recommended_mode=pooled_mode,
        gated_reason=pooled_reason,
    )
    return out


__all__ = [
    "DEFAULT_CELL_FLOOR_N",
    "DEFAULT_DEAD_BAND",
    "DEFAULT_POOLED_GATE_N",
    "DEFAULT_SHRINKAGE_K",
    "POOLED_KEY",
    "SWITCHABLE_REGIMES",
    "UNKNOWN_REGIME",
    "CellRecommendation",
    "recommend_execution_modes",
]
