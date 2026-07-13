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

import logging
from pathlib import Path
from typing import Any

import typer

logger = logging.getLogger(__name__)

feedback_app = typer.Typer(
    name="feedback",
    help="Broker-free feedback replay operator tools.",
    no_args_is_help=True,
)

# Per-user runtime data root (``~/.alphalens``). Holds the daily thematic brief
# parquets the broker-free population replay reads.
_ALPHALENS_HOME = Path.home() / ".alphalens"


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
) -> None:
    """Backfill the broker-free population-monitor outcomes.

    The nightly VPS timer's entrypoint — it runs with NO ``--date`` so it needs
    no date arithmetic. It runs the population monitor over its OWN ~42-session
    lookback, a price-path replay over Polygon bars (no broker). The legacy
    shadow-return / execution-quality metrics were removed with the broker chain,
    and the per-decision ladder replay went with the click ledger (#465).
    Idempotent and resilient: per-ticker fetch failures skip + warn, and one bad
    ticker never aborts the sweep.
    """
    # Population ladder monitor: the broker-free full-hold replay over EVERY brief
    # candidate. It uses its OWN ~42-session lookback (``MONITOR_LOOKBACK_DAYS``).
    # Never raises.
    _refresh_population_ladders(briefs_dir)


def _refresh_population_ladders(briefs_dir: Path) -> None:
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
    import os

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
    try:
        from alphalens_pipeline.feedback.population_ladder_monitor import (
            _CHART_RESERVE_S_DEFAULT,
            _FETCH_DEADLINE_S_DEFAULT,
            MONITOR_LOOKBACK_DAYS,
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
        reports = replay_population_ladders(
            briefs_dir, lookback_days=MONITOR_LOOKBACK_DAYS, deadline=deadline
        )
        terminal = sum(r.terminal for r in reports)
        ongoing = sum(r.ongoing for r in reports)
        typer.echo(
            f"population-monitor: {terminal} terminal, {ongoing} ongoing "
            f"across {len(reports)} brief dates."
        )
    except Exception:
        logger.exception("population-monitor refresh failed; continuing")

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
    _enrich_population_sector_excess(deadline=deadline)
    _enrich_population_size_fields(briefs_dir, deadline=deadline)
    _enrich_population_chart_payloads(briefs_dir, deadline=chart_deadline)


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
