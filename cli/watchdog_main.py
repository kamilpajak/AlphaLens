"""CLI entry points for the Layer 1 watchdog.

Two subcommands invoked by launchd:

    tradingagents watchdog run-once        # detection (every 15 min)
    tradingagents watchdog process-queue   # worker (every 5 min)
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import typer
from dotenv import load_dotenv

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.watchdog.classifier import Action, SignalClassifier
from tradingagents.watchdog.config import WATCHDOG_DEFAULTS
from tradingagents.watchdog.dispatch.handlers.auto_trigger import (
    AutoTriggerEnqueueHandler,
)
from tradingagents.watchdog.dispatch.handlers.digest import DigestHandler
from tradingagents.watchdog.dispatch.handlers.telegram import TelegramHandler
from tradingagents.watchdog.dispatch.router import DispatchRouter
from tradingagents.watchdog.portfolio import PortfolioState, default_portfolio_path
from tradingagents.watchdog.sources.cik_loader import CIKLoader
from tradingagents.watchdog.sources.edgar import SECEdgarSource
from tradingagents.watchdog.storage import SeenEventStore
from tradingagents.watchdog.watchdog import Watchdog

load_dotenv()

watchdog_app = typer.Typer(
    name="watchdog",
    help="Layer 1 stock monitoring — SEC EDGAR + Telegram alerts.",
    no_args_is_help=True,
)


def _build_watchdog() -> Watchdog:
    user_agent = os.environ.get("WATCHDOG_USER_AGENT") or "AlphaLens Watchdog pajakkamil@gmail.com"
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    cfg = dict(WATCHDOG_DEFAULTS)
    cfg["user_agent"] = user_agent
    cfg["fetch_form4_details"] = True

    portfolio = PortfolioState.load(default_portfolio_path())

    home = Path.home() / ".tradingagents" / "watchdog"
    cik_loader = CIKLoader(user_agent=user_agent, cache_path=home / "company_tickers.json")
    cik_loader.load()

    tickers = sorted(set(portfolio.held + portfolio.watchlist))
    if not tickers:
        raise typer.BadParameter(
            f"Portfolio is empty. Create {default_portfolio_path()} with 'held:' and 'watchlist:' lists."
        )

    store = SeenEventStore(home / "seen_events.db")
    source = SECEdgarSource(
        tickers=tickers,
        config=cfg,
        store=store,
        cik_loader=cik_loader,
    )

    telegram = TelegramHandler(bot_token=bot_token, chat_id=chat_id)
    digest = DigestHandler(db_path=home / "digest.db", sender=telegram)
    enqueue = AutoTriggerEnqueueHandler(queue_path=home / "auto_trigger_queue.db")

    router = DispatchRouter({
        Action.AUTO_TRIGGER: [enqueue, telegram],
        Action.APPROVAL: [telegram],
        Action.DIGEST: [digest],
    })

    return Watchdog(sources=[source], classifier=SignalClassifier(), portfolio=portfolio, router=router)


def _build_worker():
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    from tradingagents.watchdog.worker import AutoTriggerWorker

    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    home = Path.home() / ".tradingagents" / "watchdog"
    ta_graph = TradingAgentsGraph(debug=False, config=DEFAULT_CONFIG)
    telegram = TelegramHandler(bot_token=bot_token, chat_id=chat_id)

    return AutoTriggerWorker(
        ta_graph=ta_graph,
        notifier=telegram,
        queue_path=home / "auto_trigger_queue.db",
    )


@watchdog_app.command("run-once")
def run_once():
    """Poll EDGAR once, classify new events, dispatch alerts."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    watchdog = _build_watchdog()
    result = watchdog.run_once()
    typer.echo(f"detected={result['events_detected']} dispatched={result['events_dispatched']}")


@watchdog_app.command("process-queue")
def process_queue():
    """Drain the auto-trigger queue (one job per call of TradingAgents)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    worker = _build_worker()
    processed = worker.process_all()
    typer.echo(f"processed={processed}")


if __name__ == "__main__":
    watchdog_app()
