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
from typing import TYPE_CHECKING

import typer

# Pure-stdlib emitter (os / tempfile / pathlib) — cheap enough to import at
# module top, unlike the alpaca-py + pandas deps the command bodies lazy-load.
# Imported here so the CLI emit-callsite tests can patch it as
# ``paper.emit_domain_metrics`` (same pattern as edgar / cache / thematic).
from alphalens_pipeline.observability.textfile import emit_domain_metrics

if TYPE_CHECKING:
    from alphalens_pipeline.paper.reconciler import ReconcileReport

logger = logging.getLogger(__name__)

# Domain-metric job id for the reconcile gauges. Matches the
# ``alphalens-emit-job-metrics paper-reconcile`` bash hook on the systemd
# unit so both halves of the metric stream land in the same textfile dir.
_RECONCILE_JOB = "paper-reconcile"

paper_app = typer.Typer(
    name="paper",
    help="Paper-trade forward-observation harness (see PR #273 design memo).",
    no_args_is_help=True,
)

# Shared help text for the --ledger flag across plan / submit / reconcile.
# Sonar flagged the duplicated literal (S1192); extracting keeps the three
# command signatures in lock-step on a single source of truth.
_LEDGER_HELP = "Override the default paper ledger location (~/.alphalens/paper_ledger.db)."

_ALLOW_CLOSED_HELP = (
    "Bypass the XNYS market-closed guard (run even on weekends / US "
    "public holidays). Default off — protects against stale-ladder gap "
    "risk; see docs/research/paper_trading_non_trading_day_2026_05_29.md."
)


def _today_utc() -> dt.date:
    """Wall-clock UTC date the market-closed guard reads.

    Indirected so the tests can patch this single call site rather
    than freezing the whole interpreter clock. Pure stdlib — no
    pandas / exchange_calendars import on the hot path.
    """
    return dt.datetime.now(dt.UTC).date()


def _emit_market_closed_message(action: str) -> None:
    """Print + log the deferral message for ``action`` (\"submit\" /
    \"reconcile\"), naming the next XNYS session open.

    The message format is operator-facing — short, includes the
    next-session anchor, and stays plain-English so non-quant readers
    of the cron logs can parse it. Operator can re-run with
    ``--allow-closed-market`` for ad-hoc work (manual reconcile, smoke).
    """
    # Lazy-import the calendar module so weekday submit (the common
    # path) doesn't pay the ~50 ms exchange_calendars XNYS load on
    # startup. Only the closed-day branch needs ``next_trading_open``.
    from alphalens_pipeline.paper.calendar import next_trading_open

    # Anchor the next-session lookup to the SAME date the guard decided on
    # (``_today_utc()``), not a second independent ``datetime.now()`` read.
    # Two clock reads can straddle a UTC midnight, and tests patch only
    # ``_today_utc`` — a real ``now()`` here made the message name a different
    # session than the guard's closed day (it leaked the wall-clock date into
    # the deferral anchor). Midnight-of-today is safe because the guard only
    # fires on a fully closed day, so there is no earlier same-day open.
    now_utc = dt.datetime.combine(_today_utc(), dt.time.min, tzinfo=dt.UTC)
    nxt = next_trading_open(now_utc)
    msg = (
        f"paper {action}: market closed today; deferring until next "
        f"XNYS session open at {nxt.isoformat()} (pass --allow-closed-market "
        f"to override)."
    )
    logger.info(msg)
    typer.echo(msg)


def _emit_reconcile_metrics(report: ReconcileReport, *, account: str) -> None:
    """Emit reconcile telemetry as Prometheus gauges, labelled by account.

    Key gauges (the LIVE-account protection dead-man signals):
      * ``alphalens_paper_filled_without_sl`` — filled positions that ended
        the pass with NO live protective SL. A sustained value > 0 means an
        unprotected live position; the alert rule pages on it.
      * ``alphalens_paper_exits_failed`` — exit submits the broker rejected
        this pass (held_for_orders / insufficient qty / APIError).

    The reconcile work is already persisted before this call; an emit failure
    is pure observability debt and must NEVER fail the unit (PR #311 rule —
    a malformed dict or unwriteable metrics dir would otherwise flip the
    cron-health exit code and eventually false-page a staleness alert).
    """
    acct = account.replace("\\", "").replace('"', "")
    try:
        emit_domain_metrics(
            job=_RECONCILE_JOB,
            metrics={
                f'alphalens_paper_filled_without_sl{{account="{acct}"}}': report.n_filled_without_sl,
                f'alphalens_paper_exits_failed{{account="{acct}"}}': report.n_exits_failed,
                f'alphalens_paper_entries_canceled{{account="{acct}"}}': report.n_entries_canceled,
                f'alphalens_paper_exits_attached{{account="{acct}"}}': report.n_exits_attached,
            },
        )
    except Exception:
        logger.exception("emit_domain_metrics failed; paper reconcile run succeeded")


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
    platform: str = typer.Option(
        "alpaca",
        "--platform",
        help="Paper-trading platform to route orders to. Only 'alpaca' today.",
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
    broker = None
    if not no_alpaca:
        # Lazy-import the client so --no-alpaca + a fresh checkout without an
        # ALPACA_API_KEY can still dry-run the planner end-to-end.
        from alphalens_pipeline.paper.broker import get_default_broker_client

        broker = get_default_broker_client(platform=platform, profile=profile)

    report = plan_for_date(
        brief_date=brief_date,
        briefs_dir=resolved_briefs,
        ledger_path=resolved_ledger,
        broker=broker,
        force=force,
        account=profile,
        platform=platform,
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
    platform: str = typer.Option(
        "alpaca",
        "--platform",
        help="Paper-trading platform to route orders to. Only 'alpaca' today.",
    ),
    allow_closed_market: bool = typer.Option(
        False,
        "--allow-closed-market",
        help=_ALLOW_CLOSED_HELP,
    ),
) -> None:
    """Submit entry-tier limit orders to Alpaca paper for every PLANNED
    candidate on ``brief_date`` that hasn't been submitted yet.

    Idempotent at (plan_id, tier_index) — re-running after a partial
    submit (mid-batch crash, network blip) only pushes the tiers that
    don't already have an ENTRY row in ``orders``.

    Market-closed guard: when run on a non-XNYS-session day (weekend or
    US public holiday) the command logs a deferral message and exits 0
    without contacting Alpaca. Pass ``--allow-closed-market`` to bypass
    (useful for ad-hoc smoke tests). Rationale: queuing GTC limits over
    a weekend exposes the ladder to opening-gap fills at stale Fri-close
    anchors — see docs/research/paper_trading_non_trading_day_2026_05_29.md.
    """
    # Lazy-import the calendar gate so weekday submit doesn't pay the
    # ~50 ms exchange_calendars XNYS load on the hot path.
    from alphalens_pipeline.paper.calendar import is_trading_day

    if not allow_closed_market and not is_trading_day(_today_utc()):
        _emit_market_closed_message("submit")
        return

    from alphalens_pipeline.paper.broker import get_default_broker_client
    from alphalens_pipeline.paper.constants import DEFAULT_LEDGER_RELPATH
    from alphalens_pipeline.paper.submitter import submit_for_date

    brief_date = dt.date.fromisoformat(date)
    resolved_ledger = (
        ledger_path if ledger_path is not None else Path.home() / DEFAULT_LEDGER_RELPATH
    )

    profile = "test" if use_test_account else "main"
    broker = get_default_broker_client(platform=platform, profile=profile)

    report = submit_for_date(
        brief_date=brief_date,
        ledger_path=resolved_ledger,
        broker=broker,
        account=profile,
        platform=platform,
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
    platform: str = typer.Option(
        "alpaca",
        "--platform",
        help="Paper-trading platform to route orders to. Only 'alpaca' today.",
    ),
    allow_closed_market: bool = typer.Option(
        False,
        "--allow-closed-market",
        help=_ALLOW_CLOSED_HELP,
    ),
) -> None:
    """Reconcile every open ledger order against Alpaca paper.

    For each ledger order in SUBMITTED / PARTIALLY_FILLED:
      - GET the Alpaca order by id
      - Transition local status (FILLED / CANCELED / REJECTED / …)
      - Append a fill row if Alpaca reports new filled_qty

    Idempotent: re-running on identical Alpaca state appends no fills.

    Market-closed guard: same gating as ``submit`` — non-trading days
    skip with exit 0 (no Alpaca state can have changed since the last
    session close). Pass ``--allow-closed-market`` for ad-hoc operator
    runs. See docs/research/paper_trading_non_trading_day_2026_05_29.md.
    """
    from alphalens_pipeline.paper.calendar import is_trading_day

    if not allow_closed_market and not is_trading_day(_today_utc()):
        _emit_market_closed_message("reconcile")
        return

    from alphalens_pipeline.paper.broker import get_default_broker_client
    from alphalens_pipeline.paper.constants import DEFAULT_LEDGER_RELPATH
    from alphalens_pipeline.paper.reconciler import reconcile_orders

    resolved_ledger = (
        ledger_path if ledger_path is not None else Path.home() / DEFAULT_LEDGER_RELPATH
    )
    profile = "test" if use_test_account else "main"
    broker = get_default_broker_client(platform=platform, profile=profile)

    report = reconcile_orders(
        ledger_path=resolved_ledger,
        broker=broker,
        account=profile,
    )

    typer.echo(
        f"paper reconcile (profile={profile}): "
        f"checked={report.n_orders_checked} "
        f"transitioned={report.n_orders_transitioned} "
        f"fills+={report.n_fills_appended} "
        f"exits_attached={report.n_exits_attached} "
        f"exits_failed={report.n_exits_failed} "
        f"entries_canceled={report.n_entries_canceled} "
        f"filled_without_sl={report.n_filled_without_sl}"
    )
    if report.n_filled_without_sl > 0:
        # Surface the dead-man condition loudly on the operator console too —
        # a filled position with no live disaster-stop is the single most
        # dangerous state in the harness. The next reconcile pass retries the
        # SL convergence; this line tells the operator to watch it.
        typer.echo(
            f"  ! WARNING: {report.n_filled_without_sl} filled position(s) have "
            f"NO live protective SL — convergence retries next pass."
        )

    # Emit Prometheus gauges so an alert can page on an unprotected live
    # position (filled_without_sl) or a run of exit-submit rejections. The
    # reconcile work above is already persisted; an emit failure is pure
    # observability debt and must NOT fail the unit (PR #311 callsite rule).
    _emit_reconcile_metrics(report, account=profile)
    for outcome in report.outcomes:
        if outcome.new_status == outcome.prev_status and outcome.n_new_fills == 0:
            continue
        suffix_parts = []
        if outcome.new_status != outcome.prev_status:
            suffix_parts.append(f"{outcome.prev_status}->{outcome.new_status}")
        if outcome.n_new_fills > 0:
            suffix_parts.append(f"+{outcome.n_new_fills} fills")
        typer.echo(f"  · {outcome.alpaca_order_id[:12]}  {' '.join(suffix_parts)}")


@paper_app.command("report")
def report(
    date: str | None = typer.Option(
        None,
        "--date",
        help=(
            "Scope to a single brief date (YYYY-MM-DD). Omit to aggregate across the whole ledger."
        ),
    ),
    ledger_path: Path | None = typer.Option(
        None,
        "--ledger",
        help=_LEDGER_HELP,
    ),
    use_main_account: bool = typer.Option(
        False,
        "--use-main-account",
        help="Scope to the MAIN account only. Mutually exclusive with --use-test-account.",
    ),
    use_test_account: bool = typer.Option(
        False,
        "--use-test-account",
        help="Scope to the TEST account only. Mutually exclusive with --use-main-account.",
    ),
) -> None:
    """Aggregate ledger state into a human-readable report.

    Read-only: zero Alpaca API calls. Surfaces plan counts, order
    lifecycle, exit outcomes, and the realised R-multiple distribution
    across the chosen (date × account) scope. Per-candidate table at
    the bottom lists every plan with its current state.

    Default scope is ALL accounts — unlike plan/submit/reconcile which
    must route through one Alpaca client, report is read-only and the
    natural audit view is the whole ledger. Pass --use-main-account or
    --use-test-account to narrow.
    """
    from alphalens_pipeline.paper.constants import DEFAULT_LEDGER_RELPATH
    from alphalens_pipeline.paper.report import build_report

    if use_main_account and use_test_account:
        raise typer.BadParameter(
            "--use-main-account and --use-test-account are mutually exclusive."
        )
    resolved_ledger = (
        ledger_path if ledger_path is not None else Path.home() / DEFAULT_LEDGER_RELPATH
    )
    brief_date = dt.date.fromisoformat(date) if date is not None else None
    if use_main_account:
        account: str | None = "main"
    elif use_test_account:
        account = "test"
    else:
        # Default: no account filter — aggregate the whole ledger.
        account = None

    rep = build_report(resolved_ledger, brief_date=brief_date, account=account)

    scope = []
    if brief_date is not None:
        scope.append(f"date={brief_date.isoformat()}")
    scope.append(f"account={account or 'ALL'}")
    typer.echo(f"paper report ({', '.join(scope)})")
    typer.echo("")

    s = rep.summary
    plan_parts = [f"{s.n_plans_planned} PLANNED", f"{s.n_plans_blocked} BLOCKED"]
    if s.n_plans_skipped > 0:
        # Planner never writes SKIPPED today; only surface it when something
        # has actually populated the row so the line stays minimal pre-ship.
        plan_parts.append(f"{s.n_plans_skipped} SKIPPED")
    typer.echo(f"  Plans:    {', '.join(plan_parts)}")
    if s.n_shadowed > 0:
        shadow_str = ", ".join(f"{k}={v}" for k, v in sorted(s.shadow_by_reason.items()))
        typer.echo(f"  Shadowed: {s.n_shadowed} ({shadow_str})")
    typer.echo(
        f"  Orders:   ENTRY {s.n_entries_filled}/{s.n_entries_submitted} filled, "
        f"TP={s.n_tp_orders}, SL={s.n_sl_orders}, TIME_STOP={s.n_time_stop_orders}"
    )
    typer.echo(f"  Fills:    {s.n_fills}")
    if s.n_outcomes > 0:
        kinds_str = ", ".join(f"{k}={v}" for k, v in sorted(s.outcomes_by_kind.items()))
        typer.echo(f"  Outcomes: {s.n_outcomes} ({kinds_str})")
        if s.hit_rate is not None:
            typer.echo(f"  Hit rate: {s.hit_rate:.1%}")
        if s.r_multiple_mean is not None:
            stdev_str = f"σ={s.r_multiple_stdev:.2f}" if s.r_multiple_stdev is not None else "σ=n/a"
            typer.echo(
                f"  R-multiple (n={s.n_r_multiple_observations}): "
                f"mean={s.r_multiple_mean:+.2f}  median={s.r_multiple_median:+.2f}  {stdev_str}"
            )
    else:
        typer.echo("  Outcomes: 0 (no plans closed yet)")

    if not rep.candidates:
        return

    typer.echo("")
    typer.echo(
        f"  {'date':<10} {'ticker':<6} {'acct':<4} {'status':<8} "
        f"{'fill':<8} {'entry':>8} {'kind':<10} {'R':>6}"
    )
    for cand in rep.candidates:
        fill_str = f"{cand.entry_filled_qty}/{cand.entry_planned_qty}"
        entry_str = (
            f"{cand.blended_entry_price:.2f}" if cand.blended_entry_price is not None else "—"
        )
        kind_str = cand.exit_kind or "—"
        r_str = f"{cand.realized_r_multiple:+.2f}" if cand.realized_r_multiple is not None else "—"
        typer.echo(
            f"  {cand.brief_date:<10} {cand.ticker:<6} {cand.account:<4} "
            f"{cand.status:<8} {fill_str:<8} {entry_str:>8} {kind_str:<10} {r_str:>6}"
        )


@paper_app.command("is-trading-day")
def is_trading_day_cmd(
    date: str | None = typer.Option(
        None,
        "--date",
        help=(
            "ISO date (YYYY-MM-DD) to check instead of today UTC. "
            "Operator-only — the systemd ExecCondition= callers do "
            "not pass this."
        ),
    ),
    exchange: str = typer.Option(
        "XNYS",
        "--exchange",
        help=(
            "ISO 10383 MIC code (XNYS / XWAR / XTKS / XHKG / XSHG). "
            "Default XNYS. The paper harness is exchange-agnostic per "
            "CLAUDE.md `## Conventions`; this flag is the natural "
            "extension point for multi-venue routing."
        ),
    ),
) -> None:
    """Exit 0 if ``date`` (default today UTC) is an ``exchange`` session,
    exit 1 otherwise.

    Used as ``ExecCondition=`` in the paper-submit + paper-reconcile
    systemd units to skip US public holidays that fall on weekdays —
    the ``OnCalendar=Mon..Fri`` filter alone would fire on them.

    Exit semantics matter — systemd ``ExecCondition=`` interprets:

    * **0** → proceed with ExecStart
    * **1-254** → skip silently (no AlphalensJobFailed alert)
    * **255** → treat as error

    So an exit-1 on a holiday MUST be a clean ``raise typer.Exit(1)``,
    NOT a raised exception (which Typer would map to exit-1 too but
    with traceback noise in the journal).

    Prints a one-line status to stdout including date + exchange so a
    ``journalctl --user -u alphalens-paper-submit.service`` after a
    skip immediately tells the operator WHICH day was rejected.

    Forward-compatible on ``--exchange`` per CLAUDE.md exchange-
    agnostic policy: adding XWAR routing becomes a per-call argument,
    not a refactor (see ``project_exchange_agnostic_calendar_2026_05_30``
    memory).
    """
    # Lazy-import the calendar gate — same rationale as the
    # market-closed guard in ``submit`` / ``reconcile``. ExecCondition
    # fires on every Mon..Fri timer tick (15× per session for
    # reconcile alone), so the ~50ms exchange_calendars XNYS load
    # would add up across the day if we paid it on every tick.
    from alphalens_pipeline.paper.calendar import is_trading_day

    check_date = dt.date.fromisoformat(date) if date is not None else _today_utc()
    trading = is_trading_day(check_date, exchange=exchange)

    # One-line status to stdout (journal captures it under
    # ``alphalens-paper-{submit,reconcile}.service`` when the operator
    # debugs a skip). Format: ``2026-05-25 XNYS: not a trading day``
    # so a journal grep on either the date OR the MIC code hits.
    verdict = "trading day" if trading else "not a trading day"
    typer.echo(f"{check_date.isoformat()} {exchange}: {verdict}")

    raise typer.Exit(0 if trading else 1)


__all__ = ["paper_app"]
