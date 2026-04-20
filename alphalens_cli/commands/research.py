"""`alphalens research` — eksperymenty / walidacje poza produkcyjnym pipeline'em."""

from __future__ import annotations

from datetime import date
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


@research_app.command(name="survivorship-pit")
def survivorship_pit(
    start: str = typer.Option(
        "2021-06-01",
        help="Backtest start (YYYY-MM-DD). Polygon Basic entitlement floor is 2021-06-01.",
    ),
    end: str = typer.Option("2026-04-17", help="Backtest end (YYYY-MM-DD)"),
    top_n: int = typer.Option(5, help="Top-N picks per day"),
    holding: int = typer.Option(5, help="Holding period in trading days"),
    weighting: str = typer.Option("linear", help="Position weighting: linear | equal | conviction"),
    benchmark: str = typer.Option("SPY", help="Benchmark for calendar"),
    tests: str = typer.Option(
        "c1,c2,c3",
        help="Comma-separated subset of tests to run (c1, c2, c3). Default: all three.",
    ),
    report: str = typer.Option(
        "docs/backtest/survivorship_pit_a.md",
        help="Markdown report output path (relative to repo root)",
    ),
    parquet: str = typer.Option(
        "",
        help="Path to delisted events parquet. Default: ~/.alphalens/survivorship/delisted_2021_2026.parquet",
    ),
) -> None:
    """Test A-lite — survivorship diagnostic battery for Layer 2b.

    Three targeted diagnostics closing Test B's selection-bias blind spot:
      C1 — cohort split (pre-existing vs post-IPO tickers)
      C2 — delisting selection bias (Fisher exact over 30/90/180d)
      C3 — mid-holding wipeout audit (−100% vs NaN re-norm)

    Requires `~/.alphalens/survivorship/delisted_2021_2026.parquet` —
    populate via `scripts/backfill_delisted_2021_2024.py` first.
    """
    import yaml

    from alphalens.backtest.engine import BacktestEngine
    from alphalens.backtest.factors import load_carhart_daily
    from alphalens.backtest.history_store import HistoryStore
    from alphalens.backtest.survivorship_pit import (
        audit_mid_holding_wipeout,
        compile_report,
        compute_selection_bias,
        load_delisting_events,
        run_cohort_backtests,
        split_universe_by_ipo_cohort,
    )
    from alphalens.screeners.lean.config import DATA_DIR
    from alphalens.screeners.lean.lean_csv_loader import load_lean_histories
    from alphalens.screeners.themed.backtest_adapter import momentum_scorer_adapter
    from alphalens.screeners.themed.config import THEMED_DEFAULTS, UNIVERSE_PATH
    from alphalens.screeners.themed.universe import flatten_universe

    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)
    tests_set = {t.strip().lower() for t in tests.split(",") if t.strip()}

    typer.echo(f"Survivorship PIT battery — {start_date} → {end_date}, tests={sorted(tests_set)}")

    # Universe + histories
    universe_yaml = yaml.safe_load(UNIVERSE_PATH.read_text())
    screener_tickers = sorted(flatten_universe(universe_yaml).keys())
    typer.echo(f"Loading OHLCV for {len(screener_tickers)} themed tickers + {benchmark}…")
    histories = load_lean_histories(DATA_DIR, screener_tickers + [benchmark])
    store = HistoryStore(histories)
    typer.echo(f"  loaded {len(store.tickers())} tickers")

    scorer_config = dict(THEMED_DEFAULTS, benchmark=benchmark)

    # Load events (parquet + existing YAML fixture)
    parquet_path = (
        Path(parquet)
        if parquet
        else Path.home() / ".alphalens" / "survivorship" / "delisted_2021_2026.parquet"
    )
    yaml_fixture = (
        Path(__file__).resolve().parent.parent.parent
        / "alphalens"
        / "screeners"
        / "lean"
        / "lean_project"
        / "delisted_universe.yaml"
    )
    events = load_delisting_events(parquet_path=parquet_path, yaml_path=yaml_fixture)
    typer.echo(f"  loaded {len(events)} delisting events")

    # Try to load Carhart factors (optional — α t-stat omitted if missing)
    carhart = None
    try:
        carhart = load_carhart_daily(start=start_date, end=end_date)
    except (FileNotFoundError, ValueError) as exc:
        typer.echo(f"  Carhart factors unavailable: {exc} (α t-stat = 0.0)")

    cohort_results = None
    bias_results = None
    audit = None

    if "c1" in tests_set:
        typer.echo("\n[C1] cohort split…")
        pre, post = split_universe_by_ipo_cohort(store, screener_tickers, start_date)
        typer.echo(f"  pre-existing: {len(pre)}  post-IPO: {len(post)}")
        cohort_results = run_cohort_backtests(
            store, pre, post,
            scorer=momentum_scorer_adapter,
            scorer_config=scorer_config,
            start=start_date, end=end_date,
            benchmark=benchmark, top_n=top_n,
            holding_period=holding, weighting=weighting,
            carhart_factors=carhart,
        )
        for r in cohort_results:
            typer.echo(
                f"  {r.cohort_label:<13} n={r.ticker_count:>3}  "
                f"Sharpe={r.sharpe_gross:+.3f}  α_t={r.carhart_alpha_tstat:+.2f}"
            )

    if "c2" in tests_set or "c3" in tests_set:
        # Both need a baseline backtest with retained daily picks
        typer.echo("\n[baseline] running full-universe backtest for picks extraction…")
        engine = BacktestEngine(
            store,
            scorer=momentum_scorer_adapter,
            scorer_config=scorer_config,
            holding_period=holding,
            top_n=top_n,
            benchmark=benchmark,
            screener_tickers=screener_tickers,
            weighting=weighting,
        )
        baseline = engine.run(start=start_date, end=end_date)
        typer.echo(f"  {len(baseline.daily_results)} daily snapshots")

        if "c2" in tests_set:
            typer.echo("\n[C2] selection bias…")
            from alphalens.backtest.survivorship_pit import picks_from_report
            picks_df = picks_from_report(baseline)
            bias_results = compute_selection_bias(
                picks_df, events, screener_tickers, windows=(30, 90, 180)
            )
            for r in bias_results:
                typer.echo(
                    f"  {r.window_days:>3}d  lift={r.lift_ratio:.2f}  p={r.fisher_p:.4f}  "
                    f"picks={r.n_delistings_in_picks}/{r.n_picks}  univ={r.universe_n_delistings}/{r.universe_n}"
                )

        if "c3" in tests_set:
            typer.echo("\n[C3] mid-holding wipeout audit…")
            audit = audit_mid_holding_wipeout(
                baseline, events,
                carhart_factors=carhart,
                weighting_scheme=weighting,
            )
            typer.echo(
                f"  affected: {audit.n_picks_affected}/{audit.n_total_picks} "
                f"({audit.pct_affected * 100:.2f}%)"
            )
            typer.echo(
                f"  Sharpe: {audit.sharpe_baseline:+.3f} → {audit.sharpe_wipeout:+.3f}  "
                f"(Δ {audit.delta_sharpe:+.3f})"
            )

    report_path = Path(report)
    if not report_path.is_absolute():
        report_path = Path.cwd() / report_path
    compile_report(
        report_path,
        window_start=start_date,
        window_end=end_date,
        benchmark=benchmark,
        top_n=top_n,
        holding_period=holding,
        cohort_results=cohort_results,
        bias_results=bias_results,
        audit=audit,
        limitations=[
            "2021-04-19 → 2021-05-31 uncovered: Polygon Basic entitlement floor is 2021-06-01.",
            "Delisting-reason classification is heuristic (warrant suffix / SPAC keywords). "
            "~86% of events classified as 'unknown' — merger and bankruptcy conflated.",
            "Wipeout treatment (−100%) is optimistic upper-bound on pessimism; "
            "real mid-holding delisting has execution lag and partial fills. "
            "Current NaN re-norm is optimistic lower bound. True delta sits between.",
            "Post-IPO cohort has reduced effective universe early in the window — "
            "tickers that IPO'd 2023+ only become scorable once they reach "
            "MIN_BARS_REQUIRED=220 bars.",
        ],
    )
    typer.echo(f"\nReport written to {report_path}")


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
