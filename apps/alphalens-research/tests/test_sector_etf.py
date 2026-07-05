"""Unit tests for the SIC → SPDR-sector-ETF map (PR-2b, D4 decoupling).

The map picks a per-candidate sector benchmark ETF so the EDGE outcome
(``sector_excess_return``) is measured against the candidate's sector rather
than SPY — breaking the SPY-on-SPY confound (memo §4.2). The map is a v1
UNVALIDATED hyperparameter (logged in ``SECTOR_ETF_MAP_VERSION``); it maps at
SIC 2-digit granularity, finer than the 10 coarse SIC divisions.
"""

import unittest


class TestSectorEtfForSic(unittest.TestCase):
    def _map(self, sic):
        from alphalens_pipeline.data.fundamentals.sector_etf import sector_etf_for_sic

        return sector_etf_for_sic(sic)

    def test_representative_sics_map_to_expected_spdr(self):
        cases = {
            3674: "XLK",  # semiconductors → Technology
            7372: "XLK",  # prepackaged software → Technology
            6021: "XLF",  # national commercial banks → Financials
            6311: "XLF",  # life insurance → Financials
            1311: "XLE",  # crude petroleum & natural gas → Energy
            2834: "XLV",  # pharmaceutical preparations → Health Care
            8000: "XLV",  # health services → Health Care
            4911: "XLU",  # electric services → Utilities
            5411: "XLP",  # grocery stores → Consumer Staples
            2000: "XLP",  # food → Consumer Staples
            5311: "XLY",  # department stores → Consumer Discretionary
            3711: "XLY",  # motor vehicles → Consumer Discretionary
            4813: "XLC",  # telephone communications → Communication Services
            3312: "XLB",  # steel works → Materials
            2800: "XLB",  # industrial chemicals → Materials
            3728: "XLI",  # aircraft parts → Industrials
            1531: "XLI",  # operative builders → Industrials
            6798: "XLRE",  # REIT → Real Estate
            6512: "XLRE",  # operators of apartment buildings → Real Estate
        }
        for sic, etf in cases.items():
            with self.subTest(sic=sic):
                self.assertEqual(self._map(sic), etf)

    def test_unmapped_and_none_return_none(self):
        self.assertIsNone(self._map(None))
        self.assertIsNone(self._map(9199))  # public administration — no equity sector
        self.assertIsNone(self._map(0))  # below the mapped range

    def test_every_mapped_etf_is_a_known_spdr_ticker(self):
        from alphalens_pipeline.data.fundamentals.sector_etf import (
            _SIC_RANGES,
            SPDR_SECTOR_ETFS,
        )

        for _lo, _hi, etf in _SIC_RANGES:
            self.assertIn(etf, SPDR_SECTOR_ETFS)


class TestSicRangesWellFormed(unittest.TestCase):
    def test_ranges_are_ascending_and_non_overlapping(self):
        from alphalens_pipeline.data.fundamentals.sector_etf import _SIC_RANGES

        prev_hi = -1
        for lo, hi, _etf in _SIC_RANGES:
            self.assertLessEqual(lo, hi, f"range {lo}-{hi} inverted")
            self.assertGreater(lo, prev_hi, f"range starting {lo} overlaps prior {prev_hi}")
            prev_hi = hi


class TestSectorEtfForTicker(unittest.TestCase):
    def test_delegates_via_sic_lookup(self):
        from unittest.mock import patch

        from alphalens_pipeline.data.fundamentals import sector_etf

        with patch.object(sector_etf, "get_sic", return_value=3674):
            self.assertEqual(sector_etf.sector_etf_for_ticker("NVDA"), "XLK")

    def test_unmapped_ticker_returns_none(self):
        from unittest.mock import patch

        from alphalens_pipeline.data.fundamentals import sector_etf

        with patch.object(sector_etf, "get_sic", return_value=None):
            self.assertIsNone(sector_etf.sector_etf_for_ticker("ZZZZ"))

    def test_empty_ticker_returns_none(self):
        from alphalens_pipeline.data.fundamentals.sector_etf import sector_etf_for_ticker

        self.assertIsNone(sector_etf_for_ticker(""))


class TestMapVersion(unittest.TestCase):
    def test_version_token_present(self):
        from alphalens_pipeline.data.fundamentals.sector_etf import SECTOR_ETF_MAP_VERSION

        self.assertTrue(SECTOR_ETF_MAP_VERSION.startswith("sic"))


if __name__ == "__main__":
    unittest.main()
