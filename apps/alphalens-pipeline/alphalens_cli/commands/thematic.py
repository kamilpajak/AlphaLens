"""`alphalens thematic` — thematic event-driven tool (Phase A-D)."""

from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path

import pandas as pd
import typer
from alphalens_pipeline.observability.textfile import emit_domain_metrics
from alphalens_pipeline.thematic import clean_titles as clean_titles_mod
from alphalens_pipeline.thematic import news_ingest
from alphalens_pipeline.thematic import verify_cache as verify_cache_mod
from alphalens_pipeline.thematic.argumentation import orchestrator as brief_orchestrator
from alphalens_pipeline.thematic.extraction import event_extractor as gemini_flash
from alphalens_pipeline.thematic.extraction import themes as themes_mod
from alphalens_pipeline.thematic.mapping import orchestrator
from alphalens_pipeline.thematic.mapping import theme_mapper as gemini_mapper
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
    """Run DeepSeek v4-flash event extraction over one day's news, then roll up themes."""
    target = (
        dt.date.fromisoformat(date)
        if date
        else dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)
    )
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise typer.BadParameter("OPENROUTER_API_KEY missing from environment.")

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
        envvar="ALPHALENS_MAPPER_MODEL",
        help="OpenRouter LLM slug for theme mapping (default DeepSeek v4-pro).",
    ),
    keep_unverified: bool = typer.Option(
        False,
        "--keep-unverified",
        help="Include candidates that failed all 4 verification gates (audit/debug).",
    ),
) -> None:
    """Roll up novel themes from Phase B → DeepSeek v4-pro maps to candidates → verify."""
    # Default to yesterday so a same-day cron after Phase B extract sees a
    # fully-extracted day, matching `ingest` and `extract` defaults.
    target = (
        dt.date.fromisoformat(date)
        if date
        else dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)
    )
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise typer.BadParameter("OPENROUTER_API_KEY missing from environment.")
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


def _fmt_str_or_dash(value, max_len: int) -> str:
    """NaN-safe string truncation for CLI preview tables.

    Sibling of :func:`_fmt_num_or_dash`. ``row.get("col") or "?"`` is NOT
    safe for object-dtype string columns: pandas writes missing string
    values as ``float('nan')`` (NOT ``None``), and ``bool(float('nan'))``
    is ``True`` — so the ``or`` short-circuit never fires and the
    downstream ``[:max_len]`` slice crashes with
    ``TypeError: 'float' object is not subscriptable``. Empty strings are
    also rendered as a dash so an explicit ``""`` from a coalesced source
    reads the same as a true missing value in the preview panel.

    Bug class observed on VPS 2026-05-30 (``industry_name`` NaN halted
    the daily pipeline). The helper centralises the guard so every str
    column in the preview shares the same treatment instead of each one
    re-inventing a defensive idiom.
    """
    if value is None or pd.isna(value):
        return "-"
    text = str(value)
    if not text:
        return "-"
    return text[:max_len]


def _print_score_preview(enriched: pd.DataFrame) -> None:
    typer.echo("")
    typer.echo(
        f"{'ticker':8s} {'industry':20s} {'score':5s} {'ins$':>10s} "
        f"{'fcff%':>6s} {'val%':>5s} technicals"
    )
    typer.echo("-" * 110)
    for _, row in enriched.head(25).iterrows():
        ind = _fmt_str_or_dash(row.get("industry_name"), 19)
        ins = row.get("insider_score_usd")
        ins_str = f"{ins / 1000:.0f}k" if ins is not None and not pd.isna(ins) else "-"
        fcff_str = _fmt_num_or_dash(row.get("fcff_yield_pct"), ".1f")
        val_str = _fmt_num_or_dash(row.get("valuation_composite_sector_percentile"), ".0f")
        tech_str = _fmt_str_or_dash(row.get("technicals_summary_str"), 50)
        typer.echo(
            f"{row['ticker']:8s} {ind:20s} {int(row['layer4_weighted_score']):>5d} "
            f"{ins_str:>10s} {fcff_str:>6s} {val_str:>5s} "
            f"{tech_str}"
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
        help="Phase E brief parquet root.",
    ),
) -> None:
    """Layer 5 brief generator — enrich scored candidates with structured brief fields."""
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
    out_parquet = output_dir / f"{target.isoformat()}.parquet"
    typer.echo(f"Wrote {len(enriched)} briefs → {out_parquet}")
    typer.echo(f"  Pro: {n_pro}, Flash: {n_flash}")

    # Domain counters for the cron-observability dashboard (PR-2 of
    # the epic). ``briefs_total`` is the headline number Grafana
    # surfaces on the main panel; the per-model split is a tracer
    # for "Pro-quota burned" (Pro routing is gated on user-defined
    # Tier-1 confidence so a sudden Pro spike or zero is worth
    # noticing).
    #
    # Wrap in try/except: briefs parquet is already written, so a
    # metrics-dir failure must not turn this run into a unit
    # failure (zen pre-merge rule, PR #311).
    try:
        emit_domain_metrics(
            job="thematic-build",
            metrics={
                "alphalens_thematic_briefs_total": len(enriched),
                'alphalens_thematic_briefs_by_model{model="pro"}': n_pro,
                'alphalens_thematic_briefs_by_model{model="flash"}': n_flash,
            },
        )
    except Exception:
        logger.exception("emit_domain_metrics failed; thematic-build run succeeded")


@thematic_app.command("verify-cache")
def verify_cache_command(
    days: int = typer.Option(
        7,
        "--days",
        help=(
            "Window size in calendar days, inclusive of today. Defaults to "
            "7 (a clean week of news). Use 30 to cover the full "
            "catalyst_resolver lookback."
        ),
    ),
    cache_dir: Path = typer.Option(
        verify_cache_mod.DEFAULT_CACHE_DIR,
        "--cache-dir",
        help="Override the default thematic_news parquet root.",
    ),
    alert: bool = typer.Option(
        False,
        "--alert",
        help=(
            "On missing days, post a digest to Telegram via TELEGRAM_BOT_TOKEN "
            "+ TELEGRAM_CHAT_ID env vars. Default off — only the systemd "
            "ExecStartPost hook should set this."
        ),
    ),
    today: str = typer.Option(
        None,
        "--today",
        help=(
            "Override the anchor date in YYYY-MM-DD format. Defaults to "
            "UTC today. Tests pin a fixed anchor; the systemd hook leaves "
            "this unset so wall-clock UTC drives the check."
        ),
    ),
    lag_days: int = typer.Option(
        1,
        "--lag-days",
        help=(
            "Offset between today and the last expected file date. "
            "Default 1 because ``thematic ingest`` writes a parquet "
            "keyed on yesterday's date — so the verifier window ends "
            "on T-1, not T. Pass 0 to inspect a window that includes "
            "the anchor itself."
        ),
    ),
) -> None:
    """Verify the thematic_news parquet cache has no missing days.

    Exits 0 when every requested day has a parquet (regardless of row
    count). Exits 1 when one or more days are missing — surfaces the
    silent failure documented as Risk A in
    ``docs/research/paper_trading_non_trading_day_2026_05_29.md`` §5.1.

    "Missing" means: no parquet at the expected path, OR a non-parquet
    file at the expected path (truncated write, foreign content).
    "Zero-row" parquets are NOT missing — they represent a legitimately
    quiet day and are reported separately for observability.
    """
    anchor = dt.date.fromisoformat(today) if today else None
    result = verify_cache_mod.verify_cache(
        cache_dir=cache_dir, days=days, today=anchor, lag_days=lag_days
    )

    typer.echo(
        f"verify-cache: {result.checked_days - len(result.missing_days)}/"
        f"{result.checked_days} dates present"
    )
    if result.zero_row_days:
        z = ", ".join(d.isoformat() for d in result.zero_row_days)
        typer.echo(f"  no-news (0-row parquet): {z}")
    if result.missing_days:
        m = ", ".join(d.isoformat() for d in result.missing_days)
        typer.echo(f"  MISSING: {m}", err=True)
        if alert:
            _post_verify_cache_alert(result)
        raise typer.Exit(code=1)


def _post_verify_cache_alert(result: verify_cache_mod.VerifyResult) -> None:
    """Post the missing-day digest to Telegram if credentials are set.

    Credential absence is logged + skipped (matches the
    ``literature_scanner._maybe_dispatch`` convention) so a fresh
    checkout without TELEGRAM_* env vars degrades gracefully to
    "exit 1 + stderr only".
    """
    from alphalens_pipeline.edgar_detector.dispatch.handlers.telegram import (
        TelegramHandler,
    )

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        logger.info(
            "verify-cache: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing; skipping alert dispatch"
        )
        return
    missing = ", ".join(d.isoformat() for d in result.missing_days)
    digest = (
        f"🚨 *AlphaLens news_ingest gap*\n"
        f"Missing parquet(s): {missing}\n"
        f"Window: {result.checked_days} days. "
        f"`journalctl --user -u alphalens-thematic-build.service` to "
        f"investigate."
    )
    handler = TelegramHandler(bot_token=bot_token, chat_id=chat_id)
    handler.send_message(digest)


@thematic_app.command("clean-titles")
def clean_titles_command(
    briefs_dir: Path = typer.Option(
        clean_titles_mod.DEFAULT_BRIEFS_DIR,
        "--briefs-dir",
        help="Directory of YYYY-MM-DD.parquet brief files to clean in place.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Count but do not write."),
) -> None:
    """One-off backfill: strip GDELT space-padded punctuation from legacy titles.

    PR #259 fixed this at the ingest source (forward-only). Rows ingested
    before 2026-05-27 keep the padded form in the parquet source-of-truth.
    Run this then ``manage.py rebuild_briefs_cache --force`` so Postgres
    refreshes from the cleaned parquets. Idempotent — re-running is a no-op.
    """
    if not briefs_dir.is_dir():
        raise typer.BadParameter(f"--briefs-dir does not exist: {briefs_dir}")
    result = clean_titles_mod.clean_titles_in_parquet_dir(briefs_dir, dry_run=dry_run)
    for path, n_cleaned in result.per_file:
        marker = "would clean" if dry_run else "cleaned"
        if n_cleaned:
            typer.echo(f"  {path.name}: {marker} {n_cleaned} row(s)")
        else:
            typer.echo(f"  {path.name}: clean")
    summary = (
        f"{result.total_rows_cleaned} row(s) across {result.files_touched} file(s)"
        if not dry_run
        else f"{result.total_rows_cleaned} row(s) would be cleaned (dry-run)"
    )
    typer.echo(f"done. {summary}.")
