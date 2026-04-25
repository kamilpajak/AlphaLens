"""`alphalens rotation` — Tactical Sector Rotation (Layer 2e).

Subcommands:
  backtest — run IS/OOS backtest on committed config, write markdown report
  run      — compute today's target weights based on latest market data
  status   — show config fingerprint + git SHA + last backtest metadata
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import typer

from alphalens.backtest.metrics import sharpe, sharpe_autocorr_adjusted
from alphalens.macro.scorer import RuleBasedScorer
from alphalens.rotation.allocator import OverlayAllocator
from alphalens.rotation.config import compute_fingerprint, load_config
from alphalens.rotation.data_loader import load_rotation_data
from alphalens.rotation.overlay_engine import OverlayBacktestEngine
from alphalens.rotation.sanity_checks import (
    build_passive_benchmark,
    run_all_sanity_checks,
)

rotation_app = typer.Typer(
    name="rotation",
    help="Layer 2e: Tactical Sector Rotation (quarterly SPY/QQQ/IWM overlay).",
    no_args_is_help=True,
)

_HELP_CONFIG = "Path to rotation YAML config"


@rotation_app.command(name="backtest")
def backtest(
    config: Path = typer.Option(..., help=_HELP_CONFIG),
    start: str = typer.Option(..., help="Backtest start date (YYYY-MM-DD)"),
    end: str = typer.Option(..., help="Backtest end date (YYYY-MM-DD)"),
    output: Path = typer.Option(..., help="Markdown report output path"),
    allow_dirty: bool = typer.Option(
        True, help="Allow dirty git repo when capturing fingerprint (default for dev)"
    ),
) -> None:
    """Run a rotation backtest and write a markdown summary."""
    cfg = load_config(config)
    fingerprint = compute_fingerprint(config, allow_dirty=allow_dirty)

    store, signals = load_rotation_data(start=start, end=end)
    scorer = RuleBasedScorer(cfg.rules)
    allocator = OverlayAllocator(core_weights=cfg.core_weights, max_tilt=cfg.max_tilt)
    engine = OverlayBacktestEngine(
        store=store,
        scorer=scorer,
        allocator=allocator,
        signals=signals,
        etf_spread_bps=cfg.etf_spread_bps,
    )
    result = engine.run(
        start=pd.Timestamp(start),
        end=pd.Timestamp(end),
        rebalance_stride=cfg.rebalance_stride,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_render_report(result, cfg, fingerprint, start, end))
    typer.echo(f"Report written to {output}")
    typer.echo(
        f"Net Sharpe: {sharpe(result.daily_returns_net.tolist()):.2f}, "
        f"rebalances: {len(result.rebalances)}"
    )


@rotation_app.command(name="run")
def run(
    config: Path = typer.Option(..., help=_HELP_CONFIG),
    lookback_days: int = typer.Option(400, help="Days of history to load for signal computation"),
) -> None:
    """Compute today's target weights using latest available data."""
    cfg = load_config(config)
    end = pd.Timestamp.today().normalize()
    start = end - pd.Timedelta(days=lookback_days)

    _store, signals = load_rotation_data(start=start, end=end)
    latest = signals.yield_curve_slope.dropna().index.max()
    if pd.isna(latest):
        raise typer.Exit("No valid signals available for today")

    scorer = RuleBasedScorer(cfg.rules)
    snap = signals.as_of(latest)
    regime = scorer.score(snap)

    allocator = OverlayAllocator(core_weights=cfg.core_weights, max_tilt=cfg.max_tilt)
    weights = allocator.apply(regime)

    typer.echo(f"Signal snapshot as of {latest.date()}:")
    for k, v in snap.items():
        typer.echo(f"  {k}: {v:.4f}")
    typer.echo("\nRule firings:")
    for name, fired in regime.flags.items():
        typer.echo(f"  {name}: {'FIRED' if fired else '-'}")
    typer.echo("\nTarget weights:")
    for ticker in ("SPY", "QQQ", "IWM"):
        typer.echo(f"  {ticker}: {weights[ticker]:.2%}")


@rotation_app.command(name="sanity-check")
def sanity_check(
    config: Path = typer.Option(..., help=_HELP_CONFIG),
    start: str = typer.Option(..., help="IS start date (YYYY-MM-DD)"),
    end: str = typer.Option(..., help="IS end date (YYYY-MM-DD)"),
) -> None:
    """Run IS sanity checks (4 kill-gates) before committing to OOS."""
    cfg = load_config(config)
    store, signals = load_rotation_data(start=start, end=end)
    scorer = RuleBasedScorer(cfg.rules)
    allocator = OverlayAllocator(core_weights=cfg.core_weights, max_tilt=cfg.max_tilt)
    engine = OverlayBacktestEngine(
        store=store,
        scorer=scorer,
        allocator=allocator,
        signals=signals,
        etf_spread_bps=cfg.etf_spread_bps,
    )
    result = engine.run(
        start=pd.Timestamp(start),
        end=pd.Timestamp(end),
        rebalance_stride=cfg.rebalance_stride,
    )
    passive = build_passive_benchmark(store, core_weights=cfg.core_weights)
    benchmark_close = store.full("SPY")["close"]

    report = run_all_sanity_checks(
        strategy_returns=result.daily_returns_net,
        passive_returns=passive,
        benchmark_close=benchmark_close,
    )

    typer.echo(f"=== IS SANITY CHECKS ({start} → {end}) ===\n")
    for check in report.checks:
        status = "PASS" if check.passed else "FAIL"
        typer.echo(f"[{status}] {check.name}")
        typer.echo(f"       {check.detail}")
    verdict = "PASS" if report.passed else "FAIL"
    typer.echo(f"\n=== VERDICT: {verdict} ===")
    if not report.passed:
        typer.echo(
            "\nOOS run is NOT statistically justified. Either fix the strategy "
            "(which bumps true_n_tests) or abandon the hypothesis."
        )
        raise typer.Exit(code=1)
    typer.echo("\nAll 4 gates passed — OOS single-shot is statistically justified.")


@rotation_app.command(name="status")
def status(
    config: Path = typer.Option(..., help=_HELP_CONFIG),
    allow_dirty: bool = typer.Option(True),
) -> None:
    """Show config fingerprint and git SHA for reproducibility."""
    cfg = load_config(config)
    fp = compute_fingerprint(config, allow_dirty=allow_dirty)
    typer.echo(f"Config path: {fp.config_path}")
    typer.echo(f"Git SHA:     {fp.git_sha}")
    typer.echo(f"Content SHA: {fp.content_sha256}")
    typer.echo(f"Rules:       {len(cfg.rules)}")
    typer.echo(f"Core:        {dict(cfg.core_weights)}")
    typer.echo(f"Max tilt:    {cfg.max_tilt}")
    typer.echo(f"Rebalance:   every {cfg.rebalance_stride} trading days")


def _render_report(result, cfg, fingerprint, start, end) -> str:
    gross = result.daily_returns_gross
    net = result.daily_returns_net
    sharpe_gross = sharpe(gross.tolist())
    sharpe_net = sharpe(net.tolist())
    sharpe_net_adj = sharpe_autocorr_adjusted(net.tolist())
    cum_net = (1.0 + net).prod() - 1.0
    cum_gross = (1.0 + gross).prod() - 1.0
    total_turnover = sum(r.turnover for r in result.rebalances)
    total_cost_bps = sum(r.cost_bps for r in result.rebalances)

    lines = [
        "# Tactical Sector Rotation — backtest report",
        "",
        f"**Window:** {start} → {end}",
        f"**Config:** `{fingerprint.config_path}`",
        f"**Git SHA:** `{fingerprint.git_sha}`",
        f"**Content SHA:** `{fingerprint.content_sha256}`",
        "",
        "## Headline metrics",
        "",
        "| metric | value |",
        "|---|---|",
        f"| Sharpe (gross) | {sharpe_gross:.2f} |",
        f"| Sharpe (net) | {sharpe_net:.2f} |",
        f"| Sharpe net (autocorr-adj, Lo 2002) | {sharpe_net_adj:.2f} |",
        f"| Cumulative return (gross) | {cum_gross:.2%} |",
        f"| Cumulative return (net) | {cum_net:.2%} |",
        f"| Rebalances | {len(result.rebalances)} |",
        f"| Total turnover | {total_turnover:.2f} |",
        f"| Total rebalance cost | {total_cost_bps:.1f} bps |",
        "",
        "## Configuration",
        "",
        f"- Core weights: {dict(cfg.core_weights)}",
        f"- Max tilt: ±{cfg.max_tilt:.2%}",
        f"- Rebalance stride: {cfg.rebalance_stride} trading days",
        f"- Rules ({len(cfg.rules)}):",
    ]
    for r in cfg.rules:
        lines.append(f"  - `{r.name}`: {r.signal} {r.operator} {r.threshold} → tilt {dict(r.tilt)}")
    lines += [
        "",
        "## Rebalance log",
        "",
        "| date | turnover | cost (bps) | rules fired |",
        "|---|---|---|---|",
    ]
    for ev in result.rebalances:
        fired = [n for n, f in ev.rule_firings.items() if f]
        lines.append(
            f"| {ev.date.date()} | {ev.turnover:.3f} | {ev.cost_bps:.2f} | "
            f"{', '.join(fired) or '—'} |"
        )
    return "\n".join(lines) + "\n"
