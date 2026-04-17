from __future__ import annotations

from abc import ABC, abstractmethod

from ..types import Event


class EventSource(ABC):
    @abstractmethod
    def detect(self) -> list[Event]:
        """Poll the source and return new, deduplicated events."""
