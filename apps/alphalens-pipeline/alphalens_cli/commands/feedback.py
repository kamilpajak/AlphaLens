"""CLI: ``alphalens feedback`` subcommands for the feedback ledger.

v1 ships ``report`` only — operator-facing summary for monitoring the
ledger between sessions. v2 will surface this in the SPA weekly review
route; until then this CLI keeps the operator informed about action
distribution, dismiss-reason histogram, and the "other %" guardrail
called out in the locked design memo (>15% other = taxonomy gap).

Per zen pre-merge finding #7. Lazy imports inside the command body keep
the ``alphalens`` CLI startup time low (Layer-1 ``edgar-detect`` cron
ticks must not pay for pandas / sqlite import cost we don't need on
that path).
"""

from __future__ import annotations

import logging
from pathlib import Path

import typer

logger = logging.getLogger(__name__)

feedback_app = typer.Typer(
    name="feedback",
    help="Feedback ledger operator tools (see PR #292 design memo).",
    no_args_is_help=True,
)

# Threshold from the design memo §2.1: above this fraction of dismiss
# events tagged `other`, taxonomy needs a re-think (likely a missing
# enum candidate). Kept as a module constant so the test suite + memo
# stay in sync via reference.
_OTHER_WARN_THRESHOLD = 0.15

# Duplicates ``bar_window.DEFAULT_LOOKBACK_DAYS`` because typer.Option
# evaluates its default at import time and this CLI lazy-imports the feedback
# module inside command bodies (keeps the pipeline → research direction clean +
# the CLI startup cheap). Parity pinned by
# ``test_cli_lookback_default_in_sync_with_module``.
_DEFAULT_LOOKBACK_DAYS = 14

# Per-user runtime data root (``~/.alphalens``). Holds the feedback ledger and
# the daily thematic brief parquets the broker-free replay reads.
_ALPHALENS_HOME = Path.home() / ".alphalens"


@feedback_app.command(name="report")
def report_command(
    ledger: Path = typer.Option(
        _ALPHALENS_HOME / "feedback.db",
        "--ledger",
        help="Override the default feedback ledger location.",
    ),
) -> None:
    """Print action distribution + dismiss histogram + 'other %' guardrail.

    Read-only — never writes to the ledger. Safe to invoke from cron or
    from a session inside the prod Docker stack via the same SQLite
    file mounted by the Django app.
    """
    from collections import Counter

    from alphalens_feedback.store import FeedbackStore

    if not ledger.exists():
        typer.echo(f"no ledger at {ledger} — nothing to report yet.")
        raise typer.Exit(code=0)

    with FeedbackStore.open(ledger) as fb:
        rows = list(fb.conn.execute("SELECT action, dismiss_reason FROM decisions"))

    if not rows:
        typer.echo(f"ledger at {ledger} is empty.")
        raise typer.Exit(code=0)

    total = len(rows)
    actions = Counter(r["action"] for r in rows)
    dismiss_reasons = Counter(
        r["dismiss_reason"] for r in rows if r["action"] == "dismissed" and r["dismiss_reason"]
    )
    n_dismissed = sum(dismiss_reasons.values())
    other_pct = (dismiss_reasons.get("other", 0) / n_dismissed) if n_dismissed else 0.0

    typer.echo(f"feedback report (ledger={ledger})")
    typer.echo(f"  total decisions: {total}")
    typer.echo("  actions:")
    for action, count in actions.most_common():
        typer.echo(f"    {action:<14} {count:>5}  ({count / total:.1%})")
    if n_dismissed:
        typer.echo(f"  dismiss reasons ({n_dismissed} dismissed total):")
        for reason, count in dismiss_reasons.most_common():
            typer.echo(f"    {reason:<24} {count:>5}  ({count / n_dismissed:.1%})")
        if other_pct > _OTHER_WARN_THRESHOLD:
            typer.echo(
                f"  ⚠ other usage = {other_pct:.1%} (>{_OTHER_WARN_THRESHOLD:.0%}) "
                "— taxonomy may have a gap; review free-text notes."
            )


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


def _fmt(value: float | None) -> str:
    """Format a possibly-None decimal-fraction statistic for the report."""
    return "n/a" if value is None else f"{value:+.4f}"


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
