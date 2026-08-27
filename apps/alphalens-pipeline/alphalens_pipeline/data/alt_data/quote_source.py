"""The structural contract a live quote source must satisfy (#1172).

Dependency-free leaf. Names the EXACT surface the feed factory
(``control_loop._default_live_exits_feed_factory``) and the adapter
(``brokers.automanager.saxo_live_price_feed.SaxoLivePriceFeed``) use, so the
in-process :class:`~alphalens_pipeline.data.alt_data.saxo_price_stream.SaxoPriceStream`
and the cross-process reader client are interchangeable without either
importing the other.

Why a Protocol and not a base class: the stream predates the client by a year
and lives on the reader side of a process boundary. Structural typing lets the
client be a thin socket proxy that happens to answer the same five questions,
which is exactly what the boundary makes it.

Deliberately NOT part of the contract: ``consumer=`` on the drain/reseed pair.
A cross-process consumer's identity is its CONNECTION, resolved server-side —
a client never names itself on the wire, so the contract keeps the
single-consumer signatures the adapter already calls.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol, runtime_checkable


class Quotelike(Protocol):
    """The read-only quote shape the price-feed adapter consumes.

    Mirrors :class:`~alphalens_pipeline.data.alt_data.saxo_price_stream.Quote`
    field for field, INCLUDING its documented caveat: ``bid``/``ask`` are
    DECLARED ``float | None`` but a live payload can deliver a string or an
    explicit null (a one-sided market, a halt), so every consumer coerces or
    vetoes before doing arithmetic. Declaring them ``object`` here instead
    would be more honest in isolation and worse in practice — it would force
    the adapter's already-guarded coercion to carry casts, and it would state
    a DIFFERENT contract than the dataclass every producer builds.

    Not ``runtime_checkable``: nothing isinstance-checks a quote (the adapter
    reads fields and vetoes on doubt), and a data protocol's isinstance is a
    hasattr sweep that proves nothing about the values.
    """

    @property
    def uic(self) -> int: ...

    @property
    def bid(self) -> float | None: ...

    @property
    def ask(self) -> float | None: ...

    @property
    def event_time(self) -> dt.datetime | None: ...

    @property
    def delayed_by_minutes(self) -> int | None: ...

    @property
    def received_at(self) -> dt.datetime: ...


@runtime_checkable
class QuoteSource(Protocol):
    """The five calls a price feed makes against its quote source."""

    def get(self, uic: int) -> Quotelike | None:
        """The latest merged quote for ``uic``, or ``None`` when none is cached."""
        ...

    def drain_running_low(self, uic: int) -> float | None:
        """POP the 1 Hz touch-latch running low for ``uic`` (read-and-reset)."""
        ...

    def reseed_running_low(self, uic: int, low: float) -> None:
        """MIN-MERGE a drained-but-unusable low back into the accumulator."""
        ...

    def live_uic_for(self, ticker: str, *, exchange_mic: str) -> int | None:
        """Resolve ``(ticker, venue)`` to the LIVE uic the source is keyed by."""
        ...

    def ensure_subscribed(self, uics: set[int] | list[int], *, scope: str = "default") -> None:
        """Declare the caller's desired uic set for ``scope``; the source owns
        the wire-level subscription (the union across scopes)."""
        ...
