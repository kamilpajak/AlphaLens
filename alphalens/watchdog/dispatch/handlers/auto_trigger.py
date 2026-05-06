from __future__ import annotations

import logging
from pathlib import Path

from alphalens.core.candidates import Candidate
from alphalens.core.queue import CandidateQueue

from ...classifier import ClassifiedEvent
from .base import AlertHandler

logger = logging.getLogger(__name__)


class AutoTriggerEnqueueHandler(AlertHandler):
    """Detection-side handler: submits a `watchdog_sec` Candidate to the shared queue.

    The queue is a historical log only — the Layer 3 worker that previously
    drained it was removed per ADR 0008. New candidates accumulate on disk
    in `~/.alphalens/candidates.db`; ad-hoc inspection is via direct SQL.
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
            logger.info("Submitted candidate for %s (%s)", event.ticker, event.accession_number)
        except Exception as exc:
            logger.error("Candidate submit failed for %s: %s", event.ticker, exc, exc_info=True)

    def close(self) -> None:
        self.queue.close()
