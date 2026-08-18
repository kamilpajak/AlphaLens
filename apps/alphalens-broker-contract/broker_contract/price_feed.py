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


def _is_finite_number(value: object) -> bool:
    """math.isfinite(value) narrowed to "veto instead of raise" on a
    non-numeric value. is_fresh must not trust that a bid/ask is actually a
    number - see its docstring."""
    try:
        return math.isfinite(value)  # type: ignore[arg-type]
    except TypeError:
        return False


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
    # The same "must not trust the caller" reasoning applies to bid/ask: a
    # non-numeric side should never reach here (today's only caller coerces
    # first), but a future/other feed could construct a PricePoint straight
    # from raw JSON - math.isfinite (and PricePoint.mid) would raise
    # TypeError on that exactly like the naive datetime would above, so
    # _is_finite_number vetoes instead of raising.
    if not (_is_finite_number(point.bid) and _is_finite_number(point.ask)):
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


@runtime_checkable
class SupportsSessionLow(Protocol):
    """Extension capability: a 1 Hz running LOW accumulated between the coarse
    point samples — the touch-latch that closes the sub-sampling touch-miss
    (docs/research/entry_trailing_design_2026_08_12.md §5 mitigation path).

    ``session_low(uic)`` DRAINS (pop-and-reset) the accumulated sub-tick running
    low for ``uic``: the caller invokes it EXACTLY once per uic per decision
    tick, and the reset is what bounds the accumulation window to one tick.
    ``None`` when no trustworthy sub-tick low accrued (feed off/degraded, or
    nothing latchable this window).

    ``reseed_session_low(uic, low)`` hands a drained low BACK to the accumulator
    when the caller could not act on it — the point-veto case (2026-08-18
    incident: the drain is unconditional, so a tick whose concurrent
    point-sample is veto-stale used to DESTROY the latched evidence of a real
    touch). The feed MUST min-merge (a deeper accrual that landed after the
    drain wins), making a repeated reseed idempotent, and MUST silently ignore
    a non-finite/non-positive value.

    Mirrors the ``SupportsTrailingStop`` capability-protocol pattern (off the
    frozen :class:`PriceFeed` base): a caller ``isinstance``-narrows a
    ``PriceFeed`` to this Protocol; a feed WITHOUT it (the OFF/degraded null
    feed) simply yields no low, which is the safe degraded behaviour — no latch,
    point-sample only."""

    def session_low(self, uic: int) -> float | None: ...

    def reseed_session_low(self, uic: int, low: float) -> None: ...
