"""AlphaLens CLI entry point.

Exposes my watchdog subcommands plus `analyze TICKER` for one-shot Layer 3.
For the TradingAgents interactive menu, use `.venv/bin/tradingagents`
(upstream's console script, registered when TradingAgents is editable-
installed — its `cli` package is separate from this `alphalens_cli`
package to avoid namespace collision).
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

import typer
from dotenv import load_dotenv

from alphalens.config_gemini import build_gemini_config
from alphalens_cli.watchdog_main import watchdog_app
from tradingagents.graph.trading_graph import TradingAgentsGraph

load_dotenv()

app = typer.Typer(
    name="alphalens",
    help="AlphaLens stock analysis pipeline CLI.",
    no_args_is_help=True,
)
app.add_typer(watchdog_app, name="watchdog")


@app.command()
def analyze(
    ticker: str = typer.Argument(..., help="Ticker symbol, e.g. TSHA"),
    date: Optional[str] = typer.Option(
        None, help="Analysis date YYYY-MM-DD; defaults to today"
    ),
) -> None:
    """Run TradingAgents Layer 3 deep analysis on a single ticker (Gemini config)."""
    analysis_date = date or dt.date.today().isoformat()
    graph = TradingAgentsGraph(debug=False, config=build_gemini_config())
    _, decision = graph.propagate(ticker, analysis_date)
    typer.echo(decision)


if __name__ == "__main__":
    app()
