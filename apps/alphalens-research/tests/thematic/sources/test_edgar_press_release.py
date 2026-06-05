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

import pandas as pd
from alphalens_pipeline.data.alt_data.sec_edgar_client import SecForbiddenError
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

# --- acceptance-datetime fixtures (issue #391) ------------------------------
# Real captured header (AAPL Q2 8-K, acc 0000320193-26-000011, fetched
# 2026-06-02). "Accepted" is ET (EDT here, UTC-4), YYYY-MM-DD HH:MM:SS, no tz
# suffix; whitespace+newline between the label and value as on the live page.
# 16:30:41 EDT -> 20:30:41 UTC.
SAMPLE_INDEX_HEADER_ACCEPTED = """<div class="formGrouping">
         <div class="infoHead">Filing Date</div>
         <div class="info">2026-04-30</div>
         <div class="infoHead">Accepted</div>
         <div class="info">2026-04-30 16:30:41</div>
         <div class="infoHead">Documents</div>
         <div class="info">2</div>
      </div>
"""

# Winter (EST, UTC-5): 09:05:00 EST on 2026-01-15 -> 14:05:00 UTC. Paired with
# the summer case to prove the offset is NOT hard-coded.
SAMPLE_INDEX_HEADER_ACCEPTED_WINTER = """<div class="formGrouping">
         <div class="infoHead">Accepted</div>
         <div class="info">2026-01-15 09:05:00</div>
      </div>
"""

# Summer after-close (EDT, UTC-4): 16:05:00 EDT on 2026-07-31 -> 20:05:00 UTC.
# The #391 failure mode: an intraday instant that must compete in recency, not
# a 00:00 sink.
SAMPLE_INDEX_HEADER_ACCEPTED_SUMMER = """<div class="formGrouping">
         <div class="infoHead">Accepted</div>
         <div class="info">2026-07-31 16:05:00</div>
      </div>
"""

# Header with NO "Accepted" cell -> exercises the date-only fallback.
SAMPLE_INDEX_HEADER_NO_ACCEPTED = """<div class="formGrouping">
         <div class="infoHead">Filing Date</div>
         <div class="info">2026-05-30</div>
      </div>
"""

# Full index.htm = header (with Accepted) + the existing Document Format Files
# table, for the end-to-end enrich test.
SAMPLE_INDEX_991_WITH_ACCEPTED = SAMPLE_INDEX_HEADER_ACCEPTED + SAMPLE_INDEX_991


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

    def test_data_files_table_cannot_collide_on_type(self):
        # The page also carries a sibling "Data Files" table with the SAME
        # column layout. Even when it appears FIRST and carries a row whose
        # Type is "8-K", the parser must ignore it and only read the
        # "Document Format Files" table.
        html = """
        <table class="tableFile" summary="Data Files">
          <tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
          <tr><td>1</td><td>decoy</td><td><a href="/Archives/edgar/data/1/2/decoy.htm">decoy.htm</a></td><td>8-K</td><td>1</td></tr>
        </table>
        <table class="tableFile" summary="Document Format Files">
          <tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
          <tr><td>1</td><td>8-K</td><td><a href="/Archives/edgar/data/1/2/real_8k.htm">real_8k.htm</a></td><td>8-K</td><td>2</td></tr>
        </table>
        """
        docs = epr.parse_index_documents(html)
        self.assertEqual(docs["8-K"], "real_8k.htm")  # decoy ignored
        self.assertNotIn("decoy.htm", docs.values())

    def test_description_cell_anchor_does_not_hijack_href(self):
        # A footnote <a> in the Description cell (index 1) must NOT be captured
        # as the document href — only the Document cell (index 2) counts.
        html = """
        <table class="tableFile" summary="Document Format Files">
          <tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
          <tr>
            <td>2</td>
            <td>EXHIBIT 99.1 <a href="/footnote.htm">note</a></td>
            <td><a href="/Archives/edgar/data/1/2/the_real_ex991.htm">the_real_ex991.htm</a></td>
            <td>EX-99.1</td>
            <td>3</td>
          </tr>
        </table>
        """
        docs = epr.parse_index_documents(html)
        self.assertEqual(docs["EX-99.1"], "the_real_ex991.htm")


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

    def test_exhibit_discovery_sourced_from_index_htm(self):
        # Regression for issue #337: exhibit discovery must come from the
        # index.htm document table, NOT FilingSummary.xml (which never carries
        # an EX-99.1 doctype). The router below deliberately raises if the
        # adapter ever fetches FilingSummary.xml — proving the dead dependency
        # is gone and the EX-99.1 still resolves end-to-end.
        def route(url: str) -> str:
            if url.endswith(".idx"):
                return SAMPLE_IDX
            if url.endswith("FilingSummary.xml"):
                raise AssertionError("adapter must not fetch FilingSummary.xml (issue #337)")
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

    def test_genuine_empty_day_is_cached(self):
        # #383 — no transient errors, nothing survives the EX-99.1 filter. A
        # genuine empty day STILL caches (transient_errors==0 -> to_parquet runs)
        # so the immutable daily index is not re-fetched 5 more times.
        def route(url: str) -> str:
            if url.endswith(".idx"):
                return SAMPLE_IDX
            if url.endswith("-index.htm"):
                return INDEX_HTML_NO_EX991  # no EX-99.1 -> clean drop
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
            self.assertTrue((Path(tmpdir) / "2026-05-30.parquet").exists())

    def test_all_index_403_does_not_cache(self):
        # #382/#383 — every index.htm 403s (SecForbiddenError) -> hits empty
        # ONLY because of transient errors -> must NOT persist empty parquet, so
        # later same-UTC-day runs retry instead of reading a poisoned cache.
        def route(url: str) -> str:
            if url.endswith(".idx"):
                return SAMPLE_IDX
            if url.endswith("-index.htm"):
                raise SecForbiddenError("403 Request Rate Threshold Exceeded")
            raise AssertionError(f"unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(epr, "_load_cik_to_ticker", return_value={"0000320193": "AAPL"}):
                df = epr.fetch_daily_news(
                    date=dt.date(2026, 5, 30),
                    client=self._client(side_effect=route),
                    cache_dir=Path(tmpdir),
                )
            self.assertEqual(len(df), 0)
            self.assertFalse((Path(tmpdir) / "2026-05-30.parquet").exists())

    def test_403_on_ex991_body_does_not_cache_empty(self):
        # #382/#383 — THE refutation gap: index.htm + primary.htm succeed (item
        # gate passes) but ex991.htm 403s. _safe_text must re-raise the 403 so
        # the single in-universe filing drops to 0 hits via transient_errors,
        # and the empty frame is NOT cached (rather than caching an empty-body
        # row that breaks template matching).
        single_idx = (
            "Form Type   Company Name   CIK      Date Filed  File Name\n"
            "------------------------------------------------------------\n"
            "8-K         APPLE INC      320193   2026-05-30  "
            "edgar/data/320193/0000320193-26-000050-index.htm\n"
        )

        def route(url: str) -> str:
            if url.endswith(".idx"):
                return single_idx
            if url.endswith("-index.htm"):
                return SAMPLE_INDEX_991
            if url.endswith("primary.htm"):
                return SAMPLE_8K_HTML
            if url.endswith("ex991.htm"):
                raise SecForbiddenError("403 Request Rate Threshold Exceeded")
            raise AssertionError(f"unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(epr, "_load_cik_to_ticker", return_value={"0000320193": "AAPL"}):
                df = epr.fetch_daily_news(
                    date=dt.date(2026, 5, 30),
                    client=self._client(side_effect=route),
                    cache_dir=Path(tmpdir),
                )
            self.assertEqual(len(df), 0)
            self.assertFalse((Path(tmpdir) / "2026-05-30.parquet").exists())

    def test_403_storm_then_recovery_caches_without_force(self):
        # #383 — end-to-end: run 1 all-403 (no cache), run 2 same date healthy
        # client succeeds and caches, with NO --force (nothing to short-circuit).
        def failing(url: str) -> str:
            if url.endswith(".idx"):
                return SAMPLE_IDX
            if url.endswith("-index.htm"):
                raise SecForbiddenError("403")
            raise AssertionError(f"unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(epr, "_load_cik_to_ticker", return_value={"0000320193": "AAPL"}):
                df1 = epr.fetch_daily_news(
                    date=dt.date(2026, 5, 30),
                    client=self._client(side_effect=failing),
                    cache_dir=Path(tmpdir),
                )
                self.assertEqual(len(df1), 0)
                self.assertFalse((Path(tmpdir) / "2026-05-30.parquet").exists())
                df2 = epr.fetch_daily_news(
                    date=dt.date(2026, 5, 30),
                    client=self._client(),  # healthy _route_get_text
                    cache_dir=Path(tmpdir),
                )
            self.assertGreaterEqual(len(df2), 1)
            self.assertTrue((Path(tmpdir) / "2026-05-30.parquet").exists())

    def test_partial_403_still_caches_nonempty(self):
        # #383 — one filing 403s on index.htm, the other resolves. A non-empty
        # frame is always cached (refusing would re-enrich survivors on all 6
        # runs, amplifying the per-IP load that causes the 403s).
        bad_dir = "000032019326000051"  # the 8-K/A accession dir

        def route(url: str) -> str:
            if url.endswith(".idx"):
                return SAMPLE_IDX
            if bad_dir in url and url.endswith("-index.htm"):
                raise SecForbiddenError("403")
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
            self.assertTrue((Path(tmpdir) / "2026-05-30.parquet").exists())


class TestParseAcceptedUtc(unittest.TestCase):
    """The 'Accepted' ET-datetime parser (index.htm header -> UTC Timestamp).

    Regression for #391: EX-99.1 rows must carry the real SEC acceptance instant,
    not the date-only filing date (00:00 UTC) that sinks them to the bottom of
    the news_ingest recency cap.
    """

    def test_edt_summer_offset_is_utc_minus_4(self):
        # 16:30:41 EDT on 2026-04-30 -> 20:30:41 UTC (DST active, UTC-4).
        ts = epr.parse_accepted_utc(SAMPLE_INDEX_HEADER_ACCEPTED)
        self.assertEqual(ts, pd.Timestamp("2026-04-30 20:30:41", tz="UTC"))
        self.assertNotEqual(ts, pd.Timestamp("2026-04-30", tz="UTC"))

    def test_est_winter_offset_is_utc_minus_5(self):
        # 09:05:00 EST on 2026-01-15 -> 14:05:00 UTC (DST inactive, UTC-5).
        ts = epr.parse_accepted_utc(SAMPLE_INDEX_HEADER_ACCEPTED_WINTER)
        self.assertEqual(ts, pd.Timestamp("2026-01-15 14:05:00", tz="UTC"))

    def test_dst_pair_offsets_differ_by_one_hour(self):
        # Same 09:05:00 wall-clock maps to a UTC hour one apart across the DST
        # boundary (EDT UTC-4 vs EST UTC-5); a hard-coded fixed offset would make
        # them identical. Compare the UTC hour-of-day, not the full timestamp
        # (the two are on different calendar dates, so a full-timestamp delta
        # would fold in the months between them).
        summer = epr.parse_accepted_utc(
            '<div class="infoHead">Accepted</div><div class="info">2026-07-15 09:05:00</div>'
        )
        winter = epr.parse_accepted_utc(SAMPLE_INDEX_HEADER_ACCEPTED_WINTER)
        self.assertEqual(summer.hour, 13)  # 09:05 EDT -> 13:05 UTC
        self.assertEqual(winter.hour, 14)  # 09:05 EST -> 14:05 UTC

    def test_after_close_earnings_instant(self):
        # The #391 payoff: 16:05 ET earnings -> 20:05 UTC, an intraday instant
        # that competes in recency, NOT a 00:00 sink.
        ts = epr.parse_accepted_utc(SAMPLE_INDEX_HEADER_ACCEPTED_SUMMER)
        self.assertEqual(ts, pd.Timestamp("2026-07-31 20:05:00", tz="UTC"))

    def test_no_accepted_cell_returns_none(self):
        self.assertIsNone(epr.parse_accepted_utc(SAMPLE_INDEX_HEADER_NO_ACCEPTED))

    def test_table_only_index_returns_none(self):
        # The bare Document Format Files table (no header block) -> fallback.
        self.assertIsNone(epr.parse_accepted_utc(SAMPLE_INDEX_991))

    def test_empty_html_returns_none(self):
        self.assertIsNone(epr.parse_accepted_utc(""))

    def test_malformed_accepted_value_returns_none(self):
        # Anchor + digit-shaped value but not a real datetime -> None, not crash.
        html = '<div class="infoHead">Accepted</div><div class="info">2026-13-99 99:99:99</div>'
        self.assertIsNone(epr.parse_accepted_utc(html))

    def test_date_only_value_not_matched(self):
        # No HH:MM:SS -> regex miss -> None (guards a future header drift from
        # silently producing a wrong-precision value).
        html = '<div class="infoHead">Accepted</div><div class="info">2026-04-30</div>'
        self.assertIsNone(epr.parse_accepted_utc(html))

    def test_whitespace_tolerance(self):
        # Newline + indent between label and value (the live HTML shape) parses.
        ts = epr.parse_accepted_utc(SAMPLE_INDEX_HEADER_ACCEPTED)
        self.assertIsNotNone(ts)

    def test_value_is_tz_aware_utc(self):
        ts = epr.parse_accepted_utc(SAMPLE_INDEX_HEADER_ACCEPTED)
        self.assertIsNotNone(ts.tzinfo)
        self.assertEqual(str(ts.tz), "UTC")

    def test_tolerates_extra_attributes_on_info_div(self):
        # SEC markup drift: an added attribute on the value cell must NOT break
        # the parse (the regex tolerates extra attrs around class="info").
        html = (
            '<div class="infoHead">Accepted</div>'
            '<div class="info" id="accepted-cell">2026-04-30 16:30:41</div>'
        )
        self.assertEqual(
            epr.parse_accepted_utc(html), pd.Timestamp("2026-04-30 20:30:41", tz="UTC")
        )

    def test_bare_accepted_without_infohead_label_not_matched(self):
        # A stray "Accepted</div>" NOT in an infoHead cell must not false-match
        # (the regex is scoped to the header label).
        html = '<td>Accepted</div><div class="info">2026-04-30 16:30:41</div>'
        self.assertIsNone(epr.parse_accepted_utc(html))


class TestTransformAcceptanceTimestamp(unittest.TestCase):
    """transform() uses the acceptance UTC when present, else date-only 00:00."""

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

    def test_uses_acceptance_utc_when_present(self):
        accepted = pd.Timestamp("2026-05-30 20:30:41", tz="UTC")
        df = epr.transform(
            [self._hit(accepted_utc=accepted)],
            cik_to_ticker={"0000320193": "AAPL"},
            date=dt.date(2026, 5, 30),
        )
        self.assertEqual(df.iloc[0]["timestamp"], accepted)

    def test_falls_back_to_date_only_when_key_absent(self):
        # No accepted_utc key -> current behaviour: 00:00 UTC of filing_date.
        df = epr.transform(
            [self._hit()],
            cik_to_ticker={"0000320193": "AAPL"},
            date=dt.date(2026, 5, 30),
        )
        self.assertEqual(df.iloc[0]["timestamp"], pd.Timestamp("2026-05-30", tz="UTC"))

    def test_falls_back_when_acceptance_is_none(self):
        df = epr.transform(
            [self._hit(accepted_utc=None)],
            cik_to_ticker={"0000320193": "AAPL"},
            date=dt.date(2026, 5, 30),
        )
        self.assertEqual(df.iloc[0]["timestamp"], pd.Timestamp("2026-05-30", tz="UTC"))

    def test_acceptance_can_precede_filing_date_day(self):
        # After-close filing: filing_date rolled to the next day, but the
        # acceptance instant is the prior evening. transform honours the instant.
        accepted = pd.Timestamp("2026-04-30 23:51:52", tz="UTC")  # 19:51 EDT
        df = epr.transform(
            [self._hit(filing_date="2026-05-01", accepted_utc=accepted)],
            cik_to_ticker={"0000320193": "AAPL"},
            date=dt.date(2026, 5, 1),
        )
        self.assertEqual(df.iloc[0]["timestamp"], accepted)
        self.assertEqual(df.iloc[0]["timestamp"].date(), dt.date(2026, 4, 30))

    def test_mixed_frame_stays_tz_aware_utc(self):
        # One intraday-acceptance row + one date-only-fallback row -> single
        # tz-aware UTC dtype after the pd.to_datetime(..., utc=True) normalise.
        df = epr.transform(
            [
                self._hit(accepted_utc=pd.Timestamp("2026-05-30 20:30:41", tz="UTC")),
                self._hit(accession="0000320193-26-000051"),  # no accepted_utc
            ],
            cik_to_ticker={"0000320193": "AAPL"},
            date=dt.date(2026, 5, 30),
        )
        self.assertEqual(str(df["timestamp"].dt.tz), "UTC")


class TestEnrichFilingAcceptance(unittest.TestCase):
    """_enrich_filing reads acceptance from the already-fetched index.htm and
    issues NO extra SEC HTTP for it."""

    def _client(self, route):
        client = MagicMock()
        client.get_text.side_effect = route
        return client

    def _row(self):
        base = "https://www.sec.gov/Archives/edgar/data/320193/000032019326000050"
        return {
            "form_type": "8-K",
            "cik_padded": "0000320193",
            "accession": "0000320193-26-000050",
            "filing_date": "2026-05-30",
            "base_dir": base,
        }

    def test_acceptance_parsed_from_index_htm(self):
        def route(url):
            if url.endswith("-index.htm"):
                return SAMPLE_INDEX_991_WITH_ACCEPTED
            if url.endswith("primary.htm"):
                return SAMPLE_8K_HTML
            if url.endswith("ex991.htm"):
                return SAMPLE_EX991
            raise AssertionError(f"unexpected URL: {url}")

        hit = epr._enrich_filing(self._row(), client=self._client(route))
        self.assertIsNotNone(hit)
        self.assertEqual(hit["accepted_utc"], pd.Timestamp("2026-04-30 20:30:41", tz="UTC"))

    def test_no_extra_http_for_acceptance_time(self):
        calls = []

        def route(url):
            calls.append(url)
            if url.endswith("-index.htm"):
                return SAMPLE_INDEX_991_WITH_ACCEPTED
            if url.endswith("primary.htm"):
                return SAMPLE_8K_HTML
            if url.endswith("ex991.htm"):
                return SAMPLE_EX991
            raise AssertionError(f"unexpected URL: {url}")

        client = self._client(route)
        epr._enrich_filing(self._row(), client=client)
        # Exactly the three pre-#391 fetches: index.htm + primary 8-K + EX-99.1.
        self.assertEqual(client.get_text.call_count, 3)
        self.assertEqual(sum(u.endswith("-index.htm") for u in calls), 1)
        self.assertFalse(any(u.endswith(".txt") for u in calls))

    def test_missing_acceptance_yields_none_not_crash(self):
        def route(url):
            if url.endswith("-index.htm"):
                return SAMPLE_INDEX_991  # bare table, no header block
            if url.endswith("primary.htm"):
                return SAMPLE_8K_HTML
            if url.endswith("ex991.htm"):
                return SAMPLE_EX991
            raise AssertionError(f"unexpected URL: {url}")

        hit = epr._enrich_filing(self._row(), client=self._client(route))
        self.assertIsNotNone(hit)
        self.assertIsNone(hit["accepted_utc"])


class TestAcceptanceTimestampSurvivesRecencyCap(unittest.TestCase):
    """The #391 payoff, end-to-end through news_ingest.ingest_daily: an EX-99.1
    row with a real intraday acceptance instant survives the recency cap, where a
    00:00-UTC row is dropped. The midnight control pins that the intraday
    timestamp is the load-bearing change."""

    def setUp(self):
        from alphalens_pipeline.thematic import news_ingest as ni

        self.ni = ni
        self.date = dt.date(2026, 5, 30)

    def _edgar_frame(self, *, ts):
        return pd.DataFrame(
            [
                {
                    "id": "0000320193-26-000050",
                    "source": "edgar_press_release",
                    "timestamp": ts,
                    "tickers": ["AAPL"],
                    "title": "Apple Reports Record Q2 Revenue",
                    "body": "Apple today announced ...",
                    "url": "https://www.sec.gov/Archives/edgar/data/320193/x/idx-index.htm",
                    "keywords": [],
                    "extra": json.dumps({"accession": "0000320193-26-000050"}),
                }
            ],
            columns=NEWS_COLUMNS,
        )

    def _edgar_frame_named(self, *, id_, ts, title, url):
        return pd.DataFrame(
            [
                {
                    "id": id_,
                    "source": "edgar_press_release",
                    "timestamp": ts,
                    "tickers": ["AAPL"],
                    "title": title,
                    "body": "Apple today announced ...",
                    "url": url,
                    "keywords": [],
                    "extra": json.dumps({"accession": id_}),
                }
            ],
            columns=NEWS_COLUMNS,
        )

    def _polygon_fillers(self, *, n, base_ts):
        # n distinct headlines that must NOT lexically cluster: a single unique
        # token each (|∩|=0 < MIN_TOKEN_OVERLAP, mirroring test_caps_at_max_items),
        # so each is its own cluster. Each is strictly EARLIER than the EDGAR
        # acceptance instant but LATER than 00:00 UTC, so under the buggy 00:00
        # stamp EDGAR loses the cap tie.
        rows = []
        for i in range(n):
            rows.append(
                {
                    "id": f"poly-{i}",
                    "source": "polygon",
                    "timestamp": base_ts - pd.Timedelta(minutes=i + 1),
                    "tickers": [f"FILL{i}"],
                    "title": f"uniquefiller{i:04d}",
                    "body": f"Body {i}",
                    "url": f"https://example.com/poly/{i}",
                    "keywords": [],
                    "extra": "{}",
                }
            )
        return pd.DataFrame(rows, columns=NEWS_COLUMNS)

    def _run(self, *, edgar_ts, max_items, n_fillers):
        edgar_df = self._edgar_frame(ts=edgar_ts)
        polygon_df = self._polygon_fillers(
            n=n_fillers, base_ts=pd.Timestamp("2026-05-30 20:00:00", tz="UTC")
        )
        empty = self.ni.empty_news_frame()
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(self.ni, "_fetch_edgar_press_release", return_value=edgar_df),
                patch.object(self.ni, "_fetch_polygon", return_value=polygon_df),
                patch.object(self.ni, "_fetch_gdelt", return_value=empty),
                patch.object(self.ni, "_fetch_rss", return_value=empty),
            ):
                return self.ni.ingest_daily(
                    date=self.date,
                    cache_dir=Path(tmpdir),
                    max_items=max_items,
                    force=True,
                )

    def test_intraday_acceptance_row_survives_cap(self):
        out = self._run(
            edgar_ts=pd.Timestamp("2026-05-30 20:05:00", tz="UTC"),
            max_items=3,
            n_fillers=5,
        )
        self.assertIn("0000320193-26-000050", set(out["id"]))
        self.assertIn("edgar_press_release", set(out["source"]))

    def test_midnight_row_loses_intra_source_ranking_proving_the_bug(self):
        # Control: the OLD 00:00 stamp sorts the EDGAR row to the bottom of the
        # EDGAR source's own ranking. P1a's per-source quota guarantees EDGAR a
        # minimum slot (ceil(max_items*0.30)=1 at max_items=3), so to still prove
        # the timestamp is load-bearing we make TWO EDGAR rows compete for that
        # single quota slot: a newer intraday row and the buggy 00:00 row. The
        # newer row wins the slot; the 00:00 row is dropped — pinning that the
        # intraday acceptance timestamp is what saves a row, now at the
        # intra-source-ranking level.
        edgar_df = pd.concat(
            [
                self._edgar_frame(ts=pd.Timestamp("2026-05-30 00:00:00", tz="UTC")),
                self._edgar_frame_named(
                    id_="0000320193-26-000099",
                    ts=pd.Timestamp("2026-05-30 19:00:00", tz="UTC"),
                    title="Apple Issues Follow-up Statement",
                    url="https://www.sec.gov/Archives/edgar/data/320193/y/idx-index.htm",
                ),
            ],
            ignore_index=True,
        )
        polygon_df = self._polygon_fillers(
            n=5, base_ts=pd.Timestamp("2026-05-30 20:00:00", tz="UTC")
        )
        empty = self.ni.empty_news_frame()
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(self.ni, "_fetch_edgar_press_release", return_value=edgar_df),
                patch.object(self.ni, "_fetch_polygon", return_value=polygon_df),
                patch.object(self.ni, "_fetch_gdelt", return_value=empty),
                patch.object(self.ni, "_fetch_rss", return_value=empty),
            ):
                out = self.ni.ingest_daily(
                    date=self.date,
                    cache_dir=Path(tmpdir),
                    max_items=3,
                    force=True,
                )
        # The 00:00 row loses EDGAR's single quota slot to the 19:00 row.
        self.assertNotIn("0000320193-26-000050", set(out["id"]))
        self.assertIn("0000320193-26-000099", set(out["id"]))


if __name__ == "__main__":
    unittest.main()
