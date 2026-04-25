from __future__ import annotations

import logging

from .classifier import SignalClassifier
from .dispatch.router import DispatchRouter
from .portfolio import PortfolioState
from .sources.base import EventSource

logger = logging.getLogger(__name__)


class Watchdog:
    """Orchestrator: polls all sources, classifies events, dispatches via router."""

    def __init__(
        self,
        sources: list[EventSource],
        classifier: SignalClassifier,
        portfolio: PortfolioState,
        router: DispatchRouter,
    ):
        self.sources = sources
        self.classifier = classifier
        self.portfolio = portfolio
        self.router = router

    def run_once(self) -> dict:
        all_events = []
        for source in self.sources:
            try:
                events = source.detect()
                all_events.extend(events)
            except Exception as exc:
                logger.error("Source %s failed: %s", type(source).__name__, exc, exc_info=True)

        dispatched = 0
        for event in all_events:
            classified = self.classifier.classify(event, self.portfolio)
            self.router.dispatch(classified)
            dispatched += 1

        return {
            "events_detected": len(all_events),
            "events_dispatched": dispatched,
        }
