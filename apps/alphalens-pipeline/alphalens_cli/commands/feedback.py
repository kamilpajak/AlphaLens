"""CLI: ``alphalens feedback`` subcommands for the broker-free feedback replay.

Ships ``backfill-shadow-returns`` only — the nightly VPS timer entrypoint that
drives the broker-free ladder + population-monitor replay engines (market-
behavior feedback). The Track-A user-action report histogram was removed with
the click ledger.

Lazy imports inside the command body keep the ``alphalens`` CLI startup time low
(Layer-1 ``edgar-detect`` cron ticks must not pay for pandas / sqlite import
cost we don't need on that path).
"""

from __future__ import annotations

import logging
from pathlib import Path

import typer

logger = logging.getLogger(__name__)

feedback_app = typer.Typer(
    name="feedback",
    help="Broker-free feedback replay operator tools.",
    no_args_is_help=True,
)

# Duplicates ``bar_window.DEFAULT_LOOKBACK_DAYS`` because typer.Option
# evaluates its default at import time and this CLI lazy-imports the feedback
# module inside command bodies (keeps the pipeline → research direction clean +
# the CLI startup cheap). Parity pinned by
# ``test_cli_lookback_default_in_sync_with_module``.
_DEFAULT_LOOKBACK_DAYS = 14

# Per-user runtime data root (``~/.alphalens``). Holds the feedback ledger and
# the daily thematic brief parquets the broker-free replay reads.
_ALPHALENS_HOME = Path.home() / ".alphalens"


# NOTE: the command name ``backfill-shadow-returns`` is retained for the existing
# systemd unit ``alphalens-feedback-shadow-returns.service`` (renaming would force
# VPS-survivor churn). The legacy shadow-return / execution-quality metrics were
# removed with the broker chain; this command now drives only the broker-free
# replay engines. A rename is a deferred follow-up.
@feedback_app.command(name="backfill-shadow-returns")
def backfill_shadow_returns_command(
    lookback_days: int = typer.Option(
        _DEFAULT_LOOKBACK_DAYS,
        "--lookback-days",
        help=(
            "Calendar days to sweep back from today. The window is inclusive at "
            "both ends, so N yields N+1 dates (default 14 → 15 dates)."
        ),
    ),
    ledger: Path = typer.Option(
        _ALPHALENS_HOME / "feedback.db",
        "--ledger",
        help="Override the default feedback ledger location.",
    ),
    briefs_dir: Path = typer.Option(
        _ALPHALENS_HOME / "thematic_briefs",
        "--briefs-dir",
        help="Directory of daily thematic brief parquets (for the broker-free ladder replay).",
    ),
) -> None:
    """Backfill the broker-free ladder + population-monitor outcomes.

    The nightly VPS timer's entrypoint — it runs with NO ``--date`` so it needs
    no date arithmetic. It first replays each matured feedback decision's ladder
    over the ``--lookback-days`` window, then runs the population monitor over its
    OWN much-larger lookback. Both are price-path replays over Polygon bars (no
    broker). The legacy shadow-return / execution-quality metrics were removed
    with the broker chain. Idempotent and resilient: per-ticker fetch failures
    skip + warn, and one bad ticker never aborts the sweep.
    """
    # Broker-free ladder replay over the maturity window.
    _refresh_ladder_outcomes(ledger, briefs_dir, lookback_days=lookback_days)
    # Population ladder monitor: the broker-free full-hold replay over EVERY brief
    # candidate, NOT just clicked decisions. It uses its OWN, much larger lookback
    # (``MONITOR_LOOKBACK_DAYS`` ≈ the 42-session hold), NOT the command's
    # ``--lookback-days``. Folded here so it reuses the 06:30 UTC timer (no new
    # systemd unit). Never raises.
    _refresh_population_ladders(briefs_dir)


def _refresh_ladder_outcomes(ledger: Path, briefs_dir: Path, *, lookback_days: int) -> None:
    """Run the broker-free ladder replay over the maturity window. Never raises.

    Folded into the nightly ``backfill-shadow-returns`` tail so it reuses the
    06:30 UTC timer (no new systemd unit / alert rule). Intentionally swallow-all:
    a replay or Polygon failure must NOT change the command's exit behaviour or
    shadow the population-monitor refresh that follows.
    """
    try:
        from alphalens_pipeline.feedback.ladder_backfill import replay_ladder_decisions_window

        reports = replay_ladder_decisions_window(ledger, briefs_dir, lookback_days=lookback_days)
        stamped = sum(r.stamped for r in reports)
        matured = sum(1 for r in reports if r.matured)
        typer.echo(f"ladder-replay: {stamped} decisions stamped across {matured} matured dates.")
    except Exception:
        logger.exception("ladder-replay refresh failed; continuing")


def _refresh_population_ladders(briefs_dir: Path) -> None:
    """Run the broker-free POPULATION ladder monitor (PR-2). Never raises.

    Replays EVERY brief candidate's ladder to terminal over the monitor's OWN
    ~42-session lookback (``MONITOR_LOOKBACK_DAYS``), independent of the ladder
    replay's 14-day window. Folded into the nightly tail so it reuses the 06:30
    UTC timer (no new systemd unit / alert rule). Intentionally swallow-all: a
    replay / Polygon failure must NOT change the command's exit behaviour.
    """
    try:
        from alphalens_pipeline.feedback.population_ladder_monitor import (
            MONITOR_LOOKBACK_DAYS,
            replay_population_ladders,
        )

        reports = replay_population_ladders(briefs_dir, lookback_days=MONITOR_LOOKBACK_DAYS)
        terminal = sum(r.terminal for r in reports)
        ongoing = sum(r.ongoing for r in reports)
        typer.echo(
            f"population-monitor: {terminal} terminal, {ongoing} ongoing "
            f"across {len(reports)} brief dates."
        )
    except Exception:
        logger.exception("population-monitor refresh failed; continuing")

    # Both enrichments operate on the EXISTING store parquets (independent of the
    # fresh replay above), so they run even when the live replay failed.
    _enrich_population_benchmark_excess()
    _enrich_population_size_fields(briefs_dir)


def _enrich_population_size_fields(briefs_dir: Path) -> None:
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

        n = enrich_store_with_size_fields(_ALPHALENS_HOME / "population_ladders", briefs_dir)
        typer.echo(f"size-enrichment: backfilled size fields on {n} terminal rows.")
    except Exception:
        logger.exception("size-field enrichment failed; continuing")


def _enrich_population_benchmark_excess() -> None:
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

        n = enrich_store_with_benchmark_excess(_ALPHALENS_HOME / "population_ladders")
        typer.echo(f"benchmark-excess: enriched {n} rows with market-excess return.")
    except Exception:
        logger.exception("benchmark-excess enrichment failed; continuing")
