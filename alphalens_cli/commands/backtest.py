"""Top-level `backtest` — screener-agnostic backtest harness."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer


def _resolve_scorer(
    scorer_name: str, benchmark: str
) -> tuple[Callable, dict[str, Any], list[str], list[str]]:
    """Return (scorer_fn, scorer_config, screener_tickers, extra_history_tickers).

    `extra_history_tickers` are the additional symbols beyond the screener
    universe + benchmark that need to be loaded into the HistoryStore (lean
    needs its full BENCHMARKS list for the calendar).
    """
    import yaml

    if scorer_name == "momentum":
        from alphalens.archive.screeners.themed.backtest_adapter import momentum_scorer_adapter
        from alphalens.archive.screeners.themed.config import THEMED_DEFAULTS, UNIVERSE_PATH
        from alphalens.archive.screeners.themed.universe import flatten_universe

        universe = yaml.safe_load(UNIVERSE_PATH.read_text())
        screener_tickers = sorted(flatten_universe(universe).keys())
        scorer_config = dict(THEMED_DEFAULTS, benchmark=benchmark)
        typer.echo(f"Scorer: Layer 2b momentum ({len(screener_tickers)} curated tickers)")
        return momentum_scorer_adapter, scorer_config, screener_tickers, [benchmark]

    if scorer_name == "early-stage":
        from alphalens.archive.screeners.themed.backtest_adapter import early_stage_scorer_adapter
        from alphalens.archive.screeners.themed.config import THEMED_DEFAULTS, UNIVERSE_PATH
        from alphalens.archive.screeners.themed.early_stage_scorer import EARLY_STAGE_DEFAULTS
        from alphalens.archive.screeners.themed.universe import flatten_universe

        universe = yaml.safe_load(UNIVERSE_PATH.read_text())
        screener_tickers = sorted(flatten_universe(universe).keys())
        scorer_config = {**THEMED_DEFAULTS, **EARLY_STAGE_DEFAULTS, "benchmark": benchmark}
        typer.echo(f"Scorer: Layer 2b early-stage ({len(screener_tickers)} curated tickers)")
        return early_stage_scorer_adapter, scorer_config, screener_tickers, [benchmark]

    if scorer_name == "lean":
        from alphalens.archive.screeners.lean.config import BENCHMARKS, LEAN_DEFAULTS
        from alphalens.archive.screeners.lean.lean_project.scorer import rank_universe as lean_rank
        from alphalens.archive.screeners.lean.universe import all_tickers

        screener_tickers = all_tickers()
        typer.echo(
            f"Scorer: Layer 2c Lean (archived, {len(screener_tickers)} tickers) — "
            "this strategy failed 5-year validation per CLAUDE.md"
        )
        return lean_rank, dict(LEAN_DEFAULTS), screener_tickers, list(BENCHMARKS)

    if scorer_name == "insider":
        return _resolve_insider_scorer(benchmark)

    raise typer.BadParameter(
        f"Unknown --scorer: {scorer_name!r} (expected: momentum | early-stage | lean | insider)"
    )


def _resolve_insider_scorer(
    benchmark: str,
) -> tuple[Callable, dict[str, Any], list[str], list[str]]:
    import os

    from alphalens.archive.screeners.insider.backtest_adapter import insider_scorer_adapter
    from alphalens.archive.screeners.insider.scorer import InsiderScorer
    from alphalens.data.alt_data.russell_universe import load_iwm_current
    from alphalens.data.alt_data.sec_edgar_client import SecEdgarClient
    from alphalens.data.alt_data.ticker_cik_map import TickerCikMap

    iwm_path = Path("alphalens/data/alt_data/data/iwm_current.yaml")
    cik_map_path = Path("alphalens/data/alt_data/data/ticker_cik_map.yaml")
    for p in (iwm_path, cik_map_path):
        if not p.exists():
            raise typer.BadParameter(
                f"Missing {p}. Seed via P3/P4 refreshers before running insider backtest."
            )
    ua = os.environ.get("SEC_EDGAR_USER_AGENT")
    if not ua:
        raise typer.BadParameter("SEC_EDGAR_USER_AGENT env var required")

    screener_tickers = sorted(load_iwm_current(iwm_path))
    cache_root = Path.home() / ".alphalens" / "insider_form4"
    cache_root.mkdir(parents=True, exist_ok=True)
    insider_store = InsiderScorer(
        edgar_client=SecEdgarClient(user_agent=ua),
        ticker_cik_map=TickerCikMap.load(cik_map_path),
        cache_dir=cache_root,
    )
    scorer_config = {"benchmark": benchmark, "_insider_store": insider_store}
    typer.echo(
        f"Scorer: Layer 2d insider cluster ({len(screener_tickers)} IWM tickers) — "
        "Phase 3a live universe; Phase 3b backtest uses PIT reconstruction"
    )
    return insider_scorer_adapter, scorer_config, screener_tickers, [benchmark]


def _apply_fundamental_gate(
    scorer_config: dict,
    screener_tickers: list[str],
    *,
    source: str,
    with_prices: bool,
) -> None:
    """Pre-load fundamentals and wire them into scorer_config in place."""
    if source == "edgar":
        from alphalens.data.store.edgar_fundamentals import EdgarFundamentalsStore

        typer.echo(
            f"Preloading EDGAR fundamentals (SEC XBRL companyfacts, with_prices={with_prices})…"
        )
        store = EdgarFundamentalsStore(with_prices=with_prices)
    elif source == "av":
        from alphalens.data.store.fundamentals_pit import HistoricalFundamentalsStore

        typer.echo(f"Preloading Alpha Vantage fundamentals for {len(screener_tickers)} tickers…")
        store = HistoricalFundamentalsStore()
    else:
        raise typer.BadParameter(
            f"Unknown --fundamentals-source: {source!r} (expected: edgar | av)"
        )

    store.preload(screener_tickers)
    scorer_config["fundamental_gate_enabled"] = True
    scorer_config["_fundamentals_store"] = store
    typer.echo(f"  fundamental gate: ON (source={source})")


def _compute_diagnostics(result, regime_labels) -> tuple[list, dict, float]:
    """IC by decile + vol decomposition, only meaningful when retain_scored_frames=True."""
    from alphalens.attribution.diagnostics import (
        format_vol_decomposition,
        ic_by_decile_from_scored_frames,
        tail_concentration_score,
        vol_decomposition_by_regime,
    )

    if not result.scored_frames:
        return [], {}, 0.0
    decile_ic = ic_by_decile_from_scored_frames(result.scored_frames)
    tail_score = tail_concentration_score(decile_ic)
    vol_decomp = vol_decomposition_by_regime(result, regime_labels)
    typer.echo(f"  diagnostics: tail concentration score = {tail_score:.2f}")
    typer.echo(format_vol_decomposition(vol_decomp))
    return decile_ic, vol_decomp, tail_score


def _compute_theme_stats(result):
    from alphalens.backtest.theme_analysis import snapshots_from_backtest, theme_series

    themes_map: dict[str, list[str]] = {}
    try:
        from alphalens.archive.screeners.themed.universe import flatten_universe as flatten_2b
        from alphalens.archive.screeners.themed.universe import load_universe as load_2b

        themes_map.update(flatten_2b(load_2b()))
    except Exception as exc:
        typer.echo(f"  theme mapping skipped (2b universe not available): {exc}")
        return None

    if not themes_map:
        return None

    snaps = snapshots_from_backtest(result.rebalance_results, themes_map)
    _, theme_stats = theme_series(snaps, concentration_threshold=0.70)
    if theme_stats.all_themes:
        typer.echo(
            f"  themes: mean HHI={theme_stats.mean_hhi:.3f}, "
            f"alert days={theme_stats.concentration_alert_days}"
        )
    return theme_stats


def _print_headline(summary, attribution) -> None:
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


# Typer CLI commands legitimately need many flags; collapsing them would lose CLI ergonomics.
def backtest(  # NOSONAR — Typer CLI legitimately needs many flags
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
    csv: str = typer.Option("", help="Optional daily results CSV output path (empty = skip)"),
    no_attrib: bool = typer.Option(
        False,
        "--no-attrib",
        help="Skip factor attribution (CAPM/FF3/Carhart-4F) regressions",
    ),
    diagnose: bool = typer.Option(
        False,
        "--diagnose",
        help="Retain per-day scored frames and append IC-by-decile + vol decomposition to report",
    ),
    fundamental_gate: bool = typer.Option(
        False,
        "--fundamental-gate/--no-fundamental-gate",
        help="Apply Layer 2b fundamental soft-guardrail (issue #14). Pre-loads "
        "fundamentals once for the universe and multiplies the technical composite by "
        "the gate score. Only meaningful for --scorer momentum | early-stage.",
    ),
    fundamentals_source: str = typer.Option(
        "edgar",
        "--fundamentals-source",
        help="Data source when --fundamental-gate is on: 'edgar' (SEC XBRL "
        "companyfacts via SecEdgarClient; recommended — see PR #159) or 'av' "
        "(Alpha Vantage, 25 req/day free tier so 113 tickers × 4 endpoints "
        "will throttle).",
    ),
    with_prices: bool = typer.Option(
        False,
        "--with-prices/--no-prices",
        help="SimFin-only: load daily share-prices CSV for PIT P/S gate. Requires "
        "~/.alphalens/simfin_cache/us-shareprices-daily.csv (~435MB). If "
        "missing, simfin will download it (download speed varies — can be "
        "slow on throttled broadband). When off, P/S penalty is skipped "
        "and gate uses only runway/OCF/net_income.",
    ),
) -> None:
    """Run backtest over Lean CSV data and emit a decision-matrix report.

    Default wiring matches the live/validated strategy: Layer 2b themed screener
    with momentum scorer, top-5, linear weighting. Pass `--scorer lean` to
    re-examine the archived Layer 2c strategy.
    """
    from datetime import date

    from alphalens.archive.screeners.lean.config import DATA_DIR
    from alphalens.archive.screeners.lean.lean_csv_loader import load_lean_histories
    from alphalens.attribution.cost_model import cost_sensitivity_table
    from alphalens.attribution.factor_analysis import run_carhart_attribution
    from alphalens.attribution.regime import classify_regime, regime_breakdown
    from alphalens.attribution.report import (
        build_summary,
        rebalance_results_to_dataframe,
        write_markdown_report,
    )
    from alphalens.backtest.engine import BacktestEngine
    from alphalens.data.factors import load_carhart_daily
    from alphalens.data.store.history import HistoryStore

    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)

    scorer_fn, scorer_config, screener_tickers, extra_history = _resolve_scorer(scorer, benchmark)

    if fundamental_gate and scorer in ("momentum", "early-stage"):
        _apply_fundamental_gate(
            scorer_config,
            screener_tickers,
            source=fundamentals_source,
            with_prices=with_prices,
        )

    typer.echo(f"Loading OHLCV into HistoryStore ({start_date} → {end_date})…")
    histories = load_lean_histories(DATA_DIR, screener_tickers + extra_history)
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
    typer.echo(f"  {len(result.rebalance_results)} daily snapshots")
    if not result.rebalance_results:
        raise typer.BadParameter(
            "Backtest produced no daily snapshots — check data coverage / warmup"
        )

    summary = build_summary(result)
    cost_df = cost_sensitivity_table(result.portfolio_returns.tolist())

    benchmark_close = store.full(benchmark)["close"]
    regime_labels = classify_regime(benchmark_close)
    regimes = regime_breakdown(
        result.portfolio_returns,
        result.ic_series,
        result.universe_median_returns,
        regime_labels,
    )

    attribution = None
    if not no_attrib:
        try:
            carhart = load_carhart_daily(start=start_date, end=end_date)
            attribution = run_carhart_attribution(result.portfolio_returns, carhart)
        except (FileNotFoundError, ValueError) as exc:
            typer.echo(f"  Factor attribution skipped: {exc}")

    decile_ic, vol_decomp, tail_score = (
        _compute_diagnostics(result, regime_labels) if diagnose else ([], {}, 0.0)
    )
    theme_stats = _compute_theme_stats(result)

    report_path = Path(report)
    if not report_path.is_absolute():
        report_path = Path.cwd() / report_path
    write_markdown_report(
        result,
        report_path,
        summary,
        attribution,
        regimes,
        cost_df,
        decile_ic=decile_ic,
        vol_decomp=vol_decomp,
        tail_score=tail_score,
        theme_stats=theme_stats,
    )
    typer.echo(f"Report written to {report_path}")

    if csv:
        csv_path = Path(csv)
        if not csv_path.is_absolute():
            csv_path = Path.cwd() / csv_path
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        rebalance_results_to_dataframe(result).to_csv(csv_path, index=False)
        typer.echo(f"Daily CSV written to {csv_path}")

    _print_headline(summary, attribution)
