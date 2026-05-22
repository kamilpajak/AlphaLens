import datetime as dt
import unittest
from unittest.mock import MagicMock

from alphalens_research.thematic.screening import fcff_signal


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


if __name__ == "__main__":
    unittest.main()
