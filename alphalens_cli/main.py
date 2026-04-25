"""AlphaLens CLI entry point.

Top-level commands:
    analyze TICKER      — one-shot Layer 3 deep analysis
    status              — global state (queue + digest + dedup)
    backtest            — screener-agnostic backtest harness

Groups:
    watchdog/           — Layer 1 SEC EDGAR event detection
    queue/              — Layer 3 ops (process worker + scorer-stats)
    themed/             — Layer 2b curated YAML universe scan + monitoring
    insider/            — Layer 2d Form 4 cluster-buy scan (live/ad-hoc)
    research/           — eksperymenty (LLM filter validation, ...)

For the TradingAgents interactive menu use `.venv/bin/tradingagents`
(upstream's console script, registered when TradingAgents is editable-
installed — its `cli` package is separate from this `alphalens_cli`
package to avoid namespace collision).
"""

from __future__ import annotations

import logging

import typer
from dotenv import load_dotenv

from alphalens_cli.commands.analyze import analyze
from alphalens_cli.commands.backtest import backtest
from alphalens_cli.commands.events import events_app
from alphalens_cli.commands.guru import guru_app
from alphalens_cli.commands.insider import insider_app
from alphalens_cli.commands.queue import queue_app
from alphalens_cli.commands.research import research_app
from alphalens_cli.commands.rotation import rotation_app
from alphalens_cli.commands.status import status
from alphalens_cli.commands.themed import themed_app
from alphalens_cli.commands.watchdog import watchdog_app

load_dotenv()

app = typer.Typer(
    name="alphalens",
    help="AlphaLens stock analysis pipeline CLI.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@app.callback()
def _root_callback() -> None:
    """Configure logging once before routing to any subcommand.

    basicConfig is a no-op if the root logger is already configured (e.g. by a
    parent Python process); in that case we inherit the parent's setup.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


app.add_typer(watchdog_app, name="watchdog")
app.add_typer(queue_app, name="queue")
app.add_typer(themed_app, name="themed")
app.add_typer(insider_app, name="insider")
app.add_typer(research_app, name="research")
app.add_typer(rotation_app, name="rotation")
app.add_typer(events_app, name="events")
app.add_typer(guru_app, name="guru")
app.command(name="analyze")(analyze)
app.command(name="status")(status)
app.command(name="backtest")(backtest)


if __name__ == "__main__":
    app()
