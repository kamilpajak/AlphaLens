"""AlphaLens CLI entry point.

Exposes my watchdog subcommands. For TradingAgents interactive menu,
invoke `python -m cli.main` from inside TradingAgents/, or use
`.venv/bin/tradingagents` (upstream's console script, registered when
TradingAgents is editable-installed).
"""
from __future__ import annotations

import typer

from cli.watchdog_main import watchdog_app

app = typer.Typer(
    name="alphalens",
    help="AlphaLens stock analysis pipeline CLI.",
    no_args_is_help=True,
)
app.add_typer(watchdog_app, name="watchdog")


if __name__ == "__main__":
    app()
