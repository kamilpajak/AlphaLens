# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownParameterType=false, reportUnknownLambdaType=false
"""Markdown + CSV report assembly for a `BacktestReport`.

Consumes `BacktestReport.rebalance_results` plus cost/regime/factor outputs and
produces a human-readable summary suitable for committing to
`docs/backtest/mvp1_*.md` or pasting into Telegram.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from alphalens_research.attribution.cost_model import cost_sensitivity_table
from alphalens_research.attribution.factor_analysis import AlphaResult, format_attribution_table
from alphalens_research.attribution.regime import RegimeStats
from alphalens_research.backtest.engine import BacktestReport
from alphalens_research.backtest.metrics import (
    concentration_top_k,
    rank_ic_positive_pct,
    rank_ic_tstat,
    summarise_portfolio,
)
from alphalens_research.backtest.theme_analysis import ThemeSeriesStats, format_theme_summary

from .diagnostics import DecileICResult, VolDecomposition, format_vol_decomposition


@dataclass(frozen=True)
class BacktestSummary:
    """Flat dict-friendly view of the report for CSV/JSON export."""

    sharpe_gross: float
    sharpe_moderate: float
    sharpe_conservative: float
    annual_return_moderate: float
    max_drawdown: float
    calmar: float
    hit_rate: float
    turnover: float
    mean_ic: float
    ic_tstat: float
    ic_positive_pct: float
    concentration_top5: float
    days: int
    universe_ticker_count: int


def build_summary(report: BacktestReport) -> BacktestSummary:
    """Compute the core headline metrics used in the final decision matrix."""
    port = report.portfolio_returns
    ic_series = report.ic_series
    median = report.universe_median_returns

    summary = summarise_portfolio(port.tolist(), median.tolist())
    cost_df = cost_sensitivity_table(port.tolist())

    # Top-5 concentration — if the engine is using equal weights, each
    # rebalance top-N entry contributes 1/N. Look at the most recent snapshot.
    if report.rebalance_results:
        last = report.rebalance_results[-1]
        weights = [1.0 / max(len(last.top_n_tickers), 1)] * len(last.top_n_tickers)
        conc = concentration_top_k(weights, k=5)
    else:
        conc = 0.0

    return BacktestSummary(
        sharpe_gross=float(cost_df.loc[cost_df["profile"] == "gross", "sharpe"].iloc[0]),
        sharpe_moderate=float(cost_df.loc[cost_df["profile"] == "moderate", "sharpe"].iloc[0]),
        sharpe_conservative=float(
            cost_df.loc[cost_df["profile"] == "conservative", "sharpe"].iloc[0]
        ),
        annual_return_moderate=float(
            cost_df.loc[cost_df["profile"] == "moderate", "annual_return"].iloc[0]
        ),
        max_drawdown=summary.max_drawdown,
        calmar=summary.calmar,
        hit_rate=summary.hit_rate,
        turnover=report.turnover,
        mean_ic=float(ic_series.mean()) if not ic_series.empty else 0.0,
        ic_tstat=rank_ic_tstat(ic_series.tolist()),
        ic_positive_pct=rank_ic_positive_pct(ic_series.tolist(), window=20),
        concentration_top5=conc,
        days=summary.days,
        universe_ticker_count=report.universe_ticker_count,
    )


def rebalance_results_to_dataframe(report: BacktestReport) -> pd.DataFrame:
    """Flatten `rebalance_results` into a single DataFrame for CSV export."""
    if not report.rebalance_results:
        return pd.DataFrame()
    rows = []
    for r in report.rebalance_results:
        rows.append(
            {
                "date": r.date.date().isoformat(),
                "scored_count": r.scored_count,
                "portfolio_return": r.portfolio_return,
                "universe_median_return": r.universe_median_return,
                "ic": r.ic,
                "top_n_tickers": ",".join(r.top_n_tickers),
                "top_n_scores": ",".join(f"{s:.4f}" for s in r.top_n_scores),
            }
        )
    return pd.DataFrame(rows)


def _section_header(report: BacktestReport, summary: BacktestSummary) -> list[str]:
    return [
        "# MVP1 Backtest Report",
        "",
        f"- **Window**: {report.start} → {report.end}",
        f"- **Benchmark**: {report.benchmark}",
        f"- **Top-N**: {report.top_n}",
        f"- **Holding period**: {report.holding_period} trading days",
        f"- **Screener universe**: {report.universe_ticker_count} tickers",
        f"- **Backtest days**: {summary.days}",
        "",
    ]


def _section_headline_metrics(summary: BacktestSummary) -> list[str]:
    return [
        "## Headline metrics",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Sharpe (gross) | {summary.sharpe_gross:+.3f} |",
        f"| Sharpe (moderate 100 bps) | {summary.sharpe_moderate:+.3f} |",
        f"| Sharpe (conservative 150 bps) | {summary.sharpe_conservative:+.3f} |",
        f"| Annual return (moderate net) | {summary.annual_return_moderate * 100:+.2f}% |",
        f"| Max drawdown | {summary.max_drawdown * 100:+.2f}% |",
        f"| Calmar ratio | {summary.calmar:+.3f} |",
        f"| Hit rate (vs universe median) | {summary.hit_rate * 100:.1f}% |",
        f"| Mean Rank IC | {summary.mean_ic:+.4f} |",
        f"| IC t-stat | {summary.ic_tstat:+.2f} |",
        f"| IC positive windows (20d) | {summary.ic_positive_pct * 100:.1f}% |",
        f"| Turnover (per rebalance) | {summary.turnover * 100:.1f}% |",
        f"| Concentration top-5 | {summary.concentration_top5 * 100:.1f}% |",
        "",
    ]


def _section_cost_sensitivity(cost_sensitivity: pd.DataFrame | None) -> list[str]:
    if cost_sensitivity is None or cost_sensitivity.empty:
        return []
    lines = [
        "## Cost sensitivity",
        "",
        "| Profile | Drag (bps/yr) | Sharpe | Annual return |",
        "| --- | ---: | ---: | ---: |",
    ]
    for _, row in cost_sensitivity.iterrows():
        lines.append(
            f"| {row['profile']} | {row['drag_bps']:.0f} | "
            f"{row['sharpe']:+.3f} | {row['annual_return'] * 100:+.2f}% |"
        )
    lines.append("")
    return lines


def _section_regime_breakdown(regime_stats: Mapping[str, RegimeStats] | None) -> list[str]:
    if not regime_stats:
        return []
    lines = [
        "## Regime breakdown",
        "",
        "| Regime | Days | Sharpe | Annual return | Mean IC | Hit rate |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label in ("bull", "bear", "flat"):
        if label not in regime_stats:
            continue
        s = regime_stats[label]
        lines.append(
            f"| {label} | {s.days} | {s.sharpe:+.3f} | "
            f"{s.annual_return * 100:+.2f}% | {s.mean_ic:+.4f} | "
            f"{s.hit_rate * 100:.1f}% |"
        )
    lines.append("")
    return lines


def _section_decile_ic(decile_ic: list[DecileICResult] | None, tail_score: float) -> list[str]:
    if not decile_ic:
        return []
    lines = [
        "## IC by score decile (tail-concentration test)",
        "",
        f"**Tail concentration score**: {tail_score:.2f} (>1.5 = strong tails, ~1.0 = flat)",
        "",
        "| Decile | n samples | Mean return | Std | Sharpe within |",
        "| ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in decile_ic:
        lines.append(
            f"| {r.decile} | {r.n_samples:,} | {r.mean_return * 100:+.3f}% "
            f"| {r.std_return * 100:.3f}% | {r.sharpe_within_decile:+.2f} |"
        )
    lines.append("")
    return lines


def _section_vol_decomp(vol_decomp: Mapping[str, VolDecomposition] | None) -> list[str]:
    if not vol_decomp:
        return []
    return [
        "## Regime vol decomposition (is top-N just defensive?)",
        "",
        "```",
        format_vol_decomposition(vol_decomp),
        "```",
        "",
        "Interpretation: if **vol_ratio < 0.8 AND excess_return near zero**, "
        "top-N is capturing defensive low-vol positioning rather than predictive alpha.",
        "",
    ]


def _section_theme_concentration(
    theme_stats: ThemeSeriesStats | None, n_total_days: int
) -> list[str]:
    if theme_stats is None or not theme_stats.all_themes:
        return []
    lines = [
        "## Factor/theme concentration (factor-aware monitoring)",
        "",
        "```",
        format_theme_summary(theme_stats, n_total_days=n_total_days),
        "```",
        "",
    ]
    if theme_stats.concentration_alert_days > 0:
        alert_pct = theme_stats.concentration_alert_days / max(n_total_days, 1) * 100
        lines.append(
            f"**Alert**: {alert_pct:.1f}% of days had a single-theme concentration "
            f"> {theme_stats.concentration_threshold * 100:.0f}%. The portfolio "
            f"behaves as a single-theme bet, not a diversified thematic basket, "
            f"over this window."
        )
        lines.append("")
    return lines


def _section_attribution(attribution: list[AlphaResult] | None) -> list[str]:
    if not attribution:
        return []
    return [
        "## Factor attribution (CAPM / FF3 / Carhart-4F, HAC t-stats)",
        "",
        "```",
        format_attribution_table(attribution),
        "```",
        "",
        "Alpha that survives the Carhart row (with momentum factor Mom) is "
        "independent of generic momentum beta. If alpha collapses between FF3 "
        "and Carhart-4F, the strategy is re-packaged UMD exposure.",
        "",
    ]


def _section_decision_criteria(
    summary: BacktestSummary, attribution: list[AlphaResult] | None
) -> list[str]:
    carhart = (
        next((r for r in attribution if r.spec_name == "Carhart-4F"), None) if attribution else None
    )
    deploy_criteria = [
        ("Sharpe (net moderate) > 0.3", summary.sharpe_moderate > 0.3),
        ("IC positive windows > 60%", summary.ic_positive_pct > 0.60),
        (
            "Carhart-4F alpha t-stat > 1.5 (HAC)",
            carhart is not None and carhart.alpha_tstat > 1.5,
        ),
    ]
    lines = ["## Decision criteria (MVP1 → paper trade gate)", ""]
    for name, ok in deploy_criteria:
        lines.append(f"- {'[x]' if ok else '[ ]'} {name}")
    lines.append("")

    pass_count = sum(1 for _, ok in deploy_criteria if ok)
    if pass_count == 3:
        lines.append("**Recommendation: DEPLOY** — proceed to launchd + 3-6 month paper trade.")
    elif pass_count >= 1:
        lines.append(
            "**Recommendation: ITERATE** — tweak rule weights or guardrails before deploy."
        )
    else:
        lines.append("**Recommendation: ABANDON** — no edge detected; rely on Layer 1 + 2b only.")
    lines.append("")
    return lines


def write_markdown_report(
    report: BacktestReport,
    path: Path,
    summary: BacktestSummary,
    attribution: list[AlphaResult] | None = None,
    regime_stats: Mapping[str, RegimeStats] | None = None,
    cost_sensitivity: pd.DataFrame | None = None,
    decile_ic: list[DecileICResult] | None = None,
    vol_decomp: Mapping[str, VolDecomposition] | None = None,
    tail_score: float = 0.0,
    theme_stats: ThemeSeriesStats | None = None,
) -> None:
    """Write the full markdown decision-matrix report to `path`."""
    lines: list[str] = [
        *_section_header(report, summary),
        *_section_headline_metrics(summary),
        *_section_cost_sensitivity(cost_sensitivity),
        *_section_regime_breakdown(regime_stats),
        *_section_decile_ic(decile_ic, tail_score),
        *_section_vol_decomp(vol_decomp),
        *_section_theme_concentration(theme_stats, len(report.rebalance_results)),
        *_section_attribution(attribution),
        *_section_decision_criteria(summary, attribution),
    ]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines))


def make_decision(summary: BacktestSummary, attribution: list[AlphaResult] | None) -> str:
    """Pure helper — returns 'deploy' / 'iterate' / 'abandon' based on criteria.

    Uses the Carhart-4F spec (with Mom factor) as the alpha gate: it's the
    strictest of the three, so surviving it means the edge is independent of
    generic momentum beta.
    """
    carhart = None
    if attribution:
        carhart = next((r for r in attribution if r.spec_name == "Carhart-4F"), None)
    checks = [
        summary.sharpe_moderate > 0.3,
        summary.ic_positive_pct > 0.60,
        carhart is not None and carhart.alpha_tstat > 1.5,
    ]
    n = sum(1 for c in checks if c)
    if n == 3:
        return "deploy"
    if n >= 1:
        return "iterate"
    return "abandon"
