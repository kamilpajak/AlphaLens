"""`alphalens thematic` — thematic event-driven tool (Phase A-D)."""

from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path

import pandas as pd
import typer
from alphalens_pipeline.thematic import news_ingest
from alphalens_pipeline.thematic.argumentation import orchestrator as brief_orchestrator
from alphalens_pipeline.thematic.extraction import gemini_flash
from alphalens_pipeline.thematic.extraction import themes as themes_mod
from alphalens_pipeline.thematic.mapping import gemini_mapper, orchestrator
from alphalens_pipeline.thematic.screening import scorer as screening_scorer

thematic_app = typer.Typer(
    name="thematic",
    help="Thematic event-driven tool (parallel track to factor-paradigm-search).",
    no_args_is_help=True,
)

logger = logging.getLogger(__name__)

_DATE_OPTION_HELP = "UTC date in YYYY-MM-DD (default: yesterday)."


@thematic_app.callback()
def _thematic_callback() -> None:
    """Force multi-command behaviour even when only one command is registered."""


@thematic_app.command("ingest")
def ingest(
    date: str = typer.Option(None, "--date", help=_DATE_OPTION_HELP),
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
    date: str = typer.Option(None, "--date", help=_DATE_OPTION_HELP),
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
    date: str = typer.Option(None, "--date", help=_DATE_OPTION_HELP),
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
    dropped = int(df.attrs.get("dropped_total", 0))
    all_unknown = int(df.attrs.get("dropped_all_unknown", 0))
    summary = f"Wrote {len(df)} candidate rows → {out_path}"
    if dropped > 0:
        summary += f"  (dropped {dropped} unverified, of which {all_unknown} were all-unknown)"
    typer.echo(summary)
    if len(df) > 0:
        typer.echo("")
        typer.echo(
            f"{'theme':28s} {'ticker':8s} {'pass':20s} {'unknown':16s} {'conf':4s}  rationale"
        )
        typer.echo("-" * 110)
        for _, row in df.head(25).iterrows():
            passed = ",".join(row["gates_passed"]) or "(none)"
            unknown = ",".join(row.get("gates_unknown", []) or []) or "-"
            typer.echo(
                f"{row['theme'][:27]:28s} {row['ticker']:8s} "
                f"{passed:20s} {unknown:16s} {row['gemini_confidence']:.2f}  "
                f"{row['rationale'][:40]}"
            )


DEFAULT_SCORED_DIR = Path.home() / ".alphalens" / "thematic_scored"


@thematic_app.command("score")
def score(
    date: str = typer.Option(None, "--date", help=_DATE_OPTION_HELP),
    candidates_dir: Path = typer.Option(
        orchestrator.DEFAULT_OUTPUT_DIR,
        "--candidates-dir",
        help="Phase C candidate parquet root.",
    ),
    output_dir: Path = typer.Option(
        DEFAULT_SCORED_DIR,
        "--output-dir",
        help="Phase D scored parquet root.",
    ),
) -> None:
    """Layer 4 quantitative screen — enrich Phase C candidates with 4 signals."""
    target = (
        dt.date.fromisoformat(date)
        if date
        else dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)
    )
    src = candidates_dir / f"{target.isoformat()}.parquet"
    if not src.exists():
        raise typer.BadParameter(f"Phase C parquet missing: {src}")

    candidates = pd.read_parquet(src)
    typer.echo(f"Scoring {len(candidates)} candidates from {src} (asof={target.isoformat()})...")
    enriched = screening_scorer.score_candidates(candidates, asof=target)

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{target.isoformat()}.parquet"
    enriched.to_parquet(out_path, index=False)

    typer.echo(f"Wrote {len(enriched)} scored rows → {out_path}")
    if enriched.empty:
        return
    score_counts = enriched["layer4_weighted_score"].value_counts().sort_index().to_dict()
    typer.echo(f"  layer4_weighted_score distribution: {score_counts}")
    _print_score_preview(enriched)


def _fmt_num_or_dash(value, fmt: str) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{value:{fmt}}"


def _print_score_preview(enriched: pd.DataFrame) -> None:
    typer.echo("")
    typer.echo(
        f"{'ticker':8s} {'industry':20s} {'score':5s} {'ins$':>10s} "
        f"{'fcff%':>6s} {'val%':>5s} technicals"
    )
    typer.echo("-" * 110)
    for _, row in enriched.head(25).iterrows():
        ind = (row.get("industry_name") or "?")[:19]
        ins = row.get("insider_score_usd")
        ins_str = f"{ins / 1000:.0f}k" if ins is not None and not pd.isna(ins) else "-"
        fcff_str = _fmt_num_or_dash(row.get("fcff_yield_pct"), ".1f")
        val_str = _fmt_num_or_dash(row.get("valuation_composite_sector_percentile"), ".0f")
        typer.echo(
            f"{row['ticker']:8s} {ind:20s} {int(row['layer4_weighted_score']):>5d} "
            f"{ins_str:>10s} {fcff_str:>6s} {val_str:>5s} "
            f"{row.get('technicals_summary_str', '')[:50]}"
        )


@thematic_app.command("brief")
def brief(
    date: str = typer.Option(None, "--date", help=_DATE_OPTION_HELP),
    scored_dir: Path = typer.Option(
        DEFAULT_SCORED_DIR,
        "--scored-dir",
        help="Phase D scored parquet root.",
    ),
    output_dir: Path = typer.Option(
        brief_orchestrator.DEFAULT_OUTPUT_DIR,
        "--output-dir",
        help="Phase E brief parquet + markdown root.",
    ),
) -> None:
    """Layer 5 brief generator — compose mid-format markdown per scored candidate."""
    target = (
        dt.date.fromisoformat(date)
        if date
        else dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)
    )
    src = scored_dir / f"{target.isoformat()}.parquet"
    if not src.exists():
        raise typer.BadParameter(f"Phase D scored parquet missing: {src}")

    scored = pd.read_parquet(src)
    typer.echo(
        f"Generating briefs for {len(scored)} scored rows from {src} (asof={target.isoformat()})..."
    )
    enriched = brief_orchestrator.generate_briefs(scored, asof=target, output_dir=output_dir)

    n_pro = int(enriched.attrs.get("n_pro", 0))
    n_flash = int(enriched.attrs.get("n_flash", 0))
    out_md = output_dir / f"{target.isoformat()}.md"
    out_parquet = output_dir / f"{target.isoformat()}.parquet"
    typer.echo(f"Wrote {len(enriched)} briefs → {out_parquet}")
    typer.echo(f"  Pro: {n_pro}, Flash: {n_flash}")
    typer.echo(f"  Markdown bundle: {out_md}")
