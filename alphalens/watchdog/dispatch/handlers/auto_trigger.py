from __future__ import annotations

import logging
from pathlib import Path

from alphalens.candidates import Candidate
from alphalens.queue import CandidateQueue

from ...classifier import ClassifiedEvent
from .base import AlertHandler

logger = logging.getLogger(__name__)


class AutoTriggerEnqueueHandler(AlertHandler):
    """Detection-side handler: submits a `watchdog_sec` Candidate to the shared queue.

    Does NOT run TradingAgents. The shared worker (`alphalens.worker.AnalysisWorker`)
    drains the queue with its own budget and retry policy.
    """

    SOURCE = "watchdog_sec"
    PRIORITY = 0

    def __init__(self, queue_path: Path | str | None = None):
        self.queue = CandidateQueue(queue_path)

    def handle(self, classified: ClassifiedEvent) -> None:
        event = classified.event
        try:
            candidate = Candidate.from_screener(
                ticker=event.ticker,
                source=self.SOURCE,
                priority=self.PRIORITY,
                payload={
                    "accession": event.accession_number,
                    "url": event.url,
                    "form": event.form_type.value,
                },
                discriminator=event.accession_number,
                detected_at=event.filed_at,
            )
            self.queue.submit([candidate])
            logger.info(
                "Submitted candidate for %s (%s)", event.ticker, event.accession_number
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Candidate submit failed for %s: %s", event.ticker, exc, exc_info=True
            )

    def close(self) -> None:
        self.queue.close()
