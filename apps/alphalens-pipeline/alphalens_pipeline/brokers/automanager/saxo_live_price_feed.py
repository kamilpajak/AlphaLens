"""PriceFeed adapter over the Saxo LIVE quote stream.

Deliberately holds NO URL: it lives under ``brokers/``, where the SIM-only rail
(ADR 0014) fails red on any LIVE host string. Hosts live in the data-side stream
and client modules; this adapter only reads their cache.

Everything ambiguous returns ``None``. There is no path here that guesses.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable

from broker_contract.price_feed import PricePoint, is_fresh

from alphalens_pipeline.data.alt_data.quote_source import QuoteSource

logger = logging.getLogger(__name__)

SOURCE = "saxo-live-l1"

# One line per this many seconds while the source is dark. The daemon polls
# every ~45s and asks per uic, so an untrottled warning would be ~19 lines a
# tick; silence, though, is what #1392 had to undo. One line per outage-ish
# window is the middle the journal can actually be read at.
_DARK_WARN_INTERVAL_S = 300.0


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
        self._last_dark_warn: dt.datetime | None = None

    def _warn_dark(self) -> None:
        """Name the veto, throttled. Which of the two conditions withheld the
        price has to be legible: `no_price` alone in the daemon's disposition
        line cannot tell "the source is dark" from "this quote is too old",
        and that ambiguity is what #1392 spent a ticket removing one layer
        up."""
        now = self._clock()
        last = self._last_dark_warn
        if last is not None and (now - last).total_seconds() < _DARK_WARN_INTERVAL_S:
            return
        self._last_dark_warn = now
        logger.warning(
            "saxo live price feed: the quote source is not receiving from the venue — "
            "withholding every price until it does"
        )

    def latest(self, uic: int) -> PricePoint | None:
        # TWO questions, deliberately separated (#1397). This one is about the
        # SOURCE — is it hearing from the venue at all — and it is asked here
        # rather than once per tick by the caller because a precondition
        # somebody has to remember is a precondition that gets forgotten; the
        # round trip is a local UNIX call.
        #
        # It used to be asked by proxy, through the quote's own age: a 3s bound
        # whose docstring called itself a dead-stream detector. Measured in
        # session on 2026-09-10, that vetoed uic 641 on 70% of samples with the
        # stream demonstrably alive, because quote age really measures how
        # often Saxo restates THAT instrument (its interval reached 41.7s).
        if not self._stream.is_receiving():
            self._warn_dark()
            return None
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
