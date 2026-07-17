"""CLI: ``alphalens broker`` — broker execution layer reads (SIM-only, ADR 0014).

P1 subcommands (reads only; ``submit``/``cancel``/``orders`` land with P2,
OAuth bootstrap ``auth`` with P4):

    alphalens broker account                 — account snapshot (cash / value / margin)
    alphalens broker positions               — open positions
    alphalens broker resolve KO [--exchange XNYS]  — instrument resolution (symbol -> Uic)

All ``brokers`` imports are lazy inside command bodies — the ``alphalens``
binary's startup time is paid by the 15-min Layer-1 edgar-detect cron
(+913ms precedent; see CLAUDE.md lazy-CLI convention).
"""

from __future__ import annotations

import typer

broker_app = typer.Typer(
    name="broker",
    help="Broker execution layer — SIM-only reads (ADR 0014).",
    no_args_is_help=True,
)


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
