"""Tests for 8-K item-number extraction and primary-document selection."""
import json
import unittest
from unittest.mock import MagicMock, patch


class TestExtract8KItems(unittest.TestCase):
    def test_extracts_single_valid_item(self):
        from alphalens.watchdog.sources.eightk import extract_8k_items

        html = "<html><body><p>Item 2.02 Results of Operations</p></body></html>"
        self.assertEqual(extract_8k_items(html), ["2.02"])

    def test_extracts_multiple_items_sorted_and_deduped(self):
        from alphalens.watchdog.sources.eightk import extract_8k_items

        html = """
        <html><body>
          <p>Item 9.01 Financial Statements</p>
          <p>Item 2.04 Triggering Events</p>
          <p>Item 5.02 Departure of Directors</p>
          <p>Item 2.04 (reference again)</p>
        </body></html>
        """
        self.assertEqual(extract_8k_items(html), ["2.04", "5.02", "9.01"])

    def test_returns_empty_when_no_items(self):
        from alphalens.watchdog.sources.eightk import extract_8k_items

        self.assertEqual(extract_8k_items("<p>no items here at all</p>"), [])

    def test_whitelist_rejects_invalid_item_numbers(self):
        """Per SEC Form 8-K, items are enumerated: 1.01-1.04, 2.01-2.06, 3.01-3.03,
        4.01-4.02, 5.01-5.08, 6.01-6.05, 7.01, 8.01, 9.01. Anything outside that set
        (e.g. 'Item 10.1' in narrative cross-references) must be rejected."""
        from alphalens.watchdog.sources.eightk import extract_8k_items

        html = "<p>See Item 10.1 and Item 2.99 and Item 0.01 for details</p>"
        self.assertEqual(extract_8k_items(html), [])

    def test_handles_non_breaking_space_between_keyword_and_number(self):
        """SEC HTML frequently uses &nbsp; (\\xa0) between 'Item' and number."""
        from alphalens.watchdog.sources.eightk import extract_8k_items

        html = "<p>Item\xa02.02 Results of Operations</p>"
        self.assertEqual(extract_8k_items(html), ["2.02"])

    def test_handles_html_entities_and_case_variations(self):
        from alphalens.watchdog.sources.eightk import extract_8k_items

        html = "<p>ITEM 5.02</p>&nbsp;<span>item  4.02</span>"
        self.assertEqual(extract_8k_items(html), ["4.02", "5.02"])

    def test_handles_malformed_html(self):
        from alphalens.watchdog.sources.eightk import extract_8k_items

        html = "<bad<<<html>Item 2.02 results</p><broken"
        self.assertEqual(extract_8k_items(html), ["2.02"])


class TestPick8KPrimaryName(unittest.TestCase):
    def test_picks_file_with_8k_doctype(self):
        from alphalens.watchdog.sources.edgar import _pick_8k_primary_name

        # Real SEC FilingSummary.xml format (InputFiles section)
        summary = """<?xml version="1.0"?>
<FilingSummary>
  <InputFiles>
    <File doctype="8-K" original="qubt-20260305.htm">qubt-20260305.htm</File>
    <File>qubt-20260305.xsd</File>
    <File>qubt-20260305_lab.xml</File>
  </InputFiles>
</FilingSummary>"""
        self.assertEqual(_pick_8k_primary_name(summary), "qubt-20260305.htm")

    def test_picks_amendment_with_8k_a_doctype(self):
        from alphalens.watchdog.sources.edgar import _pick_8k_primary_name

        summary = """<?xml version="1.0"?>
<FilingSummary>
  <InputFiles>
    <File doctype="8-K/A" original="amendment.htm">amendment.htm</File>
  </InputFiles>
</FilingSummary>"""
        self.assertEqual(_pick_8k_primary_name(summary), "amendment.htm")

    def test_returns_none_when_no_file_has_8k_doctype(self):
        """Filing with only non-8-K documents (shouldn't happen for 8-K filings in
        practice but defensive against malformed summaries)."""
        from alphalens.watchdog.sources.edgar import _pick_8k_primary_name

        summary = """<?xml version="1.0"?>
<FilingSummary>
  <InputFiles>
    <File>schema.xsd</File>
    <File>labels.xml</File>
  </InputFiles>
</FilingSummary>"""
        self.assertIsNone(_pick_8k_primary_name(summary))

    def test_returns_none_on_malformed_xml(self):
        from alphalens.watchdog.sources.edgar import _pick_8k_primary_name

        self.assertIsNone(_pick_8k_primary_name("not valid xml <<"))
        self.assertIsNone(_pick_8k_primary_name(""))


class TestEnrich8K(unittest.TestCase):
    def _make_source(self, fetch_8k_details: bool):
        from alphalens.watchdog.config import WATCHDOG_DEFAULTS
        from alphalens.watchdog.sources.edgar import SECEdgarSource

        cfg = dict(WATCHDOG_DEFAULTS)
        cfg["user_agent"] = "test-agent"
        cfg["fetch_8k_details"] = fetch_8k_details
        store = MagicMock()
        cik_loader = MagicMock()
        source = SECEdgarSource(tickers=["QUBT"], config=cfg, store=store, cik_loader=cik_loader)
        return source

    def _make_event(self):
        from alphalens.watchdog.types import Event, FormType

        return Event(
            ticker="QUBT",
            form_type=FormType.FORM_8K,
            accession_number="0000000000-26-000001",
            filed_at="2026-03-05T09:00:00-05:00",
            url="https://www.sec.gov/Archives/edgar/data/1758009/000000000026000001/0000000000-26-000001-index.htm",
            raw_data={"title": "8-K - Current report"},
        )

    def test_enrich_8k_sets_items_on_raw_data(self):
        source = self._make_source(fetch_8k_details=True)
        event = self._make_event()

        summary = """<?xml version="1.0"?>
<FilingSummary><InputFiles>
  <File doctype="8-K" original="qubt-8k.htm">qubt-8k.htm</File>
</InputFiles></FilingSummary>"""
        html = "<html><body><p>Item 2.02 Results of Operations</p></body></html>"

        with patch.object(source, "_get", side_effect=[summary, html]):
            source._enrich_8k(event)

        self.assertEqual(event.raw_data.get("items"), ["2.02"])

    def test_enrich_8k_noop_when_primary_missing(self):
        source = self._make_source(fetch_8k_details=True)
        event = self._make_event()

        summary = """<?xml version="1.0"?>
<FilingSummary><InputFiles><File>schema.xsd</File></InputFiles></FilingSummary>"""
        with patch.object(source, "_get", return_value=summary):
            source._enrich_8k(event)

        self.assertNotIn("items", event.raw_data)


if __name__ == "__main__":
    unittest.main()
