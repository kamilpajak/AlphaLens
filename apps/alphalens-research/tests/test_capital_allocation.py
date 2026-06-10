"""Unit tests for the per-fiscal-year buyback-proxy computation.

Drives :func:`compute_buyback_proxy` directly against hand-built
:class:`AnnualStatement` lists (no parquet needed — the function is pure
over the #501 annual series). Confirms:

- a fall in share count year-over-year is a net buyback (shares_change < 0,
  net_buyback True), hand-computed;
- a rise is net issuance / dilution (net_buyback False);
- the oldest year (no prior) yields all-None change fields;
- a gap year (missing fiscal year) nulls the change fields via the
  consecutive-year guard;
- a missing shares figure on either side yields None change fields;
- a non-positive prior share count yields None change fields (no divide);
- newest-first order is preserved and the output length matches the input.
"""

from __future__ import annotations

import unittest
from datetime import date

from alphalens_pipeline.data.fundamentals.annual_aggregator import AnnualStatement
from alphalens_pipeline.data.fundamentals.capital_allocation import (
    CapitalAllocation,
    compute_buyback_proxy,
)


def _stmt(year: int, *, shares_outstanding: float | None = None) -> AnnualStatement:
    """Minimal AnnualStatement carrying only the shares-outstanding input.

    Every other field is None — the buyback proxy never reads them.
    """
    return AnnualStatement(
        fiscal_year_end=date(year, 12, 31),
        fy=year,
        filed_date=date(year + 1, 2, 15),
        revenue=None,
        operating_income=None,
        net_income=None,
        ocf=None,
        capex=None,
        da=None,
        total_equity=None,
        long_term_debt=None,
        short_term_debt=None,
        cash_and_equivalents=None,
        shares_outstanding=shares_outstanding,
        accounts_receivable=None,
        inventory=None,
        accounts_payable=None,
    )


class TestNetBuyback(unittest.TestCase):
    def test_shares_fall_is_net_buyback(self):
        # Shares fall 1000 -> 900: change -100, pct -0.1, net buyback True.
        statements = [
            _stmt(2022, shares_outstanding=900.0),
            _stmt(2021, shares_outstanding=1000.0),
        ]
        result = compute_buyback_proxy(statements)
        self.assertEqual(len(result), 2)
        newest = result[0]
        self.assertEqual(newest.fy, 2022)
        self.assertEqual(newest.fiscal_year_end, date(2022, 12, 31))
        self.assertEqual(newest.shares_outstanding, 900.0)
        self.assertEqual(newest.shares_change, -100.0)
        self.assertAlmostEqual(newest.shares_change_pct, -0.1)
        self.assertTrue(newest.net_buyback)


class TestDilution(unittest.TestCase):
    def test_shares_rise_is_net_issuance(self):
        # Shares rise 900 -> 1000: change +100, pct positive, net buyback False.
        statements = [
            _stmt(2022, shares_outstanding=1000.0),
            _stmt(2021, shares_outstanding=900.0),
        ]
        result = compute_buyback_proxy(statements)
        newest = result[0]
        self.assertEqual(newest.shares_change, 100.0)
        self.assertGreater(newest.shares_change_pct, 0.0)
        self.assertFalse(newest.net_buyback)

    def test_flat_shares_is_not_a_buyback(self):
        # Exactly unchanged share count: pct 0.0, net_buyback False (a buyback
        # requires a strict fall in the count).
        statements = [
            _stmt(2022, shares_outstanding=1000.0),
            _stmt(2021, shares_outstanding=1000.0),
        ]
        result = compute_buyback_proxy(statements)
        newest = result[0]
        self.assertEqual(newest.shares_change, 0.0)
        self.assertEqual(newest.shares_change_pct, 0.0)
        self.assertFalse(newest.net_buyback)


class TestOldestYearNoPrior(unittest.TestCase):
    def test_oldest_year_change_fields_all_none(self):
        statements = [
            _stmt(2022, shares_outstanding=900.0),
            _stmt(2021, shares_outstanding=1000.0),
        ]
        result = compute_buyback_proxy(statements)
        oldest = result[-1]
        self.assertEqual(oldest.fy, 2021)
        self.assertEqual(oldest.shares_outstanding, 1000.0)
        self.assertIsNone(oldest.shares_change)
        self.assertIsNone(oldest.shares_change_pct)
        self.assertIsNone(oldest.net_buyback)

    def test_single_year_yields_all_none_change(self):
        result = compute_buyback_proxy([_stmt(2022, shares_outstanding=900.0)])
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0].shares_change)
        self.assertIsNone(result[0].shares_change_pct)
        self.assertIsNone(result[0].net_buyback)

    def test_empty_input_yields_empty(self):
        self.assertEqual(compute_buyback_proxy([]), [])


class TestFiscalYearGapGuard(unittest.TestCase):
    def test_gap_year_nulls_change_fields(self):
        # 2020 is missing -> 2021's prior in the series is 2019 (~731 days).
        # The consecutive-year guard must reject that pairing: 2021 gets
        # all-None change fields. 2022 (prior 2021, ~365 days) computes normally.
        statements = [
            _stmt(2022, shares_outstanding=900.0),
            _stmt(2021, shares_outstanding=1000.0),
            _stmt(2019, shares_outstanding=1100.0),
        ]
        by_year = {r.fy: r for r in compute_buyback_proxy(statements)}

        # 2022 <- 2021 consecutive: change computes.
        self.assertEqual(by_year[2022].shares_change, -100.0)
        self.assertTrue(by_year[2022].net_buyback)

        # 2021 <- 2019 has a gap year -> guard nulls the change fields, even
        # though both years carry a share count.
        self.assertEqual(by_year[2021].shares_outstanding, 1000.0)
        self.assertIsNone(by_year[2021].shares_change)
        self.assertIsNone(by_year[2021].shares_change_pct)
        self.assertIsNone(by_year[2021].net_buyback)


class TestMissingShares(unittest.TestCase):
    def test_missing_current_shares_yields_none_change(self):
        statements = [
            _stmt(2022, shares_outstanding=None),
            _stmt(2021, shares_outstanding=1000.0),
        ]
        result = compute_buyback_proxy(statements)
        newest = result[0]
        self.assertIsNone(newest.shares_outstanding)
        self.assertIsNone(newest.shares_change)
        self.assertIsNone(newest.shares_change_pct)
        self.assertIsNone(newest.net_buyback)

    def test_missing_prior_shares_yields_none_change(self):
        statements = [
            _stmt(2022, shares_outstanding=900.0),
            _stmt(2021, shares_outstanding=None),
        ]
        result = compute_buyback_proxy(statements)
        newest = result[0]
        self.assertEqual(newest.shares_outstanding, 900.0)
        self.assertIsNone(newest.shares_change)
        self.assertIsNone(newest.shares_change_pct)
        self.assertIsNone(newest.net_buyback)

    def test_non_positive_prior_shares_yields_none_change(self):
        # A zero / negative prior share count would divide-by-zero or produce a
        # meaningless pct -> guard to None.
        statements = [
            _stmt(2022, shares_outstanding=900.0),
            _stmt(2021, shares_outstanding=0.0),
        ]
        result = compute_buyback_proxy(statements)
        newest = result[0]
        self.assertIsNone(newest.shares_change)
        self.assertIsNone(newest.shares_change_pct)
        self.assertIsNone(newest.net_buyback)


class TestOrderAndShape(unittest.TestCase):
    def test_newest_first_order_preserved(self):
        statements = [
            _stmt(2022, shares_outstanding=900.0),
            _stmt(2021, shares_outstanding=1000.0),
            _stmt(2020, shares_outstanding=1100.0),
        ]
        result = compute_buyback_proxy(statements)
        self.assertEqual([r.fy for r in result], [2022, 2021, 2020])
        self.assertEqual(
            [r.fiscal_year_end for r in result],
            [date(2022, 12, 31), date(2021, 12, 31), date(2020, 12, 31)],
        )

    def test_result_is_capital_allocation_instances(self):
        statements = [
            _stmt(2022, shares_outstanding=900.0),
            _stmt(2021, shares_outstanding=1000.0),
        ]
        result = compute_buyback_proxy(statements)
        self.assertTrue(all(isinstance(r, CapitalAllocation) for r in result))


if __name__ == "__main__":
    unittest.main()
