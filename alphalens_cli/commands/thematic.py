"""`alphalens thematic` — thematic event-driven tool (Phase A: news ingest)."""

from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path

import typer

from alphalens.thematic import news_ingest

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
