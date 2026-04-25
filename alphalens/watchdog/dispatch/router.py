from __future__ import annotations

import logging

from ..classifier import Action, ClassifiedEvent
from .handlers.base import AlertHandler

logger = logging.getLogger(__name__)


class DispatchRouter:
    def __init__(self, action_handlers: dict[Action, list[AlertHandler]]):
        self.action_handlers = action_handlers

    def dispatch(self, classified: ClassifiedEvent) -> None:
        if classified.action == Action.IGNORE:
            return
        handlers = self.action_handlers.get(classified.action, [])
        for handler in handlers:
            try:
                handler.handle(classified)
            except Exception as exc:
                logger.error(
                    "Handler %s failed for %s: %s",
                    type(handler).__name__,
                    classified.event.ticker,
                    exc,
                    exc_info=True,
                )
