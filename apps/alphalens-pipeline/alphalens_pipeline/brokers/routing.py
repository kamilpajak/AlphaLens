"""Ticker -> venue routing for the execution layer (design memo §P2).

The thematic candidate parquet carries no exchange stamp, and MIC is an
execution concern (ADR 0013 R2 — no execution data flows upstream), so the
routing decision lives HERE, not in the pipeline schema: explicit MIC wins;
otherwise probe the ordered US venue list (XNYS, then XNAS) through the
broker's exact-symbol resolve (2 throttled, cached lookups worst case) and
require EXACTLY ONE match — zero matches and both-venue matches both raise
:class:`InstrumentNotFoundError` (never guess; house doctrine). The RESOLVED
MIC is stamped on the submission record for P3 reconciliation.

XWAR stays EXPLICIT-ONLY (``exchange_mic="XWAR"``) — deliberately absent
from the probe order. The PLN/FX-leg sizing question that used to block it
is designed and implemented per
``docs/research/saxo_fx_leg_gpw_design_2026_07_18.md``; adding XWAR to any
probe order remains a follow-up decision AFTER the GPW first-fill
experiment passes (memo §6).
"""

from __future__ import annotations

from alphalens_pipeline.brokers.contract import (
    Broker,
    InstrumentNotFoundError,
    InstrumentRef,
)

# Ordered US probe list. Adding a venue here widens the AMBIGUITY surface for
# every un-suffixed ticker — extend deliberately, never for convenience.
US_MIC_PROBE_ORDER: tuple[str, ...] = ("XNYS", "XNAS")


def resolve_us_instrument(
    broker: Broker,
    ticker: str,
    exchange_mic: str | None = None,
) -> InstrumentRef:
    """Resolve ``ticker`` to a broker instrument handle.

    ``exchange_mic`` explicit -> straight ``resolve_instrument`` (any venue
    the broker maps, including XWAR). Otherwise probe
    :data:`US_MIC_PROBE_ORDER` and require exactly one venue to resolve.
    """
    if exchange_mic:
        return broker.resolve_instrument(ticker, exchange_mic)

    matches: list[InstrumentRef] = []
    for mic in US_MIC_PROBE_ORDER:
        try:
            matches.append(broker.resolve_instrument(ticker, mic))
        except InstrumentNotFoundError:
            continue
    if not matches:
        raise InstrumentNotFoundError(
            f"{ticker!r} resolved on none of the probed US venues "
            f"{US_MIC_PROBE_ORDER}; pass an explicit exchange MIC "
            "(non-US venues like XWAR are explicit-only)"
        )
    if len(matches) > 1:
        venues = [ref.exchange_mic for ref in matches]
        raise InstrumentNotFoundError(
            f"{ticker!r} is AMBIGUOUS across US venues {venues} — refusing to "
            "guess; pass an explicit exchange MIC"
        )
    return matches[0]


__all__ = [
    "US_MIC_PROBE_ORDER",
    "resolve_us_instrument",
]
