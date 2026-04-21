"""`alphalens themed` — Layer 2b curated YAML universe scan + monitoring."""

from __future__ import annotations

import os

import typer

from alphalens.queue import CandidateQueue, default_queue_path
from alphalens.watchdog.dispatch.handlers.telegram import TelegramHandler

themed_app = typer.Typer(
    name="themed",
    help="Layer 2b: curated themed YAML universe scan (pluggable scorer).",
    no_args_is_help=True,
)


@themed_app.command(name="screen")
def screen(
    top_n: int = typer.Option(5, help="Number of top names to report"),
    dry_run: bool = typer.Option(False, help="Print report to stdout, skip Telegram send"),
    analyze: bool = typer.Option(
        False, help="Submit top-N to the candidate queue for Layer 3 deep analysis"
    ),
    scorer: str = typer.Option(
        ...,
        help="Required. Which scorer to run: 'momentum' (late-stage trend continuation) or "
             "'early-stage' (CAN SLIM / VCP / Jegadeesh 11-1 base-breakout detection)",
    ),
) -> None:
    """Run the Layer 2b screener; optionally queue top-N for Layer 3.

    Two scorers:
      - momentum: 7-metric classic momentum (near_high/pct_20d/vol_surge/rel_strength/RSI/ADX/MACD)
      - early-stage: 7-metric base-breakout (base_breakout/accel/VCP/RSI_emergence/ADX_building/vol_accum/Jegadeesh_11_1)
    """
    import pandas as pd

    from alphalens.screeners.themed.pipeline import ThemedPipeline
    from alphalens.screeners.themed.reporter import format_telegram_report


    curr_date = pd.Timestamp.today().strftime("%Y-%m-%d")

    if scorer == "momentum":
        pipeline = ThemedPipeline()
    elif scorer == "early-stage":
        from alphalens.screeners.themed.config import THEMED_DEFAULTS
        from alphalens.screeners.themed.early_stage_scorer import (
            EARLY_STAGE_DEFAULTS,
            EarlyStageScorer,
        )

        cfg = dict(THEMED_DEFAULTS)
        cfg.update(EARLY_STAGE_DEFAULTS)
        pipeline = ThemedPipeline(
            config=cfg,
            scorer=EarlyStageScorer(cfg),
            source_name="early-stage",
        )
    else:
        raise typer.BadParameter(f"Unknown scorer: {scorer!r}. Use 'momentum' or 'early-stage'.")

    result = pipeline.run(curr_date=curr_date, top_n=top_n)
    text = format_telegram_report(result, curr_date)

    try:
        from alphalens.backtest.weighting import compute_position_weights
        from alphalens.screeners.themed.history_store import ThemedHistoryStore

        weights_list = compute_position_weights(len(result), "linear").tolist() if not result.empty else []
        ThemedHistoryStore().record_run(
            picks_df=result,
            config=pipeline.config,
            universe_size=len(pipeline.config.get("_universe_size", [])) or 0,
            weighting_scheme="linear",
            weights=weights_list,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"history store skipped: {exc}")

    if analyze:
        try:
            with CandidateQueue(default_queue_path()) as queue:
                submitted = queue.submit(pipeline.to_candidates(result))
            typer.echo(f"queued {submitted} {scorer} candidate(s) for Layer 3")
        except Exception as exc:  # noqa: BLE001
            typer.echo(f"queue submit failed: {exc}", err=True)
            text += f"\n\n[ALERT] queue submit failed: {exc}"

    if dry_run:
        typer.echo(text)
        return

    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    telegram = TelegramHandler(bot_token=bot_token, chat_id=chat_id)
    telegram.send_message(text)
    typer.echo(f"sent {len(result)} {scorer} candidates to Telegram")


@themed_app.command(name="status")
def status(
    days: int = typer.Option(30, help="Ile ostatnich dni runów pokazać"),
    top_n: int = typer.Option(5, help="Rozmiar top-N używanego w analizach"),
    staleness_threshold: int = typer.Option(
        10, help="Flag nazw które są w top-N przez ≥ N kolejnych runów"
    ),
    hhi_alert: float = typer.Option(
        0.70, help="Alert gdy dominujący theme weight > próg"
    ),
) -> None:
    """Dashboard monitoringu Layer 2b — rolling metrics z historic runs."""
    from alphalens.screeners.themed.history_store import (
        ThemedHistoryStore,
        compute_staleness,
        compute_theme_hhi_by_day,
        compute_turnover_by_day,
    )

    store = ThemedHistoryStore()
    runs = store.recent_runs(days=days)
    if not runs:
        typer.echo("Brak runów w historii. Uruchom `alphalens themed screen` żeby zacząć zbierać dane.")
        raise typer.Exit(0)

    timeline = store.picks_timeline(days=days)

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
        dom_counts = hhi_df["dominant_theme"].value_counts()
        typer.echo("  Dominujący:         " + ", ".join(
            f"{t}: {c} dni" for t, c in dom_counts.head(5).items()
        ))
    typer.echo("")

    turn_df = compute_turnover_by_day(timeline, top_n=top_n)
    typer.echo(f"=== Turnover (top-{top_n}) ===")
    if len(turn_df) > 1:
        tds = turn_df.iloc[1:]
        mean_turn = float(tds["turnover"].mean())
        last_turn = float(tds.iloc[-1]["turnover"])
        typer.echo(f"  Średni turnover:    {mean_turn * 100:.1f}% (fraction names changing per day)")
        typer.echo(f"  Ostatni dzień:      {last_turn * 100:.1f}%")
    typer.echo("")

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
