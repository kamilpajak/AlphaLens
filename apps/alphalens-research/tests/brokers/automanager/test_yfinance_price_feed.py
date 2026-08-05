"""Unit tests for the yfinance-backed PriceFeed (INC-2a)."""

from __future__ import annotations

import datetime as dt
import unittest

from alphalens_pipeline.brokers.automanager.yfinance_price_feed import YfinancePriceFeed
from broker_contract.price_feed import PriceFeed, PricePoint

_FROZEN = dt.datetime(2026, 8, 5, 14, 30, tzinfo=dt.UTC)


class _FakeYf:
    def __init__(self, prices):
        self._prices = prices  # ticker -> float | None
        self.requested = []

    def last_price(self, ticker):
        self.requested.append(ticker)
        return self._prices.get(ticker)


class _RaisingYf:
    """A non-canonical client whose ``last_price`` raises (no swallowing)."""

    def __init__(self):
        self.requested = []

    def last_price(self, ticker):
        self.requested.append(ticker)
        raise RuntimeError("boom")


def _feed(prices, uic_to_ticker):
    return YfinancePriceFeed(
        resolve_ticker=uic_to_ticker.get,
        yf_client=_FakeYf(prices),
        clock=lambda: _FROZEN,
    )


class TestYfinancePriceFeed(unittest.TestCase):
    def test_fresh_price_returns_pricepoint_stamped_from_clock(self):
        feed = _feed({"AAPL": 306.49}, {211: "AAPL"})
        pt = feed.latest(211)
        self.assertIsInstance(pt, PricePoint)
        self.assertEqual(pt.uic, 211)
        self.assertAlmostEqual(pt.price, 306.49)
        self.assertEqual(pt.asof, _FROZEN)

    def test_unknown_uic_returns_none_and_skips_fetch(self):
        yf = _FakeYf({"AAPL": 306.49})
        feed = YfinancePriceFeed(
            resolve_ticker=lambda uic: None, yf_client=yf, clock=lambda: _FROZEN
        )
        self.assertIsNone(feed.latest(999))
        self.assertEqual(yf.requested, [])  # resolver returned None -> no network call

    def test_failed_fetch_returns_none(self):
        feed = _feed({"AAPL": None}, {211: "AAPL"})
        self.assertIsNone(feed.latest(211))

    def test_non_positive_price_returns_none(self):
        for bad in (0.0, -1.5):
            with self.subTest(bad=bad):
                feed = _feed({"AAPL": bad}, {211: "AAPL"})
                self.assertIsNone(feed.latest(211))

    def test_nan_price_returns_none(self):
        # A non-canonical client may return NaN directly; the veto must not let
        # a NaN PricePoint reach a market-order decision (NaN <= 0 is False).
        feed = _feed({"AAPL": float("nan")}, {211: "AAPL"})
        self.assertIsNone(feed.latest(211))

    def test_inf_price_returns_none(self):
        for bad in (float("inf"), float("-inf")):
            with self.subTest(bad=bad):
                feed = _feed({"AAPL": bad}, {211: "AAPL"})
                self.assertIsNone(feed.latest(211))

    def test_raising_client_returns_none(self):
        # Any fetch exception degrades to the veto (do-not-fire), never propagates.
        yf = _RaisingYf()
        feed = YfinancePriceFeed(
            resolve_ticker={211: "AAPL"}.get, yf_client=yf, clock=lambda: _FROZEN
        )
        self.assertIsNone(feed.latest(211))
        self.assertEqual(yf.requested, ["AAPL"])  # it was called, the error was absorbed

    def test_empty_ticker_returns_none_and_skips_fetch(self):
        yf = _FakeYf({"AAPL": 306.49})
        feed = YfinancePriceFeed(resolve_ticker={211: ""}.get, yf_client=yf, clock=lambda: _FROZEN)
        self.assertIsNone(feed.latest(211))
        self.assertEqual(yf.requested, [])  # empty ticker -> no network call

    def test_distinct_uics_resolve_to_distinct_tickers(self):
        yf = _FakeYf({"AAPL": 306.49, "KO": 86.64})
        feed = YfinancePriceFeed(
            resolve_ticker={211: "AAPL", 307: "KO"}.get,
            yf_client=yf,
            clock=lambda: _FROZEN,
        )
        self.assertAlmostEqual(feed.latest(211).price, 306.49)
        self.assertAlmostEqual(feed.latest(307).price, 86.64)
        self.assertEqual(yf.requested, ["AAPL", "KO"])

    def test_satisfies_pricefeed_protocol(self):
        feed = _feed({"AAPL": 306.49}, {211: "AAPL"})
        self.assertIsInstance(feed, PriceFeed)  # runtime_checkable


if __name__ == "__main__":
    unittest.main()
