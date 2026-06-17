"""`alphalens thematic` — thematic event-driven tool (Phase A-D)."""

from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path
from typing import Any

import pandas as pd
import typer
from alphalens_pipeline.observability.textfile import emit_domain_metrics
from alphalens_pipeline.thematic import clean_titles as clean_titles_mod
from alphalens_pipeline.thematic import news_ingest
from alphalens_pipeline.thematic import verify_cache as verify_cache_mod
from alphalens_pipeline.thematic.argumentation import orchestrator as brief_orchestrator
from alphalens_pipeline.thematic.extraction import event_extractor
from alphalens_pipeline.thematic.extraction import themes as themes_mod
from alphalens_pipeline.thematic.mapping import orchestrator, theme_mapper
from alphalens_pipeline.thematic.screening import scorer as screening_scorer

thematic_app = typer.Typer(
    name="thematic",
    help="Thematic event-driven tool (parallel track to factor-paradigm-search).",
    no_args_is_help=True,
)

logger = logging.getLogger(__name__)

_DATE_OPTION_HELP = "UTC date in YYYY-MM-DD (default: yesterday)."


def _stage_volume_metrics(stage: str, *, output_rows: int, input_rows: int) -> dict[str, int]:
    """The Phase 4 per-stage input/output row-count gauge pair.

    One canonical metric-name pair across all 5 stages (distinguished by the
    ``stage`` label) so a single PromQL rule covers the whole pipeline. The
    ``input``/``output`` split lets the ``AlphalensThematicStageZeroOutput``
    alert assert "consumed input but produced nothing" — the silent
    model-retirement signature — instead of false-paging on a quiet day.
    """
    return {
        f'alphalens_thematic_stage_output_rows{{stage="{stage}"}}': output_rows,
        f'alphalens_thematic_stage_input_rows{{stage="{stage}"}}': input_rows,
    }


def _source_volume_metrics(counts: dict[str, int]) -> dict[str, int]:
    """Per-source RAW row-count gauges for the ingest dead-man-switch (#384).

    One metric name + a ``source`` label per source, mirroring the Phase-4
    ``alphalens_thematic_stage_*_rows{stage=...}`` convention. The edgar series
    is load-bearing: it catches the epic-#379 case where the SEC EX-99.1
    daily-index ingest goes dark under concurrent per-IP 403 load and the empty
    frame is swallowed by ``_safe_call``. Emitted UNCONDITIONALLY for every
    source present in ``counts`` (edgar=0 is the signal — a skipped emit lets
    node_exporter re-serve the last nonzero forever and silences the alert). An
    empty ``counts`` (cache-hit path) yields no entries.
    """
    return {
        f'alphalens_thematic_source_rows{{source="{source}"}}': count
        for source, count in counts.items()
    }


def _is_filled_template_id(v: Any) -> bool:
    """True when ``v`` is a non-empty, non-sentinel brief_template_id.

    Guards ``pd.isna`` BEFORE ``bool(v)``: a pandas nullable ``pd.NA`` (possible
    if the column reads back as a pyarrow/string dtype) makes ``bool(v)`` raise
    ``TypeError``, which would propagate through ``.apply`` and silently drop the
    gauge for that run. Excludes None / NaN / NA and the string sentinels
    ("", "None", "nan").
    """
    if v is None:
        return False
    try:
        if pd.isna(v):
            return False
    except (TypeError, ValueError):
        # pd.isna raises on some array-likes; a non-NA scalar falls through.
        pass
    return str(v) not in ("", "None", "nan")


def _brief_template_fill_metrics(enriched: pd.DataFrame) -> dict[str, float | int]:
    """Fill-rate of `brief_template_id` across a day's briefs (#399 gate instrument).

    Decides #394 (whether to build the option-c beneficiary catalyst-provenance
    bridge): measures whether the shipped option-b (#397, candidate==subject)
    actually delivers a typed-fact template_id at a useful rate. Pure read of the
    brief frame -- no schema/UI/pipeline change. Emits the count + the ratio so a
    Grafana panel can show the trend; a sustained low ratio (with group demand)
    is the gate signal. Denominator is the same `alphalens_thematic_briefs_total`
    already emitted; we add the numerator + the precomputed ratio.

    "Filled" = a non-empty, non-sentinel brief_template_id. Missing column
    (legacy frame) or zero briefs -> 0 filled / ratio 0.0 (no div-by-zero).
    """
    total = len(enriched)
    if total == 0 or "brief_template_id" not in enriched.columns:
        filled = 0
    else:
        filled = int(enriched["brief_template_id"].apply(_is_filled_template_id).sum())
    ratio = round(filled / total, 4) if total else 0.0
    return {
        "alphalens_thematic_brief_template_id_total": filled,
        "alphalens_thematic_brief_template_id_fill_ratio": ratio,
    }


def _emit_stage_volume(stage: str, *, output_rows: int, input_rows: int) -> None:
    """Emit a stage's volume gauges to its own ``thematic-<stage>`` file.

    Each stage is a separate process in ``run_thematic_day.sh`` and the
    textfile name is keyed on ``job`` — a shared job would have each stage
    clobber the prior one's file, so every stage gets a distinct job name.
    Wrapped in try/except (zen PR #311 rule): the stage's parquet is already
    written, so a metrics-dir failure must not turn a good run into a unit
    failure.
    """
    try:
        metrics = _stage_volume_metrics(stage, output_rows=output_rows, input_rows=input_rows)
        emit_domain_metrics(job=f"thematic-{stage}", metrics=metrics)
    except Exception:
        logger.exception("emit_domain_metrics failed for stage %s; the run succeeded", stage)


def _parquet_num_rows(path: Path) -> int:
    """Row count of a parquet via its footer metadata (no full read).

    Returns 0 if the file is absent — an upstream stage that produced nothing
    leaves no file, which is exactly the "zero input" the alert must see.

    A corrupt / unreadable footer also degrades to 0 rather than raising:
    this is called as an ARGUMENT to ``_emit_stage_volume`` (outside its
    try/except), and a metric read must never crash a stage whose real
    output is already written — so any read failure is observability debt,
    logged and treated as zero.
    """
    if not path.exists():
        return 0
    import pyarrow.parquet as pq

    try:
        pf = pq.ParquetFile(path)
        try:
            return pf.metadata.num_rows
        finally:
            pf.close()
    except Exception:
        logger.exception("failed to read parquet footer for %s; treating as 0 rows", path)
        return 0


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

    source_row_counts: dict[str, int] = {}
    df = news_ingest.ingest_daily(
        date=target,
        cache_dir=cache_dir,
        max_items=max_items,
        polygon_api_key=polygon_api_key,
        force=force,
        source_row_counts=source_row_counts,
    )
    cache_path = cache_dir / f"{target.isoformat()}.parquet"
    typer.echo(f"Ingested {len(df)} items for {target.isoformat()} → {cache_path}")
    if len(df) > 0:
        by_src = df["source"].value_counts().to_dict()
        typer.echo(f"  by source: {by_src}")
        unique_tickers = sorted({t for row in df["tickers"] for t in row})
        typer.echo(f"  unique tickers tagged: {len(unique_tickers)}")

    # The Phase-4 stage gauge (input==output, no upstream) folded with the #384
    # per-source RAW row-count gauges into ONE emit so they share the single
    # thematic-ingest textfile + one atomic write. The per-source gauges carry
    # the edgar_press_release dead-man-switch signal that the aggregate stage
    # gauge cannot (it undercounts edgar after dedup/cap). Wrapped in try/except
    # (PR #311 rule): the parquet is already persisted, so a metrics-dir failure
    # must never fail a good ingest. On a cache hit source_row_counts is empty,
    # so only the stage gauges are emitted.
    metrics: dict[str, int] = _stage_volume_metrics(
        "ingest", output_rows=len(df), input_rows=len(df)
    )
    metrics.update(_source_volume_metrics(source_row_counts))
    try:
        emit_domain_metrics(job="thematic-ingest", metrics=metrics)
    except Exception:
        logger.exception("emit_domain_metrics failed for stage ingest; the run succeeded")


@thematic_app.command("extract")
def extract(
    date: str = typer.Option(None, "--date", help=_DATE_OPTION_HELP),
    news_dir: Path = typer.Option(
        event_extractor.DEFAULT_NEWS_DIR, "--news-dir", help="Unified-news parquet root."
    ),
    events_dir: Path = typer.Option(
        event_extractor.DEFAULT_EVENTS_DIR,
        "--events-dir",
        help="Extracted-events parquet root.",
    ),
    model: str = typer.Option(
        event_extractor.DEFAULT_MODEL,
        "--model",
        envvar="ALPHALENS_EXTRACT_MODEL",
        help="OpenRouter LLM slug for event extraction (default DeepSeek v4-flash; "
        "env ALPHALENS_EXTRACT_MODEL as default, --model overrides).",
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

    events = event_extractor.extract_daily(
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

    # THE primary model-retirement catch: news in (input) vs events out
    # (output). A DeepSeek Flash 404 yields 0 events from a non-empty news
    # day while the run still exits 0.
    _emit_stage_volume(
        "extract",
        output_rows=len(events),
        input_rows=_parquet_num_rows(news_dir / f"{target.isoformat()}.parquet"),
    )

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
        event_extractor.DEFAULT_EVENTS_DIR,
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
        help="Cap on novel themes mapped per run (DeepSeek v4-pro spend control).",
    ),
    model: str = typer.Option(
        theme_mapper.DEFAULT_MODEL,
        "--model",
        envvar="ALPHALENS_MAPPER_MODEL",
        help="OpenRouter LLM slug for theme mapping (default DeepSeek v4-pro).",
    ),
    keep_unverified: bool = typer.Option(
        False,
        "--keep-unverified",
        help="Include candidates that failed all 4 verification gates (audit/debug).",
    ),
    rebuild: bool = typer.Option(
        False,
        "--rebuild",
        help=(
            "Force re-proposing candidates even when a frozen parquet for this date "
            "already exists. By default map-themes is idempotent per (date, config): "
            "a rerun reuses the frozen set instead of re-rolling the LLM proposal."
        ),
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
        # Quiet day: nothing to map, but still write a typed-empty candidates
        # parquet so `score` finds the file and the run_thematic_day.sh `set -e`
        # chain does not abort before brief + rebuild-cache. The empty set is
        # recompute-eligible, so a later same-date slot with news is not frozen.
        out_path = orchestrator.write_empty_candidates(
            asof=target, output_dir=output_dir, model=model
        )
        typer.echo(
            f"No novel themes above {novelty_threshold:.1f} in {window_days}d window — "
            f"wrote empty candidate set → {out_path}"
        )
        return

    typer.echo(f"Mapping {len(novel)} novel themes via DeepSeek v4-pro ({model})...")
    themes = list(novel["theme"])

    df = orchestrator.map_themes(
        themes=themes,
        asof=target,
        api_key=api_key,
        polygon_api_key=polygon_key,
        output_dir=output_dir,
        keep_unverified=keep_unverified,
        rebuild=rebuild,
        model=model,
    )
    # input = novel themes fed to the mapper; output = verified candidate
    # rows. 0 candidates from N novel themes = a DeepSeek Pro mapping /
    # verification failure (the early `return` above handles the legitimate
    # "no novel themes" case, which emits nothing — no false alert).
    _emit_stage_volume("map-themes", output_rows=len(df), input_rows=len(novel))

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
            passed = _fmt_gate_cell(row.get("gates_passed"), empty="(none)")
            unknown = _fmt_gate_cell(row.get("gates_unknown"), empty="-")
            typer.echo(
                f"{row['theme'][:27]:28s} {row['ticker']:8s} "
                f"{passed:20s} {unknown:16s} {row['llm_confidence']:.2f}  "
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

    # Cheap Buffett-delta numerics + quality score, stamped from the fundamentals
    # the scoring pass already fetched (companyfacts -> shared disk-cache hit). The
    # six columns ride the merge chain into the brief parquet + card chip; this is
    # display-only in v1 (it does NOT touch the brief sort). Lazy import keeps the
    # frequent-cron `alphalens` startup cheap. Fail-soft: any wiring failure leaves
    # the columns absent rather than aborting the score stage.
    from alphalens_pipeline.experts.buffett import quant_enrichment as buffett_quant_enrichment

    enriched = buffett_quant_enrichment.enrich(enriched, asof=target)

    # O'Neil momentum/technical numerics (PR-7). Runs AFTER score_candidates (so
    # the technical_* columns it reuses for N + L are on the frame) and after the
    # Buffett pass (so the shared companyfacts are already on disk -> O'Neil's
    # preload is a pure cache hit). Stamps eight oneil_* columns; display-only,
    # present-but-unread until PR-8 surfaces them. Same lazy-import + fail-soft
    # contract as the Buffett step.
    from alphalens_pipeline.experts.oneil import quant_enrichment as oneil_quant_enrichment

    enriched = oneil_quant_enrichment.enrich(enriched, asof=target)

    # Panel disagreement scalar (PR-8a): the raw gap between the two expert
    # composites, recorded per row for the deferred Expert×EDGE study (log-now).
    # Runs LAST — needs both buffett_quality_score + oneil_score on the frame.
    # Display-only, NOT in the brief sort (the PR-6 allowlist enforces). Pure frame
    # arithmetic (no store/network); a failure leaves the two panel columns absent
    # rather than aborting the score stage.
    from alphalens_pipeline.experts import disagreement

    enriched = disagreement.enrich(enriched)

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{target.isoformat()}.parquet"
    enriched.to_parquet(out_path, index=False)

    # Left-merge enrich: input == output in the normal case; a divergence
    # would flag a merge bug. Kept for uniform per-stage coverage.
    _emit_stage_volume("score", output_rows=len(enriched), input_rows=len(candidates))

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


def _fmt_gate_cell(value: Any, *, empty: str) -> str:
    """Render a ``gates_*`` list cell for the map-themes preview table.

    Sibling of :func:`_fmt_str_or_dash`. The candidate columns
    ``gates_passed`` / ``gates_unknown`` are Python lists on the
    fresh-compute path, but the idempotent-freeze reuse path (PR #611)
    reloads candidates from parquet, where list columns deserialize as
    numpy ndarrays. ``ndarray or []`` raises ``ValueError: truth value of
    an empty array is ambiguous`` for any array whose length is not 1 — so
    the common empty ``gates_unknown`` crashed the map-themes stage on
    EVERY reuse run (VPS 2026-06-17), halting the daily pipeline before
    score / brief / rebuild-cache. Guard on ``len`` and ``is None``, never
    on array truthiness.
    """
    if value is None:
        return empty
    items = [str(g) for g in value]
    return ",".join(items) if items else empty


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
                # Phase 4: brief is stage 5 of the uniform volume series.
                # Folded into the existing thematic-build file (the brief is
                # the same process) rather than a 6th job. scored rows in ->
                # briefs out; 0 briefs from non-empty scored = a Layer 5
                # generator / LLM failure.
                **_stage_volume_metrics("brief", output_rows=len(enriched), input_rows=len(scored)),
                # #399 gate instrument for #394: fraction of briefs carrying a
                # template_id (option-b subject-match fill-rate). Folded into the
                # same thematic-build file; a recording gauge for the dashboard,
                # no alert rule.
                **_brief_template_fill_metrics(enriched),
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
