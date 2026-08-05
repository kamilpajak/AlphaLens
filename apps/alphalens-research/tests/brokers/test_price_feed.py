from __future__ import annotations

import datetime as dt
import unittest

from broker_contract.price_feed import PriceFeed, PricePoint


class TestPriceFeed(unittest.TestCase):
    def test_pricepoint_is_frozen_and_carries_price(self):
        p = PricePoint(uic=486, price=14.36, asof=dt.datetime(2026, 8, 5, tzinfo=dt.UTC))
        self.assertEqual(p.price, 14.36)
        with self.assertRaises(Exception):
            p.price = 1.0  # frozen

    def test_protocol_runtime_checkable(self):
        class _F:
            def latest(self, uic):
                return None

        self.assertIsInstance(_F(), PriceFeed)

        class _N:
            pass

        self.assertNotIsInstance(_N(), PriceFeed)


if __name__ == "__main__":
    unittest.main()
