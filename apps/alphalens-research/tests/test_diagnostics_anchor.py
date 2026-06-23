import unittest

from alphalens_research.diagnostics.anchor import (
    ANCHOR_ARRIVAL_VWAP,
    ANCHOR_PRIOR_CLOSE,
    event_anchor,
)


class TestEventAnchor(unittest.TestCase):
    def test_prior_close_mode_returns_prior_pair(self):
        stock, spy = event_anchor(
            ANCHOR_PRIOR_CLOSE,
            prior_close_stock=100.0,
            prior_close_spy=500.0,
            arrival_vwap_stock=110.0,
            arrival_open_spy=505.0,
        )
        self.assertEqual((stock, spy), (100.0, 500.0))

    def test_arrival_vwap_mode_uses_vwap_stock_and_spy_open(self):
        stock, spy = event_anchor(
            ANCHOR_ARRIVAL_VWAP,
            prior_close_stock=100.0,
            prior_close_spy=500.0,
            arrival_vwap_stock=110.0,
            arrival_open_spy=505.0,
        )
        self.assertEqual((stock, spy), (110.0, 505.0))

    def test_arrival_vwap_mode_propagates_missing_as_none(self):
        stock, spy = event_anchor(
            ANCHOR_ARRIVAL_VWAP,
            prior_close_stock=100.0,
            prior_close_spy=500.0,
            arrival_vwap_stock=None,
            arrival_open_spy=505.0,
        )
        self.assertEqual((stock, spy), (None, 505.0))

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            event_anchor(
                "nope",
                prior_close_stock=1.0,
                prior_close_spy=1.0,
                arrival_vwap_stock=1.0,
                arrival_open_spy=1.0,
            )


if __name__ == "__main__":
    unittest.main()
