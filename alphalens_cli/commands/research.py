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


@research_app.command(name="cost-validation")
def cost_validation(
    start: str = typer.Option(
        "2021-06-01",
        help="Backtest start (YYYY-MM-DD). Polygon Basic entitlement floor is 2021-06-01.",
    ),
    end: str = typer.Option("2026-04-17", help="Backtest end (YYYY-MM-DD)"),
    portfolio_value: float = typer.Option(
        10_000_000.0,
        "--portfolio-value",
        help="Portfolio size used for participation calculation. Default $10M.",
    ),
    threshold_pct: float = typer.Option(
        15.0,
        "--threshold-pct",
        help="Per-pick ADV participation flag threshold (institutional VWAP working range).",
    ),
    max_threshold_pct: float = typer.Option(
        20.0,
        "--max-threshold-pct",
        help="Absolute ceiling for max single-pick participation.",
    ),
    window_days: int = typer.Option(
        21, help="Trailing window for dollar-ADV computation (trading days)"
    ),
    top_n: int = typer.Option(5, help="Top-N picks per day"),
    holding: int = typer.Option(5, help="Holding period in trading days"),
    weighting: str = typer.Option(
        "linear", help="Position weighting: linear | equal | conviction"
    ),
    benchmark: str = typer.Option("SPY", help="Benchmark for calendar"),
    report: str = typer.Option(
        "docs/backtest/cost_validation.md",
        help="Markdown report output path (relative to repo root)",
    ),
    csv: str = typer.Option(
        "", help="Optional per-pick-day participation CSV output path"
    ),
) -> None:
    """Tiered flat-bps cost model + scale-path validation.

    Single baseline `BacktestEngine.run`; per-pick participation computed
    vs rolling 21-day trailing dollar-ADV (lookahead-safe). Gate: PASS iff
    <5% of pick-days exceed 15% ADV AND max <20%. On PASS, ship tiered
    cost model (AQR-anchored bps: mega=3, large=10, mid=25, small=50,
    micro=100). On FAIL, document AUM ceiling and keep flat 100bps.

    Addresses the third Perplexity-flagged gap after PIT survivorship
    (PR #9 PASS) and walk-forward (PR #11 PASS).
    """
    from datetime import date as _date

    import yaml

    from alphalens.backtest.cost_validation import (
        DEFAULT_TIERS,
        build_per_date_tiers,
        compare_cost_scenarios,
        compile_report as cv_compile_report,
        CostValidationReport,
        evaluate_cost_gate,
        rolling_dollar_adv,
        run_scale_path,
    )
    from alphalens.backtest.engine import BacktestEngine
    from alphalens.backtest.history_store import HistoryStore
    from alphalens.backtest.metrics import sharpe
    from alphalens.screeners.lean.config import DATA_DIR
    from alphalens.screeners.lean.lean_csv_loader import load_lean_histories
    from alphalens.screeners.themed.backtest_adapter import momentum_scorer_adapter
    from alphalens.screeners.themed.config import THEMED_DEFAULTS, UNIVERSE_PATH
    from alphalens.screeners.themed.universe import flatten_universe

    start_date = _date.fromisoformat(start)
    end_date = _date.fromisoformat(end)
    typer.echo(
        f"Cost-validation — {start_date} → {end_date}, "
        f"portfolio ${portfolio_value:,.0f}, window={window_days}d"
    )

    # Load universe + histories
    universe_yaml = yaml.safe_load(UNIVERSE_PATH.read_text())
    screener_tickers = sorted(flatten_universe(universe_yaml).keys())
    typer.echo(f"Loading OHLCV for {len(screener_tickers)} themed tickers + {benchmark}…")
    histories = load_lean_histories(DATA_DIR, screener_tickers + [benchmark])
    store = HistoryStore(histories)
    typer.echo(f"  loaded {len(store.tickers())} tickers")

    scorer_config = dict(THEMED_DEFAULTS, benchmark=benchmark)

    # Single baseline backtest
    typer.echo("\n[baseline] running full-span backtest for picks extraction…")
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

    # Rolling ADV + per-date tier assignments
    typer.echo("\n[adv] computing rolling 21-day dollar-ADV + per-date tiers…")
    rolling_adv = rolling_dollar_adv(store, screener_tickers, window_days=window_days)
    calendar = [snap.date for snap in baseline.daily_results]
    per_date_tiers = build_per_date_tiers(rolling_adv, calendar, DEFAULT_TIERS)
    if calendar:
        last_tiers = per_date_tiers.get(calendar[-1], {})
        tier_counts: dict[str, int] = {}
        for tier in last_tiers.values():
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
        typer.echo(f"  tier counts on {calendar[-1].date()}: {tier_counts}")

    # Scale-path analysis
    typer.echo("\n[scale-path] computing participation distribution…")
    scale_path = run_scale_path(
        baseline, rolling_adv, per_date_tiers,
        portfolio_value=portfolio_value,
        threshold_pct=threshold_pct,
        max_threshold_pct=max_threshold_pct,
        weighting=weighting,
    )
    typer.echo(
        f"  pick-days: {scale_path.n_pick_days}  "
        f"median participation: {scale_path.median_participation:.2%}  "
        f"max: {scale_path.max_participation:.2%}  "
        f"fraction>{threshold_pct:.0f}%: {scale_path.fraction_exceeding_threshold:.2%}"
    )

    # Tiered cost model comparison
    typer.echo("\n[tiered-cost] comparing gross / flat 100 / tiered Sharpe…")
    bps_per_tier = {t.name: t.bps_annual for t in DEFAULT_TIERS}
    tiered = compare_cost_scenarios(
        baseline, per_date_tiers, bps_per_tier, weighting=weighting
    )
    typer.echo(
        f"  gross={tiered.sharpe_gross:+.3f}  "
        f"flat 100bps={tiered.sharpe_flat_100bps:+.3f}  "
        f"tiered={tiered.sharpe_tiered:+.3f}  "
        f"(tiered drag ≈ {tiered.annual_drag_tiered_bps:.0f} bps/yr)"
    )

    # Gate
    verdict = evaluate_cost_gate(scale_path)
    typer.echo(f"\nverdict: {verdict.overall}")
    typer.echo(f"  fraction: {'PASS' if verdict.fraction_pass else 'FAIL'} — {verdict.reasons['fraction']}")
    typer.echo(f"  max:      {'PASS' if verdict.max_pass else 'FAIL'} — {verdict.reasons['max']}")

    # Report
    cv_report = CostValidationReport(
        portfolio_value=portfolio_value,
        window_days=window_days,
        baseline_sharpe_gross=sharpe(baseline.portfolio_returns.tolist()),
        tiers=DEFAULT_TIERS,
        scale_path=scale_path,
        tiered=tiered,
        verdict=verdict,
    )
    report_path = Path(report)
    if not report_path.is_absolute():
        report_path = Path.cwd() / report_path
    cv_compile_report(
        report_path, cv_report,
        start=start_date, end=end_date,
    )
    typer.echo(f"\nReport written to {report_path}")

    if csv:
        import csv as _csv
        csv_path = Path(csv)
        if not csv_path.is_absolute():
            csv_path = Path.cwd() / csv_path
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with csv_path.open("w", newline="") as fh:
            writer = _csv.writer(fh)
            writer.writerow(
                ["date", "ticker", "rank", "tier", "participation", "dollar_position", "dollar_adv"]
            )
            for p in scale_path.worst_offenders:
                writer.writerow([
                    p.date, p.ticker, p.rank, p.tier,
                    f"{p.participation:.6f}",
                    f"{p.dollar_position:.2f}",
                    f"{p.dollar_adv:.2f}",
                ])
        typer.echo(f"Worst-offenders CSV: {csv_path}")


@research_app.command(name="walk-forward")
def walk_forward(
    start: str = typer.Option(
        "2021-06-01",
        help="Backtest start (YYYY-MM-DD). Polygon Basic entitlement floor is 2021-06-01.",
    ),
    end: str = typer.Option("2026-04-17", help="Backtest end (YYYY-MM-DD)"),
    window_days: int = typer.Option(252, help="Test window size in trading days (~1 year)"),
    step_days: int = typer.Option(21, help="Stride between windows in trading days (~1 month)"),
    top_n: int = typer.Option(5, help="Top-N picks per day"),
    holding: int = typer.Option(5, help="Holding period in trading days"),
    weighting: str = typer.Option("linear", help="Position weighting: linear | equal | conviction"),
    benchmark: str = typer.Option("SPY", help="Benchmark for calendar + regime classification"),
    no_attrib: bool = typer.Option(
        False, "--no-attrib",
        help="Skip Carhart per-window attribution (useful when FF factor files are unavailable).",
    ),
    report: str = typer.Option(
        "docs/backtest/walk_forward.md",
        help="Markdown report output path (relative to repo root)",
    ),
    csv: str = typer.Option(
        "", help="Optional per-window CSV output path (empty = skip)"
    ),
) -> None:
    """Walk-forward OOS validation — rolling 252-day test windows.

    Runs one baseline `BacktestEngine.run` over the full span, slices the
    daily_results per window, computes per-window Sharpe / Carhart α_t / IC_t
    / MaxDD / turnover, and reports distribution + decision gate (5 rules).

    Detects regime-specific performance and data-snooping bias. Gate C3
    uses non-overlapping 21-day block-return autocorr (~59 observations,
    statistically defensible) rather than windowed-Sharpe autocorr
    (mechanically near 1 due to 92% overlap). Gate C4 uses a 12-month
    dark-half threshold because momentum strategies have documented
    12-18 month "winters" during reversals.
    """
    from datetime import date as _date

    import yaml

    from alphalens.backtest.engine import BacktestEngine
    from alphalens.backtest.factors import load_carhart_daily
    from alphalens.backtest.history_store import HistoryStore
    from alphalens.backtest.walk_forward import compile_report, run_walk_forward
    from alphalens.screeners.lean.config import DATA_DIR
    from alphalens.screeners.lean.lean_csv_loader import load_lean_histories
    from alphalens.screeners.themed.backtest_adapter import momentum_scorer_adapter
    from alphalens.screeners.themed.config import THEMED_DEFAULTS, UNIVERSE_PATH
    from alphalens.screeners.themed.universe import flatten_universe

    start_date = _date.fromisoformat(start)
    end_date = _date.fromisoformat(end)
    typer.echo(
        f"Walk-forward OOS — {start_date} → {end_date}, "
        f"window={window_days}d, step={step_days}d"
    )

    # Load universe + histories
    universe_yaml = yaml.safe_load(UNIVERSE_PATH.read_text())
    screener_tickers = sorted(flatten_universe(universe_yaml).keys())
    typer.echo(f"Loading OHLCV for {len(screener_tickers)} themed tickers + {benchmark}…")
    histories = load_lean_histories(DATA_DIR, screener_tickers + [benchmark])
    store = HistoryStore(histories)
    typer.echo(f"  loaded {len(store.tickers())} tickers")

    scorer_config = dict(THEMED_DEFAULTS, benchmark=benchmark)

    # Carhart factors
    carhart = None
    if not no_attrib:
        try:
            carhart = load_carhart_daily(start=start_date, end=end_date)
        except (FileNotFoundError, ValueError) as exc:
            typer.echo(f"  Carhart factors unavailable: {exc} (gate C2 will be N/A)")
            carhart = None

    # Single baseline backtest
    typer.echo("\n[baseline] running full-span backtest…")
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

    benchmark_close = store.full(benchmark)["close"]

    typer.echo("\n[walk-forward] slicing + computing per-window metrics…")
    wf_report = run_walk_forward(
        baseline,
        benchmark_close=benchmark_close,
        carhart=carhart,
        window_days=window_days,
        step_days=step_days,
    )
    typer.echo(f"  {len(wf_report.window_results)} windows")
    typer.echo(
        f"  baseline Sharpe: {wf_report.baseline_sharpe:+.3f}"
        + (
            f"  baseline Carhart α_t: {wf_report.baseline_alpha_tstat:+.2f}"
            if wf_report.baseline_alpha_tstat is not None
            else ""
        )
    )
    typer.echo(f"  verdict: {wf_report.verdict.overall}")
    for key in ("c1", "c2", "c3", "c4", "c5"):
        reason = wf_report.verdict.reasons.get(key, "")
        flag = getattr(wf_report.verdict, f"{key}_pass")
        mark = "PASS" if flag else ("N/A" if flag is None else "FAIL")
        typer.echo(f"    {key.upper()} {mark} — {reason}")

    report_path = Path(report)
    if not report_path.is_absolute():
        report_path = Path.cwd() / report_path
    compile_report(
        report_path,
        wf_report,
        benchmark=benchmark,
        top_n=top_n,
        holding=holding,
    )
    typer.echo(f"\nReport written to {report_path}")

    if csv:
        import csv as _csv

        csv_path = Path(csv)
        if not csv_path.is_absolute():
            csv_path = Path.cwd() / csv_path
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with csv_path.open("w", newline="") as fh:
            writer = _csv.writer(fh)
            writer.writerow([
                "test_start", "test_end", "n_days", "regime", "regime_reversed_within",
                "sharpe_gross", "sharpe_moderate",
                "carhart_alpha_daily", "carhart_alpha_tstat",
                "ic_mean", "ic_tstat", "max_drawdown", "turnover", "cumulative_return",
            ])
            for r in wf_report.window_results:
                writer.writerow([
                    r.test_start, r.test_end, r.n_days, r.regime, r.regime_reversed_within,
                    f"{r.sharpe_gross:.4f}", f"{r.sharpe_moderate:.4f}",
                    f"{r.carhart_alpha_daily:.6f}" if r.carhart_alpha_daily is not None else "",
                    f"{r.carhart_alpha_tstat:.4f}" if r.carhart_alpha_tstat is not None else "",
                    f"{r.ic_mean:.6f}", f"{r.ic_tstat:.4f}",
                    f"{r.max_drawdown:.4f}", f"{r.turnover:.4f}", f"{r.cumulative_return:.4f}",
                ])
        typer.echo(f"Per-window CSV: {csv_path}")


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

    # Scorer declares its own warmup requirement — engine reads on __init__.
    lean_rank.MIN_BARS_REQUIRED = 252
    engine = BacktestEngine(
        store, scorer=lean_rank, scorer_config=LEAN_DEFAULTS,
        holding_period=5, top_n=top_n, benchmark="SPY",
        screener_tickers=screener_tickers, weighting="linear",
    )

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


_ACCEPT_RATINGS = {"BUY", "OVERWEIGHT"}
_REJECT_RATINGS = {"HOLD", "UNDERWEIGHT", "SELL"}
_PIT_SAFE_ANALYSTS = ["market", "news", "fundamentals"]  # social drops look-ahead risk


@research_app.command(name="historical-acceptance")
def historical_acceptance(
    scorer: str = typer.Option(
        ...,
        help="Which scorer's picks to replay: 'momentum' or 'early-stage'",
    ),
    samples_per_regime: int = typer.Option(
        10, help="How many (date, ticker) samples per regime bucket to run through Layer 3"
    ),
    seed: int = typer.Option(42, help="Random seed for reproducibility"),
    picks_csv: str = typer.Option(
        "",
        help="Path to picks CSV (default: docs/backtest/compare_{scorer}_2026-04-21.csv)",
    ),
    benchmark: str = typer.Option("SPY", help="Benchmark ticker for regime classification"),
    include_social: bool = typer.Option(
        False,
        "--include-social/--exclude-social",
        help="Include social analyst. Default: exclude (social has look-ahead risk on "
             "historical replay). Off for clean PIT rigor.",
    ),
    report: str = typer.Option("", help="Markdown summary report output path"),
    results_csv: str = typer.Option("", help="Per-sample results CSV"),
    reports_dir: str = typer.Option(
        "",
        help="Directory for per-sample full reports (analyst + research + trader + "
             "risk markdowns + final_state.json). Default: "
             "docs/research/acceptance_{scorer}_reports/",
    ),
    dry_run: bool = typer.Option(
        False, help="Print sampling plan + cost estimate, skip Layer 3 invocation"
    ),
) -> None:
    """Stratified random sample of historical top-5 picks → Layer 3 PIT replay.

    Sampling design (per user feedback 2026-04-21):
      - Population: every (date, ticker) in the scorer's 5y top-5 timeline.
      - Stratified by regime (bull/bear/flat) to reduce variance + isolate
        regime-specific acceptance behavior.
      - No decision-change filter — Layer 3 verdict is (date, ticker, state)-
        dependent, not purely scorer-composition-dependent. Same ticker on
        different days → genuinely independent decisions.

    PIT hygiene:
      - curr_date passed through to TradingAgents graph.propagate.
      - Social analyst excluded by default (see CLAUDE.md — its toolset doesn't
        filter by historical date). News + fundamentals + market are PIT-safe.

    Acceptance metric: fraction of samples where Layer 3 returns BUY or OVERWEIGHT.
    """
    import json
    import random
    from collections import defaultdict
    from datetime import date as _date

    import pandas as pd

    from alphalens.backtest.history_store import HistoryStore
    from alphalens.backtest.regime import classify_regime
    from alphalens.candidates import Candidate
    from alphalens.runner import TradingAgentsRunner
    from alphalens.screeners.lean.config import DATA_DIR
    from alphalens.screeners.lean.lean_csv_loader import load_lean_histories

    try:
        from cli.main import save_report_to_disk as _save_ta_report
    except Exception:  # noqa: BLE001
        _save_ta_report = None

    if scorer not in {"momentum", "early-stage"}:
        raise typer.BadParameter(f"Unknown scorer: {scorer!r} (expected: momentum | early-stage)")

    picks_path = Path(picks_csv) if picks_csv else Path(
        f"docs/backtest/compare_{scorer.replace('-', '_')}_2026-04-21.csv"
    )
    if not picks_path.exists():
        raise typer.BadParameter(f"Picks CSV not found: {picks_path}")

    typer.echo(f"Loading picks from {picks_path}")
    picks_raw = pd.read_csv(picks_path)
    # Explode top_n_tickers (comma-separated) into (date, ticker) rows.
    rows = []
    for _, r in picks_raw.iterrows():
        d = pd.to_datetime(r["date"]).date()
        for rank, t in enumerate(str(r["top_n_tickers"]).split(","), 1):
            rows.append({"date": d, "ticker": t.strip().upper(), "rank": rank})
    picks = pd.DataFrame(rows)
    typer.echo(f"  {len(picks)} (date, ticker) pairs over {picks['date'].nunique()} days")

    # Regime classification — load benchmark close.
    typer.echo(f"Loading {benchmark} OHLCV for regime classification…")
    histories = load_lean_histories(DATA_DIR, [benchmark])
    store = HistoryStore(histories)
    bench_close = store.full(benchmark)["close"]
    regime_labels = classify_regime(bench_close)
    regime_labels.index = regime_labels.index.date
    picks["regime"] = picks["date"].map(regime_labels.to_dict())
    picks = picks.dropna(subset=["regime"])

    regime_counts = picks["regime"].value_counts().to_dict()
    typer.echo(f"  population by regime: {regime_counts}")

    # Stratified sample.
    rng = random.Random(seed)
    sampled = []
    for regime in ("bull", "bear", "flat"):
        pool = picks[picks["regime"] == regime]
        if pool.empty:
            typer.echo(f"  [{regime}] empty pool, skipping")
            continue
        k = min(samples_per_regime, len(pool))
        idx = rng.sample(range(len(pool)), k)
        sampled.extend(pool.iloc[idx].to_dict(orient="records"))

    typer.echo(f"Sampled {len(sampled)} (date, ticker) pairs across regimes")
    if dry_run:
        typer.echo("--- sampling plan ---")
        for s in sampled[:10]:
            typer.echo(f"  {s['date']}  {s['ticker']:<6}  rank={s['rank']}  regime={s['regime']}")
        if len(sampled) > 10:
            typer.echo(f"  … ({len(sampled) - 10} more)")
        typer.echo(
            f"\nEstimated cost: ~{len(sampled)} Layer 3 runs × ~15 min each "
            f"= ~{len(sampled) * 15 / 60:.1f}h sequential"
        )
        typer.echo("Dry-run complete — no Layer 3 calls made.")
        raise typer.Exit(0)

    # Real runs.
    analysts = None if include_social else list(_PIT_SAFE_ANALYSTS)
    typer.echo(
        f"Running Layer 3 with selected_analysts="
        f"{'default (all 4)' if analysts is None else analysts}"
    )
    runner = TradingAgentsRunner()

    reports_root = Path(reports_dir) if reports_dir else Path(
        f"docs/research/acceptance_{scorer.replace('-', '_')}_reports"
    )
    if not reports_root.is_absolute():
        reports_root = Path.cwd() / reports_root
    reports_root.mkdir(parents=True, exist_ok=True)
    typer.echo(f"Per-sample reports → {reports_root}")
    results: list[dict] = []
    accept_by_regime: dict[str, list[int]] = defaultdict(list)

    for i, s in enumerate(sampled, 1):
        typer.echo(f"[{i}/{len(sampled)}] {s['date']} {s['ticker']} (regime={s['regime']})")
        candidate = Candidate.from_screener(
            ticker=s["ticker"],
            source=scorer,
            priority=10,
            payload={"rank": s["rank"], "replay_date": s["date"].isoformat()},
            discriminator=f"replay-{s['date']}",
        )
        try:
            result = runner.run(
                candidate,
                candidate_id=i,
                curr_date=s["date"],
                selected_analysts=analysts,
            )
            rating = (result.rating or "").upper()
            accepted = 1 if rating in _ACCEPT_RATINGS else 0
            accept_by_regime[s["regime"]].append(accepted)

            # Persist the full state + upstream's markdown render for post-hoc
            # analysis of why Layer 3 accepted/rejected this pick.
            sample_dir = reports_root / f"{s['date'].isoformat()}_{s['ticker']}"
            sample_dir.mkdir(parents=True, exist_ok=True)
            try:
                (sample_dir / "final_state.json").write_text(
                    json.dumps(result.final_state, default=str, indent=2)
                )
            except Exception as exc:  # noqa: BLE001
                typer.echo(f"    warn: final_state.json failed: {exc}", err=True)
            if _save_ta_report is not None:
                try:
                    _save_ta_report(result.final_state, s["ticker"], sample_dir)
                except Exception as exc:  # noqa: BLE001
                    typer.echo(f"    warn: save_report_to_disk failed: {exc}", err=True)

            results.append({
                "date": s["date"].isoformat(),
                "ticker": s["ticker"],
                "rank": s["rank"],
                "regime": s["regime"],
                "rating": rating,
                "accepted": accepted,
                "duration_sec": round(result.duration_sec, 1),
                "model": result.model_used,
                "report_dir": str(sample_dir.relative_to(Path.cwd())) if sample_dir.is_relative_to(Path.cwd()) else str(sample_dir),
                "error": "",
            })
            typer.echo(f"    → {rating}  ({result.duration_sec:.0f}s)  → {sample_dir.name}/")
        except Exception as exc:  # noqa: BLE001
            results.append({
                "date": s["date"].isoformat(),
                "ticker": s["ticker"],
                "rank": s["rank"],
                "regime": s["regime"],
                "rating": "",
                "accepted": 0,
                "duration_sec": 0,
                "model": "",
                "report_dir": "",
                "error": str(exc)[:200],
            })
            typer.echo(f"    → ERROR: {exc}")

    # Aggregate.
    def _accept_rate(lst):
        return sum(lst) / len(lst) if lst else float("nan")

    total_accept = [r["accepted"] for r in results if not r["error"]]
    total_rate = _accept_rate(total_accept)

    typer.echo("")
    typer.echo(f"=== Acceptance rate — scorer={scorer} ===")
    typer.echo(f"  Overall: {total_rate * 100:.1f}% ({sum(total_accept)}/{len(total_accept)})")
    for regime in ("bull", "bear", "flat"):
        lst = accept_by_regime.get(regime, [])
        if lst:
            typer.echo(f"  {regime}:   {_accept_rate(lst) * 100:.1f}% ({sum(lst)}/{len(lst)})")

    # Reports.
    report_path = Path(report) if report else Path(
        f"docs/research/historical_acceptance_{scorer.replace('-', '_')}.md"
    )
    if not report_path.is_absolute():
        report_path = Path.cwd() / report_path
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Historical Layer 3 Acceptance — {scorer}",
        "",
        f"- Picks source: `{picks_path}`",
        f"- Samples per regime: {samples_per_regime}",
        f"- Seed: {seed}",
        f"- Analysts: {'all 4' if analysts is None else ', '.join(analysts)}"
        + ("" if analysts is None else " (social excluded for PIT rigor)"),
        f"- Total samples: {len(sampled)}  (attempted), {len(total_accept)} (completed)",
        "",
        "## Acceptance rate",
        "",
        "| Regime | n | Accepted | Rate |",
        "| --- | ---: | ---: | ---: |",
    ]
    for regime in ("bull", "bear", "flat"):
        lst = accept_by_regime.get(regime, [])
        if lst:
            lines.append(f"| {regime} | {len(lst)} | {sum(lst)} | {_accept_rate(lst) * 100:.1f}% |")
    lines += [
        f"| **overall** | **{len(total_accept)}** | **{sum(total_accept)}** | **{total_rate * 100:.1f}%** |",
        "",
        "## Rating distribution",
        "",
    ]
    rating_counts: dict[str, int] = defaultdict(int)
    for r in results:
        if r["rating"]:
            rating_counts[r["rating"]] += 1
    for rating in ("BUY", "OVERWEIGHT", "HOLD", "UNDERWEIGHT", "SELL"):
        lines.append(f"- {rating}: {rating_counts.get(rating, 0)}")
    report_path.write_text("\n".join(lines))
    typer.echo(f"Report: {report_path}")

    if results_csv:
        csv_path = Path(results_csv)
        if not csv_path.is_absolute():
            csv_path = Path.cwd() / csv_path
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(results).to_csv(csv_path, index=False)
        typer.echo(f"Per-sample CSV: {csv_path}")
