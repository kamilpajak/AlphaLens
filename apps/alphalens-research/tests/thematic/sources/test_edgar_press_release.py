"""Unit tests for the EDGAR press-release ingest adapter (PR-6).

The adapter discovers market-wide 8-Ks for a UTC date via the SEC daily form
index, filters to in-universe CIKs, keeps only filings whose items intersect
the press-release item set, fetches the Exhibit 99.1 narrative as the article
body, and tags tickers from the filer CIK (not title NER).

All SEC HTTP is mocked at the ``SecEdgarClient.get_text`` boundary (mirrors
test_polygon_news.py client-boundary mocking) — no network. The CIK->ticker
inverse map loader is patched so tests never touch ``~/.alphalens``.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from alphalens_pipeline.thematic.sources import edgar_press_release as epr
from alphalens_pipeline.thematic.sources.schema import NEWS_COLUMNS

# A daily form index: header lines, a dashed separator, then fixed-width rows.
# CIK column is the unpadded integer SEC emits; the adapter zero-pads to 10.
SAMPLE_IDX = """Description:           Daily Index of EDGAR Dissemination Feed by Form Type.
Last Data Received:    May 30, 2026

Form Type   Company Name                CIK         Date Filed  File Name
---------------------------------------------------------------------------------
8-K         APPLE INC                   320193      2026-05-30  edgar/data/320193/0000320193-26-000050-index.htm
10-K        FOO HOLDINGS INC            999         2026-05-30  edgar/data/999/0000000999-26-000001-index.htm
8-K         OUT OF UNIVERSE CORP        4242        2026-05-30  edgar/data/4242/0000004242-26-000007-index.htm
8-K/A       APPLE INC                   320193      2026-05-30  edgar/data/320193/0000320193-26-000051-index.htm
"""

# FilingSummary with both the primary 8-K and an EX-99.1 exhibit.
SAMPLE_FS_991 = (
    "<FilingSummary><FilingSummaryList>"
    '<File doctype="8-K" original="primary.htm"/>'
    '<File doctype="EX-99.1" original="ex991.htm"/>'
    "</FilingSummaryList></FilingSummary>"
)

# FilingSummary whose 8-K items are out of the press-release set (5.02, 9.01)
# but which DOES carry an EX-99.1 (e.g. an officer bio) — must be dropped.
SAMPLE_FS_502 = (
    "<FilingSummary><FilingSummaryList>"
    '<File doctype="8-K" original="primary502.htm"/>'
    '<File doctype="EX-99.1" original="ex991bio.htm"/>'
    "</FilingSummaryList></FilingSummary>"
)

# FilingSummary with a qualifying item but NO EX-99.1 exhibit — must be dropped.
SAMPLE_FS_NO_EX = (
    "<FilingSummary><FilingSummaryList>"
    '<File doctype="8-K" original="primary.htm"/>'
    "</FilingSummaryList></FilingSummary>"
)

SAMPLE_8K_HTML = "<p>Item 2.02 Results of Operations and Financial Condition.</p>"
SAMPLE_8K_HTML_502 = "<p>Item 5.02 Departure of Directors. Item 9.01 Financial Statements.</p>"
SAMPLE_EX991 = (
    "<p>Apple Reports Record Q2 Revenue</p>"
    "<p>CUPERTINO, Calif. -- Apple today announced financial results ...</p>"
)


def _route_get_text(url: str) -> str:
    """Substring URL router used as ``client.get_text.side_effect`` in tests."""
    if url.endswith(".idx"):
        return SAMPLE_IDX
    if url.endswith("FilingSummary.xml"):
        return SAMPLE_FS_991
    if url.endswith("primary.htm"):
        return SAMPLE_8K_HTML
    if url.endswith("ex991.htm"):
        return SAMPLE_EX991
    raise AssertionError(f"unexpected URL: {url}")


class TestParseFormIndex(unittest.TestCase):
    def test_only_8k_rows_returned(self):
        rows = epr.parse_form_index_8k(SAMPLE_IDX)
        forms = {r["form_type"] for r in rows}
        # 8-K and 8-K/A kept; 10-K dropped.
        self.assertEqual(forms, {"8-K", "8-K/A"})
        self.assertEqual(len(rows), 3)

    def test_cik_zero_padded_to_ten(self):
        rows = epr.parse_form_index_8k(SAMPLE_IDX)
        ciks = {r["cik_padded"] for r in rows}
        self.assertIn("0000320193", ciks)
        self.assertIn("0000004242", ciks)

    def test_accession_parsed_with_dashes(self):
        rows = epr.parse_form_index_8k(SAMPLE_IDX)
        accs = {r["accession"] for r in rows}
        self.assertIn("0000320193-26-000050", accs)

    def test_header_and_separator_skipped(self):
        rows = epr.parse_form_index_8k(SAMPLE_IDX)
        # No row should carry a Form Type of "Form" (the header word).
        for r in rows:
            self.assertNotEqual(r["form_type"], "Form")

    def test_empty_or_garbage_idx_returns_empty(self):
        self.assertEqual(epr.parse_form_index_8k(""), [])
        self.assertEqual(epr.parse_form_index_8k("no separator at all\njust prose\n"), [])


class TestPickEx991(unittest.TestCase):
    def test_ex_991_doctype_returns_original(self):
        self.assertEqual(epr.pick_ex_991_name(SAMPLE_FS_991), "ex991.htm")

    def test_ex_99_fallback(self):
        fs = (
            "<FilingSummary><FilingSummaryList>"
            '<File doctype="EX-99" original="ex99.htm"/>'
            "</FilingSummaryList></FilingSummary>"
        )
        self.assertEqual(epr.pick_ex_991_name(fs), "ex99.htm")

    def test_no_ex_file_returns_none(self):
        self.assertIsNone(epr.pick_ex_991_name(SAMPLE_FS_NO_EX))

    def test_malformed_xml_returns_none(self):
        self.assertIsNone(epr.pick_ex_991_name("<not valid xml <<<"))


class TestPick8kPrimary(unittest.TestCase):
    def test_primary_8k_picked(self):
        self.assertEqual(epr._pick_8k_primary_name(SAMPLE_FS_991), "primary.htm")

    def test_malformed_xml_returns_none(self):
        self.assertIsNone(epr._pick_8k_primary_name("<<<bad"))


class TestTransform(unittest.TestCase):
    def _hit(self, **overrides):
        hit = {
            "cik_padded": "0000320193",
            "accession": "0000320193-26-000050",
            "filing_date": "2026-05-30",
            "items": ["2.02"],
            "body": SAMPLE_EX991,
            "base_dir": "https://www.sec.gov/Archives/edgar/data/320193/000032019326000050",
        }
        hit.update(overrides)
        return hit

    def test_ticker_from_cik_not_title(self):
        df = epr.transform(
            [self._hit()],
            cik_to_ticker={"0000320193": "AAPL"},
            date=dt.date(2026, 5, 30),
        )
        self.assertEqual(df.iloc[0]["tickers"], ["AAPL"])

    def test_source_stamp(self):
        df = epr.transform(
            [self._hit()], cik_to_ticker={"0000320193": "AAPL"}, date=dt.date(2026, 5, 30)
        )
        self.assertEqual(df.iloc[0]["source"], "edgar_press_release")

    def test_body_is_ex991_text(self):
        df = epr.transform(
            [self._hit()], cik_to_ticker={"0000320193": "AAPL"}, date=dt.date(2026, 5, 30)
        )
        self.assertEqual(df.iloc[0]["body"], SAMPLE_EX991)

    def test_id_is_accession(self):
        df = epr.transform(
            [self._hit()], cik_to_ticker={"0000320193": "AAPL"}, date=dt.date(2026, 5, 30)
        )
        self.assertEqual(df.iloc[0]["id"], "0000320193-26-000050")

    def test_out_of_universe_cik_dropped(self):
        df = epr.transform(
            [self._hit(cik_padded="0000004242")],
            cik_to_ticker={"0000320193": "AAPL"},
            date=dt.date(2026, 5, 30),
        )
        self.assertEqual(len(df), 0)

    def test_empty_hits_returns_empty_news_frame(self):
        df = epr.transform([], cik_to_ticker={"0000320193": "AAPL"}, date=dt.date(2026, 5, 30))
        self.assertEqual(len(df), 0)
        self.assertEqual(list(df.columns), NEWS_COLUMNS)

    def test_extra_is_json_string_with_metadata(self):
        df = epr.transform(
            [self._hit()], cik_to_ticker={"0000320193": "AAPL"}, date=dt.date(2026, 5, 30)
        )
        extra = json.loads(df.iloc[0]["extra"])
        self.assertEqual(extra["accession"], "0000320193-26-000050")
        self.assertEqual(extra["items"], ["2.02"])
        self.assertEqual(extra["cik"], "0000320193")
        self.assertEqual(extra["exhibit"], "99.1")

    def test_columns_match_schema(self):
        df = epr.transform(
            [self._hit()], cik_to_ticker={"0000320193": "AAPL"}, date=dt.date(2026, 5, 30)
        )
        self.assertEqual(list(df.columns), NEWS_COLUMNS)


class TestFetchDailyNews(unittest.TestCase):
    def _client(self, side_effect=None):
        client = MagicMock()
        client.get_text.side_effect = side_effect or _route_get_text
        return client

    def test_end_to_end_produces_one_press_release_row(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(epr, "_load_cik_to_ticker", return_value={"0000320193": "AAPL"}):
                df = epr.fetch_daily_news(
                    date=dt.date(2026, 5, 30),
                    client=self._client(),
                    cache_dir=Path(tmpdir),
                )
            # APPLE 8-K + 8-K/A both in-universe and both carry EX-99.1 + 2.02.
            self.assertGreaterEqual(len(df), 1)
            self.assertEqual(set(df["tickers"].iloc[0]), {"AAPL"})
            self.assertEqual(set(df["source"]), {"edgar_press_release"})

    def test_item_gate_drops_non_press_release_items(self):
        def route(url: str) -> str:
            if url.endswith(".idx"):
                return SAMPLE_IDX
            if url.endswith("FilingSummary.xml"):
                return SAMPLE_FS_502
            if url.endswith("primary502.htm"):
                return SAMPLE_8K_HTML_502
            if url.endswith("ex991bio.htm"):
                return "<p>officer bio</p>"
            raise AssertionError(f"unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(epr, "_load_cik_to_ticker", return_value={"0000320193": "AAPL"}):
                df = epr.fetch_daily_news(
                    date=dt.date(2026, 5, 30),
                    client=self._client(side_effect=route),
                    cache_dir=Path(tmpdir),
                )
            self.assertEqual(len(df), 0)

    def test_item_gate_keeps_2_02(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(epr, "_load_cik_to_ticker", return_value={"0000320193": "AAPL"}):
                df = epr.fetch_daily_news(
                    date=dt.date(2026, 5, 30),
                    client=self._client(),
                    cache_dir=Path(tmpdir),
                )
            self.assertTrue((df["tickers"].apply(lambda t: "AAPL" in t)).any())

    def test_filing_without_ex991_dropped(self):
        def route(url: str) -> str:
            if url.endswith(".idx"):
                return SAMPLE_IDX
            if url.endswith("FilingSummary.xml"):
                return SAMPLE_FS_NO_EX
            if url.endswith("primary.htm"):
                return SAMPLE_8K_HTML
            raise AssertionError(f"unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(epr, "_load_cik_to_ticker", return_value={"0000320193": "AAPL"}):
                df = epr.fetch_daily_news(
                    date=dt.date(2026, 5, 30),
                    client=self._client(side_effect=route),
                    cache_dir=Path(tmpdir),
                )
            self.assertEqual(len(df), 0)

    def test_parquet_written(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(epr, "_load_cik_to_ticker", return_value={"0000320193": "AAPL"}):
                epr.fetch_daily_news(
                    date=dt.date(2026, 5, 30),
                    client=self._client(),
                    cache_dir=Path(tmpdir),
                )
            self.assertTrue((Path(tmpdir) / "2026-05-30.parquet").exists())

    def test_cache_hit_short_circuits_without_client(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(epr, "_load_cik_to_ticker", return_value={"0000320193": "AAPL"}):
                epr.fetch_daily_news(
                    date=dt.date(2026, 5, 30),
                    client=self._client(),
                    cache_dir=Path(tmpdir),
                )
                # Second call must not touch the client at all.
                blocking = MagicMock()
                blocking.get_text.side_effect = AssertionError("no client call on cache hit")
                df2 = epr.fetch_daily_news(
                    date=dt.date(2026, 5, 30),
                    client=blocking,
                    cache_dir=Path(tmpdir),
                )
            self.assertGreaterEqual(len(df2), 1)

    def test_force_refetches(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(epr, "_load_cik_to_ticker", return_value={"0000320193": "AAPL"}):
                epr.fetch_daily_news(
                    date=dt.date(2026, 5, 30),
                    client=self._client(),
                    cache_dir=Path(tmpdir),
                )
                client2 = self._client()
                epr.fetch_daily_news(
                    date=dt.date(2026, 5, 30),
                    client=client2,
                    cache_dir=Path(tmpdir),
                    force=True,
                )
            # force=True bypasses the cache → the second client is called.
            self.assertTrue(client2.get_text.called)

    def test_index_fetch_failure_returns_empty_no_raise(self):
        def route(url: str) -> str:
            if url.endswith(".idx"):
                raise RuntimeError("SEC 503")
            raise AssertionError(f"unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(epr, "_load_cik_to_ticker", return_value={"0000320193": "AAPL"}):
                df = epr.fetch_daily_news(
                    date=dt.date(2026, 5, 30),
                    client=self._client(side_effect=route),
                    cache_dir=Path(tmpdir),
                )
            self.assertEqual(len(df), 0)
            self.assertEqual(list(df.columns), NEWS_COLUMNS)

    def test_per_filing_enrich_failure_skips_only_that_filing(self):
        # FilingSummary for the first APPLE accession raises; the 8-K/A accession
        # succeeds. The day must still yield the surviving filing.
        bad_accession_dir = "000032019326000050"

        def route(url: str) -> str:
            if url.endswith(".idx"):
                return SAMPLE_IDX
            if bad_accession_dir in url and url.endswith("FilingSummary.xml"):
                raise RuntimeError("bad summary")
            if url.endswith("FilingSummary.xml"):
                return SAMPLE_FS_991
            if url.endswith("primary.htm"):
                return SAMPLE_8K_HTML
            if url.endswith("ex991.htm"):
                return SAMPLE_EX991
            raise AssertionError(f"unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(epr, "_load_cik_to_ticker", return_value={"0000320193": "AAPL"}):
                df = epr.fetch_daily_news(
                    date=dt.date(2026, 5, 30),
                    client=self._client(side_effect=route),
                    cache_dir=Path(tmpdir),
                )
            # Only the 8-K/A (accession ...051) survives.
            self.assertEqual(len(df), 1)
            self.assertEqual(df.iloc[0]["id"], "0000320193-26-000051")


if __name__ == "__main__":
    unittest.main()
