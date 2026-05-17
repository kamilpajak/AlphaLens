import datetime as dt
import unittest
from unittest.mock import MagicMock

from alphalens.thematic.screening import valuation_signal


def _features(
    price=50.0,
    shares_outstanding=100.0,
    revenue_ttm=1000.0,
    fcf_margin_5y_median=0.10,
    long_term_debt=400.0,
    short_term_debt=100.0,
    cash_and_equivalents=200.0,
    ocf_ttm=200.0,
    capex_ttm=80.0,
    interest_expense_ttm=10.0,
    tax_rate=0.21,
    net_income_ttm=150.0,
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
        "net_income_ttm": net_income_ttm,
    }


class TestComputeMultiples(unittest.TestCase):
    def test_pe_from_price_shares_net_income(self):
        # market_cap = 50*100 = 5000; P/E = 5000/150 = 33.33
        m = valuation_signal.compute_multiples(_features())
        self.assertAlmostEqual(m["pe"], 33.33, places=2)

    def test_ps_from_price_shares_revenue(self):
        # 5000 / 1000 = 5.0
        m = valuation_signal.compute_multiples(_features())
        self.assertAlmostEqual(m["ps"], 5.0, places=2)

    def test_ev_rev_ratio(self):
        # EV = 5000 + (400+100-200) = 5300; EV/Rev = 5.3
        m = valuation_signal.compute_multiples(_features())
        self.assertAlmostEqual(m["ev_rev"], 5.3, places=2)

    def test_fcf_margin_from_actual_when_positive(self):
        # actual FCFF = 200 + 10*(1-0.21) - 80 = 127.9; margin = 127.9/1000 = 0.1279
        m = valuation_signal.compute_multiples(_features())
        self.assertAlmostEqual(m["fcf_margin"], 0.1279, places=4)

    def test_falls_back_to_5y_median_when_actual_negative(self):
        m = valuation_signal.compute_multiples(
            _features(ocf_ttm=-200.0, capex_ttm=50.0, fcf_margin_5y_median=0.12)
        )
        self.assertAlmostEqual(m["fcf_margin"], 0.12, places=4)

    def test_pe_none_when_negative_earnings(self):
        # Negative or zero NI -> P/E undefined (negative P/E is misleading).
        m = valuation_signal.compute_multiples(_features(net_income_ttm=-50.0))
        self.assertIsNone(m["pe"])

    def test_missing_inputs_yield_none(self):
        m = valuation_signal.compute_multiples(_features(revenue_ttm=None, net_income_ttm=None))
        self.assertIsNone(m["ps"])
        self.assertIsNone(m["pe"])
        self.assertIsNone(m["ev_rev"])

    def test_returns_dict_with_all_keys_for_empty_features(self):
        m = valuation_signal.compute_multiples({})
        self.assertEqual(set(m), {"pe", "ps", "ev_rev", "fcf_margin"})
        self.assertTrue(all(v is None for v in m.values()))


class TestScoreValuation(unittest.TestCase):
    def test_returns_none_percentile_when_all_multiples_missing(self):
        fetcher = MagicMock(return_value=None)
        out = valuation_signal.score_valuation(
            ticker="X",
            asof=dt.date(2026, 5, 15),
            peers=["A", "X"],
            feature_fetcher=fetcher,
        )
        for key in ("pe", "ps", "ev_rev", "fcf_margin"):
            self.assertIsNone(out[key])
        self.assertIsNone(out["composite_sector_percentile"])

    def test_composite_percentile_rewards_cheaper_candidate(self):
        # A is expensive (high multiples); B is cheap; C is candidate (cheaper than B).
        def fetcher(ticker, asof):
            return {
                "EXP": _features(price=200.0, shares_outstanding=100.0),  # P/S 20
                "MID": _features(price=100.0, shares_outstanding=100.0),  # P/S 10
                "CHEAP": _features(price=10.0, shares_outstanding=100.0),  # P/S 1
            }[ticker]

        out = valuation_signal.score_valuation(
            ticker="CHEAP",
            asof=dt.date(2026, 5, 15),
            peers=["EXP", "MID", "CHEAP"],
            feature_fetcher=fetcher,
        )
        # Cheapest -> highest composite percentile.
        self.assertAlmostEqual(out["composite_sector_percentile"], 100.0, places=1)


class TestFinancialsAgeDays(unittest.TestCase):
    def test_age_computed_from_publish_date(self):
        feats = _features()
        feats["publish_date_str"] = "2026-03-15"
        out = valuation_signal.score_valuation(
            ticker="X",
            asof=dt.date(2026, 4, 14),
            peers=["X"],
            feature_fetcher=lambda t, a: feats,
        )
        self.assertEqual(out["financials_publish_date"], "2026-03-15")
        self.assertEqual(out["financials_age_days"], 30)

    def test_invalid_publish_date_string_yields_none_age(self):
        feats = _features()
        feats["publish_date_str"] = "not-a-date"
        out = valuation_signal.score_valuation(
            ticker="X",
            asof=dt.date(2026, 4, 14),
            peers=["X"],
            feature_fetcher=lambda t, a: feats,
        )
        self.assertEqual(out["financials_publish_date"], "not-a-date")
        self.assertIsNone(out["financials_age_days"])


if __name__ == "__main__":
    unittest.main()
