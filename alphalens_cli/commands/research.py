"""`alphalens research` — eksperymenty / walidacje poza produkcyjnym pipeline'em."""

from __future__ import annotations

from pathlib import Path

import typer

research_app = typer.Typer(
    name="research",
    help="Research utilities — LLM validation, ad-hoc experiments.",
    no_args_is_help=True,
)


@research_app.callback()
def _research_callback() -> None:
    """Force multi-command behaviour even when only one command is registered."""


@research_app.command(name="validate-llm-filter")
def validate_llm_filter(
    start: str = typer.Option("2023-07-01", help="Początek okna picks (YYYY-MM-DD)"),
    end: str = typer.Option("2023-09-30", help="Koniec okna picks"),
    top_n: int = typer.Option(5, help="Rozmiar top-N"),
    scorer: str = typer.Option(
        "rule",
        help="Scorer: 'rule' (baseline, $0), 'gemini' (Flash, ~$0.02/pick), "
             "'hybrid' (rule→Gemini fallback), 'tradingagents' (full pipeline, ~$0.50/pick)",
    ),
    report: str = typer.Option(
        "docs/backtest/llm_filter_validation.md", help="Ścieżka do raportu MD"
    ),
    csv: str = typer.Option("", help="Opcjonalne: ścieżka do CSV z per-pick details"),
    dry_run: bool = typer.Option(False, help="Tylko wygeneruj picks, nie uruchamiaj LLM"),
) -> None:
    """Phase 0 validation per Perplexity — czy LLM rejections korelują z
    subsequent underperformance na historical picks Layer 2b.

    Generuje 60 dni top-N picks z BacktestEngine, uruchamia pluggable scorer,
    agreguje delta(accept_mean - reject_mean) + hit rate deltas + decision."""
    from datetime import date

    from alphalens.backtest.engine import BacktestEngine
    from alphalens.backtest.history_store import HistoryStore
    from alphalens.backtest.historical_validation import (
        evaluate_historical_picks,
        format_decision_matrix,
        picks_from_backtest_report,
        rule_based_tractability_scorer,
    )
    from alphalens.screeners.lean.config import BENCHMARKS, DATA_DIR, LEAN_DEFAULTS
    from alphalens.screeners.lean.lean_csv_loader import load_lean_histories
    from alphalens.screeners.lean.lean_project.scorer import rank_universe as lean_rank
    from alphalens.screeners.themed.universe import (
        flatten_universe,
        load_universe as load_2b,
    )


    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)
    typer.echo(f"Okno picks: {start_date} → {end_date}")

    themes_map = flatten_universe(load_2b())
    screener_tickers = sorted(themes_map.keys())
    typer.echo(f"Ładuję {len(screener_tickers)} tickerów Layer 2b universe...")
    histories = load_lean_histories(DATA_DIR, screener_tickers + list(BENCHMARKS))
    store = HistoryStore(histories)

    engine = BacktestEngine(
        store, scorer=lean_rank, scorer_config=LEAN_DEFAULTS,
        holding_period=5, top_n=top_n, benchmark="SPY",
        screener_tickers=screener_tickers, weighting="linear",
    )
    engine.MIN_BARS_REQUIRED = 252

    typer.echo("Generuję historical picks via BacktestEngine...")
    report_obj = engine.run(start=start_date, end=end_date)
    all_picks = picks_from_backtest_report(report_obj)
    picks_with_themes = []
    for p in all_picks:
        from alphalens.backtest.historical_validation import PickRecord
        picks_with_themes.append(PickRecord(
            asof_date=p.asof_date, ticker=p.ticker, rank=p.rank,
            momentum_score=p.momentum_score,
            themes=themes_map.get(p.ticker, []),
            forward_return=p.forward_return,
        ))
    typer.echo(f"  {len(picks_with_themes)} picks (dni × top-{top_n}) z forward returns")

    if not picks_with_themes:
        raise typer.BadParameter("Brak picks w oknie — zwiększ range lub obniż MIN_BARS_REQUIRED")

    if dry_run:
        typer.echo("--dry-run: skip LLM scoring")
        raise typer.Exit(0)

    if scorer == "rule":
        scorer_fn = rule_based_tractability_scorer
        typer.echo("Scorer: rule-based deterministic (no LLM cost)")
    elif scorer == "gemini":
        from alphalens.backtest.llm_scorers import gemini_flash_tractability_scorer
        scorer_fn = gemini_flash_tractability_scorer
        typer.echo(
            f"Scorer: Gemini Flash tractability — szacunkowy koszt "
            f"${len(picks_with_themes) * 0.02:.2f}"
        )
    elif scorer == "hybrid":
        from alphalens.backtest.llm_scorers import rule_and_gemini_hybrid_scorer
        scorer_fn = rule_and_gemini_hybrid_scorer
        typer.echo("Scorer: hybrid (rule first, Gemini on uncertain)")
    elif scorer == "tradingagents":
        from alphalens.backtest.llm_scorers import tradingagents_reduced_scorer
        scorer_fn = tradingagents_reduced_scorer
        typer.echo(
            f"Scorer: TradingAgents reduced (market+news) — szacunkowy koszt "
            f"${len(picks_with_themes) * 0.75:.2f}, czas ~{len(picks_with_themes) * 5:.0f} min"
        )
    else:
        raise typer.BadParameter(f"Unknown scorer: {scorer!r}")

    typer.echo(f"Uruchamiam scorer na {len(picks_with_themes)} picks...")
    result = evaluate_historical_picks(picks_with_themes, scorer_fn, progress_every=20)

    text = format_decision_matrix(result)
    typer.echo("")
    typer.echo(text)

    report_path = Path(report)
    if not report_path.is_absolute():
        report_path = Path.cwd() / report_path
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        f"# LLM Filter Validation — {scorer} scorer\n\n"
        f"Window: {start_date} → {end_date}, top-{top_n}, {len(picks_with_themes)} picks\n\n"
        f"```\n{text}\n```\n"
    )
    typer.echo(f"\nReport: {report_path}")

    if csv:
        csv_path = Path(csv)
        if not csv_path.is_absolute():
            csv_path = Path.cwd() / csv_path
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        import pandas as pd
        pd.DataFrame(result.per_pick_evaluations).to_csv(csv_path, index=False)
        typer.echo(f"CSV: {csv_path}")
