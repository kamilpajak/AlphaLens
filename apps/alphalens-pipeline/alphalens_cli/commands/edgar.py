"""`alphalens edgar` — Layer 1 SEC EDGAR event detector."""

from __future__ import annotations

import datetime as dt
import logging
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
from alphalens_pipeline.edgar_detector.dispatch_state import (
    compute_trading_days_since_dispatch,
    load_last_dispatch_date,
    stamp_last_dispatch_date,
)
from alphalens_pipeline.edgar_detector.portfolio import PortfolioState, default_portfolio_path
from alphalens_pipeline.edgar_detector.sources.cik_loader import CIKLoader
from alphalens_pipeline.edgar_detector.sources.edgar import SECEdgarSource
from alphalens_pipeline.edgar_detector.storage import SeenEventStore
from alphalens_pipeline.observability.textfile import emit_domain_metrics

logger = logging.getLogger(__name__)

edgar_app = typer.Typer(
    name="edgar",
    help="Layer 1: SEC EDGAR event detection + Telegram alerts.",
    no_args_is_help=True,
)


@edgar_app.callback()
def _edgar_callback() -> None:
    """Force multi-command behaviour even when only one command is registered."""


def _build_detector() -> Detector:
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    cfg = dict(DETECTOR_DEFAULTS)
    cfg["fetch_form4_details"] = True
    cfg["fetch_8k_details"] = True

    portfolio = PortfolioState.load(default_portfolio_path())

    home = Path.home() / ".alphalens" / "edgar-detect"
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


def _state_home() -> Path:
    """Edgar-detect runtime state dir. Indirection point so tests can redirect
    the dispatch-state JSON to a temp dir without touching ``~/.alphalens``.
    """
    return Path.home() / ".alphalens" / "edgar-detect"


def _today() -> dt.date:
    """Today's date (UTC). Indirection point so the calendar-aware gauge can be
    tested at a pinned date without monkeypatching the stdlib clock.
    """
    return dt.datetime.now(dt.UTC).date()


def _trading_days_since_dispatch_gauge(dispatched: int) -> int:
    """Compute the no-dispatch gauge and persist last_dispatch_date.

    On a dispatch run (``dispatched > 0``) stamp today and return 0. On a quiet
    run return the calendar-aware trading-day gap since the last dispatch
    (today excluded). Cold start (no persisted date) returns 0 and stamps today
    so the series starts clean — never a huge first-run value.
    """
    home = _state_home()
    today = _today()
    last = load_last_dispatch_date(home)
    if dispatched > 0 or last is None:
        stamp_last_dispatch_date(home, today)
        return 0
    return compute_trading_days_since_dispatch(last, today)


@edgar_app.command(name="detect")
def detect() -> None:
    """Detect new SEC filings: poll EDGAR, classify, dispatch alerts."""
    detector = _build_detector()
    result = detector.run_once()
    typer.echo(f"detected={result['events_detected']} dispatched={result['events_dispatched']}")

    # Calendar-aware no-dispatch gauge. Computed + persisted OUTSIDE the emit
    # try/except below: a state-write failure here is unexpected (it would also
    # break dedup eventually) and we want it logged, but it still must not fail
    # the cron — so it has its own guard.
    try:
        trading_days_since_dispatch = _trading_days_since_dispatch_gauge(
            result["events_dispatched"]
        )
    except Exception:
        logger.exception("dispatch-state update failed; edgar-detect run succeeded")
        trading_days_since_dispatch = 0

    # Domain counters for the cron-observability dashboard (PR-2 of
    # the epic). Numbers are gauges, not Prometheus counters — they
    # describe THIS run's outcome, not cumulative since process start.
    # ``portfolio_size`` is a sanity-check: a 0 value here means an
    # empty portfolio.yaml slipped through (would also raise above,
    # but the metric makes the misconfiguration visible on Grafana).
    #
    # Wrap in try/except so a transient metrics-dir issue (disk full,
    # perms flip) does NOT poison the unit's success state: the EDGAR
    # poll already shipped Telegram alerts + updated seen_events.db,
    # so the cron work is done — losing the gauge is observability
    # debt, not a job failure. Zen pre-merge review (PR #311) pinned
    # this rule.
    try:
        emit_domain_metrics(
            job="edgar-detect",
            metrics={
                "alphalens_edgar_events_detected_total": result["events_detected"],
                "alphalens_edgar_events_dispatched_total": result["events_dispatched"],
                'alphalens_edgar_portfolio_size{class="held"}': len(detector.portfolio.held),
                'alphalens_edgar_portfolio_size{class="watchlist"}': len(
                    detector.portfolio.watchlist
                ),
                # Calendar-aware no-dispatch gauge (emitted EVERY run, incl.
                # weekends + zero-dispatch runs, so the series never goes
                # absent). 0 on a dispatch run / cold start; otherwise the
                # count of XNYS sessions strictly between the last dispatch and
                # today. Powers AlphalensEdgarNoDispatchTradingDays.
                "alphalens_edgar_trading_days_since_last_dispatch": trading_days_since_dispatch,
            },
        )
    except Exception:
        logger.exception("emit_domain_metrics failed; edgar-detect run succeeded")
