"""Conformance pins for the BrokerClient protocol (issue #388).

PRIMARY assertion = ``inspect.signature`` param-name check per method.
``@runtime_checkable`` isinstance() validates method NAMES only, not
signatures, so isinstance here is only a decorative smoke check.

Run via the research unittest discover harness (NOT pytest):
    uv run python -m unittest discover -s apps/alphalens-research/tests \
        -t apps/alphalens-research
"""

import inspect
import unittest

from alphalens_pipeline.data.alt_data.alpaca_client import AlpacaClient
from alphalens_pipeline.paper.broker import (
    VALID_PLATFORMS,
    BrokerClient,
    get_default_broker_client,
)

# method name -> ordered expected parameter names (excluding self)
_EXPECTED = {
    "submit_limit_order": ["symbol", "qty", "limit_price", "side", "time_in_force"],
    "submit_stop_order": ["symbol", "qty", "stop_price", "side", "time_in_force"],
    "submit_market_order": ["symbol", "qty", "side", "time_in_force"],
    "get_account": [],
    "get_position": ["symbol"],
    "get_order": ["order_id"],
    "cancel_order": ["order_id"],
    # Reset-support enumerate primitives (broker-agnostic state sweep).
    "list_open_orders": [],
    "list_positions": [],
}


def _public_methods(cls):
    return {
        name for name, _ in inspect.getmembers(cls, inspect.isfunction) if not name.startswith("_")
    }


class TestBrokerProtocolSurface(unittest.TestCase):
    def test_protocol_declares_exactly_the_expected_methods(self):
        self.assertEqual(_public_methods(BrokerClient), set(_EXPECTED))

    def test_protocol_method_signatures_match_spec(self):
        for name, expected in _EXPECTED.items():
            with self.subTest(method=name):
                sig = inspect.signature(getattr(BrokerClient, name))
                params = [p for p in sig.parameters if p != "self"]
                self.assertEqual(params, expected)


class TestAlpacaClientConformsStructurally(unittest.TestCase):
    """AlpacaClient must satisfy BrokerClient by signature, no subclassing."""

    def test_alpaca_has_every_protocol_method_with_matching_params(self):
        for name, expected in _EXPECTED.items():
            with self.subTest(method=name):
                self.assertTrue(
                    hasattr(AlpacaClient, name),
                    f"AlpacaClient is missing {name!r}",
                )
                sig = inspect.signature(getattr(AlpacaClient, name))
                params = [p for p in sig.parameters if p != "self"]
                self.assertEqual(params, expected)

    def test_isinstance_runtime_checkable_smoke(self):
        # Decorative: @runtime_checkable matches method NAMES only. A bare
        # object missing the methods must NOT pass.
        self.assertFalse(isinstance(object(), BrokerClient))


class TestConformancePositiveControl(unittest.TestCase):
    """A stub missing the enumerate primitives must NOT pass isinstance.

    Guards the #388 conformance pattern: if someone adds a partial stub
    (the 7 original methods but not the 2 new enumerate primitives), the
    runtime_checkable isinstance check catches the missing names.
    """

    def test_stub_missing_enumerate_primitives_fails_isinstance(self):
        class _PartialBroker:
            def submit_limit_order(self, **kw):  # pragma: no cover - shape only
                ...

            def submit_stop_order(self, **kw):  # pragma: no cover - shape only
                ...

            def submit_market_order(self, **kw):  # pragma: no cover - shape only
                ...

            def get_account(self):  # pragma: no cover - shape only
                ...

            def get_position(self, symbol):  # pragma: no cover - shape only
                ...

            def get_order(self, order_id):  # pragma: no cover - shape only
                ...

            def cancel_order(self, order_id):  # pragma: no cover - shape only
                ...

            # Deliberately NO list_open_orders / list_positions.

        self.assertFalse(isinstance(_PartialBroker(), BrokerClient))


class TestPlatformFactory(unittest.TestCase):
    def test_valid_platforms_is_alpaca_only(self):
        self.assertEqual(VALID_PLATFORMS, frozenset({"alpaca"}))

    def test_unknown_platform_raises(self):
        with self.assertRaises(ValueError):
            get_default_broker_client(platform="ibkr")


if __name__ == "__main__":
    unittest.main()
