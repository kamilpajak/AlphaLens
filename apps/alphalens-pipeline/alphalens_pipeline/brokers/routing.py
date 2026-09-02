"""Ticker -> venue routing for the execution layer (design memo §P2).

The thematic candidate parquet carries no exchange stamp, and MIC is an
execution concern (ADR 0013 R2 — no execution data flows upstream), so the
routing decision lives HERE, not in the pipeline schema: explicit MIC wins;
otherwise probe the ordered US venue list (XNYS, then XNAS, then XASE)
through the broker's exact-symbol resolve (3 throttled, cached lookups worst
case) and require EXACTLY ONE match — zero matches and multi-venue matches
both raise :class:`InstrumentNotFoundError` (never guess; house doctrine).
The RESOLVED MIC is stamped on the submission record for P3 reconciliation.

XWAR stays EXPLICIT-ONLY (``exchange_mic="XWAR"``) — deliberately absent
from the probe order. The PLN/FX-leg sizing question that used to block it
is designed and implemented per
``docs/research/saxo_fx_leg_gpw_design_2026_07_18.md``; adding XWAR to any
probe order remains a follow-up decision AFTER the GPW first-fill
experiment passes (memo §6).
"""

from __future__ import annotations

from broker_contract.contract import (
    Broker,
    InstrumentNotFoundError,
    InstrumentRef,
)

# Ordered US probe list — the SHARED constant lives in
# ``data/alt_data/saxo_exchanges.py`` (re-exported here for the
# placement-side callers) and is also consumed by the day-1 gap gate price
# probe, so the two probe orders can never diverge. Adding a venue widens the
# AMBIGUITY surface for every un-suffixed ticker — extend deliberately, never
# for convenience.
from alphalens_pipeline.data.alt_data.saxo_exchanges import US_MIC_PROBE_ORDER


def explicit_mic_from_hint(hint_mic: str | None) -> str | None:
    """Map an intent's ``InstrumentHint.mic`` to the routing decision (#1238).

    A US hint is ADVISORY: every brief pick stamps ``mic="XNYS"`` while its
    real venue may be XNAS/XASE, so any hint inside
    :data:`US_MIC_PROBE_ORDER` (or an absent hint) returns ``None`` — the
    caller keeps probing exactly as before. A non-US hint (``arm-manual``
    stamps the operator's venue, e.g. XWAR) is AUTHORITATIVE: it returns the
    normalized MIC for an explicit single-venue resolve, so a same-ticker US
    listing is unreachable. Single-sourced here — the day-1 gap gate price
    probe applies the same rule (#1238).

    An unknown non-US MIC (a typo, an unmapped venue) is returned verbatim:
    validity is the broker venue map's call (``MIC_TO_SAXO_EXCHANGE_ID``),
    never a second whitelist here. Downstream that resolve failure is a
    caught ``InstrumentNotFoundError`` — the pick logs and retries next
    tick, it never crashes the tick and is never terminally refused.
    """
    if not hint_mic:
        return None
    mic = hint_mic.strip().upper()
    if not mic or mic in US_MIC_PROBE_ORDER:
        return None
    return mic


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
    "explicit_mic_from_hint",
    "resolve_us_instrument",
]
