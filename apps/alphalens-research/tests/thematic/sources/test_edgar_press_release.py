"""Unit tests for the EDGAR press-release ingest adapter (PR-6).

The adapter discovers market-wide 8-Ks for a UTC date via the SEC daily form
index, filters to in-universe CIKs, keeps only filings whose items intersect
the press-release item set, fetches the Exhibit 99.1 narrative as the article
body, and tags tickers from the filer CIK (not title NER).

Exhibit discovery walks the Document Format Files table of the filing's
``{accession}-index.htm`` page (the authoritative exhibit-type listing) — NOT
FilingSummary.xml, which never carries an EX-99.1 doctype (it lists only the
XBRL render files + the primary 8-K). That wrong assumption was issue #337.

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

# REAL captured fixture: the Document Format Files table of a live filing's
# {accession}-index.htm page (ACMR 8-K, CIK 1680062). Note that the primary
# 8-K row's <a href> is wrapped in the iXBRL viewer prefix ``/ix?doc=`` while
# the EX-99.1 row links directly. This is the authoritative exhibit-type table;
# FilingSummary.xml carries NO EX-99.1 doctype (issue #337).
ACMR_INDEX_HTML = """<table class="tableFile" summary="Document Format Files">
         <tr>
            <th scope="col" style="width: 5%;"><acronym title="Sequence Number">Seq</acronym></th>
            <th scope="col" style="width: 40%;">Description</th>
            <th scope="col" style="width: 20%;">Document</th>
            <th scope="col" style="width: 10%;">Type</th>
            <th scope="col">Size</th>
         </tr>
         <tr>
            <td scope="row">1</td>
            <td scope="row">8-K</td>
            <td scope="row"><a href="/ix?doc=/Archives/edgar/data/1680062/000114036126023310/ef20075131_8k.htm">ef20075131_8k.htm</a> &nbsp;&nbsp;<span style="color: green">iXBRL</span></td>
            <td scope="row">8-K</td>
            <td scope="row">35628</td>
         </tr>
         <tr class="evenRow">
            <td scope="row">2</td>
            <td scope="row">EXHIBIT 99.1</td>
            <td scope="row"><a href="/Archives/edgar/data/1680062/000114036126023310/ef20075131_ex99-1.htm">ef20075131_ex99-1.htm</a></td>
            <td scope="row">EX-99.1</td>
            <td scope="row">146082</td>
         </tr>
         <tr>
            <td scope="row">&nbsp;</td>
            <td scope="row">Complete submission text file</td>
            <td scope="row"><a href="/Archives/edgar/data/1680062/000114036126023310/0001140361-26-023310.txt">0001140361-26-023310.txt</a></td>
            <td scope="row">&nbsp;</td>
            <td scope="row">338342</td>
         </tr>
      </table>
"""

# An index.htm table whose only exhibit is a 10.1 material agreement — NO
# EX-99.1 row. A qualifying-item 8-K with this index must be dropped (no body).
INDEX_HTML_NO_EX991 = """<table class="tableFile" summary="Document Format Files">
         <tr>
            <th scope="col">Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th>
         </tr>
         <tr>
            <td scope="row">1</td>
            <td scope="row">8-K</td>
            <td scope="row"><a href="/ix?doc=/Archives/edgar/data/320193/000032019326000050/primary.htm">primary.htm</a></td>
            <td scope="row">8-K</td>
            <td scope="row">12345</td>
         </tr>
         <tr class="evenRow">
            <td scope="row">2</td>
            <td scope="row">EXHIBIT 10.1</td>
            <td scope="row"><a href="/Archives/edgar/data/320193/000032019326000050/ex10-1.htm">ex10-1.htm</a></td>
            <td scope="row">EX-10.1</td>
            <td scope="row">54321</td>
         </tr>
      </table>
"""

# Index.htm for the SAMPLE_IDX APPLE filings: primary 8-K + EX-99.1.
SAMPLE_INDEX_991 = """<table class="tableFile" summary="Document Format Files">
         <tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
         <tr>
            <td scope="row">1</td>
            <td scope="row">8-K</td>
            <td scope="row"><a href="/ix?doc=/Archives/edgar/data/320193/000032019326000050/primary.htm">primary.htm</a></td>
            <td scope="row">8-K</td>
            <td scope="row">12345</td>
         </tr>
         <tr class="evenRow">
            <td scope="row">2</td>
            <td scope="row">EXHIBIT 99.1</td>
            <td scope="row"><a href="/Archives/edgar/data/320193/000032019326000050/ex991.htm">ex991.htm</a></td>
            <td scope="row">EX-99.1</td>
            <td scope="row">99999</td>
         </tr>
      </table>
"""

# Index.htm for the out-of-press-release-item case (items 5.02/9.01): primary
# 8-K + an EX-99.1 officer bio. Item gate must drop it despite the EX-99.1.
SAMPLE_INDEX_991_502 = """<table class="tableFile" summary="Document Format Files">
         <tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
         <tr>
            <td scope="row">1</td>
            <td scope="row">8-K</td>
            <td scope="row"><a href="/ix?doc=/Archives/edgar/data/320193/000032019326000050/primary502.htm">primary502.htm</a></td>
            <td scope="row">8-K</td>
            <td scope="row">12345</td>
         </tr>
         <tr class="evenRow">
            <td scope="row">2</td>
            <td scope="row">EXHIBIT 99.1</td>
            <td scope="row"><a href="/Archives/edgar/data/320193/000032019326000050/ex991bio.htm">ex991bio.htm</a></td>
            <td scope="row">EX-99.1</td>
            <td scope="row">99999</td>
         </tr>
      </table>
"""

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
    if url.endswith("-index.htm"):
        return SAMPLE_INDEX_991
    if url.endswith("primary.htm"):
        return SAMPLE_8K_HTML
    if url.endswith("ex991.htm"):
        return SAMPLE_EX991
    raise AssertionError(f"unexpected URL: {url}")


class TestLoadCikToTicker(unittest.TestCase):
    def setUp(self):
        epr._load_cik_to_ticker.cache_clear()
        self.addCleanup(epr._load_cik_to_ticker.cache_clear)

    def test_corrupt_json_returns_empty_map_without_raising(self):
        # A truncated company_tickers.json (CIKLoader write race) must not
        # crash the whole thematic ingest — degrade to an empty universe so
        # the day survives and the next run recovers once the file is whole.
        with tempfile.TemporaryDirectory() as tmpdir:
            bad = Path(tmpdir) / "company_tickers.json"
            bad.write_text('{"0": {"ticker": "AAPL", "cik_str": 320193')  # truncated
            # default_cik_cache_path is imported lazily inside the function, so
            # patch the source-module symbol it resolves at call time.
            with patch(
                "alphalens_pipeline.edgar_detector.sources.cik_loader.default_cik_cache_path",
                return_value=bad,
            ):
                self.assertEqual(epr._load_cik_to_ticker(), {})

    def test_well_formed_json_inverts_to_cik_padded_map(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            good = Path(tmpdir) / "company_tickers.json"
            good.write_text(json.dumps({"0": {"ticker": "aapl", "cik_str": 320193}}))
            with patch(
                "alphalens_pipeline.edgar_detector.sources.cik_loader.default_cik_cache_path",
                return_value=good,
            ):
                self.assertEqual(epr._load_cik_to_ticker(), {"0000320193": "AAPL"})


class TestTitleFromBody(unittest.TestCase):
    def test_headline_in_h1_becomes_title(self):
        body = "<html><body><h1>Apple Reports Q3 Earnings</h1><p>Details...</p></body></html>"
        self.assertEqual(epr._title_from_body(body), "Apple Reports Q3 Earnings")

    def test_headline_split_by_br(self):
        body = "Acme Corp<br/>announces merger with Beta Inc"
        self.assertEqual(epr._title_from_body(body), "Acme Corp")

    def test_headline_in_div(self):
        body = "<div>Gamma Ltd Completes Financing</div><div>The company...</div>"
        self.assertEqual(epr._title_from_body(body), "Gamma Ltd Completes Financing")

    def test_empty_body_returns_empty_string(self):
        self.assertEqual(epr._title_from_body(""), "")


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


class TestParseIndexDocuments(unittest.TestCase):
    """The Document Format Files table parser (Type -> basename map)."""

    def test_real_fixture_yields_ex991_basename(self):
        docs = epr.parse_index_documents(ACMR_INDEX_HTML)
        self.assertEqual(docs["EX-99.1"], "ef20075131_ex99-1.htm")

    def test_real_fixture_strips_ix_doc_prefix_on_primary_8k(self):
        # The primary 8-K href is wrapped in the iXBRL viewer prefix
        # ``/ix?doc=/Archives/...`` — the parser must strip it and take the
        # basename, not the viewer URL.
        docs = epr.parse_index_documents(ACMR_INDEX_HTML)
        self.assertEqual(docs["8-K"], "ef20075131_8k.htm")

    def test_no_ex991_row_absent_from_map(self):
        docs = epr.parse_index_documents(INDEX_HTML_NO_EX991)
        self.assertNotIn("EX-99.1", docs)
        # The 10.1 agreement IS present (proves we parse non-99.1 rows too).
        self.assertEqual(docs["EX-10.1"], "ex10-1.htm")

    def test_malformed_html_yields_empty_map(self):
        self.assertEqual(epr.parse_index_documents(""), {})


class TestPickEx991(unittest.TestCase):
    def test_ex_991_picked_from_index_table(self):
        self.assertEqual(epr.pick_ex_991_name(ACMR_INDEX_HTML), "ef20075131_ex99-1.htm")

    def test_ex_99_fallback(self):
        # A filing whose exhibit Type is the bare ``EX-99`` (no .1 suffix).
        html = """<table class="tableFile" summary="Document Format Files">
            <tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
            <tr>
              <td scope="row">1</td><td scope="row">EXHIBIT 99</td>
              <td scope="row"><a href="/Archives/edgar/data/1/2/ex99.htm">ex99.htm</a></td>
              <td scope="row">EX-99</td><td scope="row">1</td>
            </tr>
          </table>"""
        self.assertEqual(epr.pick_ex_991_name(html), "ex99.htm")

    def test_ex_991_preferred_over_ex_99(self):
        # When both are present, EX-99.1 wins over the bare EX-99 fallback.
        html = """<table class="tableFile" summary="Document Format Files">
            <tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
            <tr>
              <td scope="row">1</td><td scope="row">EXHIBIT 99</td>
              <td scope="row"><a href="/Archives/edgar/data/1/2/ex99.htm">ex99.htm</a></td>
              <td scope="row">EX-99</td><td scope="row">1</td>
            </tr>
            <tr>
              <td scope="row">2</td><td scope="row">EXHIBIT 99.1</td>
              <td scope="row"><a href="/Archives/edgar/data/1/2/ex991.htm">ex991.htm</a></td>
              <td scope="row">EX-99.1</td><td scope="row">1</td>
            </tr>
          </table>"""
        self.assertEqual(epr.pick_ex_991_name(html), "ex991.htm")

    def test_no_ex_row_returns_none(self):
        self.assertIsNone(epr.pick_ex_991_name(INDEX_HTML_NO_EX991))

    def test_malformed_html_returns_none(self):
        self.assertIsNone(epr.pick_ex_991_name("<not a table <<<"))


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

    def test_exhibit_found_even_with_filingsummary_lacking_ex991(self):
        # Regression for issue #337: a FilingSummary.xml WITHOUT any EX-99.1
        # doctype is present (the real SEC behaviour), yet exhibit discovery
        # from index.htm still finds ef..._ex99-1.htm and the row survives.
        fs_no_991 = (
            "<FilingSummary><FilingSummaryList>"
            '<File doctype="8-K" original="primary.htm"/>'
            '<File doctype="EX-101.INS"/>'
            "</FilingSummaryList></FilingSummary>"
        )

        def route(url: str) -> str:
            if url.endswith(".idx"):
                return SAMPLE_IDX
            if url.endswith("FilingSummary.xml"):
                return fs_no_991
            if url.endswith("-index.htm"):
                return SAMPLE_INDEX_991
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
            self.assertGreaterEqual(len(df), 1)
            self.assertEqual(set(df["tickers"].iloc[0]), {"AAPL"})

    def test_item_gate_drops_non_press_release_items(self):
        def route(url: str) -> str:
            if url.endswith(".idx"):
                return SAMPLE_IDX
            if url.endswith("-index.htm"):
                return SAMPLE_INDEX_991_502
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
        # index.htm has only a 10.1 agreement (no EX-99.1) -> dropped.
        def route(url: str) -> str:
            if url.endswith(".idx"):
                return SAMPLE_IDX
            if url.endswith("-index.htm"):
                return INDEX_HTML_NO_EX991
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
            # Must NOT persist an empty parquet on transient index failure —
            # otherwise the 6x/day cadence is poisoned for the rest of the UTC
            # day (later runs would read the empty cache instead of retrying).
            self.assertFalse((Path(tmpdir) / "2026-05-30.parquet").exists())

    def test_per_filing_enrich_failure_skips_only_that_filing(self):
        # The index.htm fetch for the first APPLE accession raises; the 8-K/A
        # accession succeeds. The day must still yield the surviving filing.
        bad_accession_dir = "000032019326000050"

        def route(url: str) -> str:
            if url.endswith(".idx"):
                return SAMPLE_IDX
            if bad_accession_dir in url and url.endswith("-index.htm"):
                raise RuntimeError("bad index")
            if url.endswith("-index.htm"):
                return SAMPLE_INDEX_991
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
