"""CLI: ``alphalens feedback`` subcommands for the broker-free feedback replay.

Ships ``backfill-shadow-returns`` only — the nightly VPS timer entrypoint that
drives the broker-free population-monitor replay engine (market-behavior
feedback). The Track-A user-action click ledger was removed (#465), so the
per-decision ladder replay that read the ``decisions`` table is gone too; the
population monitor (briefs + Polygon, parquet-only) is the sole feedback signal.

Lazy imports inside the command body keep the ``alphalens`` CLI startup time low
(Layer-1 ``edgar-detect`` cron ticks must not pay for pandas import cost we don't
need on that path).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import typer
from alphalens_pipeline.observability.textfile import emit_domain_metrics

logger = logging.getLogger(__name__)

feedback_app = typer.Typer(
    name="feedback",
    help="Broker-free feedback replay operator tools.",
    no_args_is_help=True,
)

# Per-user runtime data root (``~/.alphalens``). Holds the daily thematic brief
# parquets the broker-free population replay reads.
_ALPHALENS_HOME = Path.home() / ".alphalens"

# Settled-watermark sentinel: written after ALL enrichment passes finish so the
# Django mirror can tell a completed run's parquets ("settled") from a half-run
# mid-rewrite. Content carries an explicit epoch (not the file mtime) so a
# backup/rsync pass that rewrites file mtimes cannot corrupt the watermark. The
# name is intentionally not ``*.parquet`` so the ingest's ``_scan_parquets`` glob
# skips it. See docs/research/edge_enrichment_completion_stamp_design_2026_07_28.md.
_INGEST_WATERMARK_NAME = ".ingest_watermark.json"


def _write_ingest_watermark(store_dir: Path) -> None:
    """Stamp ``store_dir`` as settled as of now. Never raises. Atomic.

    ``completed_at`` is captured AFTER the last pass returns, so it is strictly
    greater than every parquet ``st_mtime`` written during the run. The Django
    ingest only trusts a parquet whose mtime is <= this watermark. Written via
    tmp + ``os.replace`` so a mirror never reads a torn sentinel (a torn read
    would fall back to the pure mtime gate and re-open the race for that tick).
    """
    try:
        if not store_dir.exists():
            return
        sentinel = store_dir / _INGEST_WATERMARK_NAME
        tmp = sentinel.with_suffix(sentinel.suffix + ".tmp")
        tmp.write_text(json.dumps({"completed_at": time.time()}))
        os.replace(tmp, sentinel)
    except Exception:
        logger.exception("failed to write ingest watermark; continuing")


# NOTE: the command name ``backfill-shadow-returns`` is retained for the existing
# systemd unit ``alphalens-feedback-shadow-returns.service`` (renaming would force
# VPS-survivor churn). The legacy shadow-return / execution-quality metrics were
# removed with the broker chain, and the per-decision ladder replay went with the
# click ledger (#465); this command now drives only the population monitor. A
# rename is a deferred follow-up.
@feedback_app.command(name="backfill-shadow-returns")
def backfill_shadow_returns_command(
    briefs_dir: Path = typer.Option(
        _ALPHALENS_HOME / "thematic_briefs",
        "--briefs-dir",
        help="Directory of daily thematic brief parquets (for the population replay).",
    ),
    lookback_days: int | None = typer.Option(
        None,
        "--lookback-days",
        min=0,
        help=(
            "Calendar days of brief dates to replay (default: the monitor's nightly "
            "window, 75). Raise it for a one-off store rebuild that must reach older briefs."
        ),
    ),
    dates: list[dt.datetime] | None = typer.Option(
        None,
        "--date",
        formats=["%Y-%m-%d"],
        help=(
            "Replay exactly this brief date, and repeat the option for more. Use it to "
            "reach a date the nightly window no longer covers; the enrichment passes and "
            "the settled watermark still run once at the end."
        ),
    ),
) -> None:
    """Backfill the broker-free population-monitor outcomes.

    The nightly VPS timer's entrypoint — it runs with NO ``--date`` so it needs
    no date arithmetic. It runs the population monitor over its OWN ~42-session
    lookback, a price-path replay over Polygon bars (no broker). The legacy
    shadow-return / execution-quality metrics were removed with the broker chain,
    and the per-decision ladder replay went with the click ledger (#465).
    Idempotent and resilient: per-ticker fetch failures skip + warn, and one bad
    ticker never aborts the sweep.

    ``--date`` names brief dates instead, one replay each. It exists because the
    sweep is a contiguous window back from today and cannot reach an older date
    without also recomputing everything in between.
    """
    if dates and lookback_days is not None:
        # They describe different date sets. Honouring one silently would make the
        # run's own report a claim about dates it did not touch.
        raise typer.BadParameter("--date and --lookback-days name different date sets; pass one.")
    # Population ladder monitor: the broker-free full-hold replay over EVERY brief
    # candidate. It uses its OWN ~42-session lookback (``MONITOR_LOOKBACK_DAYS``).
    # Never raises.
    _refresh_population_ladders(
        briefs_dir,
        lookback_days=lookback_days,
        dates=[d.date() for d in dates] if dates else None,
    )


def _replay_named_dates(
    replay_population_ladders: Any,
    briefs_dir: Path,
    dates: list[dt.date],
    *,
    deadline: Any,
) -> list[Any]:
    """Replay each operator-named date on its own and return the concatenated reports.

    Raises whatever the replay raises, so the caller's ``reports`` stays None on a
    failed replay (see ``_refresh_population_ladders``).
    """
    # One replay per named date: the monitor sweeps a contiguous window, so a
    # single call cannot express "these dates and nothing between them". Each
    # call also gets its own fetch budget, which keeps a date with many
    # brand-new names from starving the next one.
    # Accumulate into a LOCAL: ``reports`` stays None until the loop finishes, so a
    # replay that raises leaves it None and the guard counters below stay unsent.
    # An empty list reads as "a completed replay found nothing" and would publish
    # all-zero dispositions over a run that never looked anything up.
    named_reports: list[Any] = []
    empty: list[dt.date] = []
    for named in dates:
        one = replay_population_ladders(
            briefs_dir, end_date=named, lookback_days=0, deadline=deadline
        )
        if not one:
            # The monitor skips a date it has no brief parquet for, and a spent
            # deadline makes every later date come back empty too. Either way the
            # summary alone would read 0 across 0 dates for a date the operator
            # named, so name the ones that came back empty.
            empty.append(named)
            logger.warning(
                "population-monitor: %s produced no report — no brief parquet for it?",
                named.isoformat(),
            )
        named_reports.extend(one)
    if empty:
        typer.echo(
            "population-monitor: no report for "
            + ", ".join(d.isoformat() for d in empty)
            + " (no brief for the date, or the run's deadline was already spent)."
        )
    return named_reports


def _refresh_population_ladders(
    briefs_dir: Path,
    *,
    lookback_days: int | None = None,
    dates: list[dt.date] | None = None,
) -> None:
    """Run the broker-free POPULATION ladder monitor (PR-2). Never raises.

    Replays EVERY brief candidate's ladder to terminal over the monitor's OWN
    ~42-session lookback (``MONITOR_LOOKBACK_DAYS``), independent of the ladder
    replay's 14-day window. Folded into the nightly tail so it reuses the 06:30
    UTC timer (no new systemd unit / alert rule). Intentionally swallow-all: a
    replay / Polygon failure must NOT change the command's exit behaviour.

    Two ``_RunDeadline`` instances are constructed here from one wall-clock pool:
    the replay and the benchmark/sector/size passes share ``total - reserve``;
    the chart pass gets its own deadline at the full ``total`` so it can never be
    starved to zero by a grown upstream backlog.
    """
    # Two deadlines, one pool: the upstream passes (replay + benchmark/sector/size)
    # share ``total - reserve`` while the chart pass gets its own deadline carrying
    # the full ``total``. Both anchor at the same wall-clock start, so the chart
    # pass — last in the chain, starved to "enriched 0 rows" for five nights in
    # the 2026-07 incident — always inherits at least the reserve, and the whole
    # run still fits under the systemd TimeoutStartSec. If the import itself
    # fails, fall back to deadline=None so the enrichments still run without a
    # deadline rather than crashing the nightly timer.
    deadline: Any = None
    chart_deadline: Any = None
    reports: Any = None
    try:
        from alphalens_pipeline.feedback.population_ladder_monitor import (
            _CHART_RESERVE_S_DEFAULT,
            _FETCH_DEADLINE_S_DEFAULT,
            MONITOR_LOOKBACK_DAYS,
            LegacyArrivalStoreError,
            _RunDeadline,
            replay_population_ladders,
        )

        # total must be positive for any pass to run; setting the env override to
        # 0 disables ALL budgeted fetching including the chart pass (operator's
        # explicit choice, same as before the reserve split).
        total_s = float(
            os.environ.get("ALPHALENS_FEEDBACK_FETCH_DEADLINE_S", _FETCH_DEADLINE_S_DEFAULT)
        )
        reserve_s = float(
            os.environ.get("ALPHALENS_FEEDBACK_CHART_RESERVE_S", _CHART_RESERVE_S_DEFAULT)
        )
        deadline = _RunDeadline(max(total_s - reserve_s, 0.0))
        chart_deadline = _RunDeadline(total_s)
        if dates:
            reports = _replay_named_dates(
                replay_population_ladders, briefs_dir, dates, deadline=deadline
            )
        else:
            reports = replay_population_ladders(
                briefs_dir,
                lookback_days=MONITOR_LOOKBACK_DAYS if lookback_days is None else lookback_days,
                deadline=deadline,
            )
        terminal = sum(r.terminal for r in reports)
        ongoing = sum(r.ongoing for r in reports)
        typer.echo(
            f"population-monitor: {terminal} terminal, {ongoing} ongoing "
            f"across {len(reports)} brief dates."
        )
    except LegacyArrivalStoreError as exc:
        # Fail closed (#1416): no enrichment and no ingest watermark over a store
        # that must be rebuilt, so /edge keeps its last complete state and the
        # staleness alerts surface the pending rebuild.
        logger.error("population-monitor refused the store: %s", exc)  # NOSONAR
        return
    except Exception:
        logger.exception("population-monitor refresh failed; continuing")

    # Guard-disposition counters (#1090 memo §4). Emitted ONLY off a completed
    # replay: on a failed replay there are no counts to report, and emitting
    # invented zeros would CLEAR a firing sustained-lookup_failed alert without
    # any lookup having succeeded (the .prom file keeps last night's values —
    # standard textfile gauge semantics).
    if reports is not None:
        _emit_nightly_metrics(reports)

    if deadline is not None and deadline.stopped_reason:
        logger.warning(
            "population-monitor: stopped fetching early (%s); remaining work deferred to next run.",
            deadline.stopped_reason,
        )

    # All enrichments operate on the EXISTING store parquets (independent of the
    # fresh replay above), so they run even when the live replay failed. The
    # upstream trio shares the reduced deadline; the chart pass runs on its own
    # full-total deadline so the reserve withheld above is guaranteed to it.
    _enrich_population_benchmark_excess(deadline=deadline)
    _enrich_population_event_car(deadline=deadline)
    _enrich_population_sector_excess(deadline=deadline)
    _enrich_population_size_fields(briefs_dir, deadline=deadline)
    _enrich_selection_labels(briefs_dir, deadline=deadline)
    _enrich_population_chart_payloads(briefs_dir, deadline=chart_deadline)

    # Final step: stamp the store as settled so the mirror ingests the COMPLETE
    # multi-pass state (never a mid-run half-state). Every pass above is swallow-all
    # (replay is inside a try/except; each _enrich_* "never raises"), so this
    # advances on EVERY run that reaches here — i.e. every run that is NOT
    # process-killed (SIGTERM at TimeoutStartSec / OOM). A killed run leaves its
    # half-rewritten parquets at mtime > the PREVIOUS watermark, so the mirror
    # skips them and /edge holds the last complete state. Columns from an
    # internally-failed-but-completed pass are honestly degraded by that pass's own
    # guard (benchmark -> NULL/#847 pending; chart -> last-good), NOT withheld.
    _write_ingest_watermark(_ALPHALENS_HOME / "population_ladders")


def _enrich_population_event_car(*, deadline: Any = None) -> None:
    """Stamp the event-lane outcome (car_20_event / car_40_event) on event rows. Never raises.

    Disk-first over the monitor's grouped-daily cache, so in steady state it
    costs no vendor call; runs right after benchmark-excess so a starved night
    cannot skip it behind the size / chart passes (epic #1293, #1297).
    """
    try:
        from alphalens_pipeline.feedback.event_car import enrich_store_with_event_car

        n = enrich_store_with_event_car(_ALPHALENS_HOME / "population_ladders", deadline=deadline)
        typer.echo(f"event-car: {n} event row(s) carry a matured car_20_event.")
    except Exception:
        logger.exception("event-car enrichment failed; continuing")


def _enrich_selection_labels(briefs_dir: Path, *, deadline: Any = None) -> None:
    """Stamp the ML selection label (label registry memo section 5.1). Never raises.

    Writes its own store (``~/.alphalens/selection_labels``), which nothing on the wire
    reads, so it does not touch the population store or the ingest watermark. Disk-only
    over the split-adjusted grouped-daily history. The echo carries status counts only:
    a label value printed in a journal would be an unregistered look.
    """
    try:
        from alphalens_pipeline.feedback.selection_label import enrich_selection_labels

        report = enrich_selection_labels(
            briefs_dir=briefs_dir,
            shadow_dir=_ALPHALENS_HOME / "thematic_candidates" / "proposal_shadow",
            labels_dir=_ALPHALENS_HOME / "selection_labels",
            grouped_root=_ALPHALENS_HOME / "grouped_daily_history",
            deadline=deadline,
        )
        counts = ", ".join(f"{k}={v}" for k, v in sorted(report.status_counts_h20.items()))
        typer.echo(
            f"selection-labels: {report.dates_written} date(s) written, "
            f"{report.rows_stamped} row(s) stamped, {report.dates_failed} failed; "
            f"h20 status of stamped rows: {counts or 'none'}."
        )
    except Exception:
        logger.exception("selection-label enrichment failed; continuing")


def _emit_nightly_metrics(reports: Any) -> None:
    """Emit the nightly run's guard dispositions and its completeness. Never raises.

    One ``alphalens_feedback_guard_total{disposition=...}`` series per arm of
    the Amendment-1 tree, summed across the run's per-brief-date reports, plus
    four series that say how much of the sweep actually finished. The job exits
    0 and stamps the store settled even when its fetch budget ran out mid-window
    (2026-09-19: 102 refusals, 19 rows left with no price path, no alert), so
    ``alphalens_feedback_unpriced_rows`` is the outcome the alert reads and the
    two ``deferred_total`` reasons say which ceiling bound.
    ``alphalens_feedback_oldest_deferred_sessions`` is a MAX, not a sum: each report
    already holds a per-date maximum, and adding maxima invents an age no row has.
    It is also the only one of the four that sees a row which HAS a price path and
    merely failed to advance — ``unpriced_rows`` counts total absence, not staleness.
    EVERY label is emitted on EVERY successful run, zeros included — a series
    that disappears on healthy nights is indistinguishable from a stopped
    exporter, and the sustained-lookup_failed alert needs a clean run's 0 to
    clear. Per-run GAUGE like every textfile metric (the ``_total`` suffix
    follows the existing edgar/thematic naming, not Prometheus counter
    semantics) — alert rules use ``min_over_time``/``max_over_time``, never
    ``increase()``/``rate()``.

    This is the job's ONLY ``emit_domain_metrics`` call — a second call with
    ``job="feedback-shadow-returns"`` would overwrite the .prom file and
    silently delete these series. Swallow-all (PR #311 rule): the store
    parquets are already written, so an emit failure is observability debt,
    not a job failure.
    """
    try:
        # Lazy: corporate_actions imports pandas; the constants are only
        # needed after a replay already paid that import.
        from alphalens_pipeline.feedback.corporate_actions import (
            DISPOSITION_DATA_QUALITY,
            DISPOSITION_EXTREME_VALIDATED,
            DISPOSITION_LOOKUP_FAILED,
            DISPOSITION_SPLIT_INVALIDATED,
        )

        dispositions = (
            DISPOSITION_SPLIT_INVALIDATED,
            DISPOSITION_LOOKUP_FAILED,
            DISPOSITION_EXTREME_VALIDATED,
            DISPOSITION_DATA_QUALITY,
        )
        metrics = {
            f'alphalens_feedback_guard_total{{disposition="{disposition}"}}': sum(
                getattr(report, f"guard_{disposition}", 0) for report in reports
            )
            for disposition in dispositions
        }
        metrics['alphalens_feedback_deferred_total{reason="fetch_budget"}'] = sum(
            getattr(report, "fetch_budget_refused", 0) for report in reports
        )
        metrics['alphalens_feedback_deferred_total{reason="deadline"}'] = sum(
            getattr(report, "stopped_for_deadline", 0) for report in reports
        )
        metrics["alphalens_feedback_unpriced_rows"] = sum(
            getattr(report, "unpriced_rows", 0) for report in reports
        )
        metrics["alphalens_feedback_oldest_deferred_sessions"] = max(
            (getattr(report, "oldest_deferred_touch_age", 0) for report in reports),
            default=0,
        )
        emit_domain_metrics(job="feedback-shadow-returns", metrics=metrics)
    except Exception:
        logger.exception("nightly metric emit failed; continuing")


def _enrich_population_size_fields(briefs_dir: Path, *, deadline: Any = None) -> None:
    """Backfill the size overlay on terminal rows frozen before PR #431. Never raises.

    The monitor freezes terminal rows, so a row that resolved before the
    size-overlay feature keeps its 10 size columns NULL forever (the edge
    dashboard "% book" column is empty for those matured trades). This recomputes
    them deterministically from the brief + the stored replay outcome, never
    touching the frozen verdict. Idempotent + self-healing. Swallow-all like the
    rest of the nightly tail.
    """
    try:
        from alphalens_pipeline.feedback.population_ladder_monitor import (
            enrich_store_with_size_fields,
        )

        n = enrich_store_with_size_fields(
            _ALPHALENS_HOME / "population_ladders", briefs_dir, deadline=deadline
        )
        typer.echo(f"size-enrichment: backfilled size fields on {n} terminal rows.")
    except Exception:
        logger.exception("size-field enrichment failed; continuing")


def _enrich_population_chart_payloads(briefs_dir: Path, *, deadline: Any = None) -> None:
    """Add the ladder-chart payload column to the population-ladder store. Never raises.

    Builds the pre-computed chart payload (daily OHLC candles + entry/TP/stop price
    lines + modeled fill/exit markers) per row and writes it as the
    ``chart_payload_json`` column, mirroring the benchmark-excess + size
    enrichments. This MUST run HERE in the pipeline (Polygon-cached bars +
    calendar); the slim Django ingest only READS the column and the
    ``/v1/edge/chart`` endpoint only serves it. Swallow-all like the rest of the
    nightly tail.
    """
    try:
        from alphalens_pipeline.feedback.ladder_chart import (
            enrich_store_with_chart_payloads,
        )

        n = enrich_store_with_chart_payloads(
            _ALPHALENS_HOME / "population_ladders", briefs_dir, deadline=deadline
        )
        typer.echo(f"chart-payload: enriched {n} rows with a chart payload.")
    except Exception:
        logger.exception("chart-payload enrichment failed; continuing")


def _enrich_population_benchmark_excess(*, deadline: Any = None) -> None:
    """Add benchmark-excess columns to the population-ladder store. Never raises.

    Computes ``benchmark_window_return`` + ``market_excess_return`` per row
    (market index over the SAME arrival→exit window as ``forward_return``) and
    rewrites the store parquets. This is the EDGE dashboard's benchmark-relative
    headline (memo §3.1) and must run HERE in the pipeline (Polygon + calendar);
    the slim Django ingest only READS the columns. Swallow-all like the rest of
    the nightly tail.
    """
    try:
        from alphalens_pipeline.feedback.benchmark_excess import (
            enrich_store_with_benchmark_excess,
        )

        n = enrich_store_with_benchmark_excess(
            _ALPHALENS_HOME / "population_ladders", deadline=deadline
        )
        typer.echo(f"benchmark-excess: enriched {n} rows with market-excess return.")
    except Exception:
        logger.exception("benchmark-excess enrichment failed; continuing")


def _enrich_population_sector_excess(*, deadline: Any = None) -> None:
    """Add sector-relative EDGE-outcome columns to the store. Never raises.

    Computes ``sector_etf_window_return`` + ``sector_excess_return`` per row
    (the candidate's OWN SPDR sector ETF over the SAME arrival→exit window as
    ``forward_return``), so the outcome benchmark is a different series from the
    SPY-derived market_state label — breaking the SPY-on-SPY confound (memo §4.2,
    D4 resolution). Runs HERE in the pipeline (Polygon + calendar + SIC index);
    the slim Django ingest only READS the columns. Swallow-all like the rest of
    the nightly tail, and shares the same run deadline as benchmark-excess.
    """
    try:
        from alphalens_pipeline.feedback.sector_excess import (
            enrich_store_with_sector_excess,
        )

        n = enrich_store_with_sector_excess(
            _ALPHALENS_HOME / "population_ladders", deadline=deadline
        )
        typer.echo(f"sector-excess: enriched {n} rows with sector-relative return.")
    except Exception:
        logger.exception("sector-excess enrichment failed; continuing")


@feedback_app.command(name="drop-decisions-table")
def drop_decisions_table_command(
    feedback_db: Path = typer.Option(
        _ALPHALENS_HOME / "feedback.db",
        "--feedback-db",
        help="Path to the legacy feedback.db whose dead `decisions` table to drop.",
    ),
) -> None:
    """One-shot operator teardown: drop the dead Track-A `decisions` table.

    The user-action click ledger was removed (#465) and the per-decision store
    subsystem was deleted, so nothing opens `feedback.db` at runtime any more —
    a legacy host file just keeps dead historical decision rows around. This
    drops that table (+ its indexes) so the orphaned file is clean. Idempotent
    and safe to run zero/one/many times; it ONLY touches `feedback.db` and never
    the population-ladder parquets (the live market-behavior feedback).
    """
    from alphalens_feedback import migrate

    dropped = migrate.drop_decisions_table(feedback_db)
    if dropped:
        typer.echo(f"feedback teardown: dropped dead `decisions` table from {feedback_db}.")
    else:
        typer.echo(f"feedback teardown: {feedback_db} does not exist — nothing to drop.")
