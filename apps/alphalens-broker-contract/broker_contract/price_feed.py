"""Broker-agnostic live price feed — the trigger source for live-market E/TP.

Dependency-free leaf. ``latest(uic)`` returns ``None`` when there is no FRESH
price (disconnect / staleness / halt) — the engine treats ``None`` as "do not
fire" (the stream-health veto). The real Saxo streaming feed (INC-2) implements
this; tests use an in-memory fake.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class PricePoint:
    uic: int
    price: float
    asof: dt.datetime  # UTC


@runtime_checkable
class PriceFeed(Protocol):
    def latest(self, uic: int) -> PricePoint | None: ...
