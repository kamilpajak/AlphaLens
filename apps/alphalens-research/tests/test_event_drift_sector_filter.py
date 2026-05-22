"""Tests for GICS sector filter (Financials/Utilities exclusion).

Locked into v3 pre-reg: ``sector_exclusions_gics: [40, 55]`` translates to
SIC ranges (6000-6999, 4900-4999) per US-GAAP standard mapping.

The filter takes an injectable ``sic_map`` (ticker -> SIC int) so tests
can stub directly. A real provider that reads from cached SEC submission
JSON sits in ``alphalens_research.screeners.event_drift.sic_provider``.
"""

from __future__ import annotations

import unittest


class TestSectorFilterExclusionRanges(unittest.TestCase):
    """Verify GICS 40/55 SIC ranges exclude expected industries."""

    def setUp(self):
        from alphalens_research.screeners.event_drift.sector_filter import SectorFilter

        self._SectorFilter = SectorFilter

    def test_commercial_bank_excluded(self):
        # SIC 6020 = State Commercial Banks
        f = self._SectorFilter(sic_map={"WFC": 6020})
        self.assertTrue(f.is_excluded("WFC"))

    def test_reit_excluded(self):
        # SIC 6798 = Real Estate Investment Trusts
        f = self._SectorFilter(sic_map={"AMT": 6798})
        self.assertTrue(f.is_excluded("AMT"))

    def test_insurance_excluded(self):
        # SIC 6311 = Life Insurance
        f = self._SectorFilter(sic_map={"MET": 6311})
        self.assertTrue(f.is_excluded("MET"))

    def test_securities_broker_excluded(self):
        # SIC 6211 = Security Brokers and Dealers
        f = self._SectorFilter(sic_map={"GS": 6211})
        self.assertTrue(f.is_excluded("GS"))

    def test_holding_company_excluded(self):
        # SIC 6770 = Blank Checks (holding-company SIC for many financials)
        f = self._SectorFilter(sic_map={"BRK.B": 6770})
        self.assertTrue(f.is_excluded("BRK.B"))

    def test_electric_utility_excluded(self):
        # SIC 4911 = Electric Services
        f = self._SectorFilter(sic_map={"DUK": 4911})
        self.assertTrue(f.is_excluded("DUK"))

    def test_gas_utility_excluded(self):
        # SIC 4924 = Natural Gas Distribution
        f = self._SectorFilter(sic_map={"OKE": 4924})
        self.assertTrue(f.is_excluded("OKE"))

    def test_water_utility_excluded(self):
        # SIC 4941 = Water Supply
        f = self._SectorFilter(sic_map={"AWK": 4941})
        self.assertTrue(f.is_excluded("AWK"))

    def test_industrial_not_excluded(self):
        # SIC 3711 = Motor Vehicles & Passenger Car Bodies
        f = self._SectorFilter(sic_map={"F": 3711})
        self.assertFalse(f.is_excluded("F"))

    def test_tech_software_not_excluded(self):
        # SIC 7372 = Prepackaged Software
        f = self._SectorFilter(sic_map={"MSFT": 7372})
        self.assertFalse(f.is_excluded("MSFT"))

    def test_pharmaceutical_not_excluded(self):
        # SIC 2834 = Pharmaceutical Preparations
        f = self._SectorFilter(sic_map={"PFE": 2834})
        self.assertFalse(f.is_excluded("PFE"))

    def test_retail_not_excluded(self):
        # SIC 5411 = Grocery Stores
        f = self._SectorFilter(sic_map={"KR": 5411})
        self.assertFalse(f.is_excluded("KR"))


class TestSectorFilterEdgeCases(unittest.TestCase):
    """Boundary SIC codes and unknown-ticker policy."""

    def setUp(self):
        from alphalens_research.screeners.event_drift.sector_filter import SectorFilter

        self._SectorFilter = SectorFilter

    def test_lower_boundary_financials(self):
        # SIC 6000 = first SIC in the financial range
        f = self._SectorFilter(sic_map={"X": 6000})
        self.assertTrue(f.is_excluded("X"))

    def test_upper_boundary_financials(self):
        # SIC 6999 = last SIC in the financial range
        f = self._SectorFilter(sic_map={"X": 6999})
        self.assertTrue(f.is_excluded("X"))

    def test_lower_boundary_utilities(self):
        # SIC 4900 = first SIC in the utilities range
        f = self._SectorFilter(sic_map={"X": 4900})
        self.assertTrue(f.is_excluded("X"))

    def test_upper_boundary_utilities(self):
        # SIC 4999 = last SIC in the utilities range
        f = self._SectorFilter(sic_map={"X": 4999})
        self.assertTrue(f.is_excluded("X"))

    def test_just_below_utilities_not_excluded(self):
        # SIC 4899 (e.g., communications services) is NOT a utility
        f = self._SectorFilter(sic_map={"X": 4899})
        self.assertFalse(f.is_excluded("X"))

    def test_just_above_financials_not_excluded(self):
        # SIC 7000 (services start) is NOT financial
        f = self._SectorFilter(sic_map={"X": 7000})
        self.assertFalse(f.is_excluded("X"))

    def test_unknown_ticker_default_includes(self):
        # Default policy: missing SIC is treated as include (rely on other
        # universe gates to drop truly unknown firms).
        f = self._SectorFilter(sic_map={})
        self.assertFalse(f.is_excluded("UNKNOWN"))

    def test_unknown_ticker_strict_policy_excludes(self):
        # When unknown_policy="exclude", missing SIC is treated as exclude.
        f = self._SectorFilter(sic_map={}, unknown_policy="exclude")
        self.assertTrue(f.is_excluded("UNKNOWN"))

    def test_case_insensitive_lookup(self):
        f = self._SectorFilter(sic_map={"AAPL": 3571})
        self.assertFalse(f.is_excluded("aapl"))
        self.assertEqual(f.sic("aapl"), 3571)

    def test_sic_returns_none_for_unknown(self):
        f = self._SectorFilter(sic_map={})
        self.assertIsNone(f.sic("UNKNOWN"))

    def test_filter_universe_method(self):
        """Convenience: filter a list of tickers down to non-excluded."""
        f = self._SectorFilter(sic_map={"AAPL": 3571, "WFC": 6020, "DUK": 4911, "MSFT": 7372})
        kept = f.filter(["AAPL", "WFC", "DUK", "MSFT", "UNKNOWN"])
        self.assertEqual(set(kept), {"AAPL", "MSFT", "UNKNOWN"})


if __name__ == "__main__":
    unittest.main()
