"""AlphaLens CLI entry point.

Exposes my watchdog subcommands. For the TradingAgents interactive menu,
use `.venv/bin/tradingagents` (upstream's console script, registered when
TradingAgents is editable-installed — its `cli` package is separate from
this `alphalens_cli` package to avoid namespace collision).
"""
from __future__ import annotations

import typer

from alphalens_cli.watchdog_main import watchdog_app

app = typer.Typer(
    name="alphalens",
    help="AlphaLens stock analysis pipeline CLI.",
    no_args_is_help=True,
)
app.add_typer(watchdog_app, name="watchdog")


if __name__ == "__main__":
    app()
