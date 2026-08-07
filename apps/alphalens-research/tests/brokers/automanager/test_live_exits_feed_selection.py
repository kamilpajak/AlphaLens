from __future__ import annotations

import unittest
from unittest import mock

from alphalens_pipeline.brokers.automanager.control_loop import (
    _default_live_exits_feed_factory,
    _saxo_live_prices_enabled,
)


class TestFeedSelection(unittest.TestCase):
    def test_flag_defaults_off(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(_saxo_live_prices_enabled())

    def test_flag_on_only_for_exactly_one(self):
        for value, expected in (("1", True), ("0", False), ("true", False), ("", False)):
            with mock.patch.dict("os.environ", {"ALPHALENS_SAXO_LIVE_PRICES": value}, clear=True):
                self.assertEqual(_saxo_live_prices_enabled(), expected, value)

    def test_factory_returns_a_vetoing_feed_when_the_flag_is_off(self):
        """Off means no prices at all - never a silent fall back to yfinance."""
        with mock.patch.dict("os.environ", {}, clear=True):
            feed = _default_live_exits_feed_factory({211: "AAPL"})
        self.assertIsNone(feed.latest(211))


if __name__ == "__main__":
    unittest.main()
