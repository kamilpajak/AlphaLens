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

DEFAULT_MAX_AGE_S = 45.0
"""How old a quote may be under a decision. NOT a halt detector, and NOT a
dead-stream detector — that second job moved to ``QuoteSource.is_receiving()``
(#1397), because one number cannot answer both questions honestly.

The retired 3.0 s rested on a premise that measurement refuted: the memo argued
it "still detects a dead stream within seconds **against a 1 Hz push**". Saxo's
``RefreshRate`` is a CEILING on how often it may speak about an instrument, not
a pulse, so a quiet name simply goes unmentioned. On LIVE 2026-09-09 that vetoed
28% of ladder evaluations for uic 641 while the stream was demonstrably alive.

Set from three 15-minute 1 Hz windows on LIVE 2026-09-10 (open 13:30, midmorning
~16:00, lull 17:30 UTC; ~4.5k samples each). Pooled per-sample veto rate for the
SLOWEST held name (uic 641), by candidate bound:

    3 s -> 57.9%    15 s -> 3.9%    20 s -> 1.8%    30 s -> 0.4%
    45 s -> 0.0%    60 s -> 0.0%    92 s -> 0.0%   120 s -> 0.0%

Its pooled tail: p99 23.0 s, p99.9 39.7 s, max 41.7 s (at the open). Every other
subscribed name was already at 0.0% by 15 s.

45 s is the smallest bound that clears the measured tail. The pre-registered
widening rule said ">= 4x the p99 restatement interval", which would have given
92 s — but that floor was a PROXY for "clear the tail", and the windows measure
the tail directly. Buying 92 s over 45 s adds 47 seconds of exposure to a frozen
price during a halt and returns exactly zero coverage. The rule's own
instruction was to take the low end, and the low end of what actually satisfies
it is 45.

The cost is stated rather than hidden: Saxo publishes no reliable halt signal
for ``/trade/v1/infoprices``, so the only symptom of a halt is a frozen quote —
precisely what a looser bound tolerates. Exposure in the first seconds of a halt
grows from 3 s to 45 s. A LULD halt is normally 5 minutes, so the bound still
catches one, just not instantly. What burns is ONE tranche (``plan_tranche_exits``
behind the ``_exit_clears_cost`` gate), never the position: the disaster stop
stays on the remainder. The entry-watch scope inherits the same exposure, since
``control_loop`` builds this feed for ``_FEED_SCOPE_ENTRY_WATCH`` too."""

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
