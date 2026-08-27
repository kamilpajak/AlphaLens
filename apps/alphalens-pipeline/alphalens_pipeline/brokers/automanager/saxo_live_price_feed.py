"""PriceFeed adapter over the Saxo LIVE quote stream.

Deliberately holds NO URL: it lives under ``brokers/``, where the SIM-only rail
(ADR 0014) fails red on any LIVE host string. Hosts live in the data-side stream
and client modules; this adapter only reads their cache.

Everything ambiguous returns ``None``. There is no path here that guesses.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

from broker_contract.price_feed import PricePoint, is_fresh

from alphalens_pipeline.data.alt_data.quote_source import QuoteSource

SOURCE = "saxo-live-l1"


class SaxoLivePriceFeed:
    """A structural ``PriceFeed`` reading the live quote cache.

    ``resolve_live_uic`` maps the caller's uic (from the managing instance's
    positions — SIM, or LIVE under the ADR 0017 daemon; the same feed factory
    serves both) to the LIVE uic the stream is keyed by. The two are NOT assumed
    equal: subscribing to the wrong instrument would be a silent catastrophe.

    ``stream`` is typed as the structural :class:`QuoteSource`, not the concrete
    in-process stream: under #1172 the same adapter reads either the local
    ``SaxoPriceStream`` or a socket proxy onto the one cross-process reader.
    """

    def __init__(
        self,
        *,
        stream: QuoteSource,
        resolve_live_uic: Callable[[int], int | None],
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._stream = stream
        self._resolve_live_uic = resolve_live_uic
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))

    def latest(self, uic: int) -> PricePoint | None:
        live_uic = self._resolve_live_uic(uic)
        if live_uic is None:
            return None
        quote = self._stream.get(live_uic)
        if quote is None or quote.bid is None or quote.ask is None:
            return None
        # Its OWN condition, not folded into age: a demoted session keeps
        # delivering plausible, moving, 15-minute-old quotes with no error.
        # 0 means confirmed-undelayed; anything else, INCLUDING None (no
        # DelayedByMinutes has ever arrived for this uic under delta-merge
        # semantics), means we do not know and therefore do not act.
        if quote.delayed_by_minutes != 0:
            return None
        # QuoteCache.apply stores whatever JSON value the socket delivered
        # without coercion (see its docstring) - a non-numeric Bid/Ask is a
        # doubt like any other and must veto here, not raise past this
        # module's caller into the tick.
        try:
            bid = float(quote.bid)
            ask = float(quote.ask)
        except (TypeError, ValueError):
            return None
        point = PricePoint(
            uic=uic,  # the CALLER's uic — the engine keys its state by it
            bid=bid,
            ask=ask,
            event_time=quote.event_time,
            received_at=quote.received_at,
            source=SOURCE,
        )
        return point if is_fresh(point, now=self._clock()) else None

    def session_low(self, uic: int) -> float | None:
        """DRAIN the stream's 1 Hz running-low touch-latch for this uic
        (:class:`~broker_contract.price_feed.SupportsSessionLow`). Maps the
        caller's uic to the LIVE uic exactly like :meth:`latest`; ``None`` when
        the uic does not resolve or nothing latchable accrued. A POP: the
        accumulation window resets on read, so the caller must invoke it at most
        once per uic per tick. Note it is NOT gated on the point-sample here —
        the DRAIN is unconditional (that is what keeps the window inter-tick);
        the combine at the call site discards the low when the concurrent
        point-sample is itself vetoed/stale."""
        live_uic = self._resolve_live_uic(uic)
        if live_uic is None:
            return None
        return self._stream.drain_running_low(live_uic)

    def reseed_session_low(self, uic: int, low: float) -> None:
        """Hand a drained running low BACK to the stream's accumulator
        (min-merge — see :meth:`QuoteCache.reseed_running_low`) when the caller
        could not act on it: the point-veto case only, per the combine at the
        call site (2026-08-18 incident — the unconditional drain destroyed a
        real touch's evidence on a tick whose point-sample was veto-stale).
        Maps the caller's uic to the LIVE uic exactly like :meth:`session_low`;
        silently a no-op when the uic does not resolve."""
        live_uic = self._resolve_live_uic(uic)
        if live_uic is None:
            return
        self._stream.reseed_running_low(live_uic, low)
