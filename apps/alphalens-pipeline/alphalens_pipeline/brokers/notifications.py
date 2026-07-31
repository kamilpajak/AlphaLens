"""NotificationPort: the abstract alert sink the broker manager calls.

``brokers/`` depends on this ABSTRACT port; the concrete telegram-backed sink is
wired at the composition root (the CLI). Keeps ``brokers/`` free of any
``data.alt_data.telegram`` import (dep-rule ``brokers`` -/-> ``data.alt_data.telegram``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

# A one-arg message sink. Existing call sites invoke it as alert(msg).
NotificationPort = Callable[[str], None]


class SupportsNotify(Protocol):
    def __call__(self, message: str, /) -> None: ...


__all__ = ["NotificationPort", "SupportsNotify"]
