"""CLI: ``alphalens paper`` subcommands for the paper-trade harness.

Phase A ships ``plan`` only. ``submit`` / ``reconcile`` / ``report`` land in
PR 3 + PR 4. Lazy imports inside command bodies keep the ``alphalens``
CLI startup time low (a Layer-1 ``edgar-detect`` cron tick must not pay for
the alpaca-py + pandas imports the paper subtree needs).
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import typer

logger = logging.getLogger(__name__)

paper_app = typer.Typer(
    name="paper",
    help="Paper-trade forward-observation harness (see PR #273 design memo).",
    no_args_is_help=True,
)


@paper_app.command("plan")
def plan(
    date: str = typer.Option(
        ...,
        "--date",
        help="ISO date (YYYY-MM-DD) of the brief parquet to plan against.",
    ),
    briefs_dir: Path | None = typer.Option(
        None,
        "--briefs-dir",
        help=("Override the default thematic brief directory (~/.alphalens/thematic_briefs)."),
    ),
    ledger_path: Path | None = typer.Option(
        None,
        "--ledger",
        help="Override the default paper ledger location (~/.alphalens/paper_ledger.db).",
    ),
    no_alpaca: bool = typer.Option(
        False,
        "--no-alpaca",
        help=(
            "Skip the Alpaca client (offline planning). Equity defaults to "
            "$1M; same-ticker dedup is disabled. For dry-runs + tests."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Delete existing plans + shadow_log rows for this brief_date first.",
    ),
) -> None:
    """Plan one day's verified candidates and persist to the SQLite ledger.

    Reads ``brief_date.parquet`` from ``briefs_dir``, computes the locked
    sizing math (per docs/research/paper_trading_capital_sizing_2026_05_28.md),
    and writes either a PLANNED row to the ledger or a shadow-log entry for
    candidates that are skipped (not verified, no trade-setup, same-ticker
    already open) or blocked (gross safety cap).
    """
    from alphalens_pipeline.paper.constants import (
        DEFAULT_BRIEFS_RELPATH,
        DEFAULT_LEDGER_RELPATH,
    )
    from alphalens_pipeline.paper.planner import plan_for_date

    brief_date = dt.date.fromisoformat(date)
    home = Path.home()
    resolved_briefs = briefs_dir if briefs_dir is not None else home / DEFAULT_BRIEFS_RELPATH
    resolved_ledger = ledger_path if ledger_path is not None else home / DEFAULT_LEDGER_RELPATH

    alpaca_client = None
    if not no_alpaca:
        # Lazy-import the client so --no-alpaca + a fresh checkout without an
        # ALPACA_API_KEY can still dry-run the planner end-to-end.
        from alphalens_pipeline.data.alt_data.alpaca_client import (
            get_default_alpaca_client,
        )

        alpaca_client = get_default_alpaca_client()

    report = plan_for_date(
        brief_date=brief_date,
        briefs_dir=resolved_briefs,
        ledger_path=resolved_ledger,
        alpaca_client=alpaca_client,
        force=force,
    )

    typer.echo(
        f"paper plan {report.brief_date.isoformat()}: "
        f"equity=${report.paper_equity:,.0f} "
        f"planned={report.n_planned} shadowed={report.n_shadowed} "
        f"gross=${report.total_gross_notional:,.0f}"
    )
    for outcome in report.outcomes:
        marker = "✓" if outcome.status == "PLANNED" else "·"
        suffix = f"  [{outcome.reason}]" if outcome.reason else ""
        typer.echo(f"  {marker} {outcome.ticker:<6s} {outcome.theme}{suffix}")


__all__ = ["paper_app"]
