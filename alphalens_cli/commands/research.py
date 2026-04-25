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

_HELP_START = "Backtest start (YYYY-MM-DD). Polygon Basic entitlement floor is 2021-06-01."
_HELP_END = "Backtest end (YYYY-MM-DD)"
_HELP_TOP_N = "Top-N picks per day"
_HELP_HOLDING = "Holding period in trading days"
_HELP_WEIGHTING = "Position weighting: linear | equal | conviction"
_HELP_REPORT = "Markdown report output path (relative to repo root)"


@research_app.callback()
def _research_callback() -> None:
    """Force multi-command behaviour even when only one command is registered."""


@research_app.command(name="cost-validation")
def cost_validation(
    start: str = typer.Option(
        "2021-06-01",
        help=_HELP_START,
    ),
    end: str = typer.Option("2026-04-17", help=_HELP_END),
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
    top_n: int = typer.Option(5, help=_HELP_TOP_N),
    holding: int = typer.Option(5, help=_HELP_HOLDING),
    weighting: str = typer.Option("linear", help=_HELP_WEIGHTING),
    benchmark: str = typer.Option("SPY", help="Benchmark for calendar"),
    report: str = typer.Option(
        "docs/backtest/cost_validation.md",
        help=_HELP_REPORT,
    ),
    csv: str = typer.Option("", help="Optional per-pick-day participation CSV output path"),
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
        CostValidationReport,
        build_per_date_tiers,
        compare_cost_scenarios,
        evaluate_cost_gate,
        rolling_dollar_adv,
        run_scale_path,
    )
    from alphalens.backtest.cost_validation import (
        compile_report as cv_compile_report,
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
    histories = load_lean_histories(DATA_DIR, [*screener_tickers, benchmark])
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
        baseline,
        rolling_adv,
        per_date_tiers,
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
    tiered = compare_cost_scenarios(baseline, per_date_tiers, bps_per_tier, weighting=weighting)
    typer.echo(
        f"  gross={tiered.sharpe_gross:+.3f}  "
        f"flat 100bps={tiered.sharpe_flat_100bps:+.3f}  "
        f"tiered={tiered.sharpe_tiered:+.3f}  "
        f"(tiered drag ≈ {tiered.annual_drag_tiered_bps:.0f} bps/yr)"
    )

    # Gate
    verdict = evaluate_cost_gate(scale_path)
    typer.echo(f"\nverdict: {verdict.overall}")
    typer.echo(
        f"  fraction: {'PASS' if verdict.fraction_pass else 'FAIL'} — {verdict.reasons['fraction']}"
    )
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
        report_path,
        cv_report,
        start=start_date,
        end=end_date,
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
                [
                    "date",
                    "ticker",
                    "rank",
                    "tier",
                    "participation",
                    "dollar_position",
                    "dollar_adv",
                ]
            )
            for p in scale_path.worst_offenders:
                writer.writerow(
                    [
                        p.date,
                        p.ticker,
                        p.rank,
                        p.tier,
                        f"{p.participation:.6f}",
                        f"{p.dollar_position:.2f}",
                        f"{p.dollar_adv:.2f}",
                    ]
                )
        typer.echo(f"Worst-offenders CSV: {csv_path}")


def _print_walk_forward_verdict(wf_report) -> None:
    """Echo PASS/FAIL/N/A for c1..c5 — used by `research walk-forward`."""
    _MARK = {None: "N/A", True: "PASS", False: "FAIL"}
    typer.echo(f"  verdict: {wf_report.verdict.overall}")
    for key in ("c1", "c2", "c3", "c4", "c5"):
        flag = getattr(wf_report.verdict, f"{key}_pass")
        reason = wf_report.verdict.reasons.get(key, "")
        typer.echo(f"    {key.upper()} {_MARK[flag]} — {reason}")


def _write_walk_forward_csv(csv_path: Path, wf_report) -> None:
    import csv as _csv

    if not csv_path.is_absolute():
        csv_path = Path.cwd() / csv_path
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="") as fh:
        writer = _csv.writer(fh)
        writer.writerow(
            [
                "test_start",
                "test_end",
                "n_days",
                "regime",
                "regime_reversed_within",
                "sharpe_gross",
                "sharpe_moderate",
                "carhart_alpha_daily",
                "carhart_alpha_tstat",
                "ic_mean",
                "ic_tstat",
                "max_drawdown",
                "turnover",
                "cumulative_return",
            ]
        )
        for r in wf_report.window_results:
            writer.writerow(
                [
                    r.test_start,
                    r.test_end,
                    r.n_days,
                    r.regime,
                    r.regime_reversed_within,
                    f"{r.sharpe_gross:.4f}",
                    f"{r.sharpe_moderate:.4f}",
                    f"{r.carhart_alpha_daily:.6f}" if r.carhart_alpha_daily is not None else "",
                    f"{r.carhart_alpha_tstat:.4f}" if r.carhart_alpha_tstat is not None else "",
                    f"{r.ic_mean:.6f}",
                    f"{r.ic_tstat:.4f}",
                    f"{r.max_drawdown:.4f}",
                    f"{r.turnover:.4f}",
                    f"{r.cumulative_return:.4f}",
                ]
            )
    typer.echo(f"Per-window CSV: {csv_path}")


@research_app.command(name="walk-forward")
def walk_forward(
    start: str = typer.Option(
        "2021-06-01",
        help=_HELP_START,
    ),
    end: str = typer.Option("2026-04-17", help=_HELP_END),
    window_days: int = typer.Option(252, help="Test window size in trading days (~1 year)"),
    step_days: int = typer.Option(21, help="Stride between windows in trading days (~1 month)"),
    top_n: int = typer.Option(5, help=_HELP_TOP_N),
    holding: int = typer.Option(5, help=_HELP_HOLDING),
    weighting: str = typer.Option("linear", help=_HELP_WEIGHTING),
    benchmark: str = typer.Option("SPY", help="Benchmark for calendar + regime classification"),
    no_attrib: bool = typer.Option(
        False,
        "--no-attrib",
        help="Skip Carhart per-window attribution (useful when FF factor files are unavailable).",
    ),
    report: str = typer.Option(
        "docs/backtest/walk_forward.md",
        help=_HELP_REPORT,
    ),
    csv: str = typer.Option("", help="Optional per-window CSV output path (empty = skip)"),
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
        f"Walk-forward OOS — {start_date} → {end_date}, window={window_days}d, step={step_days}d"
    )

    # Load universe + histories
    universe_yaml = yaml.safe_load(UNIVERSE_PATH.read_text())
    screener_tickers = sorted(flatten_universe(universe_yaml).keys())
    typer.echo(f"Loading OHLCV for {len(screener_tickers)} themed tickers + {benchmark}…")
    histories = load_lean_histories(DATA_DIR, [*screener_tickers, benchmark])
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
    _print_walk_forward_verdict(wf_report)

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
        _write_walk_forward_csv(Path(csv), wf_report)


@research_app.command(name="survivorship-pit")
def survivorship_pit(
    start: str = typer.Option(
        "2021-06-01",
        help=_HELP_START,
    ),
    end: str = typer.Option("2026-04-17", help=_HELP_END),
    top_n: int = typer.Option(5, help=_HELP_TOP_N),
    holding: int = typer.Option(5, help=_HELP_HOLDING),
    weighting: str = typer.Option("linear", help=_HELP_WEIGHTING),
    benchmark: str = typer.Option("SPY", help="Benchmark for calendar"),
    tests: str = typer.Option(
        "c1,c2,c3",
        help="Comma-separated subset of tests to run (c1, c2, c3). Default: all three.",
    ),
    report: str = typer.Option(
        "docs/backtest/survivorship_pit_a.md",
        help=_HELP_REPORT,
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
    histories = load_lean_histories(DATA_DIR, [*screener_tickers, benchmark])
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
            store,
            pre,
            post,
            scorer=momentum_scorer_adapter,
            scorer_config=scorer_config,
            start=start_date,
            end=end_date,
            benchmark=benchmark,
            top_n=top_n,
            holding_period=holding,
            weighting=weighting,
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
                baseline,
                events,
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
    from alphalens.backtest.historical_validation import (
        evaluate_historical_picks,
        format_decision_matrix,
        picks_from_backtest_report,
        rule_based_tractability_scorer,
    )
    from alphalens.backtest.history_store import HistoryStore
    from alphalens.screeners.lean.config import BENCHMARKS, DATA_DIR, LEAN_DEFAULTS
    from alphalens.screeners.lean.lean_csv_loader import load_lean_histories
    from alphalens.screeners.lean.lean_project.scorer import rank_universe as lean_rank
    from alphalens.screeners.themed.universe import (
        flatten_universe,
    )
    from alphalens.screeners.themed.universe import (
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
        store,
        scorer=lean_rank,
        scorer_config=LEAN_DEFAULTS,
        holding_period=5,
        top_n=top_n,
        benchmark="SPY",
        screener_tickers=screener_tickers,
        weighting="linear",
    )

    typer.echo("Generuję historical picks via BacktestEngine...")
    report_obj = engine.run(start=start_date, end=end_date)
    all_picks = picks_from_backtest_report(report_obj)
    picks_with_themes = []
    for p in all_picks:
        from alphalens.backtest.historical_validation import PickRecord

        picks_with_themes.append(
            PickRecord(
                asof_date=p.asof_date,
                ticker=p.ticker,
                rank=p.rank,
                momentum_score=p.momentum_score,
                themes=themes_map.get(p.ticker, []),
                forward_return=p.forward_return,
            )
        )
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
_PIT_SAFE_ANALYSTS = ["market", "news", "fundamentals"]  # social drops look-ahead risk
_FWD_HORIZONS = (5, 20, 60, 120)


def _classify_rating(raw: str | None) -> str:
    """Normalize an upstream decision string (strip whitespace, uppercase)."""
    if not raw:
        return ""
    return str(raw).strip().upper()


def _mean_alpha(rows, horizon: int):
    """NaN-aware mean + accurate sample count for the forward-return table.

    For 120d horizon, recent picks typically have alpha_120d = NaN (the 120-day
    forward window hasn't occurred yet). Returning len(rows) as n would overstate
    the mean's support.
    """
    import pandas as pd

    vals = [r.get(f"alpha_{horizon}d") for r in rows]
    vals = [v for v in vals if pd.notna(v)]
    if not vals:
        return None, 0
    return sum(vals) / len(vals), len(vals)


def _at_pick_trailing_return(store, ticker: str, pick_date, lookback: int) -> float | None:
    """Trailing `lookback`-day return for `ticker` ending at `pick_date` (inclusive)."""
    try:
        df = store.full(ticker)
    except Exception:
        return None
    import pandas as pd

    ts = pd.Timestamp(pick_date)
    try:
        closes = df["close"].loc[:ts]
        if len(closes) <= lookback:
            return None
        now = float(closes.iloc[-1])
        then = float(closes.iloc[-lookback - 1])
        if then <= 0:
            return None
        return (now - then) / then
    except Exception:
        return None


def _compute_forward_features(store, ticker: str, benchmark: str, pick_date) -> dict:
    """Return fwd/bench/alpha + max drawdown + realized vol for horizons {5,20,60,120}."""
    import numpy as np
    import pandas as pd

    ts = pd.Timestamp(pick_date)
    out: dict = {}

    # PIT-safe entry: signal is generated at EOD of pick_date; earliest realistic
    # fill is the NEXT trading day's close. Using pick_date's close would be
    # look-ahead (we can't execute at a price we learned after the bar closed).
    # Matches HistoryStore.forward_return convention used by the backtest engine.
    try:
        df_t = store.full(ticker)
        tkr_close = df_t["close"].loc[df_t.index > ts]
    except Exception:
        tkr_close = None
    try:
        df_b = store.full(benchmark)
        bench_close = df_b["close"].loc[df_b.index > ts]
    except Exception:
        bench_close = None

    def _window_metrics(closes, horizon):
        # Need horizon+1 bars post-entry: entry@iloc[0], exit@iloc[horizon].
        if closes is None or len(closes) < horizon + 1:
            return None, None, None
        window = closes.iloc[: horizon + 1]
        entry = float(window.iloc[0])
        exit_ = float(window.iloc[horizon])
        if entry <= 0:
            return None, None, None
        ret = (exit_ - entry) / entry
        # Intra-period max drawdown on cumulative return path vs entry.
        path = window / entry
        max_dd = float((path / path.cummax() - 1.0).min())
        # Realized daily vol, annualized.
        daily_rets = window.pct_change().dropna()
        vol_ann = float(daily_rets.std() * np.sqrt(252)) if len(daily_rets) > 1 else None
        return ret, max_dd, vol_ann

    for horizon in _FWD_HORIZONS:
        t_ret, t_dd, t_vol = _window_metrics(tkr_close, horizon)
        b_ret, b_dd, b_vol = _window_metrics(bench_close, horizon)
        alpha = (t_ret - b_ret) if (t_ret is not None and b_ret is not None) else None
        out[f"fwd_{horizon}d"] = t_ret
        out[f"bench_{horizon}d"] = b_ret
        out[f"alpha_{horizon}d"] = alpha
        out[f"fwd_max_dd_{horizon}d"] = t_dd
        out[f"bench_max_dd_{horizon}d"] = b_dd
        out[f"fwd_vol_{horizon}d"] = t_vol
        out[f"bench_vol_{horizon}d"] = b_vol
    return out


def _load_acceptance_picks(picks_path: Path) -> pd.DataFrame:  # noqa: F821
    """Load picks CSV and explode top_n_tickers/scores into per-(date, ticker) rows."""
    import pandas as pd

    picks_raw = pd.read_csv(picks_path)
    rows = []
    for _, r in picks_raw.iterrows():
        d = pd.to_datetime(r["date"]).date()
        tickers_list = [t.strip().upper() for t in str(r["top_n_tickers"]).split(",")]
        scores_list = [float(s.strip()) for s in str(r["top_n_scores"]).split(",")]
        for rank, (t, s) in enumerate(zip(tickers_list, scores_list, strict=False), 1):
            rows.append(
                {
                    "date": d,
                    "ticker": t,
                    "rank": rank,
                    "scorer_score": s,
                    "daily_ic": float(r.get("ic", float("nan"))) if "ic" in r else float("nan"),
                    "scored_count": int(r.get("scored_count", 0)) if "scored_count" in r else 0,
                }
            )
    return pd.DataFrame(rows)


def _classify_picks_by_regime(
    picks: pd.DataFrame,  # noqa: F821
    store,
    benchmark: str,
) -> pd.DataFrame:  # noqa: F821
    """Attach 'regime' column (bull/bear/flat) by mapping pick_date → benchmark regime."""
    from alphalens.backtest.regime import classify_regime

    bench_close = store.full(benchmark)["close"]
    regime_labels = classify_regime(bench_close)
    regime_labels.index = regime_labels.index.date
    picks = picks.copy()
    picks["regime"] = picks["date"].map(regime_labels.to_dict())
    return picks.dropna(subset=["regime"])


def _stratified_sample_picks(
    picks: pd.DataFrame,  # noqa: F821
    samples_per_regime: int,
    seed: int,
) -> list[dict]:
    import random

    rng = random.Random(seed)
    sampled: list[dict] = []
    for regime in ("bull", "bear", "flat"):
        pool = picks[picks["regime"] == regime]
        if pool.empty:
            typer.echo(f"  [{regime}] empty pool, skipping")
            continue
        k = min(samples_per_regime, len(pool))
        idx = rng.sample(range(len(pool)), k)
        sampled.extend(pool.iloc[idx].to_dict(orient="records"))
    return sampled


def _print_dry_run_plan(sampled: list[dict]) -> None:
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


def _build_acceptance_row(
    sample: dict,
    *,
    rating: str,
    accepted: int,
    duration_sec: float,
    model: str,
    sample_dir: Path,
    fwd: dict,
) -> dict:
    return {
        "date": sample["date"].isoformat(),
        "ticker": sample["ticker"],
        "rank": sample["rank"],
        "scorer_score": sample.get("scorer_score"),
        "daily_ic": sample.get("daily_ic"),
        "scored_count": sample.get("scored_count"),
        "regime": sample["regime"],
        "rating": rating,
        "accepted": accepted,
        "duration_sec": round(duration_sec, 1),
        "model": model,
        "report_dir": str(sample_dir.relative_to(Path.cwd()))
        if sample_dir.is_relative_to(Path.cwd())
        else str(sample_dir),
        "error": "",
        **fwd,
    }


def _build_acceptance_error_row(sample: dict, exc: Exception, store, benchmark: str) -> dict:
    err_row: dict = {
        "date": sample["date"].isoformat(),
        "ticker": sample["ticker"],
        "rank": sample["rank"],
        "scorer_score": sample.get("scorer_score"),
        "daily_ic": sample.get("daily_ic"),
        "scored_count": sample.get("scored_count"),
        "regime": sample["regime"],
        "rating": "",
        "accepted": 0,
        "duration_sec": 0,
        "model": "",
        "report_dir": "",
        "error": str(exc)[:200],
    }
    err_row.update(_compute_forward_features(store, sample["ticker"], benchmark, sample["date"]))
    err_row["spy_trailing_60d"] = _at_pick_trailing_return(store, benchmark, sample["date"], 60)
    return err_row


def _persist_layer3_artifacts(result, sample: dict, sample_dir: Path, save_ta_report) -> None:
    import json

    sample_dir.mkdir(parents=True, exist_ok=True)
    try:
        (sample_dir / "final_state.json").write_text(
            json.dumps(result.final_state, default=str, indent=2)
        )
    except Exception as exc:
        typer.echo(f"    warn: final_state.json failed: {exc}", err=True)
    if save_ta_report is not None:
        try:
            save_ta_report(result.final_state, sample["ticker"], sample_dir)
        except Exception as exc:
            typer.echo(f"    warn: save_report_to_disk failed: {exc}", err=True)


def _resolve_path(custom: str, default: Path) -> Path:
    """Use `custom` if provided, else `default`; ensure absolute (cwd-anchored)."""
    p = Path(custom) if custom else default
    return p if p.is_absolute() else Path.cwd() / p


def _run_layer3_samples(
    sampled: list[dict],
    *,
    scorer: str,
    runner,
    store,
    benchmark: str,
    analysts,
    reports_root: Path,
    csv_path: Path | None,
    save_ta_report,
) -> tuple[list[dict], dict[str, list[int]]]:
    """Drive Layer 3 over the sampled (date, ticker) pairs. CSV is flushed per sample."""
    from collections import defaultdict

    import pandas as pd

    from alphalens.candidates import Candidate

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
                candidate, candidate_id=i, curr_date=s["date"], selected_analysts=analysts
            )
        except Exception as exc:
            results.append(_build_acceptance_error_row(s, exc, store, benchmark))
            typer.echo(f"    → ERROR: {exc}")
            if csv_path is not None:
                pd.DataFrame(results).to_csv(csv_path, index=False)
            continue

        rating = _classify_rating(result.rating)
        accepted = 1 if rating in _ACCEPT_RATINGS else 0
        accept_by_regime[s["regime"]].append(accepted)

        fwd = _compute_forward_features(store, s["ticker"], benchmark, s["date"])
        fwd["spy_trailing_60d"] = _at_pick_trailing_return(store, benchmark, s["date"], 60)

        sample_dir = reports_root / f"{s['date'].isoformat()}_{s['ticker']}"
        _persist_layer3_artifacts(result, s, sample_dir, save_ta_report)
        results.append(
            _build_acceptance_row(
                s,
                rating=rating,
                accepted=accepted,
                duration_sec=result.duration_sec,
                model=result.model_used,
                sample_dir=sample_dir,
                fwd=fwd,
            )
        )

        fwd_20 = fwd.get("fwd_20d")
        alpha_20 = fwd.get("alpha_20d")
        fwd_str = (
            f"  fwd20d={fwd_20 * 100:+.1f}% α={alpha_20 * 100:+.1f}%"
            if (fwd_20 is not None and alpha_20 is not None)
            else "  fwd20d=n/a"
        )
        typer.echo(f"    → {rating}  ({result.duration_sec:.0f}s){fwd_str}  → {sample_dir.name}/")

        if csv_path is not None:
            pd.DataFrame(results).to_csv(csv_path, index=False)

    return results, accept_by_regime


def _accept_rate(lst: list[int]) -> float:
    return sum(lst) / len(lst) if lst else float("nan")


def _print_acceptance_summary(
    scorer: str,
    results: list[dict],
    accept_by_regime: dict[str, list[int]],
) -> tuple[list[int], float]:
    total_accept = [r["accepted"] for r in results if not r["error"]]
    total_rate = _accept_rate(total_accept)
    typer.echo("")
    typer.echo(f"=== Acceptance rate — scorer={scorer} ===")
    typer.echo(f"  Overall: {total_rate * 100:.1f}% ({sum(total_accept)}/{len(total_accept)})")
    for regime in ("bull", "bear", "flat"):
        lst = accept_by_regime.get(regime, [])
        if lst:
            typer.echo(f"  {regime}:   {_accept_rate(lst) * 100:.1f}% ({sum(lst)}/{len(lst)})")
    return total_accept, total_rate


def _render_acceptance_report(
    *,
    scorer: str,
    picks_path: Path,
    samples_per_regime: int,
    seed: int,
    analysts,
    sampled: list[dict],
    results: list[dict],
    accept_by_regime: dict[str, list[int]],
    total_accept: list[int],
    total_rate: float,
) -> list[str]:
    from collections import defaultdict

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

    accepted_rows = [r for r in results if r["accepted"] == 1]
    rejected_rows = [r for r in results if r["accepted"] == 0 and r["rating"]]
    lines += [
        "",
        "## Forward returns by Layer 3 decision (vs SPY benchmark)",
        "",
        "Higher α on accepted rows = Layer 3 correctly picked winners. "
        "If rejected rows have similar/higher α, Layer 3 is destroying value. "
        "`n` reflects rows with a valid alpha at that horizon — recent picks "
        "may be missing at longer horizons.",
        "",
        "| Horizon | Accepted α (n) | Rejected α (n) | Δ (accepted − rejected) |",
        "| --- | ---: | ---: | ---: |",
    ]
    for horizon in _FWD_HORIZONS:
        a_alpha, a_n = _mean_alpha(accepted_rows, horizon)
        r_alpha, r_n = _mean_alpha(rejected_rows, horizon)
        delta = (a_alpha - r_alpha) if (a_alpha is not None and r_alpha is not None) else None
        a_str = f"{a_alpha * 100:+.2f}% ({a_n})" if a_alpha is not None else f"n/a ({a_n})"
        r_str = f"{r_alpha * 100:+.2f}% ({r_n})" if r_alpha is not None else f"n/a ({r_n})"
        d_str = f"{delta * 100:+.2f}%" if delta is not None else "n/a"
        lines.append(f"| {horizon}d | {a_str} | {r_str} | {d_str} |")
    return lines


@research_app.command(name="historical-acceptance")
def historical_acceptance(
    scorer: str = typer.Option(
        ...,
        help="Which scorer's picks to replay: 'momentum' or 'early-stage'",
    ),
    samples_per_regime: int = typer.Option(
        10,
        help="How many (date, ticker) samples per regime bucket to run through Layer 3",
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
    import pandas as pd

    from alphalens.backtest.history_store import HistoryStore
    from alphalens.runner import TradingAgentsRunner
    from alphalens.screeners.lean.config import DATA_DIR
    from alphalens.screeners.lean.lean_csv_loader import load_lean_histories

    try:
        from cli.main import save_report_to_disk as _save_ta_report
    except Exception:
        _save_ta_report = None

    if scorer not in {"momentum", "early-stage"}:
        raise typer.BadParameter(f"Unknown scorer: {scorer!r} (expected: momentum | early-stage)")

    picks_path = _resolve_path(
        picks_csv, Path(f"docs/backtest/compare_{scorer.replace('-', '_')}_2026-04-21.csv")
    )
    if not picks_path.exists():
        raise typer.BadParameter(f"Picks CSV not found: {picks_path}")

    typer.echo(f"Loading picks from {picks_path}")
    picks = _load_acceptance_picks(picks_path)
    typer.echo(f"  {len(picks)} (date, ticker) pairs over {picks['date'].nunique()} days")

    # Dry-run only needs the benchmark (for regime classification). A full run
    # additionally loads the themed universe (for forward-return computation
    # on sampled tickers). Skipping the 113-ticker load on --dry-run saves ~30s.
    import yaml

    from alphalens.screeners.themed.config import UNIVERSE_PATH
    from alphalens.screeners.themed.universe import flatten_universe

    universe_tickers = sorted(flatten_universe(yaml.safe_load(UNIVERSE_PATH.read_text())).keys())
    if dry_run:
        typer.echo(f"Loading OHLCV for {benchmark} only (dry-run — no forward returns)…")
        histories = load_lean_histories(DATA_DIR, [benchmark])
    else:
        typer.echo(
            f"Loading OHLCV for {len(universe_tickers)} universe tickers + {benchmark} "
            f"(regime + forward returns)…"
        )
        histories = load_lean_histories(DATA_DIR, [*universe_tickers, benchmark])
    store = HistoryStore(histories)

    picks = _classify_picks_by_regime(picks, store, benchmark)
    typer.echo(f"  population by regime: {picks['regime'].value_counts().to_dict()}")

    sampled = _stratified_sample_picks(picks, samples_per_regime, seed)
    typer.echo(f"Sampled {len(sampled)} (date, ticker) pairs across regimes")
    if dry_run:
        _print_dry_run_plan(sampled)
        raise typer.Exit(0)

    analysts = None if include_social else list(_PIT_SAFE_ANALYSTS)
    if analysts is not None and not analysts:
        raise typer.BadParameter(
            "selected_analysts resolved to empty list — upstream requires ≥1 analyst"
        )
    typer.echo(
        f"Running Layer 3 with selected_analysts="
        f"{'default (all 4)' if analysts is None else analysts}"
    )

    reports_root = _resolve_path(
        reports_dir, Path(f"docs/research/acceptance_{scorer.replace('-', '_')}_reports")
    )
    reports_root.mkdir(parents=True, exist_ok=True)
    typer.echo(f"Per-sample reports → {reports_root}")

    # Resolve CSV path once up front so we can flush after every sample. A 3h
    # run with no intermediate persistence would lose everything on OOM/crash
    # /Ctrl-C. Per-sample flush protects ~6M tokens of API spend.
    csv_path: Path | None = None
    if results_csv:
        csv_path = _resolve_path(results_csv, Path(results_csv))
        csv_path.parent.mkdir(parents=True, exist_ok=True)

    results, accept_by_regime = _run_layer3_samples(
        sampled,
        scorer=scorer,
        runner=TradingAgentsRunner(),
        store=store,
        benchmark=benchmark,
        analysts=analysts,
        reports_root=reports_root,
        csv_path=csv_path,
        save_ta_report=_save_ta_report,
    )

    total_accept, total_rate = _print_acceptance_summary(scorer, results, accept_by_regime)

    report_path = _resolve_path(
        report, Path(f"docs/research/historical_acceptance_{scorer.replace('-', '_')}.md")
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = _render_acceptance_report(
        scorer=scorer,
        picks_path=picks_path,
        samples_per_regime=samples_per_regime,
        seed=seed,
        analysts=analysts,
        sampled=sampled,
        results=results,
        accept_by_regime=accept_by_regime,
        total_accept=total_accept,
        total_rate=total_rate,
    )
    report_path.write_text("\n".join(lines))
    typer.echo(f"Report: {report_path}")

    if csv_path is not None:
        # One final flush to catch the case where the loop exited on a skipped
        # regime (no samples executed in it) — during-loop writes still covered
        # the common path.
        pd.DataFrame(results).to_csv(csv_path, index=False)
        typer.echo(f"Per-sample CSV: {csv_path}")
