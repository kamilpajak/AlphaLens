import datetime as dt
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

    def test_is_in_thematic_etf_lazy_primes_cold_cache(self):
        # When the theme is mapped but no parquet exists yet, the gate must
        # invoke fetch_holdings so the cache primes itself on first use.
        # Without this, every cold-cache query silently returns False and
        # the ETF gate is dead until someone hand-primes it.
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            import pandas as pd

            primed = pd.DataFrame(
                [
                    {
                        "name": "IonQ Inc",
                        "cusip": "46222L108",
                        "ticker": "IONQ",
                        "pct_val": 1.16,
                        "asset_cat": "EC",
                    },
                ]
            )

            def fake_fetch(*, etf, series_name, cache_dir, max_age_days, force=False):
                primed.to_parquet(cache_dir / f"{etf}_2026-04-30.parquet", index=False)
                return primed

            with (
                patch.object(
                    etf_holdings,
                    "load_theme_etf_config",
                    return_value={"quantum": [{"etf": "QTUM", "series_name": "Defiance Quantum"}]},
                ),
                patch.object(etf_holdings, "fetch_holdings", side_effect=fake_fetch) as m,
            ):
                self.assertTrue(
                    etf_holdings.is_in_thematic_etf(
                        ticker="IONQ", themes=["quantum"], cache_dir=cache_dir
                    )
                )
                m.assert_called_once()

    def test_is_in_thematic_etf_lazy_prime_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with (
                patch.object(
                    etf_holdings,
                    "load_theme_etf_config",
                    return_value={"quantum": [{"etf": "QTUM", "series_name": "Defiance Quantum"}]},
                ),
                patch.object(
                    etf_holdings, "fetch_holdings", side_effect=AssertionError("no fetch")
                ),
            ):
                self.assertFalse(
                    etf_holdings.is_in_thematic_etf(
                        ticker="IONQ",
                        themes=["quantum"],
                        cache_dir=cache_dir,
                        prime=False,
                    )
                )

    def test_is_in_thematic_etf_returns_none_when_prime_fails(self):
        # Cold cache + lazy-prime raising = unknown (data unavailable), NOT
        # False (which would mean "ran and ticker isn't held"). Lets the
        # orchestrator record gates_unknown instead of silent false-negatives.
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(
                    etf_holdings,
                    "load_theme_etf_config",
                    return_value={"quantum": [{"etf": "QTUM", "series_name": "Defiance Quantum"}]},
                ),
                patch.object(
                    etf_holdings, "fetch_holdings", side_effect=RuntimeError("SEC unreachable")
                ),
            ):
                self.assertIsNone(
                    etf_holdings.is_in_thematic_etf(
                        ticker="IONQ", themes=["quantum"], cache_dir=Path(tmpdir)
                    )
                )

    def test_is_in_thematic_etf_returns_false_when_prime_succeeds_but_ticker_absent(self):
        # Prime succeeded but ticker just isn't in any holdings — real "no",
        # NOT unknown. Tested explicitly to pin the tri-state distinction.
        import pandas as pd

        def primed_fetch(*, etf, series_name, cache_dir, max_age_days, force=False):
            # Only NVDA in this ETF's holdings.
            df = pd.DataFrame(
                [
                    {
                        "name": "NVIDIA Corp",
                        "cusip": "x",
                        "ticker": "NVDA",
                        "pct_val": 1.0,
                        "asset_cat": "EC",
                    }
                ]
            )
            df.to_parquet(cache_dir / f"{etf}_2026-04-30.parquet", index=False)
            return df

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(
                    etf_holdings,
                    "load_theme_etf_config",
                    return_value={"quantum": [{"etf": "QTUM", "series_name": "Defiance Quantum"}]},
                ),
                patch.object(etf_holdings, "fetch_holdings", side_effect=primed_fetch),
            ):
                result = etf_holdings.is_in_thematic_etf(
                    ticker="MISSING", themes=["quantum"], cache_dir=Path(tmpdir)
                )
            self.assertIs(result, False)

    def test_is_in_thematic_etf_token_prefix_matches_layer2_themes(self):
        # Layer 2 emits richer labels (quantum_computing, quantum_error_correction)
        # than the canonical YAML keys (quantum). Resolution must token-prefix
        # match so 'quantum_*' themes all use the QTUM ETF, while unrelated
        # labels (e.g. 'AI_models') don't accidentally pick up the 'quantum'
        # mapping just because the words share a substring.
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            import pandas as pd

            pd.DataFrame(
                [
                    {
                        "name": "IonQ Inc",
                        "cusip": "46222L108",
                        "ticker": "IONQ",
                        "pct_val": 1.16,
                        "asset_cat": "EC",
                    }
                ]
            ).to_parquet(cache_dir / "QTUM_2026-04-30.parquet", index=False)

            with patch.object(
                etf_holdings,
                "load_theme_etf_config",
                return_value={"quantum": [{"etf": "QTUM", "series_name": "Defiance Quantum"}]},
            ):
                self.assertTrue(
                    etf_holdings.is_in_thematic_etf(
                        ticker="IONQ",
                        themes=["quantum_computing"],
                        cache_dir=cache_dir,
                        prime=False,
                    )
                )
                self.assertTrue(
                    etf_holdings.is_in_thematic_etf(
                        ticker="IONQ",
                        themes=["quantum_error_correction"],
                        cache_dir=cache_dir,
                        prime=False,
                    )
                )
                self.assertFalse(
                    etf_holdings.is_in_thematic_etf(
                        ticker="IONQ",
                        themes=["AI_models"],  # no quantum_ prefix
                        cache_dir=cache_dir,
                        prime=False,
                    )
                )

    def test_is_in_thematic_etf_returns_none_when_theme_unmapped(self):
        # Theme not in YAML = no mapped ETF resolved = "unknown" (we cannot
        # answer the question), not False (which would mean "checked and not
        # held"). Tri-state semantic per C5 refactor.
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(
                etf_holdings,
                "load_theme_etf_config",
                return_value={"quantum": [{"etf": "QTUM", "series_name": "Defiance Quantum"}]},
            ):
                self.assertIsNone(
                    etf_holdings.is_in_thematic_etf(
                        ticker="NVDA",
                        themes=["alien_invasion"],
                        cache_dir=Path(tmpdir),
                    )
                )


class TestVerificationGatePITPath(unittest.TestCase):
    """``asof`` selects the latest cache file with date ≤ asof, NOT the
    newest available. Symmetric to the mcap_filter PIT path from the
    2026-05-18 audit. When ``asof`` is None or >= today, current
    'always pick newest' behaviour is preserved."""

    def _seed_qtum(self, cache_dir: Path, *, date_iso: str, ticker_held: str) -> None:
        import pandas as pd

        df = pd.DataFrame(
            [
                {
                    "name": f"{ticker_held} Inc",
                    "cusip": "00000A001",
                    "ticker": ticker_held,
                    "pct_val": 1.0,
                    "asset_cat": "EC",
                }
            ]
        )
        df.to_parquet(cache_dir / f"QTUM_{date_iso}.parquet", index=False)

    def test_load_etf_holdings_picks_latest_on_or_before_asof(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            # 3 dated parquets; asof in the middle → should pick 2025-08-31, not 2026-04-30.
            self._seed_qtum(cache_dir, date_iso="2024-12-31", ticker_held="OLDX")
            self._seed_qtum(cache_dir, date_iso="2025-08-31", ticker_held="MIDX")
            self._seed_qtum(cache_dir, date_iso="2026-04-30", ticker_held="NEWX")
            df = etf_holdings._load_etf_holdings("QTUM", cache_dir, asof=dt.date(2025, 12, 1))
        self.assertEqual(list(df["ticker"]), ["MIDX"])

    def test_load_etf_holdings_returns_empty_when_no_file_on_or_before_asof(self):
        # All cached parquets are AFTER asof → graceful empty df (gate later
        # surfaces as None / gates_unknown, not a false negative).
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            self._seed_qtum(cache_dir, date_iso="2026-04-30", ticker_held="NEWX")
            df = etf_holdings._load_etf_holdings("QTUM", cache_dir, asof=dt.date(2024, 6, 1))
        self.assertTrue(df.empty)

    def test_load_etf_holdings_asof_none_preserves_legacy_latest_pick(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            self._seed_qtum(cache_dir, date_iso="2024-12-31", ticker_held="OLDX")
            self._seed_qtum(cache_dir, date_iso="2026-04-30", ticker_held="NEWX")
            df = etf_holdings._load_etf_holdings("QTUM", cache_dir, asof=None)
        self.assertEqual(list(df["ticker"]), ["NEWX"])

    def test_load_etf_holdings_skips_unparseable_filenames(self):
        # Defensive: unrelated/malformed files in the cache dir don't break
        # the date-filter — they're ignored, not coerced to NaT and crashed.
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            self._seed_qtum(cache_dir, date_iso="2025-08-31", ticker_held="MIDX")
            # Drop a junk file matching glob.
            (cache_dir / "QTUM_not-a-date.parquet").write_text("garbage")
            df = etf_holdings._load_etf_holdings("QTUM", cache_dir, asof=dt.date(2026, 4, 30))
        self.assertEqual(list(df["ticker"]), ["MIDX"])

    def test_is_in_thematic_etf_uses_pit_holdings(self):
        # End-to-end: seed two parquets, candidate held in old not new.
        # asof=mid → old wins → True. asof=today → new wins → False.
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            self._seed_qtum(cache_dir, date_iso="2024-12-31", ticker_held="OLDQ")
            self._seed_qtum(cache_dir, date_iso="2026-04-30", ticker_held="NEWQ")
            with patch.object(
                etf_holdings,
                "load_theme_etf_config",
                return_value={"quantum": [{"etf": "QTUM", "series_name": "Defiance Quantum"}]},
            ):
                self.assertTrue(
                    etf_holdings.is_in_thematic_etf(
                        ticker="OLDQ",
                        themes=["quantum"],
                        cache_dir=cache_dir,
                        asof=dt.date(2025, 6, 1),
                    )
                )
                self.assertFalse(
                    etf_holdings.is_in_thematic_etf(
                        ticker="OLDQ",
                        themes=["quantum"],
                        cache_dir=cache_dir,
                        asof=dt.date.today(),
                    )
                )

    def test_is_in_thematic_etf_past_asof_does_not_prime_cache(self):
        # Historical asof + cold cache → return None (unknown), do NOT call
        # fetch_holdings (that would just grab today's filing — leak).
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with (
                patch.object(
                    etf_holdings,
                    "load_theme_etf_config",
                    return_value={"quantum": [{"etf": "QTUM", "series_name": "Defiance Quantum"}]},
                ),
                patch.object(
                    etf_holdings, "fetch_holdings", side_effect=AssertionError("no fetch")
                ),
            ):
                result = etf_holdings.is_in_thematic_etf(
                    ticker="IONQ",
                    themes=["quantum"],
                    cache_dir=cache_dir,
                    asof=dt.date(2024, 6, 1),
                )
        self.assertIsNone(result)

    def test_is_in_thematic_etf_today_asof_still_lazy_primes(self):
        # Live flow unchanged — today/future asof primes cold cache.
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            import pandas as pd

            primed = pd.DataFrame(
                [
                    {
                        "name": "IonQ Inc",
                        "cusip": "46222L108",
                        "ticker": "IONQ",
                        "pct_val": 1.16,
                        "asset_cat": "EC",
                    }
                ]
            )

            def fake_fetch(*, etf, series_name, cache_dir, max_age_days, force=False):
                primed.to_parquet(cache_dir / f"{etf}_2026-04-30.parquet", index=False)
                return primed

            with (
                patch.object(
                    etf_holdings,
                    "load_theme_etf_config",
                    return_value={"quantum": [{"etf": "QTUM", "series_name": "Defiance Quantum"}]},
                ),
                patch.object(etf_holdings, "fetch_holdings", side_effect=fake_fetch) as m,
            ):
                self.assertTrue(
                    etf_holdings.is_in_thematic_etf(
                        ticker="IONQ",
                        themes=["quantum"],
                        cache_dir=cache_dir,
                        asof=dt.date.today(),
                    )
                )
                m.assert_called_once()


class TestConfig(unittest.TestCase):
    def test_load_theme_etf_config_returns_dict(self):
        cfg = etf_holdings.load_theme_etf_config()
        self.assertIsInstance(cfg, dict)
        self.assertIn("quantum", cfg)
        self.assertGreater(len(cfg["quantum"]), 0)
        for entry in cfg["quantum"]:
            self.assertIn("etf", entry)
            self.assertIn("series_name", entry)


class TestFetchHoldingsEmptyAndAux(unittest.TestCase):
    def test_fetch_holdings_returns_empty_when_no_filing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(etf_holdings, "find_latest_filing", return_value=None):
                df = etf_holdings.fetch_holdings(
                    etf="UNKN", series_name="No Such Fund", cache_dir=Path(tmpdir)
                )
            self.assertTrue(df.empty)

    def test_is_in_thematic_etf_word_boundary_avoids_overmatch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            import pandas as pd

            df = pd.DataFrame(
                [
                    {
                        "name": "Sunrun Inc",
                        "cusip": "x",
                        "ticker": "RUN",
                        "pct_val": 1.0,
                        "asset_cat": "EC",
                    },
                ]
            )
            df.to_parquet(cache_dir / "ICLN_2025-08-31.parquet", index=False)

            with patch.object(
                etf_holdings,
                "load_theme_etf_config",
                return_value={"clean_energy": [{"etf": "ICLN", "series_name": "iShares"}]},
            ):
                # "SUN" name match should NOT match "Sunrun" thanks to word boundary
                self.assertFalse(
                    etf_holdings.is_in_thematic_etf(
                        ticker="SUN",
                        themes=["clean_energy"],
                        ticker_to_name={"SUN": "sun"},
                        cache_dir=cache_dir,
                    )
                )


if __name__ == "__main__":
    unittest.main()
