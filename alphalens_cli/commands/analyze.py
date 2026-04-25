"""Top-level `analyze TICKER` command — one-shot Layer 3 deep analysis."""

from __future__ import annotations

import datetime as dt

import typer
from tradingagents.graph.trading_graph import TradingAgentsGraph

from alphalens.config_gemini import build_gemini_config


def analyze(
    ticker: str = typer.Argument(..., help="Ticker symbol, e.g. TSHA"),
    date: str | None = typer.Option(None, help="Analysis date YYYY-MM-DD; defaults to today"),
) -> None:
    """Run TradingAgents Layer 3 deep analysis on a single ticker (Gemini config)."""
    analysis_date = date or dt.date.today().isoformat()
    graph = TradingAgentsGraph(debug=False, config=build_gemini_config())
    _, decision = graph.propagate(ticker, analysis_date)
    typer.echo(decision)
