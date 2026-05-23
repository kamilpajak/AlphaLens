"""`alphalens watchdog` — Layer 1 SEC EDGAR event detector."""

from __future__ import annotations

import os
from pathlib import Path

import typer
from alphalens_pipeline.core.queue import default_queue_path
from alphalens_pipeline.edgar_detector.classifier import Action, SignalClassifier
from alphalens_pipeline.edgar_detector.config import DETECTOR_DEFAULTS
from alphalens_pipeline.edgar_detector.detector import Detector
from alphalens_pipeline.edgar_detector.dispatch.handlers.auto_trigger import (
    AutoTriggerEnqueueHandler,
)
from alphalens_pipeline.edgar_detector.dispatch.handlers.digest import DigestHandler
from alphalens_pipeline.edgar_detector.dispatch.handlers.telegram import TelegramHandler
from alphalens_pipeline.edgar_detector.dispatch.router import DispatchRouter
from alphalens_pipeline.edgar_detector.portfolio import PortfolioState, default_portfolio_path
from alphalens_pipeline.edgar_detector.sources.cik_loader import CIKLoader
from alphalens_pipeline.edgar_detector.sources.edgar import SECEdgarSource
from alphalens_pipeline.edgar_detector.storage import SeenEventStore

watchdog_app = typer.Typer(
    name="watchdog",
    help="Layer 1: SEC EDGAR event detection + Telegram alerts.",
    no_args_is_help=True,
)


@watchdog_app.callback()
def _watchdog_callback() -> None:
    """Force multi-command behaviour even when only one command is registered."""


def _build_detector() -> Detector:
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    cfg = dict(DETECTOR_DEFAULTS)
    cfg["fetch_form4_details"] = True
    cfg["fetch_8k_details"] = True

    portfolio = PortfolioState.load(default_portfolio_path())

    home = Path.home() / ".alphalens" / "watchdog"
    cik_loader = CIKLoader(cache_path=home / "company_tickers.json")
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
    enqueue = AutoTriggerEnqueueHandler(queue_path=default_queue_path())

    router = DispatchRouter(
        {
            Action.AUTO_TRIGGER: [enqueue, telegram],
            Action.APPROVAL: [telegram],
            Action.DIGEST: [digest],
        }
    )

    return Detector(
        sources=[source],
        classifier=SignalClassifier(),
        portfolio=portfolio,
        router=router,
    )


@watchdog_app.command(name="run-once")
def run_once() -> None:
    """Poll EDGAR once, classify new events, dispatch alerts."""
    watchdog = _build_detector()
    result = watchdog.run_once()
    typer.echo(f"detected={result['events_detected']} dispatched={result['events_dispatched']}")
