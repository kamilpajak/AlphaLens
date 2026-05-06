"""AlphaLens CLI entry point.

Top-level commands:
    status              — global state (queue + digest + dedup)
    backtest            — screener-agnostic backtest harness

Groups:
    watchdog/           — Layer 1 SEC EDGAR event detection
    queue/              — scorer-stats viewer over the historical candidate queue
    themed/             — Layer 2b curated YAML universe scan + monitoring
    insider/            — Layer 2d Form 4 cluster-buy scan (live/ad-hoc)
    research/           — eksperymenty (LLM filter validation, ...)
"""

from __future__ import annotations

import logging

import typer
from dotenv import load_dotenv

from alphalens_cli.commands.backtest import backtest
from alphalens_cli.commands.insider import insider_app
from alphalens_cli.commands.literature import literature_app
from alphalens_cli.commands.paper_trade import paper_trade_app
from alphalens_cli.commands.preregister import preregister_app
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
app.add_typer(literature_app, name="literature")
app.add_typer(paper_trade_app, name="paper-trade")
app.add_typer(preregister_app, name="preregister")
app.add_typer(research_app, name="research")
app.add_typer(rotation_app, name="rotation")
app.command(name="status")(status)
app.command(name="backtest")(backtest)


if __name__ == "__main__":
    app()
