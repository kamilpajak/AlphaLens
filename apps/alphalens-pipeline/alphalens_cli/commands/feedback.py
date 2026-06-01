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

# Duplicates ``shadow_return.DEFAULT_LOOKBACK_DAYS`` because typer.Option
# evaluates its default at import time and this CLI lazy-imports the feedback
# module inside command bodies (keeps the pipeline → research direction clean +
# the CLI startup cheap). Parity pinned by
# ``test_cli_lookback_default_in_sync_with_module``.
_DEFAULT_LOOKBACK_DAYS = 14


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


@feedback_app.command(name="compute-shadow-returns")
def compute_shadow_returns_command(
    date: str = typer.Option(
        None,
        "--date",
        help="Brief date YYYY-MM-DD to price (default: today UTC).",
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
    """Stamp shadow_return + realized_return onto decisions (Track A v2 PR-3).

    Pulls Polygon minute bars for the arrival + horizon opening windows and
    stamps the arrival-price counterfactual. A SEPARATE pass from join-outcomes
    (kept apart because Polygon is rate-limited and the horizon must have
    matured) — schedule it nightly, after the holding horizon has closed. The
    run is skipped with a loud warning if the horizon is not yet in the past.
    Per-ticker fetch failures skip + warn; one bad ticker never aborts the run.
    """
    import datetime as dt

    from alphalens_pipeline.feedback.shadow_return import compute_shadow_returns

    brief_date = dt.date.fromisoformat(date) if date else dt.datetime.now(dt.UTC).date()
    report = compute_shadow_returns(ledger, paper_ledger, brief_date=brief_date, account=account)
    if not report.matured:
        typer.echo(
            f"shadow-returns {brief_date} account={account}: horizon not matured — "
            "skipped (0 priced). Re-run after the holding horizon closes."
        )
        return
    typer.echo(
        f"shadow-returns {brief_date} account={account}: {report.n_priced} priced, "
        f"{report.n_skipped} skipped, {report.n_no_bars} no-bars "
        f"({report.n_outcomes} matured outcomes)."
    )


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
    """Sweep recent brief dates, pricing each whose holding horizon has matured.

    The nightly VPS timer's entrypoint — it runs with NO ``--date`` so it needs
    no date arithmetic. It sweeps ``[today - lookback_days, today]`` newest-first
    and prices every matured date; not-yet-matured dates are skipped per-date.
    Idempotent (re-stamps the same deterministic value), so a ``Persistent=true``
    catch-up after VPS downtime is safe. Per-ticker fetch failures skip + warn;
    one bad ticker never aborts the sweep.
    """
    from alphalens_pipeline.feedback.shadow_return import compute_shadow_returns_window

    reports = compute_shadow_returns_window(
        ledger, paper_ledger, lookback_days=lookback_days, account=account
    )
    matured = [r for r in reports if r.matured]
    pending = [r for r in reports if not r.matured]
    n_priced_total = sum(r.n_priced for r in matured)
    # reports are newest-first: [0] is today, [-1] is the oldest swept date.
    start, end = reports[-1].brief_date, reports[0].brief_date
    typer.echo(
        f"shadow-returns backfill {start}..{end} account={account}: "
        f"{n_priced_total} priced across {len(matured)} matured dates, "
        f"{len(pending)} dates not yet matured."
    )


def _fmt(value: float | None) -> str:
    """Format a possibly-None decimal-fraction statistic for the report."""
    return "n/a" if value is None else f"{value:+.4f}"


@feedback_app.command(name="execution-modes")
def execution_modes_command(
    ledger: Path = typer.Option(
        Path.home() / ".alphalens" / "feedback.db",
        "--ledger",
        help="Override the default feedback ledger location.",
    ),
) -> None:
    """Per-regime LIMIT→MARKET recommendation from the matured ledger (Track A v2 PR-4).

    READ-ONLY. Never mutates the ledger and never touches the paper submitter —
    it prints what the §6 break-even WOULD recommend once the ≥50-decision gate
    clears. Today it is inert (matured n far below 50), so every cell reads
    LIMIT; the report exists so the human sees the evidence shape building up.
    The recommendation rests on tiny denominators at the floor, so the per-stat
    backing counts (unfilled for MO, gap for the execution drag) are printed next
    to every line.
    """
    from alphalens_pipeline.feedback.execution_modes import (
        DEFAULT_POOLED_GATE_N,
        POOLED_KEY,
        SWITCHABLE_REGIMES,
        UNKNOWN_REGIME,
        recommend_execution_modes,
    )
    from alphalens_pipeline.feedback.store import FeedbackStore

    if not ledger.exists():
        typer.echo(f"no ledger at {ledger} — no matured decisions yet (all LIMIT).")
        raise typer.Exit(code=0)

    with FeedbackStore.open(ledger) as fb:
        rows = fb.iter_matured_decisions()

    recs = recommend_execution_modes(rows)
    pooled = recs[POOLED_KEY]
    total = sum(r.n for key, r in recs.items() if key != POOLED_KEY)
    unknown_n = recs[UNKNOWN_REGIME].n if UNKNOWN_REGIME in recs else 0

    typer.echo(f"execution-mode recommendations (ledger={ledger})")
    typer.echo(
        f"  matured priced outcomes: {total}  "
        f"({unknown_n} unknown-regime excluded from the gate, "
        f"{pooled.n} labelled in the pool)"
    )
    if pooled.n < DEFAULT_POOLED_GATE_N:
        typer.echo(
            f"  ⚠ GATE INERT — pooled n={pooled.n}/{DEFAULT_POOLED_GATE_N}; all cells LIMIT "
            "(design-now build-later, vision §8). The break-even is not evaluated."
        )

    def _emit(rec) -> None:
        typer.echo(
            f"    {rec.regime:<7} n={rec.n:<3} (unfilled={rec.n_unfilled}, gap={rec.n_gap})  "
            f"fill_rate={_fmt(rec.fill_rate)}  MO*={_fmt(rec.missed_opportunity_shrunk)}  "
            f"MI*={_fmt(rec.expected_market_impact)}  margin={_fmt(rec.switch_margin)}  "
            f"-> {rec.recommended_mode.upper()}  ({rec.gated_reason})"
        )

    _emit(pooled)
    for regime in (*SWITCHABLE_REGIMES, UNKNOWN_REGIME):
        if regime in recs:
            _emit(recs[regime])
