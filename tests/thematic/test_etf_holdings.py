import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alphalens.thematic.verification import etf_holdings

FIXTURE_NPORT_P = """<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/nport">
  <formData>
    <genInfo>
      <seriesName>Defiance Quantum ETF</seriesName>
      <seriesId>S000061888</seriesId>
      <repPdDate>2025-08-31</repPdDate>
    </genInfo>
    <invstOrSecs>
      <invstOrSec>
        <name>NVIDIA Corp</name>
        <lei>NVDA-LEI</lei>
        <title>NVIDIA Corp common stock</title>
        <cusip>67066G104</cusip>
        <identifiers><ticker value="NVDA"/></identifiers>
        <balance>1000</balance>
        <units>NS</units>
        <curCd>USD</curCd>
        <valUSD>1200000</valUSD>
        <pctVal>2.45</pctVal>
        <assetCat>EC</assetCat>
      </invstOrSec>
      <invstOrSec>
        <name>IonQ Inc</name>
        <lei>IONQ-LEI</lei>
        <title>IonQ Inc common stock</title>
        <cusip>46222L108</cusip>
        <identifiers><ticker value="IONQ"/></identifiers>
        <balance>500</balance>
        <units>NS</units>
        <curCd>USD</curCd>
        <valUSD>5000</valUSD>
        <pctVal>0.11</pctVal>
        <assetCat>EC</assetCat>
      </invstOrSec>
      <invstOrSec>
        <name>Microsoft Corporation</name>
        <lei>MSFT-LEI</lei>
        <title>Microsoft Corp common stock</title>
        <cusip>594918104</cusip>
        <identifiers/>
        <balance>100</balance>
        <units>NS</units>
        <curCd>USD</curCd>
        <valUSD>40000</valUSD>
        <pctVal>0.82</pctVal>
        <assetCat>EC</assetCat>
      </invstOrSec>
    </invstOrSecs>
  </formData>
</edgarSubmission>
"""


class TestParseNportP(unittest.TestCase):
    def test_parses_series_metadata(self):
        meta, _ = etf_holdings.parse_nport_p(FIXTURE_NPORT_P)
        self.assertEqual(meta["series_name"], "Defiance Quantum ETF")
        self.assertEqual(meta["series_id"], "S000061888")
        self.assertEqual(meta["report_date"], "2025-08-31")

    def test_parses_holdings_to_dataframe(self):
        _, df = etf_holdings.parse_nport_p(FIXTURE_NPORT_P)
        self.assertEqual(len(df), 3)
        self.assertEqual(set(df.columns), {"name", "cusip", "ticker", "pct_val", "asset_cat"})

    def test_picks_ticker_from_identifiers_when_present(self):
        _, df = etf_holdings.parse_nport_p(FIXTURE_NPORT_P)
        nvda = df[df["name"] == "NVIDIA Corp"].iloc[0]
        self.assertEqual(nvda["ticker"], "NVDA")

    def test_handles_missing_ticker_gracefully(self):
        _, df = etf_holdings.parse_nport_p(FIXTURE_NPORT_P)
        msft = df[df["name"] == "Microsoft Corporation"].iloc[0]
        # Empty <identifiers/> -> ticker should be empty string, not error
        self.assertEqual(msft["ticker"], "")

    def test_handles_empty_holdings_list(self):
        empty_xml = FIXTURE_NPORT_P.replace("<invstOrSecs>", "<invstOrSecs>EMPTY").replace(
            "EMPTY", ""
        )
        # Wrap with empty invstOrSecs
        xml = """<?xml version="1.0"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/nport">
  <formData><genInfo><seriesName>Empty Fund</seriesName></genInfo>
  <invstOrSecs/></formData></edgarSubmission>"""
        meta, df = etf_holdings.parse_nport_p(xml)
        self.assertEqual(len(df), 0)
        self.assertEqual(meta["series_name"], "Empty Fund")


class TestFindFiling(unittest.TestCase):
    def test_find_latest_filing_matches_series_name_case_insensitive(self):
        # Mock EDGAR search to return 3 candidate filings, only 2 match
        fake_search_hits = {
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "ciks": ["0001924868"],
                            "adsh": "0001-25-001",
                            "file_date": "2025-11-28",
                            "display_names": ["Tidal Trust II"],
                        }
                    },
                    {
                        "_source": {
                            "ciks": ["0001540305"],
                            "adsh": "0002-25-002",
                            "file_date": "2025-05-30",
                            "display_names": ["ETF Series Solutions"],
                        }
                    },
                ]
            }
        }
        # We need the second seam: fetch series name from each candidate
        # Simulate: latest filing wins if series_name matches
        with patch.object(etf_holdings, "_search_nport_p", return_value=fake_search_hits):
            with patch.object(
                etf_holdings,
                "_fetch_series_name",
                side_effect=["Defiance Large Cap Ex-mag 7 ETF", "Defiance Quantum ETF"],
            ):
                hit = etf_holdings.find_latest_filing(series_name="Defiance Quantum")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["cik"], "0001540305")
        self.assertEqual(hit["adsh"], "0002-25-002")
        self.assertEqual(hit["matched_series_name"], "Defiance Quantum ETF")

    def test_find_latest_returns_none_when_no_match(self):
        fake_hits = {
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "ciks": ["0001"],
                            "adsh": "abc",
                            "file_date": "2025-01-01",
                            "display_names": ["Some Trust"],
                        }
                    }
                ]
            }
        }
        with patch.object(etf_holdings, "_search_nport_p", return_value=fake_hits):
            with patch.object(
                etf_holdings,
                "_fetch_series_name",
                return_value="Totally Different ETF",
            ):
                hit = etf_holdings.find_latest_filing(series_name="Defiance Quantum")
        self.assertIsNone(hit)


class TestFetchHoldings(unittest.TestCase):
    def test_fetch_holdings_caches_to_parquet(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            fake_filing = {
                "cik": "0001540305",
                "adsh": "0001-25-001",
                "file_date": "2025-11-28",
                "primary_doc_url": "https://example.com/primary.xml",
                "matched_series_name": "Defiance Quantum ETF",
            }
            with (
                patch.object(etf_holdings, "find_latest_filing", return_value=fake_filing),
                patch.object(etf_holdings, "_fetch_primary_doc", return_value=FIXTURE_NPORT_P),
            ):
                df = etf_holdings.fetch_holdings(
                    etf="QTUM",
                    series_name="Defiance Quantum",
                    cache_dir=cache_dir,
                )
            self.assertEqual(len(df), 3)
            cache_path = cache_dir / "QTUM_2025-08-31.parquet"
            self.assertTrue(cache_path.exists())

    def test_fetch_holdings_reuses_cache_under_max_age(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            fake_filing = {
                "cik": "0001540305",
                "adsh": "0001-25-001",
                "file_date": "2025-11-28",
                "primary_doc_url": "https://example.com/primary.xml",
                "matched_series_name": "Defiance Quantum ETF",
            }
            with (
                patch.object(etf_holdings, "find_latest_filing", return_value=fake_filing),
                patch.object(etf_holdings, "_fetch_primary_doc", return_value=FIXTURE_NPORT_P),
            ):
                etf_holdings.fetch_holdings(
                    etf="QTUM", series_name="Defiance Quantum", cache_dir=cache_dir
                )
            with patch.object(
                etf_holdings, "find_latest_filing", side_effect=AssertionError("no call")
            ):
                df2 = etf_holdings.fetch_holdings(
                    etf="QTUM",
                    series_name="Defiance Quantum",
                    cache_dir=cache_dir,
                    max_age_days=10_000,  # fixture report date is older than today
                )
            self.assertEqual(len(df2), 3)


class TestVerificationGate(unittest.TestCase):
    def test_is_in_thematic_etf_matches_by_ticker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            # Pre-seed cache with QTUM holdings
            import pandas as pd

            df = pd.DataFrame(
                [
                    {
                        "name": "NVIDIA Corp",
                        "cusip": "67066G104",
                        "ticker": "NVDA",
                        "pct_val": 2.45,
                        "asset_cat": "EC",
                    },
                    {
                        "name": "IonQ Inc",
                        "cusip": "46222L108",
                        "ticker": "IONQ",
                        "pct_val": 0.11,
                        "asset_cat": "EC",
                    },
                ]
            )
            df.to_parquet(cache_dir / "QTUM_2025-08-31.parquet", index=False)

            with patch.object(
                etf_holdings,
                "load_theme_etf_config",
                return_value={"quantum": [{"etf": "QTUM", "series_name": "Defiance Quantum"}]},
            ):
                self.assertTrue(
                    etf_holdings.is_in_thematic_etf(
                        ticker="IONQ", themes=["quantum"], cache_dir=cache_dir
                    )
                )
                self.assertFalse(
                    etf_holdings.is_in_thematic_etf(
                        ticker="QUBT", themes=["quantum"], cache_dir=cache_dir
                    )
                )

    def test_is_in_thematic_etf_matches_by_name_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            import pandas as pd

            df = pd.DataFrame(
                [
                    # Ticker field empty, only name present
                    {
                        "name": "NVIDIA Corp",
                        "cusip": "67066G104",
                        "ticker": "",
                        "pct_val": 2.45,
                        "asset_cat": "EC",
                    },
                ]
            )
            df.to_parquet(cache_dir / "QTUM_2025-08-31.parquet", index=False)

            with patch.object(
                etf_holdings,
                "load_theme_etf_config",
                return_value={"quantum": [{"etf": "QTUM", "series_name": "Defiance Quantum"}]},
            ):
                self.assertTrue(
                    etf_holdings.is_in_thematic_etf(
                        ticker="NVDA",
                        themes=["quantum"],
                        ticker_to_name={"NVDA": "NVIDIA"},
                        cache_dir=cache_dir,
                    )
                )

    def test_is_in_thematic_etf_returns_false_when_theme_unmapped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(
                etf_holdings,
                "load_theme_etf_config",
                return_value={"quantum": [{"etf": "QTUM", "series_name": "Defiance Quantum"}]},
            ):
                self.assertFalse(
                    etf_holdings.is_in_thematic_etf(
                        ticker="NVDA",
                        themes=["alien_invasion"],
                        cache_dir=Path(tmpdir),
                    )
                )


class TestConfig(unittest.TestCase):
    def test_load_theme_etf_config_returns_dict(self):
        cfg = etf_holdings.load_theme_etf_config()
        self.assertIsInstance(cfg, dict)
        self.assertIn("quantum", cfg)
        self.assertGreater(len(cfg["quantum"]), 0)
        for entry in cfg["quantum"]:
            self.assertIn("etf", entry)
            self.assertIn("series_name", entry)


if __name__ == "__main__":
    unittest.main()
