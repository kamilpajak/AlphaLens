"""CLI: ``alphalens broker`` — broker execution layer (SIM-only, ADR 0014).

Subcommands (P1 reads + P2 orders; OAuth bootstrap ``auth`` lands with P4):

    alphalens broker account                 — account snapshot (cash / value / margin)
    alphalens broker positions               — open positions
    alphalens broker resolve KO [--exchange XNYS]  — instrument resolution (symbol -> Uic)
    alphalens broker submit KO --date 2026-07-16   — DRY-RUN by default: bracket
        table + precheck; sending needs --execute AND an interactive confirm
        (--yes skips the prompt) AND ALPHALENS_BROKER_ALLOW_ORDERS=1 in the env
    alphalens broker orders                  — open orders
    alphalens broker cancel <order_id>       — cancel (entry cancel cascades the bracket)
    alphalens broker reconcile [--json]      — READ-ONLY journal vs broker verdicts (P3):
        WORKING / PAST-TTL divergence / FILLED (+closed r) / CANCELLED / REJECTED /
        EXPIRED / UNRESOLVED(reason); exit 1 on any unresolved or divergent row

All ``brokers`` imports are lazy inside command bodies — the ``alphalens``
binary's startup time is paid by the 15-min Layer-1 edgar-detect cron
(+913ms precedent; see CLAUDE.md lazy-CLI convention).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import typer

broker_app = typer.Typer(
    name="broker",
    help="Broker execution layer — SIM-only (ADR 0014).",
    no_args_is_help=True,
)

_DEFAULT_BRIEFS_DIR = Path.home() / ".alphalens" / "thematic_briefs"


def _fail(message: str) -> typer.Exit:
    typer.secho(message, fg=typer.colors.RED, err=True)
    return typer.Exit(code=1)


@broker_app.command(name="account")
def account_command() -> None:
    """Print the broker account snapshot (cash, total value, margin)."""
    from alphalens_pipeline.brokers.contract import BrokerError
    from alphalens_pipeline.brokers.registry import get_default_broker

    try:
        snapshot = get_default_broker().get_account()
    except BrokerError as exc:
        raise _fail(f"broker account failed: {exc}") from exc

    margin = "n/a" if snapshot.margin_available is None else f"{snapshot.margin_available:,.2f}"
    typer.echo(f"account   {snapshot.account_id}")
    typer.echo(f"currency  {snapshot.currency}")
    typer.echo(f"cash      {snapshot.cash:,.2f}")
    typer.echo(f"total     {snapshot.total_value:,.2f}")
    typer.echo(f"margin    {margin}")
    typer.echo(f"asof      {snapshot.asof.isoformat(timespec='seconds')}")


@broker_app.command(name="positions")
def positions_command() -> None:
    """List open positions (signed quantity, avg price, market value, PnL)."""
    from alphalens_pipeline.brokers.contract import BrokerError
    from alphalens_pipeline.brokers.registry import get_default_broker

    try:
        positions = get_default_broker().get_positions()
    except BrokerError as exc:
        raise _fail(f"broker positions failed: {exc}") from exc

    if not positions:
        typer.echo("no open positions")
        return
    for position in positions:
        market_value = "n/a" if position.market_value is None else f"{position.market_value:,.2f}"
        pnl = "n/a" if position.unrealized_pnl is None else f"{position.unrealized_pnl:+,.2f}"
        typer.echo(
            f"{position.instrument.broker_symbol:16s} "
            f"qty {position.quantity:+10.2f}  "
            f"avg {position.avg_price:10.2f}  "
            f"mv {market_value:>12s}  "
            f"pnl {pnl:>12s}  "
            f"id {position.position_id}"
        )


@broker_app.command(name="resolve")
def resolve_command(
    ticker: str = typer.Argument(..., help="Plain ticker, e.g. KO."),
    exchange: str = typer.Option(
        "XNYS",
        "--exchange",
        help="ISO 10383 MIC of the listing venue (XNYS, XNAS, XWAR).",
    ),
) -> None:
    """Resolve (ticker, MIC) to the broker instrument handle (Saxo: Uic)."""
    from alphalens_pipeline.brokers.contract import BrokerError
    from alphalens_pipeline.brokers.registry import get_default_broker

    try:
        ref = get_default_broker().resolve_instrument(ticker, exchange)
    except BrokerError as exc:
        raise _fail(f"broker resolve failed: {exc}") from exc

    typer.echo(f"ticker        {ref.ticker}")
    typer.echo(f"exchange_mic  {ref.exchange_mic}")
    typer.echo(f"asset_type    {ref.asset_type}")
    typer.echo(f"broker_id     {ref.broker_instrument_id}")
    typer.echo(f"symbol        {ref.broker_symbol}")


def _echo_bracket_table(brackets: list) -> None:
    typer.echo(
        f"{'#':>2s}  {'qty':>6s}  {'entry':>10s}  {'stop':>10s}  {'tp':>10s}  "
        f"{'ttl':>4s}  client_request_id"
    )
    for index, bracket in enumerate(brackets):
        tp = "-" if bracket.take_profit is None else f"{bracket.take_profit:.4f}"
        stop = "-" if bracket.stop_loss is None else f"{bracket.stop_loss:.4f}"
        typer.echo(
            f"{index:>2d}  {bracket.quantity:>6d}  {bracket.entry_limit:>10.4f}  "
            f"{stop:>10s}  {tp:>10s}  {bracket.entry_ttl_days:>4d}  "
            f"{bracket.client_request_id}"
        )


@broker_app.command(name="submit")
def submit_command(
    ticker: str = typer.Argument(..., help="Plain ticker from the brief, e.g. KO."),
    date: str = typer.Option(..., "--date", help="Brief date (YYYY-MM-DD)."),
    briefs_dir: Path = typer.Option(
        _DEFAULT_BRIEFS_DIR, "--briefs-dir", help="Thematic briefs parquet directory."
    ),
    exchange: str | None = typer.Option(
        None,
        "--exchange",
        help="Explicit ISO 10383 MIC; omit to probe US venues (XNYS then XNAS). "
        "Non-US venues (XWAR) are explicit-only.",
    ),
    equity: float | None = typer.Option(
        None, "--equity", help="Sizing equity in account currency; default: broker total value."
    ),
    scale_factor: float = typer.Option(
        1.0, "--scale-factor", help="Daily global scale factor (see paper/sizing.py); default 1.0."
    ),
    execute: bool = typer.Option(
        False,
        "--execute",
        help="Actually place the brackets (default is DRY-RUN: table + precheck only). "
        "Also requires ALPHALENS_BROKER_ALLOW_ORDERS=1 in the environment.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the interactive confirmation (scripted use)."
    ),
) -> None:
    """Decompose one candidate's trade setup into per-tier brackets and submit.

    DRY-RUN BY DEFAULT: prints the decomposed bracket table and runs the
    order precheck (validates server-side, places NOTHING). Sending requires
    --execute AND an interactive confirmation (--yes skips it) AND the
    ALPHALENS_BROKER_ALLOW_ORDERS=1 env gate enforced inside the broker.
    """
    from alphalens_pipeline.brokers.contract import BrokerError
    from alphalens_pipeline.brokers.execution import (
        decompose_setup_plan,
        execution_config_version,
    )
    from alphalens_pipeline.brokers.registry import get_default_broker
    from alphalens_pipeline.brokers.routing import resolve_us_instrument
    from alphalens_pipeline.brokers.submission_log import (
        append_submission_record,
        build_submission_record,
    )
    from alphalens_pipeline.paper.brief_loader import load_brief
    from alphalens_pipeline.paper.sizing import TradeSetupNotPlannableError, compute_setup_plan

    try:
        brief_date = dt.date.fromisoformat(date)
    except ValueError as exc:
        raise _fail(f"invalid --date {date!r}: {exc}") from exc

    try:
        candidates = load_brief(brief_date, briefs_dir)
    except (FileNotFoundError, ValueError) as exc:
        raise _fail(str(exc)) from exc

    wanted = ticker.upper()
    candidate = next((c for c in candidates if c.ticker.upper() == wanted), None)
    if candidate is None:
        raise _fail(f"{wanted} not in the {brief_date} brief ({len(candidates)} candidates)")
    if candidate.trade_setup is None:
        raise _fail(f"{wanted} has no parseable brief_trade_setup on {brief_date}")

    try:
        broker = get_default_broker()
        sizing_equity = equity if equity is not None else broker.get_account().total_value
        plan = compute_setup_plan(
            brief_trade_setup=candidate.trade_setup,
            paper_equity=sizing_equity,
            scale_factor=scale_factor,
        )
        instrument = resolve_us_instrument(broker, wanted, exchange_mic=exchange)
    except TradeSetupNotPlannableError as exc:
        raise _fail(f"{wanted} is not plannable: {exc}") from exc
    except BrokerError as exc:
        raise _fail(f"broker submit failed: {exc}") from exc

    brackets = decompose_setup_plan(plan, instrument)
    if not brackets:
        raise _fail(f"{wanted}: every entry tier sized to zero shares — nothing to submit")

    typer.echo(
        f"{wanted} @ {instrument.exchange_mic} (Uic {instrument.broker_instrument_id})  "
        f"equity={sizing_equity:,.2f}  scale_factor={scale_factor}"
    )
    _echo_bracket_table(brackets)

    # Precheck every bracket (validates server-side, places nothing).
    precheck_summaries: list[dict] = []
    precheck_fn = getattr(broker, "precheck_bracket_order", None)
    if precheck_fn is None:
        typer.echo("precheck: not supported by this broker — skipping")
    else:
        for index, bracket in enumerate(brackets):
            try:
                payload = precheck_fn(bracket)
            except BrokerError as exc:
                raise _fail(f"precheck failed for bracket {index}: {exc}") from exc
            summary = {
                "client_request_id": bracket.client_request_id,
                "PreCheckResult": payload.get("PreCheckResult"),
                "EstimatedCashRequired": payload.get("EstimatedCashRequired"),
                "Costs": payload.get("Cost", payload.get("Costs")),
            }
            precheck_summaries.append(summary)
            typer.echo(
                f"precheck {index}: result={summary['PreCheckResult']!r} "
                f"est_cash={summary['EstimatedCashRequired']!r} costs={summary['Costs']!r}"
            )

    if not execute:
        typer.echo("DRY-RUN: nothing was sent. Re-run with --execute to place these brackets.")
        return

    if not yes:
        typer.confirm(
            f"Send {len(brackets)} bracket(s) for {wanted} to the Saxo SIM gateway?",
            abort=True,
        )

    placed_records: list[dict] = []
    failure_note: str | None = None
    try:
        for bracket in brackets:
            placed = broker.place_bracket_order(bracket)
            placed_records.append(
                {
                    "client_request_id": bracket.client_request_id,
                    "entry_order_id": placed.entry_order_id,
                    "exit_order_ids": list(placed.exit_order_ids),
                    "qty": bracket.quantity,
                    "entry": bracket.entry_limit,
                    "stop": bracket.stop_loss,
                    "tp": bracket.take_profit,
                    "ttl": bracket.entry_ttl_days,
                }
            )
            typer.echo(
                f"placed entry={placed.entry_order_id} "
                f"exits={','.join(placed.exit_order_ids) or '-'} "
                f"(request {bracket.client_request_id})"
            )
    except BrokerError as exc:
        failure_note = (
            f"placement stopped after {len(placed_records)}/{len(brackets)} bracket(s): {exc}"
        )
    finally:
        if placed_records or failure_note:
            record = build_submission_record(
                brief_date=brief_date.isoformat(),
                ticker=wanted,
                mic=instrument.exchange_mic,
                uic=instrument.broker_instrument_id,
                brackets=placed_records,
                precheck=precheck_summaries,
                note=failure_note,
            )
            path = append_submission_record(record)
            typer.echo(f"submission recorded: {path}")

    token = execution_config_version()
    typer.echo(f"execution_config_version {token}")
    if failure_note:
        placed_ids = [r["entry_order_id"] for r in placed_records]
        raise _fail(
            f"{failure_note}\nalready-placed entry orders: {placed_ids or 'none'} — "
            "reconcile via 'alphalens broker orders' / 'alphalens broker cancel <id>'"
        )


@broker_app.command(name="orders")
def orders_command() -> None:
    """List open orders (entry + exit children; UNKNOWN never guessed)."""
    from alphalens_pipeline.brokers.contract import BrokerError
    from alphalens_pipeline.brokers.registry import get_default_broker

    try:
        states = get_default_broker().list_open_orders()
    except BrokerError as exc:
        raise _fail(f"broker orders failed: {exc}") from exc

    if not states:
        typer.echo("no open orders")
        return
    for state in states:
        symbol = state.instrument.broker_symbol if state.instrument else "?"
        typer.echo(
            f"{state.order_id:12s} {state.status.value:16s} "
            f"filled {state.filled_quantity:10.2f}  {symbol:16s} raw={state.raw_status}"
        )


@broker_app.command(name="reconcile")
def reconcile_command(
    journal: Path | None = typer.Option(
        None,
        "--journal",
        help="Submission journal path (default: ~/.alphalens/broker_orders/submissions.jsonl).",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Emit the verdict dicts as JSON (incl. raw Status/SubStatus diagnostics, "
        "reason codes, realized r) for scripting.",
    ),
) -> None:
    """Reconcile journaled brackets against the broker — STRICTLY READ-ONLY.

    No order placement, no cancels; the journal is never rewritten (verdicts
    are recomputed at read time from the append-only SoT + the broker's
    open-orders view + the vendor's audit-log resolution capability).
    Exit code 0 when clean, 1 when any UNRESOLVED or divergent row exists
    (scriptable; a still-working entry PAST its TTL is a divergence).
    """
    from alphalens_pipeline.brokers.contract import BrokerError
    from alphalens_pipeline.brokers.reconcile import (
        has_failures,
        reconcile_brackets,
        summarize,
    )
    from alphalens_pipeline.brokers.registry import get_default_broker
    from alphalens_pipeline.brokers.submission_log import (
        DEFAULT_SUBMISSIONS_PATH,
        iter_submission_records,
    )

    path = journal or DEFAULT_SUBMISSIONS_PATH
    malformed: list[str] = []
    records = list(iter_submission_records(path, malformed=malformed))
    if malformed:
        typer.secho(
            f"journal: skipped {len(malformed)} malformed line(s) in {path}",
            fg=typer.colors.YELLOW,
            err=True,
        )
    if not records:
        typer.echo(f"no submission records in {path} — nothing to reconcile")
        return

    try:
        verdicts = reconcile_brackets(records, get_default_broker())
    except BrokerError as exc:
        raise _fail(f"broker reconcile failed: {exc}") from exc

    if as_json:
        typer.echo(json.dumps([v.as_dict() for v in verdicts], indent=2, default=str))
        if has_failures(verdicts):
            # Silent nonzero exit keeps stdout pure JSON for scripting.
            raise typer.Exit(code=1)
        return

    typer.echo(
        f"{'brief_date':10s}  {'ticker':6s}  {'qty':>8s}  {'entry_order_id':14s}  "
        f"{'verdict':30s}  {'activity_time':28s}  note"
    )
    for verdict in verdicts:
        note_parts = [part for part in (verdict.note, verdict.reason) if part]
        typer.echo(
            f"{verdict.brief_date:10s}  {verdict.ticker:6s}  {verdict.qty:>8.0f}  "
            f"{verdict.entry_order_id:14s}  {verdict.verdict:30s}  "
            f"{(verdict.activity_time or '-'):28s}  {'; '.join(note_parts) or '-'}"
        )
    summary = summarize(verdicts)
    typer.echo(
        f"{summary['total']} bracket(s): {summary['working']} working, "
        f"{summary['terminal']} terminal, {summary['unresolved']} unresolved, "
        f"{summary['divergent']} divergent"
    )
    if has_failures(verdicts):
        raise _fail("reconciliation found unresolved or divergent bracket(s) — see rows above")


@broker_app.command(name="cancel")
def cancel_command(
    order_id: str = typer.Argument(..., help="Broker OrderId (entry cancel cascades exits)."),
) -> None:
    """Cancel an order. Deliberately usable without the placement env gate."""
    from alphalens_pipeline.brokers.contract import BrokerError
    from alphalens_pipeline.brokers.registry import get_default_broker

    try:
        get_default_broker().cancel_order(order_id)
    except BrokerError as exc:
        raise _fail(f"broker cancel failed: {exc}") from exc
    typer.echo(f"cancelled {order_id} (an entry cancel cascades to its bracket children)")
