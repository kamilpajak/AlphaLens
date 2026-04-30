import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock


def _record_xml(
    *,
    cik: str = "0000320193",
    ticker: str = "AAPL",
    owner_cik: str,
    owner_name: str = "Owner",
    is_director: str = "0",
    is_officer: str = "1",
    tx_date: str = "2025-03-15",
    tx_code: str = "P",
    shares: str = "1000",
    price: str = "10.00",
) -> bytes:
    return f"""<?xml version="1.0"?>
<ownershipDocument>
<documentType>4</documentType>
<periodOfReport>{tx_date}</periodOfReport>
<issuer>
  <issuerCik>{cik}</issuerCik>
  <issuerName>Issuer</issuerName>
  <issuerTradingSymbol>{ticker}</issuerTradingSymbol>
</issuer>
<reportingOwner>
  <reportingOwnerId>
    <rptOwnerCik>{owner_cik}</rptOwnerCik>
    <rptOwnerName>{owner_name}</rptOwnerName>
  </reportingOwnerId>
  <reportingOwnerRelationship>
    <isDirector>{is_director}</isDirector>
    <isOfficer>{is_officer}</isOfficer>
    <isTenPercentOwner>0</isTenPercentOwner>
    <isOther>0</isOther>
  </reportingOwnerRelationship>
</reportingOwner>
<nonDerivativeTable>
<nonDerivativeTransaction>
  <securityTitle><value>Common Stock</value></securityTitle>
  <transactionDate><value>{tx_date}</value></transactionDate>
  <transactionCoding>
    <transactionFormType>4</transactionFormType>
    <transactionCode>{tx_code}</transactionCode>
  </transactionCoding>
  <transactionAmounts>
    <transactionShares><value>{shares}</value></transactionShares>
    <transactionPricePerShare><value>{price}</value></transactionPricePerShare>
    <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
  </transactionAmounts>
</nonDerivativeTransaction>
</nonDerivativeTable>
</ownershipDocument>""".encode()


def _submissions_payload(filings: list[dict]) -> dict:
    """Mirror the SEC submissions/CIK{...}.json structure we consume."""
    return {
        "filings": {
            "recent": {
                "form": [f["form"] for f in filings],
                "accessionNumber": [f["accession"] for f in filings],
                "filingDate": [f["filing_date"] for f in filings],
                "primaryDocument": [f["primary"] for f in filings],
            }
        }
    }


def _build_scorer(
    *,
    xml_by_accession: dict[str, bytes],
    submissions: dict,
    ticker_cik: dict[str, str] | None = None,
    cache_dir: Path | None = None,
):
    from alphalens.archive.screeners.insider.scorer import InsiderScorer

    edgar = MagicMock()
    edgar.fetch_submissions.return_value = submissions
    edgar.fetch_form4_xml.side_effect = lambda cik, accession_number, primary_doc: xml_by_accession[
        accession_number
    ]

    cik_map = MagicMock()
    mapping = ticker_cik or {"AAPL": "0000320193"}
    cik_map.lookup.side_effect = lambda t: mapping.get(t.upper())

    return InsiderScorer(edgar_client=edgar, ticker_cik_map=cik_map, cache_dir=cache_dir), edgar


class TestEmptySubmissions(unittest.TestCase):
    def test_no_form_4_filings_returns_none(self):
        submissions = _submissions_payload([])
        scorer, _ = _build_scorer(xml_by_accession={}, submissions=submissions)

        self.assertIsNone(scorer.features_as_of("AAPL", date(2025, 3, 20)))


class TestUnknownTicker(unittest.TestCase):
    def test_unknown_ticker_returns_none(self):
        scorer, edgar = _build_scorer(
            xml_by_accession={},
            submissions=_submissions_payload([]),
            ticker_cik={},
        )

        self.assertIsNone(scorer.features_as_of("NOPE", date(2025, 3, 20)))
        edgar.fetch_submissions.assert_not_called()


class TestPitFilingDateFilter(unittest.TestCase):
    def test_filing_after_asof_excluded(self):
        """Transaction is before asof but filing_date is after asof → PIT excludes it."""
        filings = [
            {
                "form": "4",
                "accession": f"acc-{i}",
                "filing_date": fd,
                "primary": "f.xml",
            }
            for i, fd in enumerate(["2025-03-10", "2025-03-11", "2025-03-25"])
        ]
        xmls = {
            "acc-0": _record_xml(owner_cik=f"000000000{i + 1}", tx_date="2025-03-08")
            for i, _ in enumerate(filings)
        }
        # All three insiders have transaction_date = 2025-03-08 (before asof 2025-03-20),
        # but filing acc-2 has filing_date = 2025-03-25 (after asof) and must be dropped.
        for i, f in enumerate(filings):
            xmls[f["accession"]] = _record_xml(owner_cik=f"000000000{i + 1}", tx_date="2025-03-08")

        scorer, _ = _build_scorer(
            xml_by_accession=xmls,
            submissions=_submissions_payload(filings),
        )

        result = scorer.features_as_of("AAPL", date(2025, 3, 20))

        # Only 2 distinct insiders visible → no cluster
        self.assertIsNone(result)

    def test_three_filings_all_pre_asof_form_cluster(self):
        filings = [
            {
                "form": "4",
                "accession": f"acc-{i}",
                "filing_date": "2025-03-10",
                "primary": "f.xml",
            }
            for i in range(3)
        ]
        xmls = {
            f"acc-{i}": _record_xml(owner_cik=f"000000000{i + 1}", tx_date="2025-03-08")
            for i in range(3)
        }

        scorer, _ = _build_scorer(
            xml_by_accession=xmls,
            submissions=_submissions_payload(filings),
        )

        result = scorer.features_as_of("AAPL", date(2025, 3, 20))

        self.assertIsNotNone(result)
        self.assertEqual(result["insider_count"], 3)


class TestFormTypeFilter(unittest.TestCase):
    def test_non_form_4_skipped(self):
        filings = [
            {
                "form": "4",
                "accession": "a1",
                "filing_date": "2025-03-10",
                "primary": "f.xml",
            },
            {
                "form": "10-Q",
                "accession": "a2",
                "filing_date": "2025-03-11",
                "primary": "q.htm",
            },
            {
                "form": "4",
                "accession": "a3",
                "filing_date": "2025-03-12",
                "primary": "f.xml",
            },
            {
                "form": "4",
                "accession": "a4",
                "filing_date": "2025-03-13",
                "primary": "f.xml",
            },
        ]
        xmls = {
            "a1": _record_xml(owner_cik="0000000001", tx_date="2025-03-10"),
            "a3": _record_xml(owner_cik="0000000002", tx_date="2025-03-12"),
            "a4": _record_xml(owner_cik="0000000003", tx_date="2025-03-13"),
        }

        scorer, edgar = _build_scorer(
            xml_by_accession=xmls,
            submissions=_submissions_payload(filings),
        )

        result = scorer.features_as_of("AAPL", date(2025, 3, 20))

        self.assertIsNotNone(result)
        # a2 (10-Q) never fetched
        fetched = {
            call.kwargs.get("accession_number") or call.args[1]
            for call in edgar.fetch_form4_xml.call_args_list
        }
        self.assertNotIn("a2", fetched)


class TestFeaturesDict(unittest.TestCase):
    def test_returns_json_serializable_dict(self):
        import json

        filings = [
            {
                "form": "4",
                "accession": f"acc-{i}",
                "filing_date": "2025-03-10",
                "primary": "f.xml",
            }
            for i in range(3)
        ]
        xmls = {
            f"acc-{i}": _record_xml(
                owner_cik=f"000000000{i + 1}",
                tx_date="2025-03-08",
                shares="100",
                price="10.00",
            )
            for i in range(3)
        }

        scorer, _ = _build_scorer(
            xml_by_accession=xmls,
            submissions=_submissions_payload(filings),
        )

        result = scorer.features_as_of("AAPL", date(2025, 3, 20))

        self.assertEqual(result["insider_count"], 3)
        self.assertAlmostEqual(result["aggregate_dollar"], 3000.0)
        self.assertEqual(result["cluster_window_days"], 30)
        self.assertEqual(result["asof"], "2025-03-20")
        json.dumps(result)  # must be serializable


class TestCacheConfigFingerprint(unittest.TestCase):
    """Zen CR fix: scorer cache must be invalidated when scorer config
    changes (window_days, min_distinct_insiders, plan_age_threshold_days).
    Cache entries include a config_hash; load treats mismatched hashes as
    cache miss. Missing hash = legacy default-config entry, accepted only
    against default config.
    """

    def test_custom_config_does_not_reuse_default_cache(self):
        from alphalens.alt_data.ticker_cik_map import TickerCikMap
        from alphalens.archive.screeners.insider.scorer import InsiderScorer, _ScorerConfig

        edgar = MagicMock()
        cik_map = TickerCikMap(_by_ticker={"AAPL": "0000320193"})

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            default_scorer = InsiderScorer(
                edgar_client=edgar, ticker_cik_map=cik_map, cache_dir=cache
            )
            default_scorer._cache_store(
                "AAPL",
                date(2024, 1, 15),
                {
                    "insider_count": 3,
                    "aggregate_dollar": 1000.0,
                    "cluster_window_days": 30,
                    "asof": "2024-01-15",
                },
            )

            hit_default = default_scorer._cache_load("AAPL", date(2024, 1, 15))
            self.assertIsNotNone(hit_default)

            custom = InsiderScorer(
                edgar_client=edgar,
                ticker_cik_map=cik_map,
                cache_dir=cache,
                config=_ScorerConfig(window_days=45, min_distinct_insiders=3),
            )
            hit_custom = custom._cache_load("AAPL", date(2024, 1, 15))
            self.assertIsNone(hit_custom)

    def test_legacy_cache_without_hash_accepted_by_default_config(self):
        """Backward compat: ~2M VPS-prewarmed entries lack config_hash.
        Accept them when current scorer uses default config."""
        import json

        from alphalens.alt_data.ticker_cik_map import TickerCikMap
        from alphalens.archive.screeners.insider.scorer import InsiderScorer

        edgar = MagicMock()
        cik_map = TickerCikMap(_by_ticker={"AAPL": "0000320193"})

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            legacy_path = cache / "AAPL_2024-01-15.json"
            legacy_path.write_text(
                json.dumps(
                    {
                        "features": {
                            "insider_count": 3,
                            "aggregate_dollar": 1000.0,
                            "cluster_window_days": 30,
                            "asof": "2024-01-15",
                        },
                        "cached_at": "2024-01-15T00:00:00+00:00",
                    }
                )
            )

            scorer = InsiderScorer(edgar_client=edgar, ticker_cik_map=cik_map, cache_dir=cache)
            hit = scorer._cache_load("AAPL", date(2024, 1, 15))
            self.assertIsNotNone(hit)
            self.assertEqual(hit["features"]["insider_count"], 3)

    def test_legacy_cache_without_hash_rejected_by_custom_config(self):
        import json

        from alphalens.alt_data.ticker_cik_map import TickerCikMap
        from alphalens.archive.screeners.insider.scorer import InsiderScorer, _ScorerConfig

        edgar = MagicMock()
        cik_map = TickerCikMap(_by_ticker={"AAPL": "0000320193"})

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            legacy_path = cache / "AAPL_2024-01-15.json"
            legacy_path.write_text(
                json.dumps(
                    {
                        "features": {
                            "insider_count": 3,
                            "aggregate_dollar": 1000.0,
                            "cluster_window_days": 30,
                            "asof": "2024-01-15",
                        },
                        "cached_at": "2024-01-15T00:00:00+00:00",
                    }
                )
            )

            custom = InsiderScorer(
                edgar_client=edgar,
                ticker_cik_map=cik_map,
                cache_dir=cache,
                config=_ScorerConfig(window_days=45),
            )
            hit = custom._cache_load("AAPL", date(2024, 1, 15))
            self.assertIsNone(hit)


class TestCacheContract(unittest.TestCase):
    def test_second_call_same_inputs_does_not_refetch(self):
        filings = [
            {
                "form": "4",
                "accession": f"acc-{i}",
                "filing_date": "2025-03-10",
                "primary": "f.xml",
            }
            for i in range(3)
        ]
        xmls = {
            f"acc-{i}": _record_xml(owner_cik=f"000000000{i + 1}", tx_date="2025-03-08")
            for i in range(3)
        }

        with tempfile.TemporaryDirectory() as td:
            scorer, edgar = _build_scorer(
                xml_by_accession=xmls,
                submissions=_submissions_payload(filings),
                cache_dir=Path(td),
            )

            first = scorer.features_as_of("AAPL", date(2025, 3, 20))
            second = scorer.features_as_of("AAPL", date(2025, 3, 20))

        self.assertEqual(first, second)
        # exactly 1 submissions fetch across both calls
        self.assertEqual(edgar.fetch_submissions.call_count, 1)
        # exactly 3 xml fetches (one per eligible Form 4)
        self.assertEqual(edgar.fetch_form4_xml.call_count, 3)

    def test_none_results_are_cached_too(self):
        """Cache miss should not retry indefinitely when there are no cluster at asof."""
        with tempfile.TemporaryDirectory() as td:
            scorer, edgar = _build_scorer(
                xml_by_accession={},
                submissions=_submissions_payload([]),
                cache_dir=Path(td),
            )

            scorer.features_as_of("AAPL", date(2025, 3, 20))
            scorer.features_as_of("AAPL", date(2025, 3, 20))

        self.assertEqual(edgar.fetch_submissions.call_count, 1)


class TestXslPrefixStripping(unittest.TestCase):
    def test_xsl_prefix_stripped_before_fetch(self):
        """SEC submissions primaryDocument points at xslF345X../form4.xml (HTML render);
        raw parseable XML sits at the basename one dir up. The scorer must fetch the raw."""
        filings = [
            {
                "form": "4",
                "accession": "acc-0",
                "filing_date": "2025-03-10",
                "primary": "xslF345X06/form4.xml",
            }
        ]
        # Map the RAW expected primary to the XML so fetch succeeds only if xsl-stripped.
        xmls = {"acc-0": _record_xml(owner_cik="0000000001", tx_date="2025-03-08")}

        scorer, edgar = _build_scorer(
            xml_by_accession=xmls,
            submissions=_submissions_payload(filings),
        )

        scorer.features_as_of("AAPL", date(2025, 3, 20))

        call = edgar.fetch_form4_xml.call_args
        primary = call.kwargs.get("primary_doc") or call.args[2]
        self.assertEqual(primary, "form4.xml")
        self.assertNotIn("xslF345X", primary)

    def test_non_xsl_primary_passthrough(self):
        """Primary doc without the xsl prefix should be left alone."""
        filings = [
            {
                "form": "4",
                "accession": "acc-0",
                "filing_date": "2025-03-10",
                "primary": "wk-form4_1234567.xml",
            }
        ]
        xmls = {"acc-0": _record_xml(owner_cik="0000000001", tx_date="2025-03-08")}

        scorer, edgar = _build_scorer(
            xml_by_accession=xmls,
            submissions=_submissions_payload(filings),
        )

        scorer.features_as_of("AAPL", date(2025, 3, 20))

        call = edgar.fetch_form4_xml.call_args
        primary = call.kwargs.get("primary_doc") or call.args[2]
        self.assertEqual(primary, "wk-form4_1234567.xml")


if __name__ == "__main__":
    unittest.main()
