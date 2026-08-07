from __future__ import annotations

import datetime as dt
import unittest

from broker_contract.price_feed import PriceFeed, PricePoint, is_fresh

_NOW = dt.datetime(2026, 8, 7, 14, 0, 0, tzinfo=dt.UTC)


def _point(**over) -> PricePoint:
    base = dict(
        uic=211,
        bid=314.01,
        ask=314.04,
        event_time=_NOW - dt.timedelta(seconds=1),
        received_at=_NOW,
        source="saxo-live-l1",
    )
    base.update(over)
    return PricePoint(**base)


class TestPricePoint(unittest.TestCase):
    def test_carries_both_sides_and_is_frozen(self):
        p = _point()
        self.assertEqual(p.bid, 314.01)
        self.assertEqual(p.ask, 314.04)
        with self.assertRaises(Exception):
            p.bid = 1.0

    def test_has_no_fabricated_single_price(self):
        """`price`/`asof` are GONE: a single number with a synthesized stamp is
        exactly the false-freshness bug this contract change removes."""
        p = _point()
        self.assertFalse(hasattr(p, "price"))
        self.assertFalse(hasattr(p, "asof"))


class TestIsFresh(unittest.TestCase):
    def test_fresh_quote_passes(self):
        self.assertTrue(is_fresh(_point(), now=_NOW))

    def test_unknown_event_time_is_vetoed(self):
        """A source that publishes no tick time can never be fresh. This is the
        structural ban on stamping fetch time as quote time."""
        self.assertFalse(is_fresh(_point(event_time=None), now=_NOW))

    def test_too_old_is_vetoed(self):
        old = _point(event_time=_NOW - dt.timedelta(seconds=3.5))
        self.assertFalse(is_fresh(old, now=_NOW))

    def test_boundary_age_passes(self):
        edge = _point(event_time=_NOW - dt.timedelta(seconds=3.0))
        self.assertTrue(is_fresh(edge, now=_NOW))

    def test_future_event_time_is_vetoed(self):
        """Clock skew must not read as extra freshness."""
        future = _point(event_time=_NOW + dt.timedelta(seconds=5))
        self.assertFalse(is_fresh(future, now=_NOW))

    def test_crossed_market_is_vetoed(self):
        self.assertFalse(is_fresh(_point(bid=314.10, ask=314.00), now=_NOW))

    def test_non_positive_or_non_finite_is_vetoed(self):
        self.assertFalse(is_fresh(_point(bid=0.0), now=_NOW))
        self.assertFalse(is_fresh(_point(ask=float("nan")), now=_NOW))
        self.assertFalse(is_fresh(_point(ask=float("inf")), now=_NOW))

    def test_absurd_relative_spread_is_vetoed(self):
        wide = _point(bid=100.0, ask=103.0)  # 3% > the 2% ceiling
        self.assertFalse(is_fresh(wide, now=_NOW))

    def test_normal_spread_passes(self):
        ok = _point(bid=100.0, ask=100.5)  # 0.5%
        self.assertTrue(is_fresh(ok, now=_NOW))


class TestPriceFeedProtocol(unittest.TestCase):
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
