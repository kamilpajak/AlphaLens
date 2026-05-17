"""`alphalens thematic` — thematic event-driven tool (Phase A ingest, B extract, C map)."""

from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path

import typer

from alphalens.thematic import news_ingest
from alphalens.thematic.extraction import gemini_flash
from alphalens.thematic.extraction import themes as themes_mod
from alphalens.thematic.mapping import gemini_mapper, orchestrator

thematic_app = typer.Typer(
    name="thematic",
    help="Thematic event-driven tool (parallel track to factor-paradigm-search).",
    no_args_is_help=True,
)

logger = logging.getLogger(__name__)


@thematic_app.callback()
def _thematic_callback() -> None:
    """Force multi-command behaviour even when only one command is registered."""


@thematic_app.command("ingest")
def ingest(
    date: str = typer.Option(None, "--date", help="UTC date in YYYY-MM-DD (default: yesterday)."),
    cache_dir: Path = typer.Option(
        news_ingest.DEFAULT_CACHE_DIR, "--cache-dir", help="Parquet output root."
    ),
    max_items: int = typer.Option(
        news_ingest.DEFAULT_MAX_ITEMS, "--max-items", help="Cap on items per day."
    ),
    force: bool = typer.Option(False, "--force", help="Bypass cache and refetch."),
) -> None:
    """Pull Polygon + GDELT + RSS + EDGAR for one day and write the unified parquet."""
    target = (
        dt.date.fromisoformat(date)
        if date
        else dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)
    )
    polygon_api_key = os.environ.get("POLYGON_API_KEY", "")
    if not polygon_api_key:
        logger.warning("POLYGON_API_KEY missing — Polygon source will be skipped.")

    df = news_ingest.ingest_daily(
        date=target,
        cache_dir=cache_dir,
        max_items=max_items,
        polygon_api_key=polygon_api_key,
        force=force,
    )
    cache_path = cache_dir / f"{target.isoformat()}.parquet"
    typer.echo(f"Ingested {len(df)} items for {target.isoformat()} → {cache_path}")
    if len(df) > 0:
        by_src = df["source"].value_counts().to_dict()
        typer.echo(f"  by source: {by_src}")
        unique_tickers = sorted({t for row in df["tickers"] for t in row})
        typer.echo(f"  unique tickers tagged: {len(unique_tickers)}")


@thematic_app.command("extract")
def extract(
    date: str = typer.Option(None, "--date", help="UTC date in YYYY-MM-DD (default: yesterday)."),
    news_dir: Path = typer.Option(
        gemini_flash.DEFAULT_NEWS_DIR, "--news-dir", help="Unified-news parquet root."
    ),
    events_dir: Path = typer.Option(
        gemini_flash.DEFAULT_EVENTS_DIR,
        "--events-dir",
        help="Extracted-events parquet root.",
    ),
    model: str = typer.Option(
        gemini_flash.DEFAULT_MODEL,
        "--model",
        envvar="GEMINI_MODEL",
        help="Gemini model id (env GEMINI_MODEL as default; --model overrides).",
    ),
    window_days: int = typer.Option(
        themes_mod.DEFAULT_WINDOW_DAYS,
        "--window-days",
        help="Theme rollup lookback window.",
    ),
    novelty_threshold: float = typer.Option(
        themes_mod.DEFAULT_NOVELTY_THRESHOLD,
        "--novelty-threshold",
        help="Recent/baseline ratio to flag a theme as novel.",
    ),
) -> None:
    """Run Gemini Flash event extraction over one day's news, then roll up themes."""
    target = (
        dt.date.fromisoformat(date)
        if date
        else dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)
    )
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise typer.BadParameter("GOOGLE_API_KEY missing from environment.")

    events = gemini_flash.extract_daily(
        date=target,
        news_dir=news_dir,
        events_dir=events_dir,
        api_key=api_key,
        model=model,
    )
    typer.echo(f"Extracted {len(events)} events for {target.isoformat()}")
    if len(events) > 0:
        ev_counts = events["event_type"].value_counts().to_dict()
        typer.echo(f"  by event_type: {ev_counts}")
        sent_counts = events["sentiment"].value_counts().to_dict()
        typer.echo(f"  by sentiment: {sent_counts}")

    rollup = themes_mod.roll_up(asof=target, events_dir=events_dir, window_days=window_days)
    typer.echo(f"Theme rollup ({window_days}d window): {len(rollup)} themes")
    novel = themes_mod.flag_novel(rollup, threshold=novelty_threshold)
    if len(novel) > 0:
        typer.echo(f"  NOVEL themes (novelty ≥ {novelty_threshold}):")
        for _, row in novel.head(10).iterrows():
            typer.echo(
                f"    - {row['theme']!r}: novelty={row['novelty_score']:.2f}, "
                f"recent={row['count_recent']}, baseline={row['count_baseline']}"
            )
    else:
        typer.echo("  no themes above novelty threshold yet (expected on first runs)")


@thematic_app.command("map-themes")
def map_themes_cmd(
    date: str = typer.Option(None, "--date", help="UTC date in YYYY-MM-DD (default: yesterday)."),
    events_dir: Path = typer.Option(
        gemini_flash.DEFAULT_EVENTS_DIR,
        "--events-dir",
        help="Phase B extracted-events parquet root.",
    ),
    output_dir: Path = typer.Option(
        orchestrator.DEFAULT_OUTPUT_DIR,
        "--output-dir",
        help="Phase C candidate-output parquet root.",
    ),
    window_days: int = typer.Option(
        themes_mod.DEFAULT_WINDOW_DAYS, "--window-days", help="Theme rollup lookback."
    ),
    novelty_threshold: float = typer.Option(
        themes_mod.DEFAULT_NOVELTY_THRESHOLD,
        "--novelty-threshold",
        help="Recent/baseline ratio for theme to be picked up by Layer 3.",
    ),
    max_themes: int = typer.Option(
        10,
        "--max-themes",
        help="Cap on novel themes mapped per run (Gemini 3 Pro spend control).",
    ),
    model: str = typer.Option(
        gemini_mapper.DEFAULT_MODEL,
        "--model",
        envvar="GEMINI_PRO_MODEL",
        help="Gemini 3 Pro model id.",
    ),
    keep_unverified: bool = typer.Option(
        False,
        "--keep-unverified",
        help="Include candidates that failed all 4 verification gates (audit/debug).",
    ),
) -> None:
    """Roll up novel themes from Phase B → Gemini 3 Pro maps to candidates → verify."""
    # Default to yesterday so a same-day cron after Phase B extract sees a
    # fully-extracted day, matching `ingest` and `extract` defaults.
    target = (
        dt.date.fromisoformat(date)
        if date
        else dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)
    )
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise typer.BadParameter("GOOGLE_API_KEY missing from environment.")
    polygon_key = os.environ.get("POLYGON_API_KEY", "")

    rollup = themes_mod.roll_up(asof=target, events_dir=events_dir, window_days=window_days)
    novel = themes_mod.flag_novel(rollup, threshold=novelty_threshold)
    novel = novel.head(max_themes)
    if len(novel) == 0:
        typer.echo(
            f"No novel themes above {novelty_threshold:.1f} in {window_days}d window — "
            f"nothing to map."
        )
        return

    typer.echo(f"Mapping {len(novel)} novel themes via Gemini 3 Pro ({model})...")
    themes = list(novel["theme"])

    df = orchestrator.map_themes(
        themes=themes,
        asof=target,
        api_key=api_key,
        polygon_api_key=polygon_key,
        output_dir=output_dir,
        keep_unverified=keep_unverified,
    )
    out_path = output_dir / f"{target.isoformat()}.parquet"
    typer.echo(f"Wrote {len(df)} candidate rows → {out_path}")
    if len(df) > 0:
        typer.echo("")
        typer.echo(f"{'theme':28s} {'ticker':8s} {'gates':30s} {'conf':4s}  rationale")
        typer.echo("-" * 100)
        for _, row in df.head(25).iterrows():
            gates = ",".join(row["gates_passed"]) or "(none)"
            typer.echo(
                f"{row['theme'][:27]:28s} {row['ticker']:8s} "
                f"{gates:30s} {row['gemini_confidence']:.2f}  "
                f"{row['rationale'][:50]}"
            )
