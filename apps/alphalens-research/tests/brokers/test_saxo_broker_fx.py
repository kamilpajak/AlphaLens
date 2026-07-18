"""Hermetic tests for ``SaxoBroker.get_fx_rate`` (the FX-leg vendor capability).

Canned ``/ref/v1/currencypairs`` + ``/trade/v1/infoprices`` (FxSpot) payloads
mirror the live-read shapes in the design memo (EUR->PLN = Uic 1343; weekend
Bid 4.3331 / Ask 4.3469). Pins:

- pair Uic resolution from the BASE side (one-directional listing) with the
  FxSpot Keywords-search fallback, refusal on unresolvable/ambiguous pairs;
- the quote is reported VERBATIM (Mid/Bid/Ask/PriceTypes/MarketState/source
  provenance + a tz-aware local ``asof``) — the adapter never filters, policy
  acceptance is ``execution.build_fx_conversion``'s job;
- ``get_fx_rate`` stays OFF the frozen Broker Protocol (capability pattern);
- vendor errors translate at the boundary.
"""

from __future__ import annotations

import unittest
from typing import Any

from alphalens_pipeline.brokers.contract import (
    Broker,
    BrokerError,
    InstrumentNotFoundError,
)
from alphalens_pipeline.brokers.saxo.broker import SaxoBroker
from alphalens_pipeline.brokers.saxo.client import SaxoError

_EURPLN_UIC = 1343

_CURRENCY_PAIRS = {
    "Data": [
        {"CurrencyPair": "EURPLN", "Uic": _EURPLN_UIC},
        {"CurrencyPair": "EURUSD", "Uic": 21},
    ]
}

_FX_INFOPRICE = {
    "AssetType": "FxSpot",
    "Uic": _EURPLN_UIC,
    "Quote": {
        "Bid": 4.3331,
        "Ask": 4.3469,
        "Mid": 4.34,
        "PriceTypeBid": "Tradable",
        "PriceTypeAsk": "Tradable",
        "MarketState": "Open",
    },
}


class _StubFxClient:
    """Duck-typed SaxoClient stand-in for the FX read path."""

    def __init__(
        self,
        *,
        currency_pairs: dict[str, Any] | None = None,
        infoprice: dict[str, Any] | None = None,
        fx_search: dict[str, Any] | None = None,
        fail_with: Exception | None = None,
    ):
        self.currency_pairs = currency_pairs if currency_pairs is not None else _CURRENCY_PAIRS
        self.infoprice = infoprice if infoprice is not None else _FX_INFOPRICE
        self.fx_search = fx_search if fx_search is not None else {"Data": []}
        self.fail_with = fail_with
        self.infoprice_calls: list[str] = []
        self.search_calls: list[tuple[str, str]] = []

    def _maybe_fail(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    def get_currency_pairs(self) -> dict[str, Any]:
        self._maybe_fail()
        return self.currency_pairs

    def get_fx_infoprice(self, uic: int | str) -> dict[str, Any]:
        self._maybe_fail()
        self.infoprice_calls.append(str(uic))
        return self.infoprice

    def search_instruments(
        self,
        keywords: str,
        *,
        asset_types: str = "Stock",
        exchange_id: str | None = None,
    ) -> dict[str, Any]:
        self._maybe_fail()
        self.search_calls.append((keywords, asset_types))
        return self.fx_search


def _broker(**kw: Any) -> tuple[SaxoBroker, _StubFxClient]:
    client = _StubFxClient(**kw)
    return SaxoBroker(client), client  # type: ignore[arg-type]


class TestGetFxRateHappyPath(unittest.TestCase):
    def test_quote_reported_verbatim_with_provenance(self):
        broker, client = _broker()

        quote = broker.get_fx_rate("EUR", "PLN")

        self.assertEqual(client.infoprice_calls, [str(_EURPLN_UIC)])
        self.assertEqual(quote.base_currency, "EUR")
        self.assertEqual(quote.quote_currency, "PLN")
        self.assertEqual(quote.mid, 4.34)
        self.assertEqual(quote.bid, 4.3331)
        self.assertEqual(quote.ask, 4.3469)
        self.assertEqual(quote.price_type_bid, "Tradable")
        self.assertEqual(quote.price_type_ask, "Tradable")
        self.assertEqual(quote.market_state, "Open")
        self.assertEqual(quote.source, f"saxo-fxspot-uic-{_EURPLN_UIC}-mid")
        self.assertIsNotNone(quote.asof.tzinfo, "asof must be tz-aware UTC (local clock)")

    def test_lowercase_input_normalized(self):
        broker, _ = _broker()
        quote = broker.get_fx_rate("eur", "pln")
        self.assertEqual((quote.base_currency, quote.quote_currency), ("EUR", "PLN"))

    def test_missing_quote_fields_pass_through_as_none_not_filtered(self):
        # The adapter reports; refusal happens in execution.build_fx_conversion.
        broker, _ = _broker(
            infoprice={"AssetType": "FxSpot", "Uic": _EURPLN_UIC, "Quote": {"Bid": 4.3331}}
        )
        quote = broker.get_fx_rate("EUR", "PLN")
        self.assertIsNone(quote.mid)
        self.assertIsNone(quote.price_type_bid)
        self.assertIsNone(quote.market_state)


class TestFxPairResolution(unittest.TestCase):
    def test_keywords_fallback_when_pair_not_listed_under_base(self):
        # One-directional listing: PLNEUR is not listed; the broker must NOT
        # invert EURPLN — a base-side miss falls back to an FxSpot search.
        broker, client = _broker(
            currency_pairs={"Data": [{"CurrencyPair": "EURUSD", "Uic": 21}]},
            fx_search={"Data": [{"Symbol": "EURPLN", "Identifier": _EURPLN_UIC}]},
        )

        quote = broker.get_fx_rate("EUR", "PLN")

        self.assertEqual(client.search_calls, [("EURPLN", "FxSpot")])
        self.assertEqual(quote.source, f"saxo-fxspot-uic-{_EURPLN_UIC}-mid")

    def test_unresolvable_pair_is_a_refusal(self):
        broker, _ = _broker(currency_pairs={"Data": []}, fx_search={"Data": []})
        with self.assertRaises(InstrumentNotFoundError) as ctx:
            broker.get_fx_rate("EUR", "PLN")
        self.assertIn("EUR->PLN", str(ctx.exception))

    def test_ambiguous_fallback_matches_are_a_refusal(self):
        broker, _ = _broker(
            currency_pairs={"Data": []},
            fx_search={
                "Data": [
                    {"Symbol": "EURPLN", "Identifier": 1343},
                    {"Symbol": "EURPLN", "Identifier": 9999},
                ]
            },
        )
        with self.assertRaises(InstrumentNotFoundError):
            broker.get_fx_rate("EUR", "PLN")


class TestFxCapabilityBoundary(unittest.TestCase):
    def test_get_fx_rate_is_not_a_protocol_member(self):
        # The frozen Broker Protocol stays currency-naive; get_fx_rate is a
        # vendor capability reached via the getattr pattern.
        self.assertFalse(hasattr(Broker, "get_fx_rate"))

    def test_vendor_errors_translate_at_the_boundary(self):
        broker, _ = _broker(fail_with=SaxoError("fx 500"))
        with self.assertRaises(BrokerError) as ctx:
            broker.get_fx_rate("EUR", "PLN")
        self.assertNotIsInstance(ctx.exception, SaxoError)


if __name__ == "__main__":
    unittest.main()
