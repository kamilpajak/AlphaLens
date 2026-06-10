"""Unit tests for the pure DCF valuation module (Buffett PR-3, #503).

Every test is pure — no network, no disk. The DCF math is exercised against
independent inline computations; the FRED helper is driven with a mock client
so no St. Louis Fed request is made.

Covers:
- ``wacc`` against a hand-computed CAPM + capital-weight expression;
- ``discount_owner_earnings`` against an explicit-period NPV + Gordon terminal
  value, plus the flat-perpetuity (growth=0) sanity decomposition;
- ``intrinsic_value_per_share`` and ``margin_of_safety`` sign convention;
- the ``discount_rate <= terminal_growth`` guard (documented: raises);
- ``intrinsic_value_from_statements`` happy path + fail-soft on missing inputs;
- ``risk_free_from_fred`` with a mock client (decimal conversion + None).
"""

from __future__ import annotations

import math
import unittest
from datetime import date

import pandas as pd
from alphalens_pipeline.data.fundamentals.annual_aggregator import AnnualStatement
from alphalens_pipeline.data.fundamentals.valuation_dcf import (
    discount_owner_earnings,
    intrinsic_value_from_statements,
    intrinsic_value_per_share,
    margin_of_safety,
    risk_free_from_fred,
    wacc,
)


def _statement(
    *,
    fiscal_year_end: date,
    ocf: float | None,
    capex: float | None,
    cash: float | None = None,
    ltd: float | None = None,
    std: float | None = None,
    shares: float | None = None,
) -> AnnualStatement:
    """Construct an AnnualStatement with only the fields the DCF reads."""
    return AnnualStatement(
        fiscal_year_end=fiscal_year_end,
        fy=fiscal_year_end.year,
        filed_date=date(fiscal_year_end.year + 1, 2, 1),
        revenue=None,
        operating_income=None,
        net_income=None,
        ocf=ocf,
        capex=capex,
        da=None,
        total_equity=None,
        long_term_debt=ltd,
        short_term_debt=std,
        cash_and_equivalents=cash,
        shares_outstanding=shares,
    )


class _FakeFred:
    """Minimal stand-in for FREDClient exposing only ``fetch_series``."""

    def __init__(self, series: pd.Series | None):
        self._series = series

    def fetch_series(self, series_id: str) -> pd.Series:
        if self._series is None:
            raise RuntimeError("no observations")
        return self._series


class TestWacc(unittest.TestCase):
    def test_known_inputs_match_hand_computation(self):
        # CAPM cost of equity = rf + beta*erp = 0.04 + 1.2*0.05 = 0.10
        # after-tax cost of debt = 0.06 * (1 - 0.21) = 0.0474
        # weights: E=600, D=400, total=1000 -> wE=0.6, wD=0.4
        # WACC = 0.6*0.10 + 0.4*0.0474 = 0.06 + 0.01896 = 0.07896
        result = wacc(
            risk_free=0.04,
            beta=1.2,
            equity_value=600.0,
            debt_value=400.0,
            cost_of_debt=0.06,
            tax_rate=0.21,
            equity_risk_premium=0.05,
        )
        self.assertAlmostEqual(result, 0.07896, places=6)

    def test_all_equity_reduces_to_cost_of_equity(self):
        result = wacc(
            risk_free=0.03,
            beta=1.0,
            equity_value=1000.0,
            debt_value=0.0,
            cost_of_debt=0.06,
            tax_rate=0.21,
            equity_risk_premium=0.05,
        )
        self.assertAlmostEqual(result, 0.08, places=6)  # 0.03 + 1.0*0.05

    def test_zero_total_capital_raises(self):
        with self.assertRaises(ValueError):
            wacc(
                risk_free=0.04,
                beta=1.0,
                equity_value=0.0,
                debt_value=0.0,
                cost_of_debt=0.06,
                tax_rate=0.21,
            )


class TestDiscountOwnerEarnings(unittest.TestCase):
    def test_matches_independent_npv(self):
        base_fcf = 100.0
        growth_rate = 0.08
        terminal_growth = 0.02
        discount_rate = 0.10
        years = 5

        # Independent inline computation.
        pv_explicit = 0.0
        cf = base_fcf
        for t in range(1, years + 1):
            cf = base_fcf * (1 + growth_rate) ** t
            pv_explicit += cf / (1 + discount_rate) ** t
        terminal_cf = cf * (1 + terminal_growth)
        terminal_value = terminal_cf / (discount_rate - terminal_growth)
        pv_terminal = terminal_value / (1 + discount_rate) ** years
        expected = pv_explicit + pv_terminal

        result = discount_owner_earnings(
            base_fcf,
            growth_rate=growth_rate,
            terminal_growth=terminal_growth,
            discount_rate=discount_rate,
            years=years,
        )
        self.assertAlmostEqual(result, expected, places=6)

    def test_flat_perpetuity_decomposition(self):
        # growth=0, terminal_growth=0 -> flat perpetuity worth base_fcf/discount.
        base_fcf = 100.0
        discount_rate = 0.10
        result = discount_owner_earnings(
            base_fcf,
            growth_rate=0.0,
            terminal_growth=0.0,
            discount_rate=discount_rate,
            years=10,
        )
        self.assertAlmostEqual(result, base_fcf / discount_rate, places=6)

    def test_discount_not_above_terminal_growth_raises(self):
        with self.assertRaises(ValueError):
            discount_owner_earnings(
                100.0,
                growth_rate=0.05,
                terminal_growth=0.10,
                discount_rate=0.08,
            )

    def test_discount_equal_terminal_growth_raises(self):
        with self.assertRaises(ValueError):
            discount_owner_earnings(
                100.0,
                growth_rate=0.05,
                terminal_growth=0.08,
                discount_rate=0.08,
            )


class TestPerShareAndMarginOfSafety(unittest.TestCase):
    def test_intrinsic_value_per_share(self):
        self.assertAlmostEqual(intrinsic_value_per_share(1000.0, 50.0), 20.0, places=6)

    def test_intrinsic_value_per_share_none_on_bad_shares(self):
        self.assertIsNone(intrinsic_value_per_share(1000.0, 0.0))
        self.assertIsNone(intrinsic_value_per_share(1000.0, None))

    def test_margin_of_safety_positive_when_market_below_intrinsic(self):
        # market 80, intrinsic 100 -> 1 - 80/100 = +0.20 (trading below intrinsic)
        self.assertAlmostEqual(margin_of_safety(100.0, 80.0), 0.20, places=6)

    def test_margin_of_safety_negative_when_market_above_intrinsic(self):
        # market 120, intrinsic 100 -> 1 - 120/100 = -0.20 (overvalued)
        self.assertAlmostEqual(margin_of_safety(100.0, 120.0), -0.20, places=6)


class TestIntrinsicValueFromStatements(unittest.TestCase):
    def test_happy_path_returns_value_and_per_share(self):
        latest = _statement(
            fiscal_year_end=date(2024, 12, 31),
            ocf=500.0,
            capex=200.0,
            cash=100.0,
            ltd=300.0,
            std=50.0,
            shares=40.0,
        )
        older = _statement(
            fiscal_year_end=date(2023, 12, 31),
            ocf=400.0,
            capex=150.0,
        )
        result = intrinsic_value_from_statements(
            [latest, older],
            growth_rate=0.06,
            terminal_growth=0.02,
            discount_rate=0.10,
            years=10,
        )
        assert result is not None

        base_fcf = 500.0 - 200.0  # 300
        ev = discount_owner_earnings(
            base_fcf,
            growth_rate=0.06,
            terminal_growth=0.02,
            discount_rate=0.10,
            years=10,
        )
        net_cash = 100.0 - (300.0 + 50.0)  # -250
        equity_value = ev + net_cash
        self.assertAlmostEqual(result.enterprise_value, ev, places=6)
        self.assertAlmostEqual(result.equity_value, equity_value, places=6)
        self.assertAlmostEqual(result.intrinsic_value_per_share, equity_value / 40.0, places=6)

    def test_per_share_none_when_shares_missing(self):
        latest = _statement(
            fiscal_year_end=date(2024, 12, 31),
            ocf=500.0,
            capex=200.0,
            cash=100.0,
            ltd=0.0,
            std=0.0,
            shares=None,
        )
        result = intrinsic_value_from_statements(
            [latest],
            growth_rate=0.06,
            terminal_growth=0.02,
            discount_rate=0.10,
        )
        assert result is not None
        self.assertIsNone(result.intrinsic_value_per_share)

    def test_failsoft_none_on_missing_ocf(self):
        latest = _statement(
            fiscal_year_end=date(2024, 12, 31),
            ocf=None,
            capex=200.0,
        )
        result = intrinsic_value_from_statements(
            [latest],
            growth_rate=0.06,
            terminal_growth=0.02,
            discount_rate=0.10,
        )
        self.assertIsNone(result)

    def test_failsoft_none_on_missing_capex(self):
        latest = _statement(
            fiscal_year_end=date(2024, 12, 31),
            ocf=500.0,
            capex=None,
        )
        result = intrinsic_value_from_statements(
            [latest],
            growth_rate=0.06,
            terminal_growth=0.02,
            discount_rate=0.10,
        )
        self.assertIsNone(result)

    def test_failsoft_none_on_empty_series(self):
        result = intrinsic_value_from_statements(
            [],
            growth_rate=0.06,
            terminal_growth=0.02,
            discount_rate=0.10,
        )
        self.assertIsNone(result)

    def test_net_cash_treats_missing_balance_items_as_zero(self):
        # cash present, debt absent -> net_cash = cash (debt treated as 0).
        latest = _statement(
            fiscal_year_end=date(2024, 12, 31),
            ocf=500.0,
            capex=200.0,
            cash=100.0,
            ltd=None,
            std=None,
            shares=None,
        )
        result = intrinsic_value_from_statements(
            [latest],
            growth_rate=0.0,
            terminal_growth=0.0,
            discount_rate=0.10,
        )
        assert result is not None
        ev = 300.0 / 0.10  # flat perpetuity
        self.assertAlmostEqual(result.equity_value, ev + 100.0, places=6)


class TestRiskFreeFromFred(unittest.TestCase):
    def test_returns_latest_decimal_at_or_before_asof(self):
        # DGS10 reported in percent (4.20 == 4.2%). Helper returns decimal.
        series = pd.Series(
            [4.10, 4.20, 4.30],
            index=pd.DatetimeIndex(
                [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03"), pd.Timestamp("2024-01-05")]
            ),
        )
        # asof 2024-01-04 -> latest <= asof is the 2024-01-03 observation (4.20).
        result = risk_free_from_fred(_FakeFred(series), date(2024, 1, 4))
        assert result is not None
        self.assertAlmostEqual(result, 0.042, places=6)

    def test_returns_none_when_no_observation_at_or_before_asof(self):
        series = pd.Series(
            [4.30],
            index=pd.DatetimeIndex([pd.Timestamp("2024-06-01")]),
        )
        self.assertIsNone(risk_free_from_fred(_FakeFred(series), date(2024, 1, 1)))

    def test_returns_none_when_client_yields_nothing(self):
        self.assertIsNone(risk_free_from_fred(_FakeFred(None), date(2024, 1, 4)))

    def test_returned_value_is_finite(self):
        series = pd.Series([4.20], index=pd.DatetimeIndex([pd.Timestamp("2024-01-02")]))
        result = risk_free_from_fred(_FakeFred(series), date(2024, 1, 3))
        assert result is not None
        self.assertTrue(math.isfinite(result))


if __name__ == "__main__":
    unittest.main()
