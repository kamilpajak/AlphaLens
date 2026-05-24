import datetime as dt
import unittest

from alphalens_pipeline.thematic.screening import _common


class TestPercentileRank(unittest.TestCase):
    def test_empty_peers_returns_neutral_50(self):
        # Empty cohort → "no information" → midpoint.
        self.assertEqual(_common.percentile_rank(100.0, []), 50.0)

    def test_top_of_cohort_is_100(self):
        self.assertAlmostEqual(_common.percentile_rank(100.0, [10.0, 20.0]), 100.0)

    def test_bottom_of_cohort_includes_self(self):
        # Candidate value not in peers -> auto-included; candidate at bottom -> low.
        result = _common.percentile_rank(0.0, [10.0, 20.0])
        # Cohort = [10, 20, 0]; le_count for 0 = 1; 1/3 ≈ 33.3.
        self.assertAlmostEqual(result, 100.0 / 3, places=2)

    def test_when_value_in_peers_no_duplicate_inclusion(self):
        # Avoid double-counting if candidate already in peer list.
        result = _common.percentile_rank(10.0, [10.0, 20.0])
        # Cohort = [10, 20]; le_count for 10 = 1; 1/2 = 50.
        self.assertEqual(result, 50.0)


class TestClampTax(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(_common.clamp_tax(None))

    def test_in_range_returns_value(self):
        self.assertEqual(_common.clamp_tax(0.21), 0.21)

    def test_below_floor_clamps_to_zero(self):
        self.assertEqual(_common.clamp_tax(-0.1), 0.0)

    def test_above_ceiling_clamps_to_035(self):
        self.assertEqual(_common.clamp_tax(0.45), 0.35)


class TestFilterPeersByMcapPrice(unittest.TestCase):
    asof = dt.date(2026, 5, 23)

    def _fetcher_factory(self, by_ticker):
        return lambda t, _asof: by_ticker.get(t)

    def test_drops_peer_below_mcap_floor(self):
        # 100k shares × $1 = $100k mcap, well below $30M floor.
        fetcher = self._fetcher_factory(
            {
                "BIG": {"price": 50.0, "shares_outstanding": 100_000_000.0},
                "SHELL": {"price": 1.0, "shares_outstanding": 100_000.0},
            }
        )
        out = _common.filter_peers_by_mcap_price(
            ["BIG", "SHELL"], feature_fetcher=fetcher, asof=self.asof
        )
        self.assertEqual(out, ["BIG"])

    def test_drops_peer_below_price_floor(self):
        # 100M shares × $0.50 = $50M mcap (clears $30M) but penny-stock.
        fetcher = self._fetcher_factory(
            {
                "PENNY": {"price": 0.50, "shares_outstanding": 100_000_000.0},
            }
        )
        out = _common.filter_peers_by_mcap_price(["PENNY"], feature_fetcher=fetcher, asof=self.asof)
        self.assertEqual(out, [])

    def test_drops_peer_with_missing_price_or_shares(self):
        fetcher = self._fetcher_factory(
            {
                "NOPRICE": {"price": None, "shares_outstanding": 100_000_000.0},
                "NOSHARES": {"price": 10.0, "shares_outstanding": None},
                "EMPTY": {},
                "OK": {"price": 10.0, "shares_outstanding": 10_000_000.0},
            }
        )
        out = _common.filter_peers_by_mcap_price(
            ["NOPRICE", "NOSHARES", "EMPTY", "OK"], feature_fetcher=fetcher, asof=self.asof
        )
        self.assertEqual(out, ["OK"])

    def test_drops_peer_without_features(self):
        # feature_fetcher returns None — peer dropped, not silently retained.
        fetcher = lambda _t, _asof: None  # noqa: E731
        out = _common.filter_peers_by_mcap_price(
            ["UNKNOWN"], feature_fetcher=fetcher, asof=self.asof
        )
        self.assertEqual(out, [])

    def test_drops_peer_with_nan_inputs(self):
        # NaN must NOT silently slip through (math.isnan is the guard).
        fetcher = self._fetcher_factory(
            {"NANPRICE": {"price": float("nan"), "shares_outstanding": 100_000_000.0}}
        )
        out = _common.filter_peers_by_mcap_price(
            ["NANPRICE"], feature_fetcher=fetcher, asof=self.asof
        )
        self.assertEqual(out, [])

    def test_passthrough_when_fetcher_is_none(self):
        # Backwards-compat / test convenience — no filter applied.
        out = _common.filter_peers_by_mcap_price(["A", "B"], feature_fetcher=None, asof=self.asof)
        self.assertEqual(out, ["A", "B"])

    def test_thresholds_are_overridable(self):
        # Caller can tighten or loosen the floor (e.g. $100M for a stricter
        # institutional cohort).
        fetcher = self._fetcher_factory(
            {
                "MID": {"price": 10.0, "shares_outstanding": 5_000_000.0},  # $50M mcap
            }
        )
        permissive = _common.filter_peers_by_mcap_price(
            ["MID"], feature_fetcher=fetcher, asof=self.asof
        )
        strict = _common.filter_peers_by_mcap_price(
            ["MID"],
            feature_fetcher=fetcher,
            asof=self.asof,
            min_mcap_usd=100_000_000.0,
        )
        self.assertEqual(permissive, ["MID"])
        self.assertEqual(strict, [])


if __name__ == "__main__":
    unittest.main()
