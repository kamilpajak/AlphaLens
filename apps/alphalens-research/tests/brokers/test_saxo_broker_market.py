"""Hermetic tests for the live-market market-order adapter (INC-1).

Market BUY (entry tranche) / market SELL (exit tranche) mirror the existing
standalone-stop flow: canonical-side check -> ALLOW_ORDERS gate -> precheck ->
single POST -> _handle_placement_response. No caller is wired yet (inert
until INC-3/4); this file pins the adapter surface in isolation.
"""

from __future__ import annotations

import unittest


class TestSupportsMarketOrdersProtocol(unittest.TestCase):
    def test_supports_market_orders_is_runtime_checkable_protocol(self):
        from broker_contract.contract import SupportsMarketOrders

        # A trivial object with the method structurally satisfies the Protocol.
        class _M:
            def place_market_order(self, uic, side, qty, request_id=None):
                return None

        self.assertIsInstance(_M(), SupportsMarketOrders)

        class _N:  # missing the method
            pass

        self.assertNotIsInstance(_N(), SupportsMarketOrders)


if __name__ == "__main__":
    unittest.main()
