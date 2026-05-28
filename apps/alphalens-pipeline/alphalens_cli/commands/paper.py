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

# Shared help text for the --ledger flag across plan / submit / reconcile.
# Sonar flagged the duplicated literal (S1192); extracting keeps the three
# command signatures in lock-step on a single source of truth.
_LEDGER_HELP = "Override the default paper ledger location (~/.alphalens/paper_ledger.db)."


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
        help=_LEDGER_HELP,
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
    use_test_account: bool = typer.Option(
        False,
        "--use-test-account",
        help=(
            "Plan against the ALPACA_TEST_* account (dev sandbox). Pulls equity "
            "from the test client + tags every plans row with account='test'."
        ),
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

    profile = "test" if use_test_account else "main"
    alpaca_client = None
    if not no_alpaca:
        # Lazy-import the client so --no-alpaca + a fresh checkout without an
        # ALPACA_API_KEY can still dry-run the planner end-to-end.
        from alphalens_pipeline.data.alt_data.alpaca_client import (
            get_default_alpaca_client,
        )

        alpaca_client = get_default_alpaca_client(profile=profile)

    report = plan_for_date(
        brief_date=brief_date,
        briefs_dir=resolved_briefs,
        ledger_path=resolved_ledger,
        alpaca_client=alpaca_client,
        force=force,
        account=profile,
    )

    typer.echo(
        f"paper plan {report.brief_date.isoformat()} (account={profile}): "
        f"equity=${report.paper_equity:,.0f} "
        f"planned={report.n_planned} shadowed={report.n_shadowed} "
        f"gross=${report.total_gross_notional:,.0f}"
    )
    for outcome in report.outcomes:
        marker = "✓" if outcome.status == "PLANNED" else "·"
        suffix = f"  [{outcome.reason}]" if outcome.reason else ""
        typer.echo(f"  {marker} {outcome.ticker:<6s} {outcome.theme}{suffix}")


@paper_app.command("submit")
def submit(
    date: str = typer.Option(
        ...,
        "--date",
        help="ISO date (YYYY-MM-DD) of the brief whose PLANNED rows to submit.",
    ),
    ledger_path: Path | None = typer.Option(
        None,
        "--ledger",
        help=_LEDGER_HELP,
    ),
    use_test_account: bool = typer.Option(
        False,
        "--use-test-account",
        help=(
            "Route orders to the ALPACA_TEST_* account (dev sandbox) instead of "
            "the main paper account. For PR 3 live smoke testing."
        ),
    ),
) -> None:
    """Submit entry-tier limit orders to Alpaca paper for every PLANNED
    candidate on ``brief_date`` that hasn't been submitted yet.

    Idempotent at (plan_id, tier_index) — re-running after a partial
    submit (mid-batch crash, network blip) only pushes the tiers that
    don't already have an ENTRY row in ``orders``.
    """
    from alphalens_pipeline.data.alt_data.alpaca_client import (
        get_default_alpaca_client,
    )
    from alphalens_pipeline.paper.constants import DEFAULT_LEDGER_RELPATH
    from alphalens_pipeline.paper.submitter import submit_for_date

    brief_date = dt.date.fromisoformat(date)
    resolved_ledger = (
        ledger_path if ledger_path is not None else Path.home() / DEFAULT_LEDGER_RELPATH
    )

    profile = "test" if use_test_account else "main"
    alpaca_client = get_default_alpaca_client(profile=profile)

    report = submit_for_date(
        brief_date=brief_date,
        ledger_path=resolved_ledger,
        alpaca_client=alpaca_client,
        account=profile,
    )

    typer.echo(
        f"paper submit {report.brief_date.isoformat()} "
        f"(profile={profile}): plans={report.n_plans_processed} "
        f"orders_submitted={report.n_orders_submitted}"
    )
    for outcome in report.outcomes:
        suffix_parts: list[str] = []
        if outcome.n_tiers_skipped_existing > 0:
            suffix_parts.append(f"skipped-existing={outcome.n_tiers_skipped_existing}")
        if outcome.n_tiers_skipped_zero_qty > 0:
            suffix_parts.append(f"zero-qty={outcome.n_tiers_skipped_zero_qty}")
        suffix = f"  [{', '.join(suffix_parts)}]" if suffix_parts else ""
        typer.echo(f"  → {outcome.ticker:<6s} submitted={outcome.n_tiers_submitted}{suffix}")


@paper_app.command("reconcile")
def reconcile(
    ledger_path: Path | None = typer.Option(
        None,
        "--ledger",
        help=_LEDGER_HELP,
    ),
    use_test_account: bool = typer.Option(
        False,
        "--use-test-account",
        help="Route through ALPACA_TEST_* account (dev sandbox).",
    ),
) -> None:
    """Reconcile every open ledger order against Alpaca paper.

    For each ledger order in SUBMITTED / PARTIALLY_FILLED:
      - GET the Alpaca order by id
      - Transition local status (FILLED / CANCELED / REJECTED / …)
      - Append a fill row if Alpaca reports new filled_qty

    Idempotent: re-running on identical Alpaca state appends no fills.
    """
    from alphalens_pipeline.data.alt_data.alpaca_client import (
        get_default_alpaca_client,
    )
    from alphalens_pipeline.paper.constants import DEFAULT_LEDGER_RELPATH
    from alphalens_pipeline.paper.reconciler import reconcile_orders

    resolved_ledger = (
        ledger_path if ledger_path is not None else Path.home() / DEFAULT_LEDGER_RELPATH
    )
    profile = "test" if use_test_account else "main"
    alpaca_client = get_default_alpaca_client(profile=profile)

    report = reconcile_orders(
        ledger_path=resolved_ledger,
        alpaca_client=alpaca_client,
        account=profile,
    )

    typer.echo(
        f"paper reconcile (profile={profile}): "
        f"checked={report.n_orders_checked} "
        f"transitioned={report.n_orders_transitioned} "
        f"fills+={report.n_fills_appended}"
    )
    for outcome in report.outcomes:
        if outcome.new_status == outcome.prev_status and outcome.n_new_fills == 0:
            continue
        suffix_parts = []
        if outcome.new_status != outcome.prev_status:
            suffix_parts.append(f"{outcome.prev_status}->{outcome.new_status}")
        if outcome.n_new_fills > 0:
            suffix_parts.append(f"+{outcome.n_new_fills} fills")
        typer.echo(f"  · {outcome.alpaca_order_id[:12]}  {' '.join(suffix_parts)}")


__all__ = ["paper_app"]
