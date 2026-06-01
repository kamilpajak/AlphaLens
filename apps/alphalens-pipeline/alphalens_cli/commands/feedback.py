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


@feedback_app.command(name="report")
def report_command(
    ledger: Path = typer.Option(
        Path.home() / ".alphalens" / "feedback.db",
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

    from alphalens_pipeline.feedback.store import FeedbackStore

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


@feedback_app.command(name="join-outcomes")
def join_outcomes_command(
    date: str = typer.Option(
        None,
        "--date",
        help="Brief date YYYY-MM-DD to join (default: today UTC).",
    ),
    account: str = typer.Option(
        "test",
        "--account",
        help="Alpaca paper account the live chain runs on ('test').",
    ),
    ledger: Path = typer.Option(
        Path.home() / ".alphalens" / "feedback.db",
        "--ledger",
        help="Override the default feedback ledger location.",
    ),
    paper_ledger: Path = typer.Option(
        Path.home() / ".alphalens" / "paper_ledger.db",
        "--paper-ledger",
        help="Override the default paper-trade ledger location.",
    ),
) -> None:
    """Stamp paper-trade outcomes onto decisions for a brief date (Track A v2).

    Links each decision to its paper plan outcome by (brief_date, ticker,
    account) and stamps fill_status / exit_kind / outcome_plan_id. The paper
    harness is decoupled from clicks, so a decision with no matching plan (or
    a plan that has not closed) is left with NULL outcomes — that is normal.
    Idempotent: safe to re-run from cron as outcomes mature.
    """
    import datetime as dt

    from alphalens_pipeline.feedback.outcome_join import join_decision_outcomes

    brief_date = dt.date.fromisoformat(date) if date else dt.datetime.now(dt.UTC).date()
    report = join_decision_outcomes(ledger, paper_ledger, brief_date=brief_date, account=account)
    typer.echo(
        f"outcome-join {brief_date} account={account}: "
        f"{report.n_matched}/{report.n_decisions} decisions stamped "
        f"({report.n_plans} plans, {report.n_unmatched} left NULL)."
    )
