"""Broker-agnostic live price feed — the trigger source for live-market E/TP.

Dependency-free leaf. ``latest(uic)`` returns ``None`` when there is no
TRUSTWORTHY price (disconnect, staleness, halt, unknown age) — the engine treats
``None`` as "do not fire" (the stream-health veto).

``event_time`` is deliberately optional. A source that publishes no tick
timestamp reports ``None``, and :func:`is_fresh` vetoes it. Stamping local fetch
time into ``event_time`` is therefore not merely discouraged — the honest
alternative is expressible, so the dishonest one has no excuse. ``received_at``
records local arrival for diagnostics and MUST NEVER be used to compute age.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

DEFAULT_MAX_AGE_S = 3.0
"""Roughly twice the worst event lag measured on the Saxo LIVE stream (1.4 s),
and still detects a dead 1 Hz push within seconds."""

DEFAULT_MAX_RELATIVE_SPREAD = 0.02
"""(ask-bid)/mid ceiling. Liquid US names measured 0.003-0.03%, so 2% catches a
broken quote without vetoing a normal one. Relative because this project has no
per-instrument spread table and inventing one is not worth the upkeep."""


@dataclass(frozen=True)
class PricePoint:
    uic: int
    bid: float
    ask: float
    event_time: dt.datetime | None  # UTC, from the PROVIDER; None = not published
    received_at: dt.datetime  # UTC, local arrival — diagnostics only
    source: str  # e.g. "saxo-live-l1", "yfinance-last"

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


def is_fresh(
    point: PricePoint,
    *,
    now: dt.datetime,
    max_age_s: float = DEFAULT_MAX_AGE_S,
    max_relative_spread: float = DEFAULT_MAX_RELATIVE_SPREAD,
) -> bool:
    """Pure predicate: may this quote drive an order decision?

    Vetoes an unknown or future ``event_time`` (clock skew must not read as
    freshness), a non-finite or non-positive side, a crossed market, and an
    absurd relative spread.
    """
    if point.event_time is None:
        return False
    if point.event_time.tzinfo is None:
        # Belt-and-braces: a naive event_time should never reach this shared
        # predicate (the Saxo parser vetoes it upstream), but is_fresh is the
        # contract every feed uses, so it must not trust that. Subtracting a
        # naive datetime from an aware `now` raises TypeError in the stdlib -
        # a doubt about which instant this names must veto here, not crash.
        return False
    if not (math.isfinite(point.bid) and math.isfinite(point.ask)):
        return False
    if point.bid <= 0.0 or point.ask <= 0.0:
        return False
    if point.bid > point.ask:
        return False
    age = (now - point.event_time).total_seconds()
    if age < 0.0 or age > max_age_s:
        return False
    mid = point.mid
    if mid <= 0.0:
        return False
    return (point.ask - point.bid) / mid <= max_relative_spread


@runtime_checkable
class PriceFeed(Protocol):
    def latest(self, uic: int) -> PricePoint | None: ...
