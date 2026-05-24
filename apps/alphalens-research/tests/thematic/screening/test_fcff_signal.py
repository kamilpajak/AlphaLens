import datetime as dt
import unittest
from unittest.mock import MagicMock, patch

from alphalens_pipeline.thematic.screening import fcff_signal


def _patch_filter_passthrough():
    """Skip the mcap/price peer filter for tests that pin synthetic
    micro-cap fixtures (issue #197 filter is exercised in
    ``TestScoreFcffDropsShellPeers`` below + ``test_common.py``).
    """
    return patch.object(
        fcff_signal,
        "filter_peers_by_mcap_price",
        side_effect=lambda peers, **_kw: peers,
    )


# 11-field dict shape returned by EdgarFundamentalsStore.ev_fcff_features_as_of
def _features(
    ocf_ttm=200.0,
    capex_ttm=80.0,
    interest_expense_ttm=10.0,
    tax_rate=0.21,
    revenue_ttm=1000.0,
    fcf_margin_5y_median=0.10,
    price=50.0,
    shares_outstanding=100.0,
    long_term_debt=400.0,
    short_term_debt=100.0,
    cash_and_equivalents=200.0,
):
    return {
        "ocf_ttm": ocf_ttm,
        "capex_ttm": capex_ttm,
        "interest_expense_ttm": interest_expense_ttm,
        "tax_rate": tax_rate,
        "revenue_ttm": revenue_ttm,
        "fcf_margin_5y_median": fcf_margin_5y_median,
        "price": price,
        "shares_outstanding": shares_outstanding,
        "long_term_debt": long_term_debt,
        "short_term_debt": short_term_debt,
        "cash_and_equivalents": cash_and_equivalents,
    }


class TestComputeYieldPct(unittest.TestCase):
    def test_returns_pct_for_complete_features(self):
        # FCFF = 200 + 10*(1-0.21) - 80 = 127.9
        # EV = 50*100 + 400+100-200 = 5300
        # yield = 127.9 / 5300 = 0.02413... → 2.413%
        y = fcff_signal.compute_fcff_yield_pct(_features())
        self.assertAlmostEqual(y, 2.413, places=2)

    def test_returns_none_when_features_missing(self):
        self.assertIsNone(fcff_signal.compute_fcff_yield_pct(None))
        self.assertIsNone(fcff_signal.compute_fcff_yield_pct({}))

    def test_returns_none_when_price_missing(self):
        # No price → can't compute EV → yield is unknown.
        self.assertIsNone(fcff_signal.compute_fcff_yield_pct(_features(price=None)))

    def test_returns_none_when_revenue_missing(self):
        # No revenue → can't impute FCFF if actual is non-positive → drop.
        self.assertIsNone(
            fcff_signal.compute_fcff_yield_pct(
                _features(ocf_ttm=-100.0, capex_ttm=10.0, revenue_ttm=None)
            )
        )

    def test_imputes_when_actual_fcff_non_positive(self):
        # Actual FCFF = -100 + 10*0.79 - 10 = -102.1 (negative).
        # Imputed FCFF = 1000 * 0.10 = 100.
        # EV = 5300; yield = 100/5300 = 1.887%.
        y = fcff_signal.compute_fcff_yield_pct(
            _features(ocf_ttm=-100.0, capex_ttm=10.0, fcf_margin_5y_median=0.10)
        )
        self.assertAlmostEqual(y, 1.887, places=2)

    def test_returns_none_when_negative_ev(self):
        # Cash-rich firm with negative EV → undefined yield.
        feats = _features(price=10.0, shares_outstanding=10.0, cash_and_equivalents=1_000_000.0)
        self.assertIsNone(fcff_signal.compute_fcff_yield_pct(feats))


class TestScoreFcff(unittest.TestCase):
    def setUp(self):
        self._filter_patch = _patch_filter_passthrough()
        self._filter_patch.start()
        self.addCleanup(self._filter_patch.stop)

    def test_returns_none_when_candidate_has_no_yield(self):
        fetcher = MagicMock(return_value=None)
        out = fcff_signal.score_fcff(
            ticker="BAD",
            asof=dt.date(2026, 5, 15),
            peers=["A", "B", "BAD"],
            feature_fetcher=fetcher,
        )
        self.assertIsNone(out["yield_pct"])
        self.assertIsNone(out["sector_percentile"])

    def test_percentile_rank_within_peers(self):
        # Candidate at top of cohort -> 100% percentile.
        def fetcher(ticker, asof):
            return {
                "A": _features(ocf_ttm=50, capex_ttm=10, price=100, shares_outstanding=100),
                "B": _features(ocf_ttm=100, capex_ttm=20, price=100, shares_outstanding=100),
                "TOP": _features(ocf_ttm=500, capex_ttm=10, price=100, shares_outstanding=100),
            }[ticker]

        out = fcff_signal.score_fcff(
            ticker="TOP",
            asof=dt.date(2026, 5, 15),
            peers=["A", "B", "TOP"],
            feature_fetcher=fetcher,
        )
        self.assertGreater(out["yield_pct"], 0)
        self.assertAlmostEqual(out["sector_percentile"], 100.0, places=2)

    def test_skips_peers_with_missing_yield(self):
        def fetcher(ticker, asof):
            if ticker == "MISSING":
                return None
            return _features()

        out = fcff_signal.score_fcff(
            ticker="A",
            asof=dt.date(2026, 5, 15),
            peers=["A", "MISSING", "OTHER"],
            feature_fetcher=fetcher,
        )
        self.assertIsNotNone(out["yield_pct"])
        # 2 valid peers (A, OTHER) with identical yields -> tied at 100% (le-count).
        self.assertAlmostEqual(out["sector_percentile"], 100.0, places=2)


class TestScoreFcffDropsShellPeers(unittest.TestCase):
    """Issue #197: shell / penny-stock / nano-cap peers must not anchor
    the FCFF percentile."""

    def test_shell_peer_excluded_from_cohort(self):
        # SHELL has tiny mcap — irrelevant to candidate's percentile.
        # Without the filter, CAND with mediocre yield would land at
        # 50%ile (50/50 against SHELL); with the filter, only HUGE counts.
        def fetcher(ticker, _asof):
            return {
                "CAND": _features(
                    ocf_ttm=120, capex_ttm=20, price=50, shares_outstanding=100_000_000
                ),
                "HUGE": _features(
                    ocf_ttm=400, capex_ttm=20, price=100, shares_outstanding=100_000_000
                ),
                "SHELL": _features(ocf_ttm=1, capex_ttm=0, price=10, shares_outstanding=10_000),
            }[ticker]

        out = fcff_signal.score_fcff(
            ticker="CAND",
            asof=dt.date(2026, 5, 23),
            peers=["CAND", "HUGE", "SHELL"],
            feature_fetcher=fetcher,
        )
        # HUGE has higher yield → CAND below it → percentile ≈ 50 (2 in cohort).
        # SHELL would have pushed CAND to ≈66 if not filtered.
        self.assertAlmostEqual(out["sector_percentile"], 50.0, places=2)

    def test_penny_stock_peer_excluded(self):
        def fetcher(ticker, _asof):
            return {
                "CAND": _features(
                    ocf_ttm=200, capex_ttm=20, price=50, shares_outstanding=100_000_000
                ),
                # PENNY has $50M mcap (clears) but $0.50 price (penny stock)
                "PENNY": _features(
                    ocf_ttm=200, capex_ttm=20, price=0.50, shares_outstanding=100_000_000
                ),
            }[ticker]

        out = fcff_signal.score_fcff(
            ticker="CAND",
            asof=dt.date(2026, 5, 23),
            peers=["CAND", "PENNY"],
            feature_fetcher=fetcher,
        )
        # PENNY filtered → CAND alone → percentile 50 (empty peers neutral midpoint).
        self.assertAlmostEqual(out["sector_percentile"], 50.0, places=2)


if __name__ == "__main__":
    unittest.main()
