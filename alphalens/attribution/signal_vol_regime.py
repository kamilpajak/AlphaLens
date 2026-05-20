"""Conditional analysis of signal returns by exogenous vol regime.

Used to test whether a strategy's alpha is concentrated in high-vol or
low-vol periods (per Cohen-Malloy-style opportunistic insider buying,
which is hypothesised to be counter-cyclical — insiders buy the dip).

The output drives Layer 4 overlay design decisions: a counter-cyclical
signal is structurally mismatched with pro-cyclical de-leveraging
overlays (vol-targeting, drawdown-control), so an empirical confirmation
of counter-cyclicality is a hard REJECT signal for the overlay class.

API:
- ``assign_vol_regime_quintiles(vol_series)`` — bucket each daily vol obs
  into Q1-Q5 by percentile.
- ``aggregate_returns_by_regime(returns, quintiles)`` — per-quintile
  mean / std / Sharpe summary.
- ``classify_cyclicality(summary)`` — classify pattern + GO/NO-GO verdict.

Critical: the classifier handles ALL sign combinations of mean(Q1+Q2)
and mean(Q4+Q5). A naive ratio R = mean_high / mean_low loses
interpretability when the denominator is non-positive — this module
classifies by sign-pattern first, then by ratio magnitude.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

_QUINTILE_LABELS = ["Q1", "Q2", "Q3", "Q4", "Q5"]
_DEFAULT_PERIODS_PER_YEAR = 252

# Decision thresholds (locked; pre-spec amendment for sign-flip cases recorded
# in tests/test_signal_vol_regime.py docstring).
_R_STRONG_COUNTER_CYCLICAL = 1.5
_R_CALM_CONCENTRATED = 0.8
_SHARPE_FLAT_TOLERANCE = 0.10  # |r_sharpe - 1| < tol → Sharpe-flat override


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VolRegimeQuintileSummary:
    """Per-quintile aggregation of returns conditional on vol regime."""

    quintile_means: pd.Series  # index Q1..Q5, daily mean return
    quintile_stds: pd.Series  # daily std
    quintile_counts: pd.Series  # n_obs per quintile
    quintile_sharpes: pd.Series  # annualized Sharpe per quintile


@dataclass(frozen=True)
class CounterCyclicalVerdict:
    """Classification + GO/NO-GO for overlay registration on this base."""

    r_mean: float  # mean(Q4+Q5) / mean(Q1+Q2); ±inf when denom ≤ 0
    r_sharpe: float  # sharpe(Q4+Q5) / sharpe(Q1+Q2); ±inf when denom ≤ 0
    sign_pattern: str  # human-readable: "Q1+Q2 negative, Q4+Q5 positive" etc.
    classification: str  # "STRONG counter-cyclical" / "EXTREME counter-cyclical" / etc.
    proceed: bool | None  # True/False/None (None = INCONCLUSIVE)
    rationale: str  # why this verdict


@dataclass(frozen=True)
class CyclicalityExcessVerdict:
    """Strategy-specific cyclicality classification (excess over benchmark baseline).

    Per session 2026-05-10 finding: R2000 long-only strategies inherit IWM
    benchmark's EXTREME counter-cyclical baseline (R≈-2.0 measured 2018-2023
    using IWM 60d realized vol as exogenous regime variable). Strategy is
    GENUINELY counter-cyclical (warrants Layer 4 overlay rejection) only
    when its r_mean is meaningfully BELOW benchmark baseline.
    """

    strategy_r_mean: float
    benchmark_r_mean: float
    excess_r_mean: float  # strategy - benchmark (negative = MORE counter-cyclical than benchmark)
    classification: str
    proceed: bool | None  # False = Layer 4 overlay would structurally hurt
    rationale: str


# ---------------------------------------------------------------------------
# Quintile assignment
# ---------------------------------------------------------------------------


def assign_vol_regime_quintiles(vol_series: pd.Series, n_quintiles: int = 5) -> pd.Series:
    """Bucket each observation into Q1..Q_n by within-series percentile.

    NaN inputs map to NaN outputs. Per-call cuts (callers wanting per-window
    cuts should call once per window).

    Raises
    ------
    ValueError
        If non-NaN obs count is less than n_quintiles.
    """
    if n_quintiles != 5:
        raise NotImplementedError("only n_quintiles=5 supported in this version")

    non_nan = vol_series.dropna()
    if len(non_nan) < n_quintiles:
        raise ValueError(
            f"need at least {n_quintiles} non-NaN observations to form "
            f"{n_quintiles} quintiles, got {len(non_nan)}"
        )

    cuts = non_nan.quantile([0.2, 0.4, 0.6, 0.8]).values
    bins = [-np.inf, *cuts, np.inf]
    quintiles = pd.cut(vol_series, bins=bins, labels=_QUINTILE_LABELS)
    # Preserve NaN mapping
    return quintiles.astype(object).where(vol_series.notna(), other=np.nan)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def aggregate_returns_by_regime(
    returns: pd.Series,
    quintiles: pd.Series,
    *,
    periods_per_year: int = _DEFAULT_PERIODS_PER_YEAR,
) -> VolRegimeQuintileSummary:
    """Aggregate returns per vol regime quintile.

    Both inputs must have matching length. NaN returns are dropped pairwise.
    All five quintiles must be represented; missing → ValueError (so the
    classifier never sees a NaN cell).
    """
    if len(returns) != len(quintiles):
        raise ValueError(
            f"returns and quintiles must have same length, got {len(returns)} vs {len(quintiles)}"
        )

    df = pd.DataFrame({"ret": returns.values, "q": quintiles.values}).dropna()
    if df.empty:
        raise ValueError("no non-NaN aligned (ret, quintile) pairs")

    grouped = df.groupby("q", observed=True)["ret"]
    means = grouped.mean()
    stds = grouped.std(ddof=1)
    counts = grouped.count()

    missing = set(_QUINTILE_LABELS) - set(means.index)
    if missing:
        raise ValueError(f"quintiles missing from input: {sorted(missing)} (empty buckets)")

    # Reindex deterministically and compute Sharpe
    means = means.reindex(_QUINTILE_LABELS)
    stds = stds.reindex(_QUINTILE_LABELS)
    counts = counts.reindex(_QUINTILE_LABELS).astype(int)
    annualizer = math.sqrt(periods_per_year)
    sharpes = (means / stds * annualizer).fillna(0.0)

    return VolRegimeQuintileSummary(
        quintile_means=means,
        quintile_stds=stds,
        quintile_counts=counts,
        quintile_sharpes=sharpes,
    )


# ---------------------------------------------------------------------------
# Classification — bug-fix epicenter
# ---------------------------------------------------------------------------


def _safe_ratio(numerator: float, denominator: float) -> float:
    """Compute ratio with sentinel for division by non-positive denominator.

    - Positive denominator → standard ratio (sign reflects pattern).
    - Zero denominator → +inf (well-defined sentinel; numerator must be positive
      to be meaningful, else NaN).
    - Negative denominator → standard ratio (negative result = sign-flip).
    """
    if denominator > 0:
        return float(numerator / denominator)
    if denominator == 0:
        return float("inf") if numerator > 0 else float("nan")
    # Negative denominator: ratio is well-defined but sign-flipped
    return float(numerator / denominator)


def _sign_pattern(low_mean: float, high_mean: float) -> str:
    def _label(x):
        if x > 0:
            return "positive"
        if x < 0:
            return "negative"
        return "zero"

    return f"Q1+Q2 {_label(low_mean)}, Q4+Q5 {_label(high_mean)}"


def classify_cyclicality(
    summary: VolRegimeQuintileSummary,
    *,
    strong_threshold: float = _R_STRONG_COUNTER_CYCLICAL,
    weak_threshold: float = _R_CALM_CONCENTRATED,
    sharpe_flat_tol: float = _SHARPE_FLAT_TOLERANCE,
) -> CounterCyclicalVerdict:
    """Classify cyclicality pattern + GO/NO-GO for overlay registration.

    Decision tree (bug-fixed for sign-flip cases):

    1. Compute mean(Q1+Q2) and mean(Q4+Q5).
    2. By sign pattern:
       a. Both positive: use ratio R.
          - R ≥ strong_threshold (1.5): STRONG counter-cyclical → REJECT.
          - weak ≤ R < strong: orthogonal → PROCEED.
          - R < weak (0.8): calm-period concentrated → PROCEED.
       b. Q1+Q2 negative, Q4+Q5 positive: EXTREME counter-cyclical (sign-flip) → REJECT.
       c. Q1+Q2 positive, Q4+Q5 negative: EXTREME calm-period (sign-flip) → PROCEED.
       d. Both negative: INCONCLUSIVE (base loses everywhere; overlay test irrelevant).
       e. Either zero with the other positive/negative: handled as a → c degenerate cases.
    3. Sharpe cross-check: if r_mean would REJECT (strong/extreme counter-cyclical)
       BUT r_sharpe is within sharpe_flat_tol of 1.0 (i.e. Sharpe is flat across
       quintiles, meaning alpha is paid for by proportional vol), flip to PROCEED.
    """
    means = summary.quintile_means
    sharpes = summary.quintile_sharpes
    low_mean = float((means["Q1"] + means["Q2"]) / 2.0)
    high_mean = float((means["Q4"] + means["Q5"]) / 2.0)
    low_sharpe = float((sharpes["Q1"] + sharpes["Q2"]) / 2.0)
    high_sharpe = float((sharpes["Q4"] + sharpes["Q5"]) / 2.0)

    r_mean = _safe_ratio(high_mean, low_mean)
    r_sharpe = _safe_ratio(high_sharpe, low_sharpe)
    sign = _sign_pattern(low_mean, high_mean)

    # Sign-flip cases first
    if low_mean <= 0 and high_mean > 0:
        # EXTREME counter-cyclical: insider loses in calm, wins in stress
        classification = "EXTREME counter-cyclical"
        proceed = False
        rationale = (
            f"Sign-flip pattern: {sign}. Insider alpha is concentrated in high-vol "
            f"regimes; pro-cyclical overlays (vol-target, drawdown-control) would "
            f"de-lever exactly when the signal is most profitable. Overlay class "
            f"structurally mismatched with this signal."
        )
        return CounterCyclicalVerdict(r_mean, r_sharpe, sign, classification, proceed, rationale)

    if low_mean > 0 and high_mean <= 0:
        # EXTREME calm-period concentrated: insider wins in calm, loses in stress
        classification = "EXTREME calm-period concentrated"
        proceed = True
        rationale = (
            f"Sign-flip pattern: {sign}. Insider alpha is concentrated in low-vol "
            f"regimes; vol-target overlay would maximally help by maintaining full "
            f"exposure during calm periods and de-leveraging the noisy high-vol periods."
        )
        return CounterCyclicalVerdict(r_mean, r_sharpe, sign, classification, proceed, rationale)

    if low_mean <= 0 and high_mean <= 0:
        # Both negative → base is unprofitable in both regimes; overlay test irrelevant
        classification = "INCONCLUSIVE (base unprofitable in both regimes)"
        proceed = None
        rationale = (
            f"{sign}. Base portfolio shows non-positive returns in both calm AND "
            f"stress regimes — overlay decision is irrelevant when the base itself "
            f"lacks profitability."
        )
        return CounterCyclicalVerdict(r_mean, r_sharpe, sign, classification, proceed, rationale)

    # Both positive: use ratio r_mean for classification
    if r_mean >= strong_threshold:
        classification = "STRONG counter-cyclical"
        # Sharpe cross-check: if Sharpe is flat, vol-target would be Sharpe-neutral
        if abs(r_sharpe - 1.0) < sharpe_flat_tol:
            proceed = True
            rationale = (
                f"r_mean = {r_mean:.2f} (≥{strong_threshold}) suggests counter-cyclical, "
                f"BUT r_sharpe = {r_sharpe:.2f} ≈ 1.0 (Sharpe-flat across quintiles). "
                f"Alpha in high-vol periods is paid for by proportional vol; vol-target "
                f"overlay would be Sharpe-neutral. Override to PROCEED."
            )
        else:
            proceed = False
            rationale = (
                f"r_mean = {r_mean:.2f} ≥ {strong_threshold}. r_sharpe = {r_sharpe:.2f} "
                f"confirms pattern (not paid-for-by-vol). Counter-cyclical signal is "
                f"structurally mismatched with pro-cyclical overlays."
            )
        return CounterCyclicalVerdict(r_mean, r_sharpe, sign, classification, proceed, rationale)

    if r_mean < weak_threshold:
        classification = "calm-period concentrated"
        proceed = True
        rationale = (
            f"r_mean = {r_mean:.2f} < {weak_threshold}. Insider alpha concentrates "
            f"in calm periods; vol-target overlay would help by maintaining exposure "
            f"during alpha-rich calm and de-leveraging noisy stress periods."
        )
        return CounterCyclicalVerdict(r_mean, r_sharpe, sign, classification, proceed, rationale)

    # weak ≤ r_mean < strong → orthogonal
    classification = "orthogonal (vol-state independent)"
    proceed = True
    rationale = (
        f"r_mean = {r_mean:.2f} ∈ [{weak_threshold}, {strong_threshold}). Insider alpha "
        f"is approximately vol-orthogonal; vol-target overlay would be neutral on "
        f"first-order signal alignment. Register vanilla M-M overlay test to confirm."
    )
    return CounterCyclicalVerdict(r_mean, r_sharpe, sign, classification, proceed, rationale)


# ---------------------------------------------------------------------------
# Strategy-specific cyclicality (excess over benchmark baseline)
# ---------------------------------------------------------------------------


# Default: strategy R must be ≤ 1.0 below benchmark R to count as strategy-specific
_DEFAULT_EXCESS_STRONG_THRESHOLD = -1.0
# Default: |excess| < 0.5 counts as "matches benchmark baseline"
_DEFAULT_EXCESS_MATCH_TOLERANCE = 0.5


def _compute_r_mean(summary: VolRegimeQuintileSummary) -> float:
    means = summary.quintile_means
    low = float((means["Q1"] + means["Q2"]) / 2.0)
    high = float((means["Q4"] + means["Q5"]) / 2.0)
    return _safe_ratio(high, low)


def classify_cyclicality_excess(
    strategy_summary: VolRegimeQuintileSummary,
    benchmark_summary: VolRegimeQuintileSummary,
    *,
    excess_strong_threshold: float = _DEFAULT_EXCESS_STRONG_THRESHOLD,
    excess_match_tolerance: float = _DEFAULT_EXCESS_MATCH_TOLERANCE,
) -> CyclicalityExcessVerdict:
    """Classify strategy cyclicality EXCESS over benchmark baseline.

    Both summaries must be computed on the SAME vol regime quintiles
    (typically derived from the benchmark's own vol series, e.g. IWM 60d
    realized vol).

    Decision tree:
    - excess_r_mean ≤ excess_strong_threshold (default -1.0):
      strategy R is meaningfully below benchmark R → strategy-specific
      counter-cyclical → Layer 4 pro-cyclical overlay would structurally
      hurt → proceed=False
    - |excess_r_mean| < excess_match_tolerance (default 0.5):
      matches benchmark baseline → universe-mechanical cyclicality, NOT
      strategy-specific → proceed=True (overlay decision driven by other
      factors)
    - excess_r_mean ≥ excess_match_tolerance: strategy is LESS counter-
      cyclical than benchmark (or even calm-period concentrated relative
      to baseline) → proceed=True
    - In-between: weakly strategy-specific → proceed=True with caveat
    """
    strat_r = _compute_r_mean(strategy_summary)
    bench_r = _compute_r_mean(benchmark_summary)

    # If either is non-finite (divide-by-zero or NaN), excess is meaningless
    if not (math.isfinite(strat_r) and math.isfinite(bench_r)):
        return CyclicalityExcessVerdict(
            strategy_r_mean=strat_r,
            benchmark_r_mean=bench_r,
            excess_r_mean=float("nan"),
            classification="INCONCLUSIVE (R undefined)",
            proceed=None,
            rationale=(
                f"strategy_R={strat_r}, benchmark_R={bench_r}. One or both R "
                f"undefined (likely zero-denominator or NaN). Excess concept "
                f"requires both R to be finite."
            ),
        )

    # Short-circuit if benchmark itself is not counter-cyclical (R >= 0).
    # Per zen 2026-05-10 code review: simple subtraction strat - bench reverses
    # semantic meaning when R is positive (higher R = more counter-cyclical
    # for positive R; lower R = more counter-cyclical for negative R). Excess
    # concept is only well-defined against a counter-cyclical baseline.
    if bench_r >= 0:
        return CyclicalityExcessVerdict(
            strategy_r_mean=strat_r,
            benchmark_r_mean=bench_r,
            excess_r_mean=float("nan"),
            classification="INCONCLUSIVE (benchmark R ≥ 0)",
            proceed=None,
            rationale=(
                f"strategy_R={strat_r:.2f}, benchmark_R={bench_r:.2f}. Benchmark "
                f"baseline does not show counter-cyclical sign-flip pattern "
                f"(R ≥ 0). Excess classification is designed specifically to isolate "
                f"strategy edge from a mechanical counter-cyclical baseline. "
                f"Use absolute classify_cyclicality() instead."
            ),
        )

    excess = strat_r - bench_r
    benchmark_weak_warning = ""

    if excess <= excess_strong_threshold:
        return CyclicalityExcessVerdict(
            strategy_r_mean=strat_r,
            benchmark_r_mean=bench_r,
            excess_r_mean=excess,
            classification="strategy-specific counter-cyclical",
            proceed=False,
            rationale=(
                f"strategy r_mean={strat_r:.2f}, benchmark r_mean={bench_r:.2f}, "
                f"excess={excess:.2f} ≤ {excess_strong_threshold:.1f}. Strategy is "
                f"meaningfully MORE counter-cyclical than benchmark baseline; pro-"
                f"cyclical Layer 4 overlays would structurally de-lever exactly when "
                f"strategy generates its excess alpha.{benchmark_weak_warning}"
            ),
        )

    if abs(excess) < excess_match_tolerance:
        return CyclicalityExcessVerdict(
            strategy_r_mean=strat_r,
            benchmark_r_mean=bench_r,
            excess_r_mean=excess,
            classification="matches benchmark baseline",
            proceed=True,
            rationale=(
                f"strategy r_mean={strat_r:.2f}, benchmark r_mean={bench_r:.2f}, "
                f"excess={excess:.2f} (|·| < {excess_match_tolerance:.1f}). Strategy "
                f"cyclicality matches universe baseline (likely mechanical artifact "
                f"of vol-regime methodology, not strategy-specific). Layer 4 overlay "
                f"decision should be driven by other factors (signal-mechanism "
                f"alignment, etc.), not cyclicality alone.{benchmark_weak_warning}"
            ),
        )

    if excess >= excess_match_tolerance:
        return CyclicalityExcessVerdict(
            strategy_r_mean=strat_r,
            benchmark_r_mean=bench_r,
            excess_r_mean=excess,
            classification="less counter-cyclical than benchmark",
            proceed=True,
            rationale=(
                f"strategy r_mean={strat_r:.2f}, benchmark r_mean={bench_r:.2f}, "
                f"excess={excess:+.2f} ≥ {excess_match_tolerance:.1f}. Strategy is "
                f"LESS counter-cyclical than universe baseline; Layer 4 overlays "
                f"unlikely to hurt strategy structurally.{benchmark_weak_warning}"
            ),
        )

    # In-between: excess in (-strong, -match_tol) — weakly strategy-specific
    return CyclicalityExcessVerdict(
        strategy_r_mean=strat_r,
        benchmark_r_mean=bench_r,
        excess_r_mean=excess,
        classification="weakly strategy-specific counter-cyclical",
        proceed=True,
        rationale=(
            f"strategy r_mean={strat_r:.2f}, benchmark r_mean={bench_r:.2f}, "
            f"excess={excess:.2f} ∈ ({excess_strong_threshold}, {-excess_match_tolerance}). "
            f"Weak strategy-specific counter-cyclical; Layer 4 overlay impact "
            f"likely modest, not structurally fatal.{benchmark_weak_warning}"
        ),
    )
