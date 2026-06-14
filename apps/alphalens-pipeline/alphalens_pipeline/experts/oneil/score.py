"""The 0-100 O'Neil score — a momentum/technical composite gated on the N term.

Mirrors the Buffett quality-score shape (cheap numerics only, coverage shrink),
but with O'Neil's defining asymmetry: **N (proximity to the 52-week high) is
mandatory**. A score without proximity-to-high is not an O'Neil score, so the
composite is ``None`` whenever N is absent (missing column, window-short, or
split-contaminated). The three OPTIONAL terms — R (relative strength, a
cross-sectional trailing-return percentile from the grouped-daily history store),
L (MA200 up-trend), and C/A (earnings YoY) — partial-credit when present and are
renormalized out of the weighting when absent (a missing optional term does not
silently deflate the score; the coverage shrink carries the thin-data penalty instead).

All weights and clip caps are hand-chosen, UNVALIDATED module constants (a
screening heuristic, not a fitted optimum) — display-only until a per-expert
O'Neil×EDGE study validates them. See ``docs/research/oneil_expert_design_2026_06_13.md``.
"""

from __future__ import annotations

from alphalens_pipeline.experts.oneil.comparison import ONeilPanel

# Term weights (sum to 1.0). N leads (the O'Neil thesis is "buy near new highs"),
# R (relative strength) next, then trend + earnings. Renormalized over the PRESENT
# terms. R re-weight (R-reactivation): N 0.40->0.35, +R 0.25, trend 0.30->0.20,
# earnings 0.30->0.20.
_W_NEW_HIGH = 0.35
_W_RS = 0.25
_W_TREND = 0.20
_W_EARNINGS = 0.20

# Clip caps / floors (same units as the panel fields).
# N: 0% off the high earns full credit, -25% or worse earns zero.
_NH_FLOOR_PCT = -25.0
# Trend: a +0.10%/day MA200 slope (or steeper) earns full credit; <=0 earns zero.
_TREND_FULL_SLOPE = 0.10
# Earnings: +50% YoY (or more) earns full credit.
_EARN_FULL_PCT = 50.0

# Coverage shrink over the 3 OPTIONAL terms: multiplier =
# ``_COVERAGE_BASE + (1 - _COVERAGE_BASE) * data_coverage``. N-only (all optional
# terms absent) is halved; all optional present leaves the composite unchanged.
_COVERAGE_BASE = 0.5


def _new_high_credit(pct_off_52w_high: float) -> float:
    """``clip((pct + 25) / 25, 0, 1)``: 0% off high -> 1.0, -25% or worse -> 0.0."""
    credit = (pct_off_52w_high - _NH_FLOOR_PCT) / (0.0 - _NH_FLOOR_PCT)
    return min(1.0, max(0.0, credit))


def _rs_credit(rs_pct: float) -> float:
    """``clip(rs_pct / 100, 0, 1)``: the relative-strength percentile mapped to [0, 1].

    ``oneil_rs_approx_pct`` is already a 0-100 within-date percentile rank from the
    split-adjusted grouped-daily history store — no split band / floor needed (the
    adjusted=true store removes the raw-close split hazard the N window has)."""
    return min(1.0, max(0.0, rs_pct / 100.0))


def _trend_credit(slope: float) -> float:
    """``clip(slope / 0.10, 0, 1)``: a flat or falling MA200 earns zero credit."""
    return min(1.0, max(0.0, slope / _TREND_FULL_SLOPE))


def _earnings_credit(growth_pct: float) -> float:
    """``clip(growth / 50, 0, 1)``: a contracting earnings line earns zero credit."""
    return min(1.0, max(0.0, growth_pct / _EARN_FULL_PCT))


def compute_oneil_score(panel: ONeilPanel) -> float | None:
    """The 0-100 O'Neil composite for one candidate, or ``None``.

    Returns ``None`` when the mandatory N term is absent — ``pct_off_52w_high`` is
    ``None`` OR a split is suspected in the raw-close window (the 52w-high peak is
    contaminated). Otherwise scores N (always) plus whichever of R / trend / earnings
    resolved, renormalized over the present weights and shrunk by ``data_coverage``.
    R is OPTIONAL (renorm + coverage), never gated — gating on a cross-sectional disk
    read would null every candidate on any store gap.
    """
    # Hard N gate: no proximity-to-high (or a contaminated one) => not a score.
    if panel.pct_off_52w_high is None or panel.new_high_split_suspected is True:
        return None

    weighted = _W_NEW_HIGH * _new_high_credit(panel.pct_off_52w_high)
    present_weight = _W_NEW_HIGH

    # R (relative strength) — optional; None when the history store lacks the asof /
    # asof-252 snapshot or the candidate is absent from either (see comparison.compute_oneil_panel).
    if panel.oneil_rs_approx_pct is not None:
        weighted += _W_RS * _rs_credit(panel.oneil_rs_approx_pct)
        present_weight += _W_RS

    if panel.ma200_slope_pct_per_day is not None:
        weighted += _W_TREND * _trend_credit(panel.ma200_slope_pct_per_day)
        present_weight += _W_TREND

    # Earnings is excluded when not usable (None already encodes <2 FY / sign-flip
    # / near-zero base — see comparison._earnings_growth_yoy).
    if panel.earnings_growth_yoy_pct is not None:
        weighted += _W_EARNINGS * _earnings_credit(panel.earnings_growth_yoy_pct)
        present_weight += _W_EARNINGS

    raw = 100.0 * weighted / present_weight
    shrink = _COVERAGE_BASE + (1.0 - _COVERAGE_BASE) * panel.data_coverage
    return raw * shrink


__all__ = ["compute_oneil_score"]
