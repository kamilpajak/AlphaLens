"""Pure FX data types for the sizing FX leg (design memo
``docs/research/saxo_fx_leg_gpw_design_2026_07_18.md`` §4).

No I/O, no vendor imports — static-compute-core testability. Two frozen
dataclasses:

- :class:`FxRateQuote` — the verbatim broker FX spot snapshot returned by a
  vendor capability (``SaxoBroker.get_fx_rate``). The ADAPTER reports, never
  filters: policy acceptance (accepted PriceTypes, staleness) is applied by
  :func:`alphalens_pipeline.brokers.execution.build_fx_conversion`.
- :class:`FxConversion` — the policy-validated conversion consumed by
  ``paper.sizing.compute_setup_plan(fx=...)``. ``rate`` is Mid, direction
  account-ccy -> instrument-ccy (e.g. EUR->PLN = 4.34 PLN per EUR). Prices
  are NEVER converted — only the sizing NOTIONAL crosses currencies
  (convert the notional first, divide by the native price).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class FxRateQuote:
    """One broker FX spot snapshot (adapter output; a fact, not state).

    ``mid`` is the ONLY sizing input (weekend EURPLN spread observed ~0.32%;
    sizing off Ask would systematically undersize). Bid/Ask + the per-side
    PriceTypes are captured verbatim for the journal and for policy
    validation — Saxo's ``LastUpdated`` is NOT a data-age signal, so
    freshness is judged from PriceType + the local ``asof`` fetch timestamp.
    """

    base_currency: str  # account side, e.g. "EUR"
    quote_currency: str  # instrument side, e.g. "PLN"
    mid: float | None  # None = broker returned no Mid (refusal downstream)
    bid: float | None
    ask: float | None
    price_type_bid: str | None  # e.g. "Tradable" / "Indicative" / "OldIndicative"
    price_type_ask: str | None
    market_state: str | None
    source: str  # provenance, e.g. "saxo-fxspot-uic-1343-mid"
    asof: dt.datetime  # UTC fetch timestamp (local clock, NOT LastUpdated)

    @property
    def price_type(self) -> str | None:
        """Single display/journal PriceType; asymmetric sides are joined."""
        if self.price_type_bid == self.price_type_ask:
            return self.price_type_bid
        return f"{self.price_type_bid}/{self.price_type_ask}"


@dataclass(frozen=True)
class FxConversion:
    """A policy-validated conversion applied between the account-currency
    notional and the per-tier qty division (``_FX_CONVERSION_POINT =
    "notional-before-qty"``).

    ``rate`` means instrument-ccy per 1 account-ccy (Mid). ``sizing_buffer_pct``
    is the settlement-drift haircut stamped from the execution policy at build
    time (T+2 settlement-rate conversion + conversion markup are not knowable
    at order time). ``None`` — not a rate of 1.0 — is the same-currency
    representation: same-currency sizing passes ``fx=None`` and is a strict
    no-op.
    """

    account_currency: str
    instrument_currency: str
    rate: float
    sizing_buffer_pct: float
    source: str
    price_type: str | None
    bid: float | None
    ask: float | None
    asof: dt.datetime


__all__ = [
    "FxConversion",
    "FxRateQuote",
]
