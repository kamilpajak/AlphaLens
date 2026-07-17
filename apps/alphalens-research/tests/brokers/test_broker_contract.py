"""Contract tests for the broker-agnostic surface (``brokers/contract.py``).

Three concerns:

1. **Dataclass semantics** — every contract dataclass is frozen (execution
   records are facts, not mutable state) and the ``OrderStatus`` enum covers
   the terminal + working states P2/P3 will branch on.
2. **Error taxonomy** — every broker error is a ``BrokerError`` is a
   ``RuntimeError``, so a consumer can catch the base class without importing
   any vendor module.
3. **Behavioral conformance** — a Protocol pins signatures but not semantics.
   :class:`BrokerConformanceMixin` documents what any adapter must actually DO
   (return the contract dataclasses, raise ``InstrumentNotFoundError`` on an
   unknown ticker, ...). It runs here against an in-memory ``FakeBroker``; a
   second broker (IBKR) gets conformance for free by subclassing the mixin
   with its own ``make_broker``.

Plus the registry: lazy factory resolution, env-var default, double-checked
singleton + test reset hook, and a loud error on an unknown broker name.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import unittest
from unittest import mock

from alphalens_pipeline.brokers import registry
from alphalens_pipeline.brokers.contract import (
    AccountSnapshot,
    BracketOrderRequest,
    Broker,
    BrokerAuthError,
    BrokerCapabilityError,
    BrokerError,
    BrokerRateLimitError,
    InstrumentNotFoundError,
    InstrumentRef,
    OrderRejectedError,
    OrderState,
    OrderStatus,
    PlacedOrder,
    Position,
)


def _instrument(ticker: str = "KO", mic: str = "XNYS") -> InstrumentRef:
    return InstrumentRef(
        ticker=ticker.upper(),
        exchange_mic=mic.upper(),
        asset_type="Stock",
        broker_instrument_id="211",
        broker_symbol=f"{ticker.lower()}:{mic.lower()}",
    )


class FakeBroker:
    """Minimal in-memory :class:`Broker` used by the conformance mixin.

    Doubles as the registry's test factory target (see ``_make_fake_broker``).
    """

    name = "fake"

    UNKNOWN_TICKER = "ZZZZZZ"

    def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(
            account_id="FAKE-ACC",
            currency="USD",
            cash=1_000.0,
            total_value=1_500.0,
            margin_available=None,
            asof=dt.datetime.now(dt.UTC),
        )

    def get_positions(self) -> list[Position]:
        return [
            Position(
                instrument=self.resolve_instrument("KO"),
                quantity=10.0,
                avg_price=50.0,
                market_value=None,
                unrealized_pnl=None,
                position_id="pos-1",
            )
        ]

    def resolve_instrument(self, ticker: str, exchange_mic: str = "XNYS") -> InstrumentRef:
        if ticker.upper() == self.UNKNOWN_TICKER:
            raise InstrumentNotFoundError(f"no instrument for ({ticker}, {exchange_mic})")
        return _instrument(ticker, exchange_mic)

    def place_bracket_order(self, request: BracketOrderRequest) -> PlacedOrder:
        raise BrokerCapabilityError("FakeBroker does not place orders")

    def get_order(self, order_id: str) -> OrderState:
        raise BrokerCapabilityError("FakeBroker has no orders")

    def list_open_orders(self) -> list[OrderState]:
        raise BrokerCapabilityError("FakeBroker has no orders")

    def cancel_order(self, order_id: str) -> None:
        raise BrokerCapabilityError("FakeBroker has no orders")


def _make_fake_broker() -> FakeBroker:
    """Registry factory target for the tests below (import-path resolved)."""
    return FakeBroker()


class TestContractDataclassesFrozen(unittest.TestCase):
    def test_every_contract_dataclass_is_frozen(self):
        samples = [
            _instrument(),
            AccountSnapshot(
                account_id="A",
                currency="USD",
                cash=0.0,
                total_value=0.0,
                margin_available=None,
                asof=dt.datetime.now(dt.UTC),
            ),
            Position(
                instrument=_instrument(),
                quantity=1.0,
                avg_price=1.0,
                market_value=None,
                unrealized_pnl=None,
                position_id="p",
            ),
            OrderState(
                order_id="o",
                status=OrderStatus.WORKING,
                instrument=None,
                filled_quantity=0.0,
                raw_status="Working",
            ),
            BracketOrderRequest(
                instrument=_instrument(),
                side="BUY",
                quantity=1,
                entry_limit=10.0,
                stop_loss=9.0,
                take_profit=12.0,
                entry_ttl_days=5,
                client_request_id="uuid",
            ),
            PlacedOrder(entry_order_id="e", exit_order_ids=("x1", "x2")),
        ]
        for sample in samples:
            first_field = dataclasses.fields(sample)[0].name
            with self.assertRaises(
                dataclasses.FrozenInstanceError,
                msg=f"{type(sample).__name__} must be frozen",
            ):
                setattr(sample, first_field, "mutated")

    def test_order_status_enum_members(self):
        expected = {
            "WORKING",
            "PARTIALLY_FILLED",
            "FILLED",
            "CANCELLED",
            "REJECTED",
            "EXPIRED",
            "UNKNOWN",
        }
        self.assertEqual({s.name for s in OrderStatus}, expected)
        for status in OrderStatus:
            self.assertEqual(status.value, status.name)


class TestErrorTaxonomy(unittest.TestCase):
    def test_every_broker_error_is_broker_error_and_runtime_error(self):
        for exc_type in (
            BrokerAuthError,
            BrokerRateLimitError,
            InstrumentNotFoundError,
            OrderRejectedError,
            BrokerCapabilityError,
        ):
            with self.subTest(exc=exc_type.__name__):
                exc = exc_type("boom")
                self.assertIsInstance(exc, BrokerError)
                self.assertIsInstance(exc, RuntimeError)

    def test_subclasses_stay_distinct(self):
        """Catching one leaf must not swallow a sibling leaf."""
        self.assertNotIsInstance(BrokerAuthError("x"), BrokerRateLimitError)
        self.assertNotIsInstance(InstrumentNotFoundError("x"), BrokerAuthError)


class BrokerConformanceMixin:
    """Behavioral contract any :class:`Broker` adapter must pass.

    Subclass with ``unittest.TestCase`` and implement :meth:`make_broker`;
    set ``unknown_ticker`` to a ticker the broker cannot resolve.
    """

    unknown_ticker = "ZZZZZZ"
    known_ticker = "KO"
    known_mic = "XNYS"

    def make_broker(self) -> Broker:
        raise NotImplementedError

    def test_satisfies_broker_protocol(self):
        broker = self.make_broker()
        self.assertIsInstance(broker, Broker)
        self.assertIsInstance(broker.name, str)
        self.assertTrue(broker.name)

    def test_get_account_returns_account_snapshot(self):
        snapshot = self.make_broker().get_account()
        self.assertIsInstance(snapshot, AccountSnapshot)
        self.assertTrue(snapshot.account_id)
        self.assertIsNotNone(snapshot.asof.tzinfo, "asof must be tz-aware UTC")

    def test_get_positions_returns_position_list(self):
        positions = self.make_broker().get_positions()
        self.assertIsInstance(positions, list)
        for position in positions:
            self.assertIsInstance(position, Position)
            self.assertIsInstance(position.instrument, InstrumentRef)

    def test_resolve_known_instrument(self):
        ref = self.make_broker().resolve_instrument(self.known_ticker, self.known_mic)
        self.assertIsInstance(ref, InstrumentRef)
        self.assertEqual(ref.ticker, self.known_ticker.upper())
        self.assertEqual(ref.exchange_mic, self.known_mic.upper())
        self.assertTrue(ref.broker_instrument_id)

    def test_resolve_unknown_instrument_raises_contract_error(self):
        with self.assertRaises(InstrumentNotFoundError):
            self.make_broker().resolve_instrument(self.unknown_ticker, self.known_mic)


class TestFakeBrokerConformance(BrokerConformanceMixin, unittest.TestCase):
    def make_broker(self) -> Broker:
        return FakeBroker()


class TestRegistry(unittest.TestCase):
    def setUp(self):
        registry._reset_default_broker_for_tests()
        self.addCleanup(registry._reset_default_broker_for_tests)

    def test_unknown_broker_name_raises_with_known_names(self):
        with self.assertRaises(ValueError) as ctx:
            registry.get_default_broker("nonexistent")
        self.assertIn("nonexistent", str(ctx.exception))
        self.assertIn("saxo", str(ctx.exception))

    def test_misregistered_factory_path_raises_broker_error_not_importlib(self):
        # A typo'd module path or attribute must surface as BrokerError (the
        # CLI renders it cleanly), never a raw ImportError/AttributeError.
        for spec in ("no.such.module:factory", f"{__name__}:no_such_attr"):
            with mock.patch.dict(registry._BROKER_FACTORIES, {"broken": spec}):
                with self.assertRaises(BrokerError) as ctx:
                    registry.get_default_broker("broken")
                self.assertIn("broken", str(ctx.exception))
                registry._reset_default_broker_for_tests()

    def test_factory_resolution_and_singleton(self):
        with mock.patch.dict(
            registry._BROKER_FACTORIES,
            {"fake": f"{__name__}:_make_fake_broker"},
        ):
            first = registry.get_default_broker("fake")
            self.assertIsInstance(first, FakeBroker)
            self.assertIs(registry.get_default_broker("fake"), first, "must be a singleton")
            registry._reset_default_broker_for_tests()
            self.assertIsNot(
                registry.get_default_broker("fake"), first, "reset must clear the singleton"
            )

    def test_env_var_selects_default_broker(self):
        with (
            mock.patch.dict(
                registry._BROKER_FACTORIES,
                {"fake": f"{__name__}:_make_fake_broker"},
            ),
            mock.patch.dict("os.environ", {"ALPHALENS_BROKER": "fake"}),
        ):
            self.assertIsInstance(registry.get_default_broker(), FakeBroker)

    def test_default_name_is_saxo(self):
        """Without ALPHALENS_BROKER the registry resolves the saxo factory.

        The saxo factory needs SAXO_SIM_TOKEN; with the env scrubbed the
        construction fails with the auth-shaped error — proving the NAME
        resolution went to saxo without needing a live token.
        """
        from alphalens_pipeline.brokers.saxo.client import _reset_default_client_for_tests

        _reset_default_client_for_tests()
        self.addCleanup(_reset_default_client_for_tests)
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(BrokerAuthError):
                registry.get_default_broker()


if __name__ == "__main__":
    unittest.main()
