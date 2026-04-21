"""Top-level `backtest` — screener-agnostic backtest harness."""

from __future__ import annotations

from pathlib import Path

import typer


def backtest(
    start: str = typer.Option("2021-04-19", help="Backtest window start (YYYY-MM-DD)"),
    end: str = typer.Option("2026-04-17", help="Backtest window end (YYYY-MM-DD)"),
    scorer: str = typer.Option(
        "momentum",
        "--scorer",
        help="Scorer: 'momentum' (Layer 2b, validated), 'early-stage' (Layer 2b, "
             "production scheduled from 2026-04-21), or 'lean' (Layer 2c, archived)",
    ),
    top_n: int = typer.Option(5, help="Top-N names to hold at each rebalance"),
    holding: int = typer.Option(5, help="Holding period in trading days"),
    weighting: str = typer.Option(
        "linear", "--weighting", help="Position weighting: linear | equal"
    ),
    benchmark: str = typer.Option("SPY", help="Benchmark ticker for calendar + regime"),
    report: str = typer.Option(
        "docs/backtest/mvp1_report.md",
        help="Markdown report output path (relative to repo root)",
    ),
    csv: str = typer.Option(
        "", help="Optional daily results CSV output path (empty = skip)"
    ),
    no_attrib: bool = typer.Option(
        False, "--no-attrib", help="Skip factor attribution (CAPM/FF3/Carhart-4F) regressions"
    ),
    diagnose: bool = typer.Option(
        False,
        "--diagnose",
        help="Retain per-day scored frames and append IC-by-decile + vol decomposition to report",
    ),
    fundamental_gate: bool = typer.Option(
        False,
        "--fundamental-gate/--no-fundamental-gate",
        help="Apply Layer 2b fundamental soft-guardrail (issue #14). Pre-loads Alpha Vantage "
             "fundamentals once for the universe and multiplies the technical composite by "
             "the gate score. Only meaningful for --scorer momentum | early-stage.",
    ),
) -> None:
    """Run backtest over Lean CSV data and emit a decision-matrix report.

    Default wiring matches the live/validated strategy: Layer 2b themed screener
    with momentum scorer, top-5, linear weighting. Pass `--scorer lean` to
    re-examine the archived Layer 2c strategy.
    """
    from datetime import date

    import yaml

    from alphalens.backtest.cost_model import cost_sensitivity_table
    from alphalens.backtest.engine import BacktestEngine
    from alphalens.backtest.factor_analysis import run_carhart_attribution
    from alphalens.backtest.factors import load_carhart_daily
    from alphalens.backtest.history_store import HistoryStore
    from alphalens.backtest.regime import (
        classify_regime,
        regime_breakdown,
    )
    from alphalens.backtest.report import (
        build_summary,
        daily_results_to_dataframe,
        write_markdown_report,
    )
    from alphalens.screeners.lean.config import DATA_DIR
    from alphalens.screeners.lean.lean_csv_loader import load_lean_histories


    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)

    if scorer == "momentum":
        from alphalens.screeners.themed.backtest_adapter import momentum_scorer_adapter
        from alphalens.screeners.themed.config import THEMED_DEFAULTS, UNIVERSE_PATH
        from alphalens.screeners.themed.universe import flatten_universe

        universe = yaml.safe_load(UNIVERSE_PATH.read_text())
        screener_tickers = sorted(flatten_universe(universe).keys())
        scorer_fn = momentum_scorer_adapter
        scorer_config = dict(THEMED_DEFAULTS, benchmark=benchmark)
        typer.echo(f"Scorer: Layer 2b momentum ({len(screener_tickers)} curated tickers)")
    elif scorer == "early-stage":
        from alphalens.screeners.themed.backtest_adapter import early_stage_scorer_adapter
        from alphalens.screeners.themed.config import THEMED_DEFAULTS, UNIVERSE_PATH
        from alphalens.screeners.themed.early_stage_scorer import EARLY_STAGE_DEFAULTS
        from alphalens.screeners.themed.universe import flatten_universe

        universe = yaml.safe_load(UNIVERSE_PATH.read_text())
        screener_tickers = sorted(flatten_universe(universe).keys())
        scorer_fn = early_stage_scorer_adapter
        scorer_config = {**THEMED_DEFAULTS, **EARLY_STAGE_DEFAULTS, "benchmark": benchmark}
        typer.echo(
            f"Scorer: Layer 2b early-stage ({len(screener_tickers)} curated tickers)"
        )
    elif scorer == "lean":
        from alphalens.screeners.lean.config import BENCHMARKS, LEAN_DEFAULTS
        from alphalens.screeners.lean.lean_project.scorer import rank_universe as lean_rank
        from alphalens.screeners.lean.universe import all_tickers

        screener_tickers = all_tickers()
        scorer_fn = lean_rank
        scorer_config = LEAN_DEFAULTS
        typer.echo(
            f"Scorer: Layer 2c Lean (archived, {len(screener_tickers)} tickers) — "
            "this strategy failed 5-year validation per CLAUDE.md"
        )
    else:
        raise typer.BadParameter(
            f"Unknown --scorer: {scorer!r} (expected: momentum | early-stage | lean)"
        )

    if fundamental_gate and scorer in ("momentum", "early-stage"):
        from alphalens.fundamentals.backtest_store import HistoricalFundamentalsStore

        typer.echo(
            f"Preloading fundamentals for {len(screener_tickers)} tickers (one-shot)…"
        )
        fundamentals_store = HistoricalFundamentalsStore()
        fundamentals_store.preload(screener_tickers)
        scorer_config["fundamental_gate_enabled"] = True
        scorer_config["_fundamentals_store"] = fundamentals_store
        typer.echo("  fundamental gate: ON")

    typer.echo(f"Loading OHLCV into HistoryStore ({start_date} → {end_date})…")
    if scorer == "lean":
        tickers = screener_tickers + list(BENCHMARKS)  # type: ignore[possibly-undefined]
    else:
        tickers = screener_tickers + [benchmark]  # themed pipeline scorers (momentum, early-stage)
    histories = load_lean_histories(DATA_DIR, tickers)
    store = HistoryStore(histories)
    typer.echo(f"  loaded {len(store.tickers())} tickers")

    engine = BacktestEngine(
        store,
        scorer=scorer_fn,
        scorer_config=scorer_config,
        holding_period=holding,
        top_n=top_n,
        benchmark=benchmark,
        screener_tickers=screener_tickers,
        weighting=weighting,
        retain_scored_frames=diagnose,
    )

    typer.echo("Running backtest replay…")
    result = engine.run(start=start_date, end=end_date)
    typer.echo(f"  {len(result.daily_results)} daily snapshots")

    if not result.daily_results:
        raise typer.BadParameter(
            "Backtest produced no daily snapshots — check data coverage / warmup"
        )

    summary = build_summary(result)
    cost_df = cost_sensitivity_table(result.portfolio_returns.tolist())

    benchmark_close = store.full(benchmark)["close"]
    regime_labels = classify_regime(benchmark_close)
    regimes = regime_breakdown(
        result.portfolio_returns, result.ic_series, result.universe_median_returns, regime_labels
    )

    attribution = None
    if not no_attrib:
        try:
            carhart = load_carhart_daily(start=start_date, end=end_date)
            attribution = run_carhart_attribution(result.portfolio_returns, carhart)
        except (FileNotFoundError, ValueError) as exc:
            typer.echo(f"  Factor attribution skipped: {exc}")

    decile_ic = []
    vol_decomp = {}
    tail_score = 0.0
    if diagnose and result.scored_frames:
        from alphalens.backtest.diagnostics import (
            format_vol_decomposition,
            ic_by_decile_from_scored_frames,
            tail_concentration_score,
            vol_decomposition_by_regime,
        )

        decile_ic = ic_by_decile_from_scored_frames(result.scored_frames)
        tail_score = tail_concentration_score(decile_ic)
        vol_decomp = vol_decomposition_by_regime(result, regime_labels)
        typer.echo("  diagnostics: tail concentration score = {:.2f}".format(tail_score))
        typer.echo(format_vol_decomposition(vol_decomp))

    from alphalens.backtest.theme_analysis import (
        snapshots_from_backtest,
        theme_series,
    )
    themes_map: dict[str, list[str]] = {}
    try:
        from alphalens.screeners.themed.universe import (
            flatten_universe as flatten_2b,
            load_universe as load_2b,
        )
        themes_map.update(flatten_2b(load_2b()))
    except Exception as exc:
        typer.echo(f"  theme mapping skipped (2b universe not available): {exc}")

    theme_stats = None
    if themes_map:
        snaps = snapshots_from_backtest(result.daily_results, themes_map)
        _, theme_stats = theme_series(snaps, concentration_threshold=0.70)
        if theme_stats.all_themes:
            typer.echo(
                f"  themes: mean HHI={theme_stats.mean_hhi:.3f}, "
                f"alert days={theme_stats.concentration_alert_days}"
            )

    report_path = Path(report)
    if not report_path.is_absolute():
        report_path = Path.cwd() / report_path
    write_markdown_report(
        result, report_path, summary, attribution, regimes, cost_df,
        decile_ic=decile_ic, vol_decomp=vol_decomp, tail_score=tail_score,
        theme_stats=theme_stats,
    )
    typer.echo(f"Report written to {report_path}")

    if csv:
        csv_path = Path(csv)
        if not csv_path.is_absolute():
            csv_path = Path.cwd() / csv_path
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        daily_results_to_dataframe(result).to_csv(csv_path, index=False)
        typer.echo(f"Daily CSV written to {csv_path}")

    typer.echo("")
    typer.echo("=== HEADLINE ===")
    typer.echo(f"  sharpe_gross      = {summary.sharpe_gross:+.3f}")
    typer.echo(f"  sharpe_moderate   = {summary.sharpe_moderate:+.3f}")
    typer.echo(f"  mean_ic           = {summary.mean_ic:+.4f}  (t={summary.ic_tstat:+.2f})")
    typer.echo(f"  ic_positive_pct   = {summary.ic_positive_pct * 100:.1f}%")
    typer.echo(f"  turnover          = {summary.turnover * 100:.1f}%")
    if attribution:
        for r in attribution:
            typer.echo(
                f"  {r.spec_name:<12} alpha_ann = {r.alpha_annualized * 100:+.2f}% "
                f"(t={r.alpha_tstat:+.2f} HAC, R²={r.r_squared:.3f})"
            )
