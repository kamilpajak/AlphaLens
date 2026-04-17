from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .queue import AutoTriggerQueue

logger = logging.getLogger(__name__)

DEFAULT_BUDGET_PER_DAY = 5


class Sender(Protocol):
    def send_message(self, text: str) -> None: ...


class TradingAgentsProtocol(Protocol):
    def propagate(self, ticker: str, date: str): ...


class AutoTriggerWorker:
    """Drains the auto-trigger queue. One launchd job invokes this periodically.

    Each process_one() call claims at most one job, runs TradingAgents,
    marks done/failed, and notifies via the injected sender.
    """

    def __init__(
        self,
        ta_graph: TradingAgentsProtocol,
        notifier: Sender,
        queue_path: Path | str | None = None,
        budget_per_day: int = DEFAULT_BUDGET_PER_DAY,
    ):
        self.ta_graph = ta_graph
        self.notifier = notifier
        self.queue = AutoTriggerQueue(queue_path)
        self.budget_per_day = budget_per_day

    def process_one(self) -> bool:
        done_today = self.queue.count_done_today()
        if done_today >= self.budget_per_day:
            self._notify(
                f"🛑 Auto-trigger budget exhausted ({done_today}/{self.budget_per_day}). "
                f"Skipping pending jobs until tomorrow. Manual trigger remains available."
            )
            return False

        job = self.queue.claim_next()
        if job is None:
            return False

        ticker = job["ticker"]
        date_str = datetime.now(timezone.utc).date().isoformat()

        try:
            _, decision = self.ta_graph.propagate(ticker, date_str)
            self.queue.mark_done(job["id"], decision=decision)
            self._notify(
                f"🤖 Auto-analysis done — {ticker}\n"
                f"Decision: *{decision}*\n"
                f"Trigger: {job['trigger_url']}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Worker propagate failed for %s: %s", ticker, exc, exc_info=True)
            self.queue.mark_failed(job["id"], error=str(exc))
            self._notify(f"❌ Auto-analysis failed for {ticker}: {exc}")

        return True

    def process_all(self) -> int:
        count = 0
        while self.process_one():
            count += 1
        return count

    def close(self) -> None:
        self.queue.close()

    def _notify(self, msg: str) -> None:
        try:
            self.notifier.send_message(msg)
        except Exception as exc:  # noqa: BLE001
            logger.error("Notifier send failed: %s", exc)
