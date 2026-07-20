"""Hermetic tests for the SaxoBroker contract adapter (``brokers/saxo/broker.py``).

The adapter is tested against a stub SaxoClient carrying canned Saxo OpenAPI
payload fixtures (portfolio ``/port/v1/*`` + reference ``/ref/v1/instruments``
shapes). Pins:

- payload -> ``AccountSnapshot`` / ``Position`` / ``InstrumentRef`` translation
- resolve: exact-symbol match, zero-match and ambiguity ->
  ``InstrumentNotFoundError``, unmapped MIC -> ``InstrumentNotFoundError``
- FIFO instrument cache: a second resolve makes no HTTP call
- ``Saxo*Error`` -> ``Broker*Error`` translation at the adapter boundary
  (vendor exceptions never escape ``brokers/saxo/``)
- multi-account without ``SAXO_ACCOUNT_KEY`` fails loudly

The P2 order surface has its own suite (``test_saxo_broker_orders.py``); the
stub here carries just enough stateful order book for the shared
conformance-mixin lifecycle pin.

Also runs the shared :class:`BrokerConformanceMixin` against SaxoBroker so the
real adapter proves the same behavioral contract as any future broker.
"""

from __future__ import annotations

import unittest
from typing import Any

from alphalens_pipeline.brokers.contract import (
    AccountSnapshot,
    Broker,
    BrokerAuthError,
    BrokerError,
    BrokerRateLimitError,
    InstrumentNotFoundError,
    InstrumentRef,
    OrderStatus,
    Position,
)
from alphalens_pipeline.brokers.saxo.broker import SaxoBroker
from alphalens_pipeline.brokers.saxo.client import (
    SaxoAuthError,
    SaxoError,
    SaxoRateLimitError,
)

from tests.brokers.test_broker_contract import BrokerConformanceMixin

_CLIENT_INFO = {"ClientKey": "CK-1", "DefaultAccountKey": "AK-1"}

_ACCOUNTS_SINGLE = {"Data": [{"AccountKey": "AK-1", "AccountId": "16371XYZ", "Currency": "USD"}]}

_ACCOUNTS_MULTI = {
    "Data": [
        {"AccountKey": "AK-1", "AccountId": "16371XYZ", "Currency": "USD"},
        {"AccountKey": "AK-2", "AccountId": "16371ABC", "Currency": "PLN"},
    ]
}

_BALANCES = {
    "Currency": "USD",
    "CashBalance": 10_000.0,
    "TotalValue": 12_500.5,
    "MarginAvailableForTrading": 9_000.0,
}

_POSITIONS = {
    "Data": [
        {
            "PositionId": "5001",
            "PositionBase": {
                "Amount": 10.0,
                "OpenPrice": 50.0,
                "Uic": 211,
                "AssetType": "Stock",
            },
            "PositionView": {"MarketValue": 510.0, "ProfitLossOnTrade": 10.0},
            "DisplayAndFormat": {"Symbol": "KO:xnys", "Description": "Coca-Cola Co."},
        },
        {
            # SIM NoAccess quote: no PositionView block at all — market_value
            # and unrealized_pnl must map to None, not crash.
            "PositionId": "5002",
            "PositionBase": {
                "Amount": -5.0,
                "OpenPrice": 100.0,
                "Uic": 212,
                "AssetType": "Stock",
            },
            "DisplayAndFormat": {"Symbol": "AAPL:xnas"},
        },
    ]
}

_SEARCH_RESULTS: dict[str, dict[str, Any]] = {
    "KO": {
        "Data": [
            {"Symbol": "KO:xnys", "Identifier": 211, "AssetType": "Stock", "CurrencyCode": "USD"},
            {"Symbol": "KOF:xnys", "Identifier": 999, "AssetType": "Stock", "CurrencyCode": "USD"},
        ]
    },
    "AAPL": {
        "Data": [
            {"Symbol": "AAPL:xnas", "Identifier": 212, "AssetType": "Stock", "CurrencyCode": "USD"}
        ]
    },
    "AMBIG": {
        "Data": [
            {"Symbol": "AMBIG:xnys", "Identifier": 1, "AssetType": "Stock", "CurrencyCode": "USD"},
            {"Symbol": "AMBIG:xnys", "Identifier": 2, "AssetType": "Stock", "CurrencyCode": "USD"},
        ]
    },
    # Live-verified shape: CDR@XWAR carries CurrencyCode PLN (P1 memo).
    "CDR": {
        "Data": [
            {"Symbol": "CDR:xwar", "Identifier": 53932, "AssetType": "Stock", "CurrencyCode": "PLN"}
        ]
    },
    # Defect fixture: a search row WITHOUT CurrencyCode must be refused.
    "NOCCY": {"Data": [{"Symbol": "NOCCY:xnys", "Identifier": 777, "AssetType": "Stock"}]},
}


class _StubSaxoClient:
    """Duck-typed stand-in for SaxoClient (the adapter only calls these five)."""

    def __init__(
        self,
        *,
        accounts: dict[str, Any] | None = None,
        fail_with: Exception | None = None,
    ):
        self.accounts = accounts or _ACCOUNTS_SINGLE
        self.fail_with = fail_with
        self.search_calls: list[tuple[str, str | None]] = []

    def _maybe_fail(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    def get_client_info(self) -> dict[str, Any]:
        self._maybe_fail()
        return dict(_CLIENT_INFO)

    def get_accounts(self) -> dict[str, Any]:
        self._maybe_fail()
        return self.accounts

    def get_balances(self, client_key: str, account_key: str | None = None) -> dict[str, Any]:
        self._maybe_fail()
        assert client_key == "CK-1"
        return dict(_BALANCES)

    def get_positions(self, client_key: str) -> dict[str, Any]:
        self._maybe_fail()
        return getattr(self, "positions_payload", _POSITIONS)

    def search_instruments(
        self,
        keywords: str,
        *,
        asset_types: str = "Stock",
        exchange_id: str | None = None,
    ) -> dict[str, Any]:
        self._maybe_fail()
        self.search_calls.append((keywords, exchange_id))
        return _SEARCH_RESULTS.get(keywords.upper(), {"Data": []})

    # ----- minimal stateful order surface (conformance-mixin lifecycle) -----

    _DETAILS = {
        "Format": {"Decimals": 2, "OrderDecimals": 2},
        "TickSizeScheme": {"DefaultTickSize": 0.01, "Elements": []},
        "SupportedOrderTypes": ["Limit", "Market", "StopIfTraded"],
    }

    def get_instrument_details(self, uic: int | str, asset_type: str = "Stock") -> dict[str, Any]:
        self._maybe_fail()
        return dict(self._DETAILS)

    def precheck_order(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        self._maybe_fail()
        return 200, {"PreCheckResult": "Ok"}

    def place_order(self, body: dict[str, Any], *, request_id: str) -> tuple[int, dict[str, Any]]:
        self._maybe_fail()
        self._order_seq = getattr(self, "_order_seq", 0) + 1
        entry_id = f"E-{self._order_seq}"
        children = [{"OrderId": f"{entry_id}-x{i}"} for i in range(len(body.get("Orders") or []))]
        self._orders = getattr(self, "_orders", {})
        self._orders[entry_id] = {
            "OrderId": entry_id,
            "Status": "Working",
            "Amount": body["Amount"],
            "FillAmount": 0.0,
            "Uic": body["Uic"],
        }
        return 201, {"OrderId": entry_id, "Orders": children}

    def cancel_order_ids(self, order_ids: str, *, account_key: str) -> tuple[int, dict[str, Any]]:
        self._maybe_fail()
        self._orders = getattr(self, "_orders", {})
        self._orders.pop(order_ids, None)
        return 200, {}

    def get_open_orders(self) -> dict[str, Any]:
        self._maybe_fail()
        return {"Data": list(getattr(self, "_orders", {}).values())}

    def get_order_status(self, client_key: str, order_id: str) -> dict[str, Any] | None:
        self._maybe_fail()
        return getattr(self, "_orders", {}).get(order_id)

    # ----- P3 audit / cross-check surface -----

    def get_order_activities(
        self,
        client_key: str,
        *,
        order_id: str | None = None,
        entry_type: str = "Last",
        from_datetime: str | None = None,
        top: int | None = None,
    ) -> dict[str, Any]:
        self._maybe_fail()
        rows = getattr(self, "activities", {}).get(order_id or "", [])
        return {"__count": len(rows), "Data": list(rows)}

    def get_closed_positions(self, client_key: str) -> dict[str, Any]:
        self._maybe_fail()
        return getattr(self, "closed_positions_payload", {"__count": 0, "Data": []})


def _make_broker(**kw: Any) -> SaxoBroker:
    return SaxoBroker(_StubSaxoClient(), **kw)  # type: ignore[arg-type]


class TestAccountSnapshotTranslation(unittest.TestCase):
    def test_single_account_maps_balances(self):
        snapshot = _make_broker().get_account()

        self.assertIsInstance(snapshot, AccountSnapshot)
        self.assertEqual(snapshot.account_id, "AK-1")
        self.assertEqual(snapshot.currency, "USD")
        self.assertEqual(snapshot.cash, 10_000.0)
        self.assertEqual(snapshot.total_value, 12_500.5)
        self.assertEqual(snapshot.margin_available, 9_000.0)
        self.assertIsNotNone(snapshot.asof.tzinfo)

    def test_multi_account_without_selector_fails_loudly(self):
        broker = SaxoBroker(_StubSaxoClient(accounts=_ACCOUNTS_MULTI))  # type: ignore[arg-type]
        with self.assertRaises(BrokerError) as ctx:
            broker.get_account()
        self.assertIn("SAXO_ACCOUNT_KEY", str(ctx.exception))

    def test_multi_account_with_selector_picks_the_key(self):
        broker = SaxoBroker(
            _StubSaxoClient(accounts=_ACCOUNTS_MULTI),  # type: ignore[arg-type]
            account_key="AK-2",
        )
        self.assertEqual(broker.get_account().account_id, "AK-2")

    def test_selector_not_in_accounts_fails_loudly(self):
        broker = SaxoBroker(_StubSaxoClient(), account_key="AK-MISSING")  # type: ignore[arg-type]
        with self.assertRaises(BrokerError):
            broker.get_account()


class TestPositionsTranslation(unittest.TestCase):
    def test_positions_map_to_contract_dataclasses(self):
        positions = _make_broker().get_positions()

        self.assertEqual(len(positions), 2)
        first, second = positions
        self.assertIsInstance(first, Position)
        self.assertEqual(first.position_id, "5001")
        self.assertEqual(first.quantity, 10.0)
        self.assertEqual(first.avg_price, 50.0)
        self.assertEqual(first.market_value, 510.0)
        self.assertEqual(first.unrealized_pnl, 10.0)
        self.assertEqual(first.instrument.ticker, "KO")
        self.assertEqual(first.instrument.exchange_mic, "XNYS")
        self.assertEqual(first.instrument.broker_instrument_id, "211")
        self.assertEqual(first.instrument.broker_symbol, "KO:xnys")

        # SIM NoAccess quote (no PositionView) -> None, and short qty stays signed.
        self.assertEqual(second.quantity, -5.0)
        self.assertIsNone(second.market_value)
        self.assertIsNone(second.unrealized_pnl)


class TestInstrumentResolution(unittest.TestCase):
    def test_exact_symbol_match_wins_over_prefix_sibling(self):
        broker = _make_broker()
        ref = broker.resolve_instrument("KO", "XNYS")

        self.assertIsInstance(ref, InstrumentRef)
        self.assertEqual(ref.ticker, "KO")
        self.assertEqual(ref.exchange_mic, "XNYS")
        self.assertEqual(ref.broker_instrument_id, "211")
        self.assertEqual(ref.broker_symbol, "KO:xnys")
        self.assertEqual(ref.asset_type, "Stock")
        self.assertEqual(ref.currency, "USD", "CurrencyCode stamped at resolve time")

    def test_resolve_stamps_pln_currency_for_wse_listing(self):
        ref = _make_broker().resolve_instrument("CDR", "XWAR")
        self.assertEqual(ref.currency, "PLN")

    def test_missing_currency_code_is_a_refusal_not_a_guess(self):
        # Authoritative instrument currency comes ONLY from Saxo's own row —
        # never MIC-inferred (FX-leg memo §4.3 item 4).
        with self.assertRaises(InstrumentNotFoundError) as ctx:
            _make_broker().resolve_instrument("NOCCY", "XNYS")
        self.assertIn("CurrencyCode", str(ctx.exception))

    def test_search_passes_saxo_exchange_id_for_mic(self):
        client = _StubSaxoClient()
        SaxoBroker(client).resolve_instrument("AAPL", "XNAS")  # type: ignore[arg-type]
        self.assertEqual(client.search_calls, [("AAPL", "NASDAQ")])

    def test_zero_match_raises_instrument_not_found(self):
        with self.assertRaises(InstrumentNotFoundError):
            _make_broker().resolve_instrument("NOPE", "XNYS")

    def test_ambiguous_match_raises_instrument_not_found(self):
        with self.assertRaises(InstrumentNotFoundError) as ctx:
            _make_broker().resolve_instrument("AMBIG", "XNYS")
        self.assertIn("ambiguous", str(ctx.exception).lower())

    def test_unmapped_mic_raises_instrument_not_found(self):
        with self.assertRaises(InstrumentNotFoundError) as ctx:
            _make_broker().resolve_instrument("KO", "XTKS")
        self.assertIn("XTKS", str(ctx.exception))

    def test_cache_hit_skips_http(self):
        client = _StubSaxoClient()
        broker = SaxoBroker(client)  # type: ignore[arg-type]

        first = broker.resolve_instrument("KO", "XNYS")
        second = broker.resolve_instrument("KO", "XNYS")

        self.assertEqual(first, second)
        self.assertEqual(len(client.search_calls), 1, "second resolve must hit the cache")

    def test_cache_is_fifo_bounded(self):
        client = _StubSaxoClient()
        broker = SaxoBroker(client, cache_size=1)  # type: ignore[arg-type]

        broker.resolve_instrument("KO", "XNYS")
        broker.resolve_instrument("AAPL", "XNAS")  # evicts KO (FIFO, size 1)
        broker.resolve_instrument("KO", "XNYS")

        self.assertEqual(len(client.search_calls), 3, "evicted entry must re-fetch")


class TestErrorTranslationBoundary(unittest.TestCase):
    def test_saxo_errors_translate_to_broker_errors(self):
        cases = [
            (SaxoAuthError("401"), BrokerAuthError),
            (SaxoRateLimitError("429"), BrokerRateLimitError),
            (SaxoError("boom"), BrokerError),
        ]
        for saxo_exc, broker_exc_type in cases:
            with self.subTest(saxo=type(saxo_exc).__name__):
                broker = SaxoBroker(_StubSaxoClient(fail_with=saxo_exc))  # type: ignore[arg-type]
                with self.assertRaises(broker_exc_type) as ctx:
                    broker.get_account()
                # The vendor exception must never escape raw.
                self.assertNotIsInstance(ctx.exception, SaxoError)

    def test_translation_covers_positions_and_resolve(self):
        broker = SaxoBroker(_StubSaxoClient(fail_with=SaxoAuthError("401")))  # type: ignore[arg-type]
        with self.assertRaises(BrokerAuthError):
            broker.get_positions()
        with self.assertRaises(BrokerAuthError):
            broker.resolve_instrument("KO", "XNYS")


# ---------------------------------------------------------------------------
# P3 terminal resolution — audit /cs/v1/audit/orderactivities classifier.
# Fixture rows mirror the VERBATIM shapes captured on the live SIM account
# 2026-07-17 (cancelled brackets 5039272886/..., rejected placements
# 5039272858/5039272868); FinalFill and Expired rows are DOC-SOURCED (both
# sweeps returned __count:0 live — zero fills/expiries existed), so the
# parser must stay guarded (UNRESOLVED on missing fill fields, never a
# fabricated quantity).
# ---------------------------------------------------------------------------

_AUDIT_ROW_COMMON = {
    "AccountId": "16371XYZ",
    "AccountKey": "AK-1",
    "AssetType": "Stock",
    "BuySell": "Buy",
    "ClientKey": "CK-1",
    "CorrelationKey": "corr-1",
    "Duration": {"DurationType": "GoodTillDate"},
    "HandledBy": "1000001",
    "OrderRelation": "StandAlone",
    "OrderType": "Limit",
    "Uic": 211,
}


def _audit_row(**overrides: Any) -> dict[str, Any]:
    row = dict(_AUDIT_ROW_COMMON)
    row.update(overrides)
    return row


# Live-verified: LogId 249474866 — Cancelled/Confirmed terminal row of entry
# 5039272886 (after Placed/Requested -> Placed/Confirmed lifecycle rows).
_ROW_CANCELLED_CONFIRMED = _audit_row(
    OrderId="5039272886",
    LogId=249474866,
    ActivityTime="2026-07-17T11:42:10.360000Z",
    Amount=10.0,
    ExternalReference="rid-cancelled",
    Status="Cancelled",
    SubStatus="Confirmed",
)

# Live-verified: rejected placement 5039272858 is a SINGLE audit row with
# Status="Placed" and SubStatus="Rejected" — Status alone is insufficient.
_ROW_PLACED_REJECTED = _audit_row(
    OrderId="5039272858",
    LogId=249474801,
    ActivityTime="2026-07-17T11:38:02.120000Z",
    Amount=10.0,
    ExternalReference="rid-rejected",
    Status="Placed",
    SubStatus="Rejected",
)

# Doc-sourced terminal fill row (FilledAmount cumulative, FillAmount per-event).
_ROW_FINAL_FILL = _audit_row(
    OrderId="5039272900",
    LogId=249474900,
    ActivityTime="2026-07-17T14:30:00.000000Z",
    Amount=10.0,
    FilledAmount=10.0,
    FillAmount=4.0,
    AveragePrice=50.25,
    ExternalReference="rid-filled",
    Status="FinalFill",
    SubStatus="Confirmed",
)

# Doc-sourced expiry row (GTD lapsed) — not live-producible on 2026-07-17.
_ROW_EXPIRED = _audit_row(
    OrderId="5039272910",
    LogId=249474910,
    ActivityTime="2026-07-20T20:00:00.000000Z",
    Amount=10.0,
    ExternalReference="rid-expired",
    Status="Expired",
    SubStatus="Confirmed",
)

# Non-terminal last row for an order ABSENT from /port/v1/orders/me — an
# inconsistent state the resolver must surface honestly, never guess.
_ROW_PLACED_CONFIRMED = _audit_row(
    OrderId="5039272920",
    LogId=249474920,
    ActivityTime="2026-07-17T11:40:00.000000Z",
    Amount=10.0,
    ExternalReference="rid-working",
    Status="Placed",
    SubStatus="Confirmed",
)


class TestResolveOrderOutcome(unittest.TestCase):
    """(Status, SubStatus) PAIR classifier over EntryType=Last audit rows."""

    def _broker_with(self, activities: dict[str, list[dict[str, Any]]]) -> SaxoBroker:
        client = _StubSaxoClient()
        client.activities = activities  # type: ignore[attr-defined]
        return SaxoBroker(client)  # type: ignore[arg-type]

    def _resolve(self, row: dict[str, Any]) -> Any:
        order_id = str(row["OrderId"])
        return self._broker_with({order_id: [row]}).resolve_order_outcome(order_id)

    def test_cancelled_confirmed_maps_cancelled(self):
        state = self._resolve(_ROW_CANCELLED_CONFIRMED)
        self.assertEqual(state.status, OrderStatus.CANCELLED)
        self.assertEqual(state.order_id, "5039272886")
        self.assertIn("Cancelled/Confirmed", state.raw_status)

    def test_placed_rejected_maps_rejected_never_status_alone(self):
        # Pins that the classifier branches on the PAIR: Status is "Placed"
        # here, yet the row is terminal-rejected.
        state = self._resolve(_ROW_PLACED_REJECTED)
        self.assertEqual(state.status, OrderStatus.REJECTED)
        self.assertIn("Placed/Rejected", state.raw_status)

    def test_final_fill_maps_filled_with_cumulative_filled_amount(self):
        state = self._resolve(_ROW_FINAL_FILL)
        self.assertEqual(state.status, OrderStatus.FILLED)
        self.assertEqual(state.filled_quantity, 10.0, "FilledAmount (cumulative) wins")
        self.assertIn("FinalFill/Confirmed", state.raw_status)

    def test_final_fill_falls_back_to_per_event_fill_amount(self):
        row = dict(_ROW_FINAL_FILL)
        del row["FilledAmount"]
        state = self._resolve(row)
        self.assertEqual(state.status, OrderStatus.FILLED)
        self.assertEqual(state.filled_quantity, 4.0)

    def test_final_fill_missing_fill_fields_is_unresolved_not_fabricated(self):
        row = dict(_ROW_FINAL_FILL)
        del row["FilledAmount"]
        del row["FillAmount"]
        state = self._resolve(row)
        self.assertEqual(state.status, OrderStatus.UNKNOWN)
        self.assertIn("fill_fields_unverified", state.raw_status)
        self.assertEqual(state.filled_quantity, 0.0)

    def test_expired_maps_expired(self):
        state = self._resolve(_ROW_EXPIRED)
        self.assertEqual(state.status, OrderStatus.EXPIRED)
        self.assertIn("Expired", state.raw_status)

    def test_non_terminal_last_row_is_unresolved_inconsistent_state(self):
        state = self._resolve(_ROW_PLACED_CONFIRMED)
        self.assertEqual(state.status, OrderStatus.UNKNOWN)
        self.assertIn("inconsistent_state", state.raw_status)
        self.assertIn("Placed/Confirmed", state.raw_status)

    def test_zero_count_is_unresolved_not_in_retention(self):
        state = self._broker_with({}).resolve_order_outcome("5039279999")
        self.assertEqual(state.status, OrderStatus.UNKNOWN)
        self.assertIn("not_in_retention", state.raw_status)

    def test_unrecognized_pair_is_unresolved_unrecognized(self):
        row = _audit_row(
            OrderId="5039272930",
            LogId=249474930,
            Status="Triggered",
            SubStatus="Weird",
        )
        state = self._resolve(row)
        self.assertEqual(state.status, OrderStatus.UNKNOWN)
        self.assertIn("unrecognized", state.raw_status)
        self.assertIn("Triggered/Weird", state.raw_status)

    def test_multiple_rows_classifies_the_highest_log_id(self):
        rows = [
            _audit_row(
                OrderId="5039272886",
                LogId=249474860,
                Status="Placed",
                SubStatus="Confirmed",
            ),
            _ROW_CANCELLED_CONFIRMED,
        ]
        state = self._broker_with({"5039272886": rows}).resolve_order_outcome("5039272886")
        self.assertEqual(state.status, OrderStatus.CANCELLED)

    def test_vendor_errors_translate_at_the_boundary(self):
        client = _StubSaxoClient(fail_with=SaxoError("audit 500"))
        broker = SaxoBroker(client)  # type: ignore[arg-type]
        with self.assertRaises(BrokerError):
            broker.resolve_order_outcome("5039272886")


# Byte-shaped from the REAL SIM FinalFill audit row captured 2026-07-20
# (first-fill experiment, entry order 5039287596 —
# ~/.alphalens/broker_orders/experiments/first_fill_2026-07-20/11_entry_activities_all.json,
# LogId 249519481). Retires the "doc-sourced only" caveat: this is the exact
# on-the-wire shape (FillAmount/FilledAmount==2.0, ExecutionPrice/AveragePrice
# ==82.09, ExternalReference==client_request_id, SubStatus="Confirmed").
_ROW_FINAL_FILL_REAL = {
    "AccountId": "22494807",
    "ActivityTime": "2026-07-20T14:09:05.447000Z",
    "Amount": 2.0,
    "AssetType": "Stock",
    "AveragePrice": 82.09,
    "BuySell": "Buy",
    "ClientId": "22494807",
    "CorrelationKey": "1c3ce5d3-db0d-4d16-8cdd-51df8fb79254",
    "Duration": {"DurationType": "GoodTillDate", "ExpirationDate": "2026-07-21T00:00:00.000000Z"},
    "ExecutionPrice": 82.09,
    "ExternalReference": "87e0ab88-c1f2-4e88-b5b8-8fbbbb6e1a6d",
    "FillAmount": 2.0,
    "FilledAmount": 2.0,
    "HandledBy": "22494807",
    "LogId": "249519481",
    "OrderId": "5039287596",
    "OrderRelation": "IfDoneMaster",
    "OrderType": "Limit",
    "PositionId": "5026930126",
    "Price": 82.86,
    "RelatedOrders": ["5039287597", "5039287598"],
    "Status": "FinalFill",
    "SubStatus": "Confirmed",
    "Uic": 307,
    "UserId": "22494807",
}


class TestFinalFillRealFixture(unittest.TestCase):
    """G5: the FinalFill classifier against a REAL SIM fill row (not doc-sourced)."""

    def _resolve(self, row: dict[str, Any]) -> Any:
        client = _StubSaxoClient()
        order_id = str(row["OrderId"])
        client.activities = {order_id: [row]}  # type: ignore[attr-defined]
        return SaxoBroker(client).resolve_order_outcome(order_id)  # type: ignore[arg-type]

    def test_real_final_fill_row_classifies_filled_with_parsed_quantity(self):
        state = self._resolve(_ROW_FINAL_FILL_REAL)
        self.assertEqual(state.status, OrderStatus.FILLED)
        self.assertEqual(state.order_id, "5039287596")
        # FilledAmount (cumulative) is the parsed fill quantity on the real row.
        self.assertEqual(state.filled_quantity, 2.0)
        self.assertIn("FinalFill/Confirmed", state.raw_status)
        # LogId of the real row is surfaced in the diagnostics string.
        self.assertIn("249519481", state.raw_status)

    def test_real_final_fill_string_typed_fill_fields_still_parse(self):
        # Defensive: if Saxo ever serializes FilledAmount as a string, float()
        # in _classify_activity_row still yields the quantity (not UNRESOLVED).
        row = dict(_ROW_FINAL_FILL_REAL)
        row["FilledAmount"] = "2.0"
        state = self._resolve(row)
        self.assertEqual(state.status, OrderStatus.FILLED)
        self.assertEqual(state.filled_quantity, 2.0)


class TestFillCrossCheckCapability(unittest.TestCase):
    """Raw-row capabilities feeding the reconcile FILLED cross-check."""

    def test_open_position_references_collects_external_references(self):
        client = _StubSaxoClient()
        client.positions_payload = {  # type: ignore[attr-defined]
            "Data": [
                {
                    "PositionId": "5001",
                    "PositionBase": {
                        "Amount": 10.0,
                        "OpenPrice": 50.0,
                        "Uic": 211,
                        "AssetType": "Stock",
                        "ExternalReference": "rid-open-1",
                    },
                    "DisplayAndFormat": {"Symbol": "KO:xnys"},
                },
                {
                    "PositionId": "5002",
                    "PositionBase": {"Amount": 1.0, "OpenPrice": 1.0, "Uic": 212},
                    "DisplayAndFormat": {"Symbol": "AAPL:xnas"},
                },
            ]
        }
        broker = SaxoBroker(client)  # type: ignore[arg-type]
        self.assertEqual(broker.get_open_position_references(), ["rid-open-1"])

    def test_closed_position_rows_flatten_the_envelope(self):
        client = _StubSaxoClient()
        client.closed_positions_payload = {  # type: ignore[attr-defined]
            "__count": 2,
            "Data": [
                {
                    "ClosedPositionUniqueId": "cp-1",
                    "ClosedPosition": {"OpeningExternalReferenceId": "rid-1", "ClosingPrice": 55.0},
                },
                {"OpeningExternalReferenceId": "rid-2", "ClosingPrice": 44.0},
            ],
        }
        broker = SaxoBroker(client)  # type: ignore[arg-type]
        rows = broker.get_closed_position_rows()
        self.assertEqual([row["OpeningExternalReferenceId"] for row in rows], ["rid-1", "rid-2"])


class TestSaxoBrokerConformance(BrokerConformanceMixin, unittest.TestCase):
    unknown_ticker = "NOPE"

    def make_broker(self) -> Broker:
        return _make_broker()


if __name__ == "__main__":
    unittest.main()
