"""Tests for MVP #2 enhancements to SECEdgarSource:

- 8-K item extraction from title
- Form 4 primary_doc.xml fetch + merge
- CIKLoader integration
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SAMPLE_ATOM_8K_WITH_ITEMS = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>8-K - Current report - Item 4.02 Non-reliance on previously issued financial statements, Item 2.02 Results of Operations</title>
    <link rel="alternate" type="text/html"
      href="https://www.sec.gov/Archives/edgar/data/320193/000032019326000020/0000320193-26-000020-index.htm"/>
    <updated>2026-04-17T12:30:00-04:00</updated>
    <category scheme="https://www.sec.gov/" label="form type" term="8-K"/>
    <id>urn:tag:sec.gov,2008:accession-number=0000320193-26-000020</id>
  </entry>
</feed>
"""

SAMPLE_ATOM_FORM4 = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>4 - Statement of changes in beneficial ownership of securities</title>
    <link rel="alternate" type="text/html"
      href="https://www.sec.gov/Archives/edgar/data/320193/000032019326000030/0000320193-26-000030-index.htm"/>
    <updated>2026-04-17T09:00:00-04:00</updated>
    <category scheme="https://www.sec.gov/" label="form type" term="4"/>
    <id>urn:tag:sec.gov,2008:accession-number=0000320193-26-000030</id>
  </entry>
</feed>
"""

SAMPLE_FORM4_PRIMARY_DOC = """<?xml version="1.0"?>
<ownershipDocument>
  <issuer><issuerTradingSymbol>AAPL</issuerTradingSymbol></issuer>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>2000</value></transactionShares>
        <transactionPricePerShare><value>175.00</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""


def _mock_resp(text, status=200):
    r = MagicMock()
    r.status_code = status
    r.text = text
    r.raise_for_status = MagicMock()
    return r


SAMPLE_INDEX_JSON = """{
  "directory": {
    "name": "/Archives/edgar/data/320193/000032019326000030/",
    "item": [
      {"name": "FilingSummary.xml", "type": "xml", "size": 100, "last-modified": "x"},
      {"name": "primary_doc.xml", "type": "xml", "size": 500, "last-modified": "x"}
    ]
  }
}"""


def _url_dispatch(atom_text, form4_xml_text, index_json_text=None):
    """Return a side_effect fn that dispatches by URL substring."""
    index_json_text = index_json_text or SAMPLE_INDEX_JSON

    def _dispatch(url, **kwargs):
        if "index.json" in url:
            return _mock_resp(index_json_text)
        if url.endswith(".xml") and "Archives" in url:
            return _mock_resp(form4_xml_text)
        return _mock_resp(atom_text)
    return _dispatch


class TestEdgarEnhanced(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "seen.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _source(self, config_overrides=None, tickers=None, ticker_to_cik=None, cik_loader=None):
        from alphalens.watchdog.config import WATCHDOG_DEFAULTS
        from alphalens.watchdog.sources.edgar import SECEdgarSource
        from alphalens.watchdog.storage import SeenEventStore

        cfg = dict(WATCHDOG_DEFAULTS)
        cfg["user_agent"] = "AlphaLens Test test@example.com"
        if config_overrides:
            cfg.update(config_overrides)

        return SECEdgarSource(
            tickers=tickers or ["AAPL"],
            config=cfg,
            store=SeenEventStore(self.db),
            ticker_to_cik=ticker_to_cik or {"AAPL": "0000320193"},
            cik_loader=cik_loader,
        )

    @patch("alphalens.watchdog.sources.edgar.requests.get")
    def test_8k_items_extracted_from_title(self, mock_get):
        mock_get.return_value = _mock_resp(SAMPLE_ATOM_8K_WITH_ITEMS)
        source = self._source()
        events = source.detect()

        self.assertEqual(len(events), 1)
        self.assertIn("items", events[0].raw_data)
        items = set(events[0].raw_data["items"])
        self.assertIn("4.02", items)
        self.assertIn("2.02", items)

    @patch("alphalens.watchdog.sources.edgar.requests.get")
    def test_form4_details_fetched_when_enabled(self, mock_get):
        mock_get.side_effect = _url_dispatch(SAMPLE_ATOM_FORM4, SAMPLE_FORM4_PRIMARY_DOC)
        source = self._source(config_overrides={"fetch_form4_details": True})
        events = source.detect()

        self.assertEqual(len(events), 1)
        evt = events[0]
        self.assertEqual(evt.raw_data.get("insider_action"), "BUY")
        self.assertAlmostEqual(evt.raw_data.get("transaction_value_usd", 0), 2000 * 175.0)

    @patch("alphalens.watchdog.sources.edgar.requests.get")
    def test_form4_details_not_fetched_when_disabled(self, mock_get):
        mock_get.return_value = _mock_resp(SAMPLE_ATOM_FORM4)
        source = self._source(config_overrides={"fetch_form4_details": False})
        source.detect()

        # Only the Atom feed call, no index.json or Form 4 XML fetch
        urls = [call.args[0] for call in mock_get.call_args_list]
        self.assertFalse(any("index.json" in u or "Archives" in u and u.endswith(".xml") for u in urls))

    @patch("alphalens.watchdog.sources.edgar.requests.get")
    def test_form4_fetch_failure_does_not_break_detect(self, mock_get):
        import requests as req_module

        def _side_effect(url, **kwargs):
            if "index.json" in url:
                raise req_module.ConnectionError("index down")
            return _mock_resp(SAMPLE_ATOM_FORM4)

        mock_get.side_effect = _side_effect
        source = self._source(config_overrides={"fetch_form4_details": True})
        events = source.detect()

        self.assertEqual(len(events), 1)
        # insider_action absent is fine — base event still emitted
        self.assertNotIn("insider_action", events[0].raw_data)

    @patch("alphalens.watchdog.sources.edgar.requests.get")
    def test_cik_loader_takes_precedence_over_dict(self, mock_get):
        from alphalens.watchdog.sources.cik_loader import CIKLoader

        mock_get.return_value = _mock_resp(SAMPLE_ATOM_FORM4)

        loader = MagicMock(spec=CIKLoader)
        loader.get_cik.return_value = "0000789019"

        source = self._source(
            tickers=["AAPL"],
            ticker_to_cik={"AAPL": "WRONG_CIK"},
            cik_loader=loader,
        )
        source.detect()

        loader.get_cik.assert_called_with("AAPL")
        # The mocked HTTP call should have used loader's CIK
        call_args = mock_get.call_args_list[0]
        params = call_args.kwargs.get("params", {})
        self.assertEqual(params.get("CIK"), "0000789019")


if __name__ == "__main__":
    unittest.main()
