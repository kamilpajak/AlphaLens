from __future__ import annotations

from abc import ABC, abstractmethod

from ...classifier import ClassifiedEvent


class AlertHandler(ABC):
    @abstractmethod
    def handle(self, classified: ClassifiedEvent) -> None:
        """Process a classified event (send alert, persist, trigger analysis...)."""
