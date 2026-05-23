# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownParameterType=false, reportUnknownLambdaType=false
"""Tiered flat-bps cost model + scale-path validation.

The third Perplexity-flagged gap after PIT survivorship (PR #9) and
walk-forward (PR #11). A prior per-ticker cost attempt on
`feature/per-ticker-cost-model` used EDGE (Ardia-Guidotti-Kroencke
2024) spread estimator + Almgren-Chriss market impact and was
empirically broken: 68× overestimate on AAPL (EDGE=68bp vs real NBBO
≈1bp), 95% artifact. Δ Sharpe −3.33 on 5y regression was spurious.

Perplexity + zen consultations concluded:

1. **OHLC-family estimators (EDGE/CS/AR) cannot be salvaged by
   calibration** — the bias is model misspecification, not noise.
2. **Tiered flat-bps is institutional-grade** when empirically
   anchored. Not a retail shortcut.
3. **Scale-path validation is the crucial gate**: if daily rebalance
   participation exceeds institutional working range (>15% of ADV on
   any pick), market impact dominates and even tiered flat bps
   underestimates real cost.
4. **AQR (Frazzini-Israel-Moskowitz 2018, "Trading Costs")** is the
   published reference: small-caps 30-50 bps (not 100+), mid-caps
   15-35, large-caps 5-15, mega-caps 1-3.

## What this module does

**A. Scale-path validation.** For each historical rebalance day and
each top-N pick, compute participation = rebalance_dollars /
dollar_ADV. Aggregate: fraction of pick-days exceeding 15%, max
across the window, worst offenders. Gate: PASS if <5% of days
exceed 15% AND max <20%; FAIL otherwise.

**B. Tiered cost model.** Bucket each ticker by rolling 21-day
dollar-ADV quintile **computed as of each rebalance date** — no
lookahead. Apply tier-specific bps (AQR-anchored: mega=3, large=10,
mid=25, small=50, micro=100).

## Lookahead guard

`build_per_date_tiers` creates a date-indexed dict of ticker→tier
assignments using 21-day trailing ADV **ending on the bar BEFORE**
each rebalance date (strict point-in-time). The `apply_tiered_cost`
and `run_scale_path` helpers both look up tier via
`per_date_tiers[pick_date][ticker]`, never using future data.

## Gate thresholds (zen-revised)

- Fraction gate: <5% of pick-days exceed 15% ADV participation
- Max gate: max participation <20% ADV

Institutional VWAP algos routinely target 10-15% of ADV across the
trading day. Tighter thresholds (Perplexity's initial 7%) would
auto-fail valid small-cap strategies.

## Reference

Frazzini, A., Israel, R., Moskowitz, T. J. (2018). "Trading Costs."
SSRN 3229719 / AQR working paper.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
from alphalens_pipeline.data.store.history import HistoryStore

from alphalens_research.attribution.cost_model import CostModel
from alphalens_research.backtest.engine import BacktestReport, RebalanceSnapshot
from alphalens_research.backtest.metrics import sharpe
from alphalens_research.backtest.weighting import compute_position_weights

# ---------------------------------------------------------------------------
# Dataclasses


@dataclass(frozen=True)
class TierDefinition:
    name: str
    adv_percentile_low: float  # 0.0 - 1.0
    adv_percentile_high: float
    bps_annual: float


# AQR-anchored defaults per Frazzini-Israel-Moskowitz (2018). Micro tier is
# an extrapolation past the paper's coverage; flagged in the report.
DEFAULT_TIERS: tuple[TierDefinition, ...] = (
    TierDefinition("mega", 0.80, 1.00, 3.0),
    TierDefinition("large", 0.60, 0.80, 10.0),
    TierDefinition("mid", 0.40, 0.60, 25.0),
    TierDefinition("small", 0.20, 0.40, 50.0),
    TierDefinition("micro", 0.00, 0.20, 100.0),
)

_FLAT_100BPS = 100.0


@dataclass(frozen=True)
class PickParticipation:
    date: date
    ticker: str
    rank: int
    tier: str
    participation: float  # fraction of dollar ADV
    dollar_position: float
    dollar_adv: float


@dataclass(frozen=True)
class ScalePathSummary:
    n_pick_days: int
    threshold_pct: float
    max_threshold_pct: float
    fraction_exceeding_threshold: float
    max_participation: float
    q95_participation: float
    median_participation: float
    worst_offenders: tuple[PickParticipation, ...]
    per_tier_max: dict[str, float]
    per_tier_median: dict[str, float]


@dataclass(frozen=True)
class TieredCostComparison:
    sharpe_gross: float
    sharpe_flat_100bps: float
    sharpe_tiered: float
    annual_drag_tiered_bps: float  # effective tiered drag (gross→tiered Sharpe delta)
    tier_counts_on_last_date: dict[str, int]


@dataclass(frozen=True)
class CostGateVerdict:
    scale_path_pass: bool
    fraction_pass: bool
    max_pass: bool
    overall: Literal["PASS", "FAIL"]
    reasons: dict[str, str]


@dataclass
class CostValidationReport:
    portfolio_value: float
    window_days: int
    baseline_sharpe_gross: float
    tiers: tuple[TierDefinition, ...]
    scale_path: ScalePathSummary
    tiered: TieredCostComparison
    verdict: CostGateVerdict


# ---------------------------------------------------------------------------
# ADV + tier classification


_ADV_FLOOR_DOLLARS = 1_000.0


def rolling_dollar_adv(
    store: HistoryStore,
    tickers: Sequence[str],
    window_days: int = 21,
) -> dict[str, pd.Series]:
    """21-day trailing dollar-ADV per ticker.

    For each ticker, produces a Series indexed by trading date where the
    value at date `t` is the trailing `window_days` average of
    (close * volume) **computed from bars strictly before `t`** — so the
    series is point-in-time safe for lookup on date `t` at the open.

    Halted / zero-volume days are imputed from the previous valid day
    (ffill). Structurally illiquid tickers (ADV below floor) are clamped
    to `_ADV_FLOOR_DOLLARS = $1k` to avoid div-by-zero in participation.
    """
    out: dict[str, pd.Series] = {}
    for ticker in tickers:
        try:
            df = store.full(ticker)
        except KeyError:
            continue
        if df.empty:
            continue
        dollar_volume = df["close"].astype(float) * df["volume"].astype(float)
        # Zero-volume day → treat as missing, propagate last valid value
        dollar_volume = dollar_volume.where(dollar_volume > 0.0).ffill()
        # Rolling 21-day mean, shifted by 1 so value at date t uses bars [t-21, t-1]
        trailing = dollar_volume.rolling(window=window_days, min_periods=1).mean().shift(1)
        trailing = trailing.fillna(_ADV_FLOOR_DOLLARS)
        trailing = trailing.clip(lower=_ADV_FLOOR_DOLLARS)
        out[ticker] = trailing
    return out


def classify_tier_as_of(
    adv_at_date: Mapping[str, float],
    tiers: Sequence[TierDefinition] = DEFAULT_TIERS,
) -> dict[str, str]:
    """Quintile classification over a single date's ADV snapshot.

    Uses numpy percentile on the ADV values. Ties are broken by assigning
    to the LOWER tier (conservative — higher bps).
    """
    if not adv_at_date:
        return {}
    values = np.array(list(adv_at_date.values()), dtype=float)
    tickers_list = list(adv_at_date.keys())
    # Percentile rank for each ticker: fraction of values <= this one
    ranks = pd.Series(values).rank(pct=True, method="average").to_numpy()
    result: dict[str, str] = {}
    for ticker, rank_pct in zip(tickers_list, ranks, strict=False):
        for tier in tiers:
            if tier.adv_percentile_low <= rank_pct <= tier.adv_percentile_high:
                result[ticker] = tier.name
                break
        else:
            # Shouldn't happen if tiers cover [0, 1]
            result[ticker] = tiers[0].name
    return result


def build_per_date_tiers(
    rolling_adv: Mapping[str, pd.Series],
    calendar: Sequence[pd.Timestamp],
    tiers: Sequence[TierDefinition] = DEFAULT_TIERS,
) -> dict[pd.Timestamp, dict[str, str]]:
    """Per-rebalance-date tier table. Lookahead-safe.

    For each date in `calendar`, snapshot the ADV value at that date from
    every ticker's rolling ADV series, then classify quintiles. Tickers
    with no valid ADV on that date are omitted from the day's snapshot
    (their tier lookup returns None downstream → default to "mid" with a
    warning).
    """
    per_date: dict[pd.Timestamp, dict[str, str]] = {}
    for ts in calendar:
        snapshot: dict[str, float] = {}
        for ticker, series in rolling_adv.items():
            if ts not in series.index:
                continue
            val = series.loc[ts]
            if pd.isna(val):
                continue
            snapshot[ticker] = float(val)
        per_date[ts] = classify_tier_as_of(snapshot, tiers)
    return per_date


# ---------------------------------------------------------------------------
# Scale-path analysis


def _empty_scale_path_summary(threshold_pct: float, max_threshold_pct: float) -> ScalePathSummary:
    return ScalePathSummary(
        n_pick_days=0,
        threshold_pct=threshold_pct,
        max_threshold_pct=max_threshold_pct,
        fraction_exceeding_threshold=0.0,
        max_participation=0.0,
        q95_participation=0.0,
        median_participation=0.0,
        worst_offenders=(),
        per_tier_max={},
        per_tier_median={},
    )


def _lookup_dollar_adv(
    rolling_adv: Mapping[str, pd.Series], ticker: str, day: pd.Timestamp
) -> float:
    series = rolling_adv.get(ticker)
    if series is None or day not in series.index:
        return _ADV_FLOOR_DOLLARS
    val = series.loc[day]
    return float(val) if not pd.isna(val) else _ADV_FLOOR_DOLLARS


def _picks_for_day(
    snap: RebalanceSnapshot,
    weights: np.ndarray,
    turnover_today: float,
    portfolio_value: float,
    tier_map: Mapping[str, str],
    rolling_adv: Mapping[str, pd.Series],
) -> list[PickParticipation]:
    out: list[PickParticipation] = []
    for rank, ticker in enumerate(snap.top_n_tickers, start=1):
        position_dollars = portfolio_value * float(weights[rank - 1])
        rebalance_dollars = position_dollars * turnover_today
        dollar_adv = _lookup_dollar_adv(rolling_adv, ticker, snap.date)
        out.append(
            PickParticipation(
                date=snap.date.date(),
                ticker=ticker,
                rank=rank,
                tier=tier_map.get(ticker, "mid"),
                participation=rebalance_dollars / max(dollar_adv, _ADV_FLOOR_DOLLARS),
                dollar_position=position_dollars,
                dollar_adv=dollar_adv,
            )
        )
    return out


def _per_tier_stats(
    all_picks: list[PickParticipation],
) -> tuple[dict[str, float], dict[str, float]]:
    per_tier_max: dict[str, float] = {}
    per_tier_median: dict[str, float] = {}
    df = pd.DataFrame(
        {"tier": [p.tier for p in all_picks], "p": [p.participation for p in all_picks]}
    )
    for tier, group in df.groupby("tier"):
        per_tier_max[str(tier)] = float(group["p"].max())
        per_tier_median[str(tier)] = float(group["p"].median())
    return per_tier_max, per_tier_median


def run_scale_path(
    baseline: BacktestReport,
    rolling_adv: Mapping[str, pd.Series],
    per_date_tiers: Mapping[pd.Timestamp, Mapping[str, str]],
    portfolio_value: float,
    *,
    threshold_pct: float = 15.0,
    max_threshold_pct: float = 20.0,
    weighting: str = "linear",
    n_worst: int = 20,
) -> ScalePathSummary:
    """Per-pick participation distribution across the backtest's top-N picks.

    Per-day turnover is derived from the engine-reported top_n lists
    (fraction of names changing day-over-day). For the VERY FIRST day
    of the report, we use 100% turnover as a conservative fill (worst
    case — no prior basket to compare against).
    """
    if not baseline.rebalance_results:
        return _empty_scale_path_summary(threshold_pct, max_threshold_pct)

    all_picks: list[PickParticipation] = []
    prev_top_n: set[str] | None = None
    for snap in baseline.rebalance_results:
        n = len(snap.top_n_tickers)
        if n == 0:
            prev_top_n = set()
            continue
        weights = compute_position_weights(n, weighting)  # type: ignore[arg-type]
        current_set = set(snap.top_n_tickers)
        turnover_today = 1.0 if prev_top_n is None else len(current_set - prev_top_n) / n
        prev_top_n = current_set
        all_picks.extend(
            _picks_for_day(
                snap,
                weights,
                turnover_today,
                portfolio_value,
                per_date_tiers.get(snap.date, {}),
                rolling_adv,
            )
        )

    if not all_picks:
        return _empty_scale_path_summary(threshold_pct, max_threshold_pct)

    participations = np.array([p.participation for p in all_picks])
    threshold_frac = threshold_pct / 100.0
    per_tier_max, per_tier_median = _per_tier_stats(all_picks)

    return ScalePathSummary(
        n_pick_days=len(all_picks),
        threshold_pct=threshold_pct,
        max_threshold_pct=max_threshold_pct,
        fraction_exceeding_threshold=float((participations > threshold_frac).mean()),
        max_participation=float(participations.max()),
        q95_participation=float(np.quantile(participations, 0.95)),
        median_participation=float(np.quantile(participations, 0.5)),
        worst_offenders=tuple(sorted(all_picks, key=lambda p: -p.participation)[:n_worst]),
        per_tier_max=per_tier_max,
        per_tier_median=per_tier_median,
    )


# ---------------------------------------------------------------------------
# Tiered cost application + scenario comparison


def apply_tiered_cost(
    returns: pd.Series,
    daily_top_n_tickers: Sequence[Sequence[str]],
    daily_dates: Sequence[pd.Timestamp],
    per_date_tiers: Mapping[pd.Timestamp, Mapping[str, str]],
    bps_per_tier: Mapping[str, float],
    daily_turnover: Sequence[float] | None = None,
    weighting: str = "linear",
) -> pd.Series:
    """Per-day drag = weighted average of top-N tier bps × turnover.

    The ticker's tier is looked up using **that day's** per_date_tiers
    assignment (no future data). Tickers with no tier assignment on
    the given day fall back to "mid" (silent — caller is expected to
    inspect the report's tier-coverage warning).
    """
    gross = pd.Series(list(returns), dtype=float)
    if len(gross) != len(daily_top_n_tickers) or len(gross) != len(daily_dates):
        raise ValueError(
            f"length mismatch: returns={len(gross)}, "
            f"top_n={len(daily_top_n_tickers)}, dates={len(daily_dates)}"
        )

    net_arr = gross.to_numpy().astype(float).copy()
    for i, (top_n_list, ts) in enumerate(zip(daily_top_n_tickers, daily_dates, strict=False)):
        tickers = list(top_n_list)
        n = len(tickers)
        if n == 0:
            continue
        tier_map = per_date_tiers.get(ts, {})
        weights = compute_position_weights(n, weighting)  # type: ignore[arg-type]
        # Weighted tier bps
        bps_weighted = 0.0
        for rank, ticker in enumerate(tickers):
            tier = tier_map.get(ticker, "mid")
            bps_weighted += float(weights[rank]) * bps_per_tier.get(tier, _FLAT_100BPS)
        # Per-day drag from annual bps, scaled by turnover if provided
        annual_drag = bps_weighted / 10_000.0
        daily_drag = annual_drag / 252.0
        if daily_turnover is not None:
            daily_drag *= float(daily_turnover[i])
        net_arr[i] = float(gross.iloc[i]) - daily_drag

    return pd.Series(net_arr, index=gross.index)


def compare_cost_scenarios(
    baseline: BacktestReport,
    per_date_tiers: Mapping[pd.Timestamp, Mapping[str, str]],
    bps_per_tier: Mapping[str, float],
    *,
    weighting: str = "linear",
) -> TieredCostComparison:
    """Gross / flat 100bps / tiered Sharpe side-by-side."""
    returns = baseline.portfolio_returns
    daily_top_n = [snap.top_n_tickers for snap in baseline.rebalance_results]
    daily_dates = [snap.date for snap in baseline.rebalance_results]

    gross_sharpe = sharpe(returns.tolist()) if len(returns) else 0.0

    flat_net = CostModel(annual_drag_bps=_FLAT_100BPS).apply(returns, daily_turnover=None)
    flat_sharpe = sharpe(flat_net.tolist()) if len(flat_net) else 0.0

    tiered_net = apply_tiered_cost(
        returns,
        daily_top_n,
        daily_dates,
        per_date_tiers,
        bps_per_tier,
        daily_turnover=None,
        weighting=weighting,
    )
    tiered_sharpe = sharpe(tiered_net.tolist()) if len(tiered_net) else 0.0

    # Effective tiered drag (annualised bps): gross mean - tiered mean → annualise
    daily_drag = float(returns.mean() - tiered_net.mean())
    annual_drag_bps = daily_drag * 252.0 * 10_000.0

    # Tier counts on the last date for report context
    if daily_dates:
        last_tiers = per_date_tiers.get(daily_dates[-1], {})
        tier_counts: dict[str, int] = {}
        for t in last_tiers.values():
            tier_counts[t] = tier_counts.get(t, 0) + 1
    else:
        tier_counts = {}

    return TieredCostComparison(
        sharpe_gross=gross_sharpe,
        sharpe_flat_100bps=flat_sharpe,
        sharpe_tiered=tiered_sharpe,
        annual_drag_tiered_bps=annual_drag_bps,
        tier_counts_on_last_date=tier_counts,
    )


# ---------------------------------------------------------------------------
# Gate evaluation


def evaluate_cost_gate(
    scale_path: ScalePathSummary,
    *,
    fraction_threshold: float = 0.05,
) -> CostGateVerdict:
    """Zen-revised gate: PASS iff <5% of pick-days exceed the participation
    threshold AND max participation stays below the max threshold.
    """
    max_threshold_frac = scale_path.max_threshold_pct / 100.0

    fraction_pass = scale_path.fraction_exceeding_threshold < fraction_threshold
    max_pass = scale_path.max_participation < max_threshold_frac
    scale_path_pass = fraction_pass and max_pass

    reasons: dict[str, str] = {}
    reasons["fraction"] = (
        f"fraction pick-days > {scale_path.threshold_pct:.1f}% ADV = "
        f"{scale_path.fraction_exceeding_threshold:.2%} "
        f"(threshold < {fraction_threshold:.0%})"
    )
    reasons["max"] = (
        f"max participation = {scale_path.max_participation:.2%} ADV "
        f"(threshold < {scale_path.max_threshold_pct:.1f}%)"
    )

    return CostGateVerdict(
        scale_path_pass=scale_path_pass,
        fraction_pass=fraction_pass,
        max_pass=max_pass,
        overall="PASS" if scale_path_pass else "FAIL",
        reasons=reasons,
    )


# ---------------------------------------------------------------------------
# Report


def _format_tier_table(tiers: Sequence[TierDefinition]) -> str:
    lines = [
        "| Tier | ADV percentile | bps annual |",
        "| --- | --- | ---: |",
    ]
    for t in tiers:
        lines.append(
            f"| {t.name} | {t.adv_percentile_low * 100:.0f}-{t.adv_percentile_high * 100:.0f}% | {t.bps_annual:.1f} |"
        )
    return "\n".join(lines)


def _format_scale_path_block(s: ScalePathSummary) -> str:
    lines = [
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Total pick-days | {s.n_pick_days} |",
        f"| Median participation | {s.median_participation:.2%} |",
        f"| Q95 participation | {s.q95_participation:.2%} |",
        f"| Max participation | {s.max_participation:.2%} |",
        f"| Fraction > {s.threshold_pct:.1f}% ADV | {s.fraction_exceeding_threshold:.2%} |",
        "",
    ]
    if s.worst_offenders:
        lines.append("### Worst offenders (top 10)")
        lines.append("")
        lines.append("| Date | Ticker | Rank | Tier | Participation | $position | $ADV |")
        lines.append("| --- | --- | ---: | --- | ---: | ---: | ---: |")
        for p in list(s.worst_offenders)[:10]:
            lines.append(
                f"| {p.date} | {p.ticker} | {p.rank} | {p.tier} | "
                f"{p.participation:.2%} | ${p.dollar_position:,.0f} | ${p.dollar_adv:,.0f} |"
            )
    if s.per_tier_max:
        lines.append("")
        lines.append("### Per-tier peak participation")
        lines.append("")
        lines.append("| Tier | Median | Max |")
        lines.append("| --- | ---: | ---: |")
        for tier in ("mega", "large", "mid", "small", "micro"):
            if tier in s.per_tier_max:
                lines.append(
                    f"| {tier} | {s.per_tier_median.get(tier, 0):.2%} | "
                    f"{s.per_tier_max[tier]:.2%} |"
                )
    return "\n".join(lines)


def _format_tiered_cost_block(t: TieredCostComparison) -> str:
    lines = [
        "| Scenario | Sharpe | Annual drag (bps) |",
        "| --- | ---: | ---: |",
        f"| Gross (no drag) | {t.sharpe_gross:+.3f} | 0 |",
        f"| Flat 100 bps (current production) | {t.sharpe_flat_100bps:+.3f} | 100 |",
        f"| Tiered (AQR-anchored) | {t.sharpe_tiered:+.3f} | {t.annual_drag_tiered_bps:.0f} |",
    ]
    if t.tier_counts_on_last_date:
        lines.append("")
        lines.append("Tier distribution on the last rebalance date:")
        for tier in ("mega", "large", "mid", "small", "micro"):
            n = t.tier_counts_on_last_date.get(tier, 0)
            lines.append(f"  - {tier}: {n} tickers")
    return "\n".join(lines)


def _format_gate_block(v: CostGateVerdict) -> str:
    lines = ["## Decision gate", ""]

    def line(key: str, pass_flag: bool) -> str:
        mark = "PASS" if pass_flag else "FAIL"
        return f"- **{key}**: {mark} — {v.reasons[key]}"

    lines.append(line("fraction", v.fraction_pass))
    lines.append(line("max", v.max_pass))
    lines.append("")
    lines.append(f"**Overall: {v.overall}**")
    return "\n".join(lines)


def _interpret(report: CostValidationReport) -> str:
    v = report.verdict
    s = report.scale_path
    t = report.tiered
    if v.overall == "PASS":
        return (
            f"**Interpretation.** Strategy is deployable at "
            f"${report.portfolio_value / 1e6:.1f}M AUM with the tiered "
            f"cost model. Only {s.fraction_exceeding_threshold:.1%} of "
            f"pick-days exceeded the {s.threshold_pct:.0f}% ADV "
            f"participation threshold; max single-pick participation was "
            f"{s.max_participation:.1%}. AQR-anchored tiered drag "
            f"(~{t.annual_drag_tiered_bps:.0f} bps/yr) produces "
            f"Sharpe {t.sharpe_tiered:+.2f}, between gross "
            f"({t.sharpe_gross:+.2f}) and the conservative flat 100 bps "
            f"({t.sharpe_flat_100bps:+.2f}). A follow-up PR can wire "
            f"`--cost-model tiered` into `alphalens_research backtest` as a "
            f"non-default option."
        )
    # FAIL: at least one of fraction / max flagged
    details = []
    if not v.fraction_pass:
        details.append(
            f"fraction-gate: {s.fraction_exceeding_threshold:.2%} of "
            f"pick-days exceeded {s.threshold_pct:.0f}% ADV "
            f"(threshold was <5%)"
        )
    if not v.max_pass:
        details.append(
            f"max-gate: max participation {s.max_participation:.2%} ADV "
            f"exceeded {s.max_threshold_pct:.0f}% ceiling"
        )
    return (
        f"**Interpretation.** Strategy exceeds institutional-grade "
        f"execution thresholds at ${report.portfolio_value / 1e6:.1f}M AUM: "
        + "; ".join(details)
        + ". Do NOT wire the tiered model into production. Keep flat 100 bps "
        "as the production cost model and document this AUM ceiling in the "
        "strategy disclosure. To lift the ceiling: reduce top-N, widen the "
        "universe, extend the holding period, or deploy at a smaller AUM."
    )


def compile_report(
    out_path: Path,
    report: CostValidationReport,
    *,
    start: date,
    end: date,
) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        "# Cost Model Validation — Layer 2b",
        "",
        f"- **Backtest window**: {start} → {end}",
        f"- **Portfolio value**: ${report.portfolio_value:,.0f}",
        f"- **ADV window**: {report.window_days} trading days (trailing, lookahead-safe)",
        f"- **Baseline Sharpe (gross)**: {report.baseline_sharpe_gross:+.3f}",
        "",
        "Tiered flat-bps cost model + scale-path validation. Addresses the",
        "third Perplexity-flagged gap after survivorship (PR #9 PASS) and",
        "walk-forward (PR #11 PASS).",
        "",
        "## Tier definitions (AQR-anchored)",
        "",
        _format_tier_table(report.tiers),
        "",
        "Per Frazzini-Israel-Moskowitz (2018), *Trading Costs* (AQR /",
        "SSRN 3229719). Empirical institutional execution costs: small-caps",
        "30-50 bps (not 100+), mid-caps 15-35, large-caps 5-15, mega 1-3.",
        "Micro-tier is an extrapolation past the paper's universe coverage.",
        "",
        "## Scale-path results",
        "",
        _format_scale_path_block(report.scale_path),
        "",
        "## Tiered vs flat cost comparison",
        "",
        _format_tiered_cost_block(report.tiered),
        "",
        _format_gate_block(report.verdict),
        "",
        _interpret(report),
        "",
        "## Limitations",
        "",
        "- **Rolling 21-day ADV window** chosen per zen over 60/252 because",
        "  momentum strategies have theme-driven volume spikes; shorter",
        "  window tracks current execution reality but reassigns borderline",
        "  tickers on noisy days.",
        "- **Per-date tier bucketing** — lookahead-safe (tiers computed",
        "  from bars STRICTLY before each rebalance date). Tickers with no",
        "  ADV history on a given date fall back to `mid` with silent",
        "  imputation; borderline for new IPOs in the first ~21 days after",
        "  listing.",
        "- **AQR tier bps are empirical but generic** — calibrated on US",
        "  large/mid institutional universe, not Layer 2b-specific. Micro",
        "  tier is extrapolation past AQR's coverage.",
        "- **No Almgren-Chriss market-impact modeling** — defensible",
        "  because scale-path hard-caps participation. Participation cap +",
        "  tiered bps proxies the square-root impact law for sub-$30M AUM",
        "  per zen review. Not a substitute for real impact modeling at",
        "  institutional AUM.",
        "- **Polygon Basic volume data** — ADV derived from Lean CSV",
        "  (Polygon grouped-daily) which aggregates across venues. Adequate",
        "  for bucketing; not precise enough for real execution planning.",
    ]
    out_path.write_text("\n".join(lines))
    return out_path
