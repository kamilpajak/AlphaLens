"""Unit tests for the per-fiscal-year owner-earnings computation.

Drives :func:`compute_owner_earnings` directly against hand-built
:class:`AnnualStatement` lists (no parquet needed — the function is pure
over the #501 annual series). Confirms:

- owner_earnings = net_income + D&A - maintenance_capex - ΔWC, hand-computed;
- maintenance_capex = min(capex, D&A) in both directions;
- ΔWC sign: working capital rising reduces owner earnings;
- the oldest year (no prior) yields working_capital_change=None ->
  owner_earnings=None (fail-soft);
- any missing required component yields owner_earnings=None but the record
  is still emitted;
- newest-first order is preserved.
"""

from __future__ import annotations

import unittest
from datetime import date

from alphalens_pipeline.data.fundamentals.annual_aggregator import AnnualStatement
from alphalens_pipeline.data.fundamentals.owner_earnings import (
    OwnerEarnings,
    compute_owner_earnings,
)


def _stmt(
    year: int,
    *,
    net_income: float | None = None,
    da: float | None = None,
    capex: float | None = None,
    accounts_receivable: float | None = None,
    inventory: float | None = None,
    accounts_payable: float | None = None,
) -> AnnualStatement:
    """Minimal AnnualStatement carrying only the owner-earnings inputs.

    Every other field is None — owner-earnings never reads them.
    """
    return AnnualStatement(
        fiscal_year_end=date(year, 12, 31),
        fy=year,
        filed_date=date(year + 1, 2, 15),
        revenue=None,
        operating_income=None,
        net_income=net_income,
        ocf=None,
        capex=capex,
        da=da,
        total_equity=None,
        long_term_debt=None,
        short_term_debt=None,
        cash_and_equivalents=None,
        shares_outstanding=None,
        accounts_receivable=accounts_receivable,
        inventory=inventory,
        accounts_payable=accounts_payable,
    )


class TestComputeOwnerEarningsHappyPath(unittest.TestCase):
    def test_two_years_hand_computed(self):
        # WC = AR + inventory - AP.
        # 2021: WC = 100 + 50 - 30 = 120
        # 2022: WC = 130 + 60 - 40 = 150  -> ΔWC = 150 - 120 = 30
        # maintenance_capex 2022 = min(capex=80, D&A=70) = 70
        # owner_earnings 2022 = NI(200) + D&A(70) - 70 - 30 = 170
        statements = [
            _stmt(
                2022,
                net_income=200.0,
                da=70.0,
                capex=80.0,
                accounts_receivable=130.0,
                inventory=60.0,
                accounts_payable=40.0,
            ),
            _stmt(
                2021,
                net_income=100.0,
                da=40.0,
                capex=50.0,
                accounts_receivable=100.0,
                inventory=50.0,
                accounts_payable=30.0,
            ),
        ]

        result = compute_owner_earnings(statements)

        self.assertEqual(len(result), 2)
        newest = result[0]
        self.assertEqual(newest.fy, 2022)
        self.assertEqual(newest.fiscal_year_end, date(2022, 12, 31))
        self.assertEqual(newest.working_capital, 150.0)
        self.assertEqual(newest.working_capital_change, 30.0)
        self.assertEqual(newest.maintenance_capex, 70.0)
        self.assertEqual(newest.owner_earnings, 170.0)


class TestMaintenanceCapexApproximation(unittest.TestCase):
    def test_capex_greater_than_da_uses_da(self):
        statements = [
            _stmt(
                2022,
                net_income=100.0,
                da=30.0,
                capex=90.0,
                accounts_receivable=10.0,
                inventory=0.0,
                accounts_payable=0.0,
            ),
            _stmt(
                2021,
                net_income=50.0,
                da=20.0,
                capex=20.0,
                accounts_receivable=10.0,
                inventory=0.0,
                accounts_payable=0.0,
            ),
        ]
        result = compute_owner_earnings(statements)
        # min(90, 30) = 30
        self.assertEqual(result[0].maintenance_capex, 30.0)

    def test_capex_less_than_da_uses_capex(self):
        statements = [
            _stmt(
                2022,
                net_income=100.0,
                da=90.0,
                capex=25.0,
                accounts_receivable=10.0,
                inventory=0.0,
                accounts_payable=0.0,
            ),
            _stmt(
                2021,
                net_income=50.0,
                da=20.0,
                capex=20.0,
                accounts_receivable=10.0,
                inventory=0.0,
                accounts_payable=0.0,
            ),
        ]
        result = compute_owner_earnings(statements)
        # min(25, 90) = 25
        self.assertEqual(result[0].maintenance_capex, 25.0)


class TestWorkingCapitalChangeSign(unittest.TestCase):
    def test_rising_working_capital_reduces_owner_earnings(self):
        # WC rises 100 -> 150, ΔWC = +50, which is SUBTRACTED.
        statements = [
            _stmt(
                2022,
                net_income=100.0,
                da=0.0,
                capex=0.0,
                accounts_receivable=150.0,
                inventory=0.0,
                accounts_payable=0.0,
            ),
            _stmt(
                2021,
                net_income=100.0,
                da=0.0,
                capex=0.0,
                accounts_receivable=100.0,
                inventory=0.0,
                accounts_payable=0.0,
            ),
        ]
        result = compute_owner_earnings(statements)
        self.assertEqual(result[0].working_capital_change, 50.0)
        # NI(100) + D&A(0) - maint(0) - ΔWC(50) = 50
        self.assertEqual(result[0].owner_earnings, 50.0)

    def test_falling_working_capital_adds_to_owner_earnings(self):
        # WC falls 100 -> 60, ΔWC = -40, which (subtracted) ADDS back.
        statements = [
            _stmt(
                2022,
                net_income=100.0,
                da=0.0,
                capex=0.0,
                accounts_receivable=60.0,
                inventory=0.0,
                accounts_payable=0.0,
            ),
            _stmt(
                2021,
                net_income=100.0,
                da=0.0,
                capex=0.0,
                accounts_receivable=100.0,
                inventory=0.0,
                accounts_payable=0.0,
            ),
        ]
        result = compute_owner_earnings(statements)
        self.assertEqual(result[0].working_capital_change, -40.0)
        # 100 + 0 - 0 - (-40) = 140
        self.assertEqual(result[0].owner_earnings, 140.0)


class TestOldestYearNoPrior(unittest.TestCase):
    def test_oldest_year_change_none_and_owner_earnings_none(self):
        statements = [
            _stmt(
                2022,
                net_income=200.0,
                da=70.0,
                capex=80.0,
                accounts_receivable=130.0,
                inventory=60.0,
                accounts_payable=40.0,
            ),
            _stmt(
                2021,
                net_income=100.0,
                da=40.0,
                capex=50.0,
                accounts_receivable=100.0,
                inventory=50.0,
                accounts_payable=30.0,
            ),
        ]
        result = compute_owner_earnings(statements)
        oldest = result[-1]
        self.assertEqual(oldest.fy, 2021)
        # WC computable, but no prior year -> ΔWC undefined.
        self.assertEqual(oldest.working_capital, 120.0)
        self.assertIsNone(oldest.working_capital_change)
        self.assertIsNone(oldest.owner_earnings)

    def test_single_year_yields_none_owner_earnings(self):
        statements = [
            _stmt(
                2022,
                net_income=200.0,
                da=70.0,
                capex=80.0,
                accounts_receivable=130.0,
                inventory=60.0,
                accounts_payable=40.0,
            ),
        ]
        result = compute_owner_earnings(statements)
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0].working_capital_change)
        self.assertIsNone(result[0].owner_earnings)

    def test_empty_input_yields_empty(self):
        self.assertEqual(compute_owner_earnings([]), [])


class TestMissingComponent(unittest.TestCase):
    def test_missing_da_yields_none_owner_earnings_record_kept(self):
        statements = [
            _stmt(
                2022,
                net_income=200.0,
                da=None,  # missing required component
                capex=80.0,
                accounts_receivable=130.0,
                inventory=60.0,
                accounts_payable=40.0,
            ),
            _stmt(
                2021,
                net_income=100.0,
                da=40.0,
                capex=50.0,
                accounts_receivable=100.0,
                inventory=50.0,
                accounts_payable=30.0,
            ),
        ]
        result = compute_owner_earnings(statements)
        self.assertEqual(len(result), 2)
        newest = result[0]
        self.assertEqual(newest.fy, 2022)
        # ΔWC is still computable (both WC values present).
        self.assertEqual(newest.working_capital_change, 30.0)
        # maintenance_capex needs D&A -> None.
        self.assertIsNone(newest.maintenance_capex)
        self.assertIsNone(newest.owner_earnings)

    def test_missing_net_income_yields_none_owner_earnings(self):
        statements = [
            _stmt(
                2022,
                net_income=None,
                da=70.0,
                capex=80.0,
                accounts_receivable=130.0,
                inventory=60.0,
                accounts_payable=40.0,
            ),
            _stmt(
                2021,
                net_income=100.0,
                da=40.0,
                capex=50.0,
                accounts_receivable=100.0,
                inventory=50.0,
                accounts_payable=30.0,
            ),
        ]
        result = compute_owner_earnings(statements)
        self.assertIsNone(result[0].owner_earnings)
        # maintenance_capex does not depend on NI.
        self.assertEqual(result[0].maintenance_capex, 70.0)

    def test_missing_wc_component_yields_none_wc_and_owner_earnings(self):
        # Inventory missing in the current year -> WC undefined -> ΔWC None.
        statements = [
            _stmt(
                2022,
                net_income=200.0,
                da=70.0,
                capex=80.0,
                accounts_receivable=130.0,
                inventory=None,
                accounts_payable=40.0,
            ),
            _stmt(
                2021,
                net_income=100.0,
                da=40.0,
                capex=50.0,
                accounts_receivable=100.0,
                inventory=50.0,
                accounts_payable=30.0,
            ),
        ]
        result = compute_owner_earnings(statements)
        self.assertIsNone(result[0].working_capital)
        self.assertIsNone(result[0].working_capital_change)
        self.assertIsNone(result[0].owner_earnings)


class TestOrderAndShape(unittest.TestCase):
    def test_newest_first_order_preserved(self):
        statements = [
            _stmt(
                2022,
                net_income=10.0,
                da=1.0,
                capex=1.0,
                accounts_receivable=5.0,
                inventory=0.0,
                accounts_payable=0.0,
            ),
            _stmt(
                2021,
                net_income=10.0,
                da=1.0,
                capex=1.0,
                accounts_receivable=5.0,
                inventory=0.0,
                accounts_payable=0.0,
            ),
            _stmt(
                2020,
                net_income=10.0,
                da=1.0,
                capex=1.0,
                accounts_receivable=5.0,
                inventory=0.0,
                accounts_payable=0.0,
            ),
        ]
        result = compute_owner_earnings(statements)
        self.assertEqual([r.fy for r in result], [2022, 2021, 2020])
        self.assertEqual(
            [r.fiscal_year_end for r in result],
            [date(2022, 12, 31), date(2021, 12, 31), date(2020, 12, 31)],
        )

    def test_result_is_owner_earnings_instances(self):
        statements = [
            _stmt(
                2022,
                net_income=10.0,
                da=1.0,
                capex=1.0,
                accounts_receivable=5.0,
                inventory=0.0,
                accounts_payable=0.0,
            ),
            _stmt(
                2021,
                net_income=10.0,
                da=1.0,
                capex=1.0,
                accounts_receivable=5.0,
                inventory=0.0,
                accounts_payable=0.0,
            ),
        ]
        result = compute_owner_earnings(statements)
        self.assertTrue(all(isinstance(r, OwnerEarnings) for r in result))


if __name__ == "__main__":
    unittest.main()
