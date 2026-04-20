"""CLI entry points for the Layer 1 watchdog.

Subcommands invoked by launchd:

    alphalens watchdog run-once        # detection (every 15 min)
    alphalens watchdog process-queue   # worker (every 5 min)
    alphalens watchdog momentum-screen # Layer 2b daily scan (22:00)
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import typer
from dotenv import load_dotenv

from alphalens.config_gemini import build_gemini_config
from alphalens.queue import CandidateQueue, default_queue_path
from alphalens.runner import TradingAgentsRunner
from alphalens.watchdog.classifier import Action, SignalClassifier
from alphalens.watchdog.config import WATCHDOG_DEFAULTS
from alphalens.watchdog.dispatch.handlers.auto_trigger import (
    AutoTriggerEnqueueHandler,
)
from alphalens.watchdog.dispatch.handlers.digest import DigestHandler
from alphalens.watchdog.dispatch.handlers.telegram import TelegramHandler
from alphalens.watchdog.dispatch.router import DispatchRouter
from alphalens.watchdog.portfolio import PortfolioState, default_portfolio_path
from alphalens.watchdog.sources.cik_loader import CIKLoader
from alphalens.watchdog.sources.edgar import SECEdgarSource
from alphalens.watchdog.storage import SeenEventStore
from alphalens.watchdog.watchdog import Watchdog
from alphalens.worker import AnalysisWorker

load_dotenv()

watchdog_app = typer.Typer(
    name="watchdog",
    help="Layer 1 stock monitoring — SEC EDGAR + Telegram alerts.",
    no_args_is_help=True,
)


def _build_watchdog() -> Watchdog:
    user_agent = os.environ.get("WATCHDOG_USER_AGENT") or "AlphaLens Watchdog pajakkamil@gmail.com"
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    cfg = dict(WATCHDOG_DEFAULTS)
    cfg["user_agent"] = user_agent
    cfg["fetch_form4_details"] = True
    cfg["fetch_8k_details"] = True

    portfolio = PortfolioState.load(default_portfolio_path())

    home = Path.home() / ".alphalens" / "watchdog"
    cik_loader = CIKLoader(user_agent=user_agent, cache_path=home / "company_tickers.json")
    cik_loader.load()

    tickers = sorted(set(portfolio.held + portfolio.watchlist))
    if not tickers:
        raise typer.BadParameter(
            f"Portfolio is empty. Create {default_portfolio_path()} with 'held:' and 'watchlist:' lists."
        )

    store = SeenEventStore(home / "seen_events.db")
    source = SECEdgarSource(
        tickers=tickers,
        config=cfg,
        store=store,
        cik_loader=cik_loader,
    )

    telegram = TelegramHandler(bot_token=bot_token, chat_id=chat_id)
    digest = DigestHandler(db_path=home / "digest.db", sender=telegram)
    enqueue = AutoTriggerEnqueueHandler(queue_path=default_queue_path())

    router = DispatchRouter({
        Action.AUTO_TRIGGER: [enqueue, telegram],
        Action.APPROVAL: [telegram],
        Action.DIGEST: [digest],
    })

    return Watchdog(sources=[source], classifier=SignalClassifier(), portfolio=portfolio, router=router)


def _build_worker():
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    telegram = TelegramHandler(bot_token=bot_token, chat_id=chat_id)
    queue = CandidateQueue(default_queue_path())
    runner = TradingAgentsRunner()

    return AnalysisWorker(queue=queue, runner=runner, notifier=telegram)


@watchdog_app.command("scorer-stats")
def scorer_stats(
    since_days: int = typer.Option(30, help="Only count Layer 3 runs finished within the last N days"),
):
    """Layer 3 acceptance rate per scorer — used for paper-trade validation.

    Queries the candidate queue for completed TradingAgents runs, groups by
    `source` (e.g. 'momentum' vs 'early-stage'), and reports decision
    distribution + accept rate (BUY+OVERWEIGHT / total).
    """
    from alphalens.scorer_stats import compute_scorer_stats, format_stats_table

    stats = compute_scorer_stats(default_queue_path(), since_days=since_days)
    typer.echo(f"=== Scorer stats — last {since_days} days ===")
    typer.echo(format_stats_table(stats))


@watchdog_app.command("run-once")
def run_once():
    """Poll EDGAR once, classify new events, dispatch alerts."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    watchdog = _build_watchdog()
    result = watchdog.run_once()
    typer.echo(f"detected={result['events_detected']} dispatched={result['events_dispatched']}")


@watchdog_app.command("process-queue")
def process_queue():
    """Drain the auto-trigger queue (one job per call of TradingAgents).

    Uses a kernel-level flock on ~/.alphalens/watchdog/worker.lock so that
    launchd-spawned workers and manual runs never execute in parallel —
    parallel workers hammer the Gemini 1M-tokens/min quota and deadlock.
    """
    from alphalens.watchdog_lock import (
        WorkerLockBusy,
        default_worker_lock_path,
        worker_lock,
    )

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        with worker_lock(default_worker_lock_path()):
            worker = _build_worker()
            processed = worker.process_all()
            typer.echo(f"processed={processed}")
    except WorkerLockBusy:
        typer.echo("another worker instance is running — skipping (see worker.lock for pid)")
        raise typer.Exit(code=0)


@watchdog_app.command("momentum-screen")
def momentum_screen(
    top_n: int = typer.Option(5, help="Number of top momentum names to report"),
    dry_run: bool = typer.Option(False, help="Print report to stdout, skip Telegram send"),
    analyze: bool = typer.Option(
        False, help="Submit top-N to the candidate queue for Layer 3 deep analysis"
    ),
    scorer: str = typer.Option(
        "momentum",
        help="Which scorer to run: 'momentum' (default, late-stage trend continuation) or "
             "'early-stage' (CAN SLIM / VCP / Jegadeesh 11-1 base-breakout detection)",
    ),
):
    """Run the Layer 2b screener; optionally queue top-N for Layer 3.

    Two scorers:
      - momentum: 7-metric classic momentum (near_high/pct_20d/vol_surge/rel_strength/RSI/ADX/MACD)
      - early-stage: 7-metric base-breakout (base_breakout/accel/VCP/RSI_emergence/ADX_building/vol_accum/Jegadeesh_11_1)
    """
    import pandas as pd

    from alphalens.momentum_screener.pipeline import MomentumPipeline
    from alphalens.momentum_screener.reporter import format_telegram_report

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    curr_date = pd.Timestamp.today().strftime("%Y-%m-%d")

    # Select scorer
    if scorer == "momentum":
        pipeline = MomentumPipeline()
    elif scorer == "early-stage":
        from alphalens.momentum_screener.early_stage_scorer import (
            EARLY_STAGE_DEFAULTS,
            EarlyStageScorer,
        )
        from alphalens.momentum_screener.config import MOMENTUM_DEFAULTS

        # Merge: momentum guardrails (min_cap/price/vol, benchmark) + early-stage weights/thresholds
        cfg = dict(MOMENTUM_DEFAULTS)
        cfg.update(EARLY_STAGE_DEFAULTS)
        pipeline = MomentumPipeline(
            config=cfg,
            scorer=EarlyStageScorer(cfg),
            source_name="early-stage",
        )
    else:
        raise typer.BadParameter(f"Unknown scorer: {scorer!r}. Use 'momentum' or 'early-stage'.")

    result = pipeline.run(curr_date=curr_date, top_n=top_n)
    text = format_telegram_report(result, curr_date)

    # Historyczny zapis do monitoring store (non-blocking — fail doesn't break run)
    try:
        from alphalens.momentum_screener.history_store import MomentumHistoryStore
        from alphalens.backtest.weighting import compute_position_weights

        weights_list = compute_position_weights(len(result), "linear").tolist() if not result.empty else []
        MomentumHistoryStore().record_run(
            picks_df=result,
            config=pipeline.config,
            universe_size=len(pipeline.config.get("_universe_size", [])) or 0,
            weighting_scheme="linear",
            weights=weights_list,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"history store skipped: {exc}")

    if analyze:
        with CandidateQueue(default_queue_path()) as queue:
            submitted = queue.submit(pipeline.to_candidates(result))
        typer.echo(f"queued {submitted} {scorer} candidate(s) for Layer 3")

    if dry_run:
        typer.echo(text)
        return

    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    telegram = TelegramHandler(bot_token=bot_token, chat_id=chat_id)
    telegram.send_message(text)
    typer.echo(f"sent {len(result)} {scorer} candidates to Telegram")


@watchdog_app.command("lean-screen")
def lean_screen(
    top_n: int = typer.Option(30, help="Number of top Lean names to keep"),
    dry_run: bool = typer.Option(False, help="Print results to stdout, skip queue submit"),
    analyze: bool = typer.Option(
        False, help="Submit top-N to the candidate queue for Layer 3 deep analysis"
    ),
    sync_only: bool = typer.Option(
        False, help="Only run the Polygon → Lean CSV sync; skip the Lean run"
    ),
):
    """Run the Layer 2c Lean batch screener (daily, Polygon + Docker)."""
    from datetime import date

    from alphalens.lean_screener.config import LEAN_DEFAULTS, polygon_api_key
    from alphalens.lean_screener.data_sync import PolygonLeanSync
    from alphalens.lean_screener.lean_csv_writer import LeanCsvWriter
    from alphalens.lean_screener.pipeline import LeanScreenerPipeline
    from alphalens.lean_screener.polygon_client import PolygonClient
    from alphalens.lean_screener.runner import (
        LeanDockerRunner,
        default_run_config,
        docker_available,
    )
    from alphalens.lean_screener.universe import all_tickers

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    api_key = polygon_api_key()
    if not api_key:
        raise typer.BadParameter("POLYGON_API_KEY not set in environment")

    from alphalens.lean_screener.config import DATA_DIR

    state_path = DATA_DIR.parent / "sync_state.json"
    client = PolygonClient(
        api_key=api_key,
        rate_limit_per_min=LEAN_DEFAULTS["polygon_rate_limit_per_min"],
    )
    writer = LeanCsvWriter(DATA_DIR)
    sync = PolygonLeanSync(
        client=client,
        writer=writer,
        universe=all_tickers(),
        state_path=state_path,
    )

    if sync_only:
        report = sync.incremental_sync(
            today=date.today(),
            bootstrap_days=LEAN_DEFAULTS["history_bootstrap_days"],
        )
        typer.echo(
            f"sync: dates={len(report.dates_synced)} "
            f"tickers={report.tickers_written} bars={report.bars_written}"
        )
        return

    if not docker_available():
        raise typer.BadParameter(
            "Docker is not available. Install/start Docker Desktop before running lean-screen."
        )

    runner = LeanDockerRunner(default_run_config())
    pipeline = LeanScreenerPipeline(sync=sync, runner=runner)
    df = pipeline.run(today=date.today(), top_n=top_n)

    if analyze:
        with CandidateQueue(default_queue_path()) as queue:
            submitted = queue.submit(pipeline.to_candidates(df))
        typer.echo(f"queued {submitted} lean candidate(s) for Layer 3")

    if dry_run or not analyze:
        typer.echo(f"lean screener produced {len(df)} ranked tickers")
        if not df.empty:
            for _, row in df.iterrows():
                typer.echo(
                    f"  {int(row['rank']):>3} {row['ticker']:<6} "
                    f"score={row['score']:.3f} roc20={row['roc20']:.3f} "
                    f"vol_surprise={row['volume_surprise']:.2f} "
                    f"breakout={bool(row['breakout'])}"
                )


@watchdog_app.command("validate-llm-filter")
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
):
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
    from alphalens.lean_screener.config import BENCHMARKS, DATA_DIR, LEAN_DEFAULTS
    from alphalens.lean_screener.lean_csv_loader import load_lean_histories
    from alphalens.lean_screener.lean_project.scorer import rank_universe as lean_rank
    from alphalens.momentum_screener.universe import (
        flatten_universe,
        load_universe as load_2b,
    )

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)
    typer.echo(f"Okno picks: {start_date} → {end_date}")

    # Krok 1: wygeneruj picks przez BacktestEngine na Layer 2b universe
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
    # Dodaj themes (backtest report tego nie ma — uzupełniamy z themes_map)
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

    # Krok 2: wybierz scorer
    if scorer == "rule":
        scorer_fn = rule_based_tractability_scorer
        typer.echo("Scorer: rule-based deterministic (no LLM cost)")
    elif scorer == "gemini":
        from alphalens.backtest.llm_scorers import (
            gemini_flash_tractability_scorer,
        )
        scorer_fn = gemini_flash_tractability_scorer
        typer.echo(
            f"Scorer: Gemini Flash tractability — szacunkowy koszt "
            f"${len(picks_with_themes) * 0.02:.2f}"
        )
    elif scorer == "hybrid":
        from alphalens.backtest.llm_scorers import (
            rule_and_gemini_hybrid_scorer,
        )
        scorer_fn = rule_and_gemini_hybrid_scorer
        typer.echo("Scorer: hybrid (rule first, Gemini on uncertain)")
    elif scorer == "tradingagents":
        from alphalens.backtest.llm_scorers import (
            tradingagents_reduced_scorer,
        )
        scorer_fn = tradingagents_reduced_scorer
        typer.echo(
            f"Scorer: TradingAgents reduced (market+news) — szacunkowy koszt "
            f"${len(picks_with_themes) * 0.75:.2f}, czas ~{len(picks_with_themes) * 5:.0f} min"
        )
    else:
        raise typer.BadParameter(f"Unknown scorer: {scorer!r}")

    # Krok 3: uruchom validation
    typer.echo(f"Uruchamiam scorer na {len(picks_with_themes)} picks...")
    result = evaluate_historical_picks(picks_with_themes, scorer_fn, progress_every=20)

    # Krok 4: zapisz raport
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


@watchdog_app.command("momentum-status")
def momentum_status(
    days: int = typer.Option(30, help="Ile ostatnich dni runów pokazać"),
    top_n: int = typer.Option(5, help="Rozmiar top-N używanego w analizach"),
    staleness_threshold: int = typer.Option(
        10, help="Flag nazw które są w top-N przez ≥ N kolejnych runów"
    ),
    hhi_alert: float = typer.Option(
        0.70, help="Alert gdy dominujący theme weight > próg"
    ),
):
    """Dashboard monitoringu Layer 2b — rolling metrics z historic runs."""
    import pandas as pd

    from alphalens.momentum_screener.history_store import (
        MomentumHistoryStore,
        compute_staleness,
        compute_theme_hhi_by_day,
        compute_turnover_by_day,
    )

    store = MomentumHistoryStore()
    runs = store.recent_runs(days=days)
    if not runs:
        typer.echo(f"Brak runów w historii. Uruchom `alphalens watchdog momentum-screen` żeby zacząć zbierać dane.")
        raise typer.Exit(0)

    timeline = store.picks_timeline(days=days)

    # --- Nagłówek: ostatnie runy ---
    typer.echo(f"=== Layer 2b Monitoring — ostatnie {len(runs)} runów (limit {days} dni) ===")
    typer.echo("")
    typer.echo(f"{'Data':<12} {'Picks':>6} {'Universe':>10} {'Error':<20}")
    typer.echo("-" * 55)
    for r in runs[:15]:
        err = (r.error[:18] + "..") if r.error else ""
        typer.echo(f"{r.run_date.isoformat():<12} {r.scored_count:>6} {r.universe_size:>10} {err:<20}")
    if len(runs) > 15:
        typer.echo(f"  ... ({len(runs) - 15} starszych)")
    typer.echo("")

    if timeline.empty:
        typer.echo("Brak picks w timelinie — nie ma co analizować.")
        raise typer.Exit(0)

    # --- Theme HHI trend ---
    hhi_df = compute_theme_hhi_by_day(timeline, top_n=top_n)
    typer.echo(f"=== Theme concentration (top-{top_n}) ===")
    if not hhi_df.empty:
        mean_hhi = float(hhi_df["hhi"].mean())
        max_hhi_day = hhi_df.loc[hhi_df["hhi"].idxmax()]
        alert_days = int((hhi_df["dominant_weight"] > hhi_alert).sum())
        typer.echo(f"  Średnie HHI:        {mean_hhi:.3f}  (0 = rozproszona, 1 = jeden theme)")
        typer.echo(f"  Max HHI dzień:      {max_hhi_day['run_date']} — {max_hhi_day['hhi']:.3f} "
                   f"({max_hhi_day['dominant_theme']} {max_hhi_day['dominant_weight'] * 100:.0f}%)")
        typer.echo(f"  Dni alert >{hhi_alert * 100:.0f}%:    {alert_days}/{len(hhi_df)} "
                   f"({alert_days / len(hhi_df) * 100:.1f}%)")
        # Dystrybucja dominujących themes
        dom_counts = hhi_df["dominant_theme"].value_counts()
        typer.echo(f"  Dominujący:         " + ", ".join(
            f"{t}: {c} dni" for t, c in dom_counts.head(5).items()
        ))
    typer.echo("")

    # --- Turnover trend ---
    turn_df = compute_turnover_by_day(timeline, top_n=top_n)
    typer.echo(f"=== Turnover (top-{top_n}) ===")
    if len(turn_df) > 1:
        # Skip dzień 0 (zawsze 0.0)
        tds = turn_df.iloc[1:]
        mean_turn = float(tds["turnover"].mean())
        last_turn = float(tds.iloc[-1]["turnover"])
        typer.echo(f"  Średni turnover:    {mean_turn * 100:.1f}% (fraction names changing per day)")
        typer.echo(f"  Ostatni dzień:      {last_turn * 100:.1f}%")
    typer.echo("")

    # --- Staleness: nazwy które za długo siedzą w top-N ---
    stale_df = compute_staleness(timeline, top_n=top_n)
    flagged = stale_df[stale_df["consecutive_days"] >= staleness_threshold]
    typer.echo(f"=== Staleness (w top-{top_n} przez ≥{staleness_threshold} dni) ===")
    if flagged.empty:
        typer.echo(f"  Żaden ticker nie stoi w top-{top_n} dłużej niż {staleness_threshold} dni.")
    else:
        typer.echo(f"{'Ticker':<8} {'Days':>6} {'Last rank':>10}")
        for _, row in flagged.head(10).iterrows():
            typer.echo(f"{row['ticker']:<8} {row['consecutive_days']:>6} {row['last_rank']:>10}")
        if len(flagged) > 10:
            typer.echo(f"  ... ({len(flagged) - 10} więcej)")
    typer.echo("")


@watchdog_app.command("backtest")
def backtest(
    start: str = typer.Option("2024-07-01", help="Backtest window start (YYYY-MM-DD)"),
    end: str = typer.Option("2026-04-17", help="Backtest window end (YYYY-MM-DD)"),
    top_n: int = typer.Option(30, help="Top-N names to hold at each rebalance"),
    holding: int = typer.Option(5, help="Holding period in trading days"),
    cost_profile: str = typer.Option(
        "moderate", help="Cost profile: gross, aggressive, moderate, conservative"
    ),
    benchmark: str = typer.Option("SPY", help="Benchmark ticker for calendar + regime"),
    report: str = typer.Option(
        "docs/backtest/mvp1_report.md",
        help="Markdown report output path (relative to repo root)",
    ),
    csv: str = typer.Option(
        "", help="Optional daily results CSV output path (empty = skip)"
    ),
    no_ff3: bool = typer.Option(
        False, "--no-ff3", help="Skip Fama-French 3-factor alpha regression"
    ),
    diagnose: bool = typer.Option(
        False,
        "--diagnose",
        help="Retain per-day scored frames and append IC-by-decile + vol decomposition to report",
    ),
):
    """Run the MVP1 backtest over Lean CSV data and emit a decision-matrix report."""
    from datetime import date

    from alphalens.backtest.cost_model import cost_sensitivity_table
    from alphalens.backtest.engine import BacktestEngine
    from alphalens.backtest.factor_analysis import fama_french_alpha
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
    from alphalens.lean_screener.config import BENCHMARKS, DATA_DIR, LEAN_DEFAULTS
    from alphalens.lean_screener.factors import load_ff3_daily
    from alphalens.lean_screener.lean_csv_loader import load_lean_histories
    from alphalens.lean_screener.lean_project.scorer import rank_universe as lean_rank
    from alphalens.lean_screener.universe import all_tickers

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)

    typer.echo(f"Loading OHLCV into HistoryStore ({start_date} → {end_date})…")
    tickers = all_tickers() + list(BENCHMARKS)
    histories = load_lean_histories(DATA_DIR, tickers)
    store = HistoryStore(histories)
    typer.echo(f"  loaded {len(store.tickers())} tickers")

    engine = BacktestEngine(
        store,
        scorer=lean_rank,
        scorer_config=LEAN_DEFAULTS,
        holding_period=holding,
        top_n=top_n,
        benchmark=benchmark,
        screener_tickers=all_tickers(),
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

    # Regime breakdown driven by benchmark close series.
    benchmark_close = store.full(benchmark)["close"]
    regime_labels = classify_regime(benchmark_close)
    regimes = regime_breakdown(
        result.portfolio_returns, result.ic_series, result.universe_median_returns, regime_labels
    )

    alpha_result = None
    if not no_ff3:
        try:
            ff3 = load_ff3_daily(start=start_date, end=end_date)
            alpha_result = fama_french_alpha(result.portfolio_returns, ff3)
        except (FileNotFoundError, ValueError) as exc:
            typer.echo(f"  FF3 regression skipped: {exc}")

    # Optional diagnostics when --diagnose is set
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

    # Factor-aware monitoring — theme concentration per day
    from alphalens.backtest.theme_analysis import (
        snapshots_from_backtest,
        theme_series,
    )
    # Buduj mapę tickers → themes z curated YAML (jeśli istnieje, fallback to empty).
    themes_map: dict[str, list[str]] = {}
    try:
        from alphalens.momentum_screener.universe import (
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
        result, report_path, summary, alpha_result, regimes, cost_df,
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
    if alpha_result is not None:
        typer.echo(
            f"  ff3_alpha_ann     = {alpha_result.alpha_annualized * 100:+.2f}% "
            f"(t={alpha_result.alpha_tstat:+.2f})"
        )


@watchdog_app.command("status")
def status():
    """Report current state: queue, digest buffer, dedup count."""
    from alphalens.watchdog.status import collect_status, format_status

    home = Path.home() / ".alphalens" / "watchdog"
    result = collect_status(
        queue_path=default_queue_path(),
        digest_path=home / "digest.db",
        seen_path=home / "seen_events.db",
    )
    typer.echo(format_status(result))


if __name__ == "__main__":
    watchdog_app()
