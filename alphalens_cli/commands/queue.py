"""`alphalens queue` — Layer 3 ops (screener-agnostic worker + stats)."""

from __future__ import annotations

import os

import typer

from alphalens.queue import CandidateQueue, default_queue_path
from alphalens.runner import TradingAgentsRunner
from alphalens.watchdog.dispatch.handlers.telegram import TelegramHandler
from alphalens.worker import AnalysisWorker

queue_app = typer.Typer(
    name="queue",
    help="Layer 3 ops: drain candidate queue via TradingAgents + scorer stats.",
    no_args_is_help=True,
)


def _build_worker() -> AnalysisWorker:
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    telegram = TelegramHandler(bot_token=bot_token, chat_id=chat_id)
    queue = CandidateQueue(default_queue_path())
    runner = TradingAgentsRunner()

    return AnalysisWorker(queue=queue, runner=runner, notifier=telegram)


@queue_app.command(name="process")
def process() -> None:
    """Drain the auto-trigger queue (one job per call of TradingAgents).

    Uses a kernel-level flock on ~/.alphalens/watchdog/worker.lock so that
    launchd-spawned workers and manual runs never execute in parallel —
    parallel workers hammer the Gemini 1M-tokens/min quota and deadlock.
    """
    from alphalens.watchdog_lock import (
        WorkerLockBusy,
        default_worker_lock_path,
        worker_lock,
    )

    try:
        with worker_lock(default_worker_lock_path()):
            worker = _build_worker()
            processed = worker.process_all()
            typer.echo(f"processed={processed}")
    except WorkerLockBusy:
        typer.echo("another worker instance is running — skipping (see worker.lock for pid)")
        raise typer.Exit(code=0)


@queue_app.command(name="scorer-stats")
def scorer_stats(
    since_days: int = typer.Option(30, help="Only count Layer 3 runs finished within the last N days"),
) -> None:
    """Layer 3 acceptance rate per scorer — used for paper-trade validation.

    Queries the candidate queue for completed TradingAgents runs, groups by
    `source` (e.g. 'momentum' vs 'early-stage'), and reports decision
    distribution + accept rate (BUY+OVERWEIGHT / total).
    """
    from alphalens.scorer_stats import compute_scorer_stats, format_stats_table

    stats = compute_scorer_stats(default_queue_path(), since_days=since_days)
    typer.echo(f"=== Scorer stats — last {since_days} days ===")
    typer.echo(format_stats_table(stats))
