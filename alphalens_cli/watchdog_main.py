"""CLI entry points for the Layer 1 watchdog.

Subcommands invoked by launchd:

    alphalens watchdog run-once        # detection (every 15 min)
    alphalens watchdog process-queue   # worker (every 5 min)
    alphalens watchdog momentum-screen # Layer 2b daily scan (22:00)
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import typer
from dotenv import load_dotenv

from alphalens.config_gemini import build_gemini_config
from alphalens.watchdog.classifier import Action, SignalClassifier
from alphalens.watchdog.config import WATCHDOG_DEFAULTS
from alphalens.watchdog.dispatch.handlers.auto_trigger import (
    AutoTriggerEnqueueHandler,
)
from alphalens.watchdog.dispatch.handlers.digest import DigestHandler
from alphalens.watchdog.dispatch.handlers.telegram import TelegramHandler
from alphalens.watchdog.dispatch.router import DispatchRouter
from alphalens.watchdog.portfolio import PortfolioState, default_portfolio_path
from alphalens.watchdog.sources.cik_loader import CIKLoader
from alphalens.watchdog.sources.edgar import SECEdgarSource
from alphalens.watchdog.storage import SeenEventStore
from alphalens.watchdog.watchdog import Watchdog

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
    cfg["fetch_8k_details"] = True

    portfolio = PortfolioState.load(default_portfolio_path())

    home = Path.home() / ".alphalens" / "watchdog"
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

    from alphalens.watchdog.worker import AutoTriggerWorker

    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    home = Path.home() / ".alphalens" / "watchdog"
    ta_graph = TradingAgentsGraph(debug=False, config=build_gemini_config())
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


@watchdog_app.command("momentum-screen")
def momentum_screen(
    top_n: int = typer.Option(5, help="Number of top momentum names to report"),
    dry_run: bool = typer.Option(False, help="Print report to stdout, skip Telegram send"),
):
    """Run the Layer 2b momentum screener and Telegram the top-N results."""
    import pandas as pd

    from alphalens.momentum_screener.pipeline import MomentumPipeline
    from alphalens.momentum_screener.reporter import format_telegram_report

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    curr_date = pd.Timestamp.today().strftime("%Y-%m-%d")
    pipeline = MomentumPipeline()
    result = pipeline.run(curr_date=curr_date, top_n=top_n)
    text = format_telegram_report(result, curr_date)

    if dry_run:
        typer.echo(text)
        return

    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    telegram = TelegramHandler(bot_token=bot_token, chat_id=chat_id)
    telegram.send_message(text)
    typer.echo(f"sent {len(result)} candidates to Telegram")


@watchdog_app.command("status")
def status():
    """Report current state: queue, digest buffer, dedup count."""
    from alphalens.watchdog.status import collect_status, format_status

    home = Path.home() / ".alphalens" / "watchdog"
    result = collect_status(
        queue_path=home / "auto_trigger_queue.db",
        digest_path=home / "digest.db",
        seen_path=home / "seen_events.db",
    )
    typer.echo(format_status(result))


if __name__ == "__main__":
    watchdog_app()
