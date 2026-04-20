"""Markdown + CSV report assembly for a `BacktestReport`.

Consumes `BacktestReport.daily_results` plus cost/regime/factor outputs and
produces a human-readable summary suitable for committing to
`docs/backtest/mvp1_*.md` or pasting into Telegram.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from alphalens.backtest.cost_model import cost_sensitivity_table
from alphalens.backtest.factor_analysis import AlphaResult, format_attribution_table
from alphalens.backtest.metrics import (
    concentration_top_k,
    rank_ic_positive_pct,
    rank_ic_tstat,
    summarise_portfolio,
)
from alphalens.backtest.regime import RegimeStats
from alphalens.backtest.theme_analysis import ThemeSeriesStats, format_theme_summary

from .diagnostics import DecileICResult, VolDecomposition, format_vol_decomposition
from .engine import BacktestReport


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

    # Top-5 concentration — if the engine is using equal weights, each daily top-N
    # entry contributes 1/N. Look at the most recent snapshot for a snapshot estimate.
    if report.daily_results:
        last = report.daily_results[-1]
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


def daily_results_to_dataframe(report: BacktestReport) -> pd.DataFrame:
    """Flatten `daily_results` into a single DataFrame for CSV export."""
    if not report.daily_results:
        return pd.DataFrame()
    rows = []
    for r in report.daily_results:
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
    lines: list[str] = []
    lines.append(f"# MVP1 Backtest Report")
    lines.append("")
    lines.append(f"- **Window**: {report.start} → {report.end}")
    lines.append(f"- **Benchmark**: {report.benchmark}")
    lines.append(f"- **Top-N**: {report.top_n}")
    lines.append(f"- **Holding period**: {report.holding_period} trading days")
    lines.append(f"- **Screener universe**: {report.universe_ticker_count} tickers")
    lines.append(f"- **Backtest days**: {summary.days}")
    lines.append("")

    lines.append("## Headline metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Sharpe (gross) | {summary.sharpe_gross:+.3f} |")
    lines.append(f"| Sharpe (moderate 100 bps) | {summary.sharpe_moderate:+.3f} |")
    lines.append(f"| Sharpe (conservative 150 bps) | {summary.sharpe_conservative:+.3f} |")
    lines.append(f"| Annual return (moderate net) | {summary.annual_return_moderate * 100:+.2f}% |")
    lines.append(f"| Max drawdown | {summary.max_drawdown * 100:+.2f}% |")
    lines.append(f"| Calmar ratio | {summary.calmar:+.3f} |")
    lines.append(f"| Hit rate (vs universe median) | {summary.hit_rate * 100:.1f}% |")
    lines.append(f"| Mean Rank IC | {summary.mean_ic:+.4f} |")
    lines.append(f"| IC t-stat | {summary.ic_tstat:+.2f} |")
    lines.append(f"| IC positive windows (20d) | {summary.ic_positive_pct * 100:.1f}% |")
    lines.append(f"| Turnover (daily rebalance) | {summary.turnover * 100:.1f}% |")
    lines.append(f"| Concentration top-5 | {summary.concentration_top5 * 100:.1f}% |")
    lines.append("")

    if cost_sensitivity is not None and not cost_sensitivity.empty:
        lines.append("## Cost sensitivity")
        lines.append("")
        lines.append("| Profile | Drag (bps/yr) | Sharpe | Annual return |")
        lines.append("| --- | ---: | ---: | ---: |")
        for _, row in cost_sensitivity.iterrows():
            lines.append(
                f"| {row['profile']} | {row['drag_bps']:.0f} | "
                f"{row['sharpe']:+.3f} | {row['annual_return'] * 100:+.2f}% |"
            )
        lines.append("")

    if regime_stats:
        lines.append("## Regime breakdown")
        lines.append("")
        lines.append("| Regime | Days | Sharpe | Annual return | Mean IC | Hit rate |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
        for label in ("bull", "bear", "flat"):
            if label in regime_stats:
                s = regime_stats[label]
                lines.append(
                    f"| {label} | {s.days} | {s.sharpe:+.3f} | "
                    f"{s.annual_return * 100:+.2f}% | {s.mean_ic:+.4f} | "
                    f"{s.hit_rate * 100:.1f}% |"
                )
        lines.append("")

    if decile_ic:
        lines.append("## IC by score decile (tail-concentration test)")
        lines.append("")
        lines.append(f"**Tail concentration score**: {tail_score:.2f} (>1.5 = strong tails, ~1.0 = flat)")
        lines.append("")
        lines.append("| Decile | n samples | Mean return | Std | Sharpe within |")
        lines.append("| ---: | ---: | ---: | ---: | ---: |")
        for r in decile_ic:
            lines.append(
                f"| {r.decile} | {r.n_samples:,} | {r.mean_return * 100:+.3f}% "
                f"| {r.std_return * 100:.3f}% | {r.sharpe_within_decile:+.2f} |"
            )
        lines.append("")

    if vol_decomp:
        lines.append("## Regime vol decomposition (is top-N just defensive?)")
        lines.append("")
        lines.append("```")
        lines.append(format_vol_decomposition(vol_decomp))
        lines.append("```")
        lines.append("")
        lines.append("Interpretation: if **vol_ratio < 0.8 AND excess_return near zero**, "
                     "top-N is capturing defensive low-vol positioning rather than predictive alpha.")
        lines.append("")

    if theme_stats is not None and theme_stats.all_themes:
        lines.append("## Factor/theme concentration (factor-aware monitoring)")
        lines.append("")
        lines.append("```")
        lines.append(format_theme_summary(theme_stats, n_total_days=len(report.daily_results)))
        lines.append("```")
        lines.append("")
        if theme_stats.concentration_alert_days > 0:
            alert_pct = theme_stats.concentration_alert_days / max(len(report.daily_results), 1) * 100
            lines.append(
                f"**Alert**: {alert_pct:.1f}% dni miało koncentrację w jednym temacie "
                f"> {theme_stats.concentration_threshold * 100:.0f}%. Portfolio zachowuje się "
                f"jak single-theme bet a nie diversified thematic basket w tym oknie."
            )
            lines.append("")

    carhart = None
    if attribution:
        carhart = next((r for r in attribution if r.spec_name == "Carhart-4F"), None)
        lines.append("## Factor attribution (CAPM / FF3 / Carhart-4F, HAC t-stats)")
        lines.append("")
        lines.append("```")
        lines.append(format_attribution_table(attribution))
        lines.append("```")
        lines.append("")
        lines.append(
            "Alpha that survives the Carhart row (with momentum factor Mom) is "
            "independent of generic momentum beta. If alpha collapses between FF3 "
            "and Carhart-4F, the strategy is re-packaged UMD exposure."
        )
        lines.append("")

    lines.append("## Decision criteria (MVP1 → paper trade gate)")
    lines.append("")
    deploy_criteria = [
        ("Sharpe (net moderate) > 0.3", summary.sharpe_moderate > 0.3),
        ("IC positive windows > 60%", summary.ic_positive_pct > 0.60),
        (
            "Carhart-4F alpha t-stat > 1.5 (HAC)",
            carhart is not None and carhart.alpha_tstat > 1.5,
        ),
    ]
    pass_count = sum(1 for _, ok in deploy_criteria if ok)
    for name, ok in deploy_criteria:
        marker = "[x]" if ok else "[ ]"
        lines.append(f"- {marker} {name}")
    lines.append("")
    if pass_count == 3:
        lines.append("**Recommendation: DEPLOY** — proceed to launchd + 3-6 month paper trade.")
    elif pass_count >= 1:
        lines.append("**Recommendation: ITERATE** — tweak rule weights or guardrails before deploy.")
    else:
        lines.append("**Recommendation: ABANDON** — no edge detected; rely on Layer 1 + 2b only.")
    lines.append("")

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines))


def make_decision(
    summary: BacktestSummary, attribution: list[AlphaResult] | None
) -> str:
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
