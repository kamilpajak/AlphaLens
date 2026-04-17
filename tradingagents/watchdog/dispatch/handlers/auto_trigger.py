from __future__ import annotations

import logging
from pathlib import Path

from ...classifier import ClassifiedEvent
from ...queue import AutoTriggerQueue
from .base import AlertHandler

logger = logging.getLogger(__name__)


class AutoTriggerEnqueueHandler(AlertHandler):
    """Detection-side handler: inserts a job into the auto-trigger queue.

    Does NOT run TradingAgents. A separate worker (see `watchdog.worker`)
    drains the queue and executes deep analysis with its own budget guard.
    """

    def __init__(self, queue_path: Path | str | None = None):
        self.queue = AutoTriggerQueue(queue_path)

    def handle(self, classified: ClassifiedEvent) -> None:
        event = classified.event
        try:
            self.queue.enqueue(
                ticker=event.ticker,
                accession=event.accession_number,
                trigger_url=event.url,
            )
            logger.info("Enqueued auto-trigger for %s (%s)", event.ticker, event.accession_number)
        except Exception as exc:  # noqa: BLE001
            logger.error("Enqueue failed for %s: %s", event.ticker, exc, exc_info=True)

    def close(self) -> None:
        self.queue.close()
