"""Hermetic tests for FakeBroker.place_market_order (acceptance in-memory).

Mirrors the SaxoBroker market-order adapter (INC-1) for the acceptance suite:
BUY grows the netted position, SELL reduces it, clamped at flat (the netting
account never flips short on an oversell).
"""

from __future__ import annotations

import unittest

from broker_contract.contract import SupportsMarketOrders

from tests.brokers.automanager.acceptance.fake_broker import FakeBroker


class TestFakeBrokerMarket(unittest.TestCase):
    def test_market_buy_opens_and_grows_the_netted_position(self):
        b = FakeBroker()
        uic = b.uic_of("KO")
        b.place_market_order(uic, "BUY", 100)
        self.assertEqual(b.get_positions_by_uic(uic).quantity, 100.0)
        b.place_market_order(uic, "BUY", 50)
        self.assertEqual(b.get_positions_by_uic(uic).quantity, 150.0)

    def test_market_sell_reduces_and_never_below_zero(self):
        b = FakeBroker()
        uic = b.uic_of("KO")
        b.place_market_order(uic, "BUY", 100)
        b.place_market_order(uic, "SELL", 30)
        self.assertEqual(b.get_positions_by_uic(uic).quantity, 70.0)
        b.place_market_order(uic, "SELL", 999)  # oversell clamps to flat
        self.assertEqual(b.get_positions_by_uic(uic).quantity, 0.0)

    def test_satisfies_capability_protocol(self):
        self.assertIsInstance(FakeBroker(), SupportsMarketOrders)


if __name__ == "__main__":
    unittest.main()
